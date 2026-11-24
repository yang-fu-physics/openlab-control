from __future__ import annotations

import asyncio
from concurrent.futures import Future
import csv
from dataclasses import replace
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication, QSpinBox
from labcontrol.config import InstrumentReadingConfig
from labcontrol.events import EventManager
from labcontrol.models import (InstrumentConnectionState, InstrumentControlState, InstrumentMetric,
                               InstrumentSnapshot, RunState, RuntimeMessage, Severity)
from labcontrol.runtime import RuntimeService
from labcontrol.system_log import SystemLogRecorder, system_log_fields
from labcontrol.ui.main_window import MainWindow
from labcontrol.ui.system_log import SystemLogDialog
from tests.configuration_fixtures import load_simulated_config


def log_fixture():
    config = load_simulated_config()
    instrument = config.instrument("temperature")
    instrument = replace(instrument,
                         readings=instrument.readings + (InstrumentReadingConfig("heater_range", "Heater Range"),),
                         auxiliary_readings=instrument.auxiliary_readings + ("heater_range",))
    config = replace(config, instrument_instances=tuple(
        instrument if i.id == instrument.id else i for i in config.instrument_instances))
    snapshot = InstrumentSnapshot(instrument.id, instrument.display_name, instrument.kind, 10.0,
                                  connection_state=InstrumentConnectionState.CONNECTED,
                                  controls={"main": InstrumentControlState(current=300.0, target=305.0)},
                                  metrics={"heater_range": InstrumentMetric("Heater Range", "LOW")})
    return config, {instrument.id: snapshot}


def log_rows(path):
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8").split("[Data]\n", 1)[1])))


class SystemLogRecorderTests(unittest.TestCase):
    def test_selected_cached_values_and_same_name_append_without_duplicate_header(self):
        config, snapshots = log_fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.log"
            log = SystemLogRecorder(config)
            fields = ("temperature.control:main:current", "temperature.metric:heater_range")
            log.start(str(path), 30, fields, snapshots)
            snapshots["temperature"].controls["main"].current = 301.25
            snapshots["temperature"].metrics["heater_range"] = InstrumentMetric("Heater Range", "HI")
            log.write(snapshots)
            log.close()
            first = path.read_bytes()
            log.start(str(path), 10, fields, snapshots)
            log.close()
            self.assertTrue(path.read_bytes().startswith(first))
            self.assertEqual(path.read_text(encoding="utf-8").count("[Data]"), 1)
            rows = log_rows(path)
            self.assertEqual(len(rows), 4)
            self.assertEqual(len(rows[0]), 3)
            self.assertEqual([row[-1] for row in rows[1:]], ["LOW", "HI", "HI"])
            self.assertEqual(float(rows[2][1]), 301.25)
            self.assertNotIn("Target", ",".join(rows[0]))
            self.assertEqual(list(Path(directory).glob("*.log")), [path])

    def test_incompatible_append_is_rejected_without_changing_original(self):
        config, snapshots = log_fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.log"
            log = SystemLogRecorder(config)
            fields = ("temperature.control:main:current",)
            log.start(str(path), 30, fields, snapshots)
            log.close()
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                log.start(str(path), 30, ("temperature.metric:heater_range",), snapshots)
            self.assertEqual(path.read_bytes(), original)
            other = SystemLogRecorder(replace(config, logging=replace(config.logging, timestamp_epoch="unix")))
            with self.assertRaises(ValueError):
                other.start(str(path), 30, fields, snapshots)
            self.assertEqual(path.read_bytes(), original)

    def test_interval_is_positive_integer_and_invalid_selections_do_not_create_files(self):
        config, snapshots = log_fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.log"
            log = SystemLogRecorder(config)
            for interval in (0, -1, 1.5, True):
                with self.subTest(interval=interval), self.assertRaises(ValueError):
                    log.start(str(path), interval, ("temperature.control:main:current",), snapshots)
            for fields in ((), ("unknown",), ("temperature.metric:heater_range",) * 2):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    log.start(str(path), 30, fields, snapshots)
            self.assertFalse(path.exists())

    def test_missing_reading_is_blank_and_connection_state_is_retained(self):
        config, snapshots = log_fixture()
        snapshot = snapshots["temperature"]
        snapshot.connection_state = InstrumentConnectionState.DISCONNECTED
        snapshot.controls["main"].current = None
        snapshot.metrics.clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.log"
            log = SystemLogRecorder(config)
            log.start(str(path), 30, ("temperature.control:main:current", "temperature.metric:heater_range",
                                      "temperature.connection_state"), snapshots)
            log.close()
            self.assertEqual(log_rows(path)[1][1:], ["", "", "disconnected"])


class SystemLogRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_log_uses_cache_only_and_does_not_change_polling_across_seq_states(self):
        config, snapshots = log_fixture()
        runtime = RuntimeService(config, module_descriptors=(), instrument_descriptors=())
        runtime.events = EventManager()
        runtime.instruments = SimpleNamespace(snapshots=Mock(return_value=snapshots), poll_all=AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.log"
            idle_interval = runtime._instrument_poll_interval()
            await runtime._start_system_log(str(path), 30, ("temperature.control:main:current",))
            self.assertEqual(runtime._instrument_poll_interval(), idle_interval)
            intervals = []

            async def tick(interval):
                intervals.append(interval)
                if len(intervals) > 2:
                    raise asyncio.CancelledError
                runtime.engine = SimpleNamespace(state=RunState.RUNNING if len(intervals) == 1 else RunState.COMPLETED)
                snapshots["temperature"].controls["main"].current += 1

            with patch("labcontrol.runtime.asyncio.sleep", side_effect=tick):
                with self.assertRaises(asyncio.CancelledError):
                    await runtime._system_log_task
            self.assertTrue(runtime.system_log.active)
            self.assertEqual(runtime._instrument_poll_interval(), idle_interval)
            await runtime._stop_system_log()
            self.assertEqual(intervals, [30, 30, 30])
            self.assertEqual(len(log_rows(path)), 4)
            runtime.instruments.poll_all.assert_not_awaited()

    async def test_disk_error_stops_log_and_reports_warning_without_stopping_seq(self):
        config, snapshots = log_fixture()
        runtime = RuntimeService(config, module_descriptors=(), instrument_descriptors=())
        runtime.events = EventManager()
        runtime.instruments = SimpleNamespace(snapshots=Mock(return_value=snapshots), poll_all=AsyncMock())
        runtime.engine = SimpleNamespace(state=RunState.RUNNING, request_stop=Mock())
        with tempfile.TemporaryDirectory() as directory:
            await runtime._start_system_log(str(Path(directory) / "status.log"), 30,
                                            ("temperature.control:main:current",))
            with patch("labcontrol.runtime.asyncio.sleep", new=AsyncMock()), \
                 patch.object(runtime.system_log, "write", side_effect=OSError("disk full")):
                await runtime._system_log_task
            self.assertFalse(runtime.system_log.active)
            self.assertEqual(runtime.events.active_events()[0].severity, Severity.WARNING)
            self.assertIn("disk full", runtime.events.active_events()[0].message)
            runtime.engine.request_stop.assert_not_called()
            runtime.instruments.poll_all.assert_not_awaited()
            await runtime._stop_system_log()

    async def test_shutdown_closes_active_manual_log(self):
        config, snapshots = log_fixture()
        runtime = RuntimeService(config, module_descriptors=(), instrument_descriptors=())
        runtime.events = EventManager()
        runtime.instruments = SimpleNamespace(snapshots=Mock(return_value=snapshots), disconnect_all=AsyncMock())
        runtime._loop = SimpleNamespace(call_soon=Mock(), stop=Mock())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.log"
            await runtime._start_system_log(str(path), 30, ("temperature.control:main:current",))
            task = runtime._system_log_task
            await runtime._shutdown_async()
            self.assertTrue(task.cancelled())
            self.assertFalse(runtime.system_log.active)
            path.rename(path.with_suffix(".closed.log"))


class SystemLogUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_integer_default_tabs_same_model_instances_and_item_selection(self):
        config, snapshots = log_fixture()
        first = config.instrument("temperature")
        second = replace(first, id="temperature_2", panels=tuple(replace(p, instrument_id="temperature_2") for p in first.panels))
        config = replace(config, instrument_instances=(first, second))
        dialog = SystemLogDialog(config, "status.log")
        try:
            self.assertIsInstance(dialog.interval_input, QSpinBox)
            self.assertEqual(dialog.interval_input.value(), 30)
            self.assertEqual(dialog.interval_input.minimum(), 1)
            self.assertEqual(dialog.tabs.count(), 2)
            self.assertIn("temperature_2", dialog.tabs.tabText(1))
            self.assertIn("temperature.metric:heater_range", dialog.checkboxes)
            self.assertIn("temperature_2.metric:heater_range", dialog.checkboxes)
            self.assertEqual(len({f.id for f in system_log_fields(config)}), len(dialog.checkboxes))
            for key, box in dialog.checkboxes.items():
                box.setChecked(key == "temperature.metric:heater_range")
            requests = []
            dialog.startRequested.connect(lambda path, interval, fields: requests.append((path, interval, fields)))
            dialog.start_button.click()
            self.assertEqual(requests, [("status.log", 30, ("temperature.metric:heater_range",))])
        finally:
            dialog.close()

    def test_busy_error_and_hiding_preserve_recording(self):
        config, _ = log_fixture()
        dialog = SystemLogDialog(config, "status.log")
        try:
            future = Future()
            dialog.set_pending(future)
            self.assertFalse(dialog.start_button.isEnabled())
            self.assertFalse(dialog.interval_input.isEnabled())
            future.set_exception(OSError("cannot append"))
            with patch("labcontrol.ui.system_log.QMessageBox.warning") as warning:
                dialog._check_operation()
            warning.assert_called_once()
            self.assertTrue(dialog.start_button.isEnabled())
            stops = []
            dialog.stopRequested.connect(lambda: stops.append(True))
            dialog.set_state({"active": True, "path": "status.log", "interval_seconds": 30, "fields": []})
            dialog.show()
            dialog.hide()
            self.assertEqual(stops, [])
            self.assertFalse(dialog.tabs.isEnabled())
            self.assertTrue(dialog.stop_button.isEnabled())
        finally:
            dialog.close()

    def test_main_window_defaults_log_to_current_dat_folder_and_keeps_action_enabled_during_run(self):
        config, _ = log_fixture()
        window = MainWindow(config)
        try:
            with tempfile.TemporaryDirectory() as directory:
                selected = Path(directory) / "measurement.dat"
                window.current_data_file = selected
                window._open_system_log()
                self.assertEqual(Path(window.system_log_dialog.path_input.text()).parent, selected.parent)
                self.assertEqual(window.system_log_dialog.interval_input.value(), 30)
                window._set_runtime_editable(False)
                self.assertTrue(window.system_log_action.isEnabled())
                state = {"active": True, "path": str(selected.with_suffix(".log")), "interval_seconds": 30, "fields": []}
                window.runtime.messages.put(RuntimeMessage("system_log_state", state))
                window._drain_runtime_messages()
                self.assertIn("Recording", window.system_log_action.text())
                self.assertIn(str(selected.with_suffix(".log")), window.system_log_dialog.status_label.text())
                self.assertFalse(selected.exists())
        finally:
            window.close()
