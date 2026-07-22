from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.config import load_config  # noqa: E402
from labcontrol.app import configure_qt_font  # noqa: E402
from labcontrol.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    output = ROOT / "docs" / "main-window-preview.png"
    application = QApplication([])
    configure_qt_font(application)
    window = MainWindow(load_config(ROOT / "configs" / "default.toml"))
    window.resize(1480, 900)
    window.show()

    def capture() -> None:
        window.grab().save(str(output), "PNG")
        window.close()

    QTimer.singleShot(2200, capture)
    code = application.exec()
    print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
