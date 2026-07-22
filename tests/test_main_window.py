from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
