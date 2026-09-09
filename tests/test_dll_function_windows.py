from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402
from shiboken6 import isValid  # noqa: E402
from labcontrol.dll_functions import normalize_dll_functions  # noqa: E402
from labcontrol.instruments.base import InstrumentWarning  # noqa: E402
from labcontrol.measurement.manifest import ModuleDescriptor  # noqa: E402
from labcontrol.models import RunState  # noqa: E402
from labcontrol.ui.dll_functions import DLLFunctionDialog  # noqa: E402
from labcontrol.ui.main_window import MainWindow  # noqa: E402
from tests.configuration_fixtures import load_simulated_config  # noqa: E402
from tests.test_dll_functions import FIXTURE, FUNCTIONS, PARAMETERS  # noqa: E402


class DLLFunctionWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.spec = normalize_dll_functions(FUNCTIONS)[0]

    def tearDown(self):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_form_preserves_double_precision_and_rejects_invalid_values(self):
        dialog = DLLFunctionDialog("module", "dll_test", "DLL Test", self.spec)
        try:
            self.assertEqual(len(dialog.findChildren(QComboBox)), 2)
            self.assertEqual(dialog.parameters(), PARAMETERS)
            dialog.inputs["level"].setText("1.234567890123456e-12")
            self.assertEqual(dialog.parameters()["level"], 1.234567890123456e-12)
            requested = []
            dialog.runRequested.connect(requested.append)
            for field, value in (("level", "nan"), ("level", "2"), ("samples", "1.5")):
                with self.subTest(field=field, value=value):
                    dialog.inputs["level"].setText("1e-12")
                    dialog.inputs["samples"].setText("10")
                    dialog.inputs[field].setText(value)
                    with patch("labcontrol.ui.dll_functions.QMessageBox.warning") as warning:
                        dialog.run_button.click()
                    warning.assert_called_once()
            self.assertEqual(requested, [])
        finally:
            dialog.close()

    def test_result_display_clears_previous_values_on_failed_call(self):
        dialog = DLLFunctionDialog("instrument", "temperature", "Temperature", self.spec)
        try:
            dialog.show_result({"value": 1e-12, "calls": 1, "pid": 10})
            self.assertEqual(dialog.outputs["value"].text(), "1e-12")
            self.assertTrue(all(widget.isReadOnly() for widget in dialog.outputs.values()))
            dialog.set_busy(True)
            self.assertFalse(dialog.run_button.isEnabled())
            self.assertTrue(all(not widget.text() for widget in dialog.outputs.values()))
            dialog.show_error("Range error")
            self.assertIn("Range error", dialog.status_label.text())
            dialog.set_runtime_editable(False)
            dialog.set_busy(False)
            self.assertFalse(dialog.run_button.isEnabled())
        finally:
            dialog.close()

    def test_menu_supports_multiple_windows_and_completion_after_window_close(self):
        with (
            patch("labcontrol.ui.main_window.RuntimeService.start"),
            patch("labcontrol.ui.main_window.RuntimeService.shutdown"),
        ):
            window = MainWindow(load_simulated_config())
            try:
                window._set_instrument_dll_functions([{
                    "instrument_id": "temperature", "display_name": "Temperature",
                    "functions": FUNCTIONS,
                }])
                action = window._instrument_dll_menus["temperature"].actions()[0]
                action.trigger()
                action.trigger()
                self.assertEqual(len(window._dll_function_dialogs), 2)
                first, second = window._dll_function_dialogs.values()
                future = Future()
                with patch.object(window.runtime, "instrument_dll_function", return_value=future) as call:
                    first.run_button.click()
                    call.assert_called_once_with("temperature", "diagnostic_read", PARAMETERS)
                self.assertFalse(window.run_button.isEnabled())
                with patch.object(window.runtime, "run_sequence") as run:
                    window._run_sequence()
                    run.assert_not_called()
                first.close()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(isValid(first))
                future.set_result({"value": 1e-12, "calls": 1, "pid": 10})
                window._check_pending_dll_operations()
                self.assertEqual(window._pending_dll_operations, [])
                self.assertEqual(len(window._dll_function_dialogs), 1)
                window.current_run_state = RunState.RUNNING
                window._set_runtime_editable(False)
                self.assertFalse(second.run_button.isEnabled())
                with patch.object(window.runtime, "instrument_dll_function") as call:
                    second.runRequested.emit(PARAMETERS)
                    call.assert_not_called()
                window.current_run_state = RunState.IDLE
                window._set_runtime_editable(True)
                warning_future = Future()
                with patch.object(window.runtime, "instrument_dll_function", return_value=warning_future):
                    second.run_button.click()
                warning_future.set_exception(InstrumentWarning("Range warning", "DLL_RANGE", "channel_a"))
                window._check_pending_dll_operations()
                self.assertIn("DLL_RANGE", second.status_label.text())
                self.assertIn("channel_a", second.status_label.text())
            finally:
                window.current_run_state = RunState.IDLE
                window.close()

    def test_module_functions_follow_enable_and_disable_without_duplicates(self):
        descriptor = ModuleDescriptor("dll_test", "DLL Test", "1.0.0", FIXTURE)
        with (
            patch("labcontrol.ui.main_window.RuntimeService.start"),
            patch("labcontrol.ui.main_window.RuntimeService.shutdown"),
            patch.object(MainWindow, "_discover_module_descriptors", return_value=(descriptor,)),
        ):
            window = MainWindow(load_simulated_config())
            try:
                window._set_module_dll_functions("dll_test", False, FUNCTIONS)
                self.assertEqual(window._module_dll_menus, {})
                window._set_module_dll_functions("dll_test", True, FUNCTIONS)
                menu = window._module_dll_menus["dll_test"]
                window._set_module_dll_functions("dll_test", True, FUNCTIONS)
                self.assertIs(window._module_dll_menus["dll_test"], menu)
                menu.actions()[0].trigger()
                dialog = next(iter(window._dll_function_dialogs.values()))
                future = Future()
                with patch.object(window.runtime, "module_dll_function", return_value=future) as call:
                    dialog.run_button.click()
                    call.assert_called_once_with("dll_test", "diagnostic_read", PARAMETERS)
                future.set_result({"value": 1e-12, "calls": 1, "pid": 10})
                window._check_pending_dll_operations()
                self.assertEqual(dialog.status_label.text(), "Completed")
                window._set_module_dll_functions("dll_test", False, [])
                self.assertEqual(window._module_dll_menus, {})
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(isValid(dialog))
                self.assertEqual(window._dll_function_dialogs, {})
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
