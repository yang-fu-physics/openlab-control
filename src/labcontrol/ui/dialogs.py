"""SEQ 参数、手动控制和事件提示对话框。

``CommandDialog`` 与 ``ManualControlDialog`` 都直接读取同一个 ``InstrumentConfig`` 的目标上下限
和最大速率；磁场切换 T/Oe 时先转换当前值再重建范围。UI 限制用于提前阻止误输入，但不是
最终安全边界，所有写入仍由 ``InstrumentManager.validate_target`` 在后台再次校验。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
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
from ..formatting import control_decimals, field_decimals, fixed_number, require_precision
from ..models import InstrumentKind, InstrumentSnapshot, LabEvent, Severity
from ..module_commands import (
    MAX_MODULE_SCAN_POINTS,
    ModuleCommandSpec,
    format_si_quantity,
    format_si_with_unit,
    linear_points_from_step,
    linear_step_from_points,
    log_points_from_per_decade,
    log_points_per_decade,
    parse_si_quantity,
    resolve_module_point_bounds,
)
from ..sequence.model import Command, CommandSpec, CommandType, command_decimals
from ..sequence.parser import format_temperature_points, parse_temperature_points
from ..units import UnitConversionError, convert_value
from .scaling import scaled, scaled_text
from .window_sizing import fit_initial_window_width
from .widgets import PrecisionDoubleSpinBox


TEMPERATURE_COMMANDS = {CommandType.SET_TEMPERATURE, CommandType.SCAN_TEMPERATURE}
FIELD_COMMANDS = {CommandType.SET_FIELD, CommandType.SCAN_FIELD}


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
        module_settings: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.command = command
        self.spec = spec
        self._instrument_configs = {item.id: item for item in instrument_configs}
        self._data_directory = Path(data_directory or Path.cwd()).resolve()
        self._module_settings = dict(module_settings) if module_settings is not None else None
        self._module_bounds: tuple[float | None, float | None, str] = (None, None, "")
        self._is_module_scan_modes = isinstance(spec, ModuleCommandSpec) and bool(spec.scan_modes)
        self._has_step_sync = (
            self._is_module_scan_modes
            or command.type in {CommandType.SCAN_TEMPERATURE, CommandType.SCAN_FIELD}
        )
        self._syncing_step = False
        self._step_edited = False
        self._bounds_edited = False
        self.step_input: QLineEdit | None = None
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
        selected = self._selected_control() if command.type in TEMPERATURE_COMMANDS else None
        panel = selected[1] if selected is not None else None
        if self._is_module_scan_modes:
            assert isinstance(spec, ModuleCommandSpec)
            self._build_module_scan_controls(spec)
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
                widget = PrecisionDoubleSpinBox()
                decimals = (
                    field.decimals
                    if field.decimals is not None
                    else command_decimals(command, field.name)
                )
                if panel is not None:
                    if field.name == "rate" and panel.rate_decimals is not None:
                        decimals = panel.rate_decimals
                    elif field.name in {"target", "start", "stop"} and panel.setpoint_decimals is not None:
                        decimals = panel.setpoint_decimals + (panel.fine_setpoint_below is not None)
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
            if (
                command.type in {CommandType.SCAN_TEMPERATURE, CommandType.SCAN_FIELD}
                and field.name == "steps"
            ):
                self.step_input = QLineEdit()
                self.form.addRow("Step", self.step_input)
        self._field_unit = ""
        if command.type in FIELD_COMMANDS:
            unit_input = self.inputs.get("unit")
            if isinstance(unit_input, QComboBox):
                self._field_unit = unit_input.currentText()
                unit_input.currentTextChanged.connect(self._change_field_unit)
        if command.type is CommandType.SCAN_TEMPERATURE or self._is_module_scan_modes:
            point_mode = self.inputs.get("point_mode")
            if isinstance(point_mode, QComboBox):
                point_mode.currentTextChanged.connect(self._update_scan_point_mode)
                self._update_scan_point_mode(point_mode.currentText())
        layout.addLayout(self.form)
        if command.type in FIELD_COMMANDS | TEMPERATURE_COMMANDS:
            self.limit_label = QLabel()
            self.limit_label.setObjectName("configuredLimits")
            self.limit_label.setWordWrap(True)
            self.limit_label.setStyleSheet("color: #59636e;")
            layout.addWidget(self.limit_label)
            self._apply_instrument_limits()
        elif command.type is CommandType.WAIT:
            hint = QLabel(
                "Waits until all checked conditions are simultaneously stable, "
                "then waits Delay (s)."
            )
            hint.setObjectName("mutedLabel")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #59636e;")
            layout.addWidget(hint)
        elif isinstance(spec, ModuleCommandSpec) and spec.point_limits is not None:
            self._module_bounds = resolve_module_point_bounds(spec, self._module_settings)
            if self._module_bounds[2]:
                self.limit_label = QLabel(self._module_bounds[2])
                self.limit_label.setObjectName("configuredLimits")
                self.limit_label.setWordWrap(True)
                self.limit_label.setStyleSheet("color: #59636e;")
                layout.addWidget(self.limit_label)
        if self._has_step_sync:
            self._connect_step_sync()
            self._refresh_step_from_points()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if command.type is CommandType.SET_DATAFILE:
            # 路径通常较长，仅扩大首次宽度，仍允许用户自由缩放。
            self.ensurePolished()
            self.resize(self.minimumSizeHint().width() * 2, self.sizeHint().height())
        else:
            fit_initial_window_width(self)

    def _build_module_scan_controls(self, spec: ModuleCommandSpec) -> None:
        params = self.command.params
        initial_mode = str(params.get("point_mode", spec.scan_modes[0]))
        default_start, default_stop, default_steps = spec.default_scan_bounds()
        raw_points = params.get(spec.points_field)
        if isinstance(raw_points, list) and raw_points:
            default_start = str(raw_points[0])
            default_stop = str(raw_points[-1])
            default_steps = max(1, min(MAX_MODULE_SCAN_POINTS, len(raw_points)))
        start_text = str(params.get("start", default_start))
        stop_text = str(params.get("stop", default_stop))
        steps_val = params.get("steps", default_steps)
        try:
            steps_int = max(1, min(MAX_MODULE_SCAN_POINTS, int(steps_val)))
        except (TypeError, ValueError):
            steps_int = default_steps

        mode_box = QComboBox()
        mode_box.addItems(spec.scan_modes)
        idx = mode_box.findText(initial_mode, Qt.MatchFlag.MatchFixedString)
        mode_box.setCurrentIndex(max(0, idx))
        self.inputs["point_mode"] = mode_box
        self.form.addRow("Point definition", mode_box)

        unit_suffix = f" ({spec.point_unit})" if spec.point_unit else ""
        start_input = QLineEdit(start_text)
        stop_input = QLineEdit(stop_text)
        steps_input = QSpinBox()
        steps_input.setRange(1, MAX_MODULE_SCAN_POINTS)
        steps_input.setValue(steps_int)
        self.step_input = QLineEdit()

        self.inputs["start"] = start_input
        self.inputs["stop"] = stop_input
        self.inputs["steps"] = steps_input
        self.form.addRow(f"Start{unit_suffix}", start_input)
        self.form.addRow(f"Stop{unit_suffix}", stop_input)
        self.form.addRow("Points", steps_input)
        self.form.addRow(f"Step{unit_suffix}", self.step_input)

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
                options=(
                    QFileDialog.Option.DontConfirmOverwrite
                    if mode == "open|create"
                    else QFileDialog.Option(0)
                ),
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

    def _set_widget_visible(self, widget: QWidget | None, visible: bool) -> None:
        if widget is None:
            return
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    def _set_field_visible(self, field_name: str, visible: bool) -> None:
        self._set_widget_visible(self.inputs.get(field_name), visible)

    def _active_scan_mode(self) -> str:
        point_mode = self.inputs.get("point_mode")
        if isinstance(point_mode, QComboBox):
            return point_mode.currentText().strip().casefold()
        return "linear"

    def _update_scan_point_mode(self, point_mode: str) -> None:
        """在 Linear、Log 与显式 List 扫描输入间切换。"""

        is_list = point_mode.casefold() == "list"
        for name in ("start", "stop", "steps"):
            self._set_field_visible(name, not is_list)
        self._set_widget_visible(self.step_input, not is_list)
        points_field = (
            self.spec.points_field
            if isinstance(self.spec, ModuleCommandSpec) and self.spec.points_field
            else "points"
        )
        self._set_field_visible(points_field, is_list)
        if self._has_step_sync:
            self._refresh_step_from_points()

    def _connect_step_sync(self) -> None:
        start_widget = self.inputs.get("start")
        stop_widget = self.inputs.get("stop")
        steps_widget = self.inputs.get("steps")
        for widget in (start_widget, stop_widget):
            if isinstance(widget, QDoubleSpinBox):
                widget.valueChanged.connect(self._on_bounds_changed)
                widget.editingFinished.connect(self._refresh_points_from_step)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._on_bounds_changed)
                widget.editingFinished.connect(self._refresh_points_from_step)
        if isinstance(steps_widget, QSpinBox):
            steps_widget.valueChanged.connect(self._refresh_step_from_points)
        if self.step_input is not None:
            self.step_input.textChanged.connect(self._on_step_text_changed)
            self.step_input.editingFinished.connect(self._on_step_editing_finished)

    def _on_bounds_changed(self, *_args: object) -> None:
        if not self._syncing_step:
            self._bounds_edited = True

    def _refresh_points_from_step(self) -> None:
        """上下限提交后优先沿用步长，取最近整数点数并显示实际步长。"""

        if self._syncing_step or self.step_input is None:
            return
        self._bounds_edited = False
        if not self.step_input.text().strip() and not self._step_edited:
            self._refresh_step_from_points()
        else:
            self._apply_step_input()

    def _on_step_text_changed(self, _text: str) -> None:
        if not self._syncing_step:
            self._step_edited = True

    def _on_step_editing_finished(self) -> None:
        if self._step_edited:
            self._apply_step_input()

    def _step_label_text(self, mode: str) -> str:
        if mode == "log":
            return "Points / decade"
        if isinstance(self.spec, ModuleCommandSpec):
            return f"Step ({self.spec.point_unit})" if self.spec.point_unit else "Step"
        unit = self._command_unit()
        return f"Step ({unit})" if unit else "Step"

    def _read_scan_endpoints(self) -> tuple[Decimal, Decimal, QSpinBox] | None:
        start_widget = self.inputs.get("start")
        stop_widget = self.inputs.get("stop")
        steps_widget = self.inputs.get("steps")
        if not isinstance(steps_widget, QSpinBox):
            return None
        if isinstance(start_widget, QDoubleSpinBox) and isinstance(stop_widget, QDoubleSpinBox):
            start_widget.interpretText()
            stop_widget.interpretText()
            return Decimal(str(start_widget.value())), Decimal(str(stop_widget.value())), steps_widget
        if isinstance(start_widget, QLineEdit) and isinstance(stop_widget, QLineEdit):
            unit = self.spec.point_unit if isinstance(self.spec, ModuleCommandSpec) else ""
            try:
                start_dec = parse_si_quantity(start_widget.text(), unit, "Start")
                stop_dec = parse_si_quantity(stop_widget.text(), unit, "Stop")
            except ValueError:
                return None
            return start_dec, stop_dec, steps_widget
        return None

    def _format_step_value(self, value: Decimal | None) -> str:
        if value is None:
            return ""
        if isinstance(self.spec, ModuleCommandSpec):
            return format_si_quantity(value, sig_figs=9)
        return f"{float(value):.6g}"

    def _refresh_step_from_points(self, *_args: object) -> None:
        if self._syncing_step or self.step_input is None:
            return
        mode = self._active_scan_mode()
        label = self.form.labelForField(self.step_input)
        if isinstance(label, QLabel):
            label.setText(self._step_label_text(mode))
        if mode == "list":
            self._step_edited = False
            self.step_input.setToolTip("")
            return
        endpoints = self._read_scan_endpoints()
        text = ""
        if endpoints is not None:
            start_dec, stop_dec, steps_widget = endpoints
            steps = steps_widget.value()
            if mode == "log":
                ppd = log_points_per_decade(start_dec, stop_dec, steps)
                text = "" if ppd is None else f"{ppd:.6g}"
            else:
                text = self._format_step_value(
                    linear_step_from_points(start_dec, stop_dec, steps)
                )
        self._syncing_step = True
        try:
            self.step_input.setText(text)
            self.step_input.setToolTip("")
            self._step_edited = False
            self._bounds_edited = False
        finally:
            self._syncing_step = False

    def _apply_step_input(self) -> str | None:
        """把用户输入的步长/每十倍频点数取整到整数点数，并回写同步后的实际步长。

        非法输入不会被静默恢复为旧步长，而是保留原文并在确认时拦截提示。
        """

        if self.step_input is None or self.step_input.isHidden():
            self._step_edited = False
            return None
        mode = self._active_scan_mode()
        if mode == "list":
            self._step_edited = False
            return None
        raw = self.step_input.text().strip()
        endpoints = self._read_scan_endpoints()
        if endpoints is None:
            error = "Enter valid Start and Stop before setting Step"
            self.step_input.setToolTip(error)
            return error
        start_dec, stop_dec, steps_widget = endpoints
        if not raw:
            if steps_widget.value() == 1:
                self.step_input.setToolTip("")
                self._step_edited = False
                return None
            error = (
                "Points per decade must be a positive number"
                if mode == "log"
                else "Step must be a non-zero finite number"
            )
            self.step_input.setToolTip(error)
            return error
        try:
            if mode == "log":
                per_decade = float(raw)
                new_steps, actual_ppd = log_points_from_per_decade(
                    start_dec,
                    stop_dec,
                    per_decade,
                    maximum=steps_widget.maximum(),
                )
                formatted = f"{actual_ppd:.6g}" if new_steps > 1 else ""
            else:
                if isinstance(self.spec, ModuleCommandSpec):
                    entered_step = parse_si_quantity(raw, self.spec.point_unit, "Step")
                else:
                    entered_step = Decimal(raw)
                    if not entered_step.is_finite():
                        raise ValueError("Step must be a non-zero finite number")
                new_steps, actual_step = linear_points_from_step(
                    start_dec,
                    stop_dec,
                    entered_step,
                    maximum=steps_widget.maximum(),
                )
                formatted = self._format_step_value(actual_step if new_steps > 1 else None)
        except (InvalidOperation, ValueError) as exc:
            error = str(exc) or "Invalid step value"
            self.step_input.setToolTip(error)
            return error

        self._syncing_step = True
        try:
            steps_widget.setValue(new_steps)
            self.step_input.setText(raw if new_steps == 1 else formatted)
            self.step_input.setToolTip("")
            self._step_edited = False
        finally:
            self._syncing_step = False
        return None

    def _validate_module_quantity(
        self,
        value: object,
        label: str,
    ) -> Decimal:
        assert isinstance(self.spec, ModuleCommandSpec)
        unit = self.spec.point_unit
        dec = parse_si_quantity(value, unit, label)
        numeric = float(dec)
        if self.spec.point_decimals is not None:
            require_precision(numeric, self.spec.point_decimals, label)
        low, high, _ = self._module_bounds
        if low is not None and numeric < low:
            raise ValueError(
                f"{label} ({format_si_with_unit(dec, unit)}) is below the configured limit "
                f"{format_si_with_unit(low, unit)}."
            )
        if high is not None and numeric > high:
            raise ValueError(
                f"{label} ({format_si_with_unit(dec, unit)}) exceeds the configured limit "
                f"{format_si_with_unit(high, unit)}."
            )
        return dec

    def accept(self) -> None:
        """关闭前同步步长并验证列表语法、Log 约束及仪表/模块范围。"""

        if self._has_step_sync and self._bounds_edited and not self._step_edited:
            self._refresh_points_from_step()
        if self._has_step_sync and self._step_edited:
            step_error = self._apply_step_input()
            if step_error is not None:
                QMessageBox.warning(self, "Invalid Step", step_error)
                if self.step_input is not None:
                    self.step_input.setFocus()
                    self.step_input.selectAll()
                return

        for field in self.spec.fields:
            if field.field_type != "list":
                continue
            list_input = self.inputs.get(field.name)
            if not isinstance(list_input, QLineEdit) or list_input.isHidden():
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

        if self._is_module_scan_modes:
            assert isinstance(self.spec, ModuleCommandSpec)
            mode = self._active_scan_mode()
            if mode == "list":
                points_input = self.inputs.get(self.spec.points_field)
                if isinstance(points_input, QLineEdit):
                    try:
                        items = json.loads(points_input.text())
                        if not items:
                            raise ValueError(f"{self.spec.points_field} must contain at least one point")
                        if self.spec.point_unit or self.spec.point_limits is not None:
                            for index, item in enumerate(items, start=1):
                                self._validate_module_quantity(item, f"Point {index}")
                    except ValueError as exc:
                        QMessageBox.warning(self, "Invalid Scan List", str(exc))
                        points_input.setFocus()
                        points_input.selectAll()
                        return
            else:
                start_input = self.inputs.get("start")
                stop_input = self.inputs.get("stop")
                if isinstance(start_input, QLineEdit) and isinstance(stop_input, QLineEdit):
                    try:
                        start_dec = self._validate_module_quantity(start_input.text(), "Start")
                        stop_dec = self._validate_module_quantity(stop_input.text(), "Stop")
                        if mode == "log" and (
                            start_dec == 0 or stop_dec == 0 or (start_dec > 0) != (stop_dec > 0)
                        ):
                            raise ValueError("Log scan requires non-zero Start and Stop with the same sign.")
                    except ValueError as exc:
                        QMessageBox.warning(self, "Invalid Module Scan", str(exc))
                        start_input.setFocus()
                        return
        elif isinstance(self.spec, ModuleCommandSpec) and self.spec.point_limits is not None:
            target_widget = self.inputs.get(self.spec.point_limits.field)
            if isinstance(target_widget, QLineEdit):
                try:
                    self._validate_module_quantity(
                        target_widget.text(),
                        self.spec.point_limits.field.replace("_", " ").title(),
                    )
                except ValueError as exc:
                    QMessageBox.warning(self, "Invalid Module Command", str(exc))
                    target_widget.setFocus()
                    target_widget.selectAll()
                    return

        if self.command.type is CommandType.SCAN_TEMPERATURE:
            point_mode = self.inputs.get("point_mode")
            points_input = self.inputs.get("points")
            if isinstance(point_mode, QComboBox):
                mode = point_mode.currentText().casefold()
                if mode == "list" and isinstance(points_input, QLineEdit):
                    try:
                        points = parse_temperature_points(points_input.text())
                        self._validate_temperature_points(points)
                        points_input.setText(format_temperature_points(points_input.text()))
                    except ValueError as exc:
                        QMessageBox.warning(self, "Invalid Temperature List", str(exc))
                        points_input.setFocus()
                        points_input.selectAll()
                        return
                elif mode == "log":
                    start_w = self.inputs.get("start")
                    stop_w = self.inputs.get("stop")
                    if (
                        isinstance(start_w, QDoubleSpinBox)
                        and isinstance(stop_w, QDoubleSpinBox)
                        and (start_w.value() <= 0.0 or stop_w.value() <= 0.0)
                    ):
                        QMessageBox.warning(
                            self,
                            "Invalid Temperature Scan",
                            "Log scan requires positive Start and Stop temperatures.",
                        )
                        start_w.setFocus()
                        return
        if self.command.type in TEMPERATURE_COMMANDS:
            selected = self._selected_control()
            if selected is not None:
                for name in ("target", "start", "stop"):
                    widget = self.inputs.get(name)
                    if isinstance(widget, QDoubleSpinBox) and not widget.isHidden():
                        precision = selected[1].target_precision(widget.value())
                        if precision is not None:
                            try:
                                require_precision(widget.value(), precision, name.capitalize())
                            except ValueError as exc:
                                QMessageBox.warning(self, "Invalid Temperature", str(exc))
                                widget.setFocus()
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
            precision = panel.target_precision(point)
            if precision is not None:
                require_precision(point, precision, f"Temperature point {index} (K)")
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
        if self._has_step_sync:
            self._refresh_step_from_points()

    def values(self) -> dict[str, Any]:
        """提取控件值供命令模型更新；本方法本身不执行仪表操作。"""

        result: dict[str, Any] = {}
        if self._is_module_scan_modes:
            assert isinstance(self.spec, ModuleCommandSpec)
            mode = self.inputs["point_mode"].currentText()  # type: ignore[union-attr]
            result["point_mode"] = mode
            if mode.casefold() == "list":
                points_widget = self.inputs[self.spec.points_field]
                assert isinstance(points_widget, QLineEdit)
                result[self.spec.points_field] = json.loads(points_widget.text())
            else:
                start_widget = self.inputs["start"]
                stop_widget = self.inputs["stop"]
                steps_widget = self.inputs["steps"]
                assert isinstance(start_widget, QLineEdit)
                assert isinstance(stop_widget, QLineEdit)
                assert isinstance(steps_widget, QSpinBox)
                result["start"] = start_widget.text().strip()
                result["stop"] = stop_widget.text().strip()
                result["steps"] = steps_widget.value()
            for field in self.spec.fields:
                if field.name == self.spec.points_field:
                    continue
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
        self._target_initialized = False
        self._target_dirty = False
        self._last_target: float | None = None
        self._precision = (
            self.reading.decimals
            if self.reading.decimals is not None
            else control_decimals(self.kind, self.reading.unit)
        )
        form = QFormLayout()
        input_precision = control_decimals(self.kind, self.reading.unit)
        self.target_input = QDoubleSpinBox()
        target_precision = (
            panel.setpoint_decimals + (panel.fine_setpoint_below is not None)
            if panel.setpoint_decimals is not None else input_precision
        )
        rate_precision = panel.rate_decimals if panel.rate_decimals is not None else input_precision
        self.target_input.setDecimals(target_precision)
        self.target_input.setRange(panel.min_value, panel.max_value)
        self.target_input.setSuffix(f" {self.reading.unit}")
        self.target_input.setValue(config.initial_value)
        self.target_input.valueChanged.connect(self._mark_target_dirty)
        self.rate_input = QDoubleSpinBox()
        self.rate_input.setDecimals(rate_precision)
        self.rate_input.setRange(10 ** -rate_precision, panel.max_rate_per_minute)
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

    def _mark_target_dirty(self) -> None:
        self._target_dirty = True

    def _set_target_display(self, value: float) -> None:
        self.target_input.blockSignals(True)
        try:
            self.target_input.setValue(value)
        finally:
            self.target_input.blockSignals(False)
        self._target_initialized = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if (
            not event.spontaneous()
            and self._last_target is not None
            and not self._target_dirty
            and not self.target_input.hasFocus()
        ):
            self._set_target_display(self._last_target)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if not event.spontaneous():
            self._target_initialized = False
            self._target_dirty = False

    def _emit_set(self) -> None:
        """只发出结构化请求；实际校验与 I/O 在后台运行时完成。"""

        precision = self.panel.target_precision(self.target_input.value())
        if precision is not None:
            try:
                require_precision(self.target_input.value(), precision, "Target")
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid Target", str(exc))
                self.target_input.setFocus()
                return
        self._target_dirty = False
        self.setRequested.emit(
            self.config.id,
            self.panel.control_id,
            self.target_input.value(),
            self.rate_input.value(),
        )

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        """刷新连接状态和读数；绝不在轮询中覆盖用户已编辑的目标输入。"""

        self._connected = snapshot.connected
        self._update_control_state()
        if not snapshot.connected:
            self.current_label.setText("Current: — (Disconnected)")
            return
        state = snapshot.controls.get(self.panel.id)
        if state is None:
            self.current_label.setText("Current: —")
            return
        current = state.current
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            value = fixed_number(current, self._precision)
            self.current_label.setText(
                f"Current: {value} {self.reading.unit}"
            )
        else:
            self.current_label.setText("Current: —")
        if state.target is not None:
            self._last_target = state.target
        if (
            state.target is not None
            and not self._target_initialized
            and not self._target_dirty
            and not self.target_input.hasFocus()
        ):
            self._set_target_display(state.target)

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
