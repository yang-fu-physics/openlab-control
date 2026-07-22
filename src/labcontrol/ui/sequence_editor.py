from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QMenu, QVBoxLayout, QWidget

from ..sequence.model import Command, CommandType, FlatRow, SequenceDocument
from ..sequence.parser import format_command


class SequenceEditorWidget(QWidget):
    commandDoubleClicked = Signal(object)
    documentChanged = Signal()

    def __init__(self, document: SequenceDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.document = document
        self._clipboard: tuple[Command, ...] = ()
        self._editable = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemDoubleClicked.connect(self._on_double_clicked)
        self.list.currentItemChanged.connect(lambda current, previous: self._update_action_states())
        self.list.itemSelectionChanged.connect(self._update_action_states)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.list)
        self._build_actions()
        self.rebuild()

    def _build_actions(self) -> None:
        self.disable_action = QAction("Disable", self)
        self.disable_action.setShortcut(QKeySequence("Ctrl+D"))
        self.disable_action.setStatusTip("Disable the selected commands (Ctrl+D)")
        self.disable_action.triggered.connect(self.disable_selected)

        self.enable_action = QAction("Enable", self)
        self.enable_action.setShortcut(QKeySequence("Ctrl+E"))
        self.enable_action.setStatusTip("Enable the selected commands (Ctrl+E)")
        self.enable_action.triggered.connect(self.enable_selected)

        self.delete_action = QAction("Delete", self)
        self.delete_action.setShortcuts(
            QKeySequence.keyBindings(QKeySequence.StandardKey.Delete)
        )
        self.delete_action.setStatusTip("Delete the selected commands or Scan blocks (Delete)")
        self.delete_action.triggered.connect(self.delete_selected)

        self.copy_action = QAction("Copy", self)
        self.copy_action.setShortcuts(QKeySequence.keyBindings(QKeySequence.StandardKey.Copy))
        self.copy_action.setStatusTip("Copy the selected commands or Scan blocks (Ctrl+C)")
        self.copy_action.triggered.connect(self.copy_selected)

        self.paste_action = QAction("Paste", self)
        self.paste_action.setShortcuts(QKeySequence.keyBindings(QKeySequence.StandardKey.Paste))
        self.paste_action.setStatusTip("Paste the copied command set (Ctrl+V)")
        self.paste_action.triggered.connect(self.paste)

        actions = (
            self.disable_action,
            self.enable_action,
            self.delete_action,
            self.copy_action,
            self.paste_action,
        )
        for action in actions:
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
        self._update_action_states()

    def set_document(self, document: SequenceDocument) -> None:
        self.document = document
        self.rebuild(preserve_selection=False)

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        self.list.setEnabled(True)
        self._update_action_states()

    @staticmethod
    def _row_key(row: FlatRow | None) -> tuple[str | None, bool, bool] | None:
        return None if row is None else (row.command_id, row.is_end, row.is_sequence_end)

    def rebuild(
        self,
        select_command_id: str | None = None,
        *,
        select_command_ids: set[str] | None = None,
        preserve_selection: bool = True,
    ) -> None:
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
            if row.is_sequence_end:
                text = "End Sequence"
            elif row.is_end:
                text = "    " * row.depth + "End Scan"
            else:
                body = format_command(command) if command is not None else "Unknown"
                if command is not None and not command.enabled:
                    body = f"[Disabled] {body}"
                text = "    " * row.depth + body
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row)
            if row.is_end or row.is_sequence_end:
                item.setForeground(QColor("#6b7280"))
            else:
                if command is not None and command.type is CommandType.UNKNOWN:
                    item.setBackground(QColor("#fff4cc"))
            if not row.effective_enabled:
                item.setForeground(QColor("#8a9099"))
                font = item.font()
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
        # Adding or selecting a long command can make QListWidget reveal its
        # right edge. Always return to column zero so command prefixes remain
        # visible, especially with larger accessibility fonts.
        self.list.horizontalScrollBar().setValue(0)
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

    def insert_command(self, command: Command) -> None:
        if not self._editable:
            return
        self.document.insert(command, self.selected_row())
        self.rebuild(command.id)
        self.documentChanged.emit()

    def delete_selected(self) -> None:
        if not self._editable:
            return
        commands = self.selected_commands(prune_descendants=True)
        selected_indices = [
            index for index in range(self.list.count()) if self.list.item(index).isSelected()
        ]
        changed = False
        for command in commands:
            changed = self.document.delete(command.id) or changed
        if changed:
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
        if not self._editable:
            return
        commands = self.selected_commands()
        changed = False
        for command in commands:
            changed = self.document.set_enabled(command.id, enabled) or changed
        if changed:
            self.rebuild()
            self.documentChanged.emit()

    def move_selected(self, offset: int) -> None:
        if not self._editable:
            return
        command = self.selected_command()
        if command is not None and self.document.move(command.id, offset):
            self.rebuild(command.id)
            self.documentChanged.emit()

    def copy_selected(self) -> None:
        commands = self.selected_commands(prune_descendants=True)
        if commands:
            self._clipboard = tuple(command.clone() for command in commands)
        self._update_action_states()

    def paste(self) -> None:
        if not self._editable or not self._clipboard:
            return
        anchor = self.selected_row()
        inserted: list[Command] = []
        for template in self._clipboard:
            duplicate = template.clone()
            self.document.insert(duplicate, anchor)
            inserted.append(duplicate)
            anchor = FlatRow(duplicate.id, 0, False)
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
        self.disable_action.setEnabled(
            self._editable and any(command.enabled for command in commands)
        )
        self.enable_action.setEnabled(
            self._editable and any(not command.enabled for command in commands)
        )
        self.delete_action.setEnabled(self._editable and has_command)
        self.copy_action.setEnabled(has_command)
        self.paste_action.setEnabled(self._editable and bool(self._clipboard))

    def build_context_menu(self) -> QMenu:
        """Build the SEQ row menu; exposed separately for deterministic UI tests."""

        self._update_action_states()
        menu = QMenu(self)
        menu.addAction(self.disable_action)
        menu.addAction(self.enable_action)
        menu.addSeparator()
        menu.addAction(self.delete_action)
        menu.addAction(self.copy_action)
        menu.addAction(self.paste_action)
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
