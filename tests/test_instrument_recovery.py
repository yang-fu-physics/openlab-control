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
from labcontrol.instruments.base import InstrumentError  # noqa: E402
from labcontrol.instruments.manifest import discover_system_instruments  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.package_support.trust import ContentTrustStore  # noqa: E402
from labcontrol.models import (  # noqa: E402
    InstrumentConnectionState,
    InstrumentRole,
    Severity,
)
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402


def _write_instrument(
    directory: Path,
    source: str,
    *,
    kinds: str = '"temperature"',
) -> None:
    directory.mkdir(parents=True)
    (directory / "instrument.toml").write_text(
        (
            'id = "recovery_test"\n'
            'name = "Recovery Test"\n'
            'version = "0.1.0"\n'
            'api_version = "1.2"\n'
            'backend = "backend:RecoveryInstrument"\n'
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
        "from labcontrol.instruments.base import InstrumentError, SystemInstrument\n"
        "from labcontrol.models import InstrumentActivity, InstrumentSnapshot\n"
        f"FAILURE = Path({str(failure_file)!r})\n"
        f"{marker_setup}"
        "class RecoveryInstrument(SystemInstrument):\n"
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
        "            raise InstrumentError('temporary link failure', 'LINK_LOST')\n"
        "        return InstrumentSnapshot(\n"
        "            self.config.id, self.config.display_name, self.config.kind,\n"
        "            time.monotonic(), True, self.config.unit,\n"
        "            self.target, self.target, self.rate, InstrumentActivity.HOLDING,\n"
        "        )\n"
        "    async def set_target(self, value, rate_per_minute, mode='Settle'):\n"
        f"{set_body}"
        "        self.rate = rate_per_minute\n"
        "    async def hold(self):\n"
        "        if not self.connected:\n"
        "            raise InstrumentError('not connected', 'NOT_CONNECTED')\n"
    )


class InstrumentRecoveryTests(unittest.TestCase):
    def _manager(
        self,
        root: Path,
        source: str,
        events: EventManager,
        *,
        monitor: bool = False,
        reconnect_timeout: float = 0.35,
        operation_timeout: float = 0.12,
    ) -> InstrumentManager:
        base = load_config(ROOT / "configs" / "default.toml")
        instrument_root = root / "instruments" / "recovery_test"
        _write_instrument(
            instrument_root,
            source,
            kinds='"monitor"' if monitor else '"temperature"',
        )
        state = root / "state"
        if monitor:
            selected = replace(
                base.instrument("second_stage"),
                backend="recovery_test",
                role=InstrumentRole.MONITOR,
                control_enabled=False,
                operation_timeout_seconds=operation_timeout,
                shutdown_timeout_seconds=0.1,
            )
        else:
            selected = replace(
                base.instrument("temperature"),
                backend="recovery_test",
                role=InstrumentRole.PRIMARY,
                control_enabled=True,
                operation_timeout_seconds=operation_timeout,
                shutdown_timeout_seconds=0.1,
            )
        config = replace(
            base,
            system_instruments=replace(
                base.system_instruments,
                directory=str(root / "instruments"),
                state_directory=str(state),
                startup_timeout_seconds=0.8,
                reconnect_timeout_seconds=reconnect_timeout,
                reconnect_interval_seconds=0.02,
            ),
            instruments=(selected,),
        )
        descriptors = discover_system_instruments(config)
        self.assertEqual(len(descriptors), 1)
        self.assertTrue(descriptors[0].can_load, descriptors[0].error)
        ContentTrustStore(state / "trusted_content.json").trust(
            "instrument",
            descriptors[0],
        )
        return InstrumentManager(config, events, descriptors)

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
                original_pid = manager.instruments["temperature"].pid
                failure.write_text("offline", encoding="utf-8")
                snapshots = await manager.poll_all()
                self.assertEqual(
                    snapshots["temperature"].connection_state,
                    InstrumentConnectionState.RECONNECTING,
                )
                recovery = manager._recovery_tasks["temperature"]
                failure.unlink()
                await asyncio.wait_for(recovery, timeout=2.0)
                self.assertEqual(
                    manager.connection_state("temperature"),
                    InstrumentConnectionState.CONNECTED,
                )
                self.assertNotEqual(
                    manager.instruments["temperature"].pid,
                    original_pid,
                )
                self.assertTrue(manager.control_ready)
                self.assertIn(
                    "INSTRUMENT_RECONNECTING",
                    [notice.event.code for notice in notices],
                )
                self.assertTrue(
                    any(
                        notice.is_resolution
                        and notice.event.code == "INSTRUMENT_RECONNECTING"
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
                instrument_id = (
                    "second_stage"
                    if monitor
                    else "temperature"
                )
                failure.write_text("offline", encoding="utf-8")
                await manager.poll_all()
                recovery = manager._recovery_tasks[instrument_id]
                await asyncio.wait_for(recovery, timeout=2.0)
                self.assertEqual(
                    manager.connection_state(instrument_id),
                    InstrumentConnectionState.FAULTED,
                )
                failures = [
                    notice.event
                    for notice in notices
                    if notice.event.code == "INSTRUMENT_RECONNECT_FAILED"
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
                    InstrumentConnectionState.FAULTED,
                )
                self.assertIn(
                    "INSTRUMENT_STATE_MISMATCH_AFTER_RECONNECT",
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
                with self.assertRaises(InstrumentError) as raised:
                    await manager.set_target(
                        "temperature",
                        250.0,
                        2.0,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                )
                self.assertEqual(
                    manager.connection_state("temperature"),
                    InstrumentConnectionState.FAULTED,
                )
                self.assertEqual(
                    commands.read_text(encoding="utf-8").splitlines(),
                    ["250.0"],
                )
                self.assertFalse(manager._recovery_tasks)
                self.assertIn(
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    [notice.event.code for notice in notices],
                )
            finally:
                await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(scenario(Path(temporary)))


if __name__ == "__main__":
    unittest.main()
