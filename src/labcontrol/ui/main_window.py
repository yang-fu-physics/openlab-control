from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QIcon, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDockWidget,
    QFileDialog,
    QFrame,
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
from ..runtime import RuntimeService
from ..sequence.model import COMMAND_SPECS, SPECS_BY_TYPE, Command, CommandType, SequenceDocument
from ..sequence.parser import load_sequence, save_sequence
from .data_browser import DatBrowserWidget
from .dialogs import AlertDialog, CommandDialog, ManualControlDialog
from .scaling import current_ui_scale, scaled
from .sequence_editor import SequenceEditorWidget
from .trend import TrendDialog
from .widgets import ElidedLabel, StatusTile


class MainWindow(QMainWindow):
    TERMINAL_STATES = {RunState.IDLE, RunState.STOPPED, RunState.COMPLETED, RunState.FAULTED}

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.runtime = RuntimeService(config)
        self.document = SequenceDocument()
        self.sequence_path: Path | None = None
        self._last_sequence_directory = self.config.project_root
        self._last_data_directory = self.config.resolve_project_path(self.config.logging.directory)
        self.current_snapshots: dict[str, DeviceSnapshot] = {}
        self.current_run_state = RunState.IDLE
        self.status_tiles: dict[str, StatusTile] = {}
        self.manual_dialogs: dict[str, ManualControlDialog] = {}
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
        self.measure_status_label = QLabel("Measurement Ready")
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
        self.new_action = QAction(qta.icon("fa5s.file"), "New", self)
        self.open_action = QAction(qta.icon("fa5s.folder-open"), "Open", self)
        self.save_action = QAction(qta.icon("fa5s.save"), "Save", self)
        self.save_as_action = QAction(qta.icon("fa5s.file-signature"), "Save As", self)
        self.run_action = QAction(qta.icon("fa5s.play", color="green"), "Run", self)
        self.pause_action = QAction(qta.icon("fa5s.pause", color="orange"), "Pause/Resume", self)
        self.stop_action = QAction(qta.icon("fa5s.stop", color="red"), "Stop", self)
        self.graph_action = QAction(qta.icon("fa5s.chart-line"), "Live Trend", self)
        self.data_browser_action = QAction(qta.icon("fa5s.database"), "Data Browser", self)
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
        toolbar.addActions([self.graph_action, self.data_browser_action])
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
        self.run_directory = None
        self.runtime.run_sequence(self.document)
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

    def _drain_runtime_messages(self) -> None:
        for message in self.runtime.drain_messages():
            if message.kind == "snapshots":
                self._handle_snapshots(message.payload)
            elif message.kind == "event":
                self._handle_event(message.payload)
            elif message.kind == "progress":
                self._handle_progress(message.payload)
            elif message.kind == "manual_measurement":
                self.statusBar().showMessage("Manual measurement completed", 3000)
            elif message.kind == "startup_error":
                QMessageBox.critical(self, "Runtime Startup Failed", str(message.payload))

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
            dialog.measureRequested.connect(lambda selected: self.runtime.measure_once([selected]))
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
            "Plugin-oriented control framework for external temperature, magnetic-field, and measurement devices.\n"
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
        self.timer.stop()
        self.runtime.shutdown()
        event.accept()
