from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..measurement.frontend_api import ModuleFrontend, ModuleFrontendContext
from ..measurement.manifest import ModuleDescriptor, load_source_object
from .scaling import scaled


MODULE_WINDOW_MIN_WIDTH = 360
MODULE_WINDOW_MIN_HEIGHT = 260


class ModuleWindow(QDialog):
    applyRequested = Signal(str)
    manualActionRequested = Signal(str, str, dict)
    statusRefreshRequested = Signal(str)

    def __init__(self, descriptor: ModuleDescriptor, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.descriptor = descriptor
        self._allow_close = False
        self._dirty = False
        self._last_applied: dict[str, Any] | None = None
        self.context = ModuleFrontendContext(self)
        frontend_class = load_source_object(
            descriptor.path, descriptor.frontend, f"frontend_{descriptor.id}"
        )
        if not isinstance(frontend_class, type) or not issubclass(frontend_class, ModuleFrontend):
            raise TypeError(f"{descriptor.frontend} is not a ModuleFrontend")
        self.frontend: ModuleFrontend = frontend_class(self.context)

        flags = self.windowFlags()
        flags &= ~Qt.WindowType.WindowCloseButtonHint
        flags |= Qt.WindowType.WindowMinimizeButtonHint
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
        self.settings_content = self.frontend.create_settings_page(self.settings_page)
        settings_layout.addWidget(self.settings_content, 1)

        footer = QHBoxLayout()
        self.apply_button = QPushButton("Apply Settings")
        self.apply_button.clicked.connect(lambda: self.applyRequested.emit(descriptor.id))
        footer.addStretch(1)
        footer.addWidget(self.apply_button)
        settings_layout.addLayout(footer)

        self.status_page = self.frontend.create_status_page(self.tabs)
        self.tabs.addTab(self.settings_page, "Settings")
        self.tabs.addTab(self.status_page, "Status")
        self.tabs.setCurrentIndex(0)
        layout.addWidget(self.tabs, 1)

        layout.activate()
        minimum = self.minimumSizeHint().expandedTo(
            QSize(scaled(MODULE_WINDOW_MIN_WIDTH), scaled(MODULE_WINDOW_MIN_HEIGHT))
        )
        self.setMinimumSize(minimum)
        self.resize(self.sizeHint().expandedTo(minimum))

        self.frontend.settingsChanged.connect(self._mark_dirty)
        self.context.manualActionRequested.connect(
            lambda name, payload: self.manualActionRequested.emit(
                descriptor.id, name, payload
            )
        )
        self.context.statusRefreshRequested.connect(
            lambda: self.statusRefreshRequested.emit(descriptor.id)
        )

    def load_settings(self, settings: Mapping[str, Any]) -> None:
        self.frontend.load_settings(deepcopy(dict(settings)))
        self._dirty = False

    def settings(self) -> dict[str, Any]:
        return deepcopy(self.frontend.settings())

    def has_unapplied_edits(self) -> bool:
        return self._dirty

    def mark_applied(self) -> None:
        self._last_applied = self.settings()
        self._dirty = False
        self.message_label.setText("Settings applied")

    def _mark_dirty(self) -> None:
        self._dirty = True
        self.message_label.setText("Unapplied changes")

    def update_runtime(
        self,
        state: str,
        status: Mapping[str, Any],
        message: str = "",
    ) -> None:
        self.state_label.setText(state.replace("_", " ").title())
        self.message_label.setText(message)
        self.frontend.update_status(dict(status))
        if message == "Settings applied":
            self.mark_applied()

    def set_sequence_running(self, running: bool) -> None:
        self.settings_page.setEnabled(not running)
        self.apply_button.setEnabled(not running)
        self.frontend.set_sequence_running(running)

    def show_in_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def allow_application_close(self) -> None:
        self._allow_close = True

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._allow_close:
            event.accept()
        else:
            event.ignore()


class ModuleManagerDialog(QDialog):
    enableRequested = Signal(str, bool)
    refreshRequested = Signal()
    installRequested = Signal(str)
    openRequested = Signal(str)

    def __init__(
        self,
        descriptors: tuple[ModuleDescriptor, ...],
        parent: QWidget,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Measurement Modules")
        self.setModal(False)
        self.setMinimumSize(scaled(610), scaled(390))
        self.descriptors: tuple[ModuleDescriptor, ...] = ()
        self._rows: dict[str, int] = {}
        self._checkboxes: dict[str, QCheckBox] = {}
        self._states: dict[str, dict[str, Any]] = {}

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Modules start Disabled. Enable initializes the module and opens its window."
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
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._double_clicked)
        layout.addWidget(self.table, 1)
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("mutedLabel")
        layout.addWidget(self.detail_label)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.install_button = QPushButton("Install Dependencies")
        close_button = QPushButton("Close")
        self.refresh_button.clicked.connect(self.refreshRequested)
        self.install_button.clicked.connect(self._install_selected)
        close_button.clicked.connect(self.hide)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.install_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.set_descriptors(descriptors)

    def set_descriptors(self, descriptors: tuple[ModuleDescriptor, ...]) -> None:
        self.descriptors = descriptors
        self.table.setRowCount(0)
        self._rows.clear()
        self._checkboxes.clear()
        for descriptor in descriptors:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._rows[descriptor.id] = row
            checkbox = QCheckBox()
            checkbox.setToolTip(descriptor.error or descriptor.dependency_error)
            checkbox.setEnabled(descriptor.can_enable)
            checkbox.toggled.connect(
                lambda enabled, module_id=descriptor.id: self._toggle(module_id, enabled)
            )
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder_layout.addWidget(checkbox)
            self.table.setCellWidget(row, 0, holder)
            name_item = QTableWidgetItem(descriptor.name)
            name_item.setData(Qt.ItemDataRole.UserRole, descriptor.id)
            name_item.setToolTip(descriptor.error or descriptor.dependency_error)
            self.table.setItem(row, 1, name_item)
            self.table.setItem(row, 2, QTableWidgetItem(descriptor.version))
            self._checkboxes[descriptor.id] = checkbox
        if descriptors:
            self.table.selectRow(0)
        self._selection_changed()

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
            self.detail_label.setText(message or state.replace("_", " ").title())

    def set_operations_enabled(self, enabled: bool) -> None:
        self.table.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        selected = next(
            (item for item in self.descriptors if item.id == self._selected_id()), None
        )
        self.install_button.setEnabled(enabled and bool(selected and selected.dependencies))
        for descriptor in self.descriptors:
            state = self._states.get(descriptor.id, {}).get("state", "disabled")
            checkbox = self._checkboxes.get(descriptor.id)
            if checkbox is not None:
                checkbox.setEnabled(
                    enabled
                    and descriptor.can_enable
                    and state not in {"initializing", "disabling"}
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
            self.install_button.setEnabled(False)
            return
        state = self._states.get(descriptor.id, {})
        detail = descriptor.error or descriptor.dependency_error or str(state.get("message", ""))
        self.detail_label.setText(detail or "Ready to enable")
        self.install_button.setEnabled(bool(descriptor.dependencies))

    def _install_selected(self) -> None:
        module_id = self._selected_id()
        if module_id:
            self.installRequested.emit(module_id)

    def _double_clicked(self, row: int, column: int) -> None:
        del column
        item = self.table.item(row, 1)
        if item is not None:
            self.openRequested.emit(str(item.data(Qt.ItemDataRole.UserRole)))
