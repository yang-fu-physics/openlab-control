from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import time
from pathlib import Path

from .config import ConfigurationError, load_config
from .devices.manifest import (
    DevicePluginDescriptor,
    configured_device_plugins,
    device_dependency_directory,
    discover_device_plugins,
)
from .extensions.dependencies import (
    DependencyInstallError,
    dependency_runtime_errors,
    install_offline_dependencies,
    missing_dependencies,
)
from .extensions.trust import ExtensionTrustError, PluginTrustStore
from .models import RunProgress, RunState
from .measurement.settings import load_settings
from .paths import default_config_path
from .runtime import RuntimeService
from .sequence.parser import load_sequence


def configure_qt_font(application, point_size: float = 10.0) -> None:
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
            font = QFont(families[-1])
            font.setPointSizeF(point_size)
            application.setFont(font)
            return
    font = application.font()
    font.setPointSizeF(point_size)
    application.setFont(font)


def _plugin_python_executable(config) -> Path | None:
    configured = config.plugins.python_executable.strip()
    if configured:
        candidate = config.resolve_project_path(configured)
        return candidate if candidate.is_file() else None
    if not getattr(sys, "frozen", False):
        return Path(sys.executable)
    candidate = (
        config.project_root
        / "runtime"
        / "python"
        / "python.exe"
    )
    return candidate if candidate.is_file() else None


def configure_qt_appearance(application, requested_scale: float | None = None) -> float:
    """Apply the same desktop appearance in production and visual QA tools."""
    from PySide6.QtGui import QColor, QPalette
    from .ui.scaling import screen_ui_scale

    scale = requested_scale if requested_scale is not None else screen_ui_scale(application.primaryScreen())
    application.setProperty("openlabUiScale", scale)
    application.setProperty("openlabUiScaleMode", "manual" if requested_scale is not None else "auto")

    application.setStyle("Fusion")
    palette = application.palette()
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#5c6b79"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f9fa"))
    application.setPalette(palette)
    configure_qt_font(application, 10.0 * scale)
    return scale


def _arguments(argv: list[str] | None) -> argparse.Namespace:
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
    device_descriptors: tuple[DevicePluginDescriptor, ...] = (),
) -> int:
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
    runtime = RuntimeService(config, device_descriptors=device_descriptors)
    runtime.start()
    try:
        for module_id in module_ids or []:
            settings_path = (
                config.resolve_project_path(config.modules.data_directory)
                / module_id
                / "settings.toml"
            )
            runtime.enable_module(module_id, load_settings(settings_path)).result()
            emit(f"INFO    module:{module_id}/ENABLED: Module enabled for headless demo")
    except Exception as exc:
        emit(f"ERROR   module enable: {type(exc).__name__}: {exc}")
        runtime.shutdown()
        diagnostic.close()
        return 2
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
    multiprocessing.freeze_support()
    args = _arguments(argv)
    try:
        config = load_config(args.config)
    except (OSError, ConfigurationError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        device_descriptors = discover_device_plugins(config)
        selected_device_plugins = configured_device_plugins(
            config,
            device_descriptors,
        )
        trust_store = PluginTrustStore(
            config.resolve_project_path(config.plugins.state_directory)
            / "trusted_plugins.json"
        )
    except (OSError, ValueError, ExtensionTrustError) as exc:
        print(f"Device plugin error: {exc}", file=sys.stderr)
        return 2
    sequence_path = args.sequence or (
        config.resolve_project_path(config.default_sequence)
        if config.default_sequence
        else config.project_root / "examples" / "nested_scan.seq"
    )
    if args.headless_demo:
        untrusted = [
            descriptor.id
            for descriptor in selected_device_plugins
            if not trust_store.is_trusted("device", descriptor)
        ]
        if untrusted:
            print(
                "Device plugin error: untrusted plugins cannot run headlessly: "
                + ", ".join(untrusted),
                file=sys.stderr,
            )
            return 2
        invalid_runtime_by_plugin = {
            descriptor.id: dependency_runtime_errors(
                descriptor.dependencies,
                device_dependency_directory(
                    config,
                    descriptor,
                ),
                descriptor.fingerprint,
            )
            for descriptor in selected_device_plugins
        }
        invalid_runtime_by_plugin = {
            plugin_id: errors
            for plugin_id, errors
            in invalid_runtime_by_plugin.items()
            if errors
        }
        if invalid_runtime_by_plugin:
            print(
                "Device plugin error: isolated dependencies are "
                "missing; prepare them in the GUI first: "
                + "; ".join(
                    f"{plugin_id}: {'; '.join(errors)}"
                    for plugin_id, errors
                    in invalid_runtime_by_plugin.items()
                ),
                file=sys.stderr,
            )
            return 2
        return _headless_demo(
            config,
            sequence_path,
            args.timeout,
            list(args.enable_module),
            device_descriptors,
        )

    if args.gui_smoke:
        # This mode is used by release verification and never opens a visible window.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        from .ui.main_window import MainWindow
        from .ui.plugin_trust import confirm_device_plugin_trust
    except ImportError as exc:
        print(f"Missing GUI dependency: {exc}. Run setup.bat first.", file=sys.stderr)
        return 2

    application = QApplication(sys.argv[:1])
    application.setApplicationName("OpenLab Control")
    application.setOrganizationName("OpenLab")
    configure_qt_appearance(application, config.ui_scale)
    for descriptor in selected_device_plugins:
        try:
            trusted = confirm_device_plugin_trust(None, trust_store, descriptor)
        except ExtensionTrustError as exc:
            QMessageBox.critical(None, "Plugin Trust Failed", str(exc))
            return 1
        if not trusted:
            QMessageBox.warning(
                None,
                "Device Plugin Not Trusted",
                f"OpenLab Control will not load {descriptor.name}.",
            )
            return 1
        runtime_errors = dependency_runtime_errors(
            descriptor.dependencies,
            device_dependency_directory(config, descriptor),
            descriptor.fingerprint,
        )
        if not runtime_errors:
            continue
        missing = missing_dependencies(
            descriptor.dependencies,
            device_dependency_directory(config, descriptor),
        )
        python = _plugin_python_executable(config)
        if python is None:
            QMessageBox.critical(
                None,
                "Device Plugin Dependencies Missing",
                f"{descriptor.name} requires:\n\n"
                + "\n".join(
                    missing or descriptor.dependencies
                )
                + "\n\nConfigure plugins.python_executable or "
                "add runtime/python/python.exe.",
            )
            return 1
        answer = QMessageBox.question(
            None,
            "Prepare Device Plugin Dependencies?",
            f"{descriptor.name} requires:\n\n"
            + "\n".join(
                missing or descriptor.dependencies
            )
            + "\n\nCurrent runtime issue:\n"
            + "\n".join(runtime_errors)
            + "\n\nInstall from local wheels into this plugin's "
            "isolated runtime? No network access will be used.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return 1
        try:
            install_offline_dependencies(
                python_executable=python,
                extension_directory=descriptor.path,
                site_packages=device_dependency_directory(
                    config,
                    descriptor,
                ),
                shared_wheels_directory=(
                    config.resolve_project_path(
                        config.plugins.shared_wheels_directory
                    )
                ),
                dependencies=descriptor.dependencies,
                fingerprint=descriptor.fingerprint,
            )
        except DependencyInstallError as exc:
            QMessageBox.critical(
                None,
                "Offline Dependency Install Failed",
                str(exc),
            )
            return 1
    try:
        window = MainWindow(config, device_descriptors)
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
