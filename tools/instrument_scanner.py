"""只读扫描 VISA 仪表，并在人工确认后写入现场资源配置。

扫描阶段只列出 VISA 资源并发送一次 *IDN?。程序不会执行 reset、clear、输出开关、
设定点或 Measurement Module 的 Apply Settings。识别失败的地址仍会列出，用户可以根据
前面板和线缆手动确认。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any


def application_root() -> Path:
    """返回源码项目根目录或打包发布目录。

    Release 把 onefile 扫描器放在 ``tools/``。onefile 解包后的 ``__file__`` 位于临时
    目录，不能用于定位现场配置；此时必须从真实 EXE 位置返回上一级发布目录。
    """

    if getattr(sys, "frozen", False):
        executable_directory = Path(sys.executable).resolve().parent
        if executable_directory.name.casefold() == "tools":
            return executable_directory.parent
        return executable_directory
    return Path(__file__).resolve().parents[1]


ROOT = application_root()
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QThread, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
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

    COLUMNS = (
        "Address",
        "*IDN?",
        "Use",
        "Resource ID",
        "System Instrument",
        "Primary reading",
        "Monitor readings",
        "Scan status",
    )

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
        self._rows: list[dict[str, Any]] = []
        try:
            self.existing = load_instrument_resources(
                self.output_path
            )
        except InstrumentResourceError as exc:
            self.existing = ()
            QMessageBox.warning(
                self,
                "Existing Configuration Is Invalid",
                str(exc),
            )

        self.setWindowTitle(
            "OpenLab Control Instrument Scanner"
        )
        central = QWidget(self)
        layout = QVBoxLayout(central)
        explanation = QLabel(
            "Read-only scan: each VISA address receives one *IDN? "
            "query. Confirm every selected row before saving. No "
            "output or settings commands are sent."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.table = QTableWidget(
            0,
            len(self.COLUMNS),
            central,
        )
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        header = self.table.horizontalHeader()
        for column in range(len(self.COLUMNS)):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        header.setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Stretch,
        )
        layout.addWidget(self.table, 1)

        controls = QHBoxLayout()
        self.scan_button = QPushButton("Scan VISA")
        self.output_button = QPushButton("Choose Output…")
        self.save_button = QPushButton("Preview and Save")
        self.output_label = QLabel(str(self.output_path))
        self.output_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.scan_button.clicked.connect(self.start_scan)
        self.output_button.clicked.connect(self.choose_output)
        self.save_button.clicked.connect(
            self.preview_and_save
        )
        controls.addWidget(self.scan_button)
        controls.addWidget(self.output_button)
        controls.addWidget(self.output_label, 1)
        controls.addWidget(self.save_button)
        layout.addLayout(controls)
        self.setCentralWidget(central)
        self.resize(1450, 620)
        self._show_results(())

    def start_scan(self) -> None:
        if self.scan_thread is not None:
            return
        self.scan_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.statusBar().showMessage(
            "Scanning VISA resources…"
        )
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
        self._show_results(values)
        self.statusBar().showMessage(
            f"Found {len(values)} VISA resources",
            5000,
        )

    def _scan_failed(self, message: str) -> None:
        QMessageBox.critical(
            self,
            "VISA Scan Failed",
            message,
        )
        self.statusBar().clearMessage()

    def _scan_finished(self) -> None:
        """只在线程真正退出后解除关闭保护，避免销毁仍运行的 QThread。"""

        self.scan_thread = None
        self.scan_button.setEnabled(True)
        self.save_button.setEnabled(True)

    def _show_results(
        self,
        results: tuple[VisaScanResult, ...],
    ) -> None:
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

        self.table.setRowCount(0)
        self._rows.clear()
        for result in merged:
            previous = existing_by_address.get(
                result.address.casefold()
            )
            descriptor = match_descriptor(
                result.identity,
                self.descriptors,
            )
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(
                row,
                0,
                self._read_only_item(result.address),
            )
            self.table.setItem(
                row,
                1,
                self._read_only_item(result.identity),
            )

            purpose = QComboBox(self.table)
            purpose.addItems(
                ["Ignore", "System", "Measurement"]
            )
            if previous is not None:
                purpose.setCurrentText(
                    previous.purpose.title()
                )
            elif descriptor is not None:
                purpose.setCurrentText("System")
            self.table.setCellWidget(row, 2, purpose)

            resource_id = QLineEdit(
                (
                    previous.id
                    if previous is not None
                    else suggest_resource_id(
                        result.identity,
                        result.address,
                    )
                ),
                self.table,
            )
            self.table.setCellWidget(
                row,
                3,
                resource_id,
            )

            system_instrument = QComboBox(self.table)
            system_instrument.addItem("", "")
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
            self.table.setCellWidget(row, 4, system_instrument)

            primary = QLineEdit(
                (
                    previous.primary_reading
                    if previous is not None
                    else (
                        descriptor.primary_reading
                        if descriptor is not None
                        else ""
                    )
                ),
                self.table,
            )
            monitors = QLineEdit(
                (
                    ", ".join(previous.monitor_readings)
                    if previous is not None
                    else (
                        ", ".join(
                            descriptor.monitor_readings
                        )
                        if descriptor is not None
                        else ""
                    )
                ),
                self.table,
            )
            primary.setPlaceholderText(
                "for example: temp_b"
            )
            monitors.setPlaceholderText(
                "comma separated, for example: temp_a"
            )
            self.table.setCellWidget(row, 5, primary)
            self.table.setCellWidget(row, 6, monitors)
            self.table.setItem(
                row,
                7,
                self._read_only_item(
                    result.error or "Identified"
                ),
            )
            row_controls = {
                "result": result,
                "purpose": purpose,
                "id": resource_id,
                "system_instrument": system_instrument,
                "primary": primary,
                "monitors": monitors,
            }
            self._rows.append(row_controls)
            purpose.currentTextChanged.connect(
                lambda _text, values=row_controls:
                self._update_row_enabled(values)
            )
            system_instrument.currentIndexChanged.connect(
                lambda _index, values=row_controls:
                self._apply_instrument_defaults(values)
            )
            self._update_row_enabled(row_controls)

    @staticmethod
    def _read_only_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(
            item.flags() & ~Qt.ItemFlag.ItemIsEditable
        )
        item.setToolTip(text)
        return item

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
        if controls["purpose"].currentText() != "System":
            return
        descriptor = self._descriptor(
            str(
                controls["system_instrument"].currentData()
                or ""
            )
        )
        if descriptor is None:
            return
        if not controls["primary"].text().strip():
            controls["primary"].setText(
                descriptor.primary_reading
            )
        if not controls["monitors"].text().strip():
            controls["monitors"].setText(
                ", ".join(descriptor.monitor_readings)
            )

    def _update_row_enabled(
        self,
        controls: dict[str, Any],
    ) -> None:
        use = controls["purpose"].currentText()
        selected = use != "Ignore"
        system = use == "System"
        controls["id"].setEnabled(selected)
        controls["system_instrument"].setEnabled(system)
        controls["primary"].setEnabled(system)
        controls["monitors"].setEnabled(system)
        if system:
            self._apply_instrument_defaults(controls)

    def _resources(self) -> tuple[InstrumentResource, ...]:
        resources: list[InstrumentResource] = []
        for controls in self._rows:
            use = controls["purpose"].currentText()
            if use == "Ignore":
                continue
            result: VisaScanResult = controls["result"]
            monitors = tuple(
                value.strip()
                for value in controls[
                    "monitors"
                ].text().split(",")
                if value.strip()
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
                    primary_reading=(
                        controls["primary"].text()
                        if use == "System"
                        else ""
                    ),
                    monitor_readings=(
                        monitors
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
        self.output_path = path.resolve()
        self.output_label.setText(str(self.output_path))

    def preview_and_save(self) -> None:
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
        answer = QMessageBox.question(
            self,
            "Confirm Instrument Configuration",
            (
                f"Write {len(resources)} confirmed resources to:\n"
                f"{self.output_path}\n\n"
                "The existing file will be atomically replaced.\n\n"
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
        self.statusBar().showMessage(
            f"Saved {self.output_path}",
            5000,
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
