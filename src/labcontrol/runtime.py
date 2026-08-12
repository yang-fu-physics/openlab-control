"""Qt/UI 线程与 asyncio 仪表运行时之间的总边界。

RuntimeService 在独立线程中拥有唯一 asyncio loop、InstrumentManager、SequenceEngine 和
MeasurementModuleService。UI 只通过线程安全提交与消息队列交互，避免 Qt 线程直接等待
仪表 I/O。关闭顺序集中在本文件，保证先停止 SEQ，再回收模块、仪表、报警线程和日志。
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from concurrent.futures import Future
from copy import deepcopy
from typing import Any

from .alarm_reporting import AlarmReporter
from .config import AppConfig
from .datafile import DatRunLogger
from .events import EventManager
from .models import EventNotice, RunProgress, RunState, RuntimeMessage, Severity
from .measurement.manifest import ModuleDescriptor, discover_modules
from .measurement.service import MeasurementModuleService
from .instruments.manifest import SystemInstrumentDescriptor, discover_system_instruments
from .instrument_manager import InstrumentManager
from .sequence.engine import SequenceEngine
from .sequence.model import SequenceDocument


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
        self.alarm_reporter = AlarmReporter(
            self.config.alarms.reporting,
            self.config.project_root,
            self._alarm_delivery_state,
        )
        self.events.subscribe(
            self.alarm_reporter.handle_notice
        )
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

    def _alarm_delivery_state(
        self,
        error: str | None,
    ) -> None:
        """把报警线程的结果安全切回 runtime loop，避免跨线程调用 EventManager。"""

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
            )
        except RuntimeError:
            return

    def _apply_alarm_delivery_state(
        self,
        error: str | None,
    ) -> None:
        """在 runtime 线程锁存或解除本地报警投递 Warning。"""

        if self.events is None:
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
    ) -> Future[Any]:
        assert self.instruments is not None
        return self._submit(
            self.instruments.set_target(
                instrument_id,
                value,
                rate_per_minute,
                mode,
                origin="manual",
            )
        )

    def hold_instrument(self, instrument_id: str) -> Future[Any]:
        assert self.instruments is not None
        return self._submit(self.instruments.hold_instrument(instrument_id, origin="manual"))

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

    def shutdown(self, timeout: float = 8.0) -> None:
        """从调用线程执行有总时限的关闭，并确认 runtime 线程真正退出。"""

        if self._loop is None or self._thread is None:
            return
        thread = self._thread
        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)
            try:
                future.result(timeout=timeout)
            except Exception:
                # 异步清理超出调用方总时限时先要求 loop 停止；之后仍 join 并在残留时
                # 明确抛错，不能静默留下后台线程。
                self._loop.call_soon_threadsafe(self._loop.stop)
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise TimeoutError(
                "OpenLab runtime thread did not stop within "
                f"{timeout:g} seconds"
            )
        self._thread = None
        self._loop = None

    async def _shutdown_async(self) -> None:
        """按安全依赖顺序关闭运行时。

        1. 请求 SEQ Stop，使正常路径有机会 Hold 并发送模块 run_end；
        2. 最多等待 3 秒，超时才取消 task；
        3. 停止轮询，避免清理期间又产生新仪表请求；
        4. 调用模块 close 并回收 worker；
        5. 断开温场和 Monitor；
        6. 有界关闭非关键报警线程，最后关闭日志和 event loop。
        """

        if self.engine is not None:
            self.engine.request_stop(False, "Application closing")
        sequence_task = self._sequence_task
        if sequence_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(sequence_task),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                # 取消是最后手段；SequenceEngine 的 CancelledError 分支仍会尝试 Hold。
                sequence_task.cancel()
                await asyncio.gather(
                    sequence_task,
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                sequence_task.cancel()
                await asyncio.gather(
                    sequence_task,
                    return_exceptions=True,
                )
                raise
            except Exception:
                await asyncio.gather(
                    sequence_task,
                    return_exceptions=True,
                )
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        if self.modules is not None:
            # 模块先于仪表断开，因为模块 close 仍需要自己的仪表连接；二者都在仪表
            # poll loop 停止后执行，避免新的状态轮询与关闭交叉。
            await self.modules.shutdown()
        if self.instruments is not None:
            await self.instruments.disconnect_all()
        if self.alarm_reporter is not None:
            reporter = self.alarm_reporter
            self.alarm_reporter = None
            await asyncio.to_thread(
                reporter.close,
                self.config.alarms.reporting.shutdown_timeout_seconds,
            )
        if self.logger is not None:
            self.logger.close()
        assert self._loop is not None
        self._loop.call_soon(self._loop.stop)
