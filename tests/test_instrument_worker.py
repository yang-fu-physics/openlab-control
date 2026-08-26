from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests.configuration_fixtures import load_simulated_config  # noqa: E402
from labcontrol.instruments.base import InstrumentError  # noqa: E402
from labcontrol.instruments.manifest import discover_system_instruments  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402


def _external_instrument(
    root: Path,
    source: str,
    kinds: str = '"temperature"',
    sequence_commands: str = "",
) -> Path:
    instrument = root / "isolated_test"
    instrument.mkdir(parents=True)
    if kinds == '"monitor"':
        panel = (
            '[[panels]]\n'
            'id = "main"\n'
            'label = "Isolated Test"\n'
            'template = "readout"\n'
            'readings = ["value"]\n'
        )
    else:
        panel = (
            '[[controls]]\n'
            'id = "main"\n'
            'label = "Isolated Test"\n'
            '[[panels]]\n'
            'id = "main"\n'
            'label = "Isolated Test"\n'
            'template = "controller"\n'
            'control = "main"\n'
            'reading_options = ["value"]\n'
            'default_reading = "value"\n'
            'min_value = 0.0\n'
            'max_value = 1000.0\n'
            'default_rate_per_minute = 1.0\n'
            'max_rate_per_minute = 100.0\n'
            'stability_tolerance = 0.1\n'
            'stability_max_slope_per_minute = 0.1\n'
            'stability_dwell_seconds = 0.0\n'
            'stability_timeout_seconds = 10.0\n'
            'stability_window_seconds = 1.0\n'
        )
    (instrument / "instrument.toml").write_text(
        (
            'id = "isolated_test"\n'
            'name = "Isolated Test"\n'
            'version = "0.1.0"\n'
            'api_version = "4"\n'
            'backend = "backend:IsolatedTestInstrument"\n'
            f"kinds = [{kinds}]\n"
            + panel
            + '[readings.value]\nlabel = "Value"\nunit = "K"\n'
            + sequence_commands
        ),
        encoding="utf-8",
    )
    (instrument / "backend.py").write_text(source, encoding="utf-8")
    return instrument


class InstrumentWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_simulated_config()

    def test_builtin_instruments_run_in_distinct_child_processes_and_close(self) -> None:
        async def scenario() -> None:
            manager = InstrumentManager(self.config, EventManager())
            await manager.connect_all()
            pids = {
                instrument_id: getattr(client, "pid", None)
                for instrument_id, client in manager.instruments.items()
            }
            self.assertEqual(
                len(set(pids.values())),
                len(self.config.instrument_instances),
            )
            self.assertNotIn(None, pids.values())
            self.assertNotIn(os.getpid(), pids.values())
            snapshots = await manager.poll_all()
            self.assertEqual(set(snapshots), {"temperature", "field", "second_stage"})
            measurement_snapshots = await manager.poll_measurement_all()
            self.assertEqual(
                set(measurement_snapshots),
                {"temperature", "field", "second_stage"},
            )
            await manager.disconnect_all()
            self.assertTrue(
                all(getattr(client, "pid", None) is None for client in manager.instruments.values())
            )

        asyncio.run(scenario())

    def test_external_backend_is_imported_only_inside_its_worker(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / "backend-pid.txt"
                source = (
                    "import os\n"
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
                    "from labcontrol.instruments.base import SystemInstrument\n"
                    "class IsolatedTestInstrument(SystemInstrument):\n"
                    "    def open(self): pass\n"
                    "    def close(self): pass\n"
                    "    def read_status(self): return {'value': 12.5}\n"
                    "    def read_measurement(self): return {'value': 99.5}\n"
                    "    def set_target(self, value, rate_per_minute, mode='Settle'): pass\n"
                    "    def hold(self): pass\n"
                )
                _external_instrument(root, source)
                instrument = replace(
                    self.config.instrument_instances[0],
                    backend="isolated_test",
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root),
                    ),
                    instrument_instances=(instrument,),
                )
                descriptors = discover_system_instruments(config)
                manager = InstrumentManager(config, EventManager(), descriptors)
                self.assertFalse(marker.exists())
                await manager.connect_all()
                child_pid = int(marker.read_text(encoding="utf-8"))
                self.assertEqual(child_pid, getattr(manager.instruments["temperature"], "pid"))
                self.assertNotEqual(child_pid, os.getpid())
                snapshots = await manager.poll_all()
                self.assertEqual(snapshots["temperature"].current, 12.5)
                measurement = await manager.poll_measurement_all()
                self.assertEqual(measurement["temperature"].current, 99.5)
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_external_event_responses_cross_json_worker_boundary(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = (
                    "from labcontrol.instruments.base import EventResponseSpec, SystemInstrument\n"
                    "class IsolatedTestInstrument(SystemInstrument):\n"
                    "    def open(self): pass\n"
                    "    def close(self): pass\n"
                    "    def read_status(self): return {'value': 12.5}\n"
                    "    def event_responses(self):\n"
                    "        return (EventResponseSpec(code='TEMP_ALARM', action='zero'),)\n"
                )
                _external_instrument(root, source)
                temperature = replace(
                    self.config.instrument("temperature"),
                    backend="isolated_test",
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root),
                    ),
                    instrument_instances=(
                        temperature,
                        self.config.instrument("field"),
                    ),
                )
                descriptors = discover_system_instruments(config)
                manager = InstrumentManager(
                    config,
                    EventManager(),
                    descriptors,
                )
                try:
                    await manager.connect_all()
                    response, target = manager.event_responses[
                        ("temperature", "TEMP_ALARM", "")
                    ]
                    self.assertEqual(response.action, "zero")
                    self.assertEqual(target, "field")
                finally:
                    await manager.disconnect_all()

        asyncio.run(scenario())

    def test_declared_sequence_command_reaches_external_worker_once(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / "sequence-command.txt"
                source = (
                    "from pathlib import Path\n"
                    "from labcontrol.instruments.base import SystemInstrument\n"
                    "class IsolatedTestInstrument(SystemInstrument):\n"
                    "    def open(self): pass\n"
                    "    def close(self): pass\n"
                    "    def read_status(self): return {'value': 12.5}\n"
                    "    def set_target(self, value, rate_per_minute, mode='Settle'): pass\n"
                    "    def hold(self): pass\n"
                    "    def execute_sequence_command(self, command_id):\n"
                    f"        Path({str(marker)!r}).write_text(command_id, encoding='utf-8')\n"
                )
                _external_instrument(
                    root / "instruments",
                    source,
                    sequence_commands=(
                        '[[sequence_commands]]\n'
                        'id = "compressor_on"\n'
                        'label = "Compressor On"\n'
                    ),
                )
                instrument = replace(
                    self.config.instrument_instances[0],
                    backend="isolated_test",
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root / "instruments"),
                    ),
                    instrument_instances=(instrument,),
                )
                descriptors = discover_system_instruments(config)
                manager = InstrumentManager(config, EventManager(), descriptors)
                await manager.connect_all()
                self.assertTrue(
                    await manager.execute_sequence_command(
                        instrument.id,
                        "compressor_on",
                    )
                )
                self.assertEqual(
                    marker.read_text(encoding="utf-8"),
                    "compressor_on",
                )
                with self.assertRaisesRegex(
                    InstrumentError,
                    "sequence command is unavailable",
                ):
                    await manager.execute_sequence_command(
                        instrument.id,
                        "missing",
                    )
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_external_safety_violation_survives_ipc_and_does_not_reconnect(
        self,
    ) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = (
                    "from labcontrol.instruments.base import SystemInstrument, SafetyViolation\n"
                    "class IsolatedTestInstrument(SystemInstrument):\n"
                    "    def open(self): pass\n"
                    "    def close(self): pass\n"
                    "    def read_status(self):\n"
                    "        raise SafetyViolation('sensor fault', 'SENSOR_FAULT', 'A')\n"
                    "    def set_target(self, value, rate_per_minute, mode='Settle'): pass\n"
                    "    def hold(self): pass\n"
                )
                _external_instrument(root / "instruments", source)
                instrument = replace(
                    self.config.instrument_instances[0],
                    backend="isolated_test",
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root / "instruments"),
                    ),
                    instrument_instances=(instrument,),
                )
                descriptors = discover_system_instruments(config)
                manager = InstrumentManager(config, EventManager(), descriptors)
                await manager.connect_all()
                snapshots = await manager.poll_all()

                self.assertFalse(snapshots[instrument.id].connected)
                self.assertEqual(
                    manager.connection_state(instrument.id).value,
                    "faulted",
                )
                self.assertNotIn(instrument.id, manager._recovery_tasks)
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_blocking_driver_is_killed_without_blocking_other_instrument(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = (
                    "import time\n"
                    "from labcontrol.instruments.base import SystemInstrument\n"
                    "class IsolatedTestInstrument(SystemInstrument):\n"
                    "    def open(self): pass\n"
                    "    def close(self): pass\n"
                    "    def read_status(self):\n"
                    "        if self.config.extras.get('hang'):\n"
                    "            time.sleep(5.0)\n"
                    "        return {'value': 4.2}\n"
                )
                _external_instrument(root, source, kinds='"monitor"')
                monitor = self.config.instrument_instances[2]
                instruments = (
                    replace(
                        monitor,
                        id="healthy",
                        display_name="Healthy",
                        backend="isolated_test",
                        operation_timeout_seconds=0.15,
                        shutdown_timeout_seconds=0.15,
                        extras={"hang": False},
                    ),
                    replace(
                        monitor,
                        id="hung",
                        display_name="Hung",
                        backend="isolated_test",
                        operation_timeout_seconds=0.15,
                        shutdown_timeout_seconds=0.15,
                        extras={"hang": True},
                    ),
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root),
                        startup_timeout_seconds=1.0,
                    ),
                    instrument_instances=instruments,
                )
                descriptors = discover_system_instruments(config)
                events = EventManager()
                manager = InstrumentManager(config, events, descriptors)
                await manager.connect_all()
                healthy_pid = getattr(manager.instruments["healthy"], "pid")
                started = time.monotonic()
                snapshots = await manager.poll_all()
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 1.0)
                self.assertEqual(snapshots["healthy"].current, 4.2)
                self.assertFalse(snapshots["hung"].connected)
                self.assertEqual(
                    snapshots["hung"].connection_state.value,
                    "reconnecting",
                )
                self.assertEqual(getattr(manager.instruments["healthy"], "pid"), healthy_pid)
                self.assertIsNotNone(healthy_pid)
                self.assertIsNone(getattr(manager.instruments["hung"], "pid"))
                self.assertTrue(
                    any(
                        event.source == "hung"
                        and event.code == "INSTRUMENT_RECONNECTING"
                        for event in events.active_events()
                    )
                )
                await manager.disconnect_all()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
