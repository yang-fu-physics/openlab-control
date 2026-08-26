from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.instruments.base import InstrumentError  # noqa: E402
from labcontrol.instruments.manifest import discover_system_instruments  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.models import (  # noqa: E402
    InstrumentConnectionState,
    Severity,
)
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from tests.configuration_fixtures import load_simulated_config  # noqa: E402


def _write_instrument(
    directory: Path,
    source: str,
    *,
    kinds: str = '"temperature"',
) -> None:
    directory.mkdir(parents=True)
    monitor = kinds == '"monitor"'
    panel = (
        '[[panels]]\nid = "main"\nlabel = "Value"\n'
        'template = "readout"\nreadings = ["value"]\n'
        if monitor
        else (
            '[[controls]]\nid = "main"\nlabel = "Value"\n'
            '[[panels]]\nid = "main"\nlabel = "Value"\n'
            'template = "controller"\ncontrol = "main"\n'
            'reading_options = ["value"]\ndefault_reading = "value"\n'
            'min_value = 1.0\nmax_value = 400.0\n'
            'default_rate_per_minute = 1.0\nmax_rate_per_minute = 30.0\n'
            'stability_tolerance = 0.1\n'
            'stability_max_slope_per_minute = 0.1\n'
            'stability_dwell_seconds = 1.0\n'
            'stability_timeout_seconds = 10.0\n'
            'stability_window_seconds = 1.0\n'
        )
    )
    (directory / "instrument.toml").write_text(
        (
            'id = "recovery_test"\n'
            'name = "Recovery Test"\n'
            'version = "0.1.0"\n'
            'api_version = "4"\n'
            'backend = "backend:RecoveryInstrument"\n'
            f"kinds = [{kinds}]\n"
            f"{panel}"
            '[readings.value]\nlabel = "Value"\nunit = "K"\n'
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
        f"FAILURE = Path({str(failure_file)!r})\n"
        f"{marker_setup}"
        "class RecoveryInstrument(SystemInstrument):\n"
        "    def __init__(self, config):\n"
        "        super().__init__(config)\n"
        "        self.connected = False\n"
        "        self.target = config.initial_value\n"
        "        self.rate = config.default_rate_per_minute\n"
        "    def open(self):\n"
        f"{connect_body}"
        "    def close(self):\n"
        "        self.connected = False\n"
        "    def read_status(self):\n"
        "        if FAILURE.exists():\n"
        "            raise InstrumentError('temporary link failure', 'LINK_LOST')\n"
        "        return {'value': self.target, 'target': self.target, 'rate': self.rate}\n"
        "    def set_target(self, value, rate_per_minute, mode='Settle', *, control):\n"
        "        del mode, control\n"
        f"{set_body}"
        "        self.rate = rate_per_minute\n"
        "    def hold(self, *, control):\n"
        "        del control\n"
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
        base = load_simulated_config()
        instrument_root = root / "instruments" / "recovery_test"
        _write_instrument(
            instrument_root,
            source,
            kinds='"monitor"' if monitor else '"temperature"',
        )
        if monitor:
            selected = replace(
                base.instrument("second_stage"),
                backend="recovery_test",
                control_enabled=False,
                operation_timeout_seconds=operation_timeout,
                shutdown_timeout_seconds=0.1,
            )
        else:
            selected = replace(
                base.instrument("temperature"),
                backend="recovery_test",
                control_enabled=True,
                operation_timeout_seconds=operation_timeout,
                shutdown_timeout_seconds=0.1,
            )
        config = replace(
            base,
            system_instruments=replace(
                base.system_instruments,
                directory=str(root / "instruments"),
                startup_timeout_seconds=0.8,
                reconnect_timeout_seconds=reconnect_timeout,
                reconnect_interval_seconds=0.02,
            ),
            instrument_instances=(selected,),
        )
        descriptors = discover_system_instruments(config)
        self.assertEqual(len(descriptors), 1)
        self.assertTrue(descriptors[0].can_load, descriptors[0].error)
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
                        control="main",
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
