"""由 DLL 自描述数据生成的手动函数窗口。"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..dll_functions import DLLFunctionSpec, validate_dll_parameters
from ..formatting import fixed_number
from .window_sizing import fit_initial_window_width


class DLLFunctionDialog(QDialog):
    """显示一个 DLL 函数的输入、结果和执行状态。"""

    runRequested = Signal(dict)

    def __init__(
        self,
        owner_kind: str,
        owner_id: str,
        owner_name: str,
        spec: DLLFunctionSpec,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.owner_kind = owner_kind
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.spec = spec
        self.inputs: dict[str, QWidget] = {}
        self.outputs: dict[str, QLineEdit] = {}
        self._runtime_editable = True
        self._busy = False

        self.setWindowTitle(f"{owner_name} - {spec.label}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        if spec.description:
            description = QLabel(spec.description)
            description.setWordWrap(True)
            description.setObjectName("mutedLabel")
            layout.addWidget(description)

        input_form = QFormLayout()
        input_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        for field in spec.inputs:
            if field.field_type == "choice":
                widget = QComboBox()
                widget.addItems(field.choices)
                widget.setCurrentText(str(field.default))
            elif field.field_type == "bool":
                widget = QCheckBox()
                widget.setChecked(bool(field.default))
            elif field.field_type == "list":
                widget = QLineEdit(
                    json.dumps(
                        field.default,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
            else:
                # 数值也用文本输入，保留 DLL 默认值和科学计数法；不能由 spin box
                # 的默认小数位或 32 位整数范围悄悄改写即将发给仪表的参数。
                widget = QLineEdit(str(field.default))
            self.inputs[field.name] = widget
            label = f"{field.label} ({field.unit})" if field.unit else field.label
            input_form.addRow(label, widget)
        layout.addLayout(input_form)

        if spec.outputs:
            output_form = QFormLayout()
            output_form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
            )
            for output in spec.outputs:
                widget = QLineEdit()
                widget.setReadOnly(True)
                widget.setPlaceholderText("—")
                self.outputs[output.name] = widget
                label = output.label
                if output.unit:
                    label = f"{label} ({output.unit})"
                output_form.addRow(label, widget)
            layout.addLayout(output_form)

        self.status_label = QLabel("Ready")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("mutedLabel")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run")
        close_button = QPushButton("Close")
        self.run_button.clicked.connect(self._request_run)
        close_button.clicked.connect(self.close)
        buttons.addWidget(self.run_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        fit_initial_window_width(self)

    def parameters(self) -> dict[str, Any] | None:
        """读取并验证用户输入；列表字段在 UI 边界解析 JSON。"""

        result: dict[str, Any] = {}
        for field in self.spec.inputs:
            widget = self.inputs[field.name]
            if isinstance(widget, QComboBox):
                result[field.name] = widget.currentText()
            elif isinstance(widget, QCheckBox):
                result[field.name] = widget.isChecked()
            elif isinstance(widget, QLineEdit):
                try:
                    if field.field_type == "list":
                        value = json.loads(widget.text())
                    elif field.field_type == "int":
                        value = int(widget.text())
                    elif field.field_type == "float":
                        value = float(widget.text())
                    else:
                        value = widget.text()
                except ValueError as exc:
                    QMessageBox.warning(
                        self,
                        "Invalid Input",
                        f"{field.label}: {exc}",
                    )
                    widget.setFocus()
                    widget.selectAll()
                    return None
                result[field.name] = value
        issues = validate_dll_parameters(self.spec, result)
        if issues:
            QMessageBox.warning(self, "Invalid Input", "\n".join(issues))
            return None
        return result

    def _request_run(self) -> None:
        parameters = self.parameters()
        if parameters is not None:
            self.runRequested.emit(parameters)

    def set_runtime_editable(self, editable: bool) -> None:
        self._runtime_editable = editable
        self.run_button.setEnabled(editable and not self._busy)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.run_button.setEnabled(self._runtime_editable and not busy)
        if busy:
            for output in self.outputs.values():
                output.clear()
            self.status_label.setText("Running…")

    def show_result(self, result: dict[str, Any]) -> None:
        for output in self.spec.outputs:
            value = result[output.name]
            if output.value_type == "float" and output.decimals is not None:
                text = fixed_number(value, output.decimals)
            elif output.value_type == "bool":
                text = "True" if value else "False"
            else:
                text = str(value)
            self.outputs[output.name].setText(text)
        self.set_busy(False)
        self.status_label.setText("Completed")

    def show_error(self, message: str) -> None:
        self.set_busy(False)
        for output in self.outputs.values():
            output.clear()
        self.status_label.setText(f"Failed: {message}")


__all__ = ["DLLFunctionDialog"]
