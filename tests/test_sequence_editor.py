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


if __name__ == "__main__":
    unittest.main()
