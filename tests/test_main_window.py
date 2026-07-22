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


if __name__ == "__main__":
    unittest.main()
