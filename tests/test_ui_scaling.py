from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.config import ConfigurationError, load_config  # noqa: E402
from labcontrol.ui.scaling import automatic_ui_scale, scaled  # noqa: E402


class UiScalingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        configure_qt_appearance(self.application, 1.0)

    def test_automatic_profiles_cover_full_hd_2k_and_4k(self) -> None:
        self.assertEqual(automatic_ui_scale(1366, 768), 1.0)
        self.assertEqual(automatic_ui_scale(1920, 1080), 1.0)
        self.assertEqual(automatic_ui_scale(2560, 1440), 1.15)
        self.assertEqual(automatic_ui_scale(3840, 2160), 1.4)

    def test_manual_scale_changes_font_and_fixed_metrics(self) -> None:
        result = configure_qt_appearance(self.application, 1.4)
        self.assertEqual(result, 1.4)
        self.assertAlmostEqual(self.application.font().pointSizeF(), 14.0, places=1)
        self.assertEqual(scaled(100), 140)
        self.assertEqual(self.application.property("openlabUiScaleMode"), "manual")

    def test_configuration_accepts_auto_and_manual_scale(self) -> None:
        default_text = (ROOT / "configs" / "default.toml").read_text(encoding="utf-8")
        self.assertIsNone(load_config(ROOT / "configs" / "default.toml").ui_scale)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.toml"
            path.write_text(
                default_text.replace('ui_scale = "auto"', "ui_scale = 1.4"),
                encoding="utf-8",
            )
            self.assertEqual(load_config(path).ui_scale, 1.4)
            path.write_text(
                default_text.replace('ui_scale = "auto"', "ui_scale = 2.5"),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
