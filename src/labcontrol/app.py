from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .config import ConfigurationError, load_config
from .models import RunProgress, RunState
from .paths import default_config_path
from .runtime import RuntimeService
from .sequence.parser import load_sequence


def configure_qt_font(application) -> None:
    """Load a Windows CJK font explicitly; offscreen and packaged Qt need it."""
    from PySide6.QtGui import QFont, QFontDatabase

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for file_name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
        candidate = fonts_dir / file_name
        if not candidate.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(candidate))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            application.setFont(QFont(families[-1], 9))
            return


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenLab Control")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--sequence", type=Path)
    parser.add_argument("--data-file", type=Path, help="Open an independent DAT file in Data Browser.")
    parser.add_argument("--headless-demo", action="store_true")
    parser.add_argument(
        "--gui-smoke",
        action="store_true",
        help="Start the packaged GUI offscreen, capture it, then exit.",
    )
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def _headless_demo(config, sequence_path: Path, timeout: float) -> int:
    diagnostic_path = config.project_root / "headless_demo.log"
    diagnostic = diagnostic_path.open("w", encoding="utf-8")

    def emit(message: str) -> None:
        print(message)
        diagnostic.write(message + "\n")
        diagnostic.flush()

    result = load_sequence(sequence_path)
    if result.has_errors:
        for issue in result.issues:
            emit(f"{issue.level}: line {issue.line_number}: {issue.message}")
        diagnostic.close()
        return 2
    runtime = RuntimeService(config)
    runtime.start()
    run_future = runtime.run_sequence(result.document)
    deadline = time.monotonic() + timeout
    terminal: RunState | None = None
    try:
        while time.monotonic() < deadline and terminal is None:
            for message in runtime.drain_messages():
                if message.kind == "event":
                    notice = message.payload
                    if not notice.is_resolution:
                        emit(
                            f"{notice.event.severity.value.upper():7} "
                            f"{notice.event.source}/{notice.event.code}: {notice.event.message}"
                        )
                elif message.kind == "progress":
                    progress: RunProgress = message.payload
                    emit(f"{progress.state.value:9} {progress.message}")
                    if progress.state in {
                        RunState.STOPPED,
                        RunState.COMPLETED,
                        RunState.FAULTED,
                    }:
                        terminal = progress.state
                elif message.kind == "startup_error":
                    emit(f"ERROR   startup: {message.payload}")
                    terminal = RunState.FAULTED
            if run_future.done() and terminal is None:
                exception = run_future.exception()
                if exception is not None:
                    emit(f"ERROR   runtime future: {type(exception).__name__}: {exception}")
                    terminal = RunState.FAULTED
            time.sleep(0.05)
        if terminal is None:
            runtime.stop_sequence()
            emit("ERROR   demo timeout")
            return 3
        return 0 if terminal is RunState.COMPLETED else 1
    finally:
        runtime.shutdown()
        diagnostic.close()


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        config = load_config(args.config)
    except (OSError, ConfigurationError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    sequence_path = args.sequence or (
        config.resolve_project_path(config.default_sequence)
        if config.default_sequence
        else config.project_root / "examples" / "nested_scan.seq"
    )
    if args.headless_demo:
        return _headless_demo(config, sequence_path, args.timeout)

    if args.gui_smoke:
        # This mode is used by release verification and never opens a visible window.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        from .ui.main_window import MainWindow
    except ImportError:
        print("PySide6 is not installed. Run setup.bat first.", file=sys.stderr)
        return 2

    application = QApplication(sys.argv[:1])
    application.setApplicationName("OpenLab Control")
    application.setOrganizationName("OpenLab")
    application.setStyle("Fusion")
    configure_qt_font(application)
    try:
        window = MainWindow(config)
    except Exception as exc:
        QMessageBox.critical(None, "OpenLab Control - Startup Failed", str(exc))
        return 1
    if args.data_file is not None:
        window._show_data_browser(args.data_file)
    window.show()
    if not args.gui_smoke:
        return application.exec()

    screenshot_path = (args.screenshot or config.project_root / "gui_smoke.png").resolve()
    screenshot_succeeded = False

    def capture_and_exit() -> None:
        nonlocal screenshot_succeeded
        try:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_succeeded = window.grab().save(str(screenshot_path), "PNG")
        finally:
            window.close()
            application.quit()

    QTimer.singleShot(2200, capture_and_exit)
    application.exec()
    return 0 if screenshot_succeeded else 4
