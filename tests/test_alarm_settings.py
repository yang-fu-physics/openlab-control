from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication, QDialog
from labcontrol.alarm_reporting import AlarmReporter, read_alarm_token
from labcontrol.alarm_settings import AlarmSettingsStore
from labcontrol.config import AlarmReportingConfig, ConfigurationError, validate_alarm_reporting
from labcontrol.events import EventManager
from labcontrol.models import Severity
from labcontrol.runtime import RuntimeService
from labcontrol.ui.alarm_settings import AlarmSettingsDialog
from labcontrol.ui.main_window import MainWindow
from labcontrol.ui.preferences import UiPreferenceStore
from tests.configuration_fixtures import load_simulated_config


class AlarmSettingsStoreTests(unittest.TestCase):
    def test_save_reload_keeps_token_outside_settings_and_blank_qq_disables(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AlarmSettingsStore(Path(temporary) / "alarms.json")
            config = AlarmReportingConfig(enabled=True, target_qq="123456789", device_name="Test cryostat")
            saved = store.save(config, "local-test-secret")
            loaded = store.load(AlarmReportingConfig())
            self.assertEqual(loaded, saved)
            self.assertEqual(loaded.device_name, "Test cryostat")
            self.assertEqual(read_alarm_token(loaded, ROOT), "local-test-secret")
            self.assertNotIn("local-test-secret", store.path.read_text(encoding="utf-8"))
            disabled = store.save(replace(loaded, target_qq="", enabled=True), "local-test-secret")
            self.assertFalse(disabled.enabled)
            self.assertFalse(store.load(config).enabled)

    def test_invalid_qq_rejected_and_clearing_token_removes_local_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AlarmSettingsStore(Path(temporary) / "alarms.json")
            with self.assertRaises(ConfigurationError):
                store.save(AlarmReportingConfig(target_qq="not-a-qq"), "secret")
            self.assertFalse(store.path.exists())
            store.save(AlarmReportingConfig(), "secret")
            store.save(AlarmReportingConfig(), "")
            self.assertFalse(store.token_path.exists())

    def test_blank_qq_never_starts_reporter_or_sends_test(self):
        opener = Mock()
        config = AlarmReportingConfig(enabled=True, target_qq="")
        self.assertFalse(validate_alarm_reporting(config).enabled)
        reporter = AlarmReporter(config, ROOT, opener=opener)
        reporter.start()
        events = EventManager()
        events.subscribe(reporter.handle_notice)
        events.report(Severity.ERROR, "test", "TEST", "not sent")
        self.assertIsNone(reporter._thread)
        with self.assertRaisesRegex(ValueError, "QQ"):
            reporter.test_delivery("test-token")
        opener.assert_not_called()


class AlarmRuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_replacement_routes_to_new_qq_then_blank_disables(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "token.txt"
            token_file.write_text("test-token", encoding="utf-8")
            runtime = RuntimeService(load_simulated_config(), module_descriptors=())
            runtime.events = EventManager()
            runtime.events.subscribe(runtime._report_alarm_notice)
            payloads = []

            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def read(self, _size): return b'{"status":"success"}'

            def opener(request, timeout):
                payloads.append(json.loads(request.data))
                return Response()

            def factory(config):
                reporter = AlarmReporter(config, ROOT, opener=opener)
                return reporter

            config = AlarmReportingConfig(enabled=True, target_qq="123456789",
                                          token_env="", token_file=str(token_file))
            with patch.object(runtime, "_new_alarm_reporter", side_effect=factory):
                await runtime._configure_alarm_reporting(config)
                old = runtime.alarm_reporter
                runtime.events.report(Severity.WARNING, "test", "FIRST", "first recipient")
                await runtime._configure_alarm_reporting(replace(config, target_qq="987654321"))
                runtime.events.report(Severity.WARNING, "test", "SECOND", "second recipient")
                runtime._apply_alarm_delivery_state("stale delivery failure", old)
                self.assertFalse(any(event.source == "alarm_reporter" for event in runtime.events.active_events()))
                await runtime._configure_alarm_reporting(replace(config, target_qq=""))
                runtime.events.report(Severity.ERROR, "test", "DISABLED", "not sent")
                self.assertFalse(runtime.alarm_reporter.enabled)
                self.assertEqual([p["target_qq"] for p in payloads], [123456789, 987654321])
                runtime.alarm_reporter.close()

    async def test_bad_credentials_do_not_replace_existing_reporter(self):
        runtime = RuntimeService(load_simulated_config(), module_descriptors=())
        runtime.events = EventManager()
        previous = AlarmReporter(AlarmReportingConfig(), ROOT)
        runtime.alarm_reporter = previous
        with self.assertRaises(OSError):
            await runtime._configure_alarm_reporting(AlarmReportingConfig(
                enabled=True, target_qq="123456789", token_env="",
                token_file=str(ROOT / "does-not-exist" / "token.txt"),
            ))
        self.assertIs(runtime.alarm_reporter, previous)


class AlarmSettingsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_top_menu_and_personal_settings_loaded_before_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)
            store = AlarmSettingsStore(profile / "alarms.json")
            config = store.save(AlarmReportingConfig(target_qq="123456789"), "secret")
            window = MainWindow(load_simulated_config(), ui_preference_store=UiPreferenceStore(profile / "ui.ini"))
            try:
                menus = [action.text() for action in window.menuBar().actions()]
                self.assertIn("Settings", menus)
                self.assertIn(window.alarm_settings_action, window.settings_menu.actions())
                self.assertEqual(window.runtime.config.alarms.reporting, config)
            finally:
                window.close()

    def test_test_uses_edited_qq_without_saving_and_failure_stays_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AlarmSettingsStore(Path(temporary) / "alarms.json")
            runtime = SimpleNamespace(config=SimpleNamespace(project_root=ROOT), test_alarm_reporting=Mock())
            future = Future()
            runtime.test_alarm_reporting.return_value = future
            dialog = AlarmSettingsDialog(runtime, store, AlarmReportingConfig(token_env=""))
            dialog.qq.setText("123456789")
            dialog.device_name.setText("Cryostat A")
            dialog.token.setText("test-secret")
            dialog.test_button.click()
            self.assertEqual(runtime.test_alarm_reporting.call_args.args[0].target_qq, "123456789")
            self.assertEqual(runtime.test_alarm_reporting.call_args.args[0].device_name, "Cryostat A")
            self.assertFalse(store.path.exists())
            future.set_exception(TimeoutError("receiver timed out"))
            dialog._check_pending()
            self.assertIn("receiver timed out", dialog.status.text())
            self.assertTrue(dialog.test_button.isEnabled())
            self.assertEqual(dialog.token.echoMode(), dialog.token.EchoMode.Password)
            dialog.reject()

    def test_clearing_qq_saves_disabled_and_closes_after_runtime_applies(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AlarmSettingsStore(Path(temporary) / "alarms.json")
            runtime = SimpleNamespace(config=SimpleNamespace(project_root=ROOT), configure_alarm_reporting=Mock())
            future = Future()
            runtime.configure_alarm_reporting.return_value = future
            dialog = AlarmSettingsDialog(runtime, store, AlarmReportingConfig(
                enabled=True, token_env="", target_qq="123456789"))
            saved = []
            dialog.settingsSaved.connect(saved.append)
            dialog.qq.clear()
            self.assertFalse(dialog.test_button.isEnabled())
            dialog._save()
            self.assertFalse(runtime.configure_alarm_reporting.call_args.args[0].enabled)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            future.set_result(None)
            dialog._check_pending()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertFalse(saved[0].enabled)
            self.assertEqual(store.load(AlarmReportingConfig()).target_qq, "")
