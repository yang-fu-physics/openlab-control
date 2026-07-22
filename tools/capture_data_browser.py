from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QPoint, QTimer  # noqa: E402
from PySide6.QtGui import QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.config import load_config  # noqa: E402
from labcontrol.plot_format import LOG_SCALE  # noqa: E402
from labcontrol.ui.dat_plot import YSeriesSelectionDialog  # noqa: E402
from labcontrol.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    output = ROOT / "docs" / "data-browser-preview.png"
    application = QApplication([])
    configure_qt_appearance(application)
    window = MainWindow(load_config(ROOT / "configs" / "default.toml"))
    window.resize(1480, 900)
    window.show()
    browser = window.data_browser
    browser._suspend_format_save = True
    browser.load_path(ROOT / "examples" / "template_original.dat", show_errors=False)
    browser._suspend_format_save = True
    browser.canvas.set_axes(
        "Time(s)",
        ("Temp(K)", "R1(Ohm)", "R2(Ohm)"),
    )
    browser.canvas.set_layout("stacked")
    browser.canvas.set_x_scale(LOG_SCALE)
    browser.canvas.set_y_scale(LOG_SCALE)
    browser._suspend_format_save = False
    window._show_data_browser()
    selector = YSeriesSelectionDialog(
        browser.document.numeric_columns(),
        browser.canvas.y_columns,
        window,
    )

    def show_selector() -> None:
        selector.show()
        selector.move(window.mapToGlobal(QPoint(725, 170)))
        selector.raise_()

    def capture() -> None:
        base = window.grab()
        overlay = selector.grab()
        selector_origin = selector.mapToGlobal(QPoint(0, 0))
        window_origin = window.mapToGlobal(QPoint(0, 0))
        painter = QPainter(base)
        painter.drawPixmap(selector_origin - window_origin, overlay)
        painter.end()
        output.parent.mkdir(parents=True, exist_ok=True)
        base.save(str(output), "PNG")
        selector.close()
        window.close()
        application.quit()

    QTimer.singleShot(1700, show_selector)
    QTimer.singleShot(2300, capture)
    code = application.exec()
    print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
