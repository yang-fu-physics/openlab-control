"""固定浅色外观主题与系统主题变化隔离。

OpenLab Control 与 Instrument Scanner 的自定义遥测卡片、序列高亮、绘图画布与模块设置页
均按实验室高对比度浅色界面设计。Qt 6 在 Windows 深色/自定义主题下会自动给 ``Fusion``
注入系统调色板，若仅覆盖个别颜色角色，会出现隔行白底白字、深色窗口拼接浅色卡片等错乱。

本模块负责：
1. 锁定 ``Qt.ColorScheme.Light``（Qt 6.8+ 同步让 Windows 原生标题栏与弹窗保持浅色）；
2. 强制 ``Fusion`` 样式并应用从零构造的完整浅色 ``QPalette``（含 Active/Inactive/Disabled
   及 Accent 角色），不继承任何 Windows 深色、高对比度或强调色；
3. 安装幂等守卫，在运行期间 Windows 切换深浅色或主题时立即恢复固定浅色调色板。
"""

from __future__ import annotations

from typing import Any


LIGHT_THEME_COLORS: dict[str, str] = {
    "window": "#f3f5f7",
    "window_text": "#1f2328",
    "base": "#ffffff",
    "alternate_base": "#f7f9fa",
    "tooltip_base": "#ffffff",
    "tooltip_text": "#1f2328",
    "placeholder_text": "#6e7781",
    "text": "#1f2328",
    "button": "#f6f8fa",
    "button_text": "#1f2328",
    "bright_text": "#ffffff",
    "light": "#ffffff",
    "midlight": "#eef2f6",
    "mid": "#d0d7de",
    "dark": "#aeb7c2",
    "shadow": "#6e7781",
    "link": "#2468b4",
    "link_visited": "#6639ba",
    "highlight": "#5c6b79",
    "highlighted_text": "#ffffff",
    "accent": "#2f6fbb",
    "inactive_highlight": "#d8e1ea",
    "inactive_highlighted_text": "#1f2328",
    "disabled_text": "#8c959f",
    "disabled_button": "#eef1f4",
    "disabled_base": "#f6f8fa",
    "disabled_highlight": "#d0d7de",
    "disabled_highlighted_text": "#6e7781",
    "disabled_placeholder_text": "#adb5bd",
    "disabled_accent": "#9fb4cc",
}

_GUARDED_PALETTE_CHECKS: tuple[tuple[str, str], ...] = (
    ("Active", "Window"),
    ("Active", "WindowText"),
    ("Active", "Base"),
    ("Active", "AlternateBase"),
    ("Active", "Text"),
    ("Active", "Button"),
    ("Active", "ButtonText"),
    ("Active", "Highlight"),
    ("Active", "HighlightedText"),
    ("Active", "ToolTipBase"),
    ("Active", "ToolTipText"),
    ("Active", "Accent"),
    ("Disabled", "WindowText"),
    ("Disabled", "Text"),
    ("Disabled", "ButtonText"),
    ("Disabled", "Button"),
    ("Disabled", "Base"),
)


def build_light_palette():
    """构造完全独立于操作系统颜色设置的固定浅色 ``QPalette``。"""
    from PySide6.QtGui import QColor, QPalette

    palette = QPalette()
    c = LIGHT_THEME_COLORS
    groups = (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    )
    base_roles = {
        QPalette.ColorRole.Window: QColor(c["window"]),
        QPalette.ColorRole.WindowText: QColor(c["window_text"]),
        QPalette.ColorRole.Base: QColor(c["base"]),
        QPalette.ColorRole.AlternateBase: QColor(c["alternate_base"]),
        QPalette.ColorRole.ToolTipBase: QColor(c["tooltip_base"]),
        QPalette.ColorRole.ToolTipText: QColor(c["tooltip_text"]),
        QPalette.ColorRole.PlaceholderText: QColor(c["placeholder_text"]),
        QPalette.ColorRole.Text: QColor(c["text"]),
        QPalette.ColorRole.Button: QColor(c["button"]),
        QPalette.ColorRole.ButtonText: QColor(c["button_text"]),
        QPalette.ColorRole.BrightText: QColor(c["bright_text"]),
        QPalette.ColorRole.Light: QColor(c["light"]),
        QPalette.ColorRole.Midlight: QColor(c["midlight"]),
        QPalette.ColorRole.Mid: QColor(c["mid"]),
        QPalette.ColorRole.Dark: QColor(c["dark"]),
        QPalette.ColorRole.Shadow: QColor(c["shadow"]),
        QPalette.ColorRole.Link: QColor(c["link"]),
        QPalette.ColorRole.LinkVisited: QColor(c["link_visited"]),
        QPalette.ColorRole.Highlight: QColor(c["highlight"]),
        QPalette.ColorRole.HighlightedText: QColor(c["highlighted_text"]),
    }
    accent_role = getattr(QPalette.ColorRole, "Accent", None)
    if accent_role is not None:
        base_roles[accent_role] = QColor(c["accent"])

    for group in groups:
        for role, color in base_roles.items():
            palette.setColor(group, role, color)

    palette.setColor(
        QPalette.ColorGroup.Inactive,
        QPalette.ColorRole.Highlight,
        QColor(c["inactive_highlight"]),
    )
    palette.setColor(
        QPalette.ColorGroup.Inactive,
        QPalette.ColorRole.HighlightedText,
        QColor(c["inactive_highlighted_text"]),
    )

    disabled_roles = {
        QPalette.ColorRole.WindowText: QColor(c["disabled_text"]),
        QPalette.ColorRole.Text: QColor(c["disabled_text"]),
        QPalette.ColorRole.ButtonText: QColor(c["disabled_text"]),
        QPalette.ColorRole.Button: QColor(c["disabled_button"]),
        QPalette.ColorRole.Base: QColor(c["disabled_base"]),
        QPalette.ColorRole.Highlight: QColor(c["disabled_highlight"]),
        QPalette.ColorRole.HighlightedText: QColor(c["disabled_highlighted_text"]),
        QPalette.ColorRole.PlaceholderText: QColor(c["disabled_placeholder_text"]),
    }
    if accent_role is not None:
        disabled_roles[accent_role] = QColor(c["disabled_accent"])
    for role, color in disabled_roles.items():
        palette.setColor(QPalette.ColorGroup.Disabled, role, color)

    return palette


def _set_light_color_scheme(application: Any) -> None:
    from PySide6.QtCore import Qt

    color_scheme_enum = getattr(Qt, "ColorScheme", None)
    style_hints = (
        application.styleHints()
        if hasattr(application, "styleHints")
        else None
    )
    if (
        color_scheme_enum is not None
        and style_hints is not None
        and hasattr(style_hints, "setColorScheme")
    ):
        style_hints.setColorScheme(color_scheme_enum.Light)


def _color_scheme_drifted(application: Any) -> bool:
    from PySide6.QtCore import Qt

    color_scheme_enum = getattr(Qt, "ColorScheme", None)
    style_hints = (
        application.styleHints()
        if hasattr(application, "styleHints")
        else None
    )
    if (
        color_scheme_enum is None
        or style_hints is None
        or not hasattr(style_hints, "colorScheme")
    ):
        return False
    return style_hints.colorScheme() != color_scheme_enum.Light


def _palette_drifted(current: Any, expected: Any) -> bool:
    from PySide6.QtGui import QPalette

    for group_name, role_name in _GUARDED_PALETTE_CHECKS:
        group = getattr(QPalette.ColorGroup, group_name, None)
        role = getattr(QPalette.ColorRole, role_name, None)
        if group is None or role is None:
            continue
        if current.color(group, role).rgba() != expected.color(group, role).rgba():
            return True
    return False


def _create_theme_guard(application: Any, palette: Any):
    from PySide6.QtCore import QEvent, QObject

    class _FixedLightThemeGuard(QObject):
        """在 Windows 运行时切换主题/配色后立即恢复固定浅色调色板。"""

        def __init__(self, app: Any, fixed_palette: Any) -> None:
            super().__init__(app)
            self._application = app
            self._palette = fixed_palette
            self._applying = False
            self._palette_change_event = getattr(
                QEvent.Type,
                "ApplicationPaletteChange",
                None,
            )
            self._theme_change_event = getattr(
                QEvent.Type,
                "ThemeChange",
                None,
            )

        def enforce(self, *, set_style: bool = True) -> None:
            if self._applying:
                return
            self._applying = True
            try:
                _set_light_color_scheme(self._application)
                if set_style and hasattr(self._application, "setStyle"):
                    self._application.setStyle("Fusion")
                self._application.setPalette(self._palette)
            finally:
                self._applying = False

        def enforce_if_drifted(self, *_args: object) -> None:
            if self._applying:
                return
            scheme_drifted = _color_scheme_drifted(self._application)
            palette_drifted = _palette_drifted(
                self._application.palette(),
                self._palette,
            )
            if scheme_drifted or palette_drifted:
                self.enforce(set_style=False)

        def eventFilter(self, watched: object, event: Any) -> bool:  # noqa: N802
            event_type = event.type()
            if (
                event_type == self._palette_change_event
                and watched is self._application
            ) or event_type == self._theme_change_event:
                self.enforce_if_drifted()
            return False

    return _FixedLightThemeGuard(application, palette)


def apply_light_theme(application: Any):
    """锁定应用为固定浅色 ``Fusion`` 主题，并安装系统主题变化守卫。"""

    palette = build_light_palette()
    guard = getattr(application, "_openlab_light_theme_guard", None)
    if guard is None:
        guard = _create_theme_guard(application, palette)
        setattr(application, "_openlab_light_theme_guard", guard)
        if hasattr(application, "installEventFilter"):
            application.installEventFilter(guard)
        style_hints = (
            application.styleHints()
            if hasattr(application, "styleHints")
            else None
        )
        scheme_changed = (
            getattr(style_hints, "colorSchemeChanged", None)
            if style_hints is not None
            else None
        )
        if scheme_changed is not None and hasattr(scheme_changed, "connect"):
            scheme_changed.connect(guard.enforce_if_drifted)
    else:
        guard._palette = palette

    guard.enforce(set_style=True)
    if hasattr(application, "setProperty"):
        application.setProperty("openlabTheme", "light")
    return palette


__all__ = [
    "LIGHT_THEME_COLORS",
    "apply_light_theme",
    "build_light_palette",
]
