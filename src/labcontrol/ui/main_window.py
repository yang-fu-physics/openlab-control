from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
import sys

import qtawesome as qta
from PySide6.QtCore import QEvent, QSize, QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QIcon, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDockWidget,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMdiArea,
    QMdiSubWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import AppConfig
from ..formatting import control_decimals, fixed_number
from ..models import DeviceKind, DeviceSnapshot, EventNotice, RunProgress, RunState, Severity
from ..measurement.manifest import (
    ModuleDescriptor,
    activate_shared_dependencies,
    discover_modules,
    missing_dependencies,
)
from ..measurement.settings import load_settings, save_settings
from ..runtime import RuntimeService
from ..sequence.model import COMMAND_SPECS, SPECS_BY_TYPE, Command, CommandType, SequenceDocument
from ..sequence.parser import load_sequence, parse_sequence, save_sequence, serialize_sequence
from .data_browser import DatBrowserWidget
from .dialogs import AlertDialog, CommandDialog, ManualControlDialog
from .measurement_modules import ModuleManagerDialog, ModuleWindow
from .scaling import current_ui_scale, scaled
from .sequence_editor import SequenceEditorWidget
from .trend import TrendDialog
from .widgets import ElidedLabel, StatusTile


class MainWindow(QMainWindow):
    TERMINAL_STATES = {RunState.IDLE, RunState.STOPPED, RunState.COMPLETED, RunState.FAULTED}

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.module_descriptors = self._discover_module_descriptors()
        self.runtime = RuntimeService(config, self.module_descriptors)
        self.document = SequenceDocument()
        self.sequence_path: Path | None = None
        self._last_sequence_directory = self.config.project_root
        self._last_data_directory = self.config.resolve_project_path(self.config.logging.directory)
        self.current_snapshots: dict[str, DeviceSnapshot] = {}
        self.current_run_state = RunState.IDLE
        self.status_tiles: dict[str, StatusTile] = {}
        self.manual_dialogs: dict[str, ManualControlDialog] = {}
        self.module_windows: dict[str, ModuleWindow] = {}
        self.enabled_modules: set[str] = set()
        self._pending_run: tuple[dict[str, dict[str, object]], list[object]] | None = None
        self._minimized_module_windows: set[str] = set()
        self.alert_dialogs: dict[str, AlertDialog] = {}
        self.run_directory: Path | None = None
        self.trend_dialog = TrendDialog(self)
        self._dirty = False
        self.ui_scale = current_ui_scale()
        application = QApplication.instance()
        scale_mode = application.property("openlabUiScaleMode") if application is not None else None
        self.ui_scale_mode = str(scale_mode or "auto").title()

        self.setWindowTitle(f"{config.title} - Simulating")
        self.resize(scaled(1480), scaled(900))
        self.setMinimumSize(scaled(1180), scaled(720))
        self.setAcceptDrops(True)
        self._build_ui()
        self._apply_style()
        self._load_default_sequence()
        self.runtime.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain_runtime_messages)
        self.timer.start(config.ui_refresh_ms)

    def _discover_module_descriptors(self) -> tuple[ModuleDescriptor, ...]:
        descriptors = discover_modules(self.config)
        for descriptor in descriptors:
            if not descriptor.valid or descriptor.dependency_error:
                continue
            missing = missing_dependencies(descriptor)
            if missing:
                descriptor.dependency_error = "Missing dependencies: " + ", ".join(missing)
        return descriptors

    def _build_ui(self) -> None:
        self.mdi = QMdiArea()
        self.mdi.setBackground(Qt.GlobalColor.lightGray)
        self.setCentralWidget(self.mdi)

        self.editor = SequenceEditorWidget(self.document)
        self.editor.commandDoubleClicked.connect(self._edit_command)
        self.editor.documentChanged.connect(self._mark_dirty)
        self.sequence_window = QMdiSubWindow()
        self.sequence_window.setWidget(self.editor)
        self.sequence_window.setWindowTitle(self.document.name)
        self.sequence_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.mdi.addSubWindow(self.sequence_window)
        self.sequence_window.resize(scaled(780), scaled(560))
        self.sequence_window.show()

        self.data_browser = DatBrowserWidget(self.config.project_root)
        self.data_browser.fileChanged.connect(self._data_browser_file_changed)
        self.data_window = QMdiSubWindow()
        self.data_window.setWidget(self.data_browser)
        self.data_window.setWindowTitle("Data Browser")
        self.data_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.mdi.addSubWindow(self.data_window)
        self.data_window.resize(scaled(900), scaled(620))
        self.data_window.hide()

        self._build_left_dock()
        self._build_command_dock()
        self._build_status_dock()
        self._build_log_dock()
        self._build_actions()
        self.statusBar().showMessage(
            f"Starting simulation framework · UI scale {self.ui_scale:.2f}x ({self.ui_scale_mode})"
        )
        QTimer.singleShot(0, self._fit_mdi_windows)

    def _build_left_dock(self) -> None:
        dock = QDockWidget("Sequence Control", self)
        dock.setObjectName("sequenceControlDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        dock.setMinimumWidth(scaled(205))
        panel = QWidget()
        layout = QVBoxLayout(panel)

        project_group = QGroupBox("Experiment")
        project_layout = QVBoxLayout(project_group)
        project_layout.addWidget(QLabel("External Device Simulation"))
        self.measure_status_label = QLabel(
            f"0 of {len(self.module_descriptors)} measurement modules enabled"
        )
        self.measure_status_label.setObjectName("mutedLabel")
        project_layout.addWidget(self.measure_status_label)
        layout.addWidget(project_group)

        data_group = QGroupBox("Data File Name")
        data_layout = QVBoxLayout(data_group)
        self.data_file_label = ElidedLabel("<created automatically>")
        data_layout.addWidget(self.data_file_label)
        data_buttons = QHBoxLayout()
        self.view_data_button = QPushButton("View")
        self.change_data_button = QPushButton("Change")
        self.view_data_button.clicked.connect(self._view_data)
        self.change_data_button.clicked.connect(self._change_datafile)
        data_buttons.addWidget(self.view_data_button)
        data_buttons.addWidget(self.change_data_button)
        data_layout.addLayout(data_buttons)
        layout.addWidget(data_group)

        sequence_group = QGroupBox("Selected Sequence")
        sequence_layout = QVBoxLayout(sequence_group)
        self.sequence_label = ElidedLabel("Untitled.seq")
        sequence_layout.addWidget(self.sequence_label)
        sequence_buttons = QHBoxLayout()
        self.edit_sequence_button = QPushButton("Edit")
        self.change_sequence_button = QPushButton("Change")
        self.edit_sequence_button.clicked.connect(self._focus_sequence)
        self.change_sequence_button.clicked.connect(self._open_sequence)
        sequence_buttons.addWidget(self.edit_sequence_button)
        sequence_buttons.addWidget(self.change_sequence_button)
        sequence_layout.addLayout(sequence_buttons)
        layout.addWidget(sequence_group)

        status_group = QGroupBox("Sequence Status")
        status_group.setObjectName("statusGroup")
        status_layout = QVBoxLayout(status_group)
        self.run_status_label = QLabel("Sequence Idle")
        self.run_status_label.setObjectName("statusBadge")
        self.run_detail_label = QLabel("")
        self.run_detail_label.setWordWrap(True)
        self.run_detail_label.setObjectName("mutedLabel")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        status_layout.addWidget(self.run_status_label)
        status_layout.addWidget(self.run_detail_label)
        status_layout.addWidget(self.progress_bar)
        layout.addWidget(status_group)

        layout.addStretch(1)
        run_buttons = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")
        self.run_button.clicked.connect(self._run_sequence)
        self.pause_button.clicked.connect(self._pause_or_resume)
        self.stop_button.clicked.connect(self.runtime.stop_sequence)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        run_buttons.addWidget(self.run_button)
        run_buttons.addWidget(self.pause_button)
        run_buttons.addWidget(self.stop_button)
        layout.addLayout(run_buttons)

        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.left_dock = dock

    def _build_command_dock(self) -> None:
        dock = QDockWidget("Sequence Command Bar", self)
        dock.setObjectName("commandDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        dock.setMinimumWidth(scaled(285))
        panel = QWidget()
        layout = QVBoxLayout(panel)
        hint = QLabel("Double-click a command to configure and insert it")
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)
        self.command_tree = QTreeWidget()
        self.command_tree.setHeaderHidden(True)
        self.command_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        groups: dict[str, QTreeWidgetItem] = {}
        for spec in COMMAND_SPECS:
            group = groups.get(spec.category)
            if group is None:
                group = QTreeWidgetItem([spec.category])
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                groups[spec.category] = group
                self.command_tree.addTopLevelItem(group)
            child = QTreeWidgetItem([spec.label])
            child.setData(0, Qt.ItemDataRole.UserRole, spec.command_type.value)
            group.addChild(child)
        self.command_tree.expandAll()
        self.command_tree.itemDoubleClicked.connect(self._insert_palette_command)
        layout.addWidget(self.command_tree, 1)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.command_dock = dock

    def _build_status_dock(self) -> None:
        dock = QDockWidget("Device Status", self)
        dock.setObjectName("statusDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        panel = QWidget()
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)
        for device in self.config.devices:
            tile = StatusTile(device.id, device.display_name, device.kind)
            if device.kind is not DeviceKind.MONITOR:
                tile.doubleClicked.connect(self._open_manual_control)
            self.status_tiles[device.id] = tile
            layout.addWidget(tile)
        layout.addStretch(1)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.status_dock = dock

    def _build_log_dock(self) -> None:
        dock = QDockWidget("Run Log", self)
        dock.setObjectName("logDock")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        dock.setWidget(self.log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        dock.hide()
        self.log_dock = dock

    def _build_actions(self) -> None:
        self.module_manager = ModuleManagerDialog(self.module_descriptors, self)
        self.module_manager.enableRequested.connect(self._set_module_enabled)
        self.module_manager.refreshRequested.connect(self._refresh_modules)
        self.module_manager.installRequested.connect(self._install_module_dependencies)
        self.module_manager.openRequested.connect(self._show_module_window)
        self.new_action = QAction(qta.icon("fa5s.file"), "New", self)
        self.open_action = QAction(qta.icon("fa5s.folder-open"), "Open", self)
        self.save_action = QAction(qta.icon("fa5s.save"), "Save", self)
        self.save_as_action = QAction(qta.icon("fa5s.file-signature"), "Save As", self)
        self.run_action = QAction(qta.icon("fa5s.play", color="green"), "Run", self)
        self.pause_action = QAction(qta.icon("fa5s.pause", color="orange"), "Pause/Resume", self)
        self.stop_action = QAction(qta.icon("fa5s.stop", color="red"), "Stop", self)
        self.graph_action = QAction(qta.icon("fa5s.chart-line"), "Live Trend", self)
        self.data_browser_action = QAction(qta.icon("fa5s.database"), "Data Browser", self)
        self.modules_action = QAction(qta.icon("fa5s.cubes"), "Modules", self)
        self.log_action = self.log_dock.toggleViewAction()
        self.about_action = QAction("About", self)
        self.exit_action = QAction("Exit", self)

        self.new_action.triggered.connect(self._new_sequence)
        self.open_action.triggered.connect(self._open_sequence)
        self.save_action.triggered.connect(self._save_sequence)
        self.save_as_action.triggered.connect(lambda: self._save_sequence(save_as=True))
        self.run_action.triggered.connect(self._run_sequence)
        self.pause_action.triggered.connect(self._pause_or_resume)
        self.stop_action.triggered.connect(self.runtime.stop_sequence)
        self.graph_action.triggered.connect(self._show_graph)
        self.data_browser_action.triggered.connect(lambda checked=False: self._show_data_browser())
        self.modules_action.triggered.connect(self._show_module_manager)
        self.about_action.triggered.connect(self._show_about)
        self.exit_action.triggered.connect(self.close)

        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        file_menu.addActions([self.new_action, self.open_action, self.save_action, self.save_as_action])
        file_menu.addSeparator()
        file_menu.addAction(self.data_browser_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)
        edit_menu = menu.addMenu("Edit")
        edit_menu.addActions([self.editor.disable_action, self.editor.enable_action])
        edit_menu.addSeparator()
        edit_menu.addActions([
            self.editor.delete_action,
            self.editor.copy_action,
            self.editor.paste_action,
        ])
        edit_menu.addSeparator()
        up_action = edit_menu.addAction("Move Up")
        down_action = edit_menu.addAction("Move Down")
        up_action.triggered.connect(lambda: self.editor.move_selected(-1))
        down_action.triggered.connect(lambda: self.editor.move_selected(1))
        view_menu = menu.addMenu("View")
        view_menu.addActions([self.left_dock.toggleViewAction(), self.command_dock.toggleViewAction(), self.log_action])
        sequence_menu = menu.addMenu("Sequence")
        sequence_menu.addActions([self.run_action, self.pause_action, self.stop_action])
        graph_menu = menu.addMenu("Graph")
        graph_menu.addActions([self.graph_action, self.data_browser_action])
        instrument_menu = menu.addMenu("Instrument")
        for device in self.config.devices:
            action = instrument_menu.addAction(device.display_name)
            action.triggered.connect(lambda checked=False, device_id=device.id: self._open_manual_control(device_id))
        modules_menu = menu.addMenu("Modules")
        modules_menu.addAction(self.modules_action)
        simulation_menu = menu.addMenu("Simulation")
        warning_action = simulation_menu.addAction("Inject Warning")
        error_action = simulation_menu.addAction("Inject Error")
        resolve_action = simulation_menu.addAction("Resolve Injected Events")
        warning_action.triggered.connect(
            lambda: self.runtime.inject_event(Severity.WARNING, "MANUAL_WARNING", "Manually injected simulation warning")
        )
        error_action.triggered.connect(
            lambda: self.runtime.inject_event(Severity.ERROR, "MANUAL_ERROR", "Manually injected simulation error")
        )
        resolve_action.triggered.connect(lambda: self._resolve_simulated_events())
        help_menu = menu.addMenu("Help")
        help_menu.addAction(self.about_action)

        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(scaled(20), scaled(20)))
        toolbar.addActions([self.new_action, self.open_action, self.save_action])
        toolbar.addSeparator()
        toolbar.addActions([self.run_action, self.pause_action, self.stop_action])
        toolbar.addSeparator()
        toolbar.addActions([self.graph_action, self.data_browser_action, self.modules_action])
        self.addToolBar(toolbar)

    def _apply_style(self) -> None:
        status_size = scaled(21)
        status_padding = scaled(6)
        status_radius = scaled(6)
        tile_title_size = scaled(18)
        tile_value_size = scaled(27)
        tile_detail_size = scaled(16)
        manual_size = scaled(30)
        manual_padding = scaled(15)
        self.setStyleSheet(
            "QLabel#mutedLabel { color: #888888; }"
            f"QLabel#statusBadge {{ font-size: {status_size}px; font-weight: bold; padding: {status_padding}px; border-radius: {status_radius}px; background: rgba(92, 107, 121, 0.15); color: #435260; }}"
            f"QLabel#tileTitle {{ font-weight: bold; font-size: {tile_title_size}px; }}"
            f"QLabel#tileValue {{ font-size: {tile_value_size}px; font-weight: bold; }}"
            f"QLabel#tileDetail {{ color: #888888; font-size: {tile_detail_size}px; }}"
            f"QLabel#manualCurrent {{ font-size: {manual_size}px; font-weight: bold; padding: {manual_padding}px; }}"
            "QGroupBox { font-weight: bold; }"
            "QListView::item:selected, QTreeView::item:selected { background: #cce5ff; color: #000000; }"
        )

    def _load_default_sequence(self) -> None:
        if not self.config.default_sequence:
            self._set_document(SequenceDocument())
            return
        path = self.config.resolve_project_path(self.config.default_sequence)
        if path.exists():
            result = load_sequence(path)
            self._set_document(result.document)
            for issue in result.issues:
                self._append_log(issue.level.upper(), "sequence", "PARSE", f"Line {issue.line_number}: {issue.message}")
        else:
            self._set_document(SequenceDocument())

    def _set_document(self, document: SequenceDocument) -> None:
        self.document = document
        self.sequence_path = document.path
        if document.path is not None:
            self._last_sequence_directory = document.path.resolve().parent
        self.editor.set_document(document)
        self.sequence_label.setFullText(document.name)
        self.sequence_window.setWindowTitle(document.name)
        self._dirty = False
        self._sync_datafile_label()
        # Closing an MDI subwindow hides it because WA_DeleteOnClose is false.
        # Loading or creating a document must reopen that existing editor.
        self._focus_sequence()

    def _sync_datafile_label(self) -> None:
        for command in self.document.commands:
            if command.type is CommandType.SET_DATAFILE:
                path_text = str(command.params.get("path", "experiment.dat"))
                self.data_file_label.setFullText(path_text)
                path = Path(path_text)
                if path.is_absolute():
                    self._last_data_directory = path.parent
                return
        self.data_file_label.setFullText("<create experiment.dat automatically>")

    def _mark_dirty(self) -> None:
        self._dirty = True
        title = self.document.name + " *"
        self.sequence_window.setWindowTitle(title)
        self._sync_datafile_label()

    def _new_sequence(self) -> None:
        if not self._confirm_discard():
            return
        self._set_document(SequenceDocument())

    def _open_sequence(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open SEQ",
            str(self._last_sequence_directory),
            "Sequence (*.seq);;All files (*)",
        )
        if not path:
            return
        self._last_sequence_directory = Path(path).resolve().parent
        result = load_sequence(path)
        self._set_document(result.document)
        if result.issues:
            summary = "\n".join(f"Line {item.line_number}: {item.message}" for item in result.issues[:12])
            QMessageBox.warning(self, "SEQ Validation", summary)

    def _save_sequence(self, save_as: bool = False) -> bool:
        path = self.sequence_path
        if save_as or path is None:
            selected, _ = QFileDialog.getSaveFileName(
                self,
                "Save SEQ",
                str(self._last_sequence_directory / self.document.name),
                "Sequence (*.seq)",
            )
            if not selected:
                return False
            path = Path(selected)
            if path.suffix.lower() != ".seq":
                path = path.with_suffix(".seq")
            self._last_sequence_directory = path.resolve().parent
        save_sequence(self.document, path)
        self.sequence_path = path
        self.sequence_label.setFullText(path.name)
        self.sequence_window.setWindowTitle(path.name)
        self._dirty = False
        return True

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved Changes",
            "The current sequence has unsaved changes. Discard them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _insert_palette_command(self, item: QTreeWidgetItem, column: int) -> None:
        value = item.data(0, Qt.ItemDataRole.UserRole)
        if not value or self.current_run_state not in self.TERMINAL_STATES:
            return
        command_type = CommandType(value)
        spec = SPECS_BY_TYPE[command_type]
        command = spec.create()
        dialog = CommandDialog(command, spec, self, device_configs=self.config.devices)
        if dialog.exec() == CommandDialog.DialogCode.Accepted:
            command.update_params(dialog.values())
            self.editor.insert_command(command)

    def _edit_command(self, command: Command) -> None:
        spec = SPECS_BY_TYPE.get(command.type)
        if spec is None:
            return
        dialog = CommandDialog(command, spec, self, device_configs=self.config.devices)
        if dialog.exec() == CommandDialog.DialogCode.Accepted:
            command.update_params(dialog.values())
            self.editor.rebuild(command.id)
            self._mark_dirty()

    def _change_datafile(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Select DAT File",
            str(self._last_data_directory / "experiment.dat"),
            "Data (*.dat)",
        )
        if not selected:
            return
        selected_path = Path(selected)
        if selected_path.suffix.lower() != ".dat":
            selected_path = selected_path.with_suffix(".dat")
        selected = str(selected_path.resolve())
        self._last_data_directory = selected_path.resolve().parent
        command = next((item for item in self.document.commands if item.type is CommandType.SET_DATAFILE), None)
        if command is None:
            command = Command(CommandType.SET_DATAFILE, {
                "mode": "open|create",
                "path_scope": "Custom folder",
                "path": selected,
            })
            self.document.commands.insert(0, command)
        else:
            command.update_params({
                "mode": "open|create",
                "path_scope": "Custom folder",
                "path": selected,
            })
        self.editor.rebuild(command.id)
        self._mark_dirty()

    def _view_data(self) -> None:
        # The browser deliberately does not follow the active measurement file.
        # It displays only the DAT file explicitly opened or dropped by the user.
        self._show_data_browser()

    def _focus_sequence(self) -> None:
        self.sequence_window.showNormal()
        # QMdiSubWindow.close() also hides its child widget even when the
        # subwindow itself is retained, so both layers must be restored.
        self.editor.show()
        self.sequence_window.show()
        self.sequence_window.setFocus()
        self.mdi.setActiveSubWindow(self.sequence_window)
        self.sequence_window.raise_()
        QTimer.singleShot(0, self._fit_mdi_windows)

    def _run_sequence(self) -> None:
        if self.current_run_state not in self.TERMINAL_STATES:
            return
        validation = parse_sequence(serialize_sequence(self.document), self.document.name)
        errors = [item for item in validation.issues if item.level == "error"]
        if errors:
            QMessageBox.critical(
                self,
                "SEQ Validation Failed",
                "\n".join(
                    f"Line {item.line_number}: {item.message}" for item in errors[:12]
                ),
            )
            return
        try:
            module_settings = self._save_and_collect_enabled_module_settings()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Module Settings Save Failed",
                f"The SEQ was not started.\n\n{exc}",
            )
            return
        dirty = [
            window.descriptor.name
            for module_id, window in self.module_windows.items()
            if module_id in self.enabled_modules and window.has_unapplied_edits()
        ]
        if dirty:
            message = QMessageBox(self)
            message.setIcon(QMessageBox.Icon.Warning)
            message.setWindowTitle("Unapplied Module Settings")
            message.setText("Some enabled modules contain unapplied setting changes.")
            message.setInformativeText(
                "\n".join(dirty)
                + "\n\nChoose whether to apply those settings before the SEQ starts."
            )
            apply_button = message.addButton(
                "Apply and Run", QMessageBox.ButtonRole.AcceptRole
            )
            run_button = message.addButton(
                "Run Without Applying", QMessageBox.ButtonRole.DestructiveRole
            )
            cancel_button = message.addButton(
                "Cancel", QMessageBox.ButtonRole.RejectRole
            )
            message.setDefaultButton(apply_button)
            message.exec()
            if message.clickedButton() is cancel_button:
                return
            if message.clickedButton() is apply_button:
                futures: list[object] = []
                for module_id in self.enabled_modules:
                    window = self.module_windows.get(module_id)
                    if window is not None and window.has_unapplied_edits():
                        futures.append(
                            self.runtime.apply_module_settings(
                                module_id, module_settings[module_id]
                            )
                        )
                self._pending_run = (module_settings, futures)
                self._set_runtime_editable(False)
                self.run_button.setEnabled(False)
                self.run_status_label.setText("Applying Module Settings")
                self.statusBar().showMessage("Applying module settings before Run...")
                return
            assert message.clickedButton() is run_button
        self._start_sequence(module_settings)

    def _start_sequence(self, module_settings: dict[str, dict[str, object]]) -> None:
        self.run_directory = None
        self.runtime.run_sequence(self.document, module_settings)
        self._set_runtime_editable(False)
        self.current_run_state = RunState.RUNNING
        self.run_status_label.setText("Sequence Running")
        self.run_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)

    def _pause_or_resume(self) -> None:
        if self.current_run_state is RunState.PAUSED:
            self.runtime.resume_sequence()
        elif self.current_run_state is RunState.RUNNING:
            self.runtime.pause_sequence()

    def _set_runtime_editable(self, editable: bool) -> None:
        self.editor.set_editable(editable)
        self.command_tree.setEnabled(editable)
        self.change_sequence_button.setEnabled(editable)
        self.change_data_button.setEnabled(editable)
        self.modules_action.setEnabled(editable)
        self.module_manager.set_operations_enabled(editable)
        for window in self.module_windows.values():
            window.set_sequence_running(not editable)

    def _drain_runtime_messages(self) -> None:
        for message in self.runtime.drain_messages():
            if message.kind == "snapshots":
                self._handle_snapshots(message.payload)
            elif message.kind == "event":
                self._handle_event(message.payload)
            elif message.kind == "progress":
                self._handle_progress(message.payload)
            elif message.kind == "module_state":
                self._handle_module_state(message.payload)
            elif message.kind == "startup_error":
                QMessageBox.critical(self, "Runtime Startup Failed", str(message.payload))
        self._check_pending_run()

    def _check_pending_run(self) -> None:
        if self._pending_run is None:
            return
        settings, futures = self._pending_run
        if not all(future.done() for future in futures):
            return
        self._pending_run = None
        errors = [future.exception() for future in futures if future.exception() is not None]
        if errors:
            self._set_runtime_editable(True)
            self.run_button.setEnabled(True)
            self.run_status_label.setText("Sequence Idle")
            self.statusBar().showMessage("Run cancelled because module settings could not be applied", 5000)
            return
        for module_id in self.enabled_modules:
            window = self.module_windows.get(module_id)
            if window is not None and window.has_unapplied_edits():
                window.mark_applied()
        self._start_sequence(settings)

    def _handle_snapshots(self, snapshots: dict[str, DeviceSnapshot]) -> None:
        self.current_snapshots = snapshots
        for device_id, snapshot in snapshots.items():
            tile = self.status_tiles.get(device_id)
            if tile is not None:
                tile.update_snapshot(snapshot)
            dialog = self.manual_dialogs.get(device_id)
            if dialog is not None:
                dialog.update_snapshot(snapshot)
        self.trend_dialog.add_snapshots(snapshots)

    def _handle_event(self, notice: EventNotice) -> None:
        event = notice.event
        if event.code == "RUN_DIRECTORY" and not notice.is_resolution:
            self.run_directory = Path(event.message)
            self.data_file_label.setFullText(str(event.context or event.message))
        elif event.code == "DATAFILE_SELECTED" and not notice.is_resolution:
            self.data_file_label.setFullText(event.message)
        state = "RESOLVED" if notice.is_resolution else event.severity.value.upper()
        self._append_log(state, event.source, event.code, event.message)
        if notice.is_resolution:
            dialog = self.alert_dialogs.pop(event.key, None)
            if dialog is not None:
                dialog.close()
            return
        if notice.show_popup and event.key not in self.alert_dialogs:
            dialog = AlertDialog(event, self)
            self.alert_dialogs[event.key] = dialog
            dialog.finished.connect(lambda result, key=event.key: self.alert_dialogs.pop(key, None))
            dialog.show()
            dialog.raise_()

    def _handle_progress(self, progress: RunProgress) -> None:
        self.current_run_state = progress.state
        self.run_status_label.setText(f"Sequence {progress.state.value.title()}")
        self.run_detail_label.setText(progress.message)
        if progress.total_steps:
            self.progress_bar.setValue(min(100, int(progress.completed_steps / progress.total_steps * 100)))
        self.statusBar().showMessage(progress.step_path or progress.message)
        if progress.state in self.TERMINAL_STATES:
            self._set_runtime_editable(True)
            self.run_button.setEnabled(True)
            self.pause_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.pause_button.setText("Pause")
            if progress.state is RunState.COMPLETED:
                self.progress_bar.setValue(100)
        elif progress.state is RunState.PAUSED:
            self.pause_button.setText("Resume")
        else:
            self.pause_button.setText("Pause")

    def _append_log(self, level: str, source: str, code: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{timestamp}  {level:<8}  {source}/{code}  {message}")

    def _module_descriptor(self, module_id: str) -> ModuleDescriptor:
        descriptor = next(
            (item for item in self.module_descriptors if item.id == module_id), None
        )
        if descriptor is None:
            raise KeyError(module_id)
        return descriptor

    def _module_settings_path(self, module_id: str) -> Path:
        root = self.config.resolve_project_path(self.config.modules.data_directory)
        return root / module_id / "settings.toml"

    def _saved_module_settings(self, module_id: str) -> dict[str, object]:
        return load_settings(self._module_settings_path(module_id))

    def _save_module_window(self, module_id: str) -> dict[str, object]:
        window = self.module_windows.get(module_id)
        settings = window.settings() if window is not None else self._saved_module_settings(module_id)
        save_settings(self._module_settings_path(module_id), settings)
        return settings

    def _save_and_collect_enabled_module_settings(self) -> dict[str, dict[str, object]]:
        settings: dict[str, dict[str, object]] = {}
        for module_id in sorted(self.enabled_modules):
            settings[module_id] = self._save_module_window(module_id)
        return settings

    def _show_module_manager(self) -> None:
        if self.module_manager.isMinimized():
            self.module_manager.showNormal()
        self.module_manager.show()
        self.module_manager.raise_()
        self.module_manager.activateWindow()

    def _set_module_enabled(self, module_id: str, enabled: bool) -> None:
        if self.current_run_state not in self.TERMINAL_STATES or self._pending_run is not None:
            self.module_manager.update_state(
                module_id,
                module_id in self.enabled_modules,
                "enabled" if module_id in self.enabled_modules else "disabled",
                "Module changes are unavailable while a SEQ is running",
            )
            return
        try:
            if enabled:
                settings = self._saved_module_settings(module_id)
                self.runtime.enable_module(module_id, settings)
                self.statusBar().showMessage(f"Initializing {self._module_descriptor(module_id).name}...")
            else:
                self._save_module_window(module_id)
                self.runtime.disable_module(module_id)
                self.statusBar().showMessage(f"Stopping {self._module_descriptor(module_id).name}...")
        except Exception as exc:
            self.module_manager.update_state(
                module_id,
                module_id in self.enabled_modules,
                "enabled" if module_id in self.enabled_modules else "disabled",
                str(exc),
            )
            QMessageBox.critical(self, "Module Operation Failed", str(exc))

    def _ensure_module_window(self, module_id: str) -> ModuleWindow:
        window = self.module_windows.get(module_id)
        if window is not None:
            return window
        descriptor = self._module_descriptor(module_id)
        window = ModuleWindow(descriptor, self)
        window.load_settings(self._saved_module_settings(module_id))
        window.applyRequested.connect(self._apply_module_settings)
        window.manualActionRequested.connect(self._module_manual_action)
        window.statusRefreshRequested.connect(self._refresh_module_status)
        self.module_windows[module_id] = window
        return window

    def _handle_module_state(self, payload: dict[str, object]) -> None:
        module_id = str(payload.get("module_id", ""))
        enabled = bool(payload.get("enabled", False))
        state = str(payload.get("state", "disabled"))
        status = dict(payload.get("status", {}))
        message = str(payload.get("message", ""))
        was_enabled = module_id in self.enabled_modules
        if enabled:
            self.enabled_modules.add(module_id)
        else:
            self.enabled_modules.discard(module_id)
        self.module_manager.update_state(module_id, enabled, state, message)
        window = self.module_windows.get(module_id)
        if enabled:
            try:
                window = self._ensure_module_window(module_id)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Module Window Failed",
                    f"{module_id}: {exc}\n\nThe module will be disabled.",
                )
                self.runtime.disable_module(module_id)
                return
            window.update_runtime(state, status, message)
            if not was_enabled:
                window.load_settings(self._saved_module_settings(module_id))
                window.show_in_front()
            elif state == "faulted":
                window.tabs.setCurrentIndex(1)
                window.show_in_front()
        elif window is not None and state == "disabled":
            window.update_runtime(state, status, message)
            window.hide()
        self.measure_status_label.setText(
            f"{len(self.enabled_modules)} of {len(self.module_descriptors)} measurement modules enabled"
        )
        if message:
            self.statusBar().showMessage(message, 4000)

    def _show_module_window(self, module_id: str) -> None:
        if module_id not in self.enabled_modules:
            self.statusBar().showMessage("Enable the module before opening its window", 3000)
            return
        window = self.module_windows.get(module_id)
        if window is not None:
            window.show_in_front()

    def _apply_module_settings(self, module_id: str) -> None:
        if self.current_run_state not in self.TERMINAL_STATES:
            return
        window = self.module_windows.get(module_id)
        if window is None or module_id not in self.enabled_modules:
            return
        answer = QMessageBox.question(
            window,
            "Apply Module Settings",
            "Send the displayed settings to the instrument now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        settings = window.settings()
        try:
            save_settings(self._module_settings_path(module_id), settings)
        except Exception as exc:
            QMessageBox.critical(window, "Module Settings Save Failed", str(exc))
            return
        self.runtime.apply_module_settings(module_id, settings)
        window.message_label.setText("Applying settings...")

    def _module_manual_action(
        self, module_id: str, action: str, payload: dict[str, object]
    ) -> None:
        if self.current_run_state not in self.TERMINAL_STATES or self._pending_run is not None:
            QMessageBox.warning(
                self,
                "SEQ Is Running",
                "Manual module actions are available only while the SEQ is idle.",
            )
            return
        self.runtime.module_manual_action(module_id, action, payload)

    def _refresh_module_status(self, module_id: str) -> None:
        if self.current_run_state in self.TERMINAL_STATES and self._pending_run is None:
            self.runtime.refresh_module_status(module_id)

    def _refresh_modules(self) -> None:
        if self.enabled_modules or self.current_run_state not in self.TERMINAL_STATES:
            QMessageBox.warning(
                self,
                "Refresh Unavailable",
                "Stop the SEQ and disable every module before refreshing module sources.",
            )
            return
        descriptors = self._discover_module_descriptors()
        self.runtime.replace_module_descriptors(descriptors)
        for window in self.module_windows.values():
            window.allow_application_close()
            window.close()
        self.module_windows.clear()
        self.module_descriptors = descriptors
        self.module_manager.set_descriptors(descriptors)
        self.measure_status_label.setText(
            f"0 of {len(descriptors)} measurement modules enabled"
        )
        self.statusBar().showMessage(f"Found {len(descriptors)} measurement modules", 3000)

    def _module_python_executable(self) -> Path | None:
        configured = self.config.modules.python_executable.strip()
        if configured:
            candidate = self.config.resolve_project_path(configured)
            return candidate if candidate.exists() else None
        if not getattr(sys, "frozen", False):
            return Path(sys.executable)
        candidate = self.config.project_root / "runtime" / "python" / "python.exe"
        return candidate if candidate.exists() else None

    def _install_module_dependencies(self, module_id: str) -> None:
        if self.enabled_modules:
            QMessageBox.warning(
                self,
                "Dependency Install Unavailable",
                "Disable every measurement module before changing the shared dependencies.",
            )
            return
        descriptor = self._module_descriptor(module_id)
        missing = missing_dependencies(descriptor)
        if not missing:
            QMessageBox.information(self, "Dependencies", "All declared dependencies are installed.")
            return
        python = self._module_python_executable()
        if python is None:
            QMessageBox.warning(
                self,
                "Python Runtime Not Configured",
                "Set modules.python_executable in configs/default.toml or add runtime/python/python.exe.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Install Module Dependencies",
            "Install the following packages into the shared Python environment?\n\n"
            + "\n".join(missing),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        wheel_folders = [
            self.config.resolve_project_path(self.config.modules.shared_wheels_directory),
            descriptor.path / "wheels",
        ]
        offline_args = [str(python), "-m", "pip", "install", "--no-index"]
        target = self.config.resolve_project_path(self.config.modules.site_packages_directory)
        target.mkdir(parents=True, exist_ok=True)
        offline_args.extend(["--target", str(target), "--upgrade"])
        for folder in wheel_folders:
            if folder.exists():
                offline_args.extend(["--find-links", str(folder)])
        offline_args.extend(missing)
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            offline_args,
            capture_output=True,
            text=True,
            creationflags=creationflags,
            check=False,
        )
        if result.returncode != 0:
            online = QMessageBox.question(
                self,
                "Offline Install Failed",
                "The required wheels were not available locally. Allow an online pip install?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if online != QMessageBox.StandardButton.Yes:
                QMessageBox.warning(self, "Dependencies Not Installed", result.stderr[-2000:])
                return
            result = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--target",
                    str(target),
                    "--upgrade",
                    *missing,
                ],
                capture_output=True,
                text=True,
                creationflags=creationflags,
                check=False,
            )
        if result.returncode == 0:
            activate_shared_dependencies(self.config)
            QMessageBox.information(self, "Dependencies Installed", "Installation completed.")
            self._refresh_modules()
        else:
            QMessageBox.critical(self, "Dependency Install Failed", result.stderr[-3000:])

    def _open_manual_control(self, device_id: str) -> None:
        config = self.config.device(device_id)
        if config.kind is DeviceKind.MONITOR:
            self.statusBar().showMessage(f"{config.display_name} is display only", 3000)
            return
        dialog = self.manual_dialogs.get(device_id)
        if dialog is None:
            dialog = ManualControlDialog(config, self)
            dialog.setRequested.connect(self._manual_set_target)
            dialog.holdRequested.connect(self.runtime.hold_device)
            self.manual_dialogs[device_id] = dialog
        snapshot = self.current_snapshots.get(device_id)
        if snapshot is not None:
            dialog.update_snapshot(snapshot)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _manual_set_target(self, device_id: str, value: float, rate: float, mode: str) -> None:
        self.runtime.set_target(device_id, value, rate, mode)
        snapshot = self.current_snapshots.get(device_id)
        precision = control_decimals(snapshot.kind, snapshot.unit) if snapshot is not None else 3
        self.statusBar().showMessage(
            f"Sent target {fixed_number(value, precision)} to {device_id}", 3000
        )

    def _show_graph(self) -> None:
        self.trend_dialog.show()
        self.trend_dialog.raise_()
        self.trend_dialog.activateWindow()

    def _show_data_browser(self, path: str | Path | None = None) -> None:
        self.data_window.showNormal()
        self.data_window.show()
        self.mdi.setActiveSubWindow(self.data_window)
        self.data_window.raise_()
        QTimer.singleShot(0, self._fit_mdi_windows)
        if path is not None:
            self.data_browser.load_path(path, show_errors=True)

    def _fit_mdi_windows(self) -> None:
        """Keep floating document windows inside the current MDI viewport."""
        viewport = self.mdi.viewport().rect()
        if viewport.width() <= 8 or viewport.height() <= 8:
            return
        max_width = max(320, viewport.width() - 4)
        max_height = max(240, viewport.height() - 4)
        for subwindow in (self.sequence_window, self.data_window):
            if subwindow.isMaximized():
                continue
            subwindow.resize(
                min(subwindow.width(), max_width),
                min(subwindow.height(), max_height),
            )
            maximum_x = max(0, viewport.width() - subwindow.width())
            maximum_y = max(0, viewport.height() - subwindow.height())
            subwindow.move(
                max(0, min(subwindow.x(), maximum_x)),
                max(0, min(subwindow.y(), maximum_y)),
            )

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "mdi"):
            QTimer.singleShot(0, self._fit_mdi_windows)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        if self.isMinimized():
            self._minimized_module_windows.clear()
            for module_id in self.enabled_modules:
                window = self.module_windows.get(module_id)
                if window is not None and window.isVisible() and not window.isMinimized():
                    self._minimized_module_windows.add(module_id)
                    window.showMinimized()
        else:
            for module_id in tuple(self._minimized_module_windows):
                window = self.module_windows.get(module_id)
                if window is not None:
                    window.showNormal()
            self._minimized_module_windows.clear()

    def _data_browser_file_changed(self, path: str) -> None:
        self.data_window.setWindowTitle(f"{Path(path).name} - Data Browser")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self.data_browser._first_dat_path(event) is not None:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        path = self.data_browser._first_dat_path(event)
        if path is not None:
            self._show_data_browser(path)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def _resolve_simulated_events(self) -> None:
        self.runtime.resolve_event("simulation", "MANUAL_WARNING", "manual")
        self.runtime.resolve_event("simulation", "MANUAL_ERROR", "manual")

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About OpenLab Control",
            f"OpenLab Control {__version__}\n\n"
            "Control framework for external temperature and magnetic-field devices, "
            "with process-isolated measurement modules.\n"
            "Current mode: Simulating (does not control PPMS or real instruments).\n"
            f"UI scale: {self.ui_scale:.2f}x ({self.ui_scale_mode}).",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.current_run_state not in self.TERMINAL_STATES:
            answer = QMessageBox.question(
                self,
                "Sequence Is Running",
                "Closing will stop the sequence and hold the current temperature and field. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.No:
                event.ignore()
                return
        for module_id in tuple(self.enabled_modules):
            try:
                self._save_module_window(module_id)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Module Settings Save Failed",
                    f"{module_id}: {exc}\n\nThe application will continue closing.",
                )
        for window in self.module_windows.values():
            window.allow_application_close()
            window.close()
        self.timer.stop()
        self.runtime.shutdown()
        event.accept()
