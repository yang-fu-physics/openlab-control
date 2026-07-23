from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.config import load_config  # noqa: E402
from labcontrol.measurement.manifest import activate_shared_dependencies  # noqa: E402
from labcontrol.ui.main_window import MainWindow  # noqa: E402


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture module UI previews.")
    parser.add_argument("--scale", type=float, help="Override the configured UI scale.")
    parser.add_argument("--output-directory", type=Path)
    return parser.parse_args()


def main() -> int:
    options = arguments()
    output_directory = (options.output_directory or ROOT / "docs").resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    suffix = "" if options.scale is None else f"-{options.scale:.2f}x"
    manager_output = output_directory / f"module-manager-preview{suffix}.png"
    module_output = output_directory / f"module-window-preview{suffix}.png"
    status_output = output_directory / f"module-status-preview{suffix}.png"
    application = QApplication([])
    config = load_config(ROOT / "configs" / "default.toml")
    activate_shared_dependencies(config)
    configure_qt_appearance(
        application,
        config.ui_scale if options.scale is None else options.scale,
    )
    window = MainWindow(config)
    window.resize(1480, 900)
    window.show()
    window._show_module_manager()
    window._set_module_enabled("simulated_transport", True)
    attempts = 0

    def capture_when_ready() -> None:
        nonlocal attempts
        attempts += 1
        module_window = window.module_windows.get("simulated_transport")
        if module_window is None and attempts < 100:
            QTimer.singleShot(100, capture_when_ready)
            return
        if module_window is not None:
            module_window.show_in_front()
            application.processEvents()
            window.module_manager.grab().save(str(manager_output), "PNG")
            module_window.grab().save(str(module_output), "PNG")
            module_window.tabs.setCurrentIndex(1)
            application.processEvents()
            module_window.grab().save(str(status_output), "PNG")
        window.close()
        application.quit()

    QTimer.singleShot(100, capture_when_ready)
    application.exec()
    if not manager_output.exists() or not module_output.exists() or not status_output.exists():
        return 1
    print(manager_output)
    print(module_output)
    print(status_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
