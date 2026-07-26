from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_REPOSITORY = (
    ROOT
    / "plugin_templates"
    / "measurement-modules-repository"
)
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.extensions.trust import PluginTrustStore  # noqa: E402
from labcontrol.models import (  # noqa: E402
    DeviceActivity,
    DeviceConnectionState,
    DeviceKind,
    DeviceSnapshot,
    RunState,
    Severity,
    StabilityState,
)
from labcontrol.measurement.manifest import discover_modules  # noqa: E402
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.plugins import DeviceManager  # noqa: E402
from labcontrol.devices.base import DeviceError  # noqa: E402
from labcontrol.sequence.engine import SequenceAbort, SequenceEngine  # noqa: E402
from labcontrol.sequence.model import Command, CommandType, SequenceDocument  # noqa: E402


class SequenceEngineTests(unittest.TestCase):
    def _fast_config(self, temp_root: Path):
        (temp_root / "configs").mkdir()
        target = temp_root / "configs" / "default.toml"
        shutil.copy2(ROOT / "configs" / "default.toml", target)
        shutil.copytree(
            MODULE_REPOSITORY / "modules",
            temp_root / "modules",
        )
        config = load_config(target)
        trust_store = PluginTrustStore(
            config.resolve_project_path(
                config.plugins.state_directory
            )
            / "trusted_plugins.json"
        )
        for descriptor in discover_modules(config):
            trust_store.trust("module", descriptor)
        devices = []
        for device in config.devices:
            stability = device.stability
            if stability is not None:
                stability = replace(
                    stability,
                    tolerance=max(stability.tolerance, 0.005),
                    max_slope_per_minute=100.0,
                    dwell_seconds=0.05,
                    timeout_seconds=3.0,
                    window_seconds=0.05,
                )
            extras = dict(device.extras)
            extras["noise"] = 0.0
            devices.append(replace(device, stability=stability, extras=extras))
        return replace(
            config,
            simulation_speed=1000.0,
            poll_interval_seconds=0.01,
            devices=tuple(devices),
        )

    async def _run(self, config, document, notices, progresses=None):
        events = EventManager()
        events.subscribe(notices.append)
        manager = DeviceManager(config, events, isolate_processes=False)
        logger = DatRunLogger(config, events)
        modules = MeasurementModuleService(discover_modules(config), events, manager)
        engine = SequenceEngine(
            config,
            manager,
            events,
            logger,
            modules,
            progress_callback=(
                progresses.append
                if progresses is not None
                else None
            ),
        )
        await manager.connect_all()
        await manager.poll_all()
        await modules.enable("simulated_transport", {
            "delay_seconds": 0.001,
            "noise_ohm": 0.0,
            "warning_threshold_ohm": 1e9,
        })

        async def poll():
            while True:
                await manager.poll_all()
                await asyncio.sleep(config.poll_interval_seconds)

        poll_task = asyncio.create_task(poll())
        try:
            state = await engine.run(document)
            await manager.poll_all()
            return state, manager.snapshots(), logger.paths
        finally:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
            await modules.shutdown()
            await manager.disconnect_all()

    def test_nested_temperature_field_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            measure = Command(CommandType.MEASURE)
            field_scan = Command(CommandType.SCAN_FIELD, {
                "device_id": "field", "start": 0.0, "stop": 0.01, "unit": "T",
                "steps": 2, "rate": 0.5, "mode": "Settle",
            }, [measure])
            temperature_scan = Command(CommandType.SCAN_TEMPERATURE, {
                "device_id": "temperature", "start": 300.0, "stop": 299.9,
                "steps": 2, "rate": 10.0, "mode": "Settle",
            }, [field_scan])
            document = SequenceDocument([temperature_scan], "nested.seq")
            notices = []
            progresses = []
            state, _, paths = asyncio.run(
                self._run(config, document, notices, progresses)
            )
            self.assertEqual(state, RunState.COMPLETED)
            self.assertIsNotNone(paths)
            data = paths.data_file.read_text(encoding="utf-8")
            device_status = paths.device_status_file.read_text(
                encoding="utf-8"
            )
            self.assertGreaterEqual(data.count("Measure"), 16)
            self.assertIn(
                "temperature.Stability",
                device_status,
            )
            self.assertGreaterEqual(
                len(
                    device_status.split("[Data]\n", 1)[1]
                    .strip()
                    .splitlines()
                ),
                2,
            )
            self.assertEqual(progresses[-1].completed_steps, 7)
            self.assertEqual(progresses[-1].total_steps, 7)
            self.assertTrue(
                all(
                    progress.completed_steps <= progress.total_steps
                    for progress in progresses
                )
            )

    def test_duplicate_warning_continues_and_only_notifies_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            document = SequenceDocument([
                Command(CommandType.INJECT_WARNING, {"code": "SAME", "message": "same"}),
                Command(CommandType.INJECT_WARNING, {"code": "SAME", "message": "same"}),
                Command(CommandType.MEASURE),
            ], "warning.seq")
            notices = []
            state, _, _ = asyncio.run(self._run(config, document, notices))
            warning_notices = [
                notice for notice in notices
                if notice.event.severity is Severity.WARNING and notice.event.code == "SAME" and not notice.is_resolution
            ]
            self.assertEqual(state, RunState.COMPLETED)
            self.assertEqual(len(warning_notices), 1)
            self.assertTrue(warning_notices[0].show_popup)

    def test_error_aborts_and_holds_current_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            document = SequenceDocument([
                Command(CommandType.SET_FIELD, {
                    "device_id": "field", "target": 1.0, "unit": "T", "rate": 0.5, "mode": "Sweep",
                }),
                Command(CommandType.WAIT, {"seconds": 0.03}),
                Command(CommandType.INJECT_ERROR, {"code": "FATAL", "message": "fatal"}),
                Command(CommandType.MEASURE),
            ], "error.seq")
            notices = []
            state, snapshots, _ = asyncio.run(self._run(config, document, notices))
            field = snapshots["field"]
            self.assertEqual(state, RunState.FAULTED)
            self.assertAlmostEqual(field.target or 0.0, field.current or 0.0, places=4)

    def test_latched_idle_error_repeated_while_running_requests_fatal_stop(self) -> None:
        events = EventManager()
        engine = SequenceEngine(
            object(), object(), events, object(), object()  # type: ignore[arg-type]
        )
        events.report(Severity.ERROR, "device", "FAULT", "fault")
        self.assertEqual(engine.state, RunState.IDLE)
        engine.state = RunState.RUNNING
        events.report(Severity.ERROR, "device", "FAULT", "fault persists")
        self.assertEqual(engine.state, RunState.STOPPING)
        self.assertTrue(engine._abort_requested)
        self.assertTrue(engine._fatal_abort)

    def test_pause_freezes_interruptible_wait_deadline(self) -> None:
        async def scenario() -> None:
            events = EventManager()
            engine = SequenceEngine(
                object(), object(), events, object(), object()  # type: ignore[arg-type]
            )
            engine.state = RunState.RUNNING
            wait_task = asyncio.create_task(engine._interruptible_sleep(0.16))
            await asyncio.sleep(0.03)
            engine.pause()
            await asyncio.sleep(0.20)
            self.assertFalse(wait_task.done())
            resumed_at = asyncio.get_running_loop().time()
            engine.resume()
            await asyncio.wait_for(wait_task, timeout=0.5)
            self.assertGreaterEqual(
                asyncio.get_running_loop().time() - resumed_at,
                0.09,
            )

        asyncio.run(scenario())

    def test_device_recovery_freezes_interruptible_wait_deadline(
        self,
    ) -> None:
        class RecoveringDevices:
            control_ready = True

            @staticmethod
            def control_block_reason():
                return "primary temperature is reconnecting"

        async def scenario() -> None:
            events = EventManager()
            devices = RecoveringDevices()
            engine = SequenceEngine(
                object(), devices, events, object(), object()  # type: ignore[arg-type]
            )
            engine.state = RunState.RUNNING
            wait_task = asyncio.create_task(
                engine._interruptible_sleep(0.16)
            )
            await asyncio.sleep(0.03)
            devices.control_ready = False
            await asyncio.sleep(0.20)
            self.assertFalse(wait_task.done())
            restored_at = asyncio.get_running_loop().time()
            devices.control_ready = True
            await asyncio.wait_for(wait_task, timeout=0.5)
            self.assertGreaterEqual(
                asyncio.get_running_loop().time() - restored_at,
                0.09,
            )

        asyncio.run(scenario())

    def test_run_preflight_rejects_missing_primary_readback(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = DeviceManager(
                config,
                events,
                isolate_processes=False,
            )
            await manager.connect_all()
            engine = SequenceEngine(
                config,
                manager,
                events,
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
            state = await engine.run(
                SequenceDocument([], "preflight.seq")
            )
            self.assertEqual(state, RunState.FAULTED)
            self.assertIn(
                "PRIMARY_DEVICE_NOT_READY",
                [notice.event.code for notice in notices],
            )
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(
                scenario(self._fast_config(Path(temp)))
            )

    def test_task_cancellation_attempts_hold_before_exiting(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            manager = DeviceManager(
                config,
                events,
                isolate_processes=False,
            )
            logger = DatRunLogger(config, events)
            modules = MeasurementModuleService((), events, manager)
            engine = SequenceEngine(
                config,
                manager,
                events,
                logger,
                modules,
            )
            await manager.connect_all()
            await manager.poll_all()
            task = asyncio.create_task(
                engine.run(
                    SequenceDocument(
                        [
                            Command(
                                CommandType.WAIT,
                                {"seconds": 5.0},
                            )
                        ],
                        "cancel.seq",
                    )
                )
            )
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for device_id in ("temperature", "field"):
                snapshot = manager.latest[device_id]
                self.assertAlmostEqual(
                    snapshot.target or 0.0,
                    snapshot.current or 0.0,
                )
            await modules.shutdown()
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(
                scenario(self._fast_config(Path(temp)))
            )

    def test_control_waits_timeout_without_new_device_readings(self) -> None:
        async def scenario(config) -> None:
            temperature = config.device("temperature")
            temperature = replace(
                temperature,
                stability=replace(
                    temperature.stability,
                    timeout_seconds=0.05,
                ),
            )
            config = replace(
                config,
                poll_interval_seconds=0.005,
                devices=tuple(
                    temperature if device.id == temperature.id else device
                    for device in config.devices
                ),
            )
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = DeviceManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            now = asyncio.get_running_loop().time()
            manager.latest["temperature"] = DeviceSnapshot(
                device_id="temperature",
                display_name=temperature.display_name,
                kind=DeviceKind.TEMPERATURE,
                timestamp=now,
                connected=True,
                unit=temperature.unit,
                current=300.0,
                target=299.0,
                rate_per_minute=1.0,
                activity=DeviceActivity.MOVING,
                stability=StabilityState.MOVING,
            )
            manager._connection_states["temperature"] = (
                DeviceConnectionState.CONNECTED
            )
            engine = SequenceEngine(
                config,
                manager,
                events,
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )

            for wait in (engine._wait_for_stability, engine._wait_for_target):
                engine.state = RunState.RUNNING
                engine._abort_requested = False
                engine._fatal_abort = False
                events.resolve("temperature", "STABILITY_TIMEOUT")
                with self.assertRaises(SequenceAbort):
                    await asyncio.wait_for(wait("temperature"), timeout=0.25)

            timeout_notices = [
                notice
                for notice in notices
                if notice.event.code == "STABILITY_TIMEOUT"
                and not notice.is_resolution
            ]
            self.assertEqual(len(timeout_notices), 2)
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            asyncio.run(scenario(config))

    def test_runtime_rejects_programmatic_parameter_limit_bypasses(self) -> None:
        cases = (
            Command(CommandType.WAIT, {"seconds": -1.0}),
            Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 1.0, "steps": 1_000_001},
            ),
            Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 1.0, "steps": float("inf")},
            ),
            Command(
                CommandType.SCAN_FIELD,
                {
                    "device_id": "field",
                    "start": 0.0,
                    "stop": 1.0,
                    "unit": "Oe",
                    "steps": 2,
                    "rate": 0.0,
                    "mode": "Settle",
                },
            ),
        )
        for command in cases:
            with self.subTest(command=command.type.value):
                with tempfile.TemporaryDirectory() as temp:
                    config = self._fast_config(Path(temp))
                    notices = []
                    state, _, _ = asyncio.run(
                        self._run(
                            config,
                            SequenceDocument([command], "invalid-parameter.seq"),
                            notices,
                        )
                    )
                    self.assertEqual(state, RunState.FAULTED)
                    self.assertIn(
                        "INVALID_SEQUENCE_PARAMETER",
                        [
                            notice.event.code
                            for notice in notices
                            if not notice.is_resolution
                        ],
                    )

    def test_stop_becomes_faulted_when_hold_current_is_not_confirmed(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = DeviceManager(config, events, isolate_processes=False)
            logger = DatRunLogger(config, events)
            modules = MeasurementModuleService((), events, manager)
            engine = SequenceEngine(config, manager, events, logger, modules)
            await manager.connect_all()
            await manager.poll_all()

            async def failed_hold() -> bool:
                return False

            manager.hold_all = failed_hold  # type: ignore[method-assign]
            run_task = asyncio.create_task(
                engine.run(
                    SequenceDocument(
                        [Command(CommandType.WAIT, {"seconds": 5.0})],
                        "hold-failed.seq",
                    )
                )
            )
            await asyncio.sleep(0.03)
            engine.request_stop(False, "Stopped by test")
            state = await asyncio.wait_for(run_task, timeout=1.0)
            self.assertEqual(state, RunState.FAULTED)
            self.assertIn("could not confirm Hold Current", engine._abort_message)
            self.assertIn(
                "RUN_FAULTED",
                [notice.event.code for notice in notices],
            )
            await modules.shutdown()
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            asyncio.run(scenario(config))

    def test_stop_interrupts_an_inflight_module_measurement(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            manager = DeviceManager(
                config,
                events,
                isolate_processes=False,
            )
            logger = DatRunLogger(config, events)
            modules = MeasurementModuleService(
                discover_modules(config),
                events,
                manager,
            )
            engine = SequenceEngine(
                config,
                manager,
                events,
                logger,
                modules,
            )
            await manager.connect_all()
            await manager.poll_all()
            await modules.enable(
                "simulated_transport",
                {
                    "delay_seconds": 5.0,
                    "noise_ohm": 0.0,
                    "warning_threshold_ohm": 1e9,
                },
            )
            try:
                run_task = asyncio.create_task(
                    engine.run(
                        SequenceDocument(
                            [Command(CommandType.MEASURE)],
                            "stop-module-measure.seq",
                        )
                    )
                )
                deadline = time.monotonic() + 2.0
                while (
                    modules.records[
                        "simulated_transport"
                    ].state
                    != "measuring"
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.01)
                self.assertEqual(
                    modules.records[
                        "simulated_transport"
                    ].state,
                    "measuring",
                )
                started = time.monotonic()
                engine.request_stop(
                    False,
                    "Stopped during module measurement",
                )
                state = await asyncio.wait_for(
                    run_task,
                    timeout=2.0,
                )
                self.assertLess(
                    time.monotonic() - started,
                    1.0,
                )
                self.assertEqual(state, RunState.STOPPED)
            finally:
                await modules.shutdown()
                await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(
                scenario(self._fast_config(Path(temp)))
            )

    def test_progress_expands_called_sequence_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config = self._fast_config(temp_root)
            child = temp_root / "child.seq"
            child.write_text(
                "T Wait For 0.0 secs\n"
                "T Scan Time 0.0 secs in 2 steps\n"
                "T     Measure\n"
                "T End Scan\n"
                "T End Sequence\n",
                encoding="utf-8",
            )
            document = SequenceDocument(
                [Command(CommandType.CALL_SEQUENCE, {"path": "child.seq"})],
                "main.seq",
                temp_root / "main.seq",
            )
            notices = []
            progresses = []
            state, _, _ = asyncio.run(
                self._run(config, document, notices, progresses)
            )
            self.assertEqual(state, RunState.COMPLETED)
            self.assertEqual(progresses[-1].completed_steps, 5)
            self.assertEqual(progresses[-1].total_steps, 5)

    def test_disabled_command_and_scan_block_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            disabled_scan = Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 0.0, "steps": 1},
                [Command(CommandType.INJECT_ERROR, {"code": "NESTED_FATAL", "message": "must not run"})],
                enabled=False,
            )
            document = SequenceDocument([
                Command(
                    CommandType.INJECT_ERROR,
                    {"code": "DIRECT_FATAL", "message": "must not run"},
                    enabled=False,
                ),
                disabled_scan,
                Command(CommandType.MEASURE),
            ], "disabled.seq")
            notices = []
            state, _, paths = asyncio.run(self._run(config, document, notices))
            self.assertEqual(state, RunState.COMPLETED)
            self.assertTrue(paths.data_file.exists())
            codes = [notice.event.code for notice in notices if not notice.is_resolution]
            self.assertNotIn("DIRECT_FATAL", codes)
            self.assertNotIn("NESTED_FATAL", codes)
            self.assertEqual(codes.count("STEP_SKIPPED_DISABLED"), 2)

    def test_temperature_list_executes_in_declared_order_with_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            measure = Command(CommandType.MEASURE)
            temperature_scan = Command(
                CommandType.SCAN_TEMPERATURE,
                {
                    "device_id": "temperature",
                    "point_mode": "List",
                    "points": "300, 299.9, 300",
                    "rate": 10.0,
                    "mode": "Settle",
                },
                [measure],
            )
            notices = []
            state, _, paths = asyncio.run(
                self._run(config, SequenceDocument([temperature_scan], "temperature-list.seq"), notices)
            )
            self.assertEqual(state, RunState.COMPLETED)
            data = paths.data_file.read_text(encoding="utf-8")
            first = data.index("point 1/3=300.000 K")
            second = data.index("point 2/3=299.900 K", first)
            third = data.index("point 3/3=300.000 K", second)
            self.assertLess(first, second)
            self.assertLess(second, third)

    def test_temperature_list_is_fully_validated_before_first_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            temperature_scan = Command(
                CommandType.SCAN_TEMPERATURE,
                {
                    "device_id": "temperature",
                    "point_mode": "List",
                    "points": "299.9, 500",
                    "rate": 10.0,
                    "mode": "Settle",
                },
            )
            notices = []
            state, snapshots, _ = asyncio.run(
                self._run(config, SequenceDocument([temperature_scan], "unsafe-list.seq"), notices)
            )
            self.assertEqual(state, RunState.FAULTED)
            temperature = snapshots["temperature"]
            self.assertAlmostEqual(temperature.current or 0.0, 300.0, places=3)
            self.assertAlmostEqual(temperature.target or 0.0, 300.0, places=3)
            self.assertIn(
                "TARGET_OUT_OF_RANGE",
                [notice.event.code for notice in notices if not notice.is_resolution],
            )

    def test_custom_datafile_command_writes_to_selected_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config = self._fast_config(temp_root)
            custom_path = temp_root / "selected output" / "measurement.dat"
            document = SequenceDocument([
                Command(CommandType.SET_DATAFILE, {
                    "mode": "create",
                    "path_scope": "Custom folder",
                    "path": str(custom_path),
                }),
                Command(CommandType.MEASURE),
            ], "custom-output.seq")
            notices = []
            state, _, paths = asyncio.run(self._run(config, document, notices))
            self.assertEqual(state, RunState.COMPLETED)
            self.assertEqual(paths.data_file, custom_path)
            self.assertTrue(custom_path.exists())
            self.assertIn(
                "DATAFILE_SELECTED",
                [notice.event.code for notice in notices if not notice.is_resolution],
            )

    def test_module_measure_error_calls_end_error_without_abort(self) -> None:
        class FailingModules:
            def __init__(self) -> None:
                self.begin_called = False
                self.end_reasons: list[str] = []
                self.abort_called = False

            async def prepare_sequence(self, settings):
                del settings
                return (), {}

            async def begin_sequence(self):
                self.begin_called = True

            async def measure_all(self, logger, sequence_step):
                del logger, sequence_step
                raise DeviceError("module equipment alarm", "MODULE_EQUIPMENT_ALARM")

            async def end_sequence(self, reason):
                self.end_reasons.append(reason)
                return True

        async def scenario(config):
            events = EventManager()
            manager = DeviceManager(config, events, isolate_processes=False)
            logger = DatRunLogger(config, events)
            modules = FailingModules()
            engine = SequenceEngine(config, manager, events, logger, modules)  # type: ignore[arg-type]
            await manager.connect_all()
            await manager.poll_all()
            try:
                state = await engine.run(
                    SequenceDocument([Command(CommandType.MEASURE)], "module-error.seq")
                )
            finally:
                await manager.disconnect_all()
            self.assertEqual(state, RunState.FAULTED)
            self.assertTrue(modules.begin_called)
            self.assertEqual(modules.end_reasons, ["error"])
            self.assertFalse(modules.abort_called)

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(self._fast_config(Path(temp))))


if __name__ == "__main__":
    unittest.main()
