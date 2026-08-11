"""只影响本机显示效果的用户偏好与窗口布局存储。

这些值不属于实验配置：它们不会进入 SEQ、DAT、运行快照或仪表 worker。使用独立的 INI
文件而不是主 TOML，可以让打包目录保持只读，也避免实验室共享配置意外覆盖个人字号。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QRect, QSettings, QStandardPaths


MIN_UI_SCALE = 0.75
MAX_UI_SCALE = 2.0
MIN_FONT_SCALE = 0.70
MAX_FONT_SCALE = 1.50
WINDOW_MODES = frozenset({"remember", "maximized", "default"})


@dataclass(frozen=True, slots=True)
class UiPreferences:
    """启动前应用的整体缩放、文字倍率和窗口恢复方式。"""

    ui_scale: float | None = None
    font_scale: float = 1.0
    window_mode: str = "remember"


def validate_ui_preferences(preferences: UiPreferences) -> UiPreferences:
    """返回规范化偏好；界面保存前用严格校验阻止损坏值落盘。"""

    ui_scale = preferences.ui_scale
    if ui_scale is not None:
        ui_scale = float(ui_scale)
        if not MIN_UI_SCALE <= ui_scale <= MAX_UI_SCALE:
            raise ValueError(
                f"UI scale must be from {MIN_UI_SCALE:.2f} to {MAX_UI_SCALE:.2f}"
            )
    font_scale = float(preferences.font_scale)
    if not MIN_FONT_SCALE <= font_scale <= MAX_FONT_SCALE:
        raise ValueError(
            f"Font scale must be from {MIN_FONT_SCALE:.2f} to {MAX_FONT_SCALE:.2f}"
        )
    window_mode = str(preferences.window_mode).strip().casefold()
    if window_mode not in WINDOW_MODES:
        raise ValueError(
            "Window mode must be remember, maximized, or default"
        )
    return UiPreferences(ui_scale, font_scale, window_mode)


def default_ui_preferences_path() -> Path:
    """返回当前用户可写的外观偏好文件，而不是安装目录中的主配置。"""

    directory = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    if not directory:
        # Qt 正常的 Windows/Linux/macOS 平台都会提供 AppConfigLocation；该回退主要
        # 供极简测试平台使用，仍放在用户目录而不是项目目录。
        directory = str(Path.home() / ".openlab-control")
    return Path(directory) / "ui.ini"


class UiPreferenceStore:
    """使用 QSettings INI 后端保存外观偏好和 Qt 几何数据。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _settings(self) -> QSettings:
        settings = QSettings(
            str(self.path),
            QSettings.Format.IniFormat,
        )
        settings.setFallbacksEnabled(False)
        return settings

    def _sync(self, settings: QSettings) -> None:
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            raise OSError(
                f"Could not save UI preferences to {self.path}"
            )

    @staticmethod
    def _bounded_float(
        value: object,
        minimum: float,
        maximum: float,
        default: float,
    ) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if minimum <= number <= maximum else default

    def load(
        self,
        configured_ui_scale: float | None,
    ) -> UiPreferences:
        """读取偏好；缺失或损坏项各自回退，不阻止程序启动。"""

        settings = self._settings()
        if settings.contains("appearance/ui_scale"):
            raw_scale = settings.value("appearance/ui_scale")
            if (
                isinstance(raw_scale, str)
                and raw_scale.strip().casefold() == "auto"
            ):
                ui_scale = None
            else:
                try:
                    parsed_scale = float(raw_scale)
                except (TypeError, ValueError):
                    parsed_scale = float("nan")
                ui_scale = (
                    parsed_scale
                    if MIN_UI_SCALE
                    <= parsed_scale
                    <= MAX_UI_SCALE
                    else configured_ui_scale
                )
        else:
            ui_scale = configured_ui_scale

        font_scale = self._bounded_float(
            settings.value("appearance/font_scale", 1.0),
            MIN_FONT_SCALE,
            MAX_FONT_SCALE,
            1.0,
        )
        window_mode = str(
            settings.value(
                "appearance/window_mode",
                "remember",
            )
        ).strip().casefold()
        if window_mode not in WINDOW_MODES:
            window_mode = "remember"
        return UiPreferences(
            ui_scale,
            font_scale,
            window_mode,
        )

    def save(self, preferences: UiPreferences) -> None:
        """写入一组已验证外观值，并在 QSettings 同步失败时报告错误。"""

        preferences = validate_ui_preferences(preferences)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = self._settings()
        settings.setValue(
            "appearance/ui_scale",
            "auto"
            if preferences.ui_scale is None
            else preferences.ui_scale,
        )
        settings.setValue(
            "appearance/font_scale",
            preferences.font_scale,
        )
        settings.setValue(
            "appearance/window_mode",
            preferences.window_mode,
        )
        self._sync(settings)

    def geometry(self, key: str) -> QByteArray | None:
        value = self._settings().value(
            f"windows/{key}/geometry"
        )
        if isinstance(value, QByteArray) and not value.isEmpty():
            return value
        if isinstance(value, bytes) and value:
            return QByteArray(value)
        return None

    def set_geometry(self, key: str, value: QByteArray) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = self._settings()
        settings.setValue(
            f"windows/{key}/geometry",
            value,
        )
        self._sync(settings)

    def rect(self, key: str) -> QRect | None:
        raw = str(
            self._settings().value(
                f"windows/{key}/rect",
                "",
            )
        )
        try:
            x, y, width, height = (
                int(part) for part in raw.split(",")
            )
        except (TypeError, ValueError):
            return None
        if width < 1 or height < 1:
            return None
        return QRect(x, y, width, height)

    def set_rect(self, key: str, value: QRect) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = self._settings()
        settings.setValue(
            f"windows/{key}/rect",
            ",".join(
                str(number)
                for number in (
                    value.x(),
                    value.y(),
                    value.width(),
                    value.height(),
                )
            ),
        )
        self._sync(settings)

    def main_window_state(self) -> QByteArray | None:
        value = self._settings().value("windows/main/state")
        return value if isinstance(value, QByteArray) else None

    def set_main_window_state(self, value: QByteArray) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = self._settings()
        settings.setValue("windows/main/state", value)
        self._sync(settings)

    def main_window_maximized(self) -> bool:
        return bool(
            self._settings().value(
                "windows/main/maximized",
                False,
                type=bool,
            )
        )

    def set_main_window_maximized(self, value: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = self._settings()
        settings.setValue("windows/main/maximized", bool(value))
        self._sync(settings)

    def clear_window_layout(self) -> None:
        """删除窗口几何但保留字号和缩放设置。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = self._settings()
        settings.remove("windows")
        self._sync(settings)


__all__ = [
    "MAX_FONT_SCALE",
    "MAX_UI_SCALE",
    "MIN_FONT_SCALE",
    "MIN_UI_SCALE",
    "UiPreferences",
    "UiPreferenceStore",
    "default_ui_preferences_path",
    "validate_ui_preferences",
]
