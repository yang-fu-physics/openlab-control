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

from ..config import InstrumentConfig, InstrumentReadingConfig
from ..formatting import control_decimals, fixed_number
from ..models import (
    InstrumentConnectionState,
    InstrumentSnapshot,
    StabilityState,
)
from ..sequence.model import SystemInstrumentCommandSpec
from .scaling import scaled


class ControllerPanel(QFrame):
    """显示温度或磁场的当前值、目标、速率和稳定状态。"""

    controlRequested = Signal(str)

    def __init__(
        self,
        config: InstrumentConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.setObjectName("instrumentPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(scaled(205))
        self.setMaximumHeight(scaled(105))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if config.control_enabled
            else Qt.CursorShape.ArrowCursor
        )

        layout = _panel_layout(self)
        self.title_label, self.state_label = _panel_header(
            layout,
            config.display_name,
        )
        self.value_label = _value_label(layout)
        self.detail_label = _detail_label(
            layout,
            "Double-click to control"
            if config.control_enabled
            else "Display only · not used for control",
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
                    if self.config.control_enabled
                    else "Display only · not used for control"
                )
            )
            self._set_state_style(snapshot.connection_state.value)
            return

        precision = control_decimals(snapshot.kind, snapshot.unit)
        current = (
            "—"
            if snapshot.current is None
            else f"{fixed_number(snapshot.current, precision)} {snapshot.unit}"
        )
        target = (
            "—"
            if snapshot.target is None
            else f"{fixed_number(snapshot.target, precision)} {snapshot.unit}"
        )
        rate = (
            "—"
            if snapshot.rate_per_minute is None
            else f"{fixed_number(snapshot.rate_per_minute, precision)} "
            f"{snapshot.unit}/min"
        )
        self.value_label.setText(current)
        self.detail_label.setText(
            f"Target {target}  ·  {rate}"
            if self.config.control_enabled
            else "Display only · not used for control"
        )
        self.state_label.setText(
            {
                StabilityState.STABLE: "Stable",
                StabilityState.SETTLING: "Settling",
                StabilityState.MOVING: "Moving",
                StabilityState.TIMED_OUT: "Timed Out",
                StabilityState.STALE: "Stale",
            }.get(snapshot.stability, snapshot.activity.value)
        )
        self._set_state_style(snapshot.stability.value)

    def mouseDoubleClickEvent(
        self,
        event: QMouseEvent,
    ) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.config.control_enabled
        ):
            self.controlRequested.emit(self.config.id)
        super().mouseDoubleClickEvent(event)

    def _set_state_style(self, state: str) -> None:
        self.setStyleSheet(_instrument_panel_style(state))


class ReadoutPanel(QFrame):
    """直接显示一个只读值，不添加标题栏或状态说明。"""

    def __init__(
        self,
        reading: InstrumentReadingConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.reading = reading
        self.setObjectName("instrumentPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(scaled(205))
        self.setMaximumHeight(scaled(105))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.setCursor(Qt.CursorShape.ArrowCursor)

        layout = _panel_layout(self)
        layout.addStretch(1)
        self.name_label = QLabel(reading.display_name)
        self.name_label.setObjectName("panelTitle")
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("panelValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.name_label)
        layout.addWidget(self.value_label)
        layout.addStretch(1)
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
        self.value_label.setText(
            _formatted_reading(
                snapshot.current,
                self.reading.unit,
                decimals,
            )
        )
        self._set_state_style(
            "stable" if snapshot.current is not None else "stale"
        )

    def _set_state_style(self, state: str) -> None:
        self.setStyleSheet(_instrument_panel_style(state))


class ReadoutGridPanel(QFrame):
    """在一个 2×2 面板内直接显示一至四个读数。"""

    def __init__(
        self,
        readings: tuple[InstrumentReadingConfig, ...],
        main_reading: str,
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
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(scaled(300))
        self.setMaximumHeight(scaled(105))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.setCursor(Qt.CursorShape.ArrowCursor)

        layout = _panel_layout(self)
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
            name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value_label = QLabel("—")
            value_label.setObjectName("readoutValue")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_layout.addWidget(name_label)
            cell_layout.addWidget(value_label)
            readings_layout.addWidget(cell, index // 2, index % 2)
            self.name_labels[reading.key] = name_label
            self.value_labels[reading.key] = value_label
        layout.addLayout(readings_layout)
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
        commands: tuple[SystemInstrumentCommandSpec, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not commands:
            raise ValueError("A switch panel requires at least one command")
        self.config = config
        self.buttons: dict[str, QPushButton] = {}
        self._actions_enabled = True
        self._connected = False
        self.setObjectName("instrumentPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(scaled(260))
        self.setMaximumHeight(scaled(105))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

        layout = _panel_layout(self)
        self.title_label, self.state_label = _panel_header(
            layout,
            config.display_name,
        )
        self.value_label = _value_label(layout)
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
        layout.addLayout(button_row)
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
        if snapshot.current is None:
            self.value_label.setText("—")
            self.state_label.setText("No Reading")
            self._set_state_style("stale")
            return
        self.value_label.setText(
            "On"
            if snapshot.current == 1.0
            else "Off"
            if snapshot.current == 0.0
            else f"{snapshot.current:.9g}"
        )
        self.state_label.setText("Monitoring")
        self._set_state_style("stable")

    def _set_state_style(self, state: str) -> None:
        self.setStyleSheet(_instrument_panel_style(state))


class InstrumentPanelHost(QWidget):
    """按配置创建主面板，并把辅助读数面板依次放在右侧。"""

    controlRequested = Signal(str)
    actionRequested = Signal(str, str)

    def __init__(
        self,
        instruments: tuple[InstrumentConfig, ...],
        instrument_commands: tuple[
            SystemInstrumentCommandSpec,
            ...,
        ] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.main_panels: dict[
            str,
            ControllerPanel
            | ReadoutPanel
            | ReadoutGridPanel
            | SwitchPanel,
        ] = {}
        self.readout_panels: dict[
            tuple[str, int],
            ReadoutGridPanel,
        ] = {}
        self._readout_panels_by_instrument: dict[
            str,
            list[ReadoutGridPanel],
        ] = {}
        self._row = QHBoxLayout(self)
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

        right_panels: list[ReadoutGridPanel] = []
        for instrument in instruments:
            if instrument.panel_template == "controller":
                controller_panel = ControllerPanel(
                    instrument,
                    self,
                )
                controller_panel.controlRequested.connect(
                    self.controlRequested
                )
                panel: (
                    ControllerPanel
                    | ReadoutPanel
                    | SwitchPanel
                    | None
                ) = controller_panel
                readout_keys = instrument.auxiliary_readings
            elif instrument.panel_template == "readout":
                panel = ReadoutPanel(
                    instrument.reading(instrument.main_reading),
                    self,
                )
                readout_keys = ()
            elif instrument.panel_template == "readout_grid":
                panel = None
                readout_keys = (
                    instrument.main_reading,
                    *instrument.auxiliary_readings,
                )
            elif instrument.panel_template == "switch":
                switch_panel = SwitchPanel(
                    instrument,
                    tuple(
                        command
                        for command in instrument_commands
                        if command.instrument_id == instrument.id
                    ),
                    self,
                )
                switch_panel.actionRequested.connect(
                    self.actionRequested
                )
                panel = switch_panel
                readout_keys = instrument.auxiliary_readings
            else:
                raise ValueError(
                    f"Unknown instrument panel template: {instrument.panel_template}"
                )
            if panel is not None:
                self.main_panels[instrument.id] = panel
                self._row.insertWidget(self._row.count() - 1, panel)

            groups = tuple(
                readout_keys[index : index + 4]
                for index in range(0, len(readout_keys), 4)
            )
            instrument_readouts: list[ReadoutGridPanel] = []
            for group_index, reading_keys in enumerate(groups):
                readout_panel = ReadoutGridPanel(
                    tuple(
                        instrument.reading(reading_key)
                        for reading_key in reading_keys
                    ),
                    instrument.main_reading,
                    self,
                )
                self.readout_panels[(instrument.id, group_index)] = readout_panel
                instrument_readouts.append(readout_panel)
                if (
                    instrument.panel_template == "readout_grid"
                    and group_index == 0
                ):
                    self.main_panels[instrument.id] = readout_panel
                    self._row.insertWidget(
                        self._row.count() - 1,
                        readout_panel,
                    )
                else:
                    right_panels.append(readout_panel)
            self._readout_panels_by_instrument[
                instrument.id
            ] = instrument_readouts

        for readout_panel in right_panels:
            self._row.insertWidget(self._row.count() - 1, readout_panel)

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        main_panel = self.main_panels[snapshot.instrument_id]
        if isinstance(
            main_panel,
            (ControllerPanel, ReadoutPanel, SwitchPanel),
        ):
            main_panel.update_snapshot(snapshot)
        for readout_panel in self._readout_panels_by_instrument[
            snapshot.instrument_id
        ]:
            readout_panel.update_snapshot(snapshot)

    def set_actions_enabled(self, enabled: bool) -> None:
        for panel in self.main_panels.values():
            if isinstance(panel, SwitchPanel):
                panel.set_actions_enabled(enabled)


def _panel_layout(panel: QFrame) -> QVBoxLayout:
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(
        scaled(10),
        scaled(6),
        scaled(10),
        scaled(6),
    )
    layout.setSpacing(scaled(2))
    return layout


def _panel_header(
    layout: QVBoxLayout,
    title: str,
) -> tuple[QLabel, QLabel]:
    header = QHBoxLayout()
    title_label = QLabel(title)
    title_label.setObjectName("panelTitle")
    state_label = QLabel("Disconnected")
    state_label.setAlignment(Qt.AlignmentFlag.AlignRight)
    header.addWidget(title_label)
    header.addStretch(1)
    header.addWidget(state_label)
    layout.addLayout(header)
    return title_label, state_label


def _value_label(layout: QVBoxLayout) -> QLabel:
    label = QLabel("—")
    label.setObjectName("panelValue")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(label)
    return label


def _detail_label(layout: QVBoxLayout, text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("panelDetail")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
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


def _instrument_panel_style(state: str) -> str:
    """返回不受 Windows 深浅主题影响的固定浅色面板样式。"""

    color = {
        "stable": "#2e9d55",
        "settling": "#d08a00",
        "moving": "#2e73c5",
        "timed_out": "#c53b3b",
        "stale": "#a55a00",
        "starting": "#777777",
        "reconnecting": "#d08a00",
        "faulted": "#c53b3b",
        "disconnected": "#777777",
    }.get(state, "#777777")
    return (
        "QFrame#instrumentPanel { background: #ffffff; "
        "border: 1px solid #c0c0c0; "
        f"border-bottom: 4px solid {color}; "
        "border-radius: 4px; }"
        "QFrame#instrumentPanel QLabel { background: transparent; color: #202124; }"
        "QFrame#instrumentPanel QLabel#panelDetail { color: #6f6f6f; }"
        "QFrame#instrumentPanel QLabel#readoutName { color: #6f6f6f; }"
        "QFrame#instrumentPanel QLabel#readoutValue { color: #202124; }"
        "QFrame#instrumentPanel QPushButton { background: #f7f8fa; "
        "color: #202124; border: 1px solid #b8bec6; border-radius: 3px; "
        "padding: 2px 8px; }"
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
