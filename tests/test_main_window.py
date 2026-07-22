from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.config import load_config  # noqa: E402
from labcontrol.sequence.model import CommandType  # noqa: E402
from labcontrol.sequence.parser import parse_sequence  # noqa: E402
from labcontrol.ui.main_window import MainWindow  # noqa: E402


class MainWindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])
        cls.config = load_config(ROOT / "configs" / "default.toml")
        configure_qt_appearance(cls.application, cls.config.ui_scale)

    def test_floating_windows_stay_inside_minimum_viewport(self) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1180, 720)
            window.show()
            window._show_data_browser()
            self.application.processEvents()
            window._fit_mdi_windows()
            self.application.processEvents()

            viewport = window.mdi.viewport().rect()
            for subwindow in (window.sequence_window, window.data_window):
                geometry = subwindow.geometry()
                self.assertGreaterEqual(geometry.left(), viewport.left())
                self.assertGreaterEqual(geometry.top(), viewport.top())
                self.assertLessEqual(geometry.right(), viewport.right())
                self.assertLessEqual(geometry.bottom(), viewport.bottom())
        finally:
            window.close()

    def test_new_sequence_reopens_a_closed_sequence_window(self) -> None:
        window = MainWindow(self.config)
        try:
            window.show()
            self.application.processEvents()
            window.sequence_window.close()
            self.application.processEvents()
            self.assertFalse(window.sequence_window.isVisible())

            window.new_action.trigger()
            self.application.processEvents()

            self.assertTrue(window.sequence_window.isVisible())
            self.assertTrue(window.editor.isVisible())
            self.assertIs(window.mdi.activeSubWindow(), window.sequence_window)
            self.assertEqual(window.document.name, "Untitled.seq")
            self.assertEqual(window.editor.list.count(), 1)
            self.assertEqual(window.editor.list.item(0).text(), "End Sequence")
        finally:
            window.close()

    def test_custom_file_paths_do_not_force_the_left_dock_wider(self) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1180, 720)
            window.show()
            self.application.processEvents()
            baseline_minimum = window.left_dock.minimumSizeHint().width()
            custom_path = ROOT / "a very long custom output directory" / (
                "a_very_long_measurement_file_name_that_must_not_expand_the_sidebar.dat"
            )
            with patch(
                "labcontrol.ui.main_window.QFileDialog.getSaveFileName",
                return_value=(str(custom_path), "Data (*.dat)"),
            ):
                window._change_datafile()
            window.sequence_label.setFullText("a_very_long_sequence_name_" * 8 + ".seq")
            self.application.processEvents()

            command = next(
                item for item in window.document.commands if item.type.value == "set_datafile"
            )
            expected_path = str(custom_path.resolve())
            self.assertEqual(command.type.value, "set_datafile")
            self.assertEqual(command.params["path_scope"], "Custom folder")
            self.assertEqual(command.params["path"], expected_path)
            self.assertEqual(window.data_file_label.fullText(), expected_path)
            self.assertEqual(window.data_file_label.toolTip(), expected_path)
            self.assertEqual(
                window.data_file_label.sizePolicy().horizontalPolicy(),
                QSizePolicy.Policy.Ignored,
            )
            self.assertIn("…", window.data_file_label.text())
            self.assertLessEqual(window.left_dock.minimumSizeHint().width(), baseline_minimum)
        finally:
            window.close()

    def test_sequence_edit_popup_receives_device_limits_from_main_config(self) -> None:
        window = MainWindow(self.config)
        try:
            command = next(
                item for item in window.document.commands
                if item.type is CommandType.SET_TEMPERATURE
            )
            observed: dict[str, float | str] = {}

            def inspect_and_reject(dialog) -> object:
                observed["minimum"] = dialog.inputs["target"].minimum()
                observed["maximum"] = dialog.inputs["target"].maximum()
                observed["max_rate"] = dialog.inputs["rate"].maximum()
                observed["summary"] = dialog.limit_label.text()
                return dialog.DialogCode.Rejected

            with patch("labcontrol.ui.main_window.CommandDialog.exec", new=inspect_and_reject):
                window._edit_command(command)

            self.assertEqual(observed["minimum"], 1.8)
            self.assertEqual(observed["maximum"], 400.0)
            self.assertEqual(observed["max_rate"], 30.0)
            self.assertIn("Configured limits (temperature)", observed["summary"])
        finally:
            window.close()

    def test_modules_manager_has_only_requested_columns_and_no_measurement_tile(self) -> None:
        window = MainWindow(self.config)
        try:
            manager = window.module_manager
            self.assertEqual(manager.table.columnCount(), 3)
            self.assertEqual(
                [manager.table.horizontalHeaderItem(index).text() for index in range(3)],
                ["Enabled", "Name", "Version"],
            )
            self.assertEqual(set(window.status_tiles), {"temperature", "field", "second_stage"})
            self.assertEqual([item.id for item in window.module_descriptors], ["simulated_transport"])
        finally:
            window.close()

    def test_dependency_install_requires_every_module_to_be_disabled(self) -> None:
        window = MainWindow(self.config)
        try:
            window.enabled_modules.add("simulated_transport")
            with patch("labcontrol.ui.main_window.QMessageBox.warning") as warning:
                window._install_module_dependencies("simulated_transport")
            warning.assert_called_once()
            self.assertIn("Disable every measurement module", warning.call_args.args[2])
        finally:
            window.close()

    def test_legacy_measure_parameters_block_run(self) -> None:
        window = MainWindow(self.config)
        try:
            document = parse_sequence(
                "T Measure devices=transport\nT End Sequence\n", "legacy.seq"
            ).document
            window._set_document(document)
            with (
                patch.object(window.runtime, "run_sequence") as run_sequence,
                patch("labcontrol.ui.main_window.QMessageBox.critical") as critical,
            ):
                window._run_sequence()
            run_sequence.assert_not_called()
            critical.assert_called_once()
            self.assertIn("Measure has no parameters", critical.call_args.args[2])
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
