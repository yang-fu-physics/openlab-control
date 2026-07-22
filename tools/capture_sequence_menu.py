from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QItemSelectionModel, QPoint, QTimer  # noqa: E402
from PySide6.QtGui import QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.config import load_config  # noqa: E402
from labcontrol.sequence.parser import load_sequence  # noqa: E402
from labcontrol.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    output = ROOT / "docs" / "sequence-context-menu-preview.png"
    application = QApplication([])
    config = load_config(ROOT / "configs" / "default.toml")
    configure_qt_appearance(application, config.ui_scale)
    window = MainWindow(config)
    document = load_sequence(ROOT / "examples" / "disabled_commands.seq").document
    window._set_document(document)
    window.editor.list.clearSelection()
    window.editor.list.item(0).setSelected(True)
    window.editor.list.item(1).setSelected(True)
    window.editor.list.setCurrentItem(
        window.editor.list.item(1),
        QItemSelectionModel.SelectionFlag.NoUpdate,
    )
    window.resize(1480, 900)
    window.show()
    menu = window.editor.build_context_menu()

    def show_menu() -> None:
        item_rect = window.editor.list.visualItemRect(window.editor.list.item(1))
        global_position = window.editor.list.viewport().mapToGlobal(
            QPoint(item_rect.left() + 260, item_rect.bottom())
        )
        menu.popup(global_position)

    def capture() -> None:
        base = window.grab()
        overlay = menu.grab()
        menu_origin = menu.mapToGlobal(QPoint(0, 0))
        window_origin = window.mapToGlobal(QPoint(0, 0))
        painter = QPainter(base)
        painter.drawPixmap(menu_origin - window_origin, overlay)
        painter.end()
        output.parent.mkdir(parents=True, exist_ok=True)
        base.save(str(output), "PNG")
        menu.close()
        window.close()
        application.quit()

    QTimer.singleShot(1800, show_menu)
    QTimer.singleShot(2200, capture)
    code = application.exec()
    print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
