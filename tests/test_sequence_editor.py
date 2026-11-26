from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.sequence.model import Command, CommandType, SequenceDocument  # noqa: E402
from labcontrol.ui.sequence_editor import SequenceEditorWidget  # noqa: E402


class SequenceEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    @staticmethod
    def _select_rows(editor: SequenceEditorWidget, *indices: int) -> None:
        editor.list.clearSelection()
        for index in indices:
            editor.list.item(index).setSelected(True)
        if indices:
            editor.list.setCurrentItem(
                editor.list.item(indices[-1]),
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )

    def test_palette_insert_is_before_selected_row_and_inside_end_scan(self):
        child = Command(CommandType.MEASURE)
        scan = Command(CommandType.SCAN_TIME, {"duration_seconds": 1.0, "steps": 2}, [child])
        editor = SequenceEditorWidget(SequenceDocument([scan]))
        self._select_rows(editor, 0)
        before_scan = Command(CommandType.REMARK, {"text": "before scan"})
        editor.insert_command(before_scan)
        self.assertEqual(editor.document.commands, [before_scan, scan])
        self._select_rows(editor, 2)
        before_child = Command(CommandType.REMARK, {"text": "before child"})
        editor.insert_command(before_child)
        self.assertEqual(scan.children, [before_child, child])
        end_index = next(i for i in range(editor.list.count()) if editor.list.item(i).data(Qt.ItemDataRole.UserRole).is_end)
        self._select_rows(editor, end_index)
        at_end = Command(CommandType.REMARK, {"text": "inside scan"})
        editor.insert_command(at_end)
        self.assertEqual(scan.children, [before_child, child, at_end])
        row = editor.selected_row()
        self.assertEqual(row.depth, 1)
        self.assertFalse(row.is_end)
        editor.close()

    def test_context_menu_and_keyboard_edit_complete_nodes(self) -> None:
        scan = Command(
            CommandType.SCAN_TIME,
            {"duration_seconds": 1.0, "steps": 2},
            [Command(CommandType.MEASURE)],
        )
        document = SequenceDocument(
            [Command(CommandType.WAIT, {"seconds": 1.0}), scan],
            "keyboard.seq",
        )
        editor = SequenceEditorWidget(document)
        editor.resize(760, 480)
        editor.show()
        editor.activateWindow()
        editor.list.setFocus()
        self.application.processEvents()

        self._select_rows(editor, 0)
        menu = editor.build_context_menu()
        labels = [action.text() for action in menu.actions() if not action.isSeparator()]
        self.assertEqual(
            labels,
            [
                "Undo",
                "Redo",
                "Disable",
                "Enable",
                "Cut",
                "Copy",
                "Paste",
                "Delete",
                "Move Up",
                "Move Down",
            ],
        )
        self.assertFalse(editor.undo_action.isEnabled())
        self.assertFalse(editor.redo_action.isEnabled())
        self.assertTrue(editor.disable_action.isEnabled())
        self.assertFalse(editor.enable_action.isEnabled())
        self.assertTrue(editor.cut_action.isEnabled())
        self.assertFalse(editor.paste_action.isEnabled())
        self.assertFalse(editor.move_up_action.isEnabled())
        self.assertTrue(editor.move_down_action.isEnabled())

        changes: list[bool] = []
        editor.documentChanged.connect(lambda: changes.append(True))
        QTest.keyClick(
            editor.list,
            Qt.Key.Key_D,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.application.processEvents()
        self.assertFalse(document.commands[0].enabled)
        self.assertIn("[Disabled]", editor.list.item(0).text())
        self.assertTrue(editor.enable_action.isEnabled())

        QTest.keyClick(
            editor.list,
            Qt.Key.Key_E,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.application.processEvents()
        self.assertTrue(document.commands[0].enabled)

        self._select_rows(editor, 1)
        QTest.keyClick(
            editor.list,
            Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier,
        )
        self._select_rows(editor, editor.list.count() - 1)
        QTest.keyClick(
            editor.list,
            Qt.Key.Key_V,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.application.processEvents()
        self.assertEqual(len(document.commands), 3)
        pasted = document.commands[2]
        self.assertEqual(pasted.type, CommandType.SCAN_TIME)
        self.assertEqual(len(pasted.children), 1)
        self.assertNotEqual(pasted.id, scan.id)
        self.assertNotEqual(pasted.children[0].id, scan.children[0].id)

        QTest.keyClick(editor.list, Qt.Key.Key_Delete)
        self.application.processEvents()
        self.assertEqual(len(document.commands), 2)
        self.assertEqual(len(changes), 4)
        editor.close()

    def test_multiple_rows_batch_actions_and_hierarchy_deduplication(self) -> None:
        scan = Command(
            CommandType.SCAN_TIME,
            {"duration_seconds": 1.0, "steps": 2},
            [Command(CommandType.MEASURE)],
        )
        document = SequenceDocument([
            Command(CommandType.WAIT, {"seconds": 1.0}),
            Command(CommandType.WAIT, {"seconds": 2.0}),
            scan,
            Command(CommandType.REMARK, {"text": "tail"}),
        ])
        editor = SequenceEditorWidget(document)
        editor.resize(760, 480)
        editor.show()
        editor.activateWindow()
        editor.list.setFocus()
        self.application.processEvents()
        self.assertEqual(
            editor.list.selectionMode(),
            editor.list.SelectionMode.ExtendedSelection,
        )

        changes: list[bool] = []
        editor.documentChanged.connect(lambda: changes.append(True))
        self._select_rows(editor, 0, 1)
        QTest.keyClick(editor.list, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
        self.application.processEvents()
        self.assertFalse(document.commands[0].enabled)
        self.assertFalse(document.commands[1].enabled)
        self.assertEqual(len(editor.selected_commands()), 2)

        QTest.keyClick(editor.list, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(editor.list, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(editor._clipboard), 2)
        self._select_rows(editor, editor.list.count() - 1)
        QTest.keyClick(editor.list, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.application.processEvents()
        self.assertEqual(len(document.commands), 6)
        self.assertEqual([command.params["seconds"] for command in document.commands[-2:]], [1.0, 2.0])
        self.assertEqual(len(editor.selected_commands()), 2)
        QTest.keyClick(editor.list, Qt.Key.Key_Delete)
        self.application.processEvents()
        self.assertEqual(len(document.commands), 4)

        # Selecting both a Scan and its child copies only the complete parent
        # block, rather than duplicating the child as a second top-level node.
        self._select_rows(editor, 2, 3)
        QTest.keyClick(editor.list, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(editor._clipboard), 1)
        self.assertEqual(editor._clipboard[0].type, CommandType.SCAN_TIME)
        self.assertEqual(len(editor._clipboard[0].children), 1)
        self._select_rows(editor, editor.list.count() - 1)
        QTest.keyClick(editor.list, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.application.processEvents()
        self.assertEqual(len(document.commands), 5)
        self.assertEqual(document.commands[-1].type, CommandType.SCAN_TIME)
        self.assertEqual(len(document.commands[-1].children), 1)
        self.assertEqual(len(changes), 5)
        editor.close()

    def test_edit_lock_disables_mutating_actions_but_allows_copy(self) -> None:
        document = SequenceDocument([
            Command(CommandType.WAIT, {"seconds": 1.0}),
            Command(CommandType.REMARK, {"text": "second"}),
        ])
        editor = SequenceEditorWidget(document)
        self._select_rows(editor, 0)
        editor.insert_command(Command(CommandType.MEASURE))
        editor.undo()
        self._select_rows(editor, 0)
        editor.set_editable(False)
        self.assertFalse(editor.undo_action.isEnabled())
        self.assertFalse(editor.redo_action.isEnabled())
        self.assertFalse(editor.disable_action.isEnabled())
        self.assertFalse(editor.enable_action.isEnabled())
        self.assertFalse(editor.cut_action.isEnabled())
        self.assertFalse(editor.delete_action.isEnabled())
        self.assertFalse(editor.paste_action.isEnabled())
        self.assertFalse(editor.move_up_action.isEnabled())
        self.assertFalse(editor.move_down_action.isEnabled())
        self.assertTrue(editor.copy_action.isEnabled())
        editor.cut_selected()
        self.assertEqual(len(document.commands), 2)
        self.assertEqual(editor._clipboard, ())
        editor.close()

    def test_cut_paste_move_and_undo_redo_shortcuts(self) -> None:
        scan = Command(
            CommandType.SCAN_TIME,
            {"duration_seconds": 1.0, "steps": 2},
            [Command(CommandType.MEASURE)],
        )
        first = Command(CommandType.WAIT, {"seconds": 1.0})
        tail = Command(CommandType.REMARK, {"text": "tail"})
        document = SequenceDocument([first, scan, tail], "cut-undo.seq")
        editor = SequenceEditorWidget(document)
        editor.resize(760, 480)
        editor.show()
        editor.activateWindow()
        editor.list.setFocus()
        self.application.processEvents()

        try:
            # 1) Cut a Scan block while both the Scan and its child are selected (deduplicated).
            self._select_rows(editor, 1, 2)
            QTest.keyClick(
                editor.list,
                Qt.Key.Key_X,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual([c.type for c in document.commands], [CommandType.WAIT, CommandType.REMARK])
            self.assertEqual(len(editor._clipboard), 1)
            self.assertEqual(editor._clipboard[0].type, CommandType.SCAN_TIME)
            self.assertEqual(len(editor._clipboard[0].children), 1)
            self.assertTrue(editor.undo_action.isEnabled())

            # 2) Paste at End Sequence, then paste a second time to confirm distinct IDs.
            self._select_rows(editor, editor.list.count() - 1)
            QTest.keyClick(
                editor.list,
                Qt.Key.Key_V,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual(
                [c.type for c in document.commands],
                [CommandType.WAIT, CommandType.REMARK, CommandType.SCAN_TIME],
            )
            pasted_once = document.commands[2]
            self.assertNotEqual(pasted_once.id, scan.id)
            self.assertEqual(len(pasted_once.children), 1)

            # 3) Move pasted Scan up with Alt+Up, then back down with Alt+Down.
            QTest.keyClick(editor.list, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
            self.application.processEvents()
            self.assertEqual(
                [c.type for c in document.commands],
                [CommandType.WAIT, CommandType.SCAN_TIME, CommandType.REMARK],
            )

            # 4) Undo move, undo paste, undo cut via Ctrl+Z -> restores exact original tree.
            QTest.keyClick(editor.list, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
            self.application.processEvents()
            self.assertEqual(
                [c.type for c in document.commands],
                [CommandType.WAIT, CommandType.REMARK, CommandType.SCAN_TIME],
            )
            QTest.keyClick(editor.list, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
            self.application.processEvents()
            self.assertEqual(
                [c.type for c in document.commands],
                [CommandType.WAIT, CommandType.REMARK],
            )
            QTest.keyClick(editor.list, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
            self.application.processEvents()
            self.assertEqual(
                [c.type for c in document.commands],
                [CommandType.WAIT, CommandType.SCAN_TIME, CommandType.REMARK],
            )
            self.assertEqual(document.commands[1].id, scan.id)
            self.assertFalse(editor.undo_action.isEnabled())
            self.assertTrue(editor.redo_action.isEnabled())

            # 5) Redo cut with Ctrl+Y and redo paste with Ctrl+Shift+Z.
            QTest.keyClick(editor.list, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
            self.application.processEvents()
            self.assertEqual(
                [c.type for c in document.commands],
                [CommandType.WAIT, CommandType.REMARK],
            )
            QTest.keyClick(
                editor.list,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
            )
            self.application.processEvents()
            self.assertEqual(
                [c.type for c in document.commands],
                [CommandType.WAIT, CommandType.REMARK, CommandType.SCAN_TIME],
            )

            # 6) New mutation clears redo stack; set_document clears both stacks.
            self._select_rows(editor, 0)
            QTest.keyClick(editor.list, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
            self.application.processEvents()
            self.assertFalse(editor.redo_action.isEnabled())
            self.assertFalse(document.commands[0].enabled)

            editor.set_document(SequenceDocument([Command(CommandType.MEASURE)]))
            self.assertFalse(editor.undo_action.isEnabled())
            self.assertFalse(editor.redo_action.isEnabled())
        finally:
            editor.close()

    def test_batch_paste_keeps_items_after_a_scan_as_siblings(self) -> None:
        scan = Command(
            CommandType.SCAN_TIME,
            {"duration_seconds": 1.0, "steps": 2},
            [Command(CommandType.MEASURE)],
        )
        document = SequenceDocument([
            scan,
            Command(CommandType.REMARK, {"text": "sibling"}),
        ])
        editor = SequenceEditorWidget(document)
        editor.show()
        self.application.processEvents()

        self._select_rows(editor, 0, 3)
        editor.copy_selected()
        self.assertEqual(len(editor._clipboard), 2)
        self._select_rows(editor, editor.list.count() - 1)
        editor.paste()

        self.assertEqual(len(document.commands), 4)
        pasted_scan = document.commands[2]
        pasted_sibling = document.commands[3]
        self.assertEqual(pasted_scan.type, CommandType.SCAN_TIME)
        self.assertEqual(
            [child.type for child in pasted_scan.children],
            [CommandType.MEASURE],
        )
        self.assertEqual(pasted_sibling.type, CommandType.REMARK)
        self.assertEqual(pasted_sibling.params["text"], "sibling")
        editor.close()

    def test_batch_paste_before_nested_scan_preserves_sibling_order_and_subtrees(self) -> None:
        source_scan = Command(CommandType.SCAN_TIME, {"duration_seconds": 1.0, "steps": 2},
                              [Command(CommandType.MEASURE)])
        source_remark = Command(CommandType.REMARK, {"text": "copied sibling"})
        inner_child = Command(CommandType.WAIT, {"seconds": 1.0})
        inner_scan = Command(CommandType.SCAN_TIME, {"duration_seconds": 2.0, "steps": 2}, [inner_child])
        tail = Command(CommandType.MEASURE)
        outer_scan = Command(CommandType.SCAN_TIME, {"duration_seconds": 3.0, "steps": 2}, [inner_scan, tail])
        document = SequenceDocument([source_scan, source_remark, outer_scan])
        editor = SequenceEditorWidget(document)
        self._select_rows(editor, 0, 3)
        editor.copy_selected()
        self._select_rows(editor, 5)
        changes = []
        editor.documentChanged.connect(lambda: changes.append(True))
        editor.paste_action.trigger()
        self.assertEqual(len(outer_scan.children), 4)
        pasted_scan, pasted_remark = outer_scan.children[:2]
        self.assertEqual(outer_scan.children[2:], [inner_scan, tail])
        self.assertEqual(inner_scan.children, [inner_child])
        self.assertEqual(pasted_scan.type, CommandType.SCAN_TIME)
        self.assertEqual([c.type for c in pasted_scan.children], [CommandType.MEASURE])
        self.assertEqual(pasted_remark.params, source_remark.params)
        self.assertNotEqual(pasted_scan.id, source_scan.id)
        self.assertNotEqual(pasted_scan.children[0].id, source_scan.children[0].id)
        self.assertEqual([c.id for c in editor.selected_commands()], [pasted_scan.id, pasted_remark.id])
        self.assertEqual(len(changes), 1)
        self.assertEqual(document.commands, [source_scan, source_remark, outer_scan])
        editor.close()

    def test_paste_before_command_and_at_nested_end_markers(self) -> None:
        for anchor in ("child", "inner_end", "outer_end", "sequence_end", "none"):
            with self.subTest(anchor=anchor):
                source = Command(CommandType.REMARK, {"text": "copy"})
                child = Command(CommandType.MEASURE)
                inner = Command(CommandType.SCAN_TIME, {"duration_seconds": 1.0, "steps": 2}, [child])
                outer = Command(CommandType.SCAN_TIME, {"duration_seconds": 2.0, "steps": 2}, [inner])
                document = SequenceDocument([source, outer])
                editor = SequenceEditorWidget(document)
                self._select_rows(editor, 0)
                editor.copy_selected()
                if anchor == "none":
                    editor.list.setCurrentRow(-1)
                else:
                    self._select_rows(editor, {"child": 3, "inner_end": 4,
                                               "outer_end": 5, "sequence_end": 6}[anchor])
                editor.paste()
                if anchor == "child":
                    self.assertEqual([c.type for c in inner.children], [CommandType.REMARK, CommandType.MEASURE])
                elif anchor == "inner_end":
                    self.assertEqual([c.type for c in inner.children], [CommandType.MEASURE, CommandType.REMARK])
                elif anchor == "outer_end":
                    self.assertEqual([c.type for c in outer.children], [CommandType.SCAN_TIME, CommandType.REMARK])
                    self.assertEqual(inner.children, [child])
                else:
                    self.assertEqual([c.type for c in document.commands],
                                     [CommandType.REMARK, CommandType.SCAN_TIME, CommandType.REMARK])
                    self.assertEqual(outer.children, [inner])
                editor.close()

    def test_rebuild_keeps_command_prefixes_visible(self) -> None:
        document = SequenceDocument([
            Command(CommandType.REMARK, {"text": "long command " * 30}),
        ])
        editor = SequenceEditorWidget(document)
        editor.resize(260, 180)
        editor.show()
        self.application.processEvents()
        scrollbar = editor.list.horizontalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        scrollbar.setValue(scrollbar.maximum())
        editor.rebuild()
        self.application.processEvents()
        self.assertEqual(scrollbar.value(), 0)
        editor.close()

    def test_temperature_list_display_uses_brackets_and_input_precision(self) -> None:
        command = Command(
            CommandType.SCAN_TEMPERATURE,
            {
                "point_mode": "List",
                "points": "[300, 299.90, 20.000]",
                "rate": 5.0,
                "mode": "Settle",
            },
            raw_text=(
                "Scan Temperature List 300, 299.90, 20.000 K "
                "at 5 K/min, Settle"
            ),
        )
        editor = SequenceEditorWidget(SequenceDocument([command]))

        self.assertEqual(
            editor.list.item(0).text(),
            (
                "Scan Temperature List [300, 299.90, 20.000] K "
                "at 5.000 K/min, Settle"
            ),
        )
        editor.close()

    def test_set_running_command_only_scrolls_when_active_command_changes(self) -> None:
        commands = [
            Command(CommandType.REMARK, {"text": f"step {index}"})
            for index in range(10)
        ]
        editor = SequenceEditorWidget(SequenceDocument(commands))
        try:
            with patch.object(editor.list, "scrollToItem") as scroll_to_item:
                editor.set_running_command(commands[0].id)
                editor.set_running_command(commands[0].id)
                self.assertEqual(scroll_to_item.call_count, 1)
                editor.set_running_command(commands[1].id)
                self.assertEqual(scroll_to_item.call_count, 2)
                editor.set_running_command("")
                self.assertEqual(scroll_to_item.call_count, 2)
                self.assertFalse(
                    any(
                        editor.list.item(index).data(Qt.ItemDataRole.UserRole + 1)
                        for index in range(editor.list.count())
                    )
                )
        finally:
            editor.close()

    def test_mini_toolbar_semantic_styles_and_elided_datafile_tooltip(self) -> None:
        long_path = (
            r"C:\Users\researcher\Desktop\cryostat-runs\2026\November\SampleGroupA"
            r"\20261125-O157S1-O157S2-O146IIS1-O146IIS2-LR700-warming.dat"
        )
        datafile = Command(
            CommandType.SET_DATAFILE,
            {"mode": "open|create", "path_scope": "Custom folder", "path": long_path},
        )
        disabled_remark = Command(CommandType.REMARK, {"text": "disabled note"}, enabled=False)
        wait_cmd = Command(CommandType.WAIT, {"wait_temperature": True, "seconds": 300.0})
        set_temp = Command(CommandType.SET_TEMPERATURE, {"target": 4.0, "rate": 2.0})
        inner_scan = Command(
            CommandType.SCAN_FIELD,
            {"start": 0.0, "stop": 1000.0, "steps": 11, "rate": 100.0, "unit": "Oe"},
            [Command(CommandType.MEASURE)],
        )
        outer_scan = Command(
            CommandType.SCAN_TEMPERATURE,
            {"start": 3.0, "stop": 30.0, "steps": 28, "rate": 0.5, "mode": "Sweep"},
            [inner_scan],
        )
        remark = Command(CommandType.REMARK, {"text": "comment line"})
        doc = SequenceDocument([datafile, disabled_remark, wait_cmd, set_temp, outer_scan, remark])
        editor = SequenceEditorWidget(doc)
        try:
            editor.resize(620, 420)
            editor.show()
            editor.activateWindow()
            editor.list.setFocus()
            self.application.processEvents()

            # Mini toolbar exposes all editing actions and buttons keep focus on the list.
            toolbar_actions = [a for a in editor.toolbar.actions() if not a.isSeparator()]
            self.assertEqual(
                [a.text() for a in toolbar_actions],
                [
                    "Undo",
                    "Redo",
                    "Cut",
                    "Copy",
                    "Paste",
                    "Delete",
                    "Move Up",
                    "Move Down",
                    "Disable",
                    "Enable",
                ],
            )
            for action in toolbar_actions:
                btn = editor.toolbar.widgetForAction(action)
                self.assertEqual(btn.focusPolicy(), Qt.FocusPolicy.NoFocus)

            # Long Set Datafile path is middle-elided in text while full command lives in tooltip.
            df_item = editor.list.item(0)
            self.assertIn("…", df_item.text())
            self.assertIn("LR700-warming.dat", df_item.text())
            self.assertIn(long_path, df_item.toolTip())

            # Disabled row uses strikeout muted text regardless of command type.
            disabled_item = editor.list.item(1)
            self.assertTrue(disabled_item.font().strikeOut())
            self.assertEqual(disabled_item.foreground().color().name(), "#98a2b3")

            # Semantic color coding differentiates Wait, Set Temp, Scan, Measure, Remark, End Sequence.
            self.assertEqual(editor.list.item(2).foreground().color().name(), "#8a5300")
            self.assertEqual(editor.list.item(3).foreground().color().name(), "#0f5c6e")
            scan_item = editor.list.item(4)
            self.assertEqual(scan_item.foreground().color().name(), "#1b4b8a")
            self.assertTrue(scan_item.font().bold())
            measure_item = editor.list.item(6)
            self.assertEqual(measure_item.foreground().color().name(), "#1a6f3c")
            self.assertTrue(measure_item.font().bold())
            remark_item = editor.list.item(9)
            self.assertTrue(remark_item.font().italic())
            end_seq_item = editor.list.item(editor.list.count() - 1)
            self.assertEqual(end_seq_item.text(), "End Sequence")
            self.assertTrue(end_seq_item.font().italic())

            # Running highlight + nested indent guides paint cleanly.
            editor.set_running_command(wait_cmd.id)
            self.application.processEvents()
            pixmap = editor.grab()
            self.assertFalse(pixmap.isNull())
        finally:
            editor.close()

    def test_drag_and_drop_reorders_crosses_scans_prevents_cycles_and_supports_undo(
        self,
    ) -> None:
        import json

        from PySide6.QtCore import QByteArray, QMimeData, QPointF
        from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent

        from labcontrol.ui.sequence_editor import SEQUENCE_COMMAND_MIME

        def _make_mime(ids: list[str]) -> QMimeData:
            mime = QMimeData()
            mime.setData(
                SEQUENCE_COMMAND_MIME,
                QByteArray(json.dumps(ids).encode("utf-8")),
            )
            return mime

        def _drop(editor: SequenceEditorWidget, ids: list[str], pos: QPointF) -> tuple[bool, bool]:
            mime = _make_mime(ids)
            enter_ev = QDragEnterEvent(
                pos.toPoint(),
                Qt.DropAction.MoveAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            editor.list.dragEnterEvent(enter_ev)
            move_ev = QDragMoveEvent(
                pos.toPoint(),
                Qt.DropAction.MoveAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            editor.list.dragMoveEvent(move_ev)
            move_accepted = move_ev.isAccepted()
            if move_accepted:
                # Exercise drop indicator paint path while active.
                editor.list.grab()
            drop_ev = QDropEvent(
                pos,
                Qt.DropAction.MoveAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            editor.list.dropEvent(drop_ev)
            return move_accepted, drop_ev.isAccepted()

        wait_cmd = Command(CommandType.WAIT, {"seconds": 10.0})
        measure_cmd = Command(CommandType.MEASURE)
        inner_scan = Command(
            CommandType.SCAN_TIME,
            {"duration_seconds": 5.0, "steps": 2},
            [measure_cmd],
        )
        outer_scan = Command(
            CommandType.SCAN_TEMPERATURE,
            {"start": 3.0, "stop": 10.0, "steps": 3, "rate": 1.0, "mode": "Settle"},
            [inner_scan],
        )
        tail_remark = Command(CommandType.REMARK, {"text": "tail"})
        doc = SequenceDocument([wait_cmd, outer_scan, tail_remark], "drag.seq")
        editor = SequenceEditorWidget(doc)
        try:
            editor.resize(620, 440)
            editor.show()
            self.application.processEvents()

            # End Scan and End Sequence markers must not be draggable.
            for idx in range(editor.list.count()):
                item = editor.list.item(idx)
                row = item.data(Qt.ItemDataRole.UserRole)
                if row.is_end or row.is_sequence_end:
                    self.assertFalse(bool(item.flags() & Qt.ItemFlag.ItemIsDragEnabled))
                else:
                    self.assertTrue(bool(item.flags() & Qt.ItemFlag.ItemIsDragEnabled))

            changes: list[bool] = []
            editor.documentChanged.connect(lambda: changes.append(True))

            # 1) Drag Wait INTO inner_scan (drop on lower half of inner_scan header -> index 0 inside).
            inner_rect = editor.list.visualItemRect(editor.list.item(2))
            below_inner = QPointF(inner_rect.center().x(), inner_rect.bottom() - 2)
            moved_ok, dropped_ok = _drop(editor, [wait_cmd.id], below_inner)
            self.assertTrue(moved_ok)
            self.assertTrue(dropped_ok)
            self.assertEqual([c.id for c in doc.commands], [outer_scan.id, tail_remark.id])
            self.assertEqual([c.id for c in inner_scan.children], [wait_cmd.id, measure_cmd.id])
            self.assertEqual(len(changes), 1)

            # 2) Drag Wait OUT of inner_scan below outer_scan's End Scan -> root after outer_scan.
            outer_end_idx = next(
                i
                for i in range(editor.list.count())
                if (r := editor.list.item(i).data(Qt.ItemDataRole.UserRole)).is_end
                and r.command_id == outer_scan.id
            )
            outer_end_rect = editor.list.visualItemRect(editor.list.item(outer_end_idx))
            below_outer_end = QPointF(outer_end_rect.center().x(), outer_end_rect.bottom() - 2)
            moved_ok, dropped_ok = _drop(editor, [wait_cmd.id], below_outer_end)
            self.assertTrue(moved_ok)
            self.assertTrue(dropped_ok)
            self.assertEqual(
                [c.id for c in doc.commands],
                [outer_scan.id, wait_cmd.id, tail_remark.id],
            )
            self.assertEqual([c.id for c in inner_scan.children], [measure_cmd.id])

            # 3) Cycle guard: dragging outer_scan into inner_scan must be rejected cleanly.
            inner_idx = next(
                i
                for i in range(editor.list.count())
                if (r := editor.list.item(i).data(Qt.ItemDataRole.UserRole)).command_id
                == inner_scan.id
                and not r.is_end
            )
            inner_rect = editor.list.visualItemRect(editor.list.item(inner_idx))
            below_inner = QPointF(inner_rect.center().x(), inner_rect.bottom() - 2)
            moved_ok, dropped_ok = _drop(editor, [outer_scan.id], below_inner)
            self.assertFalse(moved_ok)
            self.assertFalse(dropped_ok)
            self.assertEqual(
                [c.id for c in doc.commands],
                [outer_scan.id, wait_cmd.id, tail_remark.id],
            )
            self.assertEqual(len(changes), 2)

            # 4) Multi-select + descendant pruning: drag [outer_scan, measure_cmd] to end of sequence.
            end_seq_rect = editor.list.visualItemRect(editor.list.item(editor.list.count() - 1))
            above_end_seq = QPointF(end_seq_rect.center().x(), end_seq_rect.top() + 2)
            moved_ok, dropped_ok = _drop(editor, [outer_scan.id, measure_cmd.id], above_end_seq)
            self.assertTrue(moved_ok)
            self.assertTrue(dropped_ok)
            self.assertEqual(
                [c.id for c in doc.commands],
                [wait_cmd.id, tail_remark.id, outer_scan.id],
            )
            self.assertEqual([c.id for c in inner_scan.children], [measure_cmd.id])
            self.assertEqual(len(changes), 3)

            # 5) No-op drop (dropping wait_cmd right above itself) does not emit or push Undo.
            wait_rect = editor.list.visualItemRect(editor.list.item(0))
            above_wait = QPointF(wait_rect.center().x(), wait_rect.top() + 2)
            undo_depth_before = len(editor._undo_stack)
            _drop(editor, [wait_cmd.id], above_wait)
            self.assertEqual(len(changes), 3)
            self.assertEqual(len(editor._undo_stack), undo_depth_before)

            # 6) Undo & Redo restore exact pre-drag and post-drag trees.
            editor.undo()
            self.assertEqual(
                [c.id for c in doc.commands],
                [outer_scan.id, wait_cmd.id, tail_remark.id],
            )
            editor.redo()
            self.assertEqual(
                [c.id for c in doc.commands],
                [wait_cmd.id, tail_remark.id, outer_scan.id],
            )

            # 7) Edit lock rejects drops cleanly.
            editor.set_editable(False)
            self.assertFalse(editor.list.dragEnabled())
            self.assertFalse(editor.list.acceptDrops())
            moved_ok, dropped_ok = _drop(editor, [tail_remark.id], above_wait)
            self.assertFalse(moved_ok)
            self.assertFalse(dropped_ok)
            self.assertEqual(
                [c.id for c in doc.commands],
                [wait_cmd.id, tail_remark.id, outer_scan.id],
            )

            # 8) Drag badge preview pixmap and edge auto-scroll on long sequences.
            from labcontrol.ui.sequence_editor import build_drag_badge_pixmap

            badge = build_drag_badge_pixmap("Wait 10 s", editor.list.font())
            self.assertFalse(badge.isNull())
            self.assertGreater(badge.width(), 0)

            long_doc = SequenceDocument(
                [Command(CommandType.WAIT, {"seconds": float(i)}) for i in range(40)],
                "long.seq",
            )
            editor.set_document(long_doc)
            editor.set_editable(True)
            editor.resize(420, 180)
            self.application.processEvents()
            bar = editor.list.verticalScrollBar()
            self.assertGreater(bar.maximum(), 0)
            bar.setValue(0)
            bottom_edge = QPointF(
                editor.list.viewport().width() / 2,
                editor.list.viewport().height() - 4,
            )
            drag_mime = QMimeData()
            drag_mime.setData(
                SEQUENCE_COMMAND_MIME,
                QByteArray(json.dumps([long_doc.commands[0].id]).encode("utf-8")),
            )
            editor.list.dragMoveEvent(
                QDragMoveEvent(
                    bottom_edge.toPoint(),
                    Qt.DropAction.MoveAction,
                    drag_mime,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            self.assertEqual(editor.list._autoscroll_direction, 1)
            self.assertTrue(editor.list._autoscroll_timer.isActive())
            editor.list._on_autoscroll_tick()
            self.assertGreater(bar.value(), 0)
            editor.list.dragLeaveEvent(QDragLeaveEvent())
            self.assertFalse(editor.list._autoscroll_timer.isActive())
            self.assertIsNone(editor.list._drop_indicator)
        finally:
            editor.close()


if __name__ == "__main__":
    unittest.main()
