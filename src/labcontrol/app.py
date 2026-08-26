"""应用程序入口以及源码版、打包版共用的启动流程。

本文件负责命令行参数、Qt 外观和清单预检，然后才创建
:class:`~labcontrol.runtime.RuntimeService`。无界面演示和 GUI 共用同一套配置、仪表清单与
SEQ 解析器，因此发布验证不会绕过正式运行路径。这里不实现仪表控制逻辑；所有 I/O 都交给
后台运行时，防止 GUI 线程直接接触仪表。
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import time
from pathlib import Path

from .config import ConfigurationError, load_config
from .instruments.manifest import (
    SystemInstrumentDescriptor,
    configured_system_instruments,
    discover_system_instruments,
)
from .models import RunProgress, RunState
from .measurement.settings import load_settings
from .paths import default_config_path
from .runtime import RuntimeService
from .system_instrument_commands import (
    configured_system_instrument_commands,
)
from .sequence.module_settings import (
    load_sequence_module_settings,
)
from .sequence.parser import load_sequence


def configure_qt_font(application, point_size: float = 10.0) -> None:
    """显式加载 Windows 中文字体，保证打包版和无屏幕截图中的中文不会变成方框。"""
    from PySide6.QtGui import QFont, QFontDatabase

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for file_name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
        candidate = fonts_dir / file_name
        if not candidate.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(candidate))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            font = QFont(families[-1])
            font.setPointSizeF(point_size)
            application.setFont(font)
            return
    font = application.font()
    font.setPointSizeF(point_size)
    application.setFont(font)


def configure_qt_appearance(
    application,
    requested_scale: float | None = None,
    font_scale: float = 1.0,
) -> float:
    """为正式 GUI 和视觉回归工具应用同一套主题、字体与缩放系数。"""
    from PySide6.QtGui import QColor, QPalette
    from .ui.scaling import screen_ui_scale

    scale = requested_scale if requested_scale is not None else screen_ui_scale(application.primaryScreen())
    application.setProperty("openlabUiScale", scale)
    application.setProperty("openlabUiScaleMode", "manual" if requested_scale is not None else "auto")
    application.setProperty("openlabFontScale", float(font_scale))

    application.setStyle("Fusion")
    palette = application.palette()
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#5c6b79"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f9fa"))
    application.setPalette(palette)
    configure_qt_font(
        application,
        10.0 * scale * float(font_scale),
    )
    return scale


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    """解析所有启动模式；返回值由 :func:`main` 统一执行和映射退出码。"""

    parser = argparse.ArgumentParser(description="OpenLab Control")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--sequence", type=Path)
    parser.add_argument("--data-file", type=Path, help="Open an independent DAT file in Data Browser.")
    parser.add_argument("--headless-demo", action="store_true")
    parser.add_argument(
        "--enable-module",
        action="append",
        default=[],
        metavar="ID",
        help="Enable a measurement module before a headless demo; repeat for multiple modules.",
    )
    parser.add_argument(
        "--gui-smoke",
        action="store_true",
        help="Start the packaged GUI offscreen, capture it, then exit.",
    )
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def _headless_demo(
    config,
    sequence_path: Path,
    timeout: float,
    module_ids: list[str] | None = None,
    instrument_descriptors: tuple[SystemInstrumentDescriptor, ...] = (),
) -> int:
    """在没有 Qt 窗口的情况下运行一条真实 SEQ，供自动化和发布冒烟验证使用。

    ``--enable-module`` 仍需通过模块内容信任检查，SEQ 配套设置也只会应用到明确请求启用的模块；
    文件中出现其他模块不会造成隐式初始化。无论成功、失败或超时，``finally`` 都会关闭完整
    运行时。
    """

    diagnostic_path = config.project_root / "headless_demo.log"
    diagnostic = diagnostic_path.open("w", encoding="utf-8")

    def emit(message: str) -> None:
        print(message)
        diagnostic.write(message + "\n")
        diagnostic.flush()

    runtime = RuntimeService(
        config,
        instrument_descriptors=instrument_descriptors,
    )
    result = load_sequence(
        sequence_path,
        instrument_commands=runtime.instrument_sequence_commands,
    )
    if result.has_errors:
        for issue in result.issues:
            emit(f"{issue.level}: line {issue.line_number}: {issue.message}")
        diagnostic.close()
        return 2
    imported = load_sequence_module_settings(
        sequence_path
    )
    if imported.issues:
        for issue in imported.issues:
            emit(
                "ERROR   module settings import: "
                + issue
            )
        diagnostic.close()
        return 2
    descriptors = {
        descriptor.id: descriptor
        for descriptor in runtime.module_descriptors
    }
    requested_modules = set(module_ids or [])
    for module_id in sorted(imported.settings):
        descriptor = descriptors.get(module_id)
        if descriptor is None:
            emit(
                "WARNING module settings: unavailable "
                f"module {module_id!r} was not enabled"
            )
            continue
        recorded_version = imported.versions.get(
            module_id,
            "",
        )
        if (
            module_id in requested_modules
            and recorded_version
            and recorded_version != descriptor.version
        ):
            emit(
                "ERROR   module settings: "
                f"{module_id} settings require module "
                f"{recorded_version}, installed version is "
                f"{descriptor.version}"
            )
            diagnostic.close()
            return 2
    runtime.start()
    enabled_settings: dict[
        str,
        dict[str, object],
    ] = {}
    try:
        for module_id in module_ids or []:
            settings_path = (
                config.resolve_project_path(config.modules.data_directory)
                / module_id
                / "settings.toml"
            )
            settings = (
                dict(
                    imported.settings[module_id]
                )
                if module_id
                in imported.settings
                else load_settings(settings_path)
            )
            enabled_settings[module_id] = settings
            runtime.enable_module(module_id).result()
            runtime.apply_module_settings(
                module_id,
                settings,
            ).result()
            emit(f"INFO    module:{module_id}/ENABLED: Module enabled for headless demo")
    except Exception as exc:
        emit(f"ERROR   module enable: {type(exc).__name__}: {exc}")
        runtime.shutdown()
        diagnostic.close()
        return 2
    run_future = runtime.run_sequence(
        result.document,
        enabled_settings,
    )
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
    """启动 OpenLab Control，并把可预期的启动失败转换为稳定的进程退出码。"""

    # Windows 冻结程序创建模块/仪表子进程前必须调用 freeze_support，否则子进程可能再次
    # 执行 GUI 入口并形成递归启动。
    multiprocessing.freeze_support()
    args = _arguments(argv)
    try:
        config = load_config(args.config)
    except (OSError, ConfigurationError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        instrument_descriptors = discover_system_instruments(config)
        configured_system_instruments(
            config,
            instrument_descriptors,
        )
        configured_system_instrument_commands(
            config,
            instrument_descriptors,
        )
    except (OSError, ValueError) as exc:
        print(f"System Instrument error: {exc}", file=sys.stderr)
        return 2
    sequence_path = args.sequence or (
        config.resolve_project_path(config.default_sequence)
        if config.default_sequence
        else config.project_root / "examples" / "nested_scan.seq"
    )
    if args.headless_demo:
        return _headless_demo(
            config,
            sequence_path,
            args.timeout,
            list(args.enable_module),
            instrument_descriptors,
        )

    if args.gui_smoke:
        # 发布验证只在离屏平台创建窗口、截图后退出，不显示或接管用户桌面。
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        from .ui.main_window import MainWindow
        from .ui.preferences import (
            UiPreferences,
            UiPreferenceStore,
            default_ui_preferences_path,
        )
    except ImportError as exc:
        print(f"Missing GUI dependency: {exc}. Run setup.bat first.", file=sys.stderr)
        return 2

    application = QApplication(sys.argv[:1])
    application.setApplicationName("OpenLab Control")
    application.setOrganizationName("OpenLab")
    preference_store = UiPreferenceStore(
        default_ui_preferences_path()
    )
    ui_preferences = (
        UiPreferences(config.ui_scale, 1.0, "default")
        if args.gui_smoke
        else preference_store.load(config.ui_scale)
    )
    configure_qt_appearance(
        application,
        ui_preferences.ui_scale,
        ui_preferences.font_scale,
    )
    try:
        window = MainWindow(
            config,
            instrument_descriptors,
            ui_preferences=ui_preferences,
            ui_preference_store=preference_store,
        )
    except Exception as exc:
        QMessageBox.critical(None, "OpenLab Control - Startup Failed", str(exc))
        return 1
    if args.data_file is not None:
        window._show_data_browser(args.data_file)
    if window.should_start_maximized():
        window.showMaximized()
    else:
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
