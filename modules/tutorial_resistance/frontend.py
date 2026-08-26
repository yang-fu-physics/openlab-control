"""随框架提供的 Tutorial Resistance 设置与状态界面。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from labcontrol.measurement.frontend_api import ModuleUIAPI


class Frontend(QWidget):
    """前端只编辑数据和请求状态，不连接仪表、不创建线程。"""

    def __init__(self, api: ModuleUIAPI) -> None:
        super().__init__()
        self.api = api
        layout = QVBoxLayout(self)

        settings_group = QGroupBox("Simulation Settings")
        form = QFormLayout(settings_group)
        self.base_resistance = self._spin(0.001, 1e9, 6, " Ohm")
        self.channel_step = self._spin(0.0, 1e9, 6, " Ohm")
        self.delay = self._spin(0.0, 60.0, 3, " s/channel")
        self.noise = self._spin(0.0, 1e6, 9, " Ohm")
        self.over_range = self._spin(0.001, 1e12, 6, " Ohm")
        form.addRow("Base resistance", self.base_resistance)
        form.addRow("Channel step", self.channel_step)
        form.addRow("Channel delay", self.delay)
        form.addRow("Raw sample spread", self.noise)
        form.addRow("Over-range threshold", self.over_range)
        layout.addWidget(settings_group)

        note = QLabel(
            "Enable only loads these values. Review them and click the core-provided "
            "Apply Settings button before running a sequence."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

        self.status_widget = QWidget()
        status_layout = QVBoxLayout(self.status_widget)
        status_group = QGroupBox("Module Status")
        status_form = QFormLayout(status_group)
        self.status_labels: dict[str, QLabel] = {}
        for name in (
            "Connection",
            "Applied Settings",
            "Sequence",
            "Output",
            "Excitation Current (A)",
            "Last Channel",
            "Last Resistance (Ohm)",
            "Last Run Result",
        ):
            label = QLabel("—")
            self.status_labels[name] = label
            status_form.addRow(name, label)
        status_layout.addWidget(status_group)
        refresh = QPushButton("Refresh Status")
        refresh.clicked.connect(api.refresh)
        status_layout.addWidget(refresh)
        status_layout.addStretch(1)
        self.load({})

    @staticmethod
    def _spin(
        minimum: float,
        maximum: float,
        decimals: int,
        suffix: str,
    ) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(decimals)
        widget.setSuffix(suffix)
        return widget

    def dump(self) -> dict[str, Any]:
        """核心在保存设置或 Apply 时读取当前界面值。"""

        return {
            "base_resistance_ohm": self.base_resistance.value(),
            "channel_step_ohm": self.channel_step.value(),
            "delay_seconds": self.delay.value(),
            "noise_ohm": self.noise.value(),
            "over_range_ohm": self.over_range.value(),
        }

    def load(self, settings: Mapping[str, Any]) -> None:
        """加载仅修改控件；不得在这里 Apply、连接或发送仪表命令。"""

        values = {
            "base_resistance_ohm": 100.0,
            "channel_step_ohm": 10.0,
            "delay_seconds": 0.02,
            "noise_ohm": 0.001,
            "over_range_ohm": 1e6,
            **dict(settings),
        }
        widgets = (
            (self.base_resistance, "base_resistance_ohm"),
            (self.channel_step, "channel_step_ohm"),
            (self.delay, "delay_seconds"),
            (self.noise, "noise_ohm"),
            (self.over_range, "over_range_ohm"),
        )
        blockers = [QSignalBlocker(widget) for widget, _ in widgets]
        for widget, key in widgets:
            widget.setValue(float(values[key]))
        del blockers

    def show_status(self, status: Mapping[str, Any]) -> None:
        """只更新已存在标签，不根据后端数据动态创建任意控件。"""

        for key, value in status.items():
            label = self.status_labels.get(str(key))
            if label is not None:
                label.setText(f"{value:.9g}" if isinstance(value, float) else str(value))
