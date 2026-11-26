"""主窗口、浮动工具窗口及 Qt/运行时消息协调。

主窗口拥有唯一 ``RuntimeService``，通过定时器排空后台消息。手动控制、趋势、Data Browser
和模块窗口均由明确引用复用或在销毁时移除，避免窗口重建造成重复信号连接。SEQ 加载会导入
同名模块期望设置，但绝不自动 Enable、连接或 Apply。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFocusEvent,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QResizeEvent,
)
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
    QStyle,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..alarm_settings import AlarmSettingsStore
from ..config import AppConfig, InstrumentConfig
from ..instruments.frontend_api import InstrumentUIAPI
from ..instruments.manifest import SystemInstrumentDescriptor
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
    ModuleInstanceConfig,
    build_instance_descriptor,
    discover_modules,
    load_module_instances,
    module_instances_path,
    save_module_instances,
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
from .alarm_settings import AlarmSettingsDialog
from .data_browser import DatBrowserWidget
from .dialogs import AlertDialog, CommandDialog, ManualControlDialog
from .measurement_modules import (
    ModuleManagerDialog,
    ModuleWindow,
)
from .module_monitor import ModuleMonitorPanel
from .instrument_panels import (
    HIGH_FIELD_WARNING_OE,
    InstrumentPanelHost,
    field_magnitude_oe,
)
from .instrument_setup import InstrumentSetupDialog
from .preferences import UiPreferences, UiPreferenceStore, default_ui_preferences_path
from .scaling import (
    current_font_scale,
    current_ui_scale,
    scaled,
    scaled_text,
)
from .sequence_editor import SequenceEditorWidget
from .system_log import SystemLogDialog
from .trend import TrendDialog
from .widgets import ElidedLabel
from .window_sizing import (
    fit_initial_window_width,
    preserve_restored_window_size,
)


class _DataBrowserMdiSubWindow(QMdiSubWindow):
    """中央 MDI 区内的数据浏览子窗口；将标题栏拖出中央区域后自动弹出为独立窗口。"""

    detachRequested = Signal(QPoint)
    _RESIZE_CURSORS = frozenset(
        {
            Qt.CursorShape.SizeVerCursor,
            Qt.CursorShape.SizeHorCursor,
            Qt.CursorShape.SizeBDiagCursor,
            Qt.CursorShape.SizeFDiagCursor,
        }
    )

    def __init__(self, mdi: QMdiArea, detach_margin: int) -> None:
        super().__init__()
        self._mdi = mdi
        self._detach_margin = max(0, int(detach_margin))
        self._drag_start_pos: QPoint | None = None
        self._drag_start_size: QSize | None = None
        self._pressed_title_bar = False

    def _resize_border_width(self, child_rect: QRect) -> int:
        style = self.style()
        style_frame = (
            style.pixelMetric(
                QStyle.PixelMetric.PM_MdiSubWindowFrameWidth,
                None,
                self,
            )
            if style is not None
            else 0
        )
        return max(child_rect.left(), int(style_frame), scaled(6))

    def _is_draggable_title_bar_point(self, pos: QPoint) -> bool:
        child = self.widget()
        if child is None:
            return False
        if self.cursor().shape() in self._RESIZE_CURSORS:
            return False
        child_rect = child.geometry()
        resize_border = self._resize_border_width(child_rect)
        if not (resize_border <= pos.y() < child_rect.top()):
            return False
        left_bound = max(resize_border, child_rect.left() + scaled(24))
        right_bound = min(
            self.width() - resize_border - 1,
            child_rect.right() - scaled(96),
        )
        if right_bound < left_bound:
            right_bound = max(left_bound, self.width() - resize_border - 1)
        return left_bound <= pos.x() <= right_bound

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._drag_start_pos = None
        self._drag_start_size = None
        self._pressed_title_bar = False
        if event.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            pos = event.position().toPoint()
            if self._is_draggable_title_bar_point(pos):
                self._drag_start_pos = QPoint(self.pos())
                self._drag_start_size = QSize(self.size())
                self._pressed_title_bar = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        start_pos = self._drag_start_pos
        start_size = self._drag_start_size
        pressed_title_bar = self._pressed_title_bar
        self._drag_start_pos = None
        self._drag_start_size = None
        self._pressed_title_bar = False
        super().mouseReleaseEvent(event)
        if (
            event.button() != Qt.MouseButton.LeftButton
            or start_pos is None
            or start_size is None
            or not pressed_title_bar
            or self.isMaximized()
        ):
            return
        # 拉边缩放会改变窗口尺寸，且必须起源于标题栏而非任何缩放边框，防止贴顶拉边误弹出。
        if self.size() != start_size:
            return
        global_pos = event.globalPosition().toPoint()
        if self._should_detach_at(global_pos):
            QTimer.singleShot(
                0,
                lambda point=QPoint(global_pos): self.detachRequested.emit(point),
            )

    def _should_detach_at(self, global_pos: QPoint) -> bool:
        viewport = self._mdi.viewport()
        top_left = viewport.mapToGlobal(QPoint(0, 0))
        mdi_rect = QRect(top_left, viewport.size())
        expanded = mdi_rect.adjusted(
            -self._detach_margin,
            -self._detach_margin,
            self._detach_margin,
            self._detach_margin,
        )
        return not expanded.contains(global_pos)


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
        profile_path = ui_preference_store.path if ui_preference_store is not None else default_ui_preferences_path()
        self.alarm_settings_store = AlarmSettingsStore(profile_path.with_name("alarms.json"))
        self._startup_alarm_settings_error: str | None = None
        try:
            reporting_config = self.alarm_settings_store.load(config.alarms.reporting)
        except (OSError, ValueError, TypeError) as exc:
            self._startup_alarm_settings_error = str(exc)
            reporting_config = config.alarms.reporting
        config = replace(config, alarms=replace(config.alarms, reporting=reporting_config))
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
        # 运行中或等待 Apply 期间打开新 SEQ 时，不能改写已锁定的在运模块窗口；
        # 暂存导入值，待本次 Run 结束解锁后再加载进模块窗口并标为未 Apply。
        self._deferred_sequence_module_settings: dict[
            str,
            dict[str, object],
        ] = {}
        self._last_sequence_directory = self.config.project_root
        self._last_data_directory = self.config.resolve_project_path(self.config.logging.directory)
        self.current_data_file: Path | None = None
        self._datafile_selected = False
        self._effective_datafile_text = ""
        self.current_snapshots: dict[str, InstrumentSnapshot] = {}
        self._runtime_connection_summary: tuple[int, int] | None = None
        self.current_run_state = RunState.IDLE
        self._close_after_stop = False
        self.manual_dialogs: dict[str, ManualControlDialog] = {}
        self.system_log_dialog: SystemLogDialog | None = None
        self._system_log_state = {"active": False, "path": "", "interval_seconds": 30, "fields": []}
        self.instrument_setup_dialog: InstrumentSetupDialog | None = None
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
        self._pending_run: (
            tuple[SequenceDocument, dict[str, dict[str, object]], list[object]] | None
        ) = None
        # Enable/Disable 是提交到后台 event loop 的 Future。必须保留并收取异常，
        # 否则在后台尚未来得及发布最终 module_state 前失败时，复选框会永久停在
        # Initializing/Disabling，用户也看不到可重试的状态。
        self._pending_module_operations: dict[
            str,
            tuple[object, bool],
        ] = {}
        self._minimized_module_windows: set[str] = set()
        self._pending_manual_operations: list[tuple[object, str, ManualControlDialog | None]] = []
        self.alert_dialogs: dict[str, AlertDialog] = {}
        self.run_directory: Path | None = None
        self.trend_dialog = TrendDialog(self)
        self._dirty = False
        self._initializing_ui = True
        self._command_dock_user_hidden = False
        self._command_dock_sized = False
        self._last_active_subwindow: QMdiSubWindow | None = None
        self._unread_log_count = 0
        self._unread_log_level = ""
        self._high_field_target: tuple[str, str] | None = None
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
        if self._startup_alarm_settings_error:
            self._append_log(
                "WARNING",
                "alarm_settings",
                "ALARM_SETTINGS_LOAD_FAILED",
                (
                    f"Could not load local alarm settings from {self.alarm_settings_store.path}; "
                    f"falling back to TOML configuration: {self._startup_alarm_settings_error}"
                ),
            )
        self._restore_window_layout()
        self._apply_style()
        self._load_default_sequence()
        self._last_active_subwindow = self.mdi.activeSubWindow() or self.sequence_window
        self._initializing_ui = False
        self.runtime.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain_runtime_messages)
        self.timer.start(config.ui_refresh_ms)

    def _discover_module_descriptors(self) -> tuple[ModuleDescriptor, ...]:
        return discover_modules(self.config)

    def _build_ui(self) -> None:
        self.mdi = QMdiArea()
        self.mdi.setBackground(QColor("#96a0aa"))
        self.setCentralWidget(self.mdi)

        self.editor = SequenceEditorWidget(self.document)
        self.editor.commandDoubleClicked.connect(self._edit_command)
        self.editor.documentChanged.connect(self._mark_dirty)
        self.sequence_window = QMdiSubWindow()
        self.sequence_window.setWidget(self.editor)
        self.sequence_window.setFocusProxy(self.editor)
        self.sequence_window.setWindowTitle(self.document.name)
        self.sequence_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.mdi.addSubWindow(self.sequence_window)
        self.mdi.subWindowActivated.connect(self._sync_command_dock_for_subwindow)
        self.sequence_window.installEventFilter(self)
        self.editor.installEventFilter(self)
        self.editor.list.installEventFilter(self)
        self.editor.list.viewport().installEventFilter(self)

        self.mdi.subWindowActivated.connect(self._on_mdi_subwindow_activated)
        self.sequence_window.resize(scaled(780), scaled(560))
        self.sequence_window.show()

        self.data_browser_windows: dict[
            QMdiSubWindow | QMainWindow,
            DatBrowserWidget,
        ] = {}

        self._build_left_dock()
        self._build_command_dock()
        self._build_status_dock()
        self._build_log_dock()
        self._build_actions()
        self._build_status_bar_controls()

        self.editor.list.setFocus()
        self.statusBar().showMessage(
            f"Starting instrument runtime · UI scale {self.ui_scale:.2f}x "
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
        panel.setObjectName("leftDockPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(scaled(8), scaled(6), scaled(8), scaled(8))
        layout.setSpacing(scaled(6))

        data_group = QGroupBox("Data File")
        data_group.setObjectName("firstInspectorGroup")
        data_layout = QVBoxLayout(data_group)
        data_layout.setContentsMargins(0, scaled(4), 0, scaled(2))
        data_layout.setSpacing(scaled(4))
        self.data_file_label = ElidedLabel("")
        self.data_file_label.setObjectName("sidebarField")
        data_layout.addWidget(self.data_file_label)
        data_buttons = QHBoxLayout()
        data_buttons.setContentsMargins(0, 0, 0, 0)
        data_buttons.setSpacing(scaled(5))
        self.view_data_button = QPushButton("View")
        self.change_data_button = QPushButton("Change")
        self.view_data_button.clicked.connect(self._view_data)
        self.change_data_button.clicked.connect(self._change_datafile)
        data_buttons.addWidget(self.view_data_button)
        data_buttons.addWidget(self.change_data_button)
        data_layout.addLayout(data_buttons)
        layout.addWidget(data_group)

        sequence_group = QGroupBox("Sequence")
        sequence_layout = QVBoxLayout(sequence_group)
        sequence_layout.setContentsMargins(0, scaled(4), 0, scaled(2))
        sequence_layout.setSpacing(scaled(4))
        self.sequence_label = ElidedLabel("Untitled.seq")
        self.sequence_label.setObjectName("sidebarField")
        sequence_layout.addWidget(self.sequence_label)
        sequence_buttons = QHBoxLayout()
        sequence_buttons.setContentsMargins(0, 0, 0, 0)
        sequence_buttons.setSpacing(scaled(5))
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
        status_layout.setContentsMargins(0, scaled(4), 0, scaled(2))
        status_layout.setSpacing(scaled(4))
        self.run_status_label = QLabel("Sequence Idle")
        self.run_status_label.setObjectName("statusBadge")
        self.run_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.run_status_label.setProperty("runState", "idle")
        self.run_detail_label = QLabel("")
        self.run_detail_label.setWordWrap(True)
        self.run_detail_label.setObjectName("mutedLabel")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximumHeight(scaled(16))
        status_layout.addWidget(self.run_status_label)
        status_layout.addWidget(self.run_detail_label)
        status_layout.addWidget(self.progress_bar)
        layout.addWidget(status_group)

        self.measure_status_label = QLabel(
            f"0 of {len(self.module_descriptors)} measurement modules enabled"
        )
        self.measure_status_label.setObjectName("mutedLabel")
        self.measure_status_label.setWordWrap(True)
        self.module_monitor_group = ModuleMonitorPanel(panel)
        self.module_monitor_group.set_summary_widget(self.measure_status_label)
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
        run_buttons.setContentsMargins(0, scaled(2), 0, 0)
        run_buttons.setSpacing(scaled(5))
        self.run_button = QPushButton("Run")
        self.run_button.setObjectName("runPrimaryButton")
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("pauseButton")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("stopDangerButton")
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
        panel.setObjectName("commandDockPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(scaled(8), scaled(6), scaled(8), scaled(8))
        layout.setSpacing(scaled(5))
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
        self.command_tree.setObjectName("commandTree")
        self.command_tree.setHeaderHidden(True)
        self.command_tree.setIndentation(scaled(14))
        self.command_tree.setUniformRowHeights(True)
        self.command_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        groups: dict[str, QTreeWidgetItem] = {}
        for spec in COMMAND_SPECS:
            group = groups.get(spec.category)
            if group is None:
                group = QTreeWidgetItem([spec.category])
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                group_font = group.font(0)
                group_font.setBold(True)
                group.setFont(0, group_font)
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
        dock.hide()
        dock.toggleViewAction().triggered.connect(self._on_command_dock_user_toggled)
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
            group_font = group.font(0)
            group_font.setBold(True)
            group.setFont(0, group_font)
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
        hidden_title_bar = QWidget(dock)
        hidden_title_bar.setFixedHeight(0)
        dock.setTitleBarWidget(hidden_title_bar)
        panel = InstrumentPanelHost(
            self.config.instrument_instances,
            self.config.panels,
            self._instrument_sequence_commands,
            descriptors=self.runtime.instrument_descriptors,
            ui_api_factory=self._create_instrument_ui_api,
        )
        panel.controlRequested.connect(self._open_manual_control)
        panel.actionRequested.connect(self._manual_instrument_action)
        panel.setupRequested.connect(self._open_instrument_setup)
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
        dock.visibilityChanged.connect(self._on_log_dock_visibility_changed)
        self.log_dock = dock

    def _build_status_bar_controls(self) -> None:
        self.log_toggle_button = QToolButton(self)
        self.log_toggle_button.setObjectName("statusLogToggle")
        self.log_toggle_button.setCheckable(True)
        self.log_toggle_button.setAutoRaise(True)
        self.log_toggle_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.log_toggle_button.setIcon(
            qta.icon("fa5s.terminal", color="#475467")
        )
        self.log_toggle_button.setIconSize(QSize(scaled(13), scaled(13)))
        self.log_toggle_button.setText("Run Log")
        self.log_toggle_button.setToolTip("Show or hide the Run Log panel")
        self.log_toggle_button.clicked.connect(self._toggle_log_dock)
        self.statusBar().addPermanentWidget(self.log_toggle_button)
        self._refresh_log_toggle_button()

    def _build_actions(self) -> None:
        self.module_manager = ModuleManagerDialog(self.module_descriptors, self)
        self.module_manager.enableRequested.connect(self._set_module_enabled)
        self.module_manager.refreshRequested.connect(self._refresh_modules)
        self.module_manager.openRequested.connect(self._show_module_window)
        self.module_manager.addInstanceRequested.connect(self._add_module_instance)
        self.module_manager.removeInstanceRequested.connect(self._remove_module_instance)
        icon_color = "#3b4652"
        self.new_action = QAction(qta.icon("fa5s.file", color=icon_color), "New", self)
        self.new_action.setShortcuts(
            QKeySequence.keyBindings(QKeySequence.StandardKey.New)
            or [QKeySequence("Ctrl+N")]
        )
        self.open_action = QAction(qta.icon("fa5s.folder-open", color=icon_color), "Open", self)
        self.open_action.setShortcuts(
            QKeySequence.keyBindings(QKeySequence.StandardKey.Open)
            or [QKeySequence("Ctrl+O")]
        )
        self.save_action = QAction(qta.icon("fa5s.save", color=icon_color), "Save", self)
        self.save_action.setShortcuts(
            QKeySequence.keyBindings(QKeySequence.StandardKey.Save)
            or [QKeySequence("Ctrl+S")]
        )
        self.save_as_action = QAction(qta.icon("fa5s.file-signature", color=icon_color), "Save As", self)
        save_as_shortcuts = list(
            QKeySequence.keyBindings(QKeySequence.StandardKey.SaveAs)
        )
        if QKeySequence("Ctrl+Shift+S") not in save_as_shortcuts:
            save_as_shortcuts.insert(0, QKeySequence("Ctrl+Shift+S"))
        self.save_as_action.setShortcuts(save_as_shortcuts)
        self.run_action = QAction(qta.icon("fa5s.play", color="#238636"), "Run", self)
        self.pause_action = QAction(qta.icon("fa5s.pause", color="#b86b00"), "Pause/Resume", self)
        self.stop_action = QAction(qta.icon("fa5s.stop", color="#cf222e"), "Stop", self)
        self.graph_action = QAction(qta.icon("fa5s.chart-line", color=icon_color), "Live Trend", self)
        self.data_browser_action = QAction(qta.icon("fa5s.database", color=icon_color), "Data Browser", self)
        self.modules_action = QAction(qta.icon("fa5s.cubes", color=icon_color), "Modules", self)
        self.appearance_action = QAction("Appearance…", self)
        self.alarm_settings_action = QAction("Alarm Notifications…", self)
        self.alarm_settings_action.triggered.connect(self._show_alarm_settings)
        self.log_action = self.log_dock.toggleViewAction()
        self._system_log_idle_icon = qta.icon("fa5s.clipboard-list", color=icon_color)
        self._system_log_recording_icon = qta.icon("fa5s.dot-circle", color="#238636")
        self.system_log_action = QAction(self._system_log_idle_icon, "System Log…", self)
        self.system_log_action.triggered.connect(self._open_system_log)
        self.commands_toolbar_action = QAction(
            qta.icon("fa5s.list-ul", color=icon_color),
            "Commands",
            self,
        )
        self.commands_toolbar_action.setCheckable(True)
        self.commands_toolbar_action.setChecked(not self.command_dock.isHidden())
        self.commands_toolbar_action.setToolTip("Show or hide the Sequence Command Bar")
        self.commands_toolbar_action.triggered.connect(self._on_commands_toolbar_toggled)
        self.command_dock.visibilityChanged.connect(self._sync_commands_toolbar_state)
        self.high_field_action = QAction(
            qta.icon("fa5s.magnet", color="#b45309"),
            "",
            self,
        )
        self.high_field_action.setVisible(False)
        self.high_field_action.setToolTip(
            "Magnetic field exceeds 100 Oe — click to open manual field control"
        )
        self.high_field_action.triggered.connect(self._open_high_field_control)
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
        self.edit_menu = menu.addMenu("Edit")
        self.edit_menu.addActions([
            self.editor.undo_action,
            self.editor.redo_action,
        ])
        self.edit_menu.addSeparator()
        self.edit_menu.addActions([self.editor.disable_action, self.editor.enable_action])
        self.edit_menu.addSeparator()
        self.edit_menu.addActions([
            self.editor.cut_action,
            self.editor.copy_action,
            self.editor.paste_action,
            self.editor.delete_action,
        ])
        self.edit_menu.addSeparator()
        self.edit_menu.addActions([
            self.editor.move_up_action,
            self.editor.move_down_action,
        ])
        view_menu = menu.addMenu("View")
        view_menu.addActions([self.left_dock.toggleViewAction(), self.command_dock.toggleViewAction(), self.log_action])
        view_menu.addSeparator()
        view_menu.addAction(self.appearance_action)
        self.settings_menu = menu.addMenu("Settings")
        self.settings_menu.addAction(self.alarm_settings_action)
        sequence_menu = menu.addMenu("Sequence")
        sequence_menu.addActions([self.run_action, self.pause_action, self.stop_action])
        graph_menu = menu.addMenu("Graph")
        graph_menu.addActions([self.graph_action, self.data_browser_action])
        graph_menu.addAction(self.system_log_action)
        instrument_menu = menu.addMenu("Instrument")
        instrument_menu.addAction(self.system_log_action)
        instrument_menu.addSeparator()
        has_controller_menu = False
        for panel in self.config.panels:
            if panel.template != "controller":
                continue
            has_controller_menu = True
            instrument = self.config.instrument(panel.instrument_id)
            action = instrument_menu.addAction(
                f"{instrument.display_name} — {panel.display_name}"
            )
            action.triggered.connect(
                lambda checked=False, instrument_id=instrument.id, panel_id=panel.id: (
                    self._open_manual_control(instrument_id, panel_id)
                )
            )
        setup_entries = self._setup_capable_instruments()
        if has_controller_menu:
            instrument_menu.addSeparator()
        self.instrument_setup_action = instrument_menu.addAction("Instrument Setup…")
        self.instrument_setup_action.setEnabled(bool(setup_entries))
        self.instrument_setup_action.triggered.connect(
            lambda checked=False: self._open_instrument_setup()
        )
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
        self.window_menu = menu.addMenu("Window")
        self.tile_windows_action = self.window_menu.addAction("Tile Windows")
        self.tile_windows_action.triggered.connect(self.mdi.tileSubWindows)
        self.cascade_windows_action = self.window_menu.addAction("Cascade Windows")
        self.cascade_windows_action.triggered.connect(self.mdi.cascadeSubWindows)
        self.window_menu.addSeparator()
        focus_seq_action = self.window_menu.addAction("Focus Sequence")
        focus_seq_action.triggered.connect(self._focus_sequence)
        new_browser_action = self.window_menu.addAction("New Data Browser")
        new_browser_action.triggered.connect(lambda checked=False: self._show_data_browser())
        help_menu = menu.addMenu("Help")
        help_menu.addAction(self.about_action)

        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(scaled(18), scaled(18)))
        toolbar.addActions([self.new_action, self.open_action, self.save_action])
        toolbar.addSeparator()
        toolbar.addActions([self.run_action, self.pause_action, self.stop_action])
        toolbar.addSeparator()
        toolbar.addActions([self.graph_action, self.data_browser_action, self.modules_action])
        toolbar.addSeparator()
        toolbar.addAction(self.system_log_action)
        system_log_btn = toolbar.widgetForAction(self.system_log_action)
        if isinstance(system_log_btn, QToolButton):
            system_log_btn.setObjectName("systemLogToolButton")
            system_log_btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            system_log_btn.setProperty("recording", False)
            self._system_log_button: QToolButton | None = system_log_btn
        else:
            self._system_log_button = None
        toolbar.addSeparator()
        toolbar.addAction(self.commands_toolbar_action)
        commands_btn = toolbar.widgetForAction(self.commands_toolbar_action)
        if isinstance(commands_btn, QToolButton):
            commands_btn.setObjectName("commandsToolButton")
            commands_btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        toolbar_spacer = QWidget(toolbar)
        toolbar_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        toolbar.addWidget(toolbar_spacer)
        toolbar.addAction(self.high_field_action)
        high_field_btn = toolbar.widgetForAction(self.high_field_action)
        if isinstance(high_field_btn, QToolButton):
            high_field_btn.setObjectName("highFieldToolButton")
            high_field_btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            self._high_field_button: QToolButton | None = high_field_btn
        else:
            self._high_field_button = None
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
        docked_browsers = [
            window
            for window in self.data_browser_windows
            if isinstance(window, QMdiSubWindow)
        ]
        if docked_browsers:
            store.set_rect(
                "data_browser",
                docked_browsers[-1].geometry(),
            )
        floating_browsers = [
            window
            for window in self.data_browser_windows
            if isinstance(window, QMainWindow)
        ]
        if floating_browsers:
            store.set_geometry(
                "data_browser_floating",
                floating_browsers[-1].saveGeometry(),
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
        if self.instrument_setup_dialog is not None:
            store.set_geometry(
                "instrument_setup",
                self.instrument_setup_dialog.saveGeometry(),
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
        status_size = scaled_text(14)
        status_padding_v = scaled(5)
        status_padding_h = scaled(8)
        status_radius = scaled(5)
        section_size = scaled_text(11)
        section_margin = scaled(14)
        first_section_margin = scaled(11)
        tile_title_size = scaled_text(18)
        tile_value_size = scaled_text(27)
        tile_detail_size = scaled_text(16)
        manual_size = scaled_text(30)
        manual_padding = scaled(15)
        toolbar_pad_v = scaled(3)
        toolbar_pad_h = scaled(6)
        self.setStyleSheet(
            "QLabel#mutedLabel { color: #59636e; }"
            "QToolBar#mainToolbar {"
            "  background: #f7f9fb;"
            "  border: none;"
            "  border-bottom: 1px solid #d5dce4;"
            f"  spacing: {scaled(3)}px;"
            f"  padding: {scaled(2)}px {scaled(6)}px;"
            "}"
            "QToolBar#mainToolbar::separator {"
            "  background: #d0d7de;"
            "  width: 1px;"
            f"  margin: {scaled(4)}px {scaled(4)}px;"
            "}"
            "QToolBar#mainToolbar QToolButton {"
            "  border: 1px solid transparent;"
            f"  border-radius: {scaled(4)}px;"
            f"  padding: {toolbar_pad_v}px {toolbar_pad_h}px;"
            "  color: #24292f;"
            "}"
            "QToolBar#mainToolbar QToolButton:hover {"
            "  background: #e9eff6;"
            "  border-color: #cfd8e3;"
            "}"
            "QToolBar#mainToolbar QToolButton:checked {"
            "  background: #dce9f9;"
            "  border-color: #8bb4e6;"
            "  color: #174a8b;"
            "}"
            "QToolBar#mainToolbar QToolButton#systemLogToolButton {"
            "  background: #ffffff;"
            "  border: 1px solid #d0d7de;"
            f"  padding: {toolbar_pad_v}px {scaled(8)}px;"
            "}"
            "QToolBar#mainToolbar QToolButton#systemLogToolButton:hover {"
            "  background: #eef3f8;"
            "  border-color: #aeb9c6;"
            "}"
            "QToolBar#mainToolbar QToolButton#systemLogToolButton[recording=\"true\"] {"
            "  background: #eaf7ef;"
            "  border-color: #86c99a;"
            "  color: #1b7a3d;"
            "  font-weight: 600;"
            "}"
            "QToolBar#mainToolbar QToolButton#systemLogToolButton[recording=\"true\"]:hover {"
            "  background: #dbf2e3;"
            "  border-color: #5cb87a;"
            "}"
            "QToolBar#mainToolbar QToolButton#highFieldToolButton {"
            "  background: #fef3c7;"
            "  border: 1px solid #d97706;"
            "  color: #92400e;"
            "  font-weight: bold;"
            f"  padding: {toolbar_pad_v}px {scaled(10)}px;"
            "}"
            "QToolBar#mainToolbar QToolButton#highFieldToolButton:hover {"
            "  background: #fde68a;"
            "  border-color: #b45309;"
            "}"
            "QDockWidget::title {"
            "  background: #eef2f6;"
            "  color: #344054;"
            "  font-weight: 600;"
            f"  padding: {scaled(5)}px {scaled(8)}px;"
            "  border-bottom: 1px solid #d5dce4;"
            "}"
            "QWidget#leftDockPanel {"
            "  background: #f5f7fa;"
            "}"
            "QDockWidget#sequenceControlDock QGroupBox {"
            "  border: none;"
            "  border-top: 1px solid #d5dce4;"
            f"  margin-top: {section_margin}px;"
            f"  padding-top: {scaled(4)}px;"
            f"  font-size: {section_size}px;"
            "  font-weight: 600;"
            "  color: #475467;"
            "  background: transparent;"
            "}"
            "QDockWidget#sequenceControlDock QGroupBox#firstInspectorGroup {"
            "  border-top: none;"
            f"  margin-top: {first_section_margin}px;"
            "}"
            "QDockWidget#sequenceControlDock QGroupBox::title {"
            "  subcontrol-origin: margin;"
            "  subcontrol-position: top left;"
            "  left: 0px;"
            "  top: 0px;"
            "  padding: 0 4px 0 0;"
            "  background: #f5f7fa;"
            "}"
            "QLabel#sidebarField {"
            "  background: #ffffff;"
            "  border: 1px solid #d5dce4;"
            f"  border-radius: {scaled(4)}px;"
            f"  padding: {scaled(3)}px {scaled(6)}px;"
            "  color: #1f2328;"
            "}"
            "QWidget#leftDockPanel QPushButton {"
            "  background: #ffffff;"
            "  color: #24292f;"
            "  border: 1px solid #c6cfd9;"
            f"  border-radius: {scaled(4)}px;"
            f"  padding: {scaled(4)}px {scaled(8)}px;"
            "}"
            "QWidget#leftDockPanel QPushButton:hover {"
            "  background: #eef3f8;"
            "  border-color: #9fb0c3;"
            "}"
            "QWidget#leftDockPanel QPushButton:pressed {"
            "  background: #e1e8f0;"
            "}"
            "QWidget#leftDockPanel QPushButton:disabled {"
            "  background: #edf0f3;"
            "  color: #8c959f;"
            "  border-color: #d8dee6;"
            "}"
            "QWidget#leftDockPanel QPushButton#runPrimaryButton:enabled {"
            "  background: #238636;"
            "  border-color: #1e732e;"
            "  color: #ffffff;"
            "  font-weight: 600;"
            "}"
            "QWidget#leftDockPanel QPushButton#runPrimaryButton:hover:enabled {"
            "  background: #1f7730;"
            "}"
            "QWidget#leftDockPanel QPushButton#pauseButton:enabled {"
            "  background: #fff8e6;"
            "  border-color: #d49a2a;"
            "  color: #8a5700;"
            "  font-weight: 600;"
            "}"
            "QWidget#leftDockPanel QPushButton#pauseButton:hover:enabled {"
            "  background: #ffefc7;"
            "}"
            "QWidget#leftDockPanel QPushButton#stopDangerButton:enabled {"
            "  background: #fdecec;"
            "  border-color: #d95c65;"
            "  color: #b42318;"
            "  font-weight: 600;"
            "}"
            "QWidget#leftDockPanel QPushButton#stopDangerButton:hover:enabled {"
            "  background: #fad8d8;"
            "}"
            f"QLabel#statusBadge {{ font-size: {status_size}px; font-weight: bold; padding: {status_padding_v}px {status_padding_h}px; border-radius: {status_radius}px; border: 1px solid #d0d7de; background: #e9eef3; color: #344054; }}"
            "QLabel#statusBadge[runState=\"running\"] { background: #e8f1fc; border-color: #9dc0ee; color: #175cd3; }"
            "QLabel#statusBadge[runState=\"paused\"] { background: #fef3c7; border-color: #e4b65c; color: #92400e; }"
            "QLabel#statusBadge[runState=\"completed\"] { background: #eaf7ef; border-color: #86c99a; color: #1b7a3d; }"
            "QLabel#statusBadge[runState=\"faulted\"] { background: #fdecec; border-color: #e5989e; color: #b42318; }"
            "QLabel#statusBadge[runState=\"applying\"] { background: #eef2ff; border-color: #a5b4fc; color: #3730a3; }"
            "QWidget#leftDockPanel QProgressBar {"
            "  border: 1px solid #cfd7e0;"
            f"  border-radius: {scaled(4)}px;"
            "  background: #ffffff;"
            "  text-align: center;"
            "  color: #24292f;"
            "}"
            "QWidget#leftDockPanel QProgressBar::chunk {"
            "  background: #2f6fbb;"
            f"  border-radius: {scaled(3)}px;"
            "}"
            "QWidget#commandDockPanel {"
            "  background: #f5f7fa;"
            "}"
            "QTreeWidget#commandTree {"
            "  background: #ffffff;"
            "  border: 1px solid #d5dce4;"
            f"  border-radius: {scaled(4)}px;"
            f"  padding: {scaled(2)}px;"
            "}"
            "QTreeWidget#commandTree::item {"
            f"  padding: {scaled(3)}px {scaled(4)}px;"
            "}"
            "QTreeWidget#commandTree::item:hover {"
            "  background: #eef4fb;"
            "}"
            "QStatusBar {"
            "  background: #f5f7fa;"
            "  border-top: 1px solid #d5dce4;"
            "  color: #475467;"
            "}"
            "QToolButton#statusLogToggle {"
            "  border: 1px solid transparent;"
            f"  border-radius: {scaled(4)}px;"
            f"  padding: {scaled(1)}px {scaled(7)}px;"
            "  color: #344054;"
            "}"
            "QToolButton#statusLogToggle:hover {"
            "  background: #e6edf5;"
            "  border-color: #c5d0dc;"
            "}"
            "QToolButton#statusLogToggle:checked {"
            "  background: #dce9f9;"
            "  border-color: #8bb4e6;"
            "  color: #174a8b;"
            "}"
            "QToolButton#statusLogToggle[logSeverity=\"warning\"] {"
            "  background: #fff6df;"
            "  border-color: #dfb457;"
            "  color: #8a5700;"
            "  font-weight: 600;"
            "}"
            "QToolButton#statusLogToggle[logSeverity=\"error\"] {"
            "  background: #fdecec;"
            "  border-color: #e49aa0;"
            "  color: #b42318;"
            "  font-weight: 600;"
            "}"
            f"QLabel#panelTitle {{ font-weight: bold; font-size: {tile_title_size}px; }}"
            f"QLabel#panelValue {{ font-size: {tile_value_size}px; font-weight: bold; }}"
            f"QLabel#panelDetail {{ color: #59636e; font-size: {tile_detail_size}px; }}"
            f"QLabel#readoutName {{ font-size: {tile_detail_size}px; }}"
            f"QLabel#readoutValue {{ font-size: {tile_title_size}px; font-weight: bold; }}"
            f"QLabel#manualCurrent {{ font-size: {manual_size}px; font-weight: bold; padding: {manual_padding}px; }}"
            "QGroupBox { font-weight: bold; }"
            "QListView::item:selected, QTreeView::item:selected { background: #d6e8ff; color: #1f2328; }"
        )

    def _show_alarm_settings(self) -> None:
        try:
            dialog = AlarmSettingsDialog(self.runtime, self.alarm_settings_store,
                                         self.config.alarms.reporting, self)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Alarm Settings Failed", str(error))
            return
        dialog.settingsSaved.connect(self._alarm_settings_saved)
        dialog.exec()

    def _alarm_settings_saved(self, config) -> None:
        self.config = replace(self.config, alarms=replace(self.config.alarms, reporting=config))

    def _load_default_sequence(self) -> None:
        store = self.ui_preference_store
        remembered = store.last_sequence() if store is not None else None
        path = remembered if remembered is not None and remembered.is_file() else None
        if path is None and self.config.default_sequence:
            path = self.config.resolve_project_path(self.config.default_sequence)
        if remembered is not None and not remembered.is_file():
            self._append_log("WARNING", "sequence", "LAST_SEQUENCE_MISSING", f"Last SEQ is unavailable: {remembered}; using the configured default")
        if path is not None and path.is_file():
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
        self.current_data_file = None
        self._datafile_selected = False
        self._effective_datafile_text = ""
        self.sequence_path = document.path
        self._datafile_selected = self._first_effective_datafile_command() is not None
        if self.ui_preference_store is not None:
            self.ui_preference_store.set_last_sequence(document.path)
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
        self._deferred_sequence_module_settings.clear()
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
        # 运行中或正在 Apply 模块准备 Run 时，模块窗口保持锁定，暂存导入值并在 Run 结束后加载。
        run_locked = (
            self.current_run_state not in self.TERMINAL_STATES
            or self._pending_run is not None
        )
        for module_id, settings in (
            self._sequence_module_settings.items()
        ):
            if module_id not in self.enabled_modules:
                continue
            window = self.module_windows.get(module_id)
            if window is None:
                continue
            if run_locked:
                self._deferred_sequence_module_settings[module_id] = deepcopy(
                    settings
                )
                self._sequence_module_window_issues.append(
                    f"Module {module_id!r} window is locked during an active Run; "
                    "imported settings were preserved and will load when this Run finishes"
                )
                continue
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

    def _datafile_outcome(
        self,
        commands: list[Command],
        parent_enabled: bool,
        current_base: Path,
        stack: set[Path],
        *,
        in_main: bool,
    ) -> tuple[str, Command | None]:
        """按执行顺序检查首次有效 Measure 之前是否已执行有效 Set Datafile。"""

        instrument_commands = getattr(self, "_instrument_sequence_commands", ())
        for command in commands:
            effective = parent_enabled and command.enabled
            if not effective:
                continue
            if command.type is CommandType.SET_DATAFILE:
                return ("datafile", command if in_main else None)
            if command.type is CommandType.MEASURE:
                return ("measure", None)
            if command.type is CommandType.CALL_SEQUENCE:
                requested = str(command.params.get("path", "")).strip()
                if requested:
                    source = Path(requested)
                    if not source.is_absolute():
                        source = (current_base / source).resolve()
                    if source not in stack and source.is_file():
                        try:
                            parsed = load_sequence(
                                source,
                                instrument_commands=instrument_commands,
                            )
                        except OSError:
                            parsed = None
                        if parsed is not None and not parsed.has_errors:
                            outcome, _ = self._datafile_outcome(
                                parsed.document.commands,
                                True,
                                source.parent,
                                stack | {source},
                                in_main=False,
                            )
                            if outcome == "measure":
                                return ("measure", None)
                            if outcome == "datafile":
                                continue
            if command.children:
                outcome, found = self._datafile_outcome(
                    command.children,
                    effective,
                    current_base,
                    stack,
                    in_main=in_main,
                )
                if outcome != "none":
                    return (outcome, found)
        return ("none", None)

    def _first_effective_datafile_command(self) -> Command | None:
        sequence_path = getattr(self, "sequence_path", None)
        base_directory = (
            sequence_path.resolve().parent
            if sequence_path is not None
            else self.config.project_root
        )
        call_stack: set[Path] = (
            {sequence_path.resolve()} if sequence_path is not None else set()
        )
        outcome, found = self._datafile_outcome(
            self.document.commands,
            True,
            base_directory,
            call_stack,
            in_main=True,
        )
        return found if outcome == "datafile" else None

    def _has_any_effective_datafile_command(
        self,
        commands: list[Command] | None = None,
        parent_enabled: bool = True,
    ) -> bool:
        for command in self.document.commands if commands is None else commands:
            effective = parent_enabled and command.enabled
            if not effective:
                continue
            if command.type is CommandType.SET_DATAFILE:
                return True
            if self._has_any_effective_datafile_command(command.children, effective):
                return True
        return False

    def _top_level_datafile_before_measure(self) -> Command | None:
        """返回位于首次有效 Measure 之前的首个顶层 Set Datafile（含禁用态以便 Change 启用）。"""

        sequence_path = getattr(self, "sequence_path", None)
        base_directory = (
            sequence_path.resolve().parent
            if sequence_path is not None
            else self.config.project_root
        )
        call_stack: set[Path] = (
            {sequence_path.resolve()} if sequence_path is not None else set()
        )
        for command in self.document.commands:
            if command.type is CommandType.SET_DATAFILE:
                return command
            if not command.enabled:
                continue
            outcome, _ = self._datafile_outcome(
                [command],
                True,
                base_directory,
                call_stack,
                in_main=True,
            )
            if outcome == "measure":
                return None
        return None

    def _sync_datafile_label(self) -> None:
        command = self._first_effective_datafile_command()
        if command is not None:
            path_text = str(command.params.get("path", "experiment.dat")).strip()
            if path_text:
                if path_text != self._effective_datafile_text:
                    self._effective_datafile_text = path_text
                    self.current_data_file = None
                self._datafile_selected = True
                self.data_file_label.setFullText(path_text)
                path = Path(path_text)
                if path.is_absolute():
                    self._last_data_directory = path.parent
                return
        self._effective_datafile_text = ""
        self._datafile_selected = False
        self.current_data_file = None
        self.data_file_label.setFullText("")

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
        if self.ui_preference_store is not None:
            self.ui_preference_store.set_last_sequence(destination)
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
        if not value:
            return
        if not isinstance(value, tuple) or not value:
            return
        if value[0] == "instrument" and len(value) == 3:
            spec = self._instrument_sequence_command_specs.get(
                (str(value[1]), str(value[2]))
            )
            if spec is not None:
                self.editor.insert_command(spec.create())
                self.editor.list.setFocus()
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
                self.editor.list.setFocus()
            return
        dialog = CommandDialog(
            command,
            spec,
            self,
            instrument_configs=self.config.instrument_instances,
            data_directory=self._last_data_directory,
            module_settings=(
                self._module_settings(spec.module_id)
                if isinstance(spec, ModuleCommandSpec)
                else None
            ),
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
                self.editor.list.setFocus()
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
                self.editor.record_undo_state()
                command.update_params(values)
                self.editor.rebuild(command.id)
                self._mark_dirty()
                self.editor.list.setFocus()
            return
        try:
            dialog = CommandDialog(
                command,
                spec,
                self,
                instrument_configs=self.config.instrument_instances,
                data_directory=self._last_data_directory,
                module_settings=(
                    self._module_settings(spec.module_id)
                    if isinstance(spec, ModuleCommandSpec)
                    else None
                ),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Command Parameters", str(exc))
            return
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
                self.editor.record_undo_state()
                command.update_params(values)
                self._remember_datafile_directory(command)
                self.editor.rebuild(command.id)
                self._mark_dirty()
                self.editor.list.setFocus()
        finally:
            dialog.deleteLater()

    def _module_settings(self, module_id: str) -> dict[str, object] | None:
        window = self.module_windows.get(module_id)
        if window is None:
            return None
        try:
            return window.settings()
        except Exception:
            return None

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

        if command.type is CommandType.SET_DATAFILE:
            self._datafile_selected = bool(str(command.params["path"]).strip())
            self.current_data_file = None
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
            "Select DAT File (append or create)",
            str(self._last_data_directory),
            "Data (*.dat)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not selected:
            return
        selected_path = Path(selected)
        if selected_path.suffix.lower() != ".dat":
            selected_path = selected_path.with_suffix(".dat")
        selected = str(selected_path.resolve())
        self._last_data_directory = selected_path.resolve().parent
        self.editor.record_undo_state()
        command = self._top_level_datafile_before_measure()
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
        command.enabled = True
        self.editor.rebuild(command.id)
        self._datafile_selected = True
        self.current_data_file = None
        self._mark_dirty()
        self.editor.list.setFocus()

    def _view_data(self) -> None:
        path = self.current_data_file
        if path is None:
            displayed = Path(self.data_file_label.fullText())
            if displayed.is_file():
                path = displayed
        self._show_data_browser(path)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if not self._initializing_ui and watched in {
            self.sequence_window,
            self.editor,
            self.editor.list,
            self.editor.list.viewport(),
        }:
            event_type = event.type()
            if event_type == QEvent.Type.MouseButtonPress:
                self._expand_command_dock_for_sequence()
            elif event_type == QEvent.Type.FocusIn and isinstance(event, QFocusEvent):
                if event.reason() in {
                    Qt.FocusReason.MouseFocusReason,
                    Qt.FocusReason.TabFocusReason,
                    Qt.FocusReason.BacktabFocusReason,
                }:
                    self._expand_command_dock_for_sequence()
        return super().eventFilter(watched, event)

    def _show_command_dock(self) -> None:
        was_hidden = self.command_dock.isHidden()
        self.command_dock.show()
        if was_hidden and not self._command_dock_sized:
            self._command_dock_sized = True
            self.resizeDocks(
                [self.command_dock],
                [scaled(268)],
                Qt.Orientation.Horizontal,
            )

    def _expand_command_dock_for_sequence(self) -> None:
        if self._initializing_ui or self._command_dock_user_hidden:
            return
        self._show_command_dock()

    def _sync_command_dock_for_subwindow(
        self,
        subwindow: QMdiSubWindow | None,
    ) -> None:
        if self._initializing_ui:
            return
        previous = self._last_active_subwindow
        self._last_active_subwindow = subwindow
        if subwindow is self.sequence_window:
            if previous is not self.sequence_window:
                self._command_dock_user_hidden = False
                self._show_command_dock()
        elif subwindow is not None:
            self.command_dock.hide()

    def _on_command_dock_user_toggled(self, checked: bool) -> None:
        if checked:
            self._command_dock_user_hidden = False
            self._show_command_dock()
        else:
            if self.mdi.activeSubWindow() is self.sequence_window:
                self._command_dock_user_hidden = True
            self.command_dock.hide()

    def _on_commands_toolbar_toggled(self, checked: bool) -> None:
        self._on_command_dock_user_toggled(checked)

    def _sync_commands_toolbar_state(self, visible: bool) -> None:
        if hasattr(self, "commands_toolbar_action"):
            self.commands_toolbar_action.setChecked(visible)

    def _toggle_log_dock(self, checked: bool) -> None:
        self.log_dock.setVisible(checked)

    def _on_log_dock_visibility_changed(self, visible: bool) -> None:
        if visible:
            self._unread_log_count = 0
            self._unread_log_level = ""
        self._refresh_log_toggle_button()

    def _refresh_log_toggle_button(self) -> None:
        if not hasattr(self, "log_toggle_button"):
            return
        visible = not self.log_dock.isHidden()
        self.log_toggle_button.blockSignals(True)
        self.log_toggle_button.setChecked(visible)
        self.log_toggle_button.blockSignals(False)
        severity = ""
        if visible or self._unread_log_count == 0:
            self.log_toggle_button.setText("Run Log")
        elif self._unread_log_level == "ERROR":
            severity = "error"
            self.log_toggle_button.setText(f"Run Log ({self._unread_log_count} err)")
        elif self._unread_log_level == "WARNING":
            severity = "warning"
            self.log_toggle_button.setText(f"Run Log ({self._unread_log_count} warn)")
        else:
            self.log_toggle_button.setText(f"Run Log ({self._unread_log_count})")
        if self.log_toggle_button.property("logSeverity") != severity:
            self.log_toggle_button.setProperty("logSeverity", severity)
            self.log_toggle_button.style().unpolish(self.log_toggle_button)
            self.log_toggle_button.style().polish(self.log_toggle_button)

    def _set_run_status_badge(self, text: str, state: str) -> None:
        self.run_status_label.setText(text)
        if self.run_status_label.property("runState") != state:
            self.run_status_label.setProperty("runState", state)
            self.run_status_label.style().unpolish(self.run_status_label)
            self.run_status_label.style().polish(self.run_status_label)

    def _update_system_log_indicator(self, active: bool) -> None:
        self.system_log_action.setText(
            "System Log (Recording)…" if active else "System Log…"
        )
        self.system_log_action.setIcon(
            self._system_log_recording_icon if active else self._system_log_idle_icon
        )
        if getattr(self, "_system_log_button", None) is not None:
            self._system_log_button.setProperty("recording", active)
            self._system_log_button.style().unpolish(self._system_log_button)
            self._system_log_button.style().polish(self._system_log_button)

    def _on_mdi_subwindow_activated(self, subwindow: QMdiSubWindow | None) -> None:
        if subwindow is self.sequence_window and self.editor.isVisible():
            self.editor.list.setFocus()

    def _focus_sequence(self) -> None:
        self.sequence_window.showNormal()
        # QMdiSubWindow.close() also hides its child widget even when the
        # subwindow itself is retained, so both layers must be restored.
        self.editor.show()
        self.sequence_window.show()
        self.mdi.setActiveSubWindow(self.sequence_window)
        self.sequence_window.raise_()
        if not self._initializing_ui:
            self._command_dock_user_hidden = False
            self._show_command_dock()

        self.editor.list.setFocus()
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
        if (
            not self._datafile_selected
            or self._first_effective_datafile_command() is None
            or not self.data_file_label.fullText().strip()
        ):
            has_later_datafile = self._has_any_effective_datafile_command()
            message = (
                "Move an enabled Set Datafile before the first Measure "
                "(or click Change to set one at the start of the sequence)."
                if has_later_datafile
                else "Select a DAT file before Run. Existing files will be appended; a new path creates a file."
            )
            QMessageBox.warning(
                self,
                "Select Data File",
                message,
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
                self._pending_run = (
                    deepcopy(self.document),
                    module_settings,
                    futures,
                )
                self._set_runtime_editable(False)
                self.run_button.setEnabled(False)
                self._set_run_status_badge("Applying Module Settings", "applying")
                self.statusBar().showMessage("Applying module settings before Run...")
                return
            assert message.clickedButton() is run_button
        self._start_sequence(module_settings)

    def _start_sequence(
        self,
        module_settings: dict[str, dict[str, object]],
        document: SequenceDocument | None = None,
    ) -> None:
        self._close_after_stop = False
        self.run_directory = None
        target_document = self.document if document is None else document
        self.runtime.run_sequence(target_document, module_settings)
        self._set_runtime_editable(False)
        self.current_run_state = RunState.RUNNING
        self._set_run_status_badge("Sequence Running", "running")
        self.run_detail_label.setText("Running a snapshot; SEQ edits apply to the next Run")
        self.run_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)

    def _pause_or_resume(self) -> None:
        if self.current_run_state is RunState.PAUSED:
            self.runtime.resume_sequence()
        elif self.current_run_state is RunState.RUNNING:
            self.runtime.pause_sequence()

    def _set_runtime_editable(self, editable: bool) -> None:
        self.editor.set_editable(True)
        self.command_tree.setEnabled(True)
        self.change_sequence_button.setEnabled(True)
        self.change_data_button.setEnabled(True)
        self.editor.setToolTip("" if editable else "Running an independent snapshot; edits apply to the next Run.")
        self.modules_action.setEnabled(editable)
        self.module_manager.set_operations_enabled(editable)
        for window in self.module_windows.values():
            window.set_sequence_running(not editable)
        for dialog in self.manual_dialogs.values():
            dialog.set_runtime_editable(editable)
        if self.instrument_setup_dialog is not None:
            self.instrument_setup_dialog.set_runtime_editable(editable)
        self.status_panel.set_actions_enabled(editable)

    def _drain_runtime_messages(self) -> None:
        for message in self.runtime.drain_messages():
            if message.kind == "snapshots":
                self._handle_snapshots(message.payload)
            elif message.kind == "event":
                self._handle_event(message.payload)
            elif message.kind == "progress":
                self._handle_progress(message.payload)
            elif message.kind == "system_log_state":
                self._system_log_state = message.payload
                self._update_system_log_indicator(bool(message.payload["active"]))
                if self.system_log_dialog is not None:
                    self.system_log_dialog.set_state(message.payload)
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
                self.statusBar().showMessage(
                    f"Runtime startup failed: {message.payload}"
                )
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
        remaining: list[tuple[object, str, ManualControlDialog | None]] = []
        for future, success_message, dialog in self._pending_manual_operations:
            if not future.done():
                remaining.append((future, success_message, dialog))
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
                if dialog is not None:
                    dialog.accept()
        self._pending_manual_operations = remaining

    def _check_pending_run(self) -> None:
        if self._pending_run is None:
            return
        document, settings, futures = self._pending_run
        if not all(future.done() for future in futures):
            return
        self._pending_run = None
        errors = [future.exception() for future in futures if future.exception() is not None]
        if errors:
            self._set_runtime_editable(True)
            self._apply_deferred_sequence_module_settings()
            self._update_run_availability()
            self._set_run_status_badge("Sequence Idle", "idle")
            self.statusBar().showMessage("Run cancelled because module settings could not be applied", 5000)
            return
        for module_id in self.enabled_modules:
            window = self.module_windows.get(module_id)
            if window is not None and window.has_unapplied_edits():
                window.mark_applied()
        self._start_sequence(settings, document=document)

    def _handle_snapshots(self, snapshots: dict[str, InstrumentSnapshot]) -> None:
        self.current_snapshots = snapshots
        configured_ids = {
            instrument.id
            for instrument in self.config.instrument_instances
        }
        connected_count = sum(
            snapshot.connected
            for instrument_id, snapshot in snapshots.items()
            if instrument_id in configured_ids
        )
        connection_summary = (
            connected_count,
            len(configured_ids),
        )
        if connection_summary != self._runtime_connection_summary:
            self._runtime_connection_summary = connection_summary
            runtime_state = (
                "Ready"
                if connected_count == len(configured_ids)
                else "Ready with instrument warnings"
            )
            self.statusBar().showMessage(
                f"{runtime_state} · {connected_count}/{len(configured_ids)} "
                f"instruments connected · UI scale {self.ui_scale:.2f}x "
                f"({self.ui_scale_mode}) · text {self.font_scale:.0%}"
            )
        for instrument_id, snapshot in snapshots.items():
            self.status_panel.update_snapshot(snapshot)
            for dialog in self.manual_dialogs.values():
                if dialog.config.id == instrument_id:
                    dialog.update_snapshot(snapshot)
            if self.instrument_setup_dialog is not None:
                self.instrument_setup_dialog.update_snapshot(snapshot)
        self.trend_dialog.add_snapshots(snapshots)
        self._update_high_field_indicator(snapshots)
        self._update_run_availability()

    def _open_high_field_control(self) -> None:
        if self._high_field_target is not None:
            self._open_manual_control(*self._high_field_target)

    def _update_high_field_indicator(
        self,
        snapshots: dict[str, InstrumentSnapshot],
    ) -> None:
        """当任一已连接磁场面板读数超过 100 Oe 时，在顶部工具栏显示强磁提示胶囊。"""

        active_fields: list[tuple[float, str, tuple[str, str] | None]] = []
        for panel in self.config.panels:
            if not panel.enabled or panel.template not in {"controller", "readout"}:
                continue
            snapshot = snapshots.get(panel.instrument_id)
            if snapshot is None or not snapshot.connected:
                continue
            instrument = self.config.instrument(panel.instrument_id)
            is_field = panel.role == "field" or snapshot.kind is InstrumentKind.FIELD
            if not is_field:
                continue
            if panel.template == "controller":
                control_state = snapshot.controls.get(panel.id)
                value = None if control_state is None else control_state.current
                reading = instrument.reading(panel.reading)
                target_key: tuple[str, str] | None = (instrument.id, panel.id)
            else:
                reading_key = panel.readings[0] if panel.readings else instrument.main_reading
                reading = instrument.reading(reading_key)
                if reading_key == instrument.main_reading:
                    raw = snapshot.current
                else:
                    metric = snapshot.metrics.get(reading_key)
                    raw = None if metric is None else metric.value
                value = (
                    float(raw)
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool)
                    else None
                )
                target_key = None
            magnitude_oe = field_magnitude_oe(value, reading.unit)
            if magnitude_oe is None or magnitude_oe <= HIGH_FIELD_WARNING_OE or value is None:
                continue
            precision = (
                reading.decimals
                if reading.decimals is not None
                else control_decimals(InstrumentKind.FIELD, reading.unit)
            )
            label = f"Field {fixed_number(value, precision)} {reading.unit}"
            active_fields.append((magnitude_oe, label, target_key))

        if not active_fields:
            self._high_field_target = None
            self.high_field_action.setVisible(False)
            self.high_field_action.setText("")
            return

        active_fields.sort(key=lambda item: item[0], reverse=True)
        _, strongest_label, strongest_target = active_fields[0]
        if len(active_fields) > 1:
            strongest_label += f" (+{len(active_fields) - 1})"
        self._high_field_target = strongest_target
        self.high_field_action.setText(strongest_label)
        self.high_field_action.setToolTip(
            "Magnetic field exceeds 100 Oe — click to open manual field control"
            if strongest_target is not None
            else "Magnetic field exceeds 100 Oe"
        )
        self.high_field_action.setVisible(True)

    def _update_run_availability(self) -> None:
        if (
            self.current_run_state not in self.TERMINAL_STATES
            or self._pending_run is not None
        ):
            self.run_button.setEnabled(False)
            return
        reason = ""
        for config in self.config.instrument_instances:
            if not any(
                panel.enabled
                and panel.template == "controller"
                and panel.role != "none"
                for panel in config.panels
            ):
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
        elif event.code == "DATAFILE_SELECTED" and not notice.is_resolution:
            self.current_data_file = Path(event.message)
            self.data_file_label.setFullText(
                str(self.current_data_file)
            )
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
        self.editor.set_running_command(
            "" if progress.state in self.TERMINAL_STATES else progress.command_id
        )
        self._set_run_status_badge(
            f"Sequence {progress.state.value.title()}",
            progress.state.value,
        )
        if self._close_after_stop and progress.state not in self.TERMINAL_STATES:
            self.run_detail_label.setText(
                "Stopping sequence and waiting for module cleanup before closing"
            )
        else:
            self.run_detail_label.setText(progress.message)
        if progress.total_steps:
            self.progress_bar.setValue(min(100, int(progress.completed_steps / progress.total_steps * 100)))
        self.statusBar().showMessage(progress.step_path or progress.message)
        if progress.state in self.TERMINAL_STATES:
            self._set_runtime_editable(True)
            self._apply_deferred_sequence_module_settings()
            self._update_run_availability()
            self.pause_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.pause_button.setText("Pause")
            if progress.state is RunState.COMPLETED:
                self.progress_bar.setValue(100)
            if self._close_after_stop:
                self._close_after_stop = False
                if progress.state is not RunState.FAULTED:
                    QTimer.singleShot(0, self.close)
        elif progress.state is RunState.PAUSED:
            self.pause_button.setText("Resume")
        else:
            self.pause_button.setText("Pause")

    def _append_log(self, level: str, source: str, code: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{timestamp}  {level:<8}  {source}/{code}  {message}")
        if self.log_dock.isHidden():
            self._unread_log_count += 1
            upper = level.upper()
            rank = {"ERROR": 3, "WARNING": 2, "INFO": 1}
            if rank.get(upper, 0) > rank.get(self._unread_log_level, 0):
                self._unread_log_level = upper
            self._refresh_log_toggle_button()

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
            self.module_monitor_group.set_window_hidden(
                module_id,
                bool(
                    window is not None
                    and window.isHidden()
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

    def _apply_deferred_sequence_module_settings(self) -> None:
        """在 Run 结束并解锁模块窗口后，把运行中加载的新 SEQ 模块参数装回对应窗口。"""

        if (
            not self._deferred_sequence_module_settings
            or self.current_run_state not in self.TERMINAL_STATES
            or self._pending_run is not None
        ):
            return
        pending = self._deferred_sequence_module_settings
        self._deferred_sequence_module_settings = {}
        loaded_count = 0
        issues: list[str] = []
        for module_id, settings in pending.items():
            if module_id not in self.enabled_modules:
                continue
            window = self.module_windows.get(module_id)
            if window is None:
                continue
            try:
                window.load_settings(
                    deepcopy(settings),
                    mark_unapplied=True,
                )
                loaded_count += 1
            except Exception as exc:
                issue = (
                    f"Could not load deferred imported settings "
                    f"into enabled module {module_id!r}: {exc}"
                )
                issues.append(issue)
                self._append_log(
                    "WARNING",
                    "sequence",
                    "MODULE_SETTINGS_IMPORT",
                    issue,
                )
        if loaded_count:
            self._append_log(
                "INFO",
                "sequence",
                "MODULE_SETTINGS_IMPORTED",
                (
                    f"Loaded deferred SEQ module settings into {loaded_count} "
                    "enabled module window(s); review and Apply Settings before the next Run"
                ),
            )
            self.statusBar().showMessage(
                (
                    f"Loaded deferred SEQ settings into {loaded_count} module window(s); "
                    "Apply Settings before the next Run"
                ),
                8000,
            )
        if issues:
            QMessageBox.warning(
                self,
                "SEQ Module Settings Import",
                "\n".join(issues[:12]),
            )

    def _collect_sequence_module_settings(
        self,
    ) -> tuple[
        dict[str, dict[str, object]],
        dict[str, str],
    ]:
        """合并已导入关系和当前 Enabled 模块的 Settings 页快照。

        未安装或当前 Disabled 的关联模块会原样保留，避免仅打开再保存一次 SEQ 就丢掉
        第三方模块设置；运行中暂存的新 SEQ 参数亦优先保留，不被锁定中的旧窗口值覆盖。
        """

        self._apply_deferred_sequence_module_settings()
        settings = deepcopy(
            self._sequence_module_settings
        )
        versions = dict(
            self._sequence_module_versions
        )
        for module_id in sorted(
            self.enabled_modules
        ):
            if module_id in self._deferred_sequence_module_settings:
                values = deepcopy(
                    self._deferred_sequence_module_settings[module_id]
                )
            else:
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
        self._apply_deferred_sequence_module_settings()
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
        self.module_manager.update_state(
            module_id,
            enabled,
            state,
            message,
            resources_retained=bool(payload.get("resources_retained", False)),
        )
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
            window_hidden=bool(
                window is not None
                and window.isHidden()
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
        if (
            self.enabled_modules
            or self._pending_module_operations
            or self.current_run_state not in self.TERMINAL_STATES
            or self._pending_run is not None
        ):
            QMessageBox.warning(
                self,
                "Refresh Unavailable",
                "Stop the SEQ and disable every module before refreshing module sources.",
            )
            return
        descriptors = self._discover_module_descriptors()
        try:
            self.runtime.replace_module_descriptors(descriptors).result(timeout=5.0)
        except Exception as exc:
            QMessageBox.warning(self, "Refresh Modules Failed", str(exc))
            return
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

    def _add_module_instance(
        self,
        template_id: str,
        instance_id: str,
        display_name: str,
    ) -> None:
        if self.current_run_state not in self.TERMINAL_STATES or self._pending_run is not None:
            QMessageBox.warning(
                self,
                "Add Instance Unavailable",
                "Stop the SEQ before adding a measurement module instance.",
            )
            return
        template = next(
            (
                item
                for item in self.module_descriptors
                if item.id == template_id and not item.is_instance and item.valid
            ),
            None,
        )
        if template is None:
            QMessageBox.warning(
                self,
                "Source Module Unavailable",
                f"Source module {template_id!r} is not installed or is invalid.",
            )
            return
        if any(item.id == instance_id for item in self.module_descriptors):
            QMessageBox.warning(
                self,
                "Duplicate Instance ID",
                f"Module ID {instance_id!r} already exists.",
            )
            return
        instances_file = module_instances_path(self.config)
        try:
            existing_instances = load_module_instances(instances_file)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Invalid Module Instances File",
                f"Fix {instances_file.name} before adding a new instance: {exc}",
            )
            return
        entry = ModuleInstanceConfig(
            id=instance_id,
            module=template.id,
            name=display_name,
        )
        descriptor = build_instance_descriptor(
            template,
            entry,
            fallback_path=instances_file,
        )
        if not descriptor.valid:
            QMessageBox.warning(self, "Invalid Module Instance", descriptor.error)
            return
        try:
            self.runtime.add_module_descriptor(descriptor).result(timeout=5.0)
        except Exception as exc:
            QMessageBox.warning(self, "Add Instance Failed", str(exc))
            return
        try:
            save_module_instances(instances_file, (*existing_instances, entry))
        except Exception as exc:
            try:
                self.runtime.remove_module_descriptor(descriptor.id).result(timeout=5.0)
            except Exception:
                pass
            QMessageBox.warning(self, "Save Module Instances Failed", str(exc))
            return
        self.module_descriptors = (*self.module_descriptors, descriptor)
        self.module_manager.set_descriptors(
            self.module_descriptors,
            select_id=descriptor.id,
        )
        self.measure_status_label.setText(
            f"{len(self.enabled_modules)} of {len(self.module_descriptors)} measurement modules enabled"
        )
        self.statusBar().showMessage(f"Added module instance {descriptor.name}", 3000)

    def _remove_module_instance(self, module_id: str) -> None:
        if self.current_run_state not in self.TERMINAL_STATES or self._pending_run is not None:
            QMessageBox.warning(
                self,
                "Remove Instance Unavailable",
                "Stop the SEQ before removing a measurement module instance.",
            )
            return
        descriptor = next(
            (item for item in self.module_descriptors if item.id == module_id),
            None,
        )
        if descriptor is None or not descriptor.is_instance:
            return
        if (
            module_id in self.enabled_modules
            or self.module_manager.runtime_state(module_id) in {"initializing", "disabling"}
        ):
            QMessageBox.warning(
                self,
                "Instance Still Enabled",
                f"Disable {descriptor.name} before removing this instance.",
            )
            return
        instances_file = module_instances_path(self.config)
        try:
            existing_instances = load_module_instances(instances_file)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Invalid Module Instances File",
                f"Cannot update {instances_file.name}: {exc}",
            )
            return
        try:
            self.runtime.remove_module_descriptor(module_id).result(timeout=5.0)
        except Exception as exc:
            QMessageBox.warning(self, "Remove Instance Failed", str(exc))
            return
        remaining = tuple(item for item in existing_instances if item.id != module_id)
        try:
            save_module_instances(instances_file, remaining)
        except Exception as exc:
            if descriptor.valid:
                try:
                    self.runtime.add_module_descriptor(descriptor).result(timeout=5.0)
                except Exception:
                    pass
            QMessageBox.warning(self, "Save Module Instances Failed", str(exc))
            return
        window = self.module_windows.pop(module_id, None)
        if window is not None:
            window.allow_application_close()
            window.close()
        self.module_descriptors = tuple(
            item for item in self.module_descriptors if item.id != module_id
        )
        self.module_manager.set_descriptors(self.module_descriptors)
        self.measure_status_label.setText(
            f"{len(self.enabled_modules)} of {len(self.module_descriptors)} measurement modules enabled"
        )
        self.statusBar().showMessage(f"Removed module instance {descriptor.name}", 3000)

    def _open_system_log(self) -> None:
        if self.system_log_dialog is None:
            current = self.current_data_file
            if current is None:
                displayed = self.data_file_label.fullText().strip()
                if displayed:
                    current = self.config.resolve_project_path(displayed)
            directory = current.parent if current is not None else self._last_data_directory
            path = directory / f"instrument_log_{datetime.now():%Y%m%d_%H%M%S}.log"
            dialog = SystemLogDialog(self.config, str(path), self)
            dialog.startRequested.connect(self._start_system_log)
            dialog.stopRequested.connect(self._stop_system_log)
            dialog.viewRequested.connect(self._view_system_log)
            self.system_log_dialog = dialog
        self.system_log_dialog.set_state(self._system_log_state)
        self.system_log_dialog.show()
        self.system_log_dialog.raise_()
        self.system_log_dialog.activateWindow()

    def _start_system_log(self, path: str, interval_seconds: int, fields: tuple[str, ...]) -> None:
        try:
            future = self.runtime.start_system_log(path, interval_seconds, fields)
        except RuntimeError as exc:
            QMessageBox.warning(self.system_log_dialog, "System Log Failed", str(exc))
            return
        self.system_log_dialog.set_pending(future)

    def _stop_system_log(self) -> None:
        self.system_log_dialog.set_pending(self.runtime.stop_system_log())

    def _view_system_log(self, path: str) -> None:
        text = path.strip()
        if not text:
            QMessageBox.warning(
                self.system_log_dialog or self,
                "System Log",
                "Choose a System Log file first.",
            )
            return
        resolved = self.config.resolve_project_path(text)
        if not resolved.is_file():
            QMessageBox.information(
                self.system_log_dialog or self,
                "System Log",
                f"System Log file does not exist yet:\n{resolved}\n\n"
                "Click Start Log first or choose an existing .log file.",
            )
            return
        self._show_data_browser(resolved)

    def _open_manual_control(
        self,
        instrument_id: str,
        panel_id: str,
    ) -> None:
        config = self.config.instrument(instrument_id)
        panel = config.panel(panel_id)
        key = panel.key
        dialog = self.manual_dialogs.get(key)
        if dialog is None:
            dialog = ManualControlDialog(config, panel, self)
            dialog.setRequested.connect(
                lambda instrument_id, control, value, rate: self._manual_set_target(
                    instrument_id, control, value, rate, dialog
                )
            )
            dialog.holdRequested.connect(self._manual_hold_instrument)
            self._restore_dialog_geometry(
                dialog,
                f"manual/{key}",
            )
            self.manual_dialogs[key] = dialog
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

    def _setup_capable_instruments(
        self,
    ) -> tuple[tuple[InstrumentConfig, SystemInstrumentDescriptor], ...]:
        descriptors_by_id = {
            descriptor.id: descriptor
            for descriptor in self.runtime.instrument_descriptors
            if descriptor.can_load and descriptor.setup_widget
        }
        result: list[tuple[InstrumentConfig, SystemInstrumentDescriptor]] = []
        for instrument in self.config.instrument_instances:
            descriptor = descriptors_by_id.get(instrument.backend)
            if descriptor is not None:
                result.append((instrument, descriptor))
        return tuple(result)

    def _create_instrument_ui_api(
        self,
        config: InstrumentConfig,
        parent: QObject | None = None,
    ) -> InstrumentUIAPI:
        editable = (
            self.current_run_state in self.TERMINAL_STATES
            and self._pending_run is None
        )
        return InstrumentUIAPI(
            config,
            lambda inst_id, action, payload, read_only: self.runtime.execute_instrument_action(
                inst_id,
                action,
                payload,
                read_only=read_only,
            ),
            parent,
            runtime_editable=editable,
            snapshot=self.current_snapshots.get(config.id),
        )

    def _open_instrument_setup(self, instrument_id: str | None = None) -> None:
        entries = self._setup_capable_instruments()
        if not entries:
            QMessageBox.information(
                self,
                "Instrument Setup Unavailable",
                "No configured System Instruments declare an Instrument Setup interface.",
            )
            return
        if self.instrument_setup_dialog is None:
            dialog = InstrumentSetupDialog(
                entries,
                self._create_instrument_ui_api,
                self,
            )
            self._restore_dialog_geometry(dialog, "instrument_setup")
            self.instrument_setup_dialog = dialog
        editable = (
            self.current_run_state in self.TERMINAL_STATES
            and self._pending_run is None
        )
        self.instrument_setup_dialog.set_runtime_editable(editable)
        for snapshot in self.current_snapshots.values():
            self.instrument_setup_dialog.update_snapshot(snapshot)
        self.instrument_setup_dialog.open_for(instrument_id)

    def _manual_set_target(
        self,
        instrument_id: str,
        control: str,
        value: float,
        rate: float,
        dialog: ManualControlDialog,
    ) -> None:
        future = self.runtime.set_target(
            instrument_id,
            value,
            rate,
            control=control,
        )
        snapshot = self.current_snapshots.get(instrument_id)
        precision = control_decimals(snapshot.kind, snapshot.unit) if snapshot is not None else 3
        self._pending_manual_operations.append(
            (
                future,
                f"Confirmed target {fixed_number(value, precision)} for {instrument_id}",
                dialog,
            )
        )
        self.statusBar().showMessage(f"Sending target to {instrument_id}...")

    def _manual_hold_instrument(
        self,
        instrument_id: str,
        control: str,
    ) -> None:
        future = self.runtime.hold_instrument(
            instrument_id,
            control=control,
        )
        self._pending_manual_operations.append(
            (future, f"Hold Current confirmed for {instrument_id}", None)
        )
        self.statusBar().showMessage(f"Requesting Hold Current for {instrument_id}...")

    def _manual_instrument_action(
        self,
        instrument_id: str,
        command_id: str,
    ) -> None:
        if (
            self.current_run_state not in self.TERMINAL_STATES
            or self._pending_run is not None
        ):
            QMessageBox.warning(
                self,
                "SEQ Is Running",
                "Manual instrument actions are available only while the SEQ is idle.",
            )
            return
        spec = self._instrument_sequence_command_specs[
            (instrument_id, command_id)
        ]
        future = self.runtime.instrument_action(
            instrument_id,
            command_id,
        )
        self._pending_manual_operations.append(
            (future, f"{spec.label} confirmed", None)
        )
        self.statusBar().showMessage(f"Sending {spec.label}...")

    def _show_graph(self) -> None:
        self.trend_dialog.show()
        self.trend_dialog.raise_()
        self.trend_dialog.activateWindow()

    def _show_data_browser(
        self,
        path: str | Path | None = None,
    ) -> QMdiSubWindow:
        browser = DatBrowserWidget(self.config.project_root)
        browser.fileChanged.connect(
            lambda changed_path, widget=browser: (
                self._data_browser_file_changed_for_browser(
                    widget,
                    changed_path,
                )
            )
        )
        browser.detachToggled.connect(
            lambda widget=browser: self._toggle_data_browser_detached(widget)
        )
        data_window = self._create_mdi_data_browser_window(
            browser,
            title="Data Browser",
            restore_rect=True,
        )
        if path is not None:
            browser.load_path(path, show_errors=True)
        return data_window

    def _create_mdi_data_browser_window(
        self,
        browser: DatBrowserWidget,
        *,
        title: str,
        size: QSize | None = None,
        restore_rect: bool = False,
    ) -> _DataBrowserMdiSubWindow:
        docked_count = sum(
            isinstance(window, QMdiSubWindow)
            for window in self.data_browser_windows
        )
        data_window = _DataBrowserMdiSubWindow(self.mdi, scaled(20))
        data_window.setWidget(browser)
        data_window.setWindowTitle(title)
        data_window.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose,
            True,
        )
        self.mdi.addSubWindow(data_window)
        if size is not None:
            data_window.resize(size)
        else:
            data_window.resize(scaled(900), scaled(620))
        store = self.ui_preference_store
        if (
            restore_rect
            and store is not None
            and self.ui_preferences.window_mode == "remember"
        ):
            rect = store.rect("data_browser")
            if rect is not None:
                data_window.setGeometry(rect)
        offset = scaled(24) * (docked_count % 8)
        self.data_browser_windows[data_window] = browser
        data_window.destroyed.connect(
            lambda _object=None, window=data_window: (
                self.data_browser_windows.pop(window, None)
            )
        )
        data_window.detachRequested.connect(
            lambda drop_pos, window=data_window: self._detach_data_browser(
                window,
                drop_pos=drop_pos,
            )
        )
        browser.set_detached(False)
        browser.show()
        data_window.show()
        if offset:
            data_window.move(
                data_window.x() + offset,
                data_window.y() + offset,
            )
        self.mdi.setActiveSubWindow(data_window)
        data_window.raise_()
        QTimer.singleShot(0, self._fit_mdi_windows)
        return data_window

    def _create_floating_data_browser_window(
        self,
        browser: DatBrowserWidget,
        *,
        title: str,
        size: QSize,
        drop_pos: QPoint | None = None,
        origin: QPoint | None = None,
    ) -> QMainWindow:
        # 独立顶层窗不设 parent=self，避免 Windows 下被 owner 强制压在主窗口之上。
        floating = QMainWindow(None, Qt.WindowType.Window)
        if not self.windowIcon().isNull():
            floating.setWindowIcon(self.windowIcon())
        floating.setCentralWidget(browser)
        floating.setWindowTitle(title)
        floating.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose,
            True,
        )
        floating.resize(size)
        restored_geometry = False
        store = self.ui_preference_store
        if (
            drop_pos is None
            and store is not None
            and self.ui_preferences.window_mode == "remember"
        ):
            saved = store.geometry("data_browser_floating")
            if saved is not None and not saved.isEmpty():
                restored_geometry = floating.restoreGeometry(saved)
        if drop_pos is not None:
            target_x = drop_pos.x() - floating.width() // 2
            target_y = drop_pos.y() - scaled(16)
            screen = (
                QGuiApplication.screenAt(drop_pos)
                or QApplication.primaryScreen()
            )
            if screen is not None:
                avail = screen.availableGeometry()
                target_x = max(
                    avail.left(),
                    min(target_x, max(avail.left(), avail.right() - floating.width() + 1)),
                )
                target_y = max(
                    avail.top(),
                    min(target_y, max(avail.top(), avail.bottom() - scaled(40))),
                )
            floating.move(target_x, target_y)
        elif not restored_geometry and origin is not None:
            floating.move(origin)

        self.data_browser_windows[floating] = browser
        floating.destroyed.connect(
            lambda _object=None, window=floating: (
                self.data_browser_windows.pop(window, None)
            )
        )
        browser.set_detached(True)
        browser.show()
        floating.show()
        floating.raise_()
        floating.activateWindow()
        return floating

    def _window_for_data_browser(
        self,
        browser: DatBrowserWidget,
    ) -> QMdiSubWindow | QMainWindow | None:
        for window, candidate in self.data_browser_windows.items():
            if candidate is browser:
                return window
        return None

    def _toggle_data_browser_detached(
        self,
        browser: DatBrowserWidget,
    ) -> QMdiSubWindow | QMainWindow | None:
        window = self._window_for_data_browser(browser)
        if window is None:
            return None
        if isinstance(window, QMdiSubWindow):
            return self._detach_data_browser(window)
        return self._dock_data_browser(window)

    def _detach_data_browser(
        self,
        subwindow: QMdiSubWindow,
        *,
        drop_pos: QPoint | None = None,
    ) -> QMainWindow | None:
        browser = self.data_browser_windows.pop(subwindow, None)
        if browser is None:
            return None
        title = subwindow.windowTitle()
        size = subwindow.size()
        origin = subwindow.mapToGlobal(QPoint(0, 0))
        # 先摘出 DatBrowserWidget 再移除旧子窗口，保留当前图表与监控定时器。
        subwindow.setWidget(None)
        browser.setParent(None)
        self.mdi.removeSubWindow(subwindow)
        subwindow.close()
        subwindow.deleteLater()
        return self._create_floating_data_browser_window(
            browser,
            title=title,
            size=size,
            drop_pos=drop_pos,
            origin=origin,
        )

    def _dock_data_browser(
        self,
        floating: QMainWindow,
    ) -> QMdiSubWindow | None:
        browser = self.data_browser_windows.pop(floating, None)
        if browser is None:
            return None
        title = floating.windowTitle()
        size = floating.size()
        taken = floating.takeCentralWidget()
        if isinstance(taken, DatBrowserWidget):
            browser = taken
        browser.setParent(None)
        floating.close()
        floating.deleteLater()
        return self._create_mdi_data_browser_window(
            browser,
            title=title,
            size=size,
        )

    def _fit_mdi_windows(self) -> None:
        """Keep docked document windows inside the current MDI viewport."""
        viewport = self.mdi.viewport().rect()
        if viewport.width() <= 8 or viewport.height() <= 8:
            return
        max_width = max(320, viewport.width() - 4)
        max_height = max(240, viewport.height() - 4)
        for subwindow in (
            self.sequence_window,
            *(
                window
                for window in self.data_browser_windows
                if isinstance(window, QMdiSubWindow)
            ),
        ):
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

    def _data_browser_file_changed_for_browser(
        self,
        browser: DatBrowserWidget,
        path: str,
    ) -> None:
        window = self._window_for_data_browser(browser)
        if window is not None:
            self._data_browser_file_changed(window, path)

    def _data_browser_file_changed(
        self,
        window: QMdiSubWindow | QMainWindow,
        path: str,
    ) -> None:
        window.setWindowTitle(
            f"{Path(path).name} - Data Browser"
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if DatBrowserWidget._first_dat_path(event) is not None:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        path = DatBrowserWidget._first_dat_path(event)
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
            if self._close_after_stop:
                event.ignore()
                return
            answer = QMessageBox.question(
                self,
                "Sequence Is Running",
                "Closing will stop the sequence. OpenLab Control will not send Set or Hold "
                "to System Instruments; their current actions will continue. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.No:
                event.ignore()
                return
            if self.current_run_state in self.TERMINAL_STATES:
                # Sequence finished while the confirmation dialog was open.
                if self.current_run_state is RunState.FAULTED:
                    event.ignore()
                    return
            else:
                self._close_after_stop = True
                self.runtime.stop_sequence()
                self.run_detail_label.setText("Stopping sequence and waiting for module cleanup before closing")
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
        if self.instrument_setup_dialog is not None:
            self.instrument_setup_dialog.allow_application_close()
            self.instrument_setup_dialog.close()
        for window in self.module_windows.values():
            window.allow_application_close()
            window.close()
        for viewer in tuple(self.data_browser_windows):
            viewer.close()
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
