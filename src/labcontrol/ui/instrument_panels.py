"""由 System Instrument 清单选择的底部状态面板模板。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    InstrumentConfig,
    InstrumentPanelConfig,
    InstrumentReadingConfig,
)
from ..formatting import control_decimals, fixed_number
from ..models import (
    InstrumentConnectionState,
    InstrumentKind,
    InstrumentSnapshot,
    StabilityState,
)
from ..sequence.model import SystemInstrumentCommandSpec
from .scaling import scaled, scaled_text


class ControllerPanel(QFrame):
    """显示温度或磁场的当前值、目标、速率和稳定状态。"""

    controlRequested = Signal(str, str)

    def __init__(
        self,
        config: InstrumentConfig,
        panel: InstrumentPanelConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.panel = panel
        self.reading = config.reading(panel.reading)
        self.setObjectName("instrumentPanel")
        self.setMinimumWidth(scaled(205))
        self.setMaximumHeight(scaled(135))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = _panel_layout(self)
        self.title_label, self.state_label = _panel_header(
            layout,
            panel.display_name,
        )
        body = _panel_body(layout)
        self.value_label = _value_label(body)
        self.detail_label = _detail_label(
            body,
            "Double-click to control",
        )
        self._set_state_style("disconnected")

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        if not snapshot.connected:
            self.value_label.setText("—")
            self.state_label.setText(_connection_state_text(snapshot))
            self.detail_label.setText(
                snapshot.message
                or (
                    "Instrument communication unavailable"
                )
            )
            self._set_state_style(snapshot.connection_state.value)
            return

        kind = {
            "sample_temp": InstrumentKind.TEMPERATURE,
            "field": InstrumentKind.FIELD,
        }.get(self.panel.role, snapshot.kind)
        precision = (
            self.reading.decimals
            if self.reading.decimals is not None
            else control_decimals(kind, self.reading.unit)
        )
        state = snapshot.controls[self.panel.id]
        current = (
            "—"
            if state.current is None
            else f"{fixed_number(state.current, precision)} {self.reading.unit}"
        )
        target = (
            "—"
            if state.target is None
            else f"{fixed_number(state.target, precision)} {self.reading.unit}"
        )
        rate = (
            "—"
            if state.rate_per_minute is None
            else f"{fixed_number(state.rate_per_minute, precision)} "
            f"{self.reading.unit}/min"
        )
        self.value_label.setText(current)
        detail = f"Target {target}"
        if state.rate_per_minute is not None:
            detail += f"  ·  {rate}"
        self.detail_label.setText(detail)
        self.state_label.setText(
            {
                StabilityState.STABLE: "Stable",
                StabilityState.SETTLING: "Settling",
                StabilityState.MOVING: "Moving",
                StabilityState.TIMED_OUT: "Timed Out",
                StabilityState.STALE: "Stale",
            }.get(state.stability, state.activity.value)
        )
        self._set_state_style(state.stability.value)

    def mouseDoubleClickEvent(
        self,
        event: QMouseEvent,
    ) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
        ):
            self.controlRequested.emit(self.config.id, self.panel.id)
        super().mouseDoubleClickEvent(event)

    def _set_state_style(self, state: str) -> None:
        self.setStyleSheet(
            _instrument_panel_style(state, show_badge=True)
        )


class ReadoutPanel(QFrame):
    """直接显示一个只读值，不添加标题栏或状态说明。"""

    def __init__(
        self,
        reading: InstrumentReadingConfig,
        main_reading: str | None = None,
        title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.reading = reading
        self.main_reading = main_reading or reading.key
        self.setObjectName("instrumentPanel")
        self.setMinimumWidth(scaled(205))
        self.setMaximumHeight(scaled(135))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.setCursor(Qt.CursorShape.ArrowCursor)

        layout = _panel_layout(self)
        self.title_label = _panel_title(
            layout,
            title or reading.display_name,
        )
        body = _panel_body(layout)
        body.addStretch(1)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("panelValue")
        self.value_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
        )
        body.addWidget(self.value_label)
        body.addStretch(1)
        self._set_state_style("disconnected")

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        if not snapshot.connected:
            self.value_label.setText("—")
            self.setToolTip(snapshot.message)
            self._set_state_style(snapshot.connection_state.value)
            return
        self.setToolTip("")
        decimals = (
            self.reading.decimals
            if self.reading.decimals is not None
            else 3
        )
        value = (
            snapshot.current
            if self.reading.key == self.main_reading
            else (
                None
                if self.reading.key not in snapshot.metrics
                else snapshot.metrics[self.reading.key].value
            )
        )
        self.value_label.setText(
            _formatted_reading(
                value,
                self.reading.unit,
                decimals,
            )
        )
        self._set_state_style(
            "stable" if value is not None else "stale"
        )

    def _set_state_style(self, state: str) -> None:
        self.setStyleSheet(_instrument_panel_style(state))


class ReadoutGridPanel(QFrame):
    """在一个 2×2 面板内直接显示一至四个读数。"""

    def __init__(
        self,
        readings: tuple[InstrumentReadingConfig, ...],
        main_reading: str,
        title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not 1 <= len(readings) <= 4:
            raise ValueError(
                "A readout grid panel requires one to four readings"
            )
        self.readings = readings
        self.main_reading = main_reading
        self.name_labels: dict[str, QLabel] = {}
        self.value_labels: dict[str, QLabel] = {}
        self.setObjectName("instrumentPanel")
        self.setMinimumWidth(scaled(300))
        self.setMaximumHeight(scaled(135))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.setCursor(Qt.CursorShape.ArrowCursor)

        layout = _panel_layout(self)
        self.title_label = _panel_title(layout, title)
        body = _panel_body(layout)
        readings_layout = QGridLayout()
        readings_layout.setContentsMargins(0, 0, 0, 0)
        readings_layout.setHorizontalSpacing(scaled(8))
        readings_layout.setVerticalSpacing(scaled(3))
        for index, reading in enumerate(readings):
            cell = QWidget(self)
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(
                scaled(4),
                scaled(1),
                scaled(4),
                scaled(1),
            )
            cell_layout.setSpacing(0)
            name_label = QLabel(reading.display_name)
            name_label.setObjectName("readoutName")
            name_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
            value_label = QLabel("—")
            value_label.setObjectName("readoutValue")
            value_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
            cell_layout.addWidget(name_label)
            cell_layout.addWidget(value_label)
            readings_layout.addWidget(cell, index // 2, index % 2)
            self.name_labels[reading.key] = name_label
            self.value_labels[reading.key] = value_label
        readings_layout.setColumnStretch(0, 1)
        readings_layout.setColumnStretch(1, 1)
        readings_layout.setRowStretch(0, 1)
        readings_layout.setRowStretch(1, 1)
        body.addLayout(readings_layout)
        self._set_state_style("disconnected")

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        if not snapshot.connected:
            for value_label in self.value_labels.values():
                value_label.setText("—")
            self.setToolTip(snapshot.message)
            self._set_state_style(snapshot.connection_state.value)
            return
        self.setToolTip("")
        has_reading = False
        for reading in self.readings:
            if reading.key == self.main_reading:
                value = snapshot.current
                decimals = (
                    reading.decimals
                    if reading.decimals is not None
                    else 3
                )
            else:
                metric = snapshot.metrics.get(reading.key)
                value = None if metric is None else metric.value
                decimals = reading.decimals
            self.value_labels[reading.key].setText(
                _formatted_reading(value, reading.unit, decimals)
            )
            has_reading = has_reading or value is not None
        self._set_state_style("stable" if has_reading else "stale")

    def _set_state_style(self, state: str) -> None:
        self.setStyleSheet(_instrument_panel_style(state))


class SwitchPanel(QFrame):
    """显示一个开关状态，并调用该仪表声明的无参数指令。"""

    actionRequested = Signal(str, str)

    def __init__(
        self,
        config: InstrumentConfig,
        panel: InstrumentPanelConfig,
        commands: tuple[SystemInstrumentCommandSpec, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not commands:
            raise ValueError("A switch panel requires at least one command")
        self.config = config
        self.panel = panel
        self.reading = config.reading(panel.reading)
        self.buttons: dict[str, QPushButton] = {}
        self._actions_enabled = True
        self._connected = False
        self.setObjectName("instrumentPanel")
        self.setMinimumWidth(scaled(260))
        self.setMaximumHeight(scaled(135))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

        layout = _panel_layout(self)
        self.title_label, self.state_label = _panel_header(
            layout,
            panel.display_name,
        )
        body = _panel_body(layout)
        self.value_label = _value_label(body)
        button_row = QHBoxLayout()
        button_row.setSpacing(scaled(5))
        for command in commands:
            button = QPushButton(command.label)
            button.clicked.connect(
                lambda checked=False, command_id=command.command_id: (
                    self.actionRequested.emit(config.id, command_id)
                )
            )
            button.setEnabled(False)
            button_row.addWidget(button)
            self.buttons[command.command_id] = button
        body.addLayout(button_row)
        self._set_state_style("disconnected")

    def set_actions_enabled(self, enabled: bool) -> None:
        self._actions_enabled = enabled
        for button in self.buttons.values():
            button.setEnabled(enabled and self._connected)

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        self._connected = snapshot.connected
        for button in self.buttons.values():
            button.setEnabled(
                self._actions_enabled and self._connected
            )
        if not snapshot.connected:
            self.value_label.setText("—")
            self.state_label.setText(_connection_state_text(snapshot))
            self.setToolTip(snapshot.message)
            self._set_state_style(snapshot.connection_state.value)
            return
        self.setToolTip("")
        value = _snapshot_reading(
            snapshot,
            self.reading.key,
            self.config.main_reading,
        )
        if value is None:
            self.value_label.setText("—")
            self.state_label.setText("No Reading")
            self._set_state_style("stale")
            return
        self.value_label.setText(
            "On"
            if value is True or value == 1.0
            else "Off"
            if value is False or value == 0.0
            else str(value)
        )
        self.state_label.setText("Monitoring")
        self._set_state_style("stable")

    def _set_state_style(self, state: str) -> None:
        self.setStyleSheet(
            _instrument_panel_style(state, show_badge=True)
        )


class InstrumentPanelHost(QWidget):
    """按全局顺序创建面板，并把物理实例快照扇出到全部面板。"""

    controlRequested = Signal(str, str)
    actionRequested = Signal(str, str)

    def __init__(
        self,
        instruments: tuple[InstrumentConfig, ...],
        panels: tuple[InstrumentPanelConfig, ...],
        instrument_commands: tuple[
            SystemInstrumentCommandSpec,
            ...,
        ] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        instrument_by_id = {
            instrument.id: instrument for instrument in instruments
        }
        self.panels: dict[
            str,
            ControllerPanel
            | ReadoutPanel
            | ReadoutGridPanel
            | SwitchPanel,
        ] = {}
        self._panels_by_instrument: dict[
            str,
            list[
                ControllerPanel
                | ReadoutPanel
                | ReadoutGridPanel
                | SwitchPanel
            ],
        ] = {}
        self._row = QHBoxLayout(self)
        self.setObjectName("instrumentPanelHost")
        self.setStyleSheet(
            "QWidget#instrumentPanelHost { "
            "background-color: #f3f5f7; border: none; }"
        )
        self._row.setContentsMargins(
            scaled(5),
            scaled(5),
            scaled(5),
            scaled(5),
        )
        self._row.setSpacing(scaled(5))
        self._row.addStretch(1)
        self.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding,
            QSizePolicy.Policy.Preferred,
        )

        for panel_config in panels:
            instrument = instrument_by_id[panel_config.instrument_id]
            if panel_config.template == "controller":
                panel = ControllerPanel(
                    instrument,
                    panel_config,
                    self,
                )
                panel.controlRequested.connect(self.controlRequested)
            elif panel_config.template == "readout":
                panel = ReadoutPanel(
                    instrument.reading(panel_config.readings[0]),
                    instrument.main_reading,
                    panel_config.display_name,
                    parent=self,
                )
            elif panel_config.template == "readout_grid":
                panel = ReadoutGridPanel(
                    tuple(
                        instrument.reading(key)
                        for key in panel_config.readings
                    ),
                    instrument.main_reading,
                    panel_config.display_name,
                    self,
                )
            elif panel_config.template == "switch":
                panel = SwitchPanel(
                    instrument,
                    panel_config,
                    tuple(
                        command
                        for command in instrument_commands
                        if command.instrument_id == instrument.id
                        and command.command_id in panel_config.commands
                    ),
                    self,
                )
                panel.actionRequested.connect(self.actionRequested)
            else:
                raise ValueError(
                    f"Unknown instrument panel template: {panel_config.template}"
                )
            self.panels[panel_config.key] = panel
            self._panels_by_instrument.setdefault(
                instrument.id,
                [],
            ).append(panel)
            self._row.insertWidget(self._row.count() - 1, panel)

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        for panel in self._panels_by_instrument.get(
            snapshot.instrument_id,
            (),
        ):
            panel.update_snapshot(snapshot)

    def set_actions_enabled(self, enabled: bool) -> None:
        for panel in self.panels.values():
            if isinstance(panel, SwitchPanel):
                panel.set_actions_enabled(enabled)


def _panel_layout(panel: QFrame) -> QVBoxLayout:
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    return layout


def _panel_header_layout(layout: QVBoxLayout) -> QHBoxLayout:
    header = QFrame()
    header.setObjectName("panelHeader")
    header.setMinimumHeight(scaled(30))
    header_layout = QHBoxLayout(header)
    header_layout.setContentsMargins(
        scaled(10),
        scaled(4),
        scaled(10),
        scaled(4),
    )
    header_layout.setSpacing(scaled(6))
    layout.addWidget(header)
    return header_layout


def _panel_header(
    layout: QVBoxLayout,
    title: str,
) -> tuple[QLabel, QLabel]:
    header = _panel_header_layout(layout)
    title_label = QLabel(title)
    title_label.setObjectName("panelTitle")
    state_label = QLabel("Disconnected")
    state_label.setObjectName("stateBadge")
    state_label.setAlignment(Qt.AlignmentFlag.AlignRight)
    header.addWidget(title_label)
    header.addStretch(1)
    header.addWidget(state_label)
    return title_label, state_label


def _panel_title(layout: QVBoxLayout, title: str) -> QLabel:
    header = _panel_header_layout(layout)
    label = QLabel(title)
    label.setObjectName("panelTitle")
    header.addWidget(label)
    header.addStretch(1)
    return label


def _panel_body(layout: QVBoxLayout) -> QVBoxLayout:
    body = QFrame()
    body.setObjectName("panelBody")
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(
        scaled(10),
        scaled(6),
        scaled(10),
        scaled(6),
    )
    body_layout.setSpacing(scaled(2))
    layout.addWidget(body, 1)
    return body_layout


def _value_label(layout: QVBoxLayout) -> QLabel:
    label = QLabel("—")
    label.setObjectName("panelValue")
    label.setAlignment(
        Qt.AlignmentFlag.AlignLeft
        | Qt.AlignmentFlag.AlignVCenter
    )
    layout.addWidget(label)
    return label


def _detail_label(layout: QVBoxLayout, text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("panelDetail")
    label.setAlignment(Qt.AlignmentFlag.AlignLeft)
    label.setWordWrap(True)
    layout.addWidget(label)
    return label


def _connection_state_text(snapshot: InstrumentSnapshot) -> str:
    return {
        InstrumentConnectionState.STARTING: "Starting",
        InstrumentConnectionState.RECONNECTING: "Reconnecting",
        InstrumentConnectionState.FAULTED: "Faulted",
        InstrumentConnectionState.DISCONNECTED: "Disconnected",
    }.get(snapshot.connection_state, "Disconnected")


def _formatted_reading(
    value: float | int | str | bool | None,
    unit: str,
    decimals: int | None,
) -> str:
    if value is None:
        text = "—"
    elif isinstance(value, bool):
        text = "On" if value else "Off"
    elif isinstance(value, (int, float)):
        text = (
            fixed_number(float(value), decimals)
            if decimals is not None
            else f"{value:.9g}"
        )
    else:
        text = str(value)
    return text + (f" {unit}" if unit and value is not None else "")


def _snapshot_reading(
    snapshot: InstrumentSnapshot,
    reading_key: str,
    main_reading: str,
) -> float | int | str | bool | None:
    """从一份物理实例快照读取指定面板值。"""

    if reading_key == main_reading:
        return snapshot.current
    metric = snapshot.metrics.get(reading_key)
    return None if metric is None else metric.value


def _instrument_panel_style(
    state: str,
    *,
    show_badge: bool = False,
) -> str:
    """返回与主界面一致、并带清晰遥测层级的固定浅色样式。"""

    color, badge_background = {
        "stable": ("#238636", "#eaf7ef"),
        "settling": ("#9a6700", "#fff6df"),
        "moving": ("#2f6fbb", "#eaf2fb"),
        "timed_out": ("#cf222e", "#fdecec"),
        "stale": ("#9a6700", "#fff6df"),
        "starting": ("#6e7781", "#f0f2f4"),
        "reconnecting": ("#9a6700", "#fff6df"),
        "faulted": ("#cf222e", "#fdecec"),
        "disconnected": ("#6e7781", "#f0f2f4"),
    }.get(state, ("#6e7781", "#f0f2f4"))
    title_size = scaled_text(14, font_scale=1.0)
    value_size = scaled_text(26, font_scale=1.0)
    detail_size = scaled_text(12, font_scale=1.0)
    readout_size = scaled_text(17, font_scale=1.0)
    badge_size = scaled_text(12, font_scale=1.0)
    radius = scaled(6)
    badge_radius = scaled(3)
    badge_horizontal_padding = scaled(6)
    bottom_color = "#d5dbe2" if show_badge else color
    return (
        "QFrame#instrumentPanel { background-color: #ffffff; "
        "border: 1px solid #d5dbe2; "
        f"border-bottom: 3px solid {bottom_color}; "
        f"border-radius: {radius}px; }}"
        "QFrame#instrumentPanel QFrame#panelHeader { "
        "background-color: #f7f9fb; border: none; "
        "border-bottom: 1px solid #e1e6eb; }"
        "QFrame#instrumentPanel QFrame#panelBody { "
        "background: transparent; border: none; }"
        "QFrame#instrumentPanel QLabel { background: transparent; "
        "border: none; color: #1f2328; }"
        f"QFrame#instrumentPanel QLabel#panelTitle {{ color: #626b75; "
        f"font-size: {title_size}px; font-weight: bold; }}"
        f"QFrame#instrumentPanel QLabel#stateBadge {{ color: {color}; "
        f"background-color: {badge_background}; border: 1px solid {color}; "
        f"border-radius: {badge_radius}px; padding: 1px {badge_horizontal_padding}px; "
        f"font-size: {badge_size}px; font-weight: bold; }}"
        f"QFrame#instrumentPanel QLabel#panelValue {{ color: #1f2328; "
        f"font-size: {value_size}px; font-weight: bold; "
        "font-family: Consolas; }"
        f"QFrame#instrumentPanel QLabel#panelDetail {{ color: #6e7781; "
        f"font-size: {detail_size}px; }}"
        f"QFrame#instrumentPanel QLabel#readoutName {{ color: #6e7781; "
        f"font-size: {detail_size}px; }}"
        f"QFrame#instrumentPanel QLabel#readoutValue {{ color: #1f2328; "
        f"font-size: {readout_size}px; font-weight: bold; "
        "font-family: Consolas; }"
        f"QFrame#instrumentPanel QPushButton {{ background: #f6f8fa; "
        "color: #202124; border: 1px solid #b8bec6; border-radius: 3px; "
        f"font-size: {detail_size}px; padding: 2px 8px; }}"
        "QFrame#instrumentPanel QPushButton:hover { background: #eef2f7; }"
        "QFrame#instrumentPanel QPushButton:pressed { background: #e2e8f0; }"
        "QFrame#instrumentPanel QPushButton:disabled { color: #8a8a8a; "
        "background: #eeeeee; }"
    )


__all__ = [
    "ControllerPanel",
    "InstrumentPanelHost",
    "ReadoutPanel",
    "ReadoutGridPanel",
    "SwitchPanel",
]
