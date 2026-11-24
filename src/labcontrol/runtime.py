"""Qt/UI 线程与 asyncio 仪表运行时之间的总边界。

RuntimeService 在独立线程中拥有唯一 asyncio loop、InstrumentManager、SequenceEngine 和
MeasurementModuleService。UI 只通过线程安全提交与消息队列交互，避免 Qt 线程直接等待
仪表 I/O。关闭顺序集中在本文件，保证先停止 SEQ，再回收模块、仪表、报警线程和日志。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import threading
import time
from collections.abc import Mapping
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import replace
from typing import Any

_LOGGER = logging.getLogger(__name__)

from .alarm_reporting import AlarmReporter
from .config import AppConfig, AlarmReportingConfig, validate_alarm_reporting
from .datafile import DatRunLogger
from .system_log import SystemLogRecorder
from .events import EventManager
from .models import EventNotice, RunProgress, RunState, RuntimeMessage, Severity
from .measurement.manifest import ModuleDescriptor, discover_modules
from .measurement.service import MeasurementModuleService
from .instruments.manifest import SystemInstrumentDescriptor, discover_system_instruments
from .instrument_manager import InstrumentManager
from .sequence.engine import SequenceEngine
from .sequence.model import SequenceDocument
from .system_instrument_commands import (
    configured_system_instrument_commands,
)


class RuntimeService:
    """在后台线程拥有完整异步运行时，并向 UI 暴露线程安全入口。"""

    def __init__(
        self,
        config: AppConfig,
        module_descriptors: tuple[ModuleDescriptor, ...] | None = None,
        instrument_descriptors: tuple[SystemInstrumentDescriptor, ...] | None = None,
    ) -> None:
        self.config = config
        self.messages: queue.Queue[RuntimeMessage] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._sequence_task: asyncio.Task[Any] | None = None
        self._poll_task: asyncio.Task[Any] | None = None
        self._system_log_task: asyncio.Task[None] | None = None
        self.system_log = SystemLogRecorder(config)
        self._logger_closed_during_shutdown = False
        self.events: EventManager | None = None
        self.instruments: InstrumentManager | None = None
        self.logger: DatRunLogger | None = None
        self.engine: SequenceEngine | None = None
        self.modules: MeasurementModuleService | None = None
        self.alarm_reporter: AlarmReporter | None = None
        self.module_descriptors = (
            discover_modules(config) if module_descriptors is None else module_descriptors
        )
        self.instrument_descriptors = (
            discover_system_instruments(config)
            if instrument_descriptors is None
            else instrument_descriptors
        )
        self.instrument_sequence_commands = (
            configured_system_instrument_commands(
                config,
                self.instrument_descriptors,
            )
        )

    def start(self, timeout: float = 10.0) -> None:
        """启动运行时线程并等待初始化阶段结束；不会无限卡住 UI。"""

        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name="OpenLabRuntime", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("Instrument runtime startup timed out")

    def _thread_main(self) -> None:
        """运行时线程入口：创建、运行并最终销毁该线程专属 event loop。"""

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self.events = EventManager(
            popup_warnings=self.config.alarms.popup_warnings,
            popup_errors=self.config.alarms.popup_errors,
        )
        self.events.subscribe(self._on_event)
        self.alarm_reporter = self._new_alarm_reporter(self.config.alarms.reporting)
        self.events.subscribe(self._report_alarm_notice)
        # 报警线程先启动，但它只订阅事件，不参与任何仪表安全决策。
        self.alarm_reporter.start()
        try:
            self.instruments = InstrumentManager(
                self.config,
                self.events,
                self.instrument_descriptors,
            )
            self.logger = DatRunLogger(self.config, self.events)
            self.modules = MeasurementModuleService(
                self.module_descriptors,
                self.events,
                self.instruments,
                message_callback=self._on_module_message,
            )
            self.engine = SequenceEngine(
                self.config,
                self.instruments,
                self.events,
                self.logger,
                self.modules,
                progress_callback=self._on_progress,
                instrument_sequence_commands=(
                    self.instrument_sequence_commands
                ),
            )
            loop.run_until_complete(self.instruments.connect_all())
            initial_snapshots = loop.run_until_complete(
                self.instruments.poll_all()
            )
            self.messages.put(
                RuntimeMessage("snapshots", initial_snapshots)
            )
            self._poll_task = loop.create_task(self._poll_loop())
        except Exception as exc:
            self.messages.put(RuntimeMessage("startup_error", str(exc)))
        finally:
            self._ready.set()
        try:
            loop.run_forever()
        finally:
            # 正常 shutdown 应已逐项清理；这里再取消残余 task，作为 event loop 关闭的
            # 最后兜底，避免 Python 报 “Task was destroyed but pending”。
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def _poll_loop(self) -> None:
        assert self.instruments is not None
        # 启动阶段已经发布过一次初始快照。运行 SEQ 时可以更快采样用于独立判稳，
        # 但前面板与 Live Trend 仍按常规周期接收消息，避免把控制采样频率变成界面刷新率。
        last_published = time.monotonic()
        while True:
            await asyncio.sleep(self._instrument_poll_interval())
            try:
                snapshots = await self.instruments.poll_all()
                if self.logger is not None:
                    try:
                        self.logger.write_instrument_status(
                            snapshots
                        )
                    except Exception as exc:
                        if self.events is not None:
                            self.events.report(
                                Severity.ERROR,
                                "logging",
                                "INSTRUMENT_STATUS_WRITE_FAILED",
                                str(exc),
                                str(
                                    self.logger.paths.instrument_status_file
                                    if self.logger.paths is not None
                                    else ""
                                ),
                            )
                now = time.monotonic()
                if now - last_published >= self.config.poll_interval_seconds:
                    self.messages.put(RuntimeMessage("snapshots", snapshots))
                    last_published = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.events is not None:
                    self.events.report(Severity.ERROR, "runtime", "POLL_LOOP_FAILED", str(exc))

    def _instrument_poll_interval(self) -> float:
        """返回当前仪表采样周期；前面板消息仍由常规周期单独节流。"""

        if self.engine is not None and self.engine.state in {
            RunState.RUNNING,
            RunState.PAUSED,
            RunState.STOPPING,
        }:
            return self.config.control_poll_interval_seconds
        return self.config.poll_interval_seconds

    def _on_event(self, notice: EventNotice) -> None:
        self.messages.put(RuntimeMessage("event", notice))

    def _on_progress(self, progress: RunProgress) -> None:
        self.messages.put(RuntimeMessage("progress", progress))

    def _on_module_message(self, kind: str, payload: dict[str, Any]) -> None:
        self.messages.put(RuntimeMessage(kind, payload))

    def _new_alarm_reporter(self, config: AlarmReportingConfig) -> AlarmReporter:
        reporter = AlarmReporter(config, self.config.project_root,
                                 lambda error: self._alarm_delivery_state(error, reporter))
        return reporter

    def _report_alarm_notice(self, notice: EventNotice) -> None:
        if self.alarm_reporter is not None:
            self.alarm_reporter.handle_notice(notice)

    def configure_alarm_reporting(self, config: AlarmReportingConfig) -> Future[Any]:
        return self._submit(self._configure_alarm_reporting(config))

    async def _configure_alarm_reporting(self, config: AlarmReportingConfig) -> AlarmReportingConfig:
        config = validate_alarm_reporting(config)
        reporter = self._new_alarm_reporter(config)
        token = await asyncio.to_thread(reporter._load_token) if reporter.enabled else None
        reporter.start(token=token)
        previous = self.alarm_reporter
        self.alarm_reporter = reporter
        self.config = replace(self.config, alarms=replace(self.config.alarms, reporting=config))
        if previous is not None:
            assert self.events is not None
            self.events.resolve("alarm_reporter", "ALARM_DELIVERY_FAILED", previous.config.endpoint)
            await asyncio.to_thread(previous.close)
        return config

    def test_alarm_reporting(self, config: AlarmReportingConfig, token: str) -> Future[Any]:
        config = validate_alarm_reporting(replace(config, enabled=True))
        reporter = AlarmReporter(config, self.config.project_root)
        return self._submit(asyncio.to_thread(reporter.test_delivery, token))

    def _alarm_delivery_state(
        self,
        error: str | None,
        reporter: AlarmReporter | None = None,
    ) -> None:
        """把报警线程的结果安全切回 runtime loop，避免跨线程调用 EventManager。"""

        if reporter is not None and reporter is not self.alarm_reporter:
            return
        loop = self._loop
        if (
            loop is None
            or loop.is_closed()
        ):
            return
        try:
            loop.call_soon_threadsafe(
                self._apply_alarm_delivery_state,
                error,
                reporter,
            )
        except RuntimeError:
            return

    def _apply_alarm_delivery_state(
        self,
        error: str | None,
        reporter: AlarmReporter | None = None,
    ) -> None:
        """在 runtime 线程锁存或解除本地报警投递 Warning。"""

        if self.events is None or (reporter is not None and reporter is not self.alarm_reporter):
            return
        if error is None:
            self.events.resolve(
                "alarm_reporter",
                "ALARM_DELIVERY_FAILED",
                self.config.alarms.reporting.endpoint,
            )
            return
        self.events.report(
            Severity.WARNING,
            "alarm_reporter",
            "ALARM_DELIVERY_FAILED",
            error,
            self.config.alarms.reporting.endpoint,
        )

    def drain_messages(self, maximum: int = 500) -> list[RuntimeMessage]:
        result: list[RuntimeMessage] = []
        for _ in range(maximum):
            try:
                result.append(self.messages.get_nowait())
            except queue.Empty:
                break
        return result

    def _submit(self, coroutine: Any) -> Future[Any]:
        """从 UI/调用线程把协程提交到唯一 runtime loop。"""

        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("Instrument runtime has not started")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def start_system_log(self, path: str, interval_seconds: int, fields: tuple[str, ...]) -> Future[Any]:
        return self._submit(self._start_system_log(path, interval_seconds, tuple(fields)))

    async def _start_system_log(self, path: str, interval_seconds: int, fields: tuple[str, ...]) -> dict[str, object]:
        assert self.instruments is not None
        state = self.system_log.start(path, interval_seconds, fields, self.instruments.snapshots())
        self._system_log_task = asyncio.create_task(self._system_log_loop())
        self.messages.put(RuntimeMessage("system_log_state", state))
        return state

    def stop_system_log(self) -> Future[Any]:
        return self._submit(self._stop_system_log())

    async def _stop_system_log(self) -> dict[str, object]:
        task, self._system_log_task = self._system_log_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            self.system_log.close()
        finally:
            self.messages.put(RuntimeMessage("system_log_state", self.system_log.state()))
        return self.system_log.state()

    async def _system_log_loop(self) -> None:
        assert self.instruments is not None
        assert self.events is not None
        while True:
            await asyncio.sleep(self.system_log.interval_seconds)
            try:
                # Only copy the existing cache. Never poll or change polling intervals here.
                self.system_log.write(self.instruments.snapshots())
            except OSError as exc:
                error = str(exc)
                try:
                    self.system_log.close()
                except OSError as close_error:
                    error += f"; close failed: {close_error}"
                state = self.system_log.state()
                state["error"] = error
                self.messages.put(RuntimeMessage("system_log_state", state))
                self.events.report(Severity.WARNING, "system_log", "SYSTEM_LOG_WRITE_FAILED", error, str(self.system_log.path))
                return

    def run_sequence(
        self,
        document: SequenceDocument,
        module_settings: dict[str, dict[str, object]] | None = None,
    ) -> Future[Any]:
        return self._submit(
            self._run_sequence(deepcopy(document), deepcopy(module_settings or {}))
        )

    async def _run_sequence(
        self,
        document: SequenceDocument,
        module_settings: dict[str, dict[str, object]],
    ) -> Any:
        """持有 sequence control lease 运行 SEQ，并在任何退出路径释放 lease。"""

        if self._sequence_task is not None and not self._sequence_task.done():
            raise RuntimeError("A sequence is already running")
        assert self.engine is not None
        assert self.instruments is not None
        # lease 让手动 Set/Hold 在 SEQ 期间被运行时拒绝，不能只依赖 UI 按钮变灰。
        self.instruments.acquire_sequence_control()
        self._sequence_task = asyncio.create_task(
            self.engine.run(document, module_settings)
        )
        try:
            return await self._sequence_task
        finally:
            self._sequence_task = None
            self.instruments.release_sequence_control()

    def pause_sequence(self) -> None:
        if self._loop is not None and self.engine is not None:
            self._loop.call_soon_threadsafe(self.engine.pause)

    def resume_sequence(self) -> None:
        if self._loop is not None and self.engine is not None:
            self._loop.call_soon_threadsafe(self.engine.resume)

    def stop_sequence(self) -> None:
        if self._loop is not None and self.engine is not None:
            self._loop.call_soon_threadsafe(self.engine.request_stop, False, "Stopped by user")

    def set_target(
        self,
        instrument_id: str,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        control: str,
    ) -> Future[Any]:
        assert self.instruments is not None
        return self._submit(
            self.instruments.set_target(
                instrument_id,
                value,
                rate_per_minute,
                mode,
                control=control,
                origin="manual",
            )
        )

    def hold_instrument(
        self,
        instrument_id: str,
        *,
        control: str,
    ) -> Future[Any]:
        assert self.instruments is not None
        return self._submit(
            self.instruments.hold_instrument(
                instrument_id,
                control=control,
                origin="manual",
            )
        )

    def instrument_action(
        self,
        instrument_id: str,
        command_id: str,
    ) -> Future[Any]:
        assert self.instruments is not None
        return self._submit(
            self.instruments.execute_sequence_command(
                instrument_id,
                command_id,
                origin="manual",
            )
        )

    def execute_instrument_action(
        self,
        instrument_id: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        read_only: bool = False,
    ) -> Future[dict[str, Any]]:
        assert self.instruments is not None
        return self._submit(
            self.instruments.execute_action(
                instrument_id,
                action,
                deepcopy(dict(payload or {})),
                read_only=read_only,
            )
        )

    def enable_module(self, module_id: str) -> Future[Any]:
        assert self.modules is not None
        return self._submit(self.modules.enable(module_id))

    def disable_module(self, module_id: str) -> Future[Any]:
        assert self.modules is not None
        return self._submit(self.modules.disable(module_id))

    def apply_module_settings(
        self, module_id: str, settings: dict[str, object]
    ) -> Future[Any]:
        assert self.modules is not None
        return self._submit(self.modules.apply_settings(module_id, settings))

    def refresh_module_status(self, module_id: str) -> Future[Any]:
        assert self.modules is not None
        return self._submit(self.modules.refresh_status(module_id))

    def module_action(
        self, module_id: str, name: str, payload: dict[str, object]
    ) -> Future[Any]:
        assert self.modules is not None
        return self._submit(self.modules.action(module_id, name, payload))

    def replace_module_descriptors(
        self, descriptors: tuple[ModuleDescriptor, ...]
    ) -> Future[Any]:
        return self._submit(self._replace_module_descriptors(descriptors))

    async def _replace_module_descriptors(
        self, descriptors: tuple[ModuleDescriptor, ...]
    ) -> None:
        assert self.modules is not None
        self.modules.replace_descriptors(descriptors)
        self.module_descriptors = descriptors

    def add_module_descriptor(
        self, descriptor: ModuleDescriptor
    ) -> Future[Any]:
        return self._submit(self._add_module_descriptor(descriptor))

    async def _add_module_descriptor(
        self, descriptor: ModuleDescriptor
    ) -> None:
        assert self.modules is not None
        self.modules.add_descriptor(descriptor)
        self.module_descriptors = (*self.module_descriptors, descriptor)

    def remove_module_descriptor(self, module_id: str) -> Future[Any]:
        return self._submit(self._remove_module_descriptor(module_id))

    async def _remove_module_descriptor(self, module_id: str) -> None:
        assert self.modules is not None
        self.modules.remove_descriptor(module_id)
        self.module_descriptors = tuple(
            item for item in self.module_descriptors if item.id != module_id
        )

    def inject_event(self, severity: Severity, code: str, message: str) -> None:
        if self._loop is None or self.events is None:
            return
        self._loop.call_soon_threadsafe(
            self.events.report, severity, "simulation", code, message, "manual"
        )

    def resolve_event(self, source: str, code: str, context: str = "") -> None:
        if self._loop is None or self.events is None:
            return
        self._loop.call_soon_threadsafe(self.events.resolve, source, code, context)

    def reset_event_response(self, event_key: str) -> Future[Any]:
        """在源事件解除后请求释放对应的 System Instrument 响应锁。"""

        assert self.instruments is not None
        return self._submit(
            self._reset_event_response(event_key)
        )

    async def _reset_event_response(self, event_key: str) -> None:
        assert self.instruments is not None
        self.instruments.reset_event_response(event_key)

    def shutdown(self, timeout: float = 8.0) -> None:
        """从调用线程执行有总时限的关闭，并确认 runtime 线程真正退出。"""

        if self._loop is None or self._thread is None:
            return
        thread = self._thread
        deadline = time.monotonic() + timeout
        shutdown_error: Exception | None = None
        self._logger_closed_during_shutdown = False
        if self._loop.is_running():
            async_deadline = deadline - min(0.25, timeout * 0.05)
            future = asyncio.run_coroutine_threadsafe(
                self._shutdown_async(deadline=async_deadline),
                self._loop,
            )
            try:
                future.result(timeout=timeout)
            except Exception as exc:
                shutdown_error = exc
                # 异步清理超出调用方总时限或报告清理异常时确保 loop 停止；之后仍 join
                # 并在残留时明确抛错，不能静默留下后台线程或吞掉清理错误。
                if self._loop.is_running():
                    try:
                        self._loop.call_soon_threadsafe(self._loop.stop)
                    except RuntimeError:
                        pass
        join_timeout = max(0.5, deadline - time.monotonic())
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            if self.logger is not None and not self._logger_closed_during_shutdown:
                try:
                    self.logger.close()
                    self._logger_closed_during_shutdown = True
                except Exception as exc:
                    _LOGGER.error("Fallback logger.close failed: %s", exc)
            raise TimeoutError(
                "OpenLab runtime thread did not stop within "
                f"{timeout:g} seconds"
            )
        if self.logger is not None and not self._logger_closed_during_shutdown:
            try:
                self.logger.close()
                self._logger_closed_during_shutdown = True
            except Exception as exc:
                if shutdown_error is None:
                    shutdown_error = exc
        self._thread = None
        self._loop = None
        if shutdown_error is not None:
            raise shutdown_error

    @staticmethod
    def _accepts_timeout_keyword(func: Any) -> bool:
        try:
            parameters = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return False
        return "timeout_seconds" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    @staticmethod
    def _discard_abandoned_task_exception(task: asyncio.Future[Any]) -> None:
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    @classmethod
    async def _await_cancelled_task(
        cls,
        task: asyncio.Task[Any],
        timeout_seconds: float,
    ) -> None:
        """取消已有任务并在硬时限内等待其结束；超时后不再滞留。"""

        task.cancel()
        done, _ = await asyncio.wait(
            {task},
            timeout=max(0.01, timeout_seconds),
        )
        if task in done:
            cls._discard_abandoned_task_exception(task)
            return
        task.add_done_callback(cls._discard_abandoned_task_exception)
        raise TimeoutError(
            f"Cancelled task did not stop within {timeout_seconds:.2f} s"
        )

    @classmethod
    async def _await_bounded(
        cls,
        awaitable: Any,
        timeout_seconds: float,
        *,
        cancel_grace: float = 0.15,
    ) -> Any:
        """硬边界等待协程或 Future；超时取消后仅等待有限 cancel_grace，绝不被滞留 finally 无限拖延。"""

        task = asyncio.ensure_future(awaitable)
        done, _ = await asyncio.wait(
            {task},
            timeout=max(0.01, timeout_seconds),
        )
        if task in done:
            return task.result()
        task.cancel()
        if cancel_grace > 0.0:
            done_after, _ = await asyncio.wait({task}, timeout=cancel_grace)
            if task in done_after:
                cls._discard_abandoned_task_exception(task)
                raise TimeoutError(
                    f"Operation timed out after {timeout_seconds:.2f} s"
                )
        task.add_done_callback(cls._discard_abandoned_task_exception)
        raise TimeoutError(
            f"Operation timed out after {timeout_seconds:.2f} s"
        )

    async def _shutdown_async(
        self,
        *,
        deadline: float | None = None,
    ) -> None:
        """按安全依赖顺序在共享总时限内关闭运行时。

        1. 按调用方总 deadline 为模块关闭、仪表断开、报警收尾与日志关闭预留预算；
        2. 请求 SEQ Stop 并在其分片预算内等待或取消；
        3. 停止轮询，避免清理期间又产生新仪表请求；
        4. 有界关闭模块并回收 worker，再有界断开系统仪表；
        5. 有界关闭报警线程，在关闭日志前记录任何清理错误，最后关闭日志与 event loop；
        6. 若任一步骤超时或失败，完成全部剩余清理后抛出聚合错误，绝不静默吞错。
        """

        now = time.monotonic()
        total_budget = (
            max(0.5, deadline - now)
            if deadline is not None
            else 8.0
        )
        effective_deadline = now + total_budget

        def remaining() -> float:
            return max(0.0, effective_deadline - time.monotonic())

        logger_reserve = (
            min(0.15, total_budget * 0.05)
            if self.logger is not None
            else 0.0
        )
        alarm_reserve = (
            min(
                0.3,
                float(self.config.alarms.reporting.shutdown_timeout_seconds),
                total_budget * 0.08,
            )
            if self.alarm_reporter is not None
            else 0.0
        )
        instrument_reserve = (
            min(3.5, total_budget * 0.44)
            if self.instruments is not None
            else 0.0
        )
        module_reserve = (
            min(
                float(self.config.modules.shutdown_timeout_seconds),
                total_budget * 0.34,
            )
            if self.modules is not None
            else 0.0
        )

        cleanup_errors: list[str] = []
        seq_budget = min(
            3.0,
            max(
                0.2,
                remaining()
                - module_reserve
                - instrument_reserve
                - alarm_reserve
                - logger_reserve,
            ),
        )
        seq_deadline = time.monotonic() + seq_budget
        if self.engine is not None:
            mark_shutting_down = getattr(
                self.engine,
                "mark_shutting_down",
                None,
            )
            if callable(mark_shutting_down):
                mark_shutting_down(
                    timeout_seconds=max(0.1, seq_budget * 0.45),
                )
            self.engine.request_stop(False, "Application closing")
        sequence_task = self._sequence_task
        try:
            if sequence_task is not None:
                graceful_wait = max(0.05, seq_budget * 0.55)
                try:
                    done, _ = await asyncio.wait(
                        {sequence_task},
                        timeout=graceful_wait,
                    )
                    if sequence_task in done:
                        if not sequence_task.cancelled():
                            exc = sequence_task.exception()
                            if exc is not None:
                                cleanup_errors.append(
                                    f"sequence task: {type(exc).__name__}: {exc}"
                                )
                    else:
                        # 取消是最后手段；SequenceEngine 仍会以有界关闭超时发送模块 run_end，但不控制仪表。
                        post_cancel_wait = max(
                            0.1,
                            seq_deadline - time.monotonic(),
                        )
                        try:
                            await self._await_cancelled_task(
                                sequence_task,
                                post_cancel_wait,
                            )
                        except TimeoutError:
                            cleanup_errors.append(
                                "sequence stop did not complete within shutdown budget"
                            )
                except asyncio.CancelledError:
                    post_cancel_wait = max(
                        0.1,
                        seq_deadline - time.monotonic(),
                    )
                    try:
                        await self._await_cancelled_task(
                            sequence_task,
                            post_cancel_wait,
                        )
                    except TimeoutError:
                        cleanup_errors.append(
                            "sequence cancellation did not complete within shutdown budget"
                        )
                    raise
                except Exception as exc:
                    cleanup_errors.append(
                        f"sequence task: {type(exc).__name__}: {exc}"
                    )
        finally:
            try:
                if self._poll_task is not None:
                    try:
                        await self._await_cancelled_task(
                            self._poll_task,
                            min(0.25, max(0.05, remaining())),
                        )
                    except Exception as exc:
                        cleanup_errors.append(
                            f"poll task cancellation: {type(exc).__name__}: {exc}"
                        )
                try:
                    await self._stop_system_log()
                except OSError as exc:
                    cleanup_errors.append(f"system_log.close: {exc}")
                if self.modules is not None:
                    # 模块先于仪表断开，因为模块 close 仍需要自己的仪表连接；二者都在仪表
                    # poll loop 停止后执行，避免新的状态轮询与关闭交叉。
                    module_budget = max(
                        0.15,
                        remaining()
                        - instrument_reserve
                        - alarm_reserve
                        - logger_reserve,
                    )
                    try:
                        if self._accepts_timeout_keyword(self.modules.shutdown):
                            coro = self.modules.shutdown(
                                timeout_seconds=module_budget,
                            )
                        else:
                            coro = self.modules.shutdown()
                        await self._await_bounded(
                            coro,
                            module_budget,
                            cancel_grace=min(0.15, max(0.05, remaining() * 0.1)),
                        )
                    except Exception as exc:
                        cleanup_errors.append(
                            f"modules.shutdown: {type(exc).__name__}: {exc}"
                        )
                if self.instruments is not None:
                    instrument_budget = max(
                        0.15,
                        remaining() - alarm_reserve - logger_reserve,
                    )
                    try:
                        if self._accepts_timeout_keyword(
                            self.instruments.disconnect_all
                        ):
                            coro = self.instruments.disconnect_all(
                                timeout_seconds=instrument_budget,
                            )
                        else:
                            coro = self.instruments.disconnect_all()
                        await self._await_bounded(
                            coro,
                            instrument_budget,
                            cancel_grace=min(0.15, max(0.05, remaining() * 0.1)),
                        )
                    except Exception as exc:
                        cleanup_errors.append(
                            f"instruments.disconnect_all: {type(exc).__name__}: {exc}"
                        )
                if self.alarm_reporter is not None:
                    reporter = self.alarm_reporter
                    self.alarm_reporter = None
                    alarm_budget = min(
                        float(self.config.alarms.reporting.shutdown_timeout_seconds),
                        max(0.05, remaining() - logger_reserve),
                    )
                    try:
                        await self._await_bounded(
                            asyncio.to_thread(reporter.close, alarm_budget),
                            alarm_budget + 0.1,
                            cancel_grace=0.05,
                        )
                    except Exception as exc:
                        cleanup_errors.append(
                            f"alarm_reporter.close: {type(exc).__name__}: {exc}"
                        )
                if cleanup_errors and self.events is not None:
                    try:
                        self.events.report(
                            Severity.ERROR,
                            "runtime",
                            "RUNTIME_SHUTDOWN_CLEANUP_FAILED",
                            "; ".join(cleanup_errors),
                        )
                    except Exception as exc:
                        cleanup_errors.append(
                            f"events.report: {type(exc).__name__}: {exc}"
                        )
                if self.logger is not None:
                    try:
                        self.logger.close()
                        self._logger_closed_during_shutdown = True
                    except Exception as exc:
                        cleanup_errors.append(
                            f"logger.close: {type(exc).__name__}: {exc}"
                        )
            finally:
                if self._loop is not None:
                    self._loop.call_soon(self._loop.stop)
        if cleanup_errors:
            summary = "; ".join(cleanup_errors)
            _LOGGER.error("Runtime shutdown completed with cleanup errors: %s", summary)
            raise RuntimeError(
                f"Runtime shutdown completed with cleanup errors: {summary}"
            )
