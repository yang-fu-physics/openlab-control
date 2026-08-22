"""只读扫描 VISA 仪表，并在人工确认后写入现场资源配置。

扫描阶段只列出 VISA 资源并发送一次 *IDN?。程序不会执行 reset、clear、输出开关、
设定点或 Measurement Module 的 Apply Settings。识别失败的地址仍会列出，用户可以根据
前面板和线缆手动确认。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import html
from pathlib import Path
import re
import sys
from typing import Any


def application_root() -> Path:
    """返回源码项目根目录或打包发布目录。

    发布包把扫描器与主程序放在同一目录并共享 ``_internal``。打包后不能使用
    ``__file__`` 定位现场配置，因为它指向内部运行目录；应以真实 EXE 所在目录为准。
    """

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = application_root()
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QThread, QTimer, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from labcontrol.instrument_resources import (  # noqa: E402
    InstrumentResource,
    InstrumentResourceError,
    load_instrument_resources,
    render_instrument_resources,
    write_instrument_resources,
)
from labcontrol.instruments.manifest import (  # noqa: E402
    SystemInstrumentDescriptor,
    load_instrument_manifest,
)


NI_VISA_DOWNLOAD_URL = (
    "https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html"
)


SCANNER_STYLE = """
QMainWindow, QWidget {
    background: #f8fafc;
    color: #1e293b;
    font-family: "Segoe UI";
    font-size: 13px;
}
QFrame#scannerHeader, QFrame#scannerFooter {
    background: #ffffff;
    border: 0;
}
QFrame#scannerHeader { border-bottom: 1px solid #e2e8f0; }
QFrame#scannerFooter { border-top: 1px solid #e2e8f0; }
QFrame#instrumentCard {
    background: #ffffff;
    border: 1px solid #dbe3ed;
    border-radius: 8px;
}
QFrame#instrumentCard[invalid="true"] {
    border: 2px solid #dc2626;
}
QFrame#deviceDetails {
    background: #f1f5f9;
    border: 0;
    border-radius: 4px;
}
QLabel { background: transparent; }
QLabel#mutedText { color: #64748b; }
QLabel#successBadge {
    color: #15803d;
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    border-radius: 9px;
    padding: 2px 8px;
}
QLabel#errorBadge {
    color: #b91c1c;
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 9px;
    padding: 2px 8px;
}
QLineEdit, QComboBox {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    padding: 5px 8px;
    min-height: 20px;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #2563eb; }
QPushButton {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    padding: 6px 13px;
}
QPushButton:hover { background: #f1f5f9; }
QPushButton#primaryButton {
    background: #2563eb;
    border-color: #1d4ed8;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryButton:disabled {
    background: #94a3b8;
    border-color: #94a3b8;
}
QScrollArea { border: 0; background: #f8fafc; }
QCheckBox { background: transparent; spacing: 6px; }
"""


@dataclass(frozen=True, slots=True)
class VisaScanResult:
    """一个 VISA 地址的一次只读识别结果。"""

    address: str
    identity: str = ""
    error: str = ""


def scan_visa_resources(
    timeout_seconds: float = 1.0,
) -> tuple[VisaScanResult, ...]:
    """列出 VISA 资源并逐一发送一次 *IDN?，不调用 clear 或状态写指令。"""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        import pyvisa
    except ImportError as exc:
        raise RuntimeError(
            "PyVISA is not available in the OpenLab Control runtime"
        ) from exc

    try:
        manager = pyvisa.ResourceManager()
    except Exception as exc:
        raise RuntimeError(
            "Cannot initialize VISA. OpenLab Control includes PyVISA, but "
            "Windows still needs a VISA implementation such as NI-VISA "
            "before real instruments can be scanned."
        ) from exc
    timeout_ms = max(1, int(timeout_seconds * 1000.0))
    results: list[VisaScanResult] = []
    try:
        for raw_address in manager.list_resources():
            address = str(raw_address)
            handle: Any | None = None
            try:
                handle = manager.open_resource(
                    address,
                    open_timeout=timeout_ms,
                )
                handle.timeout = timeout_ms
                identity = str(handle.query("*IDN?")).strip()
                if not identity:
                    raise RuntimeError("empty *IDN? response")
                if len(identity) > 1024 or any(
                    not character.isprintable()
                    for character in identity
                ):
                    raise RuntimeError(
                        "invalid *IDN? response: expected at most 1024 "
                        "printable characters"
                    )
                results.append(VisaScanResult(address, identity))
            except Exception as exc:
                results.append(
                    VisaScanResult(
                        address,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
    finally:
        try:
            manager.close()
        except Exception:
            pass
    return tuple(results)


def discover_scan_descriptors(
    directory: str | Path,
) -> tuple[SystemInstrumentDescriptor, ...]:
    """只读清单和 discovery 元数据，不导入任何第三方后端代码。"""

    root = Path(directory)
    if not root.is_dir():
        return ()
    descriptors = (
        load_instrument_manifest(path)
        for path in sorted(
            root.iterdir(),
            key=lambda item: item.name.casefold(),
        )
        if path.is_dir() and (path / "instrument.toml").is_file()
    )
    return tuple(item for item in descriptors if item.valid)


def match_descriptor(
    identity: str,
    descriptors: tuple[SystemInstrumentDescriptor, ...],
) -> SystemInstrumentDescriptor | None:
    """只接受唯一的识别规则匹配；无匹配或多重匹配时留给用户选择。"""

    matches = [
        item
        for item in descriptors
        if item.identity_pattern
        and re.search(item.identity_pattern, identity)
    ]
    return matches[0] if len(matches) == 1 else None


def suggest_resource_id(identity: str, address: str) -> str:
    """由型号和序列号生成可编辑的稳定 ID。"""

    parts = [
        part.strip()
        for part in identity.split(",")
        if part.strip()
    ]
    source = "_".join(parts[:3]) if len(parts) >= 2 else address
    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        source.casefold(),
    ).strip("_")
    if not value or not value[0].isalpha():
        value = f"instrument_{value}" if value else "instrument"
    return value[:64].rstrip("_")


class VisaScanThread(QThread):
    """把可能较慢的 VISA 枚举移出 Qt 主线程。"""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        timeout_seconds: float,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.timeout_seconds = timeout_seconds

    def run(self) -> None:
        try:
            self.completed.emit(
                scan_visa_resources(self.timeout_seconds)
            )
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class InstrumentScannerWindow(QMainWindow):
    """扫描、人工分配用途并预览写盘的独立窗口。"""

    def __init__(
        self,
        output_path: Path,
        instrument_directory: Path,
        *,
        timeout_seconds: float = 1.0,
    ) -> None:
        super().__init__()
        self.output_path = output_path.resolve()
        self.instrument_directory = instrument_directory.resolve()
        self.timeout_seconds = timeout_seconds
        self.descriptors = discover_scan_descriptors(
            self.instrument_directory
        )
        self.scan_thread: VisaScanThread | None = None
        self._scan_results: tuple[VisaScanResult, ...] = ()
        self._rows: list[dict[str, Any]] = []
        self._existing_load_error = ""
        try:
            self.existing = load_instrument_resources(
                self.output_path
            )
        except InstrumentResourceError as exc:
            self.existing = ()
            self._existing_load_error = str(exc)
            QMessageBox.warning(
                self,
                "Existing Configuration Is Invalid",
                str(exc),
            )

        self.setWindowTitle(
            "OpenLab Control Instrument Scanner"
        )
        self.setStyleSheet(SCANNER_STYLE)
        self.setMinimumSize(960, 600)
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame(central)
        header.setObjectName("scannerHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 12, 20, 10)
        header_layout.setSpacing(7)
        file_controls = QHBoxLayout()
        file_controls.setSpacing(10)
        file_controls.addWidget(QLabel("Loaded configuration"))
        self.output_label = QLabel(self.output_path.name)
        self.output_label.setStyleSheet(
            "background:#f1f5f9; border-radius:4px; padding:5px 9px; "
            "font-family:Consolas; font-weight:600;"
        )
        self.output_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.output_label.setToolTip(str(self.output_path))
        file_controls.addWidget(self.output_label)
        self.output_button = QPushButton("Choose file…")
        file_controls.addWidget(self.output_button)
        file_controls.addStretch(1)
        header_layout.addLayout(file_controls)

        scan_controls = QHBoxLayout()
        scan_controls.setSpacing(10)
        scan_controls.addStretch(1)
        safety_label = QLabel(
            "Read-only scan · one *IDN? per VISA address"
        )
        safety_label.setStyleSheet(
            "color:#15803d; font-weight:600;"
        )
        scan_controls.addWidget(safety_label)
        self.scan_button = QPushButton("Scan VISA instruments")
        self.scan_button.setObjectName("primaryButton")
        scan_controls.addWidget(self.scan_button)
        header_layout.addLayout(scan_controls)
        self.existing_label = QLabel()
        self.existing_label.setWordWrap(True)
        self.existing_label.setObjectName("mutedText")
        self.existing_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        header_layout.addWidget(self.existing_label)
        self.discovery_label = QLabel()
        self.discovery_label.setWordWrap(True)
        self.discovery_label.setObjectName("mutedText")
        self.discovery_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        header_layout.addWidget(self.discovery_label)
        layout.addWidget(header)

        self.scroll_area = QScrollArea(central)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.cards_widget = QWidget(self.scroll_area)
        self.cards_layout = QVBoxLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(20, 16, 20, 16)
        self.cards_layout.setSpacing(12)
        self.scroll_area.setWidget(self.cards_widget)
        layout.addWidget(self.scroll_area, 1)

        footer = QFrame(central)
        footer.setObjectName("scannerFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 11, 20, 11)
        self.summary_label = QLabel("No VISA resources listed")
        self.summary_label.setMinimumWidth(0)
        self.summary_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        footer_layout.addWidget(self.summary_label)
        footer_layout.addStretch(1)
        self.save_button = QPushButton("Review changes and save…")
        self.save_button.setObjectName("primaryButton")
        footer_layout.addWidget(self.save_button)
        layout.addWidget(footer)

        self.scan_button.clicked.connect(self.start_scan)
        self.output_button.clicked.connect(self.choose_output)
        self.save_button.clicked.connect(
            self.preview_and_save
        )
        self.setCentralWidget(central)
        self.resize(1080, 750)
        self._update_existing_label()
        self._update_discovery_label()
        self._show_results(())
        QTimer.singleShot(0, self.start_scan)

    def _update_existing_label(self) -> None:
        """明确告诉操作者当前自动载入了哪一份原配置。"""

        if self._existing_load_error:
            self.existing_label.setText(
                f"Could not load {self.output_path}: "
                f"{self._existing_load_error}"
            )
        elif self.output_path.exists():
            self.existing_label.setText(
                f"Loaded {len(self.existing)} existing resource(s). "
                "Scan results are merged with this file; "
                "resources not seen in a scan stay listed until you "
                "explicitly choose Ignore."
            )
        else:
            self.existing_label.setText(
                "This configuration file does not exist yet. It will be "
                "created after every selected instrument is complete."
            )

    def _update_discovery_label(self) -> None:
        """显示 System Instrument 清单内容，不写入任何具体仪表名称。"""

        system_names = ", ".join(
            item.name for item in self.descriptors
        ) or "none found"
        self.discovery_label.setText(
            f"System Instruments ({len(self.descriptors)}): {system_names}"
        )
        self.discovery_label.setToolTip(
            f"System Instrument folder: {self.instrument_directory}"
        )

    def start_scan(self) -> None:
        if self.scan_thread is not None:
            return
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Scanning…")
        self.save_button.setEnabled(False)
        self.summary_label.setText("Scanning VISA resources…")
        worker = VisaScanThread(
            self.timeout_seconds,
            self,
        )
        self.scan_thread = worker
        worker.completed.connect(self._scan_completed)
        worker.failed.connect(self._scan_failed)
        worker.finished.connect(self._scan_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _scan_completed(self, results: object) -> None:
        values = (
            tuple(results)
            if isinstance(results, (tuple, list))
            else ()
        )
        self._scan_results = values
        self._show_results(values)

    def _scan_failed(self, message: str) -> None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle("VISA Scan Failed")
        dialog.setTextFormat(Qt.TextFormat.RichText)
        dialog.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        dialog.setText(
            html.escape(message).replace("\n", "<br>")
            + "<br><br>"
            + f'<a href="{NI_VISA_DOWNLOAD_URL}">'
            + "Download NI-VISA from NI</a>"
        )
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.exec()
        self.summary_label.setText("VISA scan failed")

    def _scan_finished(self) -> None:
        """只在线程真正退出后解除关闭保护，避免销毁仍运行的 QThread。"""

        self.scan_thread = None
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Scan VISA instruments")
        self.save_button.setEnabled(True)

    def _show_results(
        self,
        results: tuple[VisaScanResult, ...],
    ) -> None:
        self._scan_results = results
        existing_by_address = {
            item.address.casefold(): item
            for item in self.existing
        }
        scanned_addresses = {
            item.address.casefold()
            for item in results
        }
        merged = list(results)
        for item in self.existing:
            if item.address.casefold() not in scanned_addresses:
                merged.append(
                    VisaScanResult(
                        item.address,
                        item.identity,
                        "Not seen in this scan",
                    )
                )

        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()
        if not merged:
            placeholder = QLabel(
                "Select Scan VISA instruments to discover connected devices."
            )
            placeholder.setObjectName("mutedText")
            placeholder.setAlignment(
                Qt.AlignmentFlag.AlignHCenter
                | Qt.AlignmentFlag.AlignTop
            )
            placeholder.setContentsMargins(0, 50, 0, 0)
            self.cards_layout.addWidget(placeholder)
            self.cards_layout.addStretch(1)
            self._update_summary()
            return

        for result in merged:
            previous = existing_by_address.get(
                result.address.casefold()
            )
            descriptor = match_descriptor(
                result.identity,
                self.descriptors,
            )
            card = QFrame(self.cards_widget)
            card.setObjectName("instrumentCard")
            card.setProperty("invalid", "false")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 12)
            card_layout.setSpacing(9)

            identity_row = QHBoxLayout()
            identity_row.setSpacing(10)
            status = QLabel()
            if result.error == "Not seen in this scan":
                status.setText("Existing entry")
                status.setObjectName("mutedText")
            elif result.error:
                status.setText("No response")
                status.setObjectName("errorBadge")
            else:
                status.setText("Identified")
                status.setObjectName("successBadge")
            identity_row.addWidget(status)

            address = QLabel(result.address)
            address.setStyleSheet(
                "font-family:Consolas; font-weight:600;"
            )
            address.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            identity_row.addWidget(address)

            identity_parts = [
                part.strip()
                for part in result.identity.split(",")
                if part.strip()
            ]
            identity_summary = (
                " · ".join(identity_parts[:2])
                if identity_parts
                else "Identity unavailable"
            )
            identity_label = QLabel(identity_summary)
            identity_label.setObjectName("mutedText")
            identity_label.setMinimumWidth(0)
            identity_label.setToolTip(result.identity)
            identity_row.addWidget(identity_label, 1)

            details_button = QPushButton("Device details")
            details_button.setCheckable(True)
            identity_row.addWidget(details_button)
            card_layout.addLayout(identity_row)

            details_panel = QFrame(card)
            details_panel.setObjectName("deviceDetails")
            details_layout = QVBoxLayout(details_panel)
            details_layout.setContentsMargins(9, 6, 9, 6)
            details_text = QLabel(
                "\n".join(
                    text
                    for text in (
                        f"*IDN?: {result.identity}"
                        if result.identity
                        else "*IDN?: no response",
                        f"Status: {result.error}"
                        if result.error
                        else "",
                    )
                    if text
                )
            )
            details_text.setObjectName("mutedText")
            details_text.setWordWrap(True)
            details_text.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            details_layout.addWidget(details_text)
            details_panel.hide()
            details_button.toggled.connect(details_panel.setVisible)
            details_button.toggled.connect(
                lambda checked, button=details_button: button.setText(
                    "Hide details" if checked else "Device details"
                )
            )
            card_layout.addWidget(details_panel)

            divider = QFrame(card)
            divider.setFrameShape(QFrame.Shape.HLine)
            divider.setStyleSheet("color:#e2e8f0;")
            card_layout.addWidget(divider)

            purpose_row = QHBoxLayout()
            purpose_row.addWidget(QLabel("Use"))
            purpose = QComboBox(card)
            purpose.addItems(
                ["Ignore", "System", "Measurement"]
            )
            if previous is not None:
                purpose.setCurrentText(
                    previous.purpose.title()
                )
            elif descriptor is not None:
                purpose.setCurrentText("System")
            purpose_row.addWidget(purpose)
            purpose_row.addStretch(1)
            card_layout.addLayout(purpose_row)

            resource_id = QLineEdit(
                (
                    previous.id
                    if previous is not None
                    else suggest_resource_id(
                        result.identity,
                        result.address,
                    )
                ),
                card,
            )
            resource_id.setPlaceholderText("Required unique name")

            configuration = QWidget(card)
            configuration_layout = QGridLayout(configuration)
            configuration_layout.setContentsMargins(0, 0, 0, 0)
            configuration_layout.setHorizontalSpacing(10)
            configuration_layout.setVerticalSpacing(8)
            configuration_layout.addWidget(
                QLabel("Resource name (ID)"),
                0,
                0,
            )
            configuration_layout.addWidget(resource_id, 0, 1)
            configuration_layout.setColumnStretch(1, 1)

            system_options = QWidget(configuration)
            system_layout = QGridLayout(system_options)
            system_layout.setContentsMargins(0, 0, 0, 0)
            system_layout.setHorizontalSpacing(10)
            system_layout.setVerticalSpacing(8)

            system_instrument = QComboBox(system_options)
            system_instrument.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            system_instrument.setMinimumContentsLength(16)
            system_instrument.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed,
            )
            system_instrument.addItem(
                "Select a System Instrument",
                "",
            )
            for item in self.descriptors:
                system_instrument.addItem(
                    f"{item.name} ({item.id})",
                    item.id,
                )
            selected_instrument = (
                previous.system_instrument
                if previous is not None
                else (
                    descriptor.id
                    if descriptor is not None
                    else ""
                )
            )
            index = system_instrument.findData(selected_instrument)
            if index < 0 and selected_instrument:
                system_instrument.addItem(
                    f"{selected_instrument} (not installed)",
                    selected_instrument,
                )
                index = system_instrument.count() - 1
            system_instrument.setCurrentIndex(max(0, index))

            main_reading = QLabel(system_options)
            main_reading.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed,
            )
            monitor_widget = QWidget(system_options)
            monitor_layout = QVBoxLayout(monitor_widget)
            monitor_layout.setContentsMargins(0, 0, 0, 0)
            monitor_layout.setSpacing(14)
            monitor_layout.addStretch(1)

            system_layout.addWidget(
                QLabel("System Instrument"),
                0,
                0,
            )
            system_layout.addWidget(system_instrument, 0, 1)
            system_layout.addWidget(
                QLabel("Main reading"),
                0,
                2,
            )
            system_layout.addWidget(main_reading, 0, 3)
            system_layout.addWidget(
                QLabel("Auxiliary readings"),
                1,
                0,
            )
            system_layout.addWidget(monitor_widget, 1, 1, 1, 3)
            system_layout.setColumnStretch(1, 1)
            system_layout.setColumnStretch(3, 1)
            configuration_layout.addWidget(
                system_options,
                1,
                0,
                1,
                2,
            )

            card_layout.addWidget(configuration)

            ignored_message = QLabel(
                "This VISA resource will not be written to the configuration."
            )
            ignored_message.setObjectName("mutedText")
            card_layout.addWidget(ignored_message)

            selected_auxiliary = (
                previous.auxiliary_readings
                if previous is not None
                else (
                    descriptor.auxiliary_readings
                    if descriptor is not None
                    else ()
                )
            )
            row_controls = {
                "result": result,
                "purpose": purpose,
                "id": resource_id,
                "system_instrument": system_instrument,
                "main_label": main_reading,
                "auxiliary_layout": monitor_layout,
                "auxiliary_checks": {},
                "unavailable_auxiliary": (),
                "configuration": configuration,
                "system_options": system_options,
                "ignored_message": ignored_message,
                "card": card,
                "details_text": details_text,
            }
            self._rows.append(row_controls)
            self._set_reading_options(
                row_controls,
                selected_auxiliary,
            )
            purpose.currentTextChanged.connect(
                lambda _text, values=row_controls:
                self._update_row_enabled(values)
            )
            purpose.currentTextChanged.connect(
                self._update_summary
            )
            system_instrument.currentIndexChanged.connect(
                lambda _index, values=row_controls:
                self._apply_instrument_defaults(values)
            )
            self._update_row_enabled(row_controls)
            self.cards_layout.addWidget(card)

        self.cards_layout.addStretch(1)
        self._update_summary()

    def _descriptor(
        self,
        instrument_id: str,
    ) -> SystemInstrumentDescriptor | None:
        return next(
            (
                item
                for item in self.descriptors
                if item.id == instrument_id
            ),
            None,
        )

    def _apply_instrument_defaults(
        self,
        controls: dict[str, Any],
    ) -> None:
        descriptor = self._descriptor(
            str(
                controls["system_instrument"].currentData()
                or ""
            )
        )
        self._set_reading_options(
            controls,
            descriptor.auxiliary_readings if descriptor is not None else (),
        )

    def _set_reading_options(
        self,
        controls: dict[str, Any],
        selected_auxiliary: tuple[str, ...],
    ) -> None:
        main_label: QLabel = controls["main_label"]
        auxiliary_layout: QVBoxLayout = controls["auxiliary_layout"]
        while auxiliary_layout.count():
            item = auxiliary_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        controls["auxiliary_checks"] = {}
        controls["unavailable_auxiliary"] = ()

        descriptor = self._descriptor(
            str(
                controls["system_instrument"].currentData()
                or ""
            )
        )
        if descriptor is None:
            main_label.setText("Select a System Instrument first")
            note = QLabel(
                "Select a System Instrument to see available readings."
            )
            note.setObjectName("mutedText")
            auxiliary_layout.addWidget(note)
            auxiliary_layout.addStretch(1)
            return

        main_label.setText(descriptor.reading(descriptor.main_reading).label)

        selected_auxiliary_set = set(selected_auxiliary)
        for reading in descriptor.auxiliary_readings:
            checkbox = QCheckBox(
                descriptor.reading(reading).label,
                controls["card"],
            )
            checkbox.setChecked(reading in selected_auxiliary_set)
            controls["auxiliary_checks"][reading] = checkbox
            auxiliary_layout.addWidget(checkbox)
        unavailable = selected_auxiliary_set - set(
            descriptor.auxiliary_readings
        )
        controls["unavailable_auxiliary"] = tuple(sorted(unavailable))
        if unavailable:
            warning = QLabel(
                "Unavailable: " + ", ".join(sorted(unavailable))
            )
            warning.setStyleSheet("color:#b91c1c;")
            auxiliary_layout.addWidget(warning)
        elif not descriptor.auxiliary_readings:
            note = QLabel("No optional auxiliary readings declared.")
            note.setObjectName("mutedText")
            auxiliary_layout.addWidget(note)
        auxiliary_layout.addStretch(1)

    def _update_row_enabled(
        self,
        controls: dict[str, Any],
    ) -> None:
        use = controls["purpose"].currentText()
        selected = use != "Ignore"
        system = use == "System"
        controls["configuration"].setVisible(selected)
        controls["system_options"].setVisible(system)
        controls["ignored_message"].setVisible(not selected)

    def _update_summary(self, *_ignored: object) -> None:
        total = len(self._rows)
        identified = sum(
            bool(controls["result"].identity)
            for controls in self._rows
        )
        configured = sum(
            controls["purpose"].currentText() != "Ignore"
            for controls in self._rows
        )
        errors = sum(
            bool(controls["result"].error)
            and controls["result"].error != "Not seen in this scan"
            for controls in self._rows
        )
        self.summary_label.setText(
            f"{total} addresses   |   {identified} identified   |   "
            f"{configured} configured   |   {errors} errors"
        )

    def _resources(self) -> tuple[InstrumentResource, ...]:
        resources: list[InstrumentResource] = []
        for controls in self._rows:
            use = controls["purpose"].currentText()
            if use == "Ignore":
                continue
            result: VisaScanResult = controls["result"]
            auxiliary = tuple(
                reading
                for reading, checkbox in controls[
                    "auxiliary_checks"
                ].items()
                if checkbox.isChecked()
            )
            resources.append(
                InstrumentResource(
                    id=controls["id"].text(),
                    address=result.address,
                    identity=result.identity,
                    purpose=use.casefold(),
                    system_instrument=(
                        str(
                            controls[
                                "system_instrument"
                            ].currentData()
                            or ""
                        )
                        if use == "System"
                        else ""
                    ),
                    auxiliary_readings=(
                        auxiliary
                        if use == "System"
                        else ()
                    ),
                )
            )
        return tuple(resources)

    def choose_output(self) -> None:
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Save Instrument Resource Configuration",
            str(self.output_path),
            "TOML configuration (*.toml)",
        )
        if not selected:
            return
        path = Path(selected)
        if path.suffix.casefold() != ".toml":
            path = path.with_suffix(".toml")
        resolved = path.resolve()
        try:
            existing = load_instrument_resources(resolved)
        except InstrumentResourceError as exc:
            QMessageBox.warning(
                self,
                "Selected Configuration Is Invalid",
                (
                    f"{exc}\n\nThe current output file remains selected so an "
                    "invalid file is not overwritten without a replacement "
                    "preview."
                ),
            )
            return
        self.output_path = resolved
        self.existing = existing
        self._existing_load_error = ""
        self.output_label.setText(self.output_path.name)
        self.output_label.setToolTip(str(self.output_path))
        self._update_existing_label()
        self._show_results(self._scan_results)

    def preview_and_save(self) -> None:
        if self._existing_load_error:
            QMessageBox.warning(
                self,
                "Existing Configuration Is Invalid",
                "Fix the existing file or choose another output file before "
                "saving, because its replacement entries cannot be listed.",
            )
            return
        missing: list[
            tuple[dict[str, Any], str, tuple[str, ...]]
        ] = []
        for controls in self._rows:
            card: QFrame = controls["card"]
            card.setProperty("invalid", "false")
            card.style().unpolish(card)
            card.style().polish(card)
            use = controls["purpose"].currentText()
            if use == "Ignore":
                continue
            fields: list[str] = []
            if not controls["id"].text().strip():
                fields.append("Resource ID")
            if use == "System":
                if not str(
                    controls["system_instrument"].currentData()
                    or ""
                ).strip():
                    fields.append("System Instrument")
                if controls["unavailable_auxiliary"]:
                    fields.append("Unavailable auxiliary readings")
            if fields:
                result: VisaScanResult = controls["result"]
                missing.append(
                    (controls, result.address, tuple(fields))
                )
        if missing:
            first_card: QFrame = missing[0][0]["card"]
            first_card.setProperty("invalid", "true")
            first_card.style().unpolish(first_card)
            first_card.style().polish(first_card)
            self.scroll_area.ensureWidgetVisible(first_card)
            details = "\n".join(
                f"- {address}: {', '.join(fields)}"
                for _controls, address, fields in missing
            )
            QMessageBox.warning(
                self,
                "Configuration Is Incomplete",
                (
                    "Complete these selected rows before saving. Ignore is "
                    "still available for addresses that should not be used.\n\n"
                    f"{details}"
                ),
            )
            return
        try:
            resources = self._resources()
            rendered = render_instrument_resources(
                resources
            )
        except InstrumentResourceError as exc:
            QMessageBox.warning(
                self,
                "Configuration Is Incomplete",
                str(exc),
            )
            return
        old_by_id = {item.id: item for item in self.existing}
        new_by_id = {item.id: item for item in resources}
        old_ids = set(old_by_id)
        new_ids = set(new_by_id)
        replaced = sorted(
            resource_id
            for resource_id in old_ids & new_ids
            if old_by_id[resource_id] != new_by_id[resource_id]
        )
        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)
        unchanged = sorted(
            resource_id
            for resource_id in old_ids & new_ids
            if old_by_id[resource_id] == new_by_id[resource_id]
        )
        change_summary = "\n".join(
            (
                "Existing entries replaced: "
                + (", ".join(replaced) or "none"),
                "Entries added: " + (", ".join(added) or "none"),
                "Entries removed: " + (", ".join(removed) or "none"),
                "Entries kept unchanged: "
                + (", ".join(unchanged) or "none"),
            )
        )
        answer = QMessageBox.question(
            self,
            "Confirm Instrument Configuration",
            (
                f"Write {len(resources)} confirmed resources to:\n"
                f"{self.output_path}\n\n"
                f"{change_summary}\n\n"
                "The file will be atomically replaced only after you choose "
                "Save.\n\n"
                f"{rendered[:8000]}"
            ),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Save:
            return
        try:
            write_instrument_resources(
                self.output_path,
                resources,
            )
        except (OSError, InstrumentResourceError) as exc:
            QMessageBox.critical(
                self,
                "Cannot Save Configuration",
                str(exc),
            )
            return
        self.existing = resources
        self._update_existing_label()
        self._show_results(self._scan_results)
        self.summary_label.setText(
            f"Saved {len(resources)} resources to {self.output_path.name}"
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.scan_thread is not None:
            QMessageBox.information(
                self,
                "Scan In Progress",
                "Wait for the bounded VISA scan to finish.",
            )
            event.ignore()
            return
        super().closeEvent(event)


def _arguments(
    argv: list[str] | None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only VISA scan and OpenLab Control "
            "instrument resource configuration"
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "configs"
            / "instruments.local.toml"
        ),
    )
    parser.add_argument(
        "--instruments",
        type=Path,
        default=ROOT / "system_instruments",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Per-resource timeout in seconds",
    )
    values = parser.parse_args(argv)
    if values.timeout <= 0 or values.timeout > 30:
        parser.error(
            "--timeout must be greater than 0 and at most 30"
        )
    return values


def main(argv: list[str] | None = None) -> int:
    values = _arguments(argv)
    application = (
        QApplication.instance()
        or QApplication(sys.argv[:1])
    )
    window = InstrumentScannerWindow(
        values.output,
        values.instruments,
        timeout_seconds=values.timeout,
    )
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
