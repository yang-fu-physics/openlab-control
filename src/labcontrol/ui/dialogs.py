"""SEQ 参数、手动控制和事件提示对话框。

``CommandDialog`` 与 ``ManualControlDialog`` 都直接读取同一个 ``InstrumentConfig`` 的目标上下限
和最大速率；磁场切换 T/Oe 时先转换当前值再重建范围。UI 限制用于提前阻止误输入，但不是
最终安全边界，所有写入仍由 ``InstrumentManager.validate_target`` 在后台再次校验。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import InstrumentConfig, InstrumentPanelConfig
from ..formatting import control_decimals, field_decimals, fixed_number
from ..models import InstrumentKind, InstrumentSnapshot, LabEvent, Severity
from ..module_commands import ModuleCommandSpec
from ..sequence.model import Command, CommandSpec, CommandType
from ..sequence.parser import format_temperature_points, parse_temperature_points
from ..units import UnitConversionError, convert_value
from .scaling import scaled, scaled_text
from .window_sizing import fit_initial_window_width


TEMPERATURE_COMMANDS = {CommandType.SET_TEMPERATURE, CommandType.SCAN_TEMPERATURE}
FIELD_COMMANDS = {CommandType.SET_FIELD, CommandType.SCAN_FIELD}


def _command_decimals(command: Command, field_name: str) -> int:
    """按温度、磁场单位和普通数值选择 SEQ 输入精度。"""

    if command.type in TEMPERATURE_COMMANDS and field_name in {"target", "start", "stop", "rate"}:
        return 3
    if command.type in FIELD_COMMANDS and field_name in {"target", "start", "stop", "rate"}:
        return field_decimals(command.params.get("unit", "Oe"))
    return 9


class CommandDialog(QDialog):
    """根据 ``CommandSpec`` 动态生成参数窗口，并绑定对应仪表配置限制。"""

    def __init__(
        self,
        command: Command,
        spec: CommandSpec | ModuleCommandSpec,
        parent: QWidget | None = None,
        *,
        instrument_configs: tuple[InstrumentConfig, ...] = (),
        data_directory: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.command = command
        self.spec = spec
        self._instrument_configs = {item.id: item for item in instrument_configs}
        self._data_directory = Path(data_directory or Path.cwd()).resolve()
        self.inputs: dict[str, QWidget] = {}
        self.limit_label: QLabel | None = None
        self.datafile_browse_button: QPushButton | None = None
        self.setWindowTitle(f"Command Parameters - {spec.label}")
        self.setModal(True)
        layout = QVBoxLayout(self)
        if isinstance(spec, ModuleCommandSpec) and spec.description:
            description = QLabel(spec.description)
            description.setWordWrap(True)
            description.setObjectName("mutedLabel")
            layout.addWidget(description)
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
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
                if field.unit:
                    widget.setSuffix(f" {field.unit}")
            elif field.field_type == "float":
                widget = QDoubleSpinBox()
                decimals = (
                    field.decimals
                    if field.decimals is not None
                    else _command_decimals(command, field.name)
                )
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
                if field.unit:
                    widget.setSuffix(f" {field.unit}")
            elif field.field_type == "bool":
                widget = QCheckBox()
                widget.setChecked(bool(value))
            elif field.field_type == "list":
                widget = QLineEdit(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
            else:
                widget = QLineEdit(str(value))
            self.inputs[field.name] = widget
            if (
                command.type is CommandType.SET_DATAFILE
                and field.name == "path"
                and isinstance(widget, QLineEdit)
            ):
                # 使用 QFileDialog 的静态入口；在 Windows 上 Qt 默认调用系统原生
                # 文件选择窗口。路径输入框仍然保留，便于粘贴网络盘或历史路径。
                path_row = QWidget()
                path_layout = QHBoxLayout(path_row)
                path_layout.setContentsMargins(0, 0, 0, 0)
                path_layout.setSpacing(scaled(6))
                path_layout.addWidget(widget, 1)
                self.datafile_browse_button = QPushButton("Browse…")
                self.datafile_browse_button.clicked.connect(
                    self._browse_datafile
                )
                path_layout.addWidget(self.datafile_browse_button)
                self.form.addRow(field.label, path_row)
            else:
                self.form.addRow(field.label, widget)
        self._field_unit = ""
        if command.type in FIELD_COMMANDS:
            unit_input = self.inputs.get("unit")
            if isinstance(unit_input, QComboBox):
                self._field_unit = unit_input.currentText()
                unit_input.currentTextChanged.connect(self._change_field_unit)
        if command.type is CommandType.SCAN_TEMPERATURE:
            point_mode = self.inputs.get("point_mode")
            if isinstance(point_mode, QComboBox):
                point_mode.currentTextChanged.connect(self._update_temperature_point_mode)
                self._update_temperature_point_mode(point_mode.currentText())
        layout.addLayout(self.form)
        if command.type in FIELD_COMMANDS | TEMPERATURE_COMMANDS:
            self.limit_label = QLabel()
            self.limit_label.setObjectName("configuredLimits")
            self.limit_label.setWordWrap(True)
            self.limit_label.setStyleSheet("color: #59636e;")
            layout.addWidget(self.limit_label)
            self._apply_instrument_limits()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        fit_initial_window_width(self)

    def _browse_datafile(self) -> None:
        """按当前 Mode 调用 Windows 文件窗口并回填绝对 DAT 路径。"""

        path_input = self.inputs.get("path")
        mode_input = self.inputs.get("mode")
        scope_input = self.inputs.get("path_scope")
        if not isinstance(path_input, QLineEdit):
            return
        mode = (
            mode_input.currentText().strip().casefold()
            if isinstance(mode_input, QComboBox)
            else "open|create"
        )
        current = Path(path_input.text().strip() or "experiment.dat")
        initial = (
            current
            if current.is_absolute()
            else self._data_directory / current
        )
        if mode == "open":
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "Open Existing DAT File",
                str(initial),
                "Data (*.dat);;All Files (*)",
            )
        else:
            selected, _ = QFileDialog.getSaveFileName(
                self,
                "Select or Create DAT File",
                str(initial),
                "Data (*.dat);;All Files (*)",
            )
        if not selected:
            return
        selected_path = Path(selected)
        if mode != "open" and selected_path.suffix.casefold() != ".dat":
            selected_path = selected_path.with_suffix(".dat")
        selected_path = selected_path.resolve()
        path_input.setText(str(selected_path))
        self._data_directory = selected_path.parent
        if isinstance(scope_input, QComboBox):
            scope_input.setCurrentText("Custom folder")

    def _set_field_visible(self, field_name: str, visible: bool) -> None:
        widget = self.inputs.get(field_name)
        if widget is None:
            return
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    def _update_temperature_point_mode(self, point_mode: str) -> None:
        """在线性起止点和显式温度列表两种扫描输入间切换。"""

        is_list = point_mode.casefold() == "list"
        for name in ("start", "stop", "steps"):
            self._set_field_visible(name, not is_list)
        self._set_field_visible("points", is_list)

    def accept(self) -> None:
        """关闭前验证列表语法，以及温度列表的仪表范围。"""

        for field in self.spec.fields:
            if field.field_type != "list":
                continue
            list_input = self.inputs.get(field.name)
            if not isinstance(list_input, QLineEdit):
                continue
            try:
                value = json.loads(list_input.text())
                if not isinstance(value, list):
                    raise ValueError("value must be a JSON array")
                # 重新编码可同时拒绝模块参数中的 NaN/Infinity 等非标准值。
                canonical = json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                QMessageBox.warning(
                    self,
                    "Invalid List",
                    f"{field.label}: {exc}",
                )
                list_input.setFocus()
                list_input.selectAll()
                return
            list_input.setText(canonical)

        if self.command.type is CommandType.SCAN_TEMPERATURE:
            point_mode = self.inputs.get("point_mode")
            points_input = self.inputs.get("points")
            if (
                isinstance(point_mode, QComboBox)
                and point_mode.currentText().casefold() == "list"
                and isinstance(points_input, QLineEdit)
            ):
                try:
                    points = parse_temperature_points(points_input.text())
                    self._validate_temperature_points(points)
                    points_input.setText(format_temperature_points(points_input.text()))
                except ValueError as exc:
                    QMessageBox.warning(self, "Invalid Temperature List", str(exc))
                    points_input.setFocus()
                    points_input.selectAll()
                    return
        super().accept()

    def _selected_control(
        self,
    ) -> tuple[InstrumentConfig, InstrumentPanelConfig] | None:
        """返回标准命令由全局角色选中的物理实例和控制面板。"""

        role = (
            "sample_temp"
            if self.command.type in TEMPERATURE_COMMANDS
            else "field"
        )
        for config in self._instrument_configs.values():
            for panel in config.panels:
                if (
                    panel.enabled
                    and panel.template == "controller"
                    and panel.role == role
                ):
                    return config, panel
        return None

    def _command_unit(self) -> str:
        if self.command.type in TEMPERATURE_COMMANDS:
            return "K"
        unit_input = self.inputs.get("unit")
        return unit_input.currentText() if isinstance(unit_input, QComboBox) else self._field_unit

    def _reset_control_ranges(self) -> None:
        """先恢复命令通用范围，再叠加具体仪表限制。"""

        for field in self.spec.fields:
            widget = self.inputs.get(field.name)
            if not isinstance(widget, QDoubleSpinBox):
                continue
            minimum = field.minimum if field.minimum is not None else -1e12
            if field.name == "rate":
                minimum = max(minimum, 10 ** -widget.decimals())
            maximum = field.maximum if field.maximum is not None else 1e12
            widget.setRange(float(minimum), float(maximum))
            widget.setToolTip("")

    @staticmethod
    def _finite_limit(value: float, fallback: float) -> float:
        return value if math.isfinite(value) else fallback

    def _apply_instrument_limits(self, *_args: object) -> None:
        """把主配置中的目标和速率限制实时应用到当前 SEQ 参数控件。

        仪表单位与命令单位不同时显式换算。没有匹配配置时仅提示，不能伪造一个宽松安全范围；
        运行前后台验证仍会拒绝未知或越界仪表命令。
        """

        self._reset_control_ranges()
        unit = self._command_unit()
        self._set_control_suffixes(unit)
        selected = self._selected_control()
        if selected is None:
            if self.limit_label is not None:
                self.limit_label.setText(
                    "No controller panel is assigned the required role."
                )
            return
        config, panel = selected
        instrument_unit = config.reading(panel.reading).unit
        try:
            minimum = convert_value(panel.min_value, instrument_unit, unit)
            maximum = convert_value(panel.max_value, instrument_unit, unit)
            maximum_rate = convert_value(
                panel.max_rate_per_minute,
                instrument_unit,
                unit,
            )
        except UnitConversionError as exc:
            if self.limit_label is not None:
                self.limit_label.setText(f"Configured limits cannot be converted: {exc}")
            return

        minimum = self._finite_limit(minimum, -1e12)
        maximum = self._finite_limit(maximum, 1e12)
        maximum_rate = self._finite_limit(maximum_rate, 1e12)
        limit_tooltip = (
            f"From controller panel '{panel.key}' in the configuration file: "
            "min_value, max_value, and max_rate_per_minute."
        )
        for name in ("target", "start", "stop"):
            widget = self.inputs.get(name)
            if isinstance(widget, QDoubleSpinBox):
                widget.setRange(minimum, maximum)
                widget.setToolTip(limit_tooltip)
        rate_input = self.inputs.get("rate")
        if isinstance(rate_input, QDoubleSpinBox):
            smallest_rate = min(10 ** -rate_input.decimals(), maximum_rate)
            rate_input.setRange(smallest_rate, maximum_rate)
            rate_input.setToolTip(limit_tooltip)
        if self.limit_label is not None:
            decimals = 3 if self.command.type in TEMPERATURE_COMMANDS else field_decimals(unit)
            self.limit_label.setText(
                f"Configured limits ({panel.key}): "
                f"{fixed_number(minimum, decimals)} to {fixed_number(maximum, decimals)} {unit}; "
                f"rate > 0 to {fixed_number(maximum_rate, decimals)} {unit}/min."
            )

    def _set_control_suffixes(self, unit: str) -> None:
        for name in ("target", "start", "stop"):
            widget = self.inputs.get(name)
            if isinstance(widget, QDoubleSpinBox):
                widget.setSuffix(f" {unit}")
        rate_input = self.inputs.get("rate")
        if isinstance(rate_input, QDoubleSpinBox):
            rate_input.setSuffix(f" {unit}/min")

    def _validate_temperature_points(self, points: tuple[float, ...]) -> None:
        """逐点验证非线性温度列表，不允许某个中间点越过仪表边界。"""

        selected = self._selected_control()
        if selected is None:
            return
        config, panel = selected
        instrument_unit = config.reading(panel.reading).unit
        for index, point in enumerate(points, start=1):
            try:
                instrument_value = convert_value(point, "K", instrument_unit)
            except UnitConversionError as exc:
                raise ValueError(str(exc)) from exc
            if not panel.min_value <= instrument_value <= panel.max_value:
                raise ValueError(
                    f"Temperature point {index} ({fixed_number(point, 3)} K) is outside "
                    f"the configured range {fixed_number(panel.min_value, 3)} to "
                    f"{fixed_number(panel.max_value, 3)} {instrument_unit}."
                )

    def _change_field_unit(self, new_unit: str) -> None:
        """切换 T/Oe 时换算当前输入值，然后按新单位重新关联配置限制。"""

        old_unit = self._field_unit
        if not old_unit or old_unit == new_unit:
            return
        decimals = field_decimals(new_unit)
        converted_values: dict[str, float] = {}
        for name in ("target", "start", "stop", "rate"):
            widget = self.inputs.get(name)
            if not isinstance(widget, QDoubleSpinBox):
                continue
            converted_values[name] = convert_value(widget.value(), old_unit, new_unit)
            widget.setDecimals(decimals)
        self._field_unit = new_unit
        self._apply_instrument_limits()
        for name, value in converted_values.items():
            widget = self.inputs.get(name)
            if isinstance(widget, QDoubleSpinBox):
                widget.setValue(value)

    def values(self) -> dict[str, Any]:
        """提取控件值供命令模型更新；本方法本身不执行仪表操作。"""

        result: dict[str, Any] = {}
        for field in self.spec.fields:
            widget = self.inputs[field.name]
            if isinstance(widget, QComboBox):
                result[field.name] = widget.currentText()
            elif isinstance(widget, QSpinBox):
                result[field.name] = widget.value()
            elif isinstance(widget, QDoubleSpinBox):
                result[field.name] = widget.value()
            elif isinstance(widget, QCheckBox):
                result[field.name] = widget.isChecked()
            elif isinstance(widget, QLineEdit):
                if field.field_type == "list":
                    result[field.name] = json.loads(widget.text())
                else:
                    result[field.name] = widget.text().strip()
        return result


class ManualControlDialog(QDialog):
    """单台可控温度/磁场仪表的手动 Set/Hold 窗口。

    数值控件直接采用所选控制面板的上下限和最大速率。SEQ 持有控制权或仪表失联时，
    主窗口通过 ``set_runtime_editable`` 与快照共同禁用按钮。
    """

    setRequested = Signal(str, str, float, float)
    holdRequested = Signal(str, str)

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
        self.kind = {
            "sample_temp": InstrumentKind.TEMPERATURE,
            "field": InstrumentKind.FIELD,
        }.get(panel.role, config.kind)
        self.setWindowTitle(f"{panel.display_name} - Manual Control")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        layout = QVBoxLayout(self)
        self.current_label = QLabel("Current: —")
        self.current_label.setObjectName("manualCurrent")
        layout.addWidget(self.current_label)

        if (
            panel.template != "controller"
            or not panel.enabled
        ):
            raise ValueError("Manual control is available only for temperature and field instruments")
        self._runtime_editable = True
        self._connected = False
        self._precision = (
            self.reading.decimals
            if self.reading.decimals is not None
            else control_decimals(self.kind, self.reading.unit)
        )
        form = QFormLayout()
        self.target_input = QDoubleSpinBox()
        self.target_input.setDecimals(self._precision)
        self.target_input.setRange(panel.min_value, panel.max_value)
        self.target_input.setSuffix(f" {self.reading.unit}")
        self.target_input.setValue(config.initial_value)
        self.rate_input = QDoubleSpinBox()
        self.rate_input.setDecimals(self._precision)
        self.rate_input.setRange(10 ** -self._precision, panel.max_rate_per_minute)
        self.rate_input.setSuffix(f" {self.reading.unit}/min")
        self.rate_input.setValue(panel.default_rate_per_minute)
        form.addRow("Target", self.target_input)
        form.addRow("Rate", self.rate_input)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("Set")
        self.hold_button = QPushButton("Hold Current")
        close_button = QPushButton("Close")
        self.apply_button.clicked.connect(self._emit_set)
        self.hold_button.clicked.connect(
            lambda: self.holdRequested.emit(
                config.id,
                panel.control_id,
            )
        )
        close_button.clicked.connect(self.hide)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.hold_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self._update_control_state()
        fit_initial_window_width(self)

    def _emit_set(self) -> None:
        """只发出结构化请求；实际校验与 I/O 在后台运行时完成。"""

        self.setRequested.emit(
            self.config.id,
            self.panel.control_id,
            self.target_input.value(),
            self.rate_input.value(),
        )

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        """刷新连接状态和读数；用户正在编辑目标时不覆盖输入。"""

        self._connected = snapshot.connected
        self._update_control_state()
        if not snapshot.connected:
            return
        state = snapshot.controls[self.panel.id]
        current = state.current
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            value = fixed_number(current, self._precision)
            self.current_label.setText(
                f"Current: {value} {self.reading.unit}"
            )
        if state.target is not None and not self.target_input.hasFocus():
            self.target_input.setValue(state.target)

    def set_runtime_editable(self, editable: bool) -> None:
        """设置运行时是否允许手动控制，例如 SEQ 运行期间为假。"""

        self._runtime_editable = editable
        self._update_control_state()

    def _update_control_state(self) -> None:
        enabled = self._runtime_editable and self._connected
        self.apply_button.setEnabled(enabled)
        self.hold_button.setEnabled(enabled)


class AlertDialog(QDialog):
    """非模态 Warning/Error 详情窗口；由事件键保证同一故障只保留一个。"""

    def __init__(self, event: LabEvent, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.event_key = event.key
        is_error = event.severity is Severity.ERROR
        self.setWindowTitle("Error" if is_error else "Warning")
        self.setModal(False)
        layout = QVBoxLayout(self)
        title = QLabel("Operation Stopped" if is_error else "Operation Continues")
        title.setStyleSheet(
            f"font-size: {scaled_text(18)}px; font-weight: 600; "
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
        fit_initial_window_width(self)
