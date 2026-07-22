from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


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

    def test_context_menu_and_keyboard_edit_complete_nodes(self) -> None:
        scan = Command(
            CommandType.SCAN_TIME,
            {"duration_seconds": 1.0, "steps": 2},
            [Command(CommandType.MEASURE, {"devices": "all", "repeats": 1, "interval_seconds": 0.0})],
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
        self.assertEqual(labels, ["Disable", "Enable", "Delete", "Copy", "Paste"])
        self.assertTrue(editor.disable_action.isEnabled())
        self.assertFalse(editor.enable_action.isEnabled())
        self.assertFalse(editor.paste_action.isEnabled())

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
            [Command(CommandType.MEASURE, {"devices": "all", "repeats": 1, "interval_seconds": 0.0})],
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
        document = SequenceDocument([Command(CommandType.WAIT, {"seconds": 1.0})])
        editor = SequenceEditorWidget(document)
        self._select_rows(editor, 0)
        editor.set_editable(False)
        self.assertFalse(editor.disable_action.isEnabled())
        self.assertFalse(editor.enable_action.isEnabled())
        self.assertFalse(editor.delete_action.isEnabled())
        self.assertFalse(editor.paste_action.isEnabled())
        self.assertTrue(editor.copy_action.isEnabled())
        editor.close()


if __name__ == "__main__":
    unittest.main()
