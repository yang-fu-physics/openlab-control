from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.models import RunState, Severity  # noqa: E402
from labcontrol.measurement.manifest import discover_modules  # noqa: E402
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.plugins import DeviceManager  # noqa: E402
from labcontrol.devices.base import DeviceError  # noqa: E402
from labcontrol.sequence.engine import SequenceEngine  # noqa: E402
from labcontrol.sequence.model import Command, CommandType, SequenceDocument  # noqa: E402


class SequenceEngineTests(unittest.TestCase):
    def _fast_config(self, temp_root: Path):
        (temp_root / "configs").mkdir()
        target = temp_root / "configs" / "default.toml"
        shutil.copy2(ROOT / "configs" / "default.toml", target)
        shutil.copytree(ROOT / "modules", temp_root / "modules")
        config = load_config(target)
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

    async def _run(self, config, document, notices):
        events = EventManager()
        events.subscribe(notices.append)
        manager = DeviceManager(config, events)
        logger = DatRunLogger(config, events)
        modules = MeasurementModuleService(discover_modules(config), events, manager)
        engine = SequenceEngine(config, manager, events, logger, modules)
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
            state, _, paths = asyncio.run(self._run(config, document, notices))
            self.assertEqual(state, RunState.COMPLETED)
            self.assertIsNotNone(paths)
            data = paths.data_file.read_text(encoding="utf-8")
            self.assertGreaterEqual(data.count("Measure"), 16)

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
            manager = DeviceManager(config, events)
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
