from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from ..config import AppConfig
from ..datafile import DatRunLogger
from ..devices.base import DeviceError
from ..events import EventManager
from ..formatting import control_decimals, fixed_number
from ..models import DeviceKind, EventNotice, RunProgress, RunState, Severity, StabilityState
from ..plugins import DeviceManager
from ..units import convert_value
from .model import Command, CommandType, SequenceDocument
from .parser import format_command, load_sequence, parse_temperature_points, serialize_sequence


class SequenceAbort(RuntimeError):
    pass


ProgressCallback = Callable[[RunProgress], None]


class SequenceEngine:
    def __init__(
        self,
        config: AppConfig,
        devices: DeviceManager,
        events: EventManager,
        logger: DatRunLogger,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.devices = devices
        self.events = events
        self.logger = logger
        self.progress_callback = progress_callback or (lambda _: None)
        self.state = RunState.IDLE
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        self._abort_requested = False
        self._fatal_abort = False
        self._abort_message = ""
        self._completed_steps = 0
        self._total_steps = 0
        self._current_path = ""
        self._call_stack: list[Path] = []
        self.events.subscribe(self._on_event)

    def _on_event(self, notice: EventNotice) -> None:
        if (
            not notice.is_resolution
            and notice.event.severity is Severity.ERROR
            and self.state in (RunState.RUNNING, RunState.PAUSED)
        ):
            self.request_stop(fatal=True, message=notice.event.message)

    async def run(self, document: SequenceDocument) -> RunState:
        if self.state in (RunState.RUNNING, RunState.PAUSED, RunState.STOPPING):
            raise RuntimeError("A sequence is already running")
        self._abort_requested = False
        self._fatal_abort = False
        self._abort_message = ""
        self._pause_gate.set()
        self._completed_steps = 0
        self._total_steps = max(document.count_commands(enabled_only=True), 1)
        self._call_stack.clear()
        if document.path is not None:
            self._call_stack.append(document.path.resolve())
        self.state = RunState.RUNNING
        run_paths = self.logger.open_run(document.name, serialize_sequence(document))
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
            await self._execute_commands(document.commands, [])
            self._check_control()
        except SequenceAbort:
            self.state = RunState.FAULTED if self._fatal_abort else RunState.STOPPED
            await self.devices.hold_all()
            code = "RUN_FAULTED" if self._fatal_abort else "RUN_STOPPED"
            self.events.report(
                Severity.INFO,
                "sequence",
                code,
                self._abort_message or ("Aborted due to error" if self._fatal_abort else "Stopped by user"),
            )
        except DeviceError as exc:
            self._fatal_abort = True
            self._abort_message = str(exc)
            self.state = RunState.FAULTED
            await self.devices.hold_all()
            self.events.report(Severity.ERROR, "sequence", exc.code, str(exc), exc.context)
        except Exception as exc:
            self._fatal_abort = True
            self._abort_message = str(exc)
            self.state = RunState.FAULTED
            await self.devices.hold_all()
            self.events.report(Severity.ERROR, "sequence", "UNHANDLED_EXCEPTION", str(exc))
        else:
            self.state = RunState.COMPLETED
            self.events.report(Severity.INFO, "sequence", "RUN_COMPLETED", "Sequence completed")
        finally:
            self._publish(self._abort_message or self.state.value)
            self.logger.close()
            self.events.resolve_source("logging")
        return self.state

    def pause(self) -> None:
        if self.state is RunState.RUNNING:
            self.state = RunState.PAUSED
            self._pause_gate.clear()
            self.events.report(Severity.INFO, "sequence", "RUN_PAUSED", "Sequence paused")
            self._publish("Paused")

    def resume(self) -> None:
        if self.state is RunState.PAUSED:
            self.state = RunState.RUNNING
            self._pause_gate.set()
            self.events.report(Severity.INFO, "sequence", "RUN_RESUMED", "Sequence resumed")
            self._publish("Resumed")

    def request_stop(self, fatal: bool = False, message: str = "Stopped by user") -> None:
        if self.state not in (RunState.RUNNING, RunState.PAUSED, RunState.STOPPING):
            return
        self._abort_requested = True
        self._fatal_abort = self._fatal_abort or fatal
        self._abort_message = message
        self.state = RunState.STOPPING
        self._pause_gate.set()
        self._publish(message)

    async def _execute_commands(self, commands: list[Command], prefix: list[str]) -> None:
        for index, command in enumerate(commands, start=1):
            await self._checkpoint()
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
        if command.type is CommandType.INITIALIZE:
            self.events.report(
                Severity.INFO,
                "sequence",
                "INITIALIZE_ACCEPTED",
                f"Simulated initialization: {p.get('model', 'device')} {p.get('config_path', '')}".rstrip(),
                self._current_path,
            )
            return
        if command.type is CommandType.SET_DATAFILE:
            self.logger.set_datafile(str(p.get("path", "experiment.dat")), str(p.get("mode", "open|create")))
            return
        if command.type is CommandType.WAIT:
            await self._interruptible_sleep(float(p.get("seconds", 0.0)))
            return
        if command.type is CommandType.SET_TEMPERATURE:
            device_id = str(p.get("device_id", "temperature"))
            await self.devices.set_target_by_kind(
                DeviceKind.TEMPERATURE,
                float(p.get("target", 300.0)),
                float(p.get("rate", 5.0)),
                str(p.get("mode", "Settle")),
                device_id,
            )
            if "settle" in str(p.get("mode", "Settle")).lower():
                await self._wait_for_stability(device_id)
            return
        if command.type is CommandType.SET_FIELD:
            device_id = str(p.get("device_id", "field"))
            device_unit = self.devices.device_configs[device_id].unit
            source_unit = str(p.get("unit", device_unit))
            target = convert_value(float(p.get("target", 0.0)), source_unit, device_unit)
            rate = convert_value(float(p.get("rate", 0.5)), source_unit, device_unit)
            await self.devices.set_target_by_kind(
                DeviceKind.FIELD,
                target,
                rate,
                str(p.get("mode", "Settle")),
                device_id,
            )
            if "settle" in str(p.get("mode", "Settle")).lower():
                await self._wait_for_stability(device_id)
            return
        if command.type is CommandType.SCAN_TEMPERATURE:
            await self._scan_controlled(command, DeviceKind.TEMPERATURE, path)
            return
        if command.type is CommandType.SCAN_FIELD:
            await self._scan_controlled(command, DeviceKind.FIELD, path)
            return
        if command.type is CommandType.SCAN_TIME:
            await self._scan_time(command, path)
            return
        if command.type is CommandType.MEASURE:
            await self._measure(command)
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
        kind: DeviceKind,
        path: list[str],
    ) -> None:
        p = command.params
        device_id = str(p.get("device_id", kind.value))
        config = self.devices.device_configs[device_id]
        source_unit = "K" if kind is DeviceKind.TEMPERATURE else str(p.get("unit", config.unit))
        rate = convert_value(float(p.get("rate", config.default_rate_per_minute)), source_unit, config.unit)
        mode = str(p.get("mode", "Settle"))
        if kind is DeviceKind.TEMPERATURE and str(p.get("point_mode", "Linear")).casefold() == "list":
            try:
                source_points = parse_temperature_points(p.get("points", ""))
            except ValueError as exc:
                raise DeviceError(
                    f"Invalid Scan Temperature list: {exc}",
                    "INVALID_TEMPERATURE_LIST",
                    device_id,
                ) from exc
            points = [convert_value(point, source_unit, config.unit) for point in source_points]
            steps = len(points)
        else:
            start = convert_value(float(p.get("start", 0.0)), source_unit, config.unit)
            stop = convert_value(float(p.get("stop", 0.0)), source_unit, config.unit)
            steps = max(1, int(p.get("steps", 1)))
            points = self._linspace(start, stop, steps)

        # Validate the complete path before moving the first device. A bad later
        # list entry must not leave an experiment half-executed.
        for point in points:
            self.devices.validate_target(device_id, point, rate)
        for point_index, point in enumerate(points, start=1):
            await self._checkpoint()
            await self.devices.set_target(device_id, point, rate, mode)
            if "settle" in mode.lower():
                await self._wait_for_stability(device_id)
            else:
                await self._wait_for_target(device_id)
            decimals = control_decimals(kind, config.unit)
            point_path = path + [
                f"point {point_index}/{steps}={fixed_number(point, decimals)} {config.unit}"
            ]
            await self._execute_commands(command.children, point_path)

    async def _scan_time(self, command: Command, path: list[str]) -> None:
        duration = max(0.0, float(command.params.get("duration_seconds", 0.0)))
        steps = max(1, int(command.params.get("steps", 1)))
        offsets = self._linspace(0.0, duration, steps)
        started = time.monotonic()
        for index, offset in enumerate(offsets, start=1):
            await self._sleep_until(started + offset)
            point_path = path + [f"time {index}/{steps}={offset:g} s"]
            await self._execute_commands(command.children, point_path)

    async def _measure(self, command: Command) -> None:
        p = command.params
        device_text = str(p.get("devices", "all")).strip()
        selected = None if not device_text or device_text.lower() == "all" else [
            item.strip() for item in device_text.split(",") if item.strip()
        ]
        repeats = max(1, int(p.get("repeats", 1)))
        interval = max(0.0, float(p.get("interval_seconds", 0.0)))
        for index in range(repeats):
            await self._checkpoint()
            values = await self.devices.measure(selected)
            self.logger.write_measurement(self.devices.snapshots(), values, self._current_path)
            if index + 1 < repeats:
                await self._interruptible_sleep(interval)

    async def _call_sequence(self, requested: str, path: list[str]) -> None:
        source = Path(requested)
        if not source.is_absolute():
            base = self._call_stack[-1].parent if self._call_stack else self.config.project_root
            source = (base / source).resolve()
        if source in self._call_stack:
            raise DeviceError(f"Circular sequence call detected: {source}", "SEQUENCE_CALL_CYCLE", str(source))
        if not source.exists():
            raise DeviceError(f"Subsequence does not exist: {source}", "SEQUENCE_NOT_FOUND", str(source))
        result = load_sequence(source)
        if result.has_errors:
            details = "; ".join(issue.message for issue in result.issues if issue.level == "error")
            raise DeviceError(f"Subsequence parsing failed: {details}", "SEQUENCE_PARSE_ERROR", str(source))
        self._call_stack.append(source)
        try:
            await self._execute_commands(result.document.commands, path + [f"call {source.name}"])
        finally:
            self._call_stack.pop()

    async def _wait_for_stability(self, device_id: str) -> None:
        while True:
            await self._checkpoint()
            snapshot = self.devices.latest.get(device_id)
            if snapshot is not None:
                if snapshot.stability is StabilityState.STABLE:
                    return
                if snapshot.stability is StabilityState.TIMED_OUT:
                    self._check_control()
                    if self.config.alarms.stability_timeout is not Severity.ERROR:
                        return
            await self._interruptible_sleep(self.config.poll_interval_seconds)

    async def _wait_for_target(self, device_id: str) -> None:
        config = self.devices.device_configs[device_id]
        tolerance = config.stability.tolerance if config.stability else 0.0
        while True:
            await self._checkpoint()
            snapshot = self.devices.latest.get(device_id)
            if (
                snapshot is not None
                and snapshot.current is not None
                and snapshot.target is not None
                and abs(snapshot.current - snapshot.target) <= tolerance
            ):
                return
            await self._interruptible_sleep(self.config.poll_interval_seconds)

    async def _checkpoint(self) -> None:
        self._check_control()
        await self._pause_gate.wait()
        self._check_control()

    def _check_control(self) -> None:
        if self._abort_requested:
            raise SequenceAbort(self._abort_message)

    async def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            await self._checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    async def _sleep_until(self, deadline: float) -> None:
        await self._interruptible_sleep(max(0.0, deadline - time.monotonic()))

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
