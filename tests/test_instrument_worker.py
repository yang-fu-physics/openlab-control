from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.instruments.manifest import (  # noqa: E402
    instrument_dependency_directory,
    discover_system_instruments,
)
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.package_support.trust import (  # noqa: E402
    ContentTrustStore,
    content_tree_digest,
)
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402


def _external_instrument(
    root: Path,
    source: str,
    kinds: str = '"temperature"',
    dependencies: str = "",
) -> Path:
    instrument = root / "isolated_test"
    instrument.mkdir(parents=True)
    panel_template = "readout" if kinds == '"monitor"' else "controller"
    (instrument / "instrument.toml").write_text(
        (
            'id = "isolated_test"\n'
            'name = "Isolated Test"\n'
            'version = "0.1.0"\n'
            'api_version = "3"\n'
            'backend = "backend:IsolatedTestInstrument"\n'
            f"kinds = [{kinds}]\n"
            + (
                f"dependencies = [{dependencies}]\n"
                if dependencies
                else ""
            )
            + 'main_reading = "value"\n'
            + '[panel]\n'
            + f'template = "{panel_template}"\n'
            + '[readings.value]\nlabel = "Value"\nunit = "K"\n'
        ),
        encoding="utf-8",
    )
    (instrument / "backend.py").write_text(source, encoding="utf-8")
    return instrument


class InstrumentWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "configs" / "default.toml")

    def test_builtin_instruments_run_in_distinct_child_processes_and_close(self) -> None:
        async def scenario() -> None:
            manager = InstrumentManager(self.config, EventManager())
            await manager.connect_all()
            pids = {
                instrument_id: getattr(client, "pid", None)
                for instrument_id, client in manager.instruments.items()
            }
            self.assertEqual(len(set(pids.values())), len(self.config.instruments))
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
                state = root / "state"
                instrument = replace(
                    self.config.instruments[0],
                    backend="isolated_test",
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root),
                        state_directory=str(state),
                    ),
                    instruments=(instrument,),
                )
                descriptors = discover_system_instruments(config)
                ContentTrustStore(state / "trusted_content.json").trust(
                    "instrument",
                    descriptors[0],
                )
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
                state = root / "state"
                instrument = replace(
                    self.config.instruments[0],
                    backend="isolated_test",
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root / "instruments"),
                        state_directory=str(state),
                    ),
                    instruments=(instrument,),
                )
                descriptors = discover_system_instruments(config)
                ContentTrustStore(state / "trusted_content.json").trust(
                    "instrument",
                    descriptors[0],
                )
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

    def test_external_instrument_dependency_is_visible_only_in_worker(
        self,
    ) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = (
                    "from instrument_only_demo import VALUE\n"
                    "from labcontrol.instruments.base import SystemInstrument\n"
                    "class IsolatedTestInstrument(SystemInstrument):\n"
                    "    def open(self): pass\n"
                    "    def close(self): pass\n"
                    "    def read_status(self): return {'value': VALUE}\n"
                    "    def set_target("
                    "self, value, rate_per_minute, mode='Settle'"
                    "): pass\n"
                    "    def hold(self): pass\n"
                )
                instrument = _external_instrument(
                    root / "instruments",
                    source,
                    dependencies='"instrument-only-demo==1.0.0"',
                )
                (instrument / "requirements.lock").write_text(
                    "instrument-only-demo==1.0.0 --hash=sha256:"
                    + "0" * 64
                    + "\n",
                    encoding="utf-8",
                )
                state = root / "state"
                instrument = replace(
                    self.config.instruments[0],
                    backend="isolated_test",
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root / "instruments"),
                        state_directory=str(state),
                        runtime_directory=str(root / "runtime"),
                    ),
                    instruments=(instrument,),
                )
                descriptor = discover_system_instruments(config)[0]
                dependency_root = instrument_dependency_directory(
                    config,
                    descriptor,
                )
                package = dependency_root / "instrument_only_demo"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text(
                    "VALUE = 17.5\n",
                    encoding="utf-8",
                )
                metadata = (
                    dependency_root
                    / "instrument_only_demo-1.0.0.dist-info"
                )
                metadata.mkdir()
                (metadata / "METADATA").write_text(
                    "Metadata-Version: 2.1\n"
                    "Name: instrument-only-demo\n"
                    "Version: 1.0.0\n",
                    encoding="utf-8",
                )
                (dependency_root.parent / "runtime.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "fingerprint": descriptor.fingerprint,
                            "requirements": list(
                                descriptor.dependencies
                            ),
                            "runtime_digest": (
                                content_tree_digest(
                                    dependency_root
                                )
                            ),
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                ContentTrustStore(
                    state / "trusted_content.json"
                ).trust("instrument", descriptor)
                manager = InstrumentManager(
                    config,
                    EventManager(),
                    (descriptor,),
                )
                try:
                    self.assertNotIn(
                        "instrument_only_demo",
                        sys.modules,
                    )
                    await manager.connect_all()
                    snapshots = await manager.poll_all()
                    self.assertEqual(
                        snapshots["temperature"].current,
                        17.5,
                    )
                    self.assertNotIn(
                        "instrument_only_demo",
                        sys.modules,
                    )
                finally:
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
                state = root / "state"
                monitor = self.config.instruments[2]
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
                        state_directory=str(state),
                        startup_timeout_seconds=1.0,
                    ),
                    instruments=instruments,
                )
                descriptors = discover_system_instruments(config)
                ContentTrustStore(state / "trusted_content.json").trust(
                    "instrument",
                    descriptors[0],
                )
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
