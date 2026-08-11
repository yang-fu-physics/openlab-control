from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.devices.base import DeviceError  # noqa: E402
from labcontrol.devices.manifest import discover_device_plugins  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.extensions.trust import PluginTrustStore  # noqa: E402
from labcontrol.models import (  # noqa: E402
    DeviceConnectionState,
    DeviceRole,
    Severity,
)
from labcontrol.plugins import DeviceManager  # noqa: E402


def _write_plugin(
    directory: Path,
    source: str,
    *,
    kinds: str = '"temperature"',
) -> None:
    directory.mkdir(parents=True)
    (directory / "device.toml").write_text(
        (
            'id = "recovery_test"\n'
            'name = "Recovery Test"\n'
            'version = "0.1.0"\n'
            'api_version = "1.1"\n'
            'backend = "backend:RecoveryDevice"\n'
            f"kinds = [{kinds}]\n"
        ),
        encoding="utf-8",
    )
    (directory / "backend.py").write_text(source, encoding="utf-8")


def _source(
    *,
    failure_file: Path,
    marker_file: Path | None = None,
    command_file: Path | None = None,
) -> str:
    marker_setup = ""
    connect_body = "        self.connected = True\n"
    if marker_file is not None:
        marker_setup = f"MARKER = Path({str(marker_file)!r})\n"
        connect_body = (
            "        self.connected = True\n"
            "        if MARKER.exists():\n"
            "            self.target = self.config.initial_value + 1.0\n"
            "        else:\n"
            "            MARKER.write_text('started', encoding='utf-8')\n"
        )
    set_body = (
        "        self.target = value\n"
        if command_file is None
        else (
            f"        with Path({str(command_file)!r}).open('a', encoding='utf-8') as stream:\n"
            "            stream.write(f'{value}\\n')\n"
            "        time.sleep(5.0)\n"
        )
    )
    return (
        "import time\n"
        "from pathlib import Path\n"
        "from labcontrol.devices.base import DeviceError, DevicePlugin\n"
        "from labcontrol.models import DeviceActivity, DeviceSnapshot\n"
        f"FAILURE = Path({str(failure_file)!r})\n"
        f"{marker_setup}"
        "class RecoveryDevice(DevicePlugin):\n"
        "    def __init__(self, config, simulation_speed=1.0):\n"
        "        super().__init__(config, simulation_speed)\n"
        "        self.connected = False\n"
        "        self.target = config.initial_value\n"
        "        self.rate = config.default_rate_per_minute\n"
        "    async def connect(self):\n"
        f"{connect_body}"
        "    async def disconnect(self):\n"
        "        self.connected = False\n"
        "    async def poll(self):\n"
        "        if FAILURE.exists():\n"
        "            raise DeviceError('temporary link failure', 'LINK_LOST')\n"
        "        return DeviceSnapshot(\n"
        "            self.config.id, self.config.display_name, self.config.kind,\n"
        "            time.monotonic(), True, self.config.unit,\n"
        "            self.target, self.target, self.rate, DeviceActivity.HOLDING,\n"
        "        )\n"
        "    async def set_target(self, value, rate_per_minute, mode='Settle'):\n"
        f"{set_body}"
        "        self.rate = rate_per_minute\n"
        "    async def hold(self):\n"
        "        if not self.connected:\n"
        "            raise DeviceError('not connected', 'NOT_CONNECTED')\n"
    )


class DeviceRecoveryTests(unittest.TestCase):
    def _manager(
        self,
        root: Path,
        source: str,
        events: EventManager,
        *,
        monitor: bool = False,
        reconnect_timeout: float = 0.35,
        operation_timeout: float = 0.12,
    ) -> DeviceManager:
        base = load_config(ROOT / "configs" / "default.toml")
        plugin_root = root / "plugins" / "recovery_test"
        _write_plugin(
            plugin_root,
            source,
            kinds='"monitor"' if monitor else '"temperature"',
        )
        state = root / "state"
        if monitor:
            selected = replace(
                base.device("second_stage"),
                plugin="recovery_test",
                role=DeviceRole.MONITOR,
                control_enabled=False,
                operation_timeout_seconds=operation_timeout,
                shutdown_timeout_seconds=0.1,
            )
        else:
            selected = replace(
                base.device("temperature"),
                plugin="recovery_test",
                role=DeviceRole.PRIMARY,
                control_enabled=True,
                operation_timeout_seconds=operation_timeout,
                shutdown_timeout_seconds=0.1,
            )
        config = replace(
            base,
            plugins=replace(
                base.plugins,
                device_directory=str(root / "plugins"),
                state_directory=str(state),
                device_startup_timeout_seconds=0.8,
                device_reconnect_timeout_seconds=reconnect_timeout,
                device_reconnect_interval_seconds=0.02,
            ),
            devices=(selected,),
        )
        descriptors = discover_device_plugins(config)
        self.assertEqual(len(descriptors), 1)
        self.assertTrue(descriptors[0].can_load, descriptors[0].error)
        PluginTrustStore(state / "trusted_plugins.json").trust(
            "device",
            descriptors[0],
        )
        return DeviceManager(config, events, descriptors)

    def test_transient_read_failure_restarts_worker_and_resolves_warning(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            failure = root / "failure.flag"
            notices = []
            events = EventManager()
            events.subscribe(notices.append)
            manager = self._manager(
                root,
                _source(failure_file=failure),
                events,
            )
            try:
                await manager.connect_all()
                await manager.poll_all()
                original_pid = manager.devices["temperature"].pid
                failure.write_text("offline", encoding="utf-8")
                snapshots = await manager.poll_all()
                self.assertEqual(
                    snapshots["temperature"].connection_state,
                    DeviceConnectionState.RECONNECTING,
                )
                recovery = manager._recovery_tasks["temperature"]
                failure.unlink()
                await asyncio.wait_for(recovery, timeout=2.0)
                self.assertEqual(
                    manager.connection_state("temperature"),
                    DeviceConnectionState.CONNECTED,
                )
                self.assertNotEqual(
                    manager.devices["temperature"].pid,
                    original_pid,
                )
                self.assertTrue(manager.control_ready)
                self.assertIn(
                    "DEVICE_RECONNECTING",
                    [notice.event.code for notice in notices],
                )
                self.assertTrue(
                    any(
                        notice.is_resolution
                        and notice.event.code == "DEVICE_RECONNECTING"
                        for notice in notices
                    )
                )
            finally:
                await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(scenario(Path(temporary)))

    def test_reconnect_deadline_faults_primary_but_only_warns_monitor(
        self,
    ) -> None:
        async def scenario(root: Path, monitor: bool) -> None:
            failure = root / "failure.flag"
            notices = []
            events = EventManager()
            events.subscribe_occurrences(notices.append)
            manager = self._manager(
                root,
                _source(failure_file=failure),
                events,
                monitor=monitor,
                reconnect_timeout=0.18,
                operation_timeout=0.08,
            )
            try:
                await manager.connect_all()
                await manager.poll_all()
                device_id = (
                    "second_stage"
                    if monitor
                    else "temperature"
                )
                failure.write_text("offline", encoding="utf-8")
                await manager.poll_all()
                recovery = manager._recovery_tasks[device_id]
                await asyncio.wait_for(recovery, timeout=2.0)
                self.assertEqual(
                    manager.connection_state(device_id),
                    DeviceConnectionState.FAULTED,
                )
                failures = [
                    notice.event
                    for notice in notices
                    if notice.event.code == "DEVICE_RECONNECT_FAILED"
                ]
                self.assertTrue(failures)
                self.assertEqual(
                    failures[-1].severity,
                    Severity.WARNING if monitor else Severity.ERROR,
                )
            finally:
                await manager.disconnect_all()

        for monitor in (False, True):
            with self.subTest(monitor=monitor):
                with tempfile.TemporaryDirectory() as temporary:
                    asyncio.run(
                        scenario(Path(temporary), monitor)
                    )

    def test_recovered_target_mismatch_is_a_terminal_fault(self) -> None:
        async def scenario(root: Path) -> None:
            failure = root / "failure.flag"
            marker = root / "started.flag"
            notices = []
            events = EventManager()
            events.subscribe_occurrences(notices.append)
            manager = self._manager(
                root,
                _source(
                    failure_file=failure,
                    marker_file=marker,
                ),
                events,
            )
            try:
                await manager.connect_all()
                await manager.poll_all()
                failure.write_text("offline", encoding="utf-8")
                await manager.poll_all()
                failure.unlink()
                recovery = manager._recovery_tasks["temperature"]
                await asyncio.wait_for(recovery, timeout=2.0)
                self.assertEqual(
                    manager.connection_state("temperature"),
                    DeviceConnectionState.FAULTED,
                )
                self.assertIn(
                    "DEVICE_STATE_MISMATCH_AFTER_RECONNECT",
                    [notice.event.code for notice in notices],
                )
            finally:
                await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(scenario(Path(temporary)))

    def test_timed_out_write_is_never_replayed(self) -> None:
        async def scenario(root: Path) -> None:
            failure = root / "unused.flag"
            commands = root / "commands.log"
            notices = []
            events = EventManager()
            events.subscribe_occurrences(notices.append)
            manager = self._manager(
                root,
                _source(
                    failure_file=failure,
                    command_file=commands,
                ),
                events,
                reconnect_timeout=0.2,
                operation_timeout=0.08,
            )
            try:
                await manager.connect_all()
                await manager.poll_all()
                with self.assertRaises(DeviceError) as raised:
                    await manager.set_target(
                        "temperature",
                        250.0,
                        2.0,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "DEVICE_WRITE_RESULT_UNKNOWN",
                )
                self.assertEqual(
                    manager.connection_state("temperature"),
                    DeviceConnectionState.FAULTED,
                )
                self.assertEqual(
                    commands.read_text(encoding="utf-8").splitlines(),
                    ["250.0"],
                )
                self.assertFalse(manager._recovery_tasks)
                self.assertIn(
                    "DEVICE_WRITE_RESULT_UNKNOWN",
                    [notice.event.code for notice in notices],
                )
            finally:
                await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(scenario(Path(temporary)))


if __name__ == "__main__":
    unittest.main()
