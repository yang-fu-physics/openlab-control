from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QByteArray, QRect, QSettings  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialogButtonBox,
)

from labcontrol.ui.appearance import AppearanceDialog  # noqa: E402
from labcontrol.ui.preferences import (  # noqa: E402
    UiPreferences,
    UiPreferenceStore,
    validate_ui_preferences,
)


class UiPreferenceStoreTests(unittest.TestCase):
    def test_missing_values_use_site_scale_and_safe_defaults(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = UiPreferenceStore(
                Path(directory) / "ui.ini"
            )
            self.assertEqual(
                store.load(1.25),
                UiPreferences(1.25, 1.0, "remember"),
            )
            self.assertEqual(
                store.load(None),
                UiPreferences(None, 1.0, "remember"),
            )

    def test_round_trip_supports_very_small_text_and_auto(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui.ini"
            store = UiPreferenceStore(path)
            expected = UiPreferences(
                None,
                0.70,
                "maximized",
            )
            store.save(expected)

            self.assertTrue(path.is_file())
            self.assertEqual(store.load(1.4), expected)

    def test_corrupt_values_fall_back_independently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui.ini"
            settings = QSettings(
                str(path),
                QSettings.Format.IniFormat,
            )
            settings.setValue("appearance/ui_scale", 9.0)
            settings.setValue("appearance/font_scale", 0.1)
            settings.setValue("appearance/window_mode", "lost")
            settings.sync()

            loaded = UiPreferenceStore(path).load(None)
            self.assertEqual(
                loaded,
                UiPreferences(None, 1.0, "remember"),
            )

    def test_window_geometry_round_trip_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = UiPreferenceStore(
                Path(directory) / "ui.ini"
            )
            geometry = QByteArray(b"window-geometry")
            state = QByteArray(b"dock-state")
            rect = QRect(11, 22, 640, 480)
            store.set_geometry("module/sample", geometry)
            store.set_rect("sequence", rect)
            store.set_main_window_state(state)
            store.set_main_window_maximized(True)

            self.assertEqual(
                store.geometry("module/sample"),
                geometry,
            )
            self.assertEqual(store.rect("sequence"), rect)
            self.assertEqual(store.main_window_state(), state)
            self.assertTrue(store.main_window_maximized())

            store.clear_window_layout()
            self.assertIsNone(
                store.geometry("module/sample")
            )
            self.assertIsNone(store.rect("sequence"))
            self.assertIsNone(store.main_window_state())
            self.assertFalse(store.main_window_maximized())

    def test_strict_save_validation_rejects_unsafe_ranges(
        self,
    ) -> None:
        for preferences in (
            UiPreferences(0.5, 1.0, "remember"),
            UiPreferences(1.0, 0.69, "remember"),
            UiPreferences(1.0, 1.0, "somewhere"),
        ):
            with self.subTest(preferences=preferences):
                with self.assertRaises(ValueError):
                    validate_ui_preferences(preferences)


class AppearanceDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = (
            QApplication.instance() or QApplication([])
        )

    def test_dialog_exposes_seventy_percent_text(self) -> None:
        dialog = AppearanceDialog(
            UiPreferences(0.90, 0.70, "default"),
            None,
        )
        try:
            self.assertEqual(
                dialog.preferences(),
                UiPreferences(0.90, 0.70, "default"),
            )
            self.assertIn(
                "70%",
                dialog.font_combo.currentText(),
            )
        finally:
            dialog.close()

    def test_custom_values_and_reset_are_explicit(self) -> None:
        dialog = AppearanceDialog(
            UiPreferences(None, 1.0, "remember"),
            1.25,
        )
        try:
            dialog.overall_combo.setCurrentIndex(
                dialog.overall_combo.findData("custom")
            )
            dialog.overall_custom.setValue(83.0)
            dialog.font_combo.setCurrentIndex(
                dialog.font_combo.findData("custom")
            )
            dialog.font_custom.setValue(73.0)
            self.assertEqual(
                dialog.preferences(),
                UiPreferences(0.83, 0.73, "remember"),
            )

            restore = dialog.buttons.button(
                QDialogButtonBox.StandardButton.RestoreDefaults
            )
            restore.click()
            self.assertEqual(
                dialog.preferences(),
                UiPreferences(1.25, 1.0, "remember"),
            )
            self.assertTrue(
                dialog.reset_window_layout_requested
            )
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
