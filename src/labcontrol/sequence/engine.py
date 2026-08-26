"""SEQ 的执行状态机、任意嵌套扫描和 Pause/Stop 安全检查点。

SequenceEngine 只在核心 asyncio 线程运行。所有耗时等待都基于“活动逻辑时间”，主动
扣除用户 Pause 和主仪表重连等待，因此恢复后不会把暂停时间误判为 dwell/settle 已完成
或稳定超时。

Stop/Error 都沿协作路径退出当前命令，但不改变任何 System Instrument 的目标或状态。
强制取消仅用于应用关闭超时；所有退出路径仍会发送 Measurement Module 的 run_end。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from pathlib import Path

from ..config import AppConfig, InstrumentPanelConfig
from ..datafile import DatRunLogger
from ..instruments.base import InstrumentError
from ..events import EventManager
from ..formatting import control_decimals, fixed_number
from ..models import InstrumentKind, EventNotice, RunProgress, RunState, Severity, StabilityState
from ..measurement.service import MeasurementModuleService
from ..instrument_manager import InstrumentManager
from ..units import convert_value
from .model import (
    Command,
    CommandType,
    SequenceDocument,
    SystemInstrumentCommandSpec,
    validate_command_parameters,
)
from .parser import format_command, load_sequence, parse_temperature_points, serialize_sequence


class SequenceAbort(RuntimeError):
    """内部控制流：把用户 Stop 或已锁存 Error 从任意检查点带回 run()。"""


ProgressCallback = Callable[[RunProgress], None]


class SequenceEngine:
    """执行一个已解析的 SEQ，并协调仪表、模块、DAT 和事件状态。"""

    def __init__(
        self,
        config: AppConfig,
        instruments: InstrumentManager,
        events: EventManager,
        logger: DatRunLogger,
        modules: MeasurementModuleService | None = None,
        progress_callback: ProgressCallback | None = None,
        instrument_sequence_commands: tuple[
            SystemInstrumentCommandSpec,
            ...,
        ] = (),
    ) -> None:
        self.config = config
        self.instruments = instruments
        self.events = events
        self.logger = logger
        self.modules = modules or MeasurementModuleService((), events, instruments)
        self.progress_callback = progress_callback or (lambda _: None)
        self.instrument_sequence_commands = instrument_sequence_commands
        self.state = RunState.IDLE
        # gate 被 clear 时，所有经过 _checkpoint 的 SEQ 调度都停住。它不会主动关闭
        # 模块输出或仪表连接；这些物理策略由具体模块/仪表定义。
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        # 以下累计量共同构成逻辑时钟：Pause 和主仪表恢复等待都不计入实验计时。
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._instrument_wait_total = 0.0
        self._waiting_for_instruments = False
        self._abort_requested = False
        self._fatal_abort = False
        self._abort_message = ""
        self._completed_steps = 0
        self._total_steps = 0
        self._current_path = ""
        self._call_stack: list[Path] = []
        self.events.subscribe_occurrences(self._on_event)

    def _on_event(self, notice: EventNotice) -> None:
        """把运行期间任意 Error 统一转换为 fatal Stop 请求。

        订阅 occurrence 而非只订阅“首次锁存”，因此一个在 Idle 已存在、运行中再次发生
        的 Error 仍能终止当前 SEQ。
        """

        if (
            not notice.is_resolution
            and notice.event.severity is Severity.ERROR
            and self.state in (RunState.RUNNING, RunState.PAUSED)
        ):
            self.request_stop(fatal=True, message=notice.event.message)

    async def run(
        self,
        document: SequenceDocument,
        module_settings: dict[str, dict[str, object]] | None = None,
    ) -> RunState:
        """执行一次完整 Run，并保证所有退出路径都经过模块收尾和日志关闭。"""

        if self.state in (RunState.RUNNING, RunState.PAUSED, RunState.STOPPING):
            raise RuntimeError("A sequence is already running")
        self._abort_requested = False
        self._fatal_abort = False
        self._abort_message = ""
        self._pause_gate.set()
        self._paused_at = None
        self._paused_total = 0.0
        self._instrument_wait_total = 0.0
        self._waiting_for_instruments = False
        self._completed_steps = 0
        self._call_stack.clear()
        if document.path is not None:
            self._call_stack.append(document.path.resolve())
        # 在连接仪表、创建运行目录或移动第一个设定点之前递归验证全部参数，防止后段的
        # 非法点让实验只执行一半。
        invalid_parameter = self._find_invalid_parameter(document.commands)
        if invalid_parameter is not None:
            command, parameter_issues = invalid_parameter
            self.state = RunState.FAULTED
            self._fatal_abort = True
            self._abort_message = (
                "Invalid sequence parameter: " + "; ".join(parameter_issues)
            )
            self.events.report(
                Severity.ERROR,
                "sequence",
                "INVALID_SEQUENCE_PARAMETER",
                self._abort_message,
                command.raw_text or command.type.value,
            )
            self._publish(self._abort_message)
            return self.state
        base_directory = (
            self._call_stack[-1].parent
            if self._call_stack
            else self.config.project_root
        )
        invalid_dynamic_command = self._find_invalid_dynamic_command(
            document.commands,
            base_directory,
            frozenset(self._call_stack),
        )
        if invalid_dynamic_command is not None:
            command, issues = invalid_dynamic_command
            instrument_command = (
                command.type is CommandType.INSTRUMENT_COMMAND
            )
            self.state = RunState.FAULTED
            self._fatal_abort = True
            self._abort_message = (
                (
                    "Invalid System Instrument sequence command: "
                    if instrument_command
                    else "Invalid module sequence command: "
                )
                + "; ".join(issues)
            )
            self.events.report(
                Severity.ERROR,
                "sequence",
                (
                    "INSTRUMENT_SEQUENCE_COMMAND_INVALID"
                    if instrument_command
                    else "MODULE_SEQUENCE_COMMAND_INVALID"
                ),
                self._abort_message,
                format_command(command, preserve_raw=False),
            )
            self._publish(self._abort_message)
            return self.state
        self._total_steps = max(
            self._estimate_execution_steps(
                document.commands,
                base_directory,
                frozenset(self._call_stack),
            ),
            1,
        )
        try:
            # 新鲜读回预检确保 primary 温度/磁场可用，不能仅凭缓存的 Connected UI
            # 状态开始 Run。
            self.instruments.ensure_run_ready()
        except InstrumentError as exc:
            self.state = RunState.FAULTED
            self._fatal_abort = True
            self._abort_message = str(exc)
            self.events.report(
                Severity.ERROR,
                "sequence",
                exc.code,
                str(exc),
                exc.context,
            )
            self._publish(self._abort_message)
            return self.state
        self.state = RunState.RUNNING
        try:
            # prepare_sequence 冻结 Enabled 模块集合；open_run 同时写入本次实际 SEQ、
            # 配置、模块 desired settings 和 status-at-start。
            descriptors, module_status = await self.modules.prepare_sequence()
            run_paths = self.logger.open_run(
                document.name,
                serialize_sequence(document),
                descriptors,
                module_settings or {},
                module_status,
                self.instruments.snapshots(),
            )
            # 即使 SEQ 只有 End Sequence、尚未来得及等到下一次后台 poll，也保留
            # 一行通过 Run 前新鲜读回检查的初始仪表状态。
            self.logger.write_instrument_status(
                self.instruments.snapshots(),
                force=True,
            )
        except Exception as exc:
            self.state = RunState.FAULTED
            self._fatal_abort = True
            self._abort_message = str(exc)
            self.events.report(
                Severity.ERROR,
                "logging",
                "RUN_PREPARATION_FAILED",
                str(exc),
            )
            await self.modules.end_sequence("error")
            self._publish(self._abort_message)
            self.logger.close()
            return self.state
        self.events.report(
            Severity.INFO,
            "logging",
            "RUN_DIRECTORY",
            str(run_paths.directory),
            str(run_paths.data_file),
        )
        self._publish("Sequence started")
        self.events.report(Severity.INFO, "sequence", "RUN_STARTED", f"Running {document.name}")
        try:
            self._check_control()
            await self.modules.begin_sequence()
            await self._execute_commands(document.commands, [])
            self._check_control()
        except SequenceAbort:
            # 用户 Stop 和事件 Error 共用这里，区别只在最终 STOPPED/FAULTED。
            # SEQ 只结束 Measurement Module 的本次运行，不控制 System Instrument。
            self.state = RunState.FAULTED if self._fatal_abort else RunState.STOPPED
            code = "RUN_FAULTED" if self._fatal_abort else "RUN_STOPPED"
            self.events.report(
                Severity.INFO,
                "sequence",
                code,
                self._abort_message or ("Aborted due to error" if self._fatal_abort else "Stopped by user"),
            )
        except asyncio.CancelledError:
            # 只有应用关闭等待超时才应走强制 task cancellation。继续抛给
            # RuntimeService 完成资源回收，不向 System Instrument 发送控制指令。
            self._fatal_abort = True
            self._abort_message = (
                "Sequence task was cancelled during shutdown"
            )
            self.state = RunState.FAULTED
            self.events.report(
                Severity.ERROR,
                "sequence",
                "RUN_CANCELLED",
                self._abort_message,
            )
            raise
        except InstrumentError as exc:
            self._fatal_abort = True
            self._abort_message = str(exc)
            self.state = RunState.FAULTED
            self.events.report(Severity.ERROR, "sequence", exc.code, str(exc), exc.context)
        except Exception as exc:
            self._fatal_abort = True
            self._abort_message = str(exc)
            self.state = RunState.FAULTED
            self.events.report(Severity.ERROR, "sequence", "UNHANDLED_EXCEPTION", str(exc))
        else:
            self.state = RunState.COMPLETED
        finally:
            # run_end 对 completed/stopped/error 都发送。它不是 close：Stop 后模块
            # 保持 Enabled，只结束本次运行资源。
            reason = {
                RunState.COMPLETED: "completed",
                RunState.STOPPED: "stopped",
            }.get(self.state, "error")
            cleanup_succeeded = await self.modules.end_sequence(reason)
            if not cleanup_succeeded:
                self.state = RunState.FAULTED
                self._abort_message = "One or more modules failed to end the sequence safely"
                self.events.report(
                    Severity.INFO,
                    "sequence",
                    "MODULE_END_SEQUENCE_FAILED",
                    self._abort_message,
                )
            elif self.state is RunState.COMPLETED:
                self.events.report(Severity.INFO, "sequence", "RUN_COMPLETED", "Sequence completed")
            self._publish(self._abort_message or self.state.value)
            self.events.resolve_source("logging")
            self.logger.close()
        return self.state

    def pause(self) -> None:
        """请求协作 Pause；冻结调度和模块计时，不改变仪表当前输出。"""

        if self.state is RunState.RUNNING:
            self.state = RunState.PAUSED
            self._paused_at = time.monotonic()
            self._pause_gate.clear()
            pause_modules = getattr(
                self.modules,
                "pause_operations",
                None,
            )
            if callable(pause_modules):
                pause_modules()
            self.events.report(Severity.INFO, "sequence", "RUN_PAUSED", "Sequence paused")
            self._publish("Paused")

    def resume(self) -> None:
        """结束 Pause，并把暂停时长从后续逻辑计时中扣除。"""

        if self.state is RunState.PAUSED:
            self.state = RunState.RUNNING
            self._finish_pause()
            self._pause_gate.set()
            resume_modules = getattr(
                self.modules,
                "resume_operations",
                None,
            )
            if callable(resume_modules):
                resume_modules()
            self.events.report(Severity.INFO, "sequence", "RUN_RESUMED", "Sequence resumed")
            self._publish("Resumed")

    def request_stop(self, fatal: bool = False, message: str = "Stopped by user") -> None:
        """设置 Stop 标志并唤醒所有 Pause checkpoint。

        本方法必须快速且不做仪表 I/O。模块通过 ``cancel_operations`` 在自己的
        checkpoint 收到协作取消。SEQ 退出不控制 System Instrument；正在阻塞的
        第三方驱动调用仍受模块操作总超时约束。
        """

        if self.state not in (RunState.RUNNING, RunState.PAUSED, RunState.STOPPING):
            return
        self._abort_requested = True
        self._fatal_abort = self._fatal_abort or fatal
        self._abort_message = message
        self.state = RunState.STOPPING
        cancel_modules = getattr(
            self.modules,
            "cancel_operations",
            None,
        )
        if callable(cancel_modules):
            cancel_modules()
        # Stop 发生在 Paused 时必须先打开 gate，否则 SEQ 永远到不了抛出
        # SequenceAbort 的下一次 _check_control。
        self._finish_pause()
        self._pause_gate.set()
        self._publish(message)

    async def _execute_commands(self, commands: list[Command], prefix: list[str]) -> None:
        for index, command in enumerate(commands, start=1):
            # 每条命令（包括嵌套 Scan 子命令）前都有统一 Pause/Stop/仪表恢复检查点。
            await self._checkpoint()
            parameter_issues = validate_command_parameters(command)
            if parameter_issues:
                self._current_path = " / ".join(
                    prefix + [f"{index}:{command.type.value}"]
                )
                raise InstrumentError(
                    "Invalid sequence parameter: " + "; ".join(parameter_issues),
                    "INVALID_SEQUENCE_PARAMETER",
                    self._current_path,
                )
            label = format_command(command)
            path = prefix + [f"{index}:{label}"]
            self._current_path = " / ".join(path)
            if not command.enabled:
                self.events.report(
                    Severity.INFO,
                    "sequence",
                    "STEP_SKIPPED_DISABLED",
                    f"Skipped disabled command: {label}",
                    self._current_path,
                )
                self._publish(f"Disabled: {label}")
                continue
            self._publish(label)
            self.events.report(
                Severity.INFO,
                "sequence",
                "STEP_STARTED",
                label,
                self._current_path,
            )
            await self._execute_command(command, path)
            self._completed_steps += 1
            self.events.report(
                Severity.INFO,
                "sequence",
                "STEP_COMPLETED",
                label,
                self._current_path,
            )
            self._publish(label)

    async def _execute_command(self, command: Command, path: list[str]) -> None:
        p = command.params
        parameter_issues = validate_command_parameters(command)
        if parameter_issues:
            raise InstrumentError(
                "Invalid sequence parameter: " + "; ".join(parameter_issues),
                "INVALID_SEQUENCE_PARAMETER",
                self._current_path,
            )
        if command.type is CommandType.SET_DATAFILE:
            destination = self.logger.set_datafile(
                str(p.get("path", "experiment.dat")),
                str(p.get("mode", "open|create")),
                allow_external=str(p.get("path_scope", "Run folder")) == "Custom folder",
            )
            self.events.report(
                Severity.INFO,
                "logging",
                "DATAFILE_SELECTED",
                str(destination),
                self._current_path,
            )
            return
        if command.type is CommandType.WAIT:
            await self._interruptible_sleep(float(p.get("seconds", 0.0)))
            return
        if command.type is CommandType.SET_TEMPERATURE:
            panel = self.instruments.resolve_control_panel(
                InstrumentKind.TEMPERATURE
            )
            instrument_id = panel.instrument_id
            applied = await self.instruments.set_target_by_kind(
                InstrumentKind.TEMPERATURE,
                float(p.get("target", 300.0)),
                float(p.get("rate", 5.0)),
                str(p.get("mode", "Settle")),
            )
            if not applied:
                return
            if "settle" in str(p.get("mode", "Settle")).lower():
                await self._wait_for_stability(instrument_id, panel)
            return
        if command.type is CommandType.SET_FIELD:
            panel = self.instruments.resolve_control_panel(
                InstrumentKind.FIELD
            )
            instrument_id = panel.instrument_id
            instrument = self.instruments.instrument_configs[instrument_id]
            instrument_unit = instrument.reading(panel.reading).unit
            source_unit = str(p.get("unit", instrument_unit))
            target = convert_value(float(p.get("target", 0.0)), source_unit, instrument_unit)
            rate = convert_value(float(p.get("rate", 0.5)), source_unit, instrument_unit)
            applied = await self.instruments.set_target_by_kind(
                InstrumentKind.FIELD,
                target,
                rate,
                str(p.get("mode", "Settle")),
            )
            if not applied:
                return
            if "settle" in str(p.get("mode", "Settle")).lower():
                await self._wait_for_stability(instrument_id, panel)
            return
        if command.type is CommandType.SCAN_TEMPERATURE:
            await self._scan_controlled(command, InstrumentKind.TEMPERATURE, path)
            return
        if command.type is CommandType.SCAN_FIELD:
            await self._scan_controlled(command, InstrumentKind.FIELD, path)
            return
        if command.type is CommandType.SCAN_TIME:
            await self._scan_time(command, path)
            return
        if command.type is CommandType.INSTRUMENT_COMMAND:
            await self._checkpoint()
            await self.instruments.execute_sequence_command(
                command.instrument_id,
                command.instrument_command_id,
            )
            await self._checkpoint()
            return
        if command.type is CommandType.MODULE_COMMAND:
            await self._execute_module_command(command)
            return
        if command.type is CommandType.MODULE_SCAN:
            await self._scan_module(command, path)
            return
        if command.type is CommandType.MEASURE:
            await self._measure()
            return
        if command.type is CommandType.REMARK:
            self.events.report(
                Severity.INFO, "sequence", "REMARK", str(p.get("text", "")), self._current_path
            )
            return
        if command.type is CommandType.CALL_SEQUENCE:
            await self._call_sequence(str(p.get("path", "")), path)
            return
        if command.type is CommandType.INJECT_WARNING:
            self.events.report(
                Severity.WARNING,
                "simulation",
                str(p.get("code", "SIM_WARNING")),
                str(p.get("message", "Simulated Warning")),
            )
            return
        if command.type is CommandType.INJECT_ERROR:
            self.events.report(
                Severity.ERROR,
                "simulation",
                str(p.get("code", "SIM_ERROR")),
                str(p.get("message", "Simulated Error")),
            )
            self._check_control()
            return
        self.events.report(
            Severity.WARNING,
            "sequence",
            "UNKNOWN_COMMAND",
            f"Skipped unknown command: {format_command(command)}",
            self._current_path,
        )

    async def _scan_controlled(
        self,
        command: Command,
        kind: InstrumentKind,
        path: list[str],
    ) -> None:
        p = command.params
        panel = self.instruments.resolve_control_panel(kind)
        instrument_id = panel.instrument_id
        config = self.instruments.instrument_configs[instrument_id]
        instrument_unit = config.reading(panel.reading).unit
        source_unit = "K" if kind is InstrumentKind.TEMPERATURE else str(p.get("unit", instrument_unit))
        rate = convert_value(float(p.get("rate", panel.default_rate_per_minute)), source_unit, instrument_unit)
        mode = str(p.get("mode", "Settle"))
        if kind is InstrumentKind.TEMPERATURE and str(p.get("point_mode", "Linear")).casefold() == "list":
            try:
                source_points = parse_temperature_points(p.get("points", ""))
            except ValueError as exc:
                raise InstrumentError(
                    f"Invalid Scan Temperature list: {exc}",
                    "INVALID_TEMPERATURE_LIST",
                    instrument_id,
                ) from exc
            points = [convert_value(point, source_unit, instrument_unit) for point in source_points]
            steps = len(points)
        else:
            start = convert_value(float(p.get("start", 0.0)), source_unit, instrument_unit)
            stop = convert_value(float(p.get("stop", 0.0)), source_unit, instrument_unit)
            steps = int(p.get("steps", 1))
            points = self._linspace(start, stop, steps)

        if (
            kind is InstrumentKind.FIELD
            and p.get("nearest_polarity", False) is True
        ):
            # 极性必须在这条 Scan 真正运行到时决定。先经过统一 Pause/Stop/恢复检查，
            # 再取得后台仪表轮询刚发布的实际场；不能使用加载 SEQ 或启动 Run 时的值。
            # _checkpoint 已保证 primary 读数未超过配置的新鲜度期限，这里仍复核快照，
            # 防止自定义 InstrumentManager 或极短竞态把空值带入极性判断。
            await self._checkpoint()
            snapshot = self.instruments.snapshots().get(instrument_id)
            control_state = (
                None
                if snapshot is None
                else snapshot.controls.get(panel.id)
            )
            current = (
                None
                if control_state is None
                else control_state.current
            )
            snapshot_age = (
                math.inf
                if snapshot is None
                else max(0.0, time.monotonic() - snapshot.timestamp)
            )
            if (
                snapshot is None
                or not snapshot.connected
                or snapshot_age > config.stale_after_seconds
                or current is None
                or not math.isfinite(current)
            ):
                raise InstrumentError(
                    f"{config.display_name} has no fresh finite current field for "
                    "nearest-polarity scan",
                    "FIELD_SCAN_CURRENT_UNAVAILABLE",
                    instrument_id,
                )
            entered_start = points[0]
            inverted_start = -entered_start
            invert = abs(current - inverted_start) < abs(current - entered_start)
            if invert:
                # 整条扫描路径一起反号，不能只改变起点；显式消除 -0.0，避免日志和
                # 数据路径出现容易误读的负零。
                points = [0.0 if point == 0.0 else -point for point in points]
            decimals = control_decimals(InstrumentKind.FIELD, instrument_unit)
            chosen = "sign-inverted" if invert else "entered"
            self.events.report(
                Severity.INFO,
                "sequence",
                "FIELD_SCAN_POLARITY_SELECTED",
                f"{config.display_name} actual field "
                f"{fixed_number(current, decimals)} {instrument_unit}; selected "
                f"{chosen} path {fixed_number(points[0], decimals)} to "
                f"{fixed_number(points[-1], decimals)} {instrument_unit}",
                self._current_path,
            )

        # 在移动第一个设定点之前验证完整路径。后面的坏点不能让实验停在“已执行一半”
        # 的中间状态。
        for point in points:
            self.instruments.validate_target(
                instrument_id,
                point,
                rate,
                control=panel.control_id,
            )
        for point_index, point in enumerate(points, start=1):
            await self._checkpoint()
            applied = await self.instruments.set_target(
                instrument_id,
                point,
                rate,
                mode,
                control=panel.control_id,
            )
            if not applied:
                continue
            if "settle" in mode.lower():
                await self._wait_for_stability(instrument_id, panel)
            else:
                await self._wait_for_target(instrument_id, panel)
            decimals = control_decimals(kind, instrument_unit)
            point_path = path + [
                f"point {point_index}/{steps}={fixed_number(point, decimals)} {instrument_unit}"
            ]
            await self._execute_commands(command.children, point_path)

    async def _scan_time(self, command: Command, path: list[str]) -> None:
        duration = float(command.params.get("duration_seconds", 0.0))
        steps = int(command.params.get("steps", 1))
        offsets = self._linspace(0.0, duration, steps)
        started = self._active_time()
        for index, offset in enumerate(offsets, start=1):
            await self._sleep_until(started + offset)
            point_path = path + [f"time {index}/{steps}={offset:g} s"]
            await self._execute_commands(command.children, point_path)

    async def _execute_module_command(self, command: Command) -> bool:
        """执行一个不写 DAT 的模块动作，并在调用前后保留统一控制检查点。"""

        await self._checkpoint()
        completed = await self.modules.execute_sequence_command(command)
        await self._checkpoint()
        return completed

    async def _scan_module(self, command: Command, path: list[str]) -> None:
        """逐点调用模块动作；只有该点设置成功后才执行扫描子树。"""

        spec = self.modules.sequence_command_spec(
            command.module_id,
            command.module_command_id,
        )
        if spec is None or spec.kind != "scan":
            raise InstrumentError(
                f"Module scan is unavailable: {command.module_id}.{command.module_command_id}",
                "MODULE_SEQUENCE_COMMAND_UNAVAILABLE",
                f"{command.module_id}.{command.module_command_id}",
            )
        points = command.params.get(spec.points_field)
        if not isinstance(points, list) or not points:
            raise InstrumentError(
                f"Module scan field {spec.points_field!r} must contain at least one point",
                "MODULE_SEQUENCE_COMMAND_INVALID",
                f"{command.module_id}.{command.module_command_id}",
            )
        total = len(points)
        for index, point in enumerate(points, start=1):
            await self._checkpoint()
            parameters = dict(command.params)
            parameters[spec.point_parameter] = point
            completed = await self.modules.execute_sequence_command(
                command,
                parameters,
            )
            await self._checkpoint()
            if not completed:
                continue
            point_text = json.dumps(
                point,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            await self._execute_commands(
                command.children,
                path + [f"point {index}/{total}={point_text}"],
            )

    async def _measure(self) -> None:
        # 前一个 checkpoint 防止 Pause 后启动新测量；后一个 checkpoint 让测量期间到达
        # 的 Stop/Error 在进入下一条 SEQ 命令前立即生效。
        await self._checkpoint()
        await self.modules.measure_all(self.logger, self._current_path)
        await self._checkpoint()

    async def _call_sequence(self, requested: str, path: list[str]) -> None:
        source = Path(requested)
        if not source.is_absolute():
            base = self._call_stack[-1].parent if self._call_stack else self.config.project_root
            source = (base / source).resolve()
        if source in self._call_stack:
            raise InstrumentError(f"Circular sequence call detected: {source}", "SEQUENCE_CALL_CYCLE", str(source))
        if not source.exists():
            raise InstrumentError(f"Subsequence does not exist: {source}", "SEQUENCE_NOT_FOUND", str(source))
        result = load_sequence(
            source,
            instrument_commands=self.instrument_sequence_commands,
        )
        if result.has_errors:
            details = "; ".join(issue.message for issue in result.issues if issue.level == "error")
            raise InstrumentError(f"Subsequence parsing failed: {details}", "SEQUENCE_PARSE_ERROR", str(source))
        self._call_stack.append(source)
        try:
            await self._execute_commands(result.document.commands, path + [f"call {source.name}"])
        finally:
            self._call_stack.pop()

    def _estimate_execution_steps(
        self,
        commands: list[Command],
        base_directory: Path,
        call_stack: frozenset[Path],
    ) -> int:
        total = 0
        for command in commands:
            if not command.enabled:
                continue
            total += 1
            if command.type.is_container:
                try:
                    if command.type is CommandType.MODULE_SCAN:
                        spec = self.modules.sequence_command_spec(
                            command.module_id,
                            command.module_command_id,
                        )
                        iterations = (
                            0
                            if spec is None
                            else len(command.params.get(spec.points_field, []))
                        )
                    elif (
                        command.type is CommandType.SCAN_TEMPERATURE
                        and str(
                            command.params.get("point_mode", "Linear")
                        ).casefold()
                        == "list"
                    ):
                        iterations = len(
                            parse_temperature_points(
                                command.params.get("points", "")
                            )
                        )
                    else:
                        iterations = max(
                            1,
                            int(command.params.get("steps", 1)),
                        )
                except (TypeError, ValueError):
                    iterations = 0
                child_steps = self._estimate_execution_steps(
                    command.children,
                    base_directory,
                    call_stack,
                )
                total += iterations * child_steps
            elif command.type is CommandType.CALL_SEQUENCE:
                source = Path(str(command.params.get("path", "")))
                if not source.is_absolute():
                    source = (base_directory / source).resolve()
                if source in call_stack or not source.exists():
                    continue
                try:
                    result = load_sequence(
                        source,
                        instrument_commands=(
                            self.instrument_sequence_commands
                        ),
                    )
                except OSError:
                    continue
                if result.has_errors:
                    continue
                total += self._estimate_execution_steps(
                    result.document.commands,
                    source.parent,
                    call_stack | {source},
                )
        return total

    def _find_invalid_dynamic_command(
        self,
        commands: list[Command],
        base_directory: Path,
        call_stack: frozenset[Path],
        *,
        parent_enabled: bool = True,
    ) -> tuple[Command, tuple[str, ...]] | None:
        """在产生运行目录或移动仪表前递归验证所有会执行的动态指令。"""

        for command in commands:
            effective_enabled = parent_enabled and command.enabled
            if not effective_enabled:
                continue
            if command.type is CommandType.INSTRUMENT_COMMAND:
                if not self.instruments.has_sequence_command(
                    command.instrument_id,
                    command.instrument_command_id,
                ):
                    return command, (
                        "command is not declared by the configured instrument: "
                        f"{command.instrument_id}."
                        f"{command.instrument_command_id}",
                    )
            elif command.type in {
                CommandType.MODULE_COMMAND,
                CommandType.MODULE_SCAN,
            }:
                issues = self.modules.sequence_command_issues(command)
                if issues:
                    return command, issues
            invalid_child = self._find_invalid_dynamic_command(
                command.children,
                base_directory,
                call_stack,
                parent_enabled=effective_enabled,
            )
            if invalid_child is not None:
                return invalid_child
            if command.type is not CommandType.CALL_SEQUENCE:
                continue
            source = Path(str(command.params.get("path", "")))
            if not source.is_absolute():
                source = (base_directory / source).resolve()
            if source in call_stack or not source.exists():
                continue
            try:
                result = load_sequence(
                    source,
                    instrument_commands=(
                        self.instrument_sequence_commands
                    ),
                )
            except OSError:
                continue
            if result.has_errors:
                continue
            invalid_called = self._find_invalid_dynamic_command(
                result.document.commands,
                source.parent,
                call_stack | {source},
            )
            if invalid_called is not None:
                return invalid_called
        return None

    def _find_invalid_parameter(
        self,
        commands: list[Command],
    ) -> tuple[Command, tuple[str, ...]] | None:
        for command in commands:
            issues = validate_command_parameters(command)
            if issues:
                return command, issues
            invalid_child = self._find_invalid_parameter(command.children)
            if invalid_child is not None:
                return invalid_child
        return None

    async def _wait_for_stability(
        self,
        instrument_id: str,
        panel: InstrumentPanelConfig,
    ) -> None:
        started = self._active_time()
        while True:
            await self._checkpoint()
            snapshot = self.instruments.latest.get(instrument_id)
            if snapshot is not None:
                control_state = snapshot.controls.get(panel.id)
                if (
                    control_state is not None
                    and control_state.stability is StabilityState.STABLE
                ):
                    return
                if (
                    control_state is not None
                    and control_state.stability
                    is StabilityState.TIMED_OUT
                ):
                    self._check_control()
                    if self.config.alarms.stability_timeout is not Severity.ERROR:
                        return
            if self._control_wait_timed_out(
                instrument_id,
                panel,
                started,
                "stabilize",
            ):
                return
            await self._interruptible_sleep(
                self.config.control_poll_interval_seconds
            )

    async def _wait_for_target(
        self,
        instrument_id: str,
        panel: InstrumentPanelConfig,
    ) -> None:
        tolerance = panel.stability.tolerance if panel.stability else 0.0
        started = self._active_time()
        while True:
            await self._checkpoint()
            snapshot = self.instruments.latest.get(instrument_id)
            control_state = (
                None
                if snapshot is None
                else snapshot.controls.get(panel.id)
            )
            if (
                control_state is not None
                and control_state.current is not None
                and control_state.target is not None
                and abs(control_state.current - control_state.target)
                <= tolerance
            ):
                return
            if self._control_wait_timed_out(
                instrument_id,
                panel,
                started,
                "reach its target",
            ):
                return
            await self._interruptible_sleep(
                self.config.control_poll_interval_seconds
            )

    def _control_wait_timed_out(
        self,
        instrument_id: str,
        panel: InstrumentPanelConfig,
        started: float,
        action: str,
    ) -> bool:
        timeout = (
            panel.stability.timeout_seconds
            if panel.stability is not None
            else self.instruments.instrument_configs[
                instrument_id
            ].operation_timeout_seconds
        )
        elapsed = self._active_time() - started
        if elapsed < timeout:
            return False
        self.events.report(
            self.config.alarms.stability_timeout,
            instrument_id,
            "STABILITY_TIMEOUT",
            f"{panel.display_name} did not {action} within {elapsed:.1f} seconds",
        )
        self._check_control()
        return True

    async def _checkpoint(self) -> None:
        """统一等待用户 Pause 和 primary 仪表恢复，并随时响应 Stop/Error。

        先检查 Stop，再等待 pause gate，gate 打开后再检查一次，避免 Stop 与 Resume
        竞态漏过。主仪表恢复期间每 100 ms 检查 Stop；整段恢复等待累计到
        ``_instrument_wait_total``，不消耗 Settle/Wait/Scan Time 的逻辑时限。
        """

        while True:
            self._check_control()
            await self._pause_gate.wait()
            self._check_control()
            control_ready = bool(
                getattr(self.instruments, "control_ready", True)
            )
            if control_ready:
                if self._waiting_for_instruments:
                    self._waiting_for_instruments = False
                    self._publish(
                        "Primary instrument communication restored"
                    )
                return
            if not self._waiting_for_instruments:
                self._waiting_for_instruments = True
                reason_callback = getattr(
                    self.instruments,
                    "control_block_reason",
                    None,
                )
                reason = (
                    reason_callback()
                    if callable(reason_callback)
                    else None
                )
                self._publish(
                    "Waiting for primary instrument recovery"
                    + (f": {reason}" if reason else "")
                )
            wait_started = time.monotonic()
            try:
                while (
                    self._pause_gate.is_set()
                    and not bool(
                        getattr(
                            self.instruments,
                            "control_ready",
                            True,
                        )
                    )
                ):
                    self._check_control()
                    await asyncio.sleep(0.1)
            finally:
                self._instrument_wait_total += max(
                    0.0,
                    time.monotonic() - wait_started,
                )

    def _check_control(self) -> None:
        """在当前协程栈中同步抛出已请求的 Stop/Error。"""

        if self._abort_requested:
            raise SequenceAbort(self._abort_message)

    async def _interruptible_sleep(self, seconds: float) -> None:
        """按活动逻辑时间等待，最长约 100 ms 响应 Pause/Stop/仪表失联。"""

        deadline = self._active_time() + max(0.0, seconds)
        while True:
            await self._checkpoint()
            remaining = deadline - self._active_time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    async def _sleep_until(self, deadline: float) -> None:
        await self._interruptible_sleep(max(0.0, deadline - self._active_time()))

    def _active_time(self) -> float:
        """返回扣除 Pause 与主仪表恢复等待后的单调逻辑时间。

        使用 ``time.monotonic`` 避免系统时钟校时影响实验时限。当前仍在进行的 Pause
        单独即时扣除，已结束 Pause 和仪表等待使用累计值扣除。
        """

        now = time.monotonic()
        current_pause = (
            max(0.0, now - self._paused_at)
            if self._paused_at is not None
            else 0.0
        )
        return (
            now
            - self._paused_total
            - current_pause
            - self._instrument_wait_total
        )

    def _finish_pause(self) -> None:
        """把当前 Pause 结算进累计量；可重复调用。"""

        if self._paused_at is None:
            return
        self._paused_total += max(0.0, time.monotonic() - self._paused_at)
        self._paused_at = None

    def _publish(self, message: str) -> None:
        self.progress_callback(RunProgress(
            state=self.state,
            step_path=self._current_path,
            message=message,
            completed_steps=self._completed_steps,
            total_steps=self._total_steps,
        ))

    @staticmethod
    def _linspace(start: float, stop: float, steps: int) -> list[float]:
        if steps <= 1:
            return [start]
        increment = (stop - start) / (steps - 1)
        return [start + index * increment for index in range(steps)]
