"""Measurement Module 列表、独立窗口和 Settings/Status 前端协调。

所有模块启动为 Disabled。勾选 Enable 才初始化 worker 并打开独立窗口；关闭或最小化
只隐藏窗口。保存或随 SEQ 导入的设置只填入 Settings 页，必须由用户点击 Apply Settings 才送到
后端。Disable 与应用退出由主窗口统一回收窗口和进程。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
import re
from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..package_support.loading import load_source_object
from ..measurement.frontend_api import ModuleUIAPI
from ..measurement.manifest import ModuleDescriptor
from .scaling import scaled
from .window_sizing import fit_initial_window_width


MODULE_WINDOW_MIN_WIDTH = 360
MODULE_WINDOW_MIN_HEIGHT = 260


class _GenericFrontend(QWidget):
    """没有自定义界面时使用的核心占位页；不是 Measurement Module API。"""

    def __init__(self, _api: ModuleUIAPI) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        label = QLabel("This module has no settings.")
        label.setObjectName("mutedLabel")
        layout.addWidget(label)
        layout.addStretch(1)
        self.status_widget = QLabel("No status reported")
        self.status_widget.setWordWrap(True)

    def load(self, _settings: Mapping[str, Any]) -> None:
        return

    def dump(self) -> dict[str, Any]:
        return {}

    def show_status(self, status: Mapping[str, Any]) -> None:
        self.status_widget.setText(
            json.dumps(dict(status), ensure_ascii=False, indent=2)
            if status
            else "No status reported"
        )


class ModuleWindow(QDialog):
    applyRequested = Signal(str)
    actionRequested = Signal(str, str, dict)
    statusRefreshRequested = Signal(str)

    def __init__(
        self,
        descriptor: ModuleDescriptor,
        parent: QWidget,
        *,
        resources: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.descriptor = descriptor
        self._allow_close = False
        self._dirty = False
        self.ui_api = ModuleUIAPI(self, resources=resources)
        self._has_frontend = (descriptor.path / "frontend.py").is_file()
        frontend_class: type[Any] = _GenericFrontend
        if self._has_frontend:
            loaded = load_source_object(
                descriptor.path,
                "frontend:Frontend",
                f"frontend_{descriptor.id}",
            )
            if not isinstance(loaded, type):
                raise TypeError("frontend:Frontend is not a class")
            frontend_class = loaded
        self.frontend: Any = frontend_class(self.ui_api)
        if not isinstance(self.frontend, QWidget):
            raise TypeError("Frontend must be a QWidget")
        for method_name in ("load", "dump"):
            if not callable(getattr(self.frontend, method_name, None)):
                raise TypeError(
                    f"Frontend must implement {method_name}()"
                )
        self._baseline_settings: dict[str, Any] = {}

        flags = self.windowFlags()
        flags |= Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowMinimizeButtonHint
        self.setWindowFlags(flags)
        self.setWindowTitle(f"{descriptor.name} {descriptor.version}")

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.state_label = QLabel("Enabled")
        self.state_label.setObjectName("moduleState")
        self.message_label = QLabel("")
        self.message_label.setObjectName("mutedLabel")
        header.addWidget(self.state_label)
        header.addWidget(self.message_label, 1)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.settings_page = QWidget(self.tabs)
        settings_layout = QVBoxLayout(self.settings_page)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_content = self.frontend
        settings_layout.addWidget(self.settings_content, 1)

        footer = QHBoxLayout()
        self.apply_button = QPushButton("Apply Settings")
        self.apply_button.clicked.connect(lambda: self.applyRequested.emit(descriptor.id))
        self.apply_button.setVisible(self._has_frontend)
        footer.addStretch(1)
        footer.addWidget(self.apply_button)
        settings_layout.addLayout(footer)

        self.status_page = getattr(self.frontend, "status_widget", None)
        if self.status_page is None:
            self.status_page = QLabel("No status reported", self.tabs)
            self.status_page.setWordWrap(True)
        if not isinstance(self.status_page, QWidget):
            raise TypeError("Frontend.status_widget must be a QWidget")
        self.tabs.addTab(self.settings_page, "Settings")
        self.tabs.addTab(self.status_page, "Status")
        self.tabs.setCurrentIndex(0)
        layout.addWidget(self.tabs, 1)

        layout.activate()
        minimum = self.minimumSizeHint().expandedTo(
            QSize(scaled(MODULE_WINDOW_MIN_WIDTH), scaled(MODULE_WINDOW_MIN_HEIGHT))
        )
        self.setMinimumSize(minimum)
        fit_initial_window_width(
            self,
            preferred_height=max(
                minimum.height(),
                self.sizeHint().height(),
            ),
        )

        self.ui_api.actionRequested.connect(
            lambda name, payload: self.actionRequested.emit(
                descriptor.id, name, payload
            )
        )
        self.ui_api.refreshRequested.connect(
            lambda: self.statusRefreshRequested.emit(descriptor.id)
        )

    def load_settings(
        self,
        settings: Mapping[str, Any],
        *,
        mark_unapplied: bool = False,
    ) -> None:
        """只更新 Settings 控件，不把任何值发送给模块 worker。

        普通 Enable 读取模块自己的持久设置时保持干净；从 SEQ 伴随文件切换实验参数时，
        ``mark_unapplied`` 会明确标记为待 Apply，避免窗口仍显示上一组设置已应用。
        """

        self.frontend.load(deepcopy(dict(settings)))
        self._baseline_settings = self.settings()
        self._dirty = mark_unapplied
        if mark_unapplied:
            self.message_label.setText(
                "SEQ settings imported — not applied"
            )

    def settings(self) -> dict[str, Any]:
        values = self.frontend.dump()
        if not isinstance(values, Mapping):
            raise TypeError("Frontend.dump() must return a mapping")
        return deepcopy(dict(values))

    def edit_sequence_command(
        self,
        command_id: str,
        parameters: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """委托可选 Frontend 打开模块自定义参数窗口。

        ``None`` 表示用户取消；接受时只能返回普通 Mapping。方法运行在 GUI 线程，只能
        编辑参数，不能借此访问 worker、VISA 或温场控制。
        """

        editor = getattr(
            self.frontend,
            "edit_sequence_command",
            None,
        )
        if not callable(editor):
            raise TypeError(
                "Command declares custom_editor but Frontend does not implement "
                "edit_sequence_command(command_id, parameters)"
            )
        result = editor(
            str(command_id),
            deepcopy(dict(parameters)),
        )
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise TypeError(
                "Frontend.edit_sequence_command() must return a mapping or None"
            )
        return deepcopy(dict(result))

    def has_unapplied_edits(self) -> bool:
        return self._dirty or self.settings() != self._baseline_settings

    def mark_applied(self) -> None:
        self._baseline_settings = self.settings()
        self._dirty = False
        self.message_label.setText("Settings applied")

    def update_runtime(
        self,
        state: str,
        status: Mapping[str, Any],
        message: str = "",
    ) -> None:
        self.state_label.setText(state.replace("_", " ").title())
        self.message_label.setText(message)
        update = getattr(self.frontend, "show_status", None)
        if callable(update):
            update(dict(status))
        if message == "Settings applied":
            self.mark_applied()

    def set_sequence_running(self, running: bool) -> None:
        self.settings_page.setEnabled(not running)
        self.apply_button.setEnabled(not running and self._has_frontend)

    def show_in_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def allow_application_close(self) -> None:
        self._allow_close = True

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            self.hide()
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._allow_close:
            event.accept()
        else:
            event.ignore()
            self.hide()


_INSTANCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _suggest_instance_identity(
    template: ModuleDescriptor,
    existing_ids: set[str],
) -> tuple[str, str]:
    suffix = 2
    while f"{template.id}_{suffix}" in existing_ids:
        suffix += 1
    return f"{template.id}_{suffix}", f"{template.name} #{suffix}"


class AddModuleInstanceDialog(QDialog):
    """基于现有测量模块源码创建独立复用实例的参数窗口。"""

    def __init__(
        self,
        templates: Sequence[ModuleDescriptor],
        existing_ids: set[str],
        parent: QWidget | None = None,
        *,
        initial_template_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._templates = tuple(templates)
        self._existing_ids = set(existing_ids)
        self._last_suggested_id = ""
        self._last_suggested_name = ""

        self.setWindowTitle("Add Module Instance")
        self.setModal(True)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Share the selected module's source code with an independent window, "
            "settings, worker process, and DAT columns."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.template_combo = QComboBox()
        for template in self._templates:
            self.template_combo.addItem(f"{template.name} ({template.id})", template.id)
        if initial_template_id:
            index = self.template_combo.findData(initial_template_id)
            if index >= 0:
                self.template_combo.setCurrentIndex(index)
        form.addRow("Source module", self.template_combo)

        self.id_input = QLineEdit()
        self.id_input.setPlaceholderText("e.g. keithley_2400_gate")
        form.addRow("Instance ID", self.id_input)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. Keithley 2400 - Gate")
        form.addRow("Display name", self.name_input)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.template_combo.currentIndexChanged.connect(self._update_suggestions)
        self._update_suggestions()
        fit_initial_window_width(self)

    def _selected_template(self) -> ModuleDescriptor:
        index = max(0, self.template_combo.currentIndex())
        return self._templates[index]

    def _update_suggestions(self, *_args: object) -> None:
        if not self._templates:
            return
        suggested_id, suggested_name = _suggest_instance_identity(
            self._selected_template(),
            self._existing_ids,
        )
        if not self.id_input.text().strip() or self.id_input.text().strip() == self._last_suggested_id:
            self.id_input.setText(suggested_id)
        if not self.name_input.text().strip() or self.name_input.text().strip() == self._last_suggested_name:
            self.name_input.setText(suggested_name)
        self._last_suggested_id = suggested_id
        self._last_suggested_name = suggested_name

    def accept(self) -> None:
        instance_id = self.id_input.text().strip().casefold()
        display_name = self.name_input.text().strip()
        if not _INSTANCE_ID_RE.fullmatch(instance_id):
            QMessageBox.warning(
                self,
                "Invalid Instance ID",
                "Instance ID must start with a lowercase letter and use only lowercase letters, digits, and underscores.",
            )
            self.id_input.setFocus()
            self.id_input.selectAll()
            return
        if instance_id in self._existing_ids:
            QMessageBox.warning(
                self,
                "Duplicate Instance ID",
                f"Module ID {instance_id!r} already exists.",
            )
            self.id_input.setFocus()
            self.id_input.selectAll()
            return
        if not display_name or len(display_name) > 100 or any(ch in display_name for ch in "\r\n\0"):
            QMessageBox.warning(
                self,
                "Invalid Display Name",
                "Enter a single-line display name (1–100 characters).",
            )
            self.name_input.setFocus()
            self.name_input.selectAll()
            return
        self.id_input.setText(instance_id)
        self.name_input.setText(display_name)
        super().accept()

    def values(self) -> tuple[str, str, str]:
        return (
            self._selected_template().id,
            self.id_input.text().strip().casefold(),
            self.name_input.text().strip(),
        )


class ModuleManagerDialog(QDialog):
    enableRequested = Signal(str, bool)
    refreshRequested = Signal()
    openRequested = Signal(str)
    addInstanceRequested = Signal(str, str, str)
    removeInstanceRequested = Signal(str)

    def __init__(
        self,
        descriptors: tuple[ModuleDescriptor, ...],
        parent: QWidget,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Measurement Modules")
        self.setModal(False)
        self.setMinimumHeight(scaled(390))
        self.descriptors: tuple[ModuleDescriptor, ...] = ()
        self._rows: dict[str, int] = {}
        self._checkboxes: dict[str, QCheckBox] = {}
        self._states: dict[str, dict[str, Any]] = {}

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Modules start Disabled. Enable initializes the module and opens its window. "
            "Use Add Instance to run multiple instruments from the same module code."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Enabled", "Name", "Version"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        # Name 列按完整内容计算。首次宽度适配器会据此把窗口扩到刚好不需要横向
        # 滚动条；若用户之后主动缩窄，表格仍可正常出现横向滚动条访问完整名称。
        self.table.horizontalHeader().setSectionResizeMode(
            1,
            self.table.horizontalHeader().ResizeMode.ResizeToContents,
        )
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._double_clicked)
        layout.addWidget(self.table, 1)
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("mutedLabel")
        layout.addWidget(self.detail_label)
        buttons = QHBoxLayout()
        self.add_instance_button = QPushButton("Add Instance…")
        self.remove_instance_button = QPushButton("Remove Instance")
        self.refresh_button = QPushButton("Refresh")
        close_button = QPushButton("Close")
        self.add_instance_button.clicked.connect(self._open_add_instance_dialog)
        self.remove_instance_button.clicked.connect(self._request_remove_selected)
        self.refresh_button.clicked.connect(self.refreshRequested)
        close_button.clicked.connect(self.hide)
        buttons.addWidget(self.add_instance_button)
        buttons.addWidget(self.remove_instance_button)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.set_descriptors(descriptors)
        fit_initial_window_width(
            self,
            preferred_height=scaled(390),
        )

    def set_descriptors(
        self,
        descriptors: tuple[ModuleDescriptor, ...],
        *,
        select_id: str | None = None,
    ) -> None:
        previous_selected = select_id or self._selected_id()
        self.descriptors = descriptors
        known_ids = {item.id for item in descriptors}
        self._states = {
            module_id: state
            for module_id, state in self._states.items()
            if module_id in known_ids
        }
        self.table.setRowCount(0)
        self._rows.clear()
        self._checkboxes.clear()
        table_enabled = self.table.isEnabled()
        for descriptor in descriptors:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._rows[descriptor.id] = row
            state = self._states.get(descriptor.id, {})
            enabled = bool(state.get("enabled", False))
            runtime_state = str(state.get("state", "disabled"))
            checkbox = QCheckBox()
            descriptor_detail = descriptor.error
            checkbox.setToolTip(descriptor_detail)
            checkbox.setChecked(enabled)
            checkbox.setEnabled(
                table_enabled
                and descriptor.can_enable
                and runtime_state not in {"initializing", "disabling"}
            )
            checkbox.toggled.connect(
                lambda checked, module_id=descriptor.id: self._toggle(module_id, checked)
            )
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder_layout.addWidget(checkbox)
            self.table.setCellWidget(row, 0, holder)
            name_item = QTableWidgetItem(descriptor.name)
            name_item.setData(Qt.ItemDataRole.UserRole, descriptor.id)
            name_item.setToolTip(descriptor_detail)
            self.table.setItem(row, 1, name_item)
            self.table.setItem(row, 2, QTableWidgetItem(descriptor.version))
            self._checkboxes[descriptor.id] = checkbox
        if previous_selected and previous_selected in self._rows:
            self.table.selectRow(self._rows[previous_selected])
        elif descriptors:
            self.table.selectRow(0)
        self._selection_changed()

    def _template_descriptors(self) -> tuple[ModuleDescriptor, ...]:
        return tuple(
            item for item in self.descriptors if not item.is_instance and item.valid
        )

    def _open_add_instance_dialog(self) -> None:
        templates = self._template_descriptors()
        if not templates:
            return
        selected = next(
            (item for item in self.descriptors if item.id == self._selected_id()),
            None,
        )
        initial_template = None
        if selected is not None:
            initial_template = selected.template_id or (selected.id if selected.valid else None)
        dialog = AddModuleInstanceDialog(
            templates,
            {item.id for item in self.descriptors},
            self,
            initial_template_id=initial_template,
        )
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.addInstanceRequested.emit(*dialog.values())
        finally:
            dialog.deleteLater()

    def _request_remove_selected(self) -> None:
        module_id = self._selected_id()
        if module_id is not None:
            self.removeInstanceRequested.emit(module_id)

    def _update_instance_buttons(self) -> None:
        table_enabled = self.table.isEnabled()
        self.add_instance_button.setEnabled(
            table_enabled and bool(self._template_descriptors())
        )
        module_id = self._selected_id()
        descriptor = next((item for item in self.descriptors if item.id == module_id), None)
        state = self._states.get(descriptor.id, {}) if descriptor is not None else {}
        can_remove = (
            table_enabled
            and descriptor is not None
            and descriptor.is_instance
            and not bool(state.get("enabled", False))
            and str(state.get("state", "disabled")) not in {"initializing", "disabling"}
        )
        self.remove_instance_button.setEnabled(can_remove)

    def _toggle(self, module_id: str, enabled: bool) -> None:
        checkbox = self._checkboxes[module_id]
        checkbox.setEnabled(False)
        self.enableRequested.emit(module_id, enabled)

    def update_state(
        self,
        module_id: str,
        enabled: bool,
        state: str,
        message: str = "",
    ) -> None:
        self._states[module_id] = {
            "enabled": enabled,
            "state": state,
            "message": message,
        }
        checkbox = self._checkboxes.get(module_id)
        descriptor = next((item for item in self.descriptors if item.id == module_id), None)
        if checkbox is not None:
            checkbox.blockSignals(True)
            checkbox.setChecked(enabled)
            checkbox.blockSignals(False)
            checkbox.setEnabled(
                self.table.isEnabled()
                and state not in {"initializing", "disabling"}
                and bool(descriptor and descriptor.can_enable)
            )
        if descriptor is not None and self._selected_id() == module_id:
            status_text = message or state.replace("_", " ").title()
            if descriptor.is_instance:
                status_text = f"Instance of {descriptor.template_id} ({descriptor.id}) — {status_text}"
            self.detail_label.setText(status_text)
        self._update_instance_buttons()

    def set_operations_enabled(self, enabled: bool) -> None:
        self.table.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        for descriptor in self.descriptors:
            state = self._states.get(descriptor.id, {}).get("state", "disabled")
            checkbox = self._checkboxes.get(descriptor.id)
            if checkbox is not None:
                checkbox.setEnabled(
                    enabled
                    and descriptor.can_enable
                    and state not in {"initializing", "disabling"}
                )
        self._update_instance_buttons()

    def runtime_state(self, module_id: str) -> str:
        """返回管理器最后收到的运行状态；尚无消息时按 Disabled 处理。"""

        return str(
            self._states.get(
                module_id,
                {},
            ).get("state", "disabled")
        )

    def _selected_id(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, 1) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _selection_changed(self) -> None:
        module_id = self._selected_id()
        descriptor = next((item for item in self.descriptors if item.id == module_id), None)
        if descriptor is None:
            self.detail_label.clear()
            self._update_instance_buttons()
            return
        state = self._states.get(descriptor.id, {})
        detail = (
            descriptor.error
            or str(state.get("message", ""))
            or "Ready to enable"
        )
        if descriptor.is_instance:
            detail = f"Instance of {descriptor.template_id} ({descriptor.id}) — {detail}"
        self.detail_label.setText(detail)
        self._update_instance_buttons()

    def _double_clicked(self, row: int, column: int) -> None:
        del column
        item = self.table.item(row, 1)
        if item is not None:
            self.openRequested.emit(str(item.data(Qt.ItemDataRole.UserRole)))
