"""外观设置对话框；只编辑本机显示偏好，不接触运行时或仪表。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .preferences import UiPreferences, validate_ui_preferences
from .scaling import current_ui_scale, scaled, screen_ui_scale


_OVERALL_PRESETS = (
    ("Automatic (recommended)", "auto"),
    ("Compact — 75%", "0.75"),
    ("Small — 90%", "0.90"),
    ("Standard — 100%", "1.00"),
    ("Comfortable — 110%", "1.10"),
    ("Large — 125%", "1.25"),
    ("Extra large — 150%", "1.50"),
    ("Very large — 175%", "1.75"),
    ("Maximum — 200%", "2.00"),
    ("Custom", "custom"),
)

_FONT_PRESETS = (
    ("Very small — 70%", "0.70"),
    ("Small — 80%", "0.80"),
    ("Slightly small — 90%", "0.90"),
    ("Standard — 100%", "1.00"),
    ("Large — 115%", "1.15"),
    ("Extra large — 130%", "1.30"),
    ("Custom", "custom"),
)


class AppearanceDialog(QDialog):
    """选择下次启动使用的整体、文字和窗口尺寸策略。"""

    def __init__(
        self,
        preferences: UiPreferences,
        configured_ui_scale: float | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Appearance")
        self._configured_ui_scale = configured_ui_scale
        self._reset_window_layout = False

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Overall size changes controls, spacing, icons and text. "
            "Text size is an additional adjustment. Changes take effect "
            "after restarting OpenLab Control."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        form = QFormLayout()
        self.overall_combo = QComboBox()
        for label, value in _OVERALL_PRESETS:
            self.overall_combo.addItem(label, value)
        self.overall_custom = QDoubleSpinBox()
        self.overall_custom.setRange(75.0, 200.0)
        self.overall_custom.setDecimals(0)
        self.overall_custom.setSingleStep(5.0)
        self.overall_custom.setSuffix(" %")
        overall_row = QHBoxLayout()
        overall_row.addWidget(self.overall_combo, 1)
        overall_row.addWidget(self.overall_custom)
        form.addRow("Overall size", overall_row)

        self.font_combo = QComboBox()
        for label, value in _FONT_PRESETS:
            self.font_combo.addItem(label, value)
        self.font_custom = QDoubleSpinBox()
        self.font_custom.setRange(70.0, 150.0)
        self.font_custom.setDecimals(0)
        self.font_custom.setSingleStep(5.0)
        self.font_custom.setSuffix(" %")
        font_row = QHBoxLayout()
        font_row.addWidget(self.font_combo, 1)
        font_row.addWidget(self.font_custom)
        form.addRow("Text size", font_row)

        self.window_mode_combo = QComboBox()
        self.window_mode_combo.addItem(
            "Remember last sizes and positions",
            "remember",
        )
        self.window_mode_combo.addItem(
            "Always start maximized",
            "maximized",
        )
        self.window_mode_combo.addItem(
            "Always use default layout",
            "default",
        )
        form.addRow("At startup", self.window_mode_combo)
        layout.addLayout(form)

        preview = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview)
        self.preview_title = QLabel("Measurement Module · Enabled")
        self.preview_title.setStyleSheet("font-weight: bold;")
        self.preview_value = QLabel("R1  1.234 kΩ   ·   Warning")
        self.preview_button = QPushButton("Restore Window")
        preview_layout.addWidget(self.preview_title)
        preview_layout.addWidget(self.preview_value)
        preview_layout.addWidget(self.preview_button)
        layout.addWidget(preview)

        reset_row = QHBoxLayout()
        self.reset_windows_button = QPushButton(
            "Reset Window Positions"
        )
        self.reset_status = QLabel("")
        self.reset_status.setObjectName("mutedLabel")
        reset_row.addWidget(self.reset_windows_button)
        reset_row.addWidget(self.reset_status, 1)
        layout.addLayout(reset_row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        layout.addWidget(self.buttons)

        self._set_preferences(preferences)
        self.overall_combo.currentIndexChanged.connect(
            self._selection_changed
        )
        self.font_combo.currentIndexChanged.connect(
            self._selection_changed
        )
        self.overall_custom.valueChanged.connect(
            self._update_preview
        )
        self.font_custom.valueChanged.connect(
            self._update_preview
        )
        self.reset_windows_button.clicked.connect(
            self._request_window_reset
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        restore = self.buttons.button(
            QDialogButtonBox.StandardButton.RestoreDefaults
        )
        restore.clicked.connect(self._restore_defaults)
        self._selection_changed()
        self.setMinimumWidth(scaled(520))

    @staticmethod
    def _find_value(combo: QComboBox, value: str) -> int:
        for index in range(combo.count()):
            if str(combo.itemData(index)) == value:
                return index
        return -1

    def _set_scale_choice(
        self,
        combo: QComboBox,
        custom: QDoubleSpinBox,
        value: float | None,
        *,
        allow_auto: bool,
    ) -> None:
        if value is None and allow_auto:
            combo.setCurrentIndex(
                self._find_value(combo, "auto")
            )
            return
        number = 1.0 if value is None else float(value)
        text = f"{number:.2f}"
        index = self._find_value(combo, text)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentIndex(
                self._find_value(combo, "custom")
            )
        custom.setValue(number * 100.0)

    def _set_preferences(
        self,
        preferences: UiPreferences,
    ) -> None:
        self._set_scale_choice(
            self.overall_combo,
            self.overall_custom,
            preferences.ui_scale,
            allow_auto=True,
        )
        self._set_scale_choice(
            self.font_combo,
            self.font_custom,
            preferences.font_scale,
            allow_auto=False,
        )
        index = self.window_mode_combo.findData(
            preferences.window_mode
        )
        self.window_mode_combo.setCurrentIndex(max(0, index))

    @staticmethod
    def _choice_value(
        combo: QComboBox,
        custom: QDoubleSpinBox,
        *,
        allow_auto: bool,
    ) -> float | None:
        value = str(combo.currentData())
        if allow_auto and value == "auto":
            return None
        if value == "custom":
            return custom.value() / 100.0
        return float(value)

    def preferences(self) -> UiPreferences:
        return validate_ui_preferences(
            UiPreferences(
                ui_scale=self._choice_value(
                    self.overall_combo,
                    self.overall_custom,
                    allow_auto=True,
                ),
                font_scale=float(
                    self._choice_value(
                        self.font_combo,
                        self.font_custom,
                        allow_auto=False,
                    )
                ),
                window_mode=str(
                    self.window_mode_combo.currentData()
                ),
            )
        )

    @property
    def reset_window_layout_requested(self) -> bool:
        return self._reset_window_layout

    def _selection_changed(self, *_args) -> None:
        self.overall_custom.setVisible(
            self.overall_combo.currentData() == "custom"
        )
        self.font_custom.setVisible(
            self.font_combo.currentData() == "custom"
        )
        self._update_preview()

    def _update_preview(self, *_args) -> None:
        ui_scale = self._choice_value(
            self.overall_combo,
            self.overall_custom,
            allow_auto=True,
        )
        if ui_scale is None:
            application = QApplication.instance()
            ui_scale = screen_ui_scale(
                application.primaryScreen()
                if application is not None
                else None
            )
        font_scale = float(
            self._choice_value(
                self.font_combo,
                self.font_custom,
                allow_auto=False,
            )
        )
        font = self.font()
        font.setPointSizeF(10.0 * ui_scale * font_scale)
        for widget in (
            self.preview_title,
            self.preview_value,
            self.preview_button,
        ):
            widget.setFont(font)
        # 预览最多按 1.5 倍放大控件，避免 200% 选项反过来撑大设置窗口；旁边的
        # 百分比文本仍显示真实选择。
        preview_scale = min(1.5, ui_scale)
        self.preview_button.setMinimumHeight(
            max(1, round(30 * preview_scale))
        )

    def _request_window_reset(self) -> None:
        self._reset_window_layout = True
        self.reset_status.setText(
            "Saved window positions will be cleared"
        )

    def _restore_defaults(self) -> None:
        self._set_preferences(
            UiPreferences(
                self._configured_ui_scale,
                1.0,
                "remember",
            )
        )
        self._reset_window_layout = True
        self.reset_status.setText(
            "Appearance and window positions will use defaults"
        )
        self._selection_changed()


__all__ = ["AppearanceDialog"]
