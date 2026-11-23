from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.ui.theme import (  # noqa: E402
    LIGHT_THEME_COLORS,
    _color_scheme_drifted,
    apply_light_theme,
    build_light_palette,
)


def _make_dark_palette() -> QPalette:
    palette = QPalette()
    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        palette.setColor(group, QPalette.ColorRole.Window, QColor("#1e1e1e"))
        palette.setColor(group, QPalette.ColorRole.WindowText, QColor("#ffffff"))
        palette.setColor(group, QPalette.ColorRole.Base, QColor("#252526"))
        palette.setColor(group, QPalette.ColorRole.AlternateBase, QColor("#2d2d30"))
        palette.setColor(group, QPalette.ColorRole.Text, QColor("#ffffff"))
        palette.setColor(group, QPalette.ColorRole.Button, QColor("#333333"))
        palette.setColor(group, QPalette.ColorRole.ButtonText, QColor("#ffffff"))
        palette.setColor(group, QPalette.ColorRole.Highlight, QColor("#ff00aa"))
        palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor("#000000"))
    return palette


class FixedLightThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        configure_qt_appearance(self.application, 1.0)

    def test_build_light_palette_defines_high_contrast_light_roles(self) -> None:
        palette = build_light_palette()
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window).name().lower(),
            LIGHT_THEME_COLORS["window"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Base).name().lower(),
            LIGHT_THEME_COLORS["base"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.AlternateBase).name().lower(),
            LIGHT_THEME_COLORS["alternate_base"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText).name().lower(),
            LIGHT_THEME_COLORS["window_text"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Text).name().lower(),
            LIGHT_THEME_COLORS["text"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight).name().lower(),
            LIGHT_THEME_COLORS["highlight"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText).name().lower(),
            LIGHT_THEME_COLORS["disabled_text"],
        )
        accent_role = getattr(QPalette.ColorRole, "Accent", None)
        if accent_role is not None:
            self.assertEqual(
                palette.color(QPalette.ColorGroup.Active, accent_role).name().lower(),
                LIGHT_THEME_COLORS["accent"],
            )

    def test_configure_qt_appearance_locks_fusion_light_scheme_and_palette(self) -> None:
        configure_qt_appearance(self.application, 1.0)
        self.assertEqual(self.application.property("openlabTheme"), "light")
        style = self.application.style()
        if style is not None and hasattr(style, "objectName"):
            self.assertEqual(style.objectName().lower(), "fusion")

        color_scheme_enum = getattr(Qt, "ColorScheme", None)
        style_hints = (
            self.application.styleHints()
            if hasattr(self.application, "styleHints")
            else None
        )
        if (
            color_scheme_enum is not None
            and style_hints is not None
            and hasattr(style_hints, "colorScheme")
        ):
            platform_name = (
                self.application.platformName().lower()
                if hasattr(self.application, "platformName")
                else ""
            )
            if platform_name == "offscreen":
                self.assertIn(
                    style_hints.colorScheme(),
                    (color_scheme_enum.Light, color_scheme_enum.Unknown),
                )
            else:
                self.assertEqual(
                    style_hints.colorScheme(),
                    color_scheme_enum.Light,
                )

        palette = self.application.palette()
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window).name().lower(),
            LIGHT_THEME_COLORS["window"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Base).name().lower(),
            LIGHT_THEME_COLORS["base"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Text).name().lower(),
            LIGHT_THEME_COLORS["text"],
        )

    def test_apply_light_theme_is_idempotent_and_recovers_from_runtime_dark_switch(
        self,
    ) -> None:
        apply_light_theme(self.application)
        first_guard = getattr(self.application, "_openlab_light_theme_guard")
        apply_light_theme(self.application)
        self.assertIs(
            getattr(self.application, "_openlab_light_theme_guard"),
            first_guard,
        )

        # Simulate Windows switching to dark mode / broadcasting a dark palette at runtime.
        self.application.setPalette(_make_dark_palette())
        QCoreApplication.sendEvent(
            self.application,
            QEvent(QEvent.Type.ApplicationPaletteChange),
        )
        palette = self.application.palette()
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window).name().lower(),
            LIGHT_THEME_COLORS["window"],
        )
        self.assertEqual(
            palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Text).name().lower(),
            LIGHT_THEME_COLORS["text"],
        )

        color_scheme_enum = getattr(Qt, "ColorScheme", None)
        style_hints = (
            self.application.styleHints()
            if hasattr(self.application, "styleHints")
            else None
        )
        if (
            color_scheme_enum is not None
            and style_hints is not None
            and hasattr(style_hints, "setColorScheme")
        ):
            platform_name = (
                self.application.platformName().lower()
                if hasattr(self.application, "platformName")
                else ""
            )
            style_hints.setColorScheme(color_scheme_enum.Dark)
            QCoreApplication.sendEvent(
                self.application,
                QEvent(QEvent.Type.ThemeChange),
            )
            if platform_name == "offscreen":
                self.assertIn(
                    style_hints.colorScheme(),
                    (color_scheme_enum.Light, color_scheme_enum.Unknown),
                )
            else:
                self.assertEqual(
                    style_hints.colorScheme(),
                    color_scheme_enum.Light,
                )
            self.assertEqual(
                self.application.palette()
                .color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window)
                .name()
                .lower(),
                LIGHT_THEME_COLORS["window"],
            )

    def test_unknown_offscreen_scheme_does_not_drift_and_dark_scheme_recovers(
        self,
    ) -> None:
        color_scheme_enum = getattr(Qt, "ColorScheme", None)
        if color_scheme_enum is None:
            return

        class _FakeHints:
            def __init__(self, scheme) -> None:
                self._scheme = scheme

            def colorScheme(self):
                return self._scheme

            def setColorScheme(self, scheme) -> None:
                self._scheme = scheme

        class _FakeApp:
            def __init__(self, scheme) -> None:
                self._hints = _FakeHints(scheme)

            def styleHints(self):
                return self._hints

        self.assertFalse(_color_scheme_drifted(_FakeApp(color_scheme_enum.Unknown)))
        self.assertFalse(_color_scheme_drifted(_FakeApp(color_scheme_enum.Light)))
        self.assertTrue(_color_scheme_drifted(_FakeApp(color_scheme_enum.Dark)))


if __name__ == "__main__":
    unittest.main()
