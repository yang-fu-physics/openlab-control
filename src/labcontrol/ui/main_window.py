"""主窗口、浮动工具窗口及 Qt/运行时消息协调。

主窗口拥有唯一 ``RuntimeService``，通过定时器排空后台消息。手动控制、趋势、Data Browser
和模块窗口均由明确引用复用或在销毁时移除，避免窗口重建造成重复信号连接。SEQ 加载会导入
同名模块期望设置，但绝不自动 Enable、连接或 Apply。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
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
    QScrollArea,
    QSizePolicy,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import AppConfig
from ..instruments.manifest import SystemInstrumentDescriptor
from ..package_support.dependencies import (
    DependencyInstallError,
    install_offline_dependencies,
)
from ..package_support.trust import (
    ContentTrustError,
    ContentTrustStore,
    content_tree_digest,
)
from ..formatting import control_decimals, fixed_number
from ..models import (
    InstrumentConnectionState,
    InstrumentKind,
    InstrumentSnapshot,
    EventNotice,
    RunProgress,
    RunState,
    Severity,
    StabilityState,
)
from ..module_commands import (
    ModuleCommandSpec,
    module_command_key,
    normalize_module_commands,
    validate_module_command_parameters,
)
from ..measurement.manifest import (
    ModuleDescriptor,
    discover_modules,
    missing_dependencies,
    module_dependency_errors,
    module_dependency_directory,
)
from ..measurement.settings import load_settings, save_settings
from ..runtime import RuntimeService
from ..sequence.model import (
    COMMAND_SPECS,
    SPECS_BY_TYPE,
    Command,
    CommandSpec,
    CommandType,
    SequenceDocument,
    SystemInstrumentCommandSpec,
)
from ..sequence.module_settings import (
    SequenceModuleSettings,
    load_sequence_module_settings,
    save_sequence_module_settings,
    sequence_module_settings_path,
)
from ..sequence.parser import load_sequence, parse_sequence, save_sequence, serialize_sequence
from .appearance import AppearanceDialog
from .data_browser import DatBrowserWidget
from .dialogs import AlertDialog, CommandDialog, ManualControlDialog
from .measurement_modules import (
    ModuleManagerDialog,
    ModuleWindow,
)
from .module_monitor import ModuleMonitorPanel
from .instrument_panels import InstrumentPanelHost
from .trust_dialogs import confirm_measurement_module_trust
from .preferences import UiPreferences, UiPreferenceStore
from .scaling import (
    current_font_scale,
    current_ui_scale,
    scaled,
    scaled_text,
)
from .sequence_editor import SequenceEditorWidget
from .trend import TrendDialog
from .widgets import ElidedLabel
from .window_sizing import (
    fit_initial_window_width,
    preserve_restored_window_size,
)


class MainWindow(QMainWindow):
    TERMINAL_STATES = {RunState.IDLE, RunState.STOPPED, RunState.COMPLETED, RunState.FAULTED}

    def __init__(
        self,
        config: AppConfig,
        instrument_descriptors: tuple[SystemInstrumentDescriptor, ...] = (),
        *,
        ui_preferences: UiPreferences | None = None,
        ui_preference_store: UiPreferenceStore | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.ui_preference_store = ui_preference_store
        self.ui_preferences = ui_preferences or UiPreferences(
            config.ui_scale,
            current_font_scale(),
            "default",
        )
        self._skip_window_layout_save = False
        self._start_maximized = (
            self.ui_preferences.window_mode == "maximized"
        )
        self.content_trust_store = ContentTrustStore(
            config.resolve_project_path(
                config.modules.state_directory
            )
            / "trusted_content.json"
        )
        self.module_descriptors = self._discover_module_descriptors()
        self.runtime = RuntimeService(
            config,
            self.module_descriptors,
            instrument_descriptors,
        )
        self._instrument_sequence_commands: tuple[
            SystemInstrumentCommandSpec,
            ...,
        ] = (
            self.runtime.instrument_sequence_commands
        )
        self._instrument_sequence_command_specs = {
            (command.instrument_id, command.command_id): command
            for command in self._instrument_sequence_commands
        }
        self.document = SequenceDocument()
        self.sequence_path: Path | None = None
        # 这些值属于当前打开的 SEQ，而不是“已发送到仪表”的状态。模块保持 Disabled；
        # 用户 Enable 后只会看到设置，仍须在 Settings 页显式 Apply。
        self._sequence_module_settings: dict[
            str,
            dict[str, object],
        ] = {}
        self._sequence_module_versions: dict[
            str,
            str,
        ] = {}
        self._sequence_module_settings_source: (
            Path | None
        ) = None
        self._sequence_module_window_issues: list[
            str
        ] = []
        self._last_sequence_directory = self.config.project_root
        self._last_data_directory = self.config.resolve_project_path(self.config.logging.directory)
        self.current_snapshots: dict[str, InstrumentSnapshot] = {}
        self.current_run_state = RunState.IDLE
        self.manual_dialogs: dict[str, ManualControlDialog] = {}
        self.module_windows: dict[str, ModuleWindow] = {}
        self.enabled_modules: set[str] = set()
        # 这份注册表只包含 open 已成功的 Enabled 模块。Disable 或 worker 故障会立即
        # 从右侧指令树移除对应顶层组，但文档中的通用 Module 行仍原样保留。
        self._module_command_specs: dict[
            tuple[str, str],
            ModuleCommandSpec,
        ] = {}
        self._module_command_groups: dict[
            str,
            QTreeWidgetItem,
        ] = {}
        self._pending_run: tuple[dict[str, dict[str, object]], list[object]] | None = None
        # Enable/Disable 是提交到后台 event loop 的 Future。必须保留并收取异常，
        # 否则在后台尚未来得及发布最终 module_state 前失败时，复选框会永久停在
        # Initializing/Disabling，用户也看不到可重试的状态。
        self._pending_module_operations: dict[
            str,
            tuple[object, bool],
        ] = {}
        self._minimized_module_windows: set[str] = set()
        self._pending_manual_operations: list[tuple[object, str]] = []
        self.alert_dialogs: dict[str, AlertDialog] = {}
        self.run_directory: Path | None = None
        self.trend_dialog = TrendDialog(self)
        self._dirty = False
        self.ui_scale = current_ui_scale()
        self.font_scale = current_font_scale()
        application = QApplication.instance()
        scale_mode = application.property("openlabUiScaleMode") if application is not None else None
        self.ui_scale_mode = str(scale_mode or "auto").title()

        self.setWindowTitle(config.title)
        self.resize(scaled(1480), scaled(900))
        self.setMinimumSize(scaled(1180), scaled(720))
        self.setAcceptDrops(True)
        self._build_ui()
        self._restore_window_layout()
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
            dependency_errors = module_dependency_errors(
                self.config,
                descriptor,
            )
            if dependency_errors:
                descriptor.dependency_error = "; ".join(
                    dependency_errors
                )
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
            f"Starting simulation framework · UI scale {self.ui_scale:.2f}x "
            f"({self.ui_scale_mode}) · text {self.font_scale:.0%}"
        )
        QTimer.singleShot(0, self._fit_mdi_windows)

    def _build_left_dock(self) -> None:
        dock = QDockWidget("Sequence Control", self)
        dock.setObjectName("sequenceControlDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        # 左右主侧栏属于固定工作区，不允许误关闭或拖成浮动窗口；QMainWindow 的
        # 分隔线缩放不依赖这些 feature，因此用户仍可自由调节侧栏宽度。
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.NoDockWidgetFeatures
        )
        dock.setMinimumWidth(scaled(205))
        panel = QWidget()
        layout = QVBoxLayout(panel)

        project_group = QGroupBox("Experiment")
        project_layout = QVBoxLayout(project_group)
        project_layout.addWidget(QLabel("External Instrument Simulation"))
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

        self.module_monitor_group = ModuleMonitorPanel(panel)
        self.module_monitor_group.activated.connect(
            self._show_module_window
        )
        # 保留清楚的别名，布局测试和主窗口的窗口状态同步不需要了解面板内部布局。
        self.module_monitor_scroll = (
            self.module_monitor_group.scroll
        )
        self.module_monitor_cards = (
            self.module_monitor_group.cards
        )
        layout.addWidget(self.module_monitor_group, 1)

        run_buttons = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")
        self.run_button.clicked.connect(self._run_sequence)
        self.pause_button.clicked.connect(self._pause_or_resume)
        self.stop_button.clicked.connect(self.runtime.stop_sequence)
        self.run_button.setEnabled(False)
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
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.NoDockWidgetFeatures
        )
        # 只保留命令树可用所需的紧凑下限。原来的 285 px 硬下限会让 4K 自动缩放
        # 后的右栏接近 400 px，看起来像分隔线失效。
        dock.setMinimumWidth(scaled(190))
        panel = QWidget()
        layout = QVBoxLayout(panel)
        # 允许按停靠栏的实际宽度自动换行。横向 Ignored 很关键：否则 QLabel 的
        # 单行 sizeHint 会反过来决定停靠栏最低宽度，使“自动换行”永远没有机会发生。
        self.command_hint = QLabel(
            "Double-click a command to configure and insert it"
        )
        self.command_hint.setObjectName("mutedLabel")
        self.command_hint.setWordWrap(True)
        self.command_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self.command_hint)
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
            child.setData(
                0,
                Qt.ItemDataRole.UserRole,
                ("core", spec.command_type.value),
            )
            group.addChild(child)
        system_group = groups["System Commands"]
        for spec in self._instrument_sequence_commands:
            child = QTreeWidgetItem([spec.label])
            child.setData(
                0,
                Qt.ItemDataRole.UserRole,
                (
                    "instrument",
                    spec.instrument_id,
                    spec.command_id,
                ),
            )
            system_group.addChild(child)
        self.command_tree.expandAll()
        self.command_tree.itemDoubleClicked.connect(self._insert_palette_command)
        layout.addWidget(self.command_tree, 1)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.command_dock = dock

    def _set_module_command_palette(
        self,
        module_id: str,
        declarations: object,
    ) -> None:
        """原子替换一个 Enabled 模块的顶层指令组。"""

        try:
            specs = normalize_module_commands(
                module_id,
                declarations,
            )
        except (TypeError, ValueError) as exc:
            # worker 和 runtime 已验证过一次；若跨线程消息仍损坏，宁可不注册，也不能
            # 让一个不可信参数描述进入 GUI 或随后发送给仪表。
            specs = ()
            QMessageBox.critical(
                self,
                "Module Command Metadata Failed",
                f"{module_id}: {exc}",
            )
        current = tuple(
            spec
            for (registered_module, _), spec
            in self._module_command_specs.items()
            if registered_module == module_id
        )
        if current == specs:
            self.editor.set_available_module_commands(
                set(self._module_command_specs)
            )
            return
        group = self._module_command_groups.pop(
            module_id,
            None,
        )
        if group is not None:
            index = self.command_tree.indexOfTopLevelItem(group)
            if index >= 0:
                self.command_tree.takeTopLevelItem(index)
        for key in tuple(self._module_command_specs):
            if key[0] == module_id:
                self._module_command_specs.pop(key, None)
        if specs:
            descriptor = next(
                (
                    item
                    for item in self.module_descriptors
                    if item.id == module_id
                ),
                None,
            )
            group = QTreeWidgetItem(
                [descriptor.name if descriptor is not None else module_id]
            )
            group.setFlags(
                group.flags()
                & ~Qt.ItemFlag.ItemIsSelectable
            )
            for spec in specs:
                self._module_command_specs[
                    (module_id, spec.command_id)
                ] = spec
                child = QTreeWidgetItem([spec.label])
                child.setToolTip(0, spec.description)
                child.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    ("module", module_id, spec.command_id),
                )
                group.addChild(child)
            self.command_tree.addTopLevelItem(group)
            group.setExpanded(True)
            self._module_command_groups[module_id] = group
        self.editor.set_available_module_commands(
            set(self._module_command_specs)
        )

    def _build_status_dock(self) -> None:
        dock = QDockWidget("Instrument Status", self)
        dock.setObjectName("statusDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        panel = InstrumentPanelHost(self.config.instruments)
        panel.controlRequested.connect(self._open_manual_control)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setWidget(panel)
        dock.setWidget(scroll)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.status_panel = panel
        self.status_scroll = scroll
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
        self.appearance_action = QAction("Appearance…", self)
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
        self.appearance_action.triggered.connect(
            self._show_appearance
        )
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
        view_menu.addSeparator()
        view_menu.addAction(self.appearance_action)
        sequence_menu = menu.addMenu("Sequence")
        sequence_menu.addActions([self.run_action, self.pause_action, self.stop_action])
        graph_menu = menu.addMenu("Graph")
        graph_menu.addActions([self.graph_action, self.data_browser_action])
        instrument_menu = menu.addMenu("Instrument")
        for instrument in self.config.instruments:
            if not instrument.control_enabled:
                continue
            action = instrument_menu.addAction(instrument.display_name)
            action.triggered.connect(lambda checked=False, instrument_id=instrument.id: self._open_manual_control(instrument_id))
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

    def _restore_dialog_geometry(
        self,
        window: QWidget,
        key: str,
    ) -> bool:
        """恢复一个浮动窗口，并禁止默认宽度适配覆盖用户尺寸。"""

        store = self.ui_preference_store
        if (
            store is None
            or self.ui_preferences.window_mode != "remember"
        ):
            return False
        geometry = store.geometry(key)
        if geometry is None or not window.restoreGeometry(geometry):
            return False
        preserve_restored_window_size(window)
        return True

    def _restore_window_layout(self) -> None:
        """在首次 Show 前恢复主窗口、MDI 子窗口和已创建工具窗口。"""

        store = self.ui_preference_store
        if (
            store is None
            or self.ui_preferences.window_mode != "remember"
        ):
            return
        main_geometry = store.geometry("main")
        if main_geometry is not None:
            self.restoreGeometry(main_geometry)
        main_state = store.main_window_state()
        if main_state is not None:
            self.restoreState(main_state)
        for key, window in (
            ("sequence", self.sequence_window),
            ("data_browser", self.data_window),
        ):
            rect = store.rect(key)
            if rect is not None:
                window.setGeometry(rect)
        self._restore_dialog_geometry(
            self.trend_dialog,
            "live_trend",
        )
        self._restore_dialog_geometry(
            self.module_manager,
            "module_manager",
        )
        self._start_maximized = (
            store.main_window_maximized()
        )

    def _save_window_layout(self) -> None:
        """保存本机窗口几何；失败只影响便利性，不改变仪表关闭流程。"""

        store = self.ui_preference_store
        if (
            store is None
            or self.ui_preferences.window_mode != "remember"
            or self._skip_window_layout_save
        ):
            return
        store.set_geometry("main", self.saveGeometry())
        store.set_main_window_state(self.saveState())
        store.set_main_window_maximized(self.isMaximized())
        store.set_rect(
            "sequence",
            self.sequence_window.geometry(),
        )
        store.set_rect(
            "data_browser",
            self.data_window.geometry(),
        )
        store.set_geometry(
            "live_trend",
            self.trend_dialog.saveGeometry(),
        )
        store.set_geometry(
            "module_manager",
            self.module_manager.saveGeometry(),
        )
        for instrument_id, dialog in self.manual_dialogs.items():
            store.set_geometry(
                f"manual/{instrument_id}",
                dialog.saveGeometry(),
            )
        for module_id, window in self.module_windows.items():
            store.set_geometry(
                f"module/{module_id}",
                window.saveGeometry(),
            )

    def should_start_maximized(self) -> bool:
        """供应用入口在第一次显示时选择 show 或 showMaximized。"""

        return self._start_maximized

    def _show_appearance(self) -> None:
        """保存下次启动使用的外观值；本次会话不重建任何仪表窗口。"""

        store = self.ui_preference_store
        if store is None:
            QMessageBox.warning(
                self,
                "Appearance Unavailable",
                "No writable UI preference store was configured.",
            )
            return
        dialog = AppearanceDialog(
            self.ui_preferences,
            self.config.ui_scale,
            self,
        )
        try:
            if (
                dialog.exec()
                != AppearanceDialog.DialogCode.Accepted
            ):
                return
            preferences = dialog.preferences()
            store.save(preferences)
            if dialog.reset_window_layout_requested:
                store.clear_window_layout()
                # 若本次关闭时重新保存当前几何，刚才的 Reset 会被抵消；跳过一次，
                # 下次启动采用默认位置后再恢复正常记忆。
                self._skip_window_layout_save = True
            self.ui_preferences = preferences
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "Appearance Save Failed",
                str(exc),
            )
            return
        finally:
            dialog.deleteLater()
        QMessageBox.information(
            self,
            "Appearance Saved",
            "The new overall size, text size and startup layout "
            "will take effect after restarting OpenLab Control.",
        )

    def _apply_style(self) -> None:
        status_size = scaled_text(21)
        status_padding = scaled(6)
        status_radius = scaled(6)
        tile_title_size = scaled_text(18)
        tile_value_size = scaled_text(27)
        tile_detail_size = scaled_text(16)
        manual_size = scaled_text(30)
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
            result = load_sequence(
                path,
                instrument_commands=(
                    self._instrument_sequence_commands
                ),
            )
            module_settings = (
                load_sequence_module_settings(path)
            )
            self._set_document(
                result.document,
                module_settings,
            )
            for issue in result.issues:
                self._append_log(issue.level.upper(), "sequence", "PARSE", f"Line {issue.line_number}: {issue.message}")
            for issue in self._module_command_document_issues(
                result.document
            ):
                self._append_log(
                    "WARNING",
                    "sequence",
                    "MODULE_COMMAND_IMPORT",
                    issue,
                )
            for issue in (
                self._module_settings_import_issues(
                    module_settings
                )
            ):
                self._append_log(
                    "WARNING",
                    "sequence",
                    "MODULE_SETTINGS_IMPORT",
                    issue,
                )
            if module_settings.settings:
                self._append_log(
                    "INFO",
                    "sequence",
                    "MODULE_SETTINGS_IMPORTED",
                    (
                        f"Imported {len(module_settings.settings)} "
                        "module setting set(s); nothing was "
                        "enabled or applied"
                    ),
                )
        else:
            self._set_document(SequenceDocument())

    def _set_document(
        self,
        document: SequenceDocument,
        module_settings: (
            SequenceModuleSettings | None
        ) = None,
    ) -> None:
        self.document = document
        self.sequence_path = document.path
        imported = module_settings or (
            SequenceModuleSettings({}, {})
        )
        self._sequence_module_settings = deepcopy(
            imported.settings
        )
        self._sequence_module_versions = dict(
            imported.versions
        )
        self._sequence_module_settings_source = (
            imported.source
        )
        self._sequence_module_window_issues = []
        if document.path is not None:
            self._last_sequence_directory = document.path.resolve().parent
        self.editor.set_document(document)
        self.editor.set_available_module_commands(
            set(self._module_command_specs)
        )
        self.sequence_label.setFullText(document.name)
        self.sequence_window.setWindowTitle(document.name)
        # 对已经 Enabled 的模块只替换 Settings 页显示值并标为未 Apply；不会调用
        # runtime.apply_module_settings，更不会自动打开新的仪表 session。
        for module_id, settings in (
            self._sequence_module_settings.items()
        ):
            if module_id not in self.enabled_modules:
                continue
            window = self.module_windows.get(module_id)
            if window is not None:
                try:
                    window.load_settings(
                        settings,
                        mark_unapplied=True,
                    )
                except Exception as exc:
                    # 第三方前端可能升级后不再接受旧字段。保留导入数据供用户修复，
                    # 但不让一个模块的 UI 异常阻止 SEQ 文本本身打开。
                    self._sequence_module_window_issues.append(
                        f"Could not load imported settings "
                        f"into enabled module {module_id!r}: "
                        f"{exc}"
                    )
        self._dirty = False
        self._sync_datafile_label()
        # Closing an MDI subwindow hides it because WA_DeleteOnClose is false.
        # Loading or creating a document must reopen that existing editor.
        self._focus_sequence()

    def _module_settings_import_issues(
        self,
        imported: SequenceModuleSettings,
    ) -> list[str]:
        issues = [
            *imported.issues,
            *self._sequence_module_window_issues,
        ]
        descriptors = {
            descriptor.id: descriptor
            for descriptor in self.module_descriptors
        }
        for module_id in sorted(
            imported.settings
        ):
            descriptor = descriptors.get(module_id)
            if descriptor is None:
                issues.append(
                    f"Settings for unavailable module "
                    f"{module_id!r} were kept but not loaded "
                    "into a window"
                )
                continue
            recorded = imported.versions.get(
                module_id,
                "",
            )
            if (
                recorded
                and recorded != descriptor.version
            ):
                issues.append(
                    f"{descriptor.name} settings were saved "
                    f"with module {recorded}, but installed "
                    f"version is {descriptor.version}; review "
                    "them before Apply Settings"
                )
        return issues

    def _module_command_document_issues(
        self,
        document: SequenceDocument,
        *,
        runnable_only: bool = False,
    ) -> list[str]:
        """说明文档中的模块指令为何尚不可执行；绝不删除或改写原参数。"""

        installed = {
            descriptor.id
            for descriptor in self.module_descriptors
            if descriptor.valid
        }
        issues: list[str] = []

        def visit(
            commands: list[Command],
            parent_enabled: bool,
        ) -> None:
            for command in commands:
                effective_enabled = parent_enabled and command.enabled
                if runnable_only and not effective_enabled:
                    continue
                key = module_command_key(command)
                if key is not None:
                    module_id, command_id = key
                    spec = self._module_command_specs.get(key)
                    prefix = f"{module_id}.{command_id}"
                    if module_id not in installed:
                        issues.append(
                            f"{prefix}: measurement module is not installed"
                        )
                    elif module_id not in self.enabled_modules:
                        issues.append(
                            f"{prefix}: module must be Enabled before Run or editing"
                        )
                    elif spec is None:
                        issues.append(
                            f"{prefix}: the Enabled module does not declare this command ID"
                        )
                    elif command.type is not spec.command_type:
                        expected = (
                            "Module Scan"
                            if spec.kind == "scan"
                            else "Module Command"
                        )
                        issues.append(
                            f"{prefix}: command kind must be {expected}"
                        )
                    else:
                        parameter_issues = validate_module_command_parameters(
                            spec,
                            command.params,
                        )
                        if parameter_issues:
                            issues.append(
                                f"{prefix}: "
                                + "; ".join(parameter_issues)
                            )
                visit(command.children, effective_enabled)

        visit(document.commands, True)
        return list(dict.fromkeys(issues))

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
        result = load_sequence(
            path,
            instrument_commands=(
                self._instrument_sequence_commands
            ),
        )
        imported = load_sequence_module_settings(
            path
        )
        self._set_document(
            result.document,
            imported,
        )
        messages = [
            (
                f"Line {item.line_number}: "
                f"{item.message}"
            )
            for item in result.issues
        ]
        messages.extend(
            self._module_command_document_issues(
                result.document
            )
        )
        messages.extend(
            self._module_settings_import_issues(
                imported
            )
        )
        if messages:
            summary = "\n".join(messages[:12])
            QMessageBox.warning(self, "SEQ Validation", summary)
        if imported.settings:
            self.statusBar().showMessage(
                (
                    f"Imported {len(imported.settings)} "
                    "module setting set(s); the import did "
                    "not enable modules or apply settings"
                ),
                8000,
            )
        elif not messages:
            self.statusBar().showMessage(
                f"Loaded {Path(path).name}",
                3000,
            )

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
        try:
            (
                associated_settings,
                associated_versions,
            ) = self._collect_sequence_module_settings()
            destination = save_sequence(
                self.document,
                path,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "SEQ Save Failed",
                str(exc),
            )
            return False

        self.sequence_path = destination
        self.sequence_label.setFullText(
            destination.name
        )
        self.sequence_window.setWindowTitle(
            destination.name
        )
        sidecar = sequence_module_settings_path(
            destination
        )
        try:
            # 不为完全没有模块关系的旧 SEQ 强行增加文件；若伴随文件已经存在，则用
            # 空 modules 表覆盖，确保用户清空关系后不会再次导入旧设置。
            if associated_settings or sidecar.exists():
                sidecar = save_sequence_module_settings(
                    destination,
                    associated_settings,
                    associated_versions,
                )
                self._sequence_module_settings_source = (
                    sidecar
                )
            else:
                self._sequence_module_settings_source = (
                    None
                )
        except Exception as exc:
            self._dirty = True
            self.sequence_window.setWindowTitle(
                destination.name + " *"
            )
            QMessageBox.critical(
                self,
                "Module Settings Save Failed",
                (
                    "The SEQ text was saved, but its module "
                    "settings companion was not.\n\n"
                    f"{exc}"
                ),
            )
            return False
        self._sequence_module_settings = deepcopy(
            associated_settings
        )
        self._sequence_module_versions = dict(
            associated_versions
        )
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
        if not isinstance(value, tuple) or not value:
            return
        if value[0] == "instrument" and len(value) == 3:
            spec = self._instrument_sequence_command_specs.get(
                (str(value[1]), str(value[2]))
            )
            if spec is not None:
                self.editor.insert_command(spec.create())
            return
        if value[0] == "core" and len(value) == 2:
            command_type = CommandType(value[1])
            spec: CommandSpec | ModuleCommandSpec = SPECS_BY_TYPE[command_type]
        elif value[0] == "module" and len(value) == 3:
            spec = self._module_command_specs.get(
                (str(value[1]), str(value[2]))
            )
            if spec is None:
                return
        else:
            return
        command = spec.create()
        if isinstance(spec, ModuleCommandSpec) and spec.custom_editor:
            values = self._edit_custom_module_command(
                command,
                spec,
            )
            if values is not None:
                command.update_params(values)
                self.editor.insert_command(command)
            return
        dialog = CommandDialog(
            command,
            spec,
            self,
            instrument_configs=self.config.instruments,
            data_directory=self._last_data_directory,
        )
        try:
            if dialog.exec() == CommandDialog.DialogCode.Accepted:
                values = dialog.values()
                if isinstance(spec, ModuleCommandSpec):
                    issues = validate_module_command_parameters(
                        spec,
                        values,
                    )
                    if issues:
                        QMessageBox.warning(
                            self,
                            "Invalid Module Command",
                            "\n".join(issues),
                        )
                        return
                command.update_params(values)
                self._remember_datafile_directory(command)
                self.editor.insert_command(command)
        finally:
            dialog.deleteLater()

    def _edit_command(self, command: Command) -> None:
        key = module_command_key(command)
        spec: CommandSpec | ModuleCommandSpec | None = (
            self._module_command_specs.get(key)
            if key is not None
            else SPECS_BY_TYPE.get(command.type)
        )
        if spec is None:
            if key is not None:
                QMessageBox.warning(
                    self,
                    "Module Command Unavailable",
                    (
                        f"Enable module {command.module_id!r} with command "
                        f"{command.module_command_id!r} before editing this line."
                    ),
                )
            return
        if isinstance(spec, ModuleCommandSpec) and spec.custom_editor:
            values = self._edit_custom_module_command(
                command,
                spec,
            )
            if values is not None:
                command.update_params(values)
                self.editor.rebuild(command.id)
                self._mark_dirty()
            return
        dialog = CommandDialog(
            command,
            spec,
            self,
            instrument_configs=self.config.instruments,
            data_directory=self._last_data_directory,
        )
        try:
            if dialog.exec() == CommandDialog.DialogCode.Accepted:
                values = dialog.values()
                if isinstance(spec, ModuleCommandSpec):
                    issues = validate_module_command_parameters(
                        spec,
                        values,
                    )
                    if issues:
                        QMessageBox.warning(
                            self,
                            "Invalid Module Command",
                            "\n".join(issues),
                        )
                        return
                command.update_params(values)
                self._remember_datafile_directory(command)
                self.editor.rebuild(command.id)
                self._mark_dirty()
        finally:
            dialog.deleteLater()

    def _edit_custom_module_command(
        self,
        command: Command,
        spec: ModuleCommandSpec,
    ) -> dict[str, object] | None:
        """调用 Enabled 模块 Frontend 的可选自定义参数窗口并验证返回值。"""

        window = self.module_windows.get(spec.module_id)
        if window is None or spec.module_id not in self.enabled_modules:
            QMessageBox.warning(
                self,
                "Module Command Unavailable",
                f"Enable module {spec.module_id!r} before editing this command.",
            )
            return None
        try:
            values = window.edit_sequence_command(
                spec.command_id,
                command.params,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Module Command Editor Failed",
                f"{spec.module_id}.{spec.command_id}: {exc}",
            )
            return None
        if values is None:
            return None
        issues = validate_module_command_parameters(
            spec,
            values,
        )
        if issues:
            QMessageBox.warning(
                self,
                "Invalid Module Command",
                "\n".join(issues),
            )
            return None
        return dict(values)

    def _remember_datafile_directory(self, command: Command) -> None:
        """记住参数窗口明确选择的目录，供下一次系统文件窗口使用。"""

        if (
            command.type is not CommandType.SET_DATAFILE
            or command.params.get("path_scope") != "Custom folder"
        ):
            return
        selected_path = Path(str(command.params.get("path", "")))
        if selected_path.is_absolute():
            self._last_data_directory = selected_path.parent

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
        module_command_issues = self._module_command_document_issues(
            self.document,
            runnable_only=True,
        )
        if module_command_issues:
            QMessageBox.critical(
                self,
                "Module Command Validation Failed",
                "\n".join(module_command_issues[:12]),
            )
            return
        validation = parse_sequence(
            serialize_sequence(self.document),
            self.document.name,
            instrument_commands=(
                self._instrument_sequence_commands
            ),
        )
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
            fit_initial_window_width(message)
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
        for dialog in self.manual_dialogs.values():
            dialog.set_runtime_editable(editable)

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
            elif message.kind == "module_result":
                self.module_monitor_group.update_result(
                    message.payload
                )
            elif message.kind == "module_results_reset":
                self.module_monitor_group.reset_results(
                    str(message.payload.get("module_id", ""))
                )
            elif message.kind == "startup_error":
                QMessageBox.critical(self, "Runtime Startup Failed", str(message.payload))
        self._sync_module_monitor_window_states()
        self._check_pending_run()
        self._check_pending_module_operations()
        self._check_pending_manual_operations()

    def _check_pending_module_operations(self) -> None:
        """收取 Enable/Disable Future，并为异常路径恢复可操作的终止状态。"""

        for module_id, (
            future,
            requested_enabled,
        ) in tuple(
            self._pending_module_operations.items()
        ):
            if not future.done():
                continue
            del self._pending_module_operations[module_id]
            try:
                exception = future.exception()
            except Exception as exc:
                exception = exc
            if exception is None:
                continue

            actual_enabled = (
                module_id in self.enabled_modules
            )
            current_state = (
                self.module_manager.runtime_state(
                    module_id
                )
            )
            # 正常 service 错误会先发布 disabled/faulted。这里可为 Disabled 刷新
            # 最终错误详情，或在消息缺失时结束过渡态，但不覆盖更精确的 Faulted。
            if current_state in {
                "disabled",
                "initializing",
                "disabling",
            }:
                self.module_manager.update_state(
                    module_id,
                    actual_enabled,
                    (
                        "enabled"
                        if actual_enabled
                        else "disabled"
                    ),
                    str(exception),
                )
            operation = (
                "enable"
                if requested_enabled
                else "disable"
            )
            self.statusBar().showMessage(
                f"Could not {operation} "
                f"{self._module_descriptor(module_id).name}: "
                f"{exception}",
                8000,
            )

    def _check_pending_manual_operations(self) -> None:
        remaining: list[tuple[object, str]] = []
        for future, success_message in self._pending_manual_operations:
            if not future.done():
                remaining.append((future, success_message))
                continue
            exception = future.exception()
            if exception is not None:
                self.statusBar().showMessage(
                    f"Manual operation failed: {exception}",
                    5000,
                )
                continue
            result = future.result()
            if result is False:
                self.statusBar().showMessage(
                    "Manual request was not confirmed by the instrument",
                    5000,
                )
            else:
                self.statusBar().showMessage(success_message, 3000)
        self._pending_manual_operations = remaining

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
            self._update_run_availability()
            self.run_status_label.setText("Sequence Idle")
            self.statusBar().showMessage("Run cancelled because module settings could not be applied", 5000)
            return
        for module_id in self.enabled_modules:
            window = self.module_windows.get(module_id)
            if window is not None and window.has_unapplied_edits():
                window.mark_applied()
        self._start_sequence(settings)

    def _handle_snapshots(self, snapshots: dict[str, InstrumentSnapshot]) -> None:
        self.current_snapshots = snapshots
        for instrument_id, snapshot in snapshots.items():
            self.status_panel.update_snapshot(snapshot)
            dialog = self.manual_dialogs.get(instrument_id)
            if dialog is not None:
                dialog.update_snapshot(snapshot)
        self.trend_dialog.add_snapshots(snapshots)
        self._update_run_availability()

    def _update_run_availability(self) -> None:
        if (
            self.current_run_state not in self.TERMINAL_STATES
            or self._pending_run is not None
        ):
            self.run_button.setEnabled(False)
            return
        reason = ""
        for config in self.config.instruments:
            if not config.control_enabled:
                continue
            snapshot = self.current_snapshots.get(config.id)
            if snapshot is None:
                reason = f"Waiting for {config.display_name}"
                break
            if (
                not snapshot.connected
                or snapshot.connection_state
                is not InstrumentConnectionState.CONNECTED
            ):
                reason = (
                    snapshot.message
                    or f"{config.display_name} is not connected"
                )
                break
            if snapshot.stability is StabilityState.STALE:
                reason = f"{config.display_name} reading is stale"
                break
        self.run_button.setEnabled(not reason)
        self.run_button.setToolTip(
            reason
            if reason
            else "Run the current sequence"
        )

    def _handle_event(self, notice: EventNotice) -> None:
        event = notice.event
        self._handle_module_alert(notice)
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
            self._update_run_availability()
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

    def _sync_module_monitor_window_states(self) -> None:
        """只观察 Qt 窗口状态；不向 worker 或仪表发送任何请求。"""

        for module_id in self.module_monitor_cards:
            window = self.module_windows.get(module_id)
            self.module_monitor_group.set_minimized(
                module_id,
                bool(
                    window is not None
                    and window.isMinimized()
                )
            )

    def _handle_module_alert(
        self,
        notice: EventNotice,
    ) -> None:
        source = notice.event.source
        if not source.startswith("module:"):
            return
        module_id = source.split(":", 1)[1]
        self.module_monitor_group.update_alert(
            module_id,
            notice.event.key,
            notice.event.severity.value,
            resolved=notice.is_resolution,
        )

    def _module_settings_path(self, module_id: str) -> Path:
        root = self.config.resolve_project_path(self.config.modules.data_directory)
        return root / module_id / "settings.toml"

    def _saved_module_settings(self, module_id: str) -> dict[str, object]:
        if module_id in self._sequence_module_settings:
            return deepcopy(
                self._sequence_module_settings[
                    module_id
                ]
            )
        return load_settings(self._module_settings_path(module_id))

    def _remember_sequence_module_settings(
        self,
        module_id: str,
        settings: dict[str, object],
    ) -> None:
        self._sequence_module_settings[
            module_id
        ] = deepcopy(settings)
        try:
            descriptor = self._module_descriptor(
                module_id
            )
        except KeyError:
            return
        self._sequence_module_versions[
            module_id
        ] = descriptor.version

    def _collect_sequence_module_settings(
        self,
    ) -> tuple[
        dict[str, dict[str, object]],
        dict[str, str],
    ]:
        """合并已导入关系和当前 Enabled 模块的 Settings 页快照。

        未安装或当前 Disabled 的关联模块会原样保留，避免仅打开再保存一次 SEQ 就丢掉
        第三方模块设置；Enabled 模块则始终以界面当前值为准。
        """

        settings = deepcopy(
            self._sequence_module_settings
        )
        versions = dict(
            self._sequence_module_versions
        )
        for module_id in sorted(
            self.enabled_modules
        ):
            window = self.module_windows.get(
                module_id
            )
            values = (
                window.settings()
                if window is not None
                else self._saved_module_settings(
                    module_id
                )
            )
            settings[module_id] = deepcopy(
                values
            )
            versions[module_id] = (
                self._module_descriptor(
                    module_id
                ).version
            )
        return settings, versions

    def _save_module_window(self, module_id: str) -> dict[str, object]:
        window = self.module_windows.get(module_id)
        settings = window.settings() if window is not None else self._saved_module_settings(module_id)
        save_settings(self._module_settings_path(module_id), settings)
        self._remember_sequence_module_settings(
            module_id,
            settings,
        )
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
        if module_id in self._pending_module_operations:
            return
        try:
            if enabled:
                descriptor = self._module_descriptor(module_id)
                current_fingerprint = content_tree_digest(
                    descriptor.path
                )
                if current_fingerprint != descriptor.fingerprint:
                    raise ContentTrustError(
                        f"{descriptor.name} changed after discovery; "
                        "refresh modules before enabling it"
                    )
                if not confirm_measurement_module_trust(
                    self,
                    self.content_trust_store,
                    descriptor,
                ):
                    self.module_manager.update_state(
                        module_id,
                        False,
                        "disabled",
                        "Module was not trusted",
                    )
                    return
                future = self.runtime.enable_module(module_id)
                self._pending_module_operations[
                    module_id
                ] = (future, True)
                self.statusBar().showMessage(f"Initializing {self._module_descriptor(module_id).name}...")
            else:
                self._save_module_window(module_id)
                future = self.runtime.disable_module(
                    module_id
                )
                self._pending_module_operations[
                    module_id
                ] = (future, False)
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
        if (
            content_tree_digest(descriptor.path)
            != descriptor.fingerprint
            or not self.content_trust_store.is_trusted(
                "module",
                descriptor,
            )
        ):
            raise PermissionError(
                f"{descriptor.name} changed or is not trusted"
            )
        window = ModuleWindow(
            descriptor,
            self,
            resources=self.config.resource_payload("measurement"),
        )
        window.load_settings(
            self._saved_module_settings(module_id),
            mark_unapplied=(
                module_id
                in self._sequence_module_settings
            ),
        )
        window.applyRequested.connect(self._apply_module_settings)
        window.actionRequested.connect(self._module_action)
        window.statusRefreshRequested.connect(self._refresh_module_status)
        self._restore_dialog_geometry(
            window,
            f"module/{module_id}",
        )
        self.module_windows[module_id] = window
        return window

    def _handle_module_state(self, payload: dict[str, object]) -> None:
        module_id = str(payload.get("module_id", ""))
        enabled = bool(payload.get("enabled", False))
        state = str(payload.get("state", "disabled"))
        status = dict(payload.get("status", {}))
        message = str(payload.get("message", ""))
        sequence_commands = payload.get(
            "sequence_commands",
            [],
        )
        display_columns = payload.get(
            "display_columns",
            [],
        )
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
            self._set_module_command_palette(
                module_id,
                sequence_commands,
            )
            if not was_enabled:
                window.load_settings(
                    self._saved_module_settings(
                        module_id
                    ),
                    mark_unapplied=(
                        module_id
                        in self._sequence_module_settings
                    ),
                )
                window.show_in_front()
            elif state == "faulted":
                window.tabs.setCurrentIndex(1)
                window.show_in_front()
        elif window is not None and state == "disabled":
            window.update_runtime(state, status, message)
            window.hide()
            self._set_module_command_palette(module_id, [])
        elif not enabled:
            self._set_module_command_palette(module_id, [])
        self.module_monitor_group.update_module(
            self._module_descriptor(module_id),
            enabled=enabled,
            state=state,
            message=message,
            minimized=bool(
                window is not None
                and window.isMinimized()
            ),
            display_columns=display_columns,
        )
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
        self._remember_sequence_module_settings(
            module_id,
            settings,
        )
        self.runtime.apply_module_settings(module_id, settings)
        window.message_label.setText("Applying settings...")

    def _module_action(
        self, module_id: str, action: str, payload: dict[str, object]
    ) -> None:
        if self.current_run_state not in self.TERMINAL_STATES or self._pending_run is not None:
            QMessageBox.warning(
                self,
                "SEQ Is Running",
                "Manual module actions are available only while the SEQ is idle.",
            )
            return
        self.runtime.module_action(module_id, action, payload)

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
        self.module_monitor_group.clear()
        for module_id in tuple(self._module_command_groups):
            self._set_module_command_palette(module_id, [])
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
        if module_id in self.enabled_modules:
            QMessageBox.warning(
                self,
                "Dependency Install Unavailable",
                "Disable this measurement module before replacing "
                "its isolated dependencies.",
            )
            return
        descriptor = self._module_descriptor(module_id)
        try:
            current_fingerprint = content_tree_digest(
                descriptor.path
            )
        except ContentTrustError as exc:
            QMessageBox.critical(
                self,
                "Module Validation Failed",
                str(exc),
            )
            return
        if current_fingerprint != descriptor.fingerprint:
            QMessageBox.warning(
                self,
                "Module Changed",
                "Refresh modules before preparing dependencies.",
            )
            return
        if not confirm_measurement_module_trust(
            self,
            self.content_trust_store,
            descriptor,
        ):
            return
        dependency_errors = module_dependency_errors(
            self.config,
            descriptor,
        )
        if not dependency_errors:
            QMessageBox.information(
                self,
                "Dependencies",
                "All declared dependencies are installed in "
                "this module's isolated runtime.",
            )
            return
        missing = missing_dependencies(
            self.config,
            descriptor,
        )
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
            "Prepare the following packages in this module's "
            "isolated runtime using local wheels only?\n\n"
            + "\n".join(
                missing or descriptor.dependencies
            )
            + "\n\nCurrent runtime issue:\n"
            + "\n".join(dependency_errors),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            install_offline_dependencies(
                python_executable=python,
                package_directory=descriptor.path,
                site_packages=module_dependency_directory(
                    self.config,
                    descriptor,
                ),
                shared_wheels_directory=(
                    self.config.resolve_project_path(
                        self.config.modules.shared_wheels_directory
                    )
                ),
                dependencies=descriptor.dependencies,
                fingerprint=descriptor.fingerprint,
            )
        except DependencyInstallError as exc:
            QMessageBox.critical(
                self,
                "Offline Dependency Install Failed",
                str(exc),
            )
            return
        QMessageBox.information(
            self,
            "Dependencies Installed",
            "Offline preparation completed for this module.",
        )
        self._refresh_modules()

    def _open_manual_control(self, instrument_id: str) -> None:
        config = self.config.instrument(instrument_id)
        if not config.control_enabled:
            self.statusBar().showMessage(f"{config.display_name} is display only", 3000)
            return
        dialog = self.manual_dialogs.get(instrument_id)
        if dialog is None:
            dialog = ManualControlDialog(config, self)
            dialog.setRequested.connect(self._manual_set_target)
            dialog.holdRequested.connect(self._manual_hold_instrument)
            self._restore_dialog_geometry(
                dialog,
                f"manual/{instrument_id}",
            )
            self.manual_dialogs[instrument_id] = dialog
        dialog.set_runtime_editable(
            self.current_run_state in self.TERMINAL_STATES
            and self._pending_run is None
        )
        snapshot = self.current_snapshots.get(instrument_id)
        if snapshot is not None:
            dialog.update_snapshot(snapshot)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _manual_set_target(self, instrument_id: str, value: float, rate: float, mode: str) -> None:
        future = self.runtime.set_target(instrument_id, value, rate, mode)
        snapshot = self.current_snapshots.get(instrument_id)
        precision = control_decimals(snapshot.kind, snapshot.unit) if snapshot is not None else 3
        self._pending_manual_operations.append(
            (
                future,
                f"Confirmed target {fixed_number(value, precision)} for {instrument_id}",
            )
        )
        self.statusBar().showMessage(f"Sending target to {instrument_id}...")

    def _manual_hold_instrument(self, instrument_id: str) -> None:
        future = self.runtime.hold_instrument(instrument_id)
        self._pending_manual_operations.append(
            (future, f"Hold Current confirmed for {instrument_id}")
        )
        self.statusBar().showMessage(f"Requesting Hold Current for {instrument_id}...")

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
            "Control framework for external temperature and magnetic-field instruments, "
            "with process-isolated measurement modules.\n"
            "Configured System Instruments and Measurement Modules may communicate with external instruments. "
            "This application does not control PPMS.\n"
            f"UI scale: {self.ui_scale:.2f}x ({self.ui_scale_mode}).\n"
            f"Text scale: {self.font_scale:.0%}.",
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
        try:
            self._save_window_layout()
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Window Layout Save Failed",
                f"The application will continue closing.\n\n{exc}",
            )
        for window in self.module_windows.values():
            window.allow_application_close()
            window.close()
        self.timer.stop()
        try:
            self.runtime.shutdown()
        except Exception as exc:
            self.timer.start(self.config.ui_refresh_ms)
            QMessageBox.critical(
                self,
                "Shutdown Incomplete",
                "The instrument runtime did not stop cleanly. "
                "OpenLab Control will remain open so the failure is visible.\n\n"
                f"{exc}",
            )
            event.ignore()
            return
        event.accept()
