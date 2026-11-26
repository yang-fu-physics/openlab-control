"""SEQ 树形文档的列表编辑器。

控件把命令、End Scan 和 End Sequence 展平成可多选列表，但所有修改仍通过
``SequenceDocument`` 完成。运行期间只关闭修改动作，列表保持可查看；剪切/复制/粘贴会
克隆完整扫描子树并刷新节点 ID，防止一个命令在模型中出现重复身份；编辑历史支持多步撤销
（Ctrl+Z）与重做（Ctrl+Y / Ctrl+Shift+Z）。
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
import json

import qtawesome as qta
from PySide6.QtCore import (
    QByteArray,
    QItemSelectionModel,
    QMimeData,
    QPoint,
    QRect,
    QSize,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDrag,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..sequence.model import Command, CommandType, FlatRow, SequenceDocument
from ..sequence.parser import format_command
from .scaling import scaled


_MAX_UNDO_DEPTH = 100
_DATAFILE_PATH_MAX_CHARS = 56
_ROLE_ROW = Qt.ItemDataRole.UserRole
_ROLE_RUNNING = Qt.ItemDataRole.UserRole + 1
SEQUENCE_COMMAND_MIME = "application/x-openlab-seq-commands"
PALETTE_COMMAND_MIME = "application/x-openlab-palette-command"


@dataclass(frozen=True, slots=True)
class _EditorSnapshot:
    commands: list[Command]
    selected_keys: tuple[tuple[str | None, bool, bool], ...]
    current_key: tuple[str | None, bool, bool] | None


def _unique_shortcuts(*sequences: QKeySequence) -> list[QKeySequence]:
    result: list[QKeySequence] = []
    for sequence in sequences:
        if not sequence.isEmpty() and sequence not in result:
            result.append(sequence)
    return result


def _elide_middle_path(path_text: str, max_chars: int = _DATAFILE_PATH_MAX_CHARS) -> str:
    """在列表行内中段省略过长的数据文件路径，保留盘符前缀与末尾文件名。"""

    if len(path_text) <= max_chars:
        return path_text
    sep_index = max(path_text.rfind("\\"), path_text.rfind("/"))
    if sep_index > 0:
        filename = path_text[sep_index + 1 :]
        if filename and len(filename) <= max_chars - 12:
            head_len = max(8, max_chars - len(filename) - 5)
            return f"{path_text[:head_len]}…{path_text[sep_index:]}"
    head = max(12, (max_chars - 1) // 2)
    tail = max(12, max_chars - head - 1)
    return f"{path_text[:head]}…{path_text[-tail:]}"


def _display_command(command: Command) -> str:
    """生成列表行文本；显式温度点始终使用更易辨认的方括号形式。

    SEQ 保存仍保留未编辑旧文件的原文，但界面不受旧式裸列表语法影响，因此加载旧文件后
    也会看到 ``[300, 299.9, 20]``。每个点的小数位来自解析时保留的输入文本。
    """

    is_temperature_list = (
        command.type is CommandType.SCAN_TEMPERATURE
        and str(command.params.get("point_mode", "Linear")).casefold() == "list"
    )
    return format_command(command, preserve_raw=not is_temperature_list)


def _display_command_and_tooltip(command: Command) -> tuple[str, str]:
    """返回列表行展示文本与可选完整路径提示。"""

    full_text = _display_command(command)
    if command.type is not CommandType.SET_DATAFILE:
        return full_text, ""
    raw_path = str(command.params.get("path", "")).strip()
    if not raw_path or len(raw_path) <= _DATAFILE_PATH_MAX_CHARS:
        return full_text, ""
    elided = _elide_middle_path(raw_path)
    if raw_path in full_text:
        return full_text.replace(raw_path, elided, 1), full_text
    return full_text, ""


def _semantic_style(command: Command) -> tuple[QColor, bool, bool]:
    """返回 ``(前景色, 是否加粗, 是否斜体)``，用于温和区分命令语义层级。"""

    if command.type in {
        CommandType.SCAN_TEMPERATURE,
        CommandType.SCAN_FIELD,
        CommandType.SCAN_TIME,
        CommandType.MODULE_SCAN,
    }:
        return QColor("#1b4b8a"), True, False
    if command.type is CommandType.MEASURE:
        return QColor("#1a6f3c"), True, False
    if command.type in {
        CommandType.SET_TEMPERATURE,
        CommandType.SET_FIELD,
        CommandType.INSTRUMENT_COMMAND,
        CommandType.MODULE_COMMAND,
    }:
        return QColor("#0f5c6e"), False, False
    if command.type is CommandType.WAIT:
        return QColor("#8a5300"), False, False
    if command.type in {CommandType.SET_DATAFILE, CommandType.CALL_SEQUENCE}:
        return QColor("#475467"), False, False
    if command.type is CommandType.REMARK:
        return QColor("#667085"), False, True
    if command.type in {CommandType.INJECT_WARNING, CommandType.INJECT_ERROR}:
        return QColor("#a61b29"), False, False
    return QColor("#1f2328"), False, False


class RunningCommandDelegate(QStyledItemDelegate):
    """绘制嵌套缩进参考线、当前执行行左侧强调条与高亮底色。"""

    def sizeHint(self, option, index) -> QSize:
        hint = super().sizeHint(option, index)
        min_height = max(scaled(22), option.fontMetrics.height() + scaled(6))
        return QSize(hint.width(), max(hint.height(), min_height))

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        if index.data(_ROLE_RUNNING):
            option.backgroundBrush = QColor("#ffebb0")
            option.palette.setColor(QPalette.ColorRole.Highlight, QColor("#ffebb0"))
            option.palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#3b2600"))
            option.palette.setColor(QPalette.ColorRole.Text, QColor("#3b2600"))
            option.font.setBold(True)

    def paint(self, painter: QPainter, option, index) -> None:
        running = bool(index.data(_ROLE_RUNNING))
        row = index.data(_ROLE_ROW)
        draw_option = option
        if running:
            draw_option = QStyleOptionViewItem(option)
            self.initStyleOption(draw_option, index)
            # Clear State_Selected before delegating so QSS ::item:selected never
            # overrides the warm amber execution highlight on the running row.
            draw_option.state &= ~QStyle.StateFlag.State_Selected
            painter.save()
            painter.fillRect(draw_option.rect, QColor("#ffebb0"))
            painter.restore()
            draw_option.backgroundBrush = QColor(Qt.GlobalColor.transparent)
            super().paint(painter, draw_option, index)
        else:
            super().paint(painter, option, index)
        painter.save()
        try:
            rect: QRect = option.rect
            if isinstance(row, FlatRow) and row.depth > 0:
                indent_step = max(scaled(14), option.fontMetrics.horizontalAdvance("    "))
                base_left = rect.left() + scaled(7)
                guide_color = (
                    QColor("#d0a242")
                    if running
                    else QColor("#b9cbe0")
                    if option.state & QStyle.StateFlag.State_Selected
                    else QColor("#d4dde8")
                )
                painter.setPen(QPen(guide_color, 1))
                for level in range(row.depth):
                    x = int(base_left + level * indent_step)
                    painter.drawLine(x, rect.top(), x, rect.bottom())
            if running:
                bar_width = scaled(3)
                painter.fillRect(
                    rect.left(),
                    rect.top(),
                    bar_width,
                    rect.height(),
                    QColor("#d98200"),
                )
                painter.setPen(QPen(QColor("#e8c468"), 1))
                painter.drawLine(rect.left() + bar_width, rect.top(), rect.right(), rect.top())
                painter.drawLine(rect.left() + bar_width, rect.bottom(), rect.right(), rect.bottom())
        finally:
            painter.restore()


def build_drag_badge_pixmap(label: str, font) -> QPixmap:
    """绘制拖拽过程中的轻量语句胶囊预览，提升拖放时的可视反馈。"""

    metrics = QFontMetrics(font)
    max_text_width = scaled(240)
    elided = metrics.elidedText(
        label.strip() or "Command",
        Qt.TextElideMode.ElideRight,
        max_text_width,
    )
    pad_x = scaled(10)
    pad_y = scaled(4)
    width = max(scaled(48), metrics.horizontalAdvance(elided) + pad_x * 2)
    height = max(scaled(22), metrics.height() + pad_y * 2)
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(239, 246, 255, 238))
        painter.setPen(QPen(QColor("#1d63b8"), 1.2))
        radius = scaled(5)
        painter.drawRoundedRect(QRect(1, 1, width - 2, height - 2), radius, radius)
        painter.setFont(font)
        painter.setPen(QColor("#102a43"))
        painter.drawText(
            QRect(pad_x, 0, width - pad_x * 2, height),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided,
        )
    finally:
        painter.end()
    return pixmap


class _SequenceListWidget(QListWidget):
    """支持语句拖拽重排、跨层移入/移出 Scan 和指令栏拖入插入的列表视图。"""

    def __init__(self, editor: "SequenceEditorWidget") -> None:
        super().__init__(editor)
        self._editor = editor
        self._drop_indicator: tuple[int, int] | None = None
        self._autoscroll_direction = 0
        self._last_drag_pos: QPoint | None = None
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.setInterval(40)
        self._autoscroll_timer.timeout.connect(self._on_autoscroll_tick)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def _clear_drop_indicator(self) -> None:
        if self._drop_indicator is not None:
            self._drop_indicator = None
            self.viewport().update()

    def _stop_autoscroll(self) -> None:
        self._autoscroll_direction = 0
        self._last_drag_pos = None
        if self._autoscroll_timer.isActive():
            self._autoscroll_timer.stop()

    def _update_autoscroll(self, pos: QPoint) -> None:
        self._last_drag_pos = QPoint(pos)
        margin = scaled(22)
        viewport_height = self.viewport().height()
        if viewport_height <= margin * 2:
            self._stop_autoscroll()
            return
        if pos.y() < margin:
            self._autoscroll_direction = -1
        elif pos.y() > viewport_height - margin:
            self._autoscroll_direction = 1
        else:
            self._autoscroll_direction = 0
        if self._autoscroll_direction != 0:
            if not self._autoscroll_timer.isActive():
                self._autoscroll_timer.start()
        elif self._autoscroll_timer.isActive():
            self._autoscroll_timer.stop()

    def _on_autoscroll_tick(self) -> None:
        if self._autoscroll_direction == 0:
            self._autoscroll_timer.stop()
            return
        bar = self.verticalScrollBar()
        step = max(1, bar.singleStep())
        new_value = max(
            bar.minimum(),
            min(bar.maximum(), bar.value() + self._autoscroll_direction * step),
        )
        if new_value == bar.value():
            return
        bar.setValue(new_value)
        if self._last_drag_pos is not None and self._drop_indicator is not None:
            _, _, indicator_y, depth = self._drop_target_at(self._last_drag_pos)
            self._drop_indicator = (indicator_y, depth)
            self.viewport().update()

    def _drop_target_at(self, pos: QPoint) -> tuple[FlatRow | None, bool, int, int]:
        """根据鼠标位置计算 ``(锚点行, 是否插在行后, 指示线 Y, 目标缩进深度)``。"""

        item = self.itemAt(pos)
        if item is None:
            if self.count() > 0:
                last_item = self.item(self.count() - 1)
                last_row = last_item.data(_ROLE_ROW)
                last_rect = self.visualItemRect(last_item)
                if isinstance(last_row, FlatRow) and last_row.is_sequence_end:
                    return last_row, False, last_rect.top(), 0
                return None, False, last_rect.bottom(), 0
            return None, False, scaled(2), 0

        row = item.data(_ROLE_ROW)
        rect = self.visualItemRect(item)
        if not isinstance(row, FlatRow):
            return None, False, rect.bottom(), 0
        if row.is_sequence_end:
            return row, False, rect.top(), 0
        after = pos.y() >= rect.center().y()
        indicator_y = rect.bottom() if after else rect.top()
        _, _, depth = self._editor.document.resolve_drop_target(row, after=after)
        return row, after, indicator_y, depth

    @staticmethod
    def _decode_command_ids(mime: QMimeData) -> list[str] | None:
        if not mime.hasFormat(SEQUENCE_COMMAND_MIME):
            return None
        try:
            raw = bytes(mime.data(SEQUENCE_COMMAND_MIME)).decode("utf-8")
            parsed = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return None
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return parsed
        return None

    @staticmethod
    def _decode_palette_payload(mime: QMimeData) -> tuple[str, ...] | None:
        if not mime.hasFormat(PALETTE_COMMAND_MIME):
            return None
        try:
            raw = bytes(mime.data(PALETTE_COMMAND_MIME)).decode("utf-8")
            parsed = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return None
        if isinstance(parsed, list) and parsed and all(isinstance(item, str) for item in parsed):
            return tuple(parsed)
        return None

    def startDrag(self, supportedActions) -> None:  # noqa: N802
        if not self._editor._editable:
            return
        current_row = self._editor.selected_row()
        if current_row is not None and (current_row.is_end or current_row.is_sequence_end):
            return
        commands = self._editor.selected_commands(prune_descendants=True)
        if not commands:
            return
        mime = QMimeData()
        payload = json.dumps([command.id for command in commands]).encode("utf-8")
        mime.setData(SEQUENCE_COMMAND_MIME, QByteArray(payload))
        drag = QDrag(self)
        drag.setMimeData(mime)
        if len(commands) == 1:
            label = format_command(commands[0])
            if commands[0].children:
                label = f"{label} (+{len(commands[0].children)})"
        else:
            label = f"{len(commands)} commands"
        pixmap = build_drag_badge_pixmap(label, self.font())
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(scaled(12), pixmap.height() // 2))
        drag.exec(Qt.DropAction.MoveAction)
        self._stop_autoscroll()
        self._clear_drop_indicator()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if not self._editor._editable:
            event.ignore()
            return
        mime = event.mimeData()
        if (
            self._decode_command_ids(mime) is not None
            or self._decode_palette_payload(mime) is not None
        ):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if not self._editor._editable:
            self._stop_autoscroll()
            self._clear_drop_indicator()
            event.ignore()
            return
        mime = event.mimeData()
        pos = event.position().toPoint()
        target_row, after, indicator_y, depth = self._drop_target_at(pos)
        command_ids = self._decode_command_ids(mime)
        if command_ids is not None:
            self._update_autoscroll(pos)
            if not self._editor.document.can_relocate(
                command_ids,
                target_row,
                after=after,
            ):
                self._clear_drop_indicator()
                event.ignore()
                return
            self._drop_indicator = (indicator_y, depth)
            self.viewport().update()
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        if self._decode_palette_payload(mime) is not None:
            self._update_autoscroll(pos)
            self._drop_indicator = (indicator_y, depth)
            self.viewport().update()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        self._stop_autoscroll()
        self._clear_drop_indicator()
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._stop_autoscroll()
        self._clear_drop_indicator()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._stop_autoscroll()
        self._clear_drop_indicator()
        if not self._editor._editable:
            event.ignore()
            return
        mime = event.mimeData()
        pos = event.position().toPoint()
        target_row, after, _, _ = self._drop_target_at(pos)

        command_ids = self._decode_command_ids(mime)
        if command_ids is not None:
            if self._editor.document.can_relocate(
                command_ids,
                target_row,
                after=after,
            ):
                self._editor.relocate_commands(
                    command_ids,
                    target_row,
                    after=after,
                )
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
                return
            event.ignore()
            return

        palette_payload = self._decode_palette_payload(mime)
        if palette_payload is not None:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._editor.paletteCommandDropped.emit(
                palette_payload,
                target_row,
                after,
            )
            return

        event.ignore()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._drop_indicator is None:
            return
        indicator_y, depth = self._drop_indicator
        viewport_rect = self.viewport().rect()
        y = max(viewport_rect.top() + 1, min(indicator_y, viewport_rect.bottom() - 1))
        indent_step = max(scaled(14), self.fontMetrics().horizontalAdvance("    "))
        left_x = scaled(7) + depth * indent_step
        right_x = max(left_x + scaled(12), viewport_rect.right() - scaled(6))
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = QColor("#1d63b8")
            painter.setPen(QPen(color, 2))
            painter.drawLine(left_x, y, right_x, y)
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            radius = scaled(3)
            painter.drawEllipse(QPoint(left_x, y), radius, radius)
        finally:
            painter.end()


class SequenceEditorWidget(QWidget):
    """支持任意嵌套、批量启停、复制粘贴、拖拽重排和键盘快捷键的 SEQ 编辑控件。"""

    commandDoubleClicked = Signal(object)
    documentChanged = Signal()
    paletteCommandDropped = Signal(object, object, bool)

    def __init__(self, document: SequenceDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.document = document
        self._available_module_commands: set[
            tuple[str, str]
        ] = set()
        self._clipboard: tuple[Command, ...] = ()
        self._undo_stack: list[_EditorSnapshot] = []
        self._redo_stack: list[_EditorSnapshot] = []
        self._editable = True
        self._running_command_id = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.list = _SequenceListWidget(self)
        self.list.setObjectName("sequenceCommandList")
        self.list.setItemDelegate(RunningCommandDelegate(self.list))
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemDoubleClicked.connect(self._on_double_clicked)
        self.list.currentItemChanged.connect(lambda current, previous: self._update_action_states())
        self.list.itemSelectionChanged.connect(self._update_action_states)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_context_menu)
        self.list.setStyleSheet(
            "QListWidget#sequenceCommandList {"
            "  background: #ffffff;"
            "  alternate-background-color: #f8fafc;"
            "  border: none;"
            f"  padding: {scaled(2)}px 0px;"
            "}"
            "QListWidget#sequenceCommandList::item:selected {"
            "  background: #dbeafe;"
            "  color: #101828;"
            "}"
            "QListWidget#sequenceCommandList::item:hover:!selected {"
            "  background: #f1f5f9;"
            "}"
        )

        self._build_actions()
        self.toolbar = self._build_toolbar()
        layout.addWidget(self.toolbar)
        layout.addWidget(self.list, 1)
        self.setFocusProxy(self.list)
        self.rebuild()

    def _build_toolbar(self) -> QToolBar:
        """构建紧贴 SEQ 列表顶部的紧凑图标编辑栏，点击不抢占列表键盘焦点。"""

        toolbar = QToolBar("Sequence Edit", self)
        toolbar.setObjectName("sequenceMiniToolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(scaled(14), scaled(14)))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.setStyleSheet(
            "QToolBar#sequenceMiniToolbar {"
            "  background: #f6f8fa;"
            "  border: none;"
            "  border-bottom: 1px solid #d5dce4;"
            f"  spacing: {scaled(2)}px;"
            f"  padding: {scaled(2)}px {scaled(4)}px;"
            "}"
            "QToolBar#sequenceMiniToolbar::separator {"
            "  background: #d0d7de;"
            "  width: 1px;"
            f"  margin: {scaled(3)}px {scaled(3)}px;"
            "}"
            "QToolBar#sequenceMiniToolbar QToolButton {"
            "  border: 1px solid transparent;"
            f"  border-radius: {scaled(3)}px;"
            f"  padding: {scaled(2)}px {scaled(4)}px;"
            "}"
            "QToolBar#sequenceMiniToolbar QToolButton:hover:enabled {"
            "  background: #e6edf5;"
            "  border-color: #c8d4e3;"
            "}"
            "QToolBar#sequenceMiniToolbar QToolButton:pressed:enabled {"
            "  background: #d9e4f1;"
            "}"
        )
        groups = (
            (self.undo_action, self.redo_action),
            (self.cut_action, self.copy_action, self.paste_action, self.delete_action),
            (self.move_up_action, self.move_down_action),
            (self.disable_action, self.enable_action),
        )
        for index, group in enumerate(groups):
            if index > 0:
                toolbar.addSeparator()
            for action in group:
                toolbar.addAction(action)
                button = toolbar.widgetForAction(action)
                if isinstance(button, QToolButton):
                    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return toolbar

    def _build_actions(self) -> None:
        """创建一次性复用的 QAction，避免每次右键菜单重复连接信号。"""

        icon_color = "#3b4652"
        disabled_color = "#a8b2bd"

        def _icon(name: str, color: str = icon_color):
            return qta.icon(name, color=color, color_disabled=disabled_color)

        self.undo_action = QAction(_icon("fa5s.undo"), "Undo", self)
        self.undo_action.setShortcuts(
            _unique_shortcuts(
                *QKeySequence.keyBindings(QKeySequence.StandardKey.Undo),
                QKeySequence("Ctrl+Z"),
            )
        )
        self.undo_action.setToolTip("Undo (Ctrl+Z)")
        self.undo_action.setStatusTip("Undo the last sequence edit (Ctrl+Z)")
        self.undo_action.triggered.connect(self.undo)

        self.redo_action = QAction(_icon("fa5s.redo"), "Redo", self)
        self.redo_action.setShortcuts(
            _unique_shortcuts(
                *QKeySequence.keyBindings(QKeySequence.StandardKey.Redo),
                QKeySequence("Ctrl+Y"),
                QKeySequence("Ctrl+Shift+Z"),
            )
        )
        self.redo_action.setToolTip("Redo (Ctrl+Y / Ctrl+Shift+Z)")
        self.redo_action.setStatusTip("Redo the undone sequence edit (Ctrl+Y / Ctrl+Shift+Z)")
        self.redo_action.triggered.connect(self.redo)

        self.disable_action = QAction(_icon("fa5s.ban", "#8a5700"), "Disable", self)
        self.disable_action.setShortcut(QKeySequence("Ctrl+D"))
        self.disable_action.setToolTip("Disable selected (Ctrl+D)")
        self.disable_action.setStatusTip("Disable the selected commands (Ctrl+D)")
        self.disable_action.triggered.connect(self.disable_selected)

        self.enable_action = QAction(_icon("fa5s.check-circle", "#1a6f3c"), "Enable", self)
        self.enable_action.setShortcut(QKeySequence("Ctrl+E"))
        self.enable_action.setToolTip("Enable selected (Ctrl+E)")
        self.enable_action.setStatusTip("Enable the selected commands (Ctrl+E)")
        self.enable_action.triggered.connect(self.enable_selected)

        self.cut_action = QAction(_icon("fa5s.cut"), "Cut", self)
        self.cut_action.setShortcuts(
            _unique_shortcuts(
                *QKeySequence.keyBindings(QKeySequence.StandardKey.Cut),
                QKeySequence("Ctrl+X"),
            )
        )
        self.cut_action.setToolTip("Cut (Ctrl+X)")
        self.cut_action.setStatusTip("Cut the selected commands or Scan blocks (Ctrl+X)")
        self.cut_action.triggered.connect(self.cut_selected)

        self.copy_action = QAction(_icon("fa5s.copy"), "Copy", self)
        self.copy_action.setShortcuts(
            _unique_shortcuts(
                *QKeySequence.keyBindings(QKeySequence.StandardKey.Copy),
                QKeySequence("Ctrl+C"),
            )
        )
        self.copy_action.setToolTip("Copy (Ctrl+C)")
        self.copy_action.setStatusTip("Copy the selected commands or Scan blocks (Ctrl+C)")
        self.copy_action.triggered.connect(self.copy_selected)

        self.paste_action = QAction(_icon("fa5s.paste"), "Paste", self)
        self.paste_action.setShortcuts(
            _unique_shortcuts(
                *QKeySequence.keyBindings(QKeySequence.StandardKey.Paste),
                QKeySequence("Ctrl+V"),
            )
        )
        self.paste_action.setToolTip("Paste (Ctrl+V)")
        self.paste_action.setStatusTip("Paste the copied or cut command set (Ctrl+V)")
        self.paste_action.triggered.connect(self.paste)

        self.delete_action = QAction(_icon("fa5s.trash-alt", "#b42318"), "Delete", self)
        self.delete_action.setShortcuts(
            _unique_shortcuts(
                *QKeySequence.keyBindings(QKeySequence.StandardKey.Delete),
                QKeySequence(Qt.Key.Key_Delete),
            )
        )
        self.delete_action.setToolTip("Delete (Del)")
        self.delete_action.setStatusTip("Delete the selected commands or Scan blocks (Delete)")
        self.delete_action.triggered.connect(self.delete_selected)

        self.move_up_action = QAction(_icon("fa5s.arrow-up"), "Move Up", self)
        self.move_up_action.setShortcut(QKeySequence("Alt+Up"))
        self.move_up_action.setToolTip("Move Up (Alt+Up)")
        self.move_up_action.setStatusTip("Move the selected command up (Alt+Up)")
        self.move_up_action.triggered.connect(lambda _checked=False: self.move_selected(-1))

        self.move_down_action = QAction(_icon("fa5s.arrow-down"), "Move Down", self)
        self.move_down_action.setShortcut(QKeySequence("Alt+Down"))
        self.move_down_action.setToolTip("Move Down (Alt+Down)")
        self.move_down_action.setStatusTip("Move the selected command down (Alt+Down)")
        self.move_down_action.triggered.connect(lambda _checked=False: self.move_selected(1))

        actions = (
            self.undo_action,
            self.redo_action,
            self.disable_action,
            self.enable_action,
            self.cut_action,
            self.copy_action,
            self.paste_action,
            self.delete_action,
            self.move_up_action,
            self.move_down_action,
        )
        for action in actions:
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
        self._update_action_states()

    def set_document(self, document: SequenceDocument) -> None:
        """切换当前文档并清除旧选择与撤销历史。"""

        self.document = document
        self._running_command_id = ""
        self._undo_stack.clear()
        self._redo_stack.clear()
        self.rebuild(preserve_selection=False)

    def set_available_module_commands(
        self,
        commands: set[tuple[str, str]],
    ) -> None:
        """更新 Enabled 模块注册表，并立即重绘缺失指令而不改动文档。"""

        normalized = {
            (str(module_id), str(command_id))
            for module_id, command_id in commands
        }
        if normalized == self._available_module_commands:
            return
        self._available_module_commands = normalized
        self.rebuild()

    def set_editable(self, editable: bool) -> None:
        """控制修改动作与拖放是否可用；运行时仍允许滚动和查看列表。"""

        self._editable = editable
        self.list.setEnabled(True)
        self.list.setDragEnabled(editable)
        self.list.setAcceptDrops(editable)
        self.list.viewport().setAcceptDrops(editable)
        self._update_action_states()

    @staticmethod
    def _row_key(row: FlatRow | None) -> tuple[str | None, bool, bool] | None:
        """构造重建列表前后可比较的稳定行键。"""

        return None if row is None else (row.command_id, row.is_end, row.is_sequence_end)

    def set_running_command(
        self,
        command_id: str,
        *,
        scroll: bool | None = None,
    ) -> None:
        """Mark the executing command without changing the user's selection."""

        changed = command_id != self._running_command_id
        should_scroll = changed if scroll is None else scroll
        self._running_command_id = command_id
        for index in range(self.list.count()):
            item = self.list.item(index)
            row = item.data(Qt.ItemDataRole.UserRole)
            running = bool(command_id) and row.command_id == command_id and not row.is_end
            item.setData(Qt.ItemDataRole.UserRole + 1, running)
            if running and should_scroll:
                horizontal = self.list.horizontalScrollBar().value()
                self.list.scrollToItem(item)
                self.list.horizontalScrollBar().setValue(horizontal)

    def rebuild(
        self,
        select_command_id: str | None = None,
        *,
        select_command_ids: set[str] | None = None,
        preserve_selection: bool = True,
        restore_selected_keys: tuple[tuple[str | None, bool, bool], ...] | None = None,
        restore_current_key: tuple[str | None, bool, bool] | None = None,
    ) -> None:
        """从文档重新生成列表，并尽可能恢复多选、当前项和水平滚动位置。"""

        if restore_selected_keys is not None or restore_current_key is not None:
            selected_keys = set(restore_selected_keys or ())
            current_key = restore_current_key
        else:
            selected_keys = (
                {self._row_key(row) for row in self.selected_rows()}
                if preserve_selection and select_command_id is None and select_command_ids is None
                else set()
            )
            current_key = (
                self._row_key(self.selected_row())
                if preserve_selection and select_command_id is None and select_command_ids is None
                else None
            )
        forced_ids = set(select_command_ids or ())
        if select_command_id is not None:
            forced_ids.add(select_command_id)
        self.list.clear()
        current_item: QListWidgetItem | None = None
        selected_items: list[QListWidgetItem] = []
        for row in self.document.flat_rows():
            command = self.document.find(row.command_id or "") if row.command_id else None
            row_tooltip = ""
            if row.is_sequence_end:
                text = "End Sequence"
            elif row.is_end:
                text = "    " * row.depth + "End Scan"
            else:
                if command is not None:
                    body, row_tooltip = _display_command_and_tooltip(command)
                else:
                    body = "Unknown"
                if command is not None and not command.enabled:
                    body = f"[Disabled] {body}"
                text = "    " * row.depth + body
            item = QListWidgetItem(text)
            item.setData(_ROLE_ROW, row)
            if row_tooltip:
                item.setToolTip(row_tooltip)
            if row.is_sequence_end:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
                item.setForeground(QColor("#8c959f"))
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            elif row.is_end:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
                item.setForeground(QColor("#7b8794"))
            else:
                if command is not None:
                    fg, is_bold, is_italic = _semantic_style(command)
                    item.setForeground(fg)
                    if is_bold or is_italic:
                        font = item.font()
                        font.setBold(is_bold)
                        font.setItalic(is_italic)
                        item.setFont(font)
                unavailable_module_command = (
                    command is not None
                    and command.type
                    in {
                        CommandType.MODULE_COMMAND,
                        CommandType.MODULE_SCAN,
                    }
                    and (
                        command.module_id,
                        command.module_command_id,
                    )
                    not in self._available_module_commands
                )
                if unavailable_module_command:
                    item.setBackground(QColor("#ffd8d8"))
                    item.setToolTip(
                        "Module command unavailable: enable the matching module and command ID before Run"
                    )
                elif command is not None and command.type is CommandType.UNKNOWN:
                    item.setBackground(QColor("#fff4cc"))
            if not row.effective_enabled:
                item.setForeground(QColor("#98a2b3"))
                font = item.font()
                font.setBold(False)
                font.setStrikeOut(True)
                item.setFont(font)
                if command is not None and not command.enabled:
                    item.setToolTip("Disabled command - skipped when the sequence runs")
                else:
                    item.setToolTip("Inactive because a parent Scan is disabled")
            self.list.addItem(item)
            should_select = self._row_key(row) in selected_keys or (
                row.command_id in forced_ids and not row.is_end
            )
            if should_select:
                item.setSelected(True)
                selected_items.append(item)
            if self._row_key(row) == current_key or (
                select_command_id is not None
                and row.command_id == select_command_id
                and not row.is_end
            ):
                current_item = item
        if current_item is None and selected_items:
            current_item = selected_items[-1]
        if current_item is not None:
            self.list.setCurrentItem(
                current_item,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        elif self.list.count():
            item = self.list.item(self.list.count() - 1)
            item.setSelected(True)
            self.list.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
        # 添加或选中长命令时 QListWidget 可能自动滚到文本右端；总是回到最左侧，让命令前缀
        # 在大字体和 4K 缩放下仍然可见。
        self.list.horizontalScrollBar().setValue(0)
        self.set_running_command(self._running_command_id, scroll=False)
        self._update_action_states()

    def selected_row(self) -> FlatRow | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def selected_rows(self) -> list[FlatRow]:
        return [
            self.list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.list.count())
            if self.list.item(index).isSelected()
        ]

    def selected_commands(self, *, prune_descendants: bool = False) -> list[Command]:
        """按文档顺序返回选中命令；可去掉已选父扫描下的重复子节点。"""

        selected_ids = {
            row.command_id for row in self.selected_rows() if row.command_id is not None
        }
        result: list[Command] = []

        def visit(commands: list[Command], selected_ancestor: bool) -> None:
            for command in commands:
                selected = command.id in selected_ids
                if selected and (not prune_descendants or not selected_ancestor):
                    result.append(command)
                visit(
                    command.children,
                    selected_ancestor or (selected if prune_descendants else False),
                )

        visit(self.document.commands, False)
        return result

    def selected_command(self) -> Command | None:
        row = self.selected_row()
        if row is None or row.command_id is None:
            return None
        return self.document.find(row.command_id)

    def _capture_snapshot(self) -> _EditorSnapshot:
        selected_keys = tuple(
            key
            for row in self.selected_rows()
            if (key := self._row_key(row)) is not None
        )
        return _EditorSnapshot(
            commands=deepcopy(self.document.commands),
            selected_keys=selected_keys,
            current_key=self._row_key(self.selected_row()),
        )

    def _push_undo_snapshot(self, snapshot: _EditorSnapshot) -> None:
        self._undo_stack.append(snapshot)
        if len(self._undo_stack) > _MAX_UNDO_DEPTH:
            del self._undo_stack[:-_MAX_UNDO_DEPTH]
        self._redo_stack.clear()

    def record_undo_state(self) -> None:
        """在外部弹窗直接修改当前文档前记录一次撤销点。"""

        self._push_undo_snapshot(self._capture_snapshot())
        self._update_action_states()

    def _restore_snapshot(self, snapshot: _EditorSnapshot) -> None:
        self.document.commands[:] = deepcopy(snapshot.commands)
        self.rebuild(
            preserve_selection=False,
            restore_selected_keys=snapshot.selected_keys,
            restore_current_key=snapshot.current_key,
        )
        self.documentChanged.emit()

    def undo(self) -> None:
        """撤销最近一次 SEQ 编辑，并恢复当时选中行。"""

        if not self._editable or not self._undo_stack:
            return
        self._redo_stack.append(self._capture_snapshot())
        if len(self._redo_stack) > _MAX_UNDO_DEPTH:
            del self._redo_stack[:-_MAX_UNDO_DEPTH]
        snapshot = self._undo_stack.pop()
        self._restore_snapshot(snapshot)

    def redo(self) -> None:
        """重做最近一次撤销的 SEQ 编辑，并恢复当时选中行。"""

        if not self._editable or not self._redo_stack:
            return
        self._undo_stack.append(self._capture_snapshot())
        if len(self._undo_stack) > _MAX_UNDO_DEPTH:
            del self._undo_stack[:-_MAX_UNDO_DEPTH]
        snapshot = self._redo_stack.pop()
        self._restore_snapshot(snapshot)

    def _can_move_selected(self, offset: int) -> bool:
        command = self.selected_command()
        if command is None:
            return False
        location = self.document._locate(command.id)
        if location is None:
            return False
        parent, index = location
        return 0 <= index + offset < len(parent)

    def insert_command(
        self,
        command: Command,
        *,
        anchor: FlatRow | None = None,
        after: bool | None = None,
    ) -> None:
        """按当前行或拖放目标的树形语义插入命令并发出一次文档变化信号。"""

        if not self._editable:
            return
        self._push_undo_snapshot(self._capture_snapshot())
        if after is not None:
            self.document.insert_at(command, anchor, after=after)
        else:
            self.document.insert(command, self.selected_row(), before=True)
        self.rebuild(command.id)
        self.documentChanged.emit()

    def relocate_commands(
        self,
        command_ids: Sequence[str],
        target_row: FlatRow | None,
        *,
        after: bool = False,
    ) -> bool:
        """在单次撤销步内把拖拽的命令（含完整 Scan 子树）移动到目标行间位置。"""

        if not self._editable:
            return False
        moved_commands = self.document._top_level_commands_in_order(command_ids)
        if not moved_commands:
            return False
        snapshot = self._capture_snapshot()
        if not self.document.relocate(command_ids, target_row, after=after):
            return False
        self._push_undo_snapshot(snapshot)
        moved_ids = {command.id for command in moved_commands}
        self.rebuild(
            moved_commands[-1].id,
            select_command_ids=moved_ids,
            preserve_selection=False,
        )
        self.documentChanged.emit()
        return True

    def delete_selected(self) -> None:
        """批量删除命令；选中扫描时整块删除且不重复处理其子节点。"""

        if not self._editable:
            return
        commands = self.selected_commands(prune_descendants=True)
        if not commands:
            return
        snapshot = self._capture_snapshot()
        selected_indices = [
            index for index in range(self.list.count()) if self.list.item(index).isSelected()
        ]
        changed = False
        for command in commands:
            changed = self.document.delete(command.id) or changed
        if changed:
            self._push_undo_snapshot(snapshot)
            target_index = min(selected_indices, default=0)
            self.rebuild(preserve_selection=False)
            if self.list.count():
                self.list.setCurrentRow(min(target_index, self.list.count() - 1))
            self.documentChanged.emit()

    def cut_selected(self) -> None:
        """剪切顶层选中子树到控件内剪贴板，并在单次撤销步内删除原节点。"""

        if not self._editable:
            return
        commands = self.selected_commands(prune_descendants=True)
        if not commands:
            return
        snapshot = self._capture_snapshot()
        cloned = tuple(command.clone() for command in commands)
        selected_indices = [
            index for index in range(self.list.count()) if self.list.item(index).isSelected()
        ]
        changed = False
        for command in commands:
            changed = self.document.delete(command.id) or changed
        if changed:
            self._clipboard = cloned
            self._push_undo_snapshot(snapshot)
            target_index = min(selected_indices, default=0)
            self.rebuild(preserve_selection=False)
            if self.list.count():
                self.list.setCurrentRow(min(target_index, self.list.count() - 1))
            self.documentChanged.emit()

    def disable_selected(self) -> None:
        self._set_selected_enabled(False)

    def enable_selected(self) -> None:
        self._set_selected_enabled(True)

    def _set_selected_enabled(self, enabled: bool) -> None:
        """批量修改节点自身 T/F 状态，保留后代原始开关。"""

        if not self._editable:
            return
        commands = self.selected_commands()
        if not commands:
            return
        snapshot = self._capture_snapshot()
        changed = False
        for command in commands:
            changed = self.document.set_enabled(command.id, enabled) or changed
        if changed:
            self._push_undo_snapshot(snapshot)
            self.rebuild()
            self.documentChanged.emit()

    def move_selected(self, offset: int) -> None:
        if not self._editable:
            return
        command = self.selected_command()
        if command is None:
            return
        snapshot = self._capture_snapshot()
        if self.document.move(command.id, offset):
            self._push_undo_snapshot(snapshot)
            self.rebuild(command.id)
            self.documentChanged.emit()

    def copy_selected(self) -> None:
        """复制顶层选中子树到控件内剪贴板。"""

        commands = self.selected_commands(prune_descendants=True)
        if commands:
            self._clipboard = tuple(command.clone() for command in commands)
        self._update_action_states()

    def paste(self) -> None:
        """在选中行前连续粘贴同级子树；End Scan 处追加到扫描内部。"""

        if not self._editable or not self._clipboard:
            return
        self._push_undo_snapshot(self._capture_snapshot())
        anchor = self.selected_row()
        inserted: list[Command] = []
        for template in self._clipboard:
            duplicate = template.clone()
            if inserted:
                self.document.insert_after(duplicate, inserted[-1].id)
            else:
                self.document.insert(duplicate, anchor, before=True)
            inserted.append(duplicate)
        self.rebuild(
            inserted[-1].id,
            select_command_ids={command.id for command in inserted},
            preserve_selection=False,
        )
        self.documentChanged.emit()

    def _update_action_states(self) -> None:
        if not hasattr(self, "disable_action"):
            return
        commands = self.selected_commands()
        has_command = bool(commands)
        self.undo_action.setEnabled(self._editable and bool(self._undo_stack))
        self.redo_action.setEnabled(self._editable and bool(self._redo_stack))
        self.disable_action.setEnabled(
            self._editable and any(command.enabled for command in commands)
        )
        self.enable_action.setEnabled(
            self._editable and any(not command.enabled for command in commands)
        )
        self.cut_action.setEnabled(self._editable and has_command)
        self.copy_action.setEnabled(has_command)
        self.paste_action.setEnabled(self._editable and bool(self._clipboard))
        self.delete_action.setEnabled(self._editable and has_command)
        self.move_up_action.setEnabled(self._editable and self._can_move_selected(-1))
        self.move_down_action.setEnabled(self._editable and self._can_move_selected(1))

    def build_context_menu(self) -> QMenu:
        """构造 SEQ 行菜单；单独暴露以便确定性 UI 测试。"""

        self._update_action_states()
        menu = QMenu(self)
        menu.addActions([self.undo_action, self.redo_action])
        menu.addSeparator()
        menu.addActions([self.disable_action, self.enable_action])
        menu.addSeparator()
        menu.addActions([
            self.cut_action,
            self.copy_action,
            self.paste_action,
            self.delete_action,
        ])
        menu.addSeparator()
        menu.addActions([self.move_up_action, self.move_down_action])
        return menu

    def _show_context_menu(self, position) -> None:
        item = self.list.itemAt(position)
        if item is not None:
            if not item.isSelected():
                self.list.clearSelection()
                item.setSelected(True)
            self.list.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
        menu = self.build_context_menu()
        menu.exec(self.list.viewport().mapToGlobal(position))

    def _on_double_clicked(self, item: QListWidgetItem) -> None:
        if not self._editable:
            return
        row: FlatRow = item.data(Qt.ItemDataRole.UserRole)
        if row.is_end or row.is_sequence_end or row.command_id is None:
            return
        command = self.document.find(row.command_id)
        if command is not None and command.type is not CommandType.UNKNOWN:
            self.commandDoubleClicked.emit(command)
