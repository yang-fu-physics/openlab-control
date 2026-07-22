from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import DeviceConfig
from ..formatting import control_decimals, field_decimals, fixed_number
from ..models import DeviceKind, DeviceSnapshot, LabEvent, Severity
from ..sequence.model import Command, CommandSpec, CommandType
from ..units import convert_value
from .scaling import scaled


TEMPERATURE_COMMANDS = {CommandType.SET_TEMPERATURE, CommandType.SCAN_TEMPERATURE}
FIELD_COMMANDS = {CommandType.SET_FIELD, CommandType.SCAN_FIELD}


def _command_decimals(command: Command, field_name: str) -> int:
    if command.type in TEMPERATURE_COMMANDS and field_name in {"target", "start", "stop", "rate"}:
        return 3
    if command.type in FIELD_COMMANDS and field_name in {"target", "start", "stop", "rate"}:
        return field_decimals(command.params.get("unit", "Oe"))
    return 9


class CommandDialog(QDialog):
    def __init__(self, command: Command, spec: CommandSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.command = command
        self.spec = spec
        self.inputs: dict[str, QWidget] = {}
        self.setWindowTitle(f"Command Parameters - {spec.label}")
        self.setModal(True)
        self.setMinimumWidth(scaled(430))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        for field in spec.fields:
            value = command.params.get(field.name, field.default)
            if field.field_type == "choice":
                widget = QComboBox()
                widget.addItems(field.choices)
                index = widget.findText(str(value), Qt.MatchFlag.MatchFixedString)
                widget.setCurrentIndex(max(0, index))
            elif field.field_type == "int":
                widget = QSpinBox()
                widget.setRange(int(field.minimum if field.minimum is not None else -2_000_000_000), int(field.maximum if field.maximum is not None else 2_000_000_000))
                widget.setValue(int(value))
            elif field.field_type == "float":
                widget = QDoubleSpinBox()
                decimals = _command_decimals(command, field.name)
                widget.setDecimals(decimals)
                minimum = field.minimum if field.minimum is not None else -1e12
                if field.name == "rate" and command.type in FIELD_COMMANDS | TEMPERATURE_COMMANDS:
                    minimum = max(minimum, 10 ** -decimals)
                widget.setRange(
                    minimum,
                    field.maximum if field.maximum is not None else 1e12,
                )
                widget.setValue(float(value))
                widget.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
            else:
                widget = QLineEdit(str(value))
            self.inputs[field.name] = widget
            form.addRow(field.label, widget)
        self._field_unit = ""
        if command.type in FIELD_COMMANDS:
            unit_input = self.inputs.get("unit")
            if isinstance(unit_input, QComboBox):
                self._field_unit = unit_input.currentText()
                unit_input.currentTextChanged.connect(self._change_field_unit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _change_field_unit(self, new_unit: str) -> None:
        old_unit = self._field_unit
        if not old_unit or old_unit == new_unit:
            return
        decimals = field_decimals(new_unit)
        for name in ("target", "start", "stop", "rate"):
            widget = self.inputs.get(name)
            if not isinstance(widget, QDoubleSpinBox):
                continue
            value = convert_value(widget.value(), old_unit, new_unit)
            widget.setDecimals(decimals)
            if name == "rate":
                widget.setMinimum(10 ** -decimals)
            widget.setValue(value)
        self._field_unit = new_unit

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field in self.spec.fields:
            widget = self.inputs[field.name]
            if isinstance(widget, QComboBox):
                result[field.name] = widget.currentText()
            elif isinstance(widget, QSpinBox):
                result[field.name] = widget.value()
            elif isinstance(widget, QDoubleSpinBox):
                result[field.name] = widget.value()
            elif isinstance(widget, QLineEdit):
                result[field.name] = widget.text().strip()
        return result


class ManualControlDialog(QDialog):
    setRequested = Signal(str, float, float, str)
    holdRequested = Signal(str)
    measureRequested = Signal(str)

    def __init__(self, config: DeviceConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle(f"{config.display_name} - Manual Control")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumWidth(scaled(390))
        layout = QVBoxLayout(self)
        self.current_label = QLabel("Current: —")
        self.current_label.setObjectName("manualCurrent")
        layout.addWidget(self.current_label)

        if config.kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
            self._precision = control_decimals(config.kind, config.unit)
            form = QFormLayout()
            self.target_input = QDoubleSpinBox()
            self.target_input.setDecimals(self._precision)
            self.target_input.setRange(config.min_value, config.max_value)
            self.target_input.setSuffix(f" {config.unit}")
            self.target_input.setValue(config.initial_value)
            self.rate_input = QDoubleSpinBox()
            self.rate_input.setDecimals(self._precision)
            self.rate_input.setRange(10 ** -self._precision, config.max_rate_per_minute)
            self.rate_input.setSuffix(f" {config.unit}/min")
            self.rate_input.setValue(config.default_rate_per_minute)
            self.mode_input = QComboBox()
            self.mode_input.addItems(["Settle", "Sweep"])
            form.addRow("Target", self.target_input)
            form.addRow("Rate", self.rate_input)
            form.addRow("Mode", self.mode_input)
            layout.addLayout(form)
            buttons = QHBoxLayout()
            apply_button = QPushButton("Set")
            hold_button = QPushButton("Hold Current")
            close_button = QPushButton("Close")
            apply_button.clicked.connect(self._emit_set)
            hold_button.clicked.connect(lambda: self.holdRequested.emit(config.id))
            close_button.clicked.connect(self.hide)
            buttons.addWidget(apply_button)
            buttons.addWidget(hold_button)
            buttons.addStretch(1)
            buttons.addWidget(close_button)
            layout.addLayout(buttons)
            self.channels_label = None
        else:
            self.channels_label = QLabel("No measurement yet")
            self.channels_label.setWordWrap(True)
            self.channels_label.setFrameShape(QFrame.Shape.StyledPanel)
            layout.addWidget(self.channels_label)
            buttons = QHBoxLayout()
            measure_button = QPushButton("Measure Now")
            close_button = QPushButton("Close")
            measure_button.clicked.connect(lambda: self.measureRequested.emit(config.id))
            close_button.clicked.connect(self.hide)
            buttons.addWidget(measure_button)
            buttons.addStretch(1)
            buttons.addWidget(close_button)
            layout.addLayout(buttons)

    def _emit_set(self) -> None:
        self.setRequested.emit(
            self.config.id,
            self.target_input.value(),
            self.rate_input.value(),
            self.mode_input.currentText(),
        )

    def update_snapshot(self, snapshot: DeviceSnapshot) -> None:
        if snapshot.current is not None:
            if snapshot.kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
                precision = control_decimals(snapshot.kind, snapshot.unit)
                value = fixed_number(snapshot.current, precision)
            else:
                value = f"{snapshot.current:.9g}"
            self.current_label.setText(f"Current: {value} {snapshot.unit}")
        elif snapshot.channels:
            self.current_label.setText("Current: Connected")
        if self.config.kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
            if snapshot.target is not None and not self.target_input.hasFocus():
                self.target_input.setValue(snapshot.target)
        elif self.channels_label is not None:
            values = [
                f"{key}: {'—' if value is None else f'{value:.9g}'} {snapshot.unit}"
                for key, value in snapshot.channels.items()
            ]
            self.channels_label.setText("\n".join(values) if values else "No measurement yet")


class AlertDialog(QDialog):
    def __init__(self, event: LabEvent, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.event_key = event.key
        is_error = event.severity is Severity.ERROR
        self.setWindowTitle("Error" if is_error else "Warning")
        self.setModal(False)
        self.setMinimumWidth(scaled(460))
        layout = QVBoxLayout(self)
        title = QLabel("Measurement Aborted" if is_error else "Measurement Continues")
        title.setStyleSheet(
            f"font-size: {scaled(18)}px; font-weight: 600; "
            f"color: {'#b42318' if is_error else '#a15c00'};"
        )
        layout.addWidget(title)
        message = QLabel(event.message)
        message.setWordWrap(True)
        layout.addWidget(message)
        details = QLabel(
            f"Source: {event.source}\nCode: {event.code}"
            + (f"\nContext: {event.context}" if event.context else "")
        )
        details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details.setStyleSheet(
            f"color: #555; background: #f2f2f2; padding: {scaled(8)}px;"
        )
        layout.addWidget(details)
        button = QPushButton("Acknowledge")
        button.clicked.connect(self.accept)
        button.setDefault(True)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        layout.addLayout(row)
