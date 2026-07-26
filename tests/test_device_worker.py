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

from labcontrol.config import load_config  # noqa: E402
from labcontrol.devices.manifest import discover_device_plugins  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.extensions.trust import PluginTrustStore  # noqa: E402
from labcontrol.plugins import DeviceManager  # noqa: E402


def _external_plugin(root: Path, source: str, kinds: str = '"temperature"') -> Path:
    plugin = root / "isolated_test"
    plugin.mkdir(parents=True)
    (plugin / "device.toml").write_text(
        (
            'id = "isolated_test"\n'
            'name = "Isolated Test"\n'
            'version = "0.1.0"\n'
            'api_version = "1.0"\n'
            'backend = "backend:IsolatedTestDevice"\n'
            f"kinds = [{kinds}]\n"
        ),
        encoding="utf-8",
    )
    (plugin / "backend.py").write_text(source, encoding="utf-8")
    return plugin


class DeviceWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "configs" / "default.toml")

    def test_builtin_devices_run_in_distinct_child_processes_and_close(self) -> None:
        async def scenario() -> None:
            manager = DeviceManager(self.config, EventManager())
            await manager.connect_all()
            pids = {
                device_id: getattr(client, "pid", None)
                for device_id, client in manager.devices.items()
            }
            self.assertEqual(len(set(pids.values())), len(self.config.devices))
            self.assertNotIn(None, pids.values())
            self.assertNotIn(os.getpid(), pids.values())
            snapshots = await manager.poll_all()
            self.assertEqual(set(snapshots), {"temperature", "field", "second_stage"})
            await manager.disconnect_all()
            self.assertTrue(
                all(getattr(client, "pid", None) is None for client in manager.devices.values())
            )

        asyncio.run(scenario())

    def test_external_backend_is_imported_only_inside_its_worker(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / "backend-pid.txt"
                source = (
                    "import os\n"
                    "import time\n"
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
                    "from labcontrol.devices.base import DevicePlugin\n"
                    "from labcontrol.models import DeviceSnapshot\n"
                    "class IsolatedTestDevice(DevicePlugin):\n"
                    "    async def connect(self): pass\n"
                    "    async def disconnect(self): pass\n"
                    "    async def poll(self):\n"
                    "        return DeviceSnapshot(self.config.id, self.config.display_name, "
                    "self.config.kind, time.monotonic(), True, self.config.unit, 12.5)\n"
                    "    async def set_target(self, value, rate_per_minute, mode='Settle'): pass\n"
                    "    async def hold(self): pass\n"
                )
                _external_plugin(root, source)
                state = root / "state"
                device = replace(
                    self.config.devices[0],
                    plugin="isolated_test",
                )
                config = replace(
                    self.config,
                    plugins=replace(
                        self.config.plugins,
                        device_directory=str(root),
                        state_directory=str(state),
                    ),
                    devices=(device,),
                )
                descriptors = discover_device_plugins(config)
                PluginTrustStore(state / "trusted_plugins.json").trust(
                    "device",
                    descriptors[0],
                )
                manager = DeviceManager(config, EventManager(), descriptors)
                self.assertFalse(marker.exists())
                await manager.connect_all()
                child_pid = int(marker.read_text(encoding="utf-8"))
                self.assertEqual(child_pid, getattr(manager.devices["temperature"], "pid"))
                self.assertNotEqual(child_pid, os.getpid())
                snapshots = await manager.poll_all()
                self.assertEqual(snapshots["temperature"].current, 12.5)
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_blocking_driver_is_killed_without_blocking_other_device(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = (
                    "import time\n"
                    "from labcontrol.devices.base import DevicePlugin\n"
                    "from labcontrol.models import DeviceSnapshot\n"
                    "class IsolatedTestDevice(DevicePlugin):\n"
                    "    async def connect(self): pass\n"
                    "    async def disconnect(self): pass\n"
                    "    async def poll(self):\n"
                    "        if self.config.extras.get('hang'):\n"
                    "            time.sleep(5.0)\n"
                    "        return DeviceSnapshot(self.config.id, self.config.display_name, "
                    "self.config.kind, time.monotonic(), True, self.config.unit, 4.2)\n"
                )
                _external_plugin(root, source, kinds='"monitor"')
                state = root / "state"
                monitor = self.config.devices[2]
                devices = (
                    replace(
                        monitor,
                        id="healthy",
                        display_name="Healthy",
                        plugin="isolated_test",
                        operation_timeout_seconds=0.15,
                        shutdown_timeout_seconds=0.15,
                        extras={"hang": False},
                    ),
                    replace(
                        monitor,
                        id="hung",
                        display_name="Hung",
                        plugin="isolated_test",
                        operation_timeout_seconds=0.15,
                        shutdown_timeout_seconds=0.15,
                        extras={"hang": True},
                    ),
                )
                config = replace(
                    self.config,
                    plugins=replace(
                        self.config.plugins,
                        device_directory=str(root),
                        state_directory=str(state),
                        device_startup_timeout_seconds=1.0,
                    ),
                    devices=devices,
                )
                descriptors = discover_device_plugins(config)
                PluginTrustStore(state / "trusted_plugins.json").trust(
                    "device",
                    descriptors[0],
                )
                events = EventManager()
                manager = DeviceManager(config, events, descriptors)
                await manager.connect_all()
                healthy_pid = getattr(manager.devices["healthy"], "pid")
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
                self.assertEqual(getattr(manager.devices["healthy"], "pid"), healthy_pid)
                self.assertIsNotNone(healthy_pid)
                self.assertIsNone(getattr(manager.devices["hung"], "pid"))
                self.assertTrue(
                    any(
                        event.source == "hung"
                        and event.code == "DEVICE_RECONNECTING"
                        for event in events.active_events()
                    )
                )
                await manager.disconnect_all()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
