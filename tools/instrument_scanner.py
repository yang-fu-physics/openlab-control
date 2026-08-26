"""Scan VISA resources and build the complete local instrument configuration."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib
from typing import Any


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = application_root()
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QThread, QTimer, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
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
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


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
QFrame#scannerNavigation {
    background: #ffffff;
    border-right: 1px solid #e2e8f0;
}
QFrame#instrumentCard, QFrame#resourceCard, QFrame#panelCard {
    background: #ffffff;
    border: 1px solid #dbe3ed;
    border-radius: 8px;
}
QFrame#resourceCard[missing="true"] {
    background: #f1f5f9;
    border-color: #cbd5e1;
    color: #64748b;
}
QFrame#instrumentCard[invalid="true"] { border: 2px solid #dc2626; }
QLabel { background: transparent; }
QLabel#mutedText { color: #64748b; }
QLabel#successBadge {
    color: #15803d;
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    border-radius: 9px;
    padding: 2px 8px;
}
QLabel#warningBadge {
    color: #a16207;
    background: #fefce8;
    border: 1px solid #fde68a;
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
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit, QListWidget {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    padding: 5px 8px;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {
    border: 1px solid #2563eb;
}
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
QListWidget#stepList {
    background: #ffffff;
    border: 0;
    border-radius: 0;
    padding: 2px 0;
    outline: 0;
}
QListWidget#stepList::item {
    border-radius: 5px;
    margin: 2px 0;
    padding: 10px 9px;
}
QListWidget#stepList::item:hover { background: #f1f5f9; }
QListWidget#stepList::item:selected {
    background: #dbeafe;
    color: #1d4ed8;
    font-weight: 600;
}
QScrollArea { border: 0; background: #f8fafc; }
QCheckBox { background: transparent; spacing: 6px; }
"""


class ScannerConfigurationError(ValueError):
    """The generated-instrument configuration is invalid."""


@dataclass(frozen=True, slots=True)
class VisaScanResult:
    address: str
    identity: str = ""
    error: str = ""


@dataclass(slots=True)
class ResourceDraft:
    id: str
    address: str
    identity: str
    present: bool
    error: str = ""
    keep_for_measurement: bool = True
    previous_system: str = ""


@dataclass(frozen=True, slots=True)
class ConfiguredVisaResource:
    address: str
    identity: str
    instrument_id: str
    instance_id: str


@dataclass(frozen=True, slots=True)
class SimulationDefinition:
    file_id: str
    instance_id: str
    label: str
    kind: str
    backend: str
    unit: str
    initial_value: float
    default_rate_per_minute: float | None
    min_value: float | None
    max_value: float | None
    max_rate_per_minute: float | None
    stability_tolerance: float | None
    stability_max_slope_per_minute: float | None
    stability_dwell_seconds: float | None
    stability_timeout_seconds: float | None
    stability_window_seconds: float | None
    noise: float


SIMULATIONS = (
    SimulationDefinition(
        "simulated_temperature",
        "temperature",
        "Simulated Temperature",
        "temperature",
        "labcontrol.instruments.simulated:SimulatedTemperatureController",
        "K",
        300.0,
        10.0,
        1.8,
        400.0,
        30.0,
        0.05,
        0.03,
        1.5,
        120.0,
        1.0,
        0.00005,
    ),
    SimulationDefinition(
        "simulated_field",
        "field",
        "Simulated Magnetic Field",
        "field",
        "labcontrol.instruments.simulated:SimulatedFieldController",
        "Oe",
        0.0,
        5000.0,
        -90000.0,
        90000.0,
        10000.0,
        20.0,
        10.0,
        1.0,
        120.0,
        0.8,
        0.01,
    ),
    SimulationDefinition(
        "simulated_second_stage",
        "second_stage",
        "Simulated 2nd Stage",
        "monitor",
        "labcontrol.instruments.simulated:SimulatedReadOnlyMonitor",
        "K",
        4.2,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0.002,
    ),
)


@dataclass(frozen=True, slots=True)
class PlannedWrite:
    path: Path
    text: str


@dataclass(frozen=True, slots=True)
class PlannedPidCreation:
    source: Path
    destination: Path
    requires_validated_zones: bool = False


@dataclass(frozen=True, slots=True)
class SavePlan:
    resources: tuple[InstrumentResource, ...]
    writes: tuple[PlannedWrite, ...]
    deletions: tuple[Path, ...]
    pid_creations: tuple[PlannedPidCreation, ...]
    incomplete_instances: tuple[str, ...]


def scan_visa_resources(
    timeout_seconds: float = 1.0,
) -> tuple[VisaScanResult, ...]:
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
            "Cannot initialize VISA. Install a VISA implementation such as "
            "NI-VISA before scanning real instruments."
        ) from exc
    timeout_ms = max(1, int(timeout_seconds * 1000.0))
    results: list[VisaScanResult] = []
    try:
        for raw_address in manager.list_resources():
            address = str(raw_address)
            handle: Any | None = None
            try:
                handle = manager.open_resource(address, open_timeout=timeout_ms)
                handle.timeout = timeout_ms
                identity = str(handle.query("*IDN?")).strip()
                if not identity:
                    raise RuntimeError("empty *IDN? response")
                if len(identity) > 1024 or any(
                    not character.isprintable() for character in identity
                ):
                    raise RuntimeError(
                        "invalid *IDN? response: expected at most 1024 "
                        "printable characters"
                    )
                results.append(VisaScanResult(address, identity))
            except Exception as exc:
                results.append(
                    VisaScanResult(
                        address=address,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            finally:
                if handle is not None:
                    handle.close()
    finally:
        manager.close()
    return tuple(results)


def discover_scan_descriptors(
    directory: str | Path,
) -> tuple[SystemInstrumentDescriptor, ...]:
    root = Path(directory)
    if not root.is_dir():
        return ()
    descriptors = (
        load_instrument_manifest(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir() and (path / "instrument.toml").is_file()
    )
    return tuple(item for item in descriptors if item.valid)


def match_descriptor(
    identity: str,
    descriptors: tuple[SystemInstrumentDescriptor, ...],
) -> SystemInstrumentDescriptor | None:
    matches = [
        item
        for item in descriptors
        if item.identity_pattern
        and re.search(item.identity_pattern, identity)
    ]
    return matches[0] if len(matches) == 1 else None


def suggest_resource_id(identity: str, address: str) -> str:
    parts = [part.strip() for part in identity.split(",") if part.strip()]
    source = "_".join(parts[:3]) if len(parts) >= 2 else address
    value = re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_")
    if not value or not value[0].isalpha():
        value = f"instrument_{value}" if value else "instrument"
    return value[:64].rstrip("_")


def load_generated_documents(directory: Path) -> dict[str, dict[str, Any]]:
    if not directory.exists():
        return {}
    documents: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.toml"), key=lambda item: item.name.casefold()):
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ScannerConfigurationError(
                f"Cannot read generated instrument configuration {path}: {exc}"
            ) from exc
        documents[path.stem] = raw
    return documents


def configured_visa_resources(
    documents: dict[str, dict[str, Any]],
) -> tuple[ConfiguredVisaResource, ...]:
    result: list[ConfiguredVisaResource] = []
    addresses: set[str] = set()
    for instrument_id, raw in documents.items():
        entries = raw.get("instances", [])
        if not isinstance(entries, list):
            raise ScannerConfigurationError(
                f"configs/instruments/{instrument_id}.toml instances must be an array"
            )
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise ScannerConfigurationError(
                    f"{instrument_id} instances[{index}] must be a table"
                )
            address = str(entry.get("resource", "")).strip()
            if not address:
                continue
            key = address.casefold()
            if key in addresses:
                raise ScannerConfigurationError(
                    f"System resource address is assigned more than once: {address}"
                )
            addresses.add(key)
            instance_id = str(entry.get("id", "")).strip()
            result.append(
                ConfiguredVisaResource(
                    address=address,
                    identity=str(entry.get("identity", "")).strip(),
                    instrument_id=instrument_id,
                    instance_id=instance_id,
                )
            )
    return tuple(result)


def merge_resource_drafts(
    existing: tuple[InstrumentResource, ...],
    previous_system: tuple[ConfiguredVisaResource, ...],
    scanned: tuple[VisaScanResult, ...],
    edits: dict[str, tuple[str, bool]] | None = None,
) -> list[ResourceDraft]:
    edits = edits or {}
    by_address: dict[str, ResourceDraft] = {}
    for item in existing:
        key = item.address.casefold()
        by_address[key] = ResourceDraft(
            id=item.id,
            address=item.address,
            identity=item.identity,
            present=False,
            keep_for_measurement=True,
        )
    for item in previous_system:
        key = item.address.casefold()
        if key in by_address:
            raise ScannerConfigurationError(
                f"Address is both a Measurement and System resource: {item.address}"
            )
        by_address[key] = ResourceDraft(
            id=item.instance_id or suggest_resource_id(item.identity, item.address),
            address=item.address,
            identity=item.identity,
            present=False,
            keep_for_measurement=False,
            previous_system=f"{item.instrument_id}/{item.instance_id}",
        )
    scanned_order: list[str] = []
    for item in scanned:
        key = item.address.casefold()
        if key in scanned_order:
            raise ScannerConfigurationError(
                f"VISA scan returned duplicate address: {item.address}"
            )
        scanned_order.append(key)
        previous = by_address.get(key)
        if previous is None:
            previous = ResourceDraft(
                id=suggest_resource_id(item.identity, item.address),
                address=item.address,
                identity=item.identity,
                present=True,
            )
            by_address[key] = previous
        previous.present = True
        previous.error = item.error
        if item.identity:
            previous.identity = item.identity
    for key, (resource_id, keep) in edits.items():
        if key in by_address:
            by_address[key].id = resource_id
            by_address[key].keep_for_measurement = keep
    missing_order = [key for key in by_address if key not in scanned_order]
    return [by_address[key] for key in (*scanned_order, *missing_order)]


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ScannerConfigurationError("Generated TOML numbers must be finite")
        return repr(value)
    if isinstance(value, list) and not any(isinstance(item, dict) for item in value):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ScannerConfigurationError(
        f"Cannot render generated TOML value of type {type(value).__name__}"
    )


def render_toml_document(raw: dict[str, Any]) -> str:
    lines: list[str] = []

    def emit_table(values: dict[str, Any], path: tuple[str, ...]) -> None:
        scalars = [
            (key, value)
            for key, value in values.items()
            if not isinstance(value, dict)
            and not (
                isinstance(value, list)
                and value
                and all(isinstance(item, dict) for item in value)
            )
        ]
        for key, value in scalars:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in values.items():
            child_path = (*path, key)
            header = ".".join(_toml_key(item) for item in child_path)
            if isinstance(value, dict):
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append(f"[{header}]")
                emit_table(value, child_path)
            elif (
                isinstance(value, list)
                and value
                and all(isinstance(item, dict) for item in value)
            ):
                for item in value:
                    if lines and lines[-1] != "":
                        lines.append("")
                    lines.append(f"[[{header}]]")
                    emit_table(item, child_path)

    emit_table(raw, ())
    return "\n".join(lines).rstrip() + "\n"


def render_generated_instrument(
    descriptor: SystemInstrumentDescriptor,
    instances: list[dict[str, Any]],
) -> str:
    manifest_path = descriptor.path / "instrument.toml"
    with manifest_path.open("rb") as handle:
        raw = tomllib.load(handle)
    del raw["panels"]
    raw["instances"] = instances
    return render_toml_document(raw)


def render_simulation(
    definition: SimulationDefinition,
    order: int,
) -> str:
    controller = definition.kind in {"temperature", "field"}
    panel: dict[str, Any] = {
        "id": "main",
        "label": definition.label,
        "template": "controller" if controller else "readout",
    }
    instance_panel: dict[str, Any] = {
        "id": "main",
        "enabled": True,
        "order": order,
        "role": (
            "sample_temp"
            if definition.kind == "temperature"
            else "field" if definition.kind == "field" else "none"
        ),
    }
    raw: dict[str, Any] = {
        "id": definition.file_id,
        "name": definition.label,
        "version": "1.0.0",
        "api_version": "4",
        "backend": definition.backend,
        "kinds": [definition.kind],
        "readings": {
            "value": {
                "label": definition.label,
                "unit": definition.unit,
                "decimals": 3,
            }
        },
    }
    if controller:
        defaults = {
            "min_value": definition.min_value,
            "max_value": definition.max_value,
            "default_rate_per_minute": definition.default_rate_per_minute,
            "max_rate_per_minute": definition.max_rate_per_minute,
            "stability_tolerance": definition.stability_tolerance,
            "stability_max_slope_per_minute": (
                definition.stability_max_slope_per_minute
            ),
            "stability_dwell_seconds": definition.stability_dwell_seconds,
            "stability_timeout_seconds": definition.stability_timeout_seconds,
            "stability_window_seconds": definition.stability_window_seconds,
        }
        panel.update(
            {
                "control": "main",
                "reading_options": ["value"],
                "default_reading": "value",
                **defaults,
            }
        )
        instance_panel.update({"reading": "value", **defaults})
        raw["controls"] = [{"id": "main", "label": definition.label}]
    else:
        panel["readings"] = ["value"]
    raw["panels"] = [panel]
    raw["instances"] = [
        {
            "id": definition.instance_id,
            "initial_value": definition.initial_value,
            "noise": definition.noise,
            "panels": [instance_panel],
        }
    ]
    return render_toml_document(raw)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def execute_save_plan(visa_path: Path, plan: SavePlan) -> None:
    for item in plan.pid_creations:
        if item.destination.exists():
            continue
        text = item.source.read_text(encoding="utf-8")
        _atomic_write_text(item.destination, text)
    for item in plan.writes:
        _atomic_write_text(item.path, item.text)
    write_instrument_resources(visa_path, plan.resources)
    for path in plan.deletions:
        path.unlink()


class VisaScanThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, timeout_seconds: float, parent: QWidget) -> None:
        super().__init__(parent)
        self.timeout_seconds = timeout_seconds

    def run(self) -> None:
        try:
            self.completed.emit(scan_visa_resources(self.timeout_seconds))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class InstrumentScannerWindow(QMainWindow):
    """A page-per-instrument configuration wizard for laboratory operators."""

    def __init__(
        self,
        config_directory: Path,
        instrument_directory: Path,
        *,
        timeout_seconds: float = 1.0,
    ) -> None:
        super().__init__()
        self.config_directory = config_directory.resolve()
        self.visa_path = self.config_directory / "visa.resources.toml"
        self.generated_directory = self.config_directory / "instruments"
        self.pid_directory = self.config_directory / "pid"
        self.instrument_directory = instrument_directory.resolve()
        self.timeout_seconds = timeout_seconds
        self.descriptors = discover_scan_descriptors(self.instrument_directory)
        self.existing_resources = load_instrument_resources(self.visa_path)
        self.existing_documents = load_generated_documents(
            self.generated_directory
        )
        self.previous_system_resources = configured_visa_resources(
            self.existing_documents
        )
        self.resources = merge_resource_drafts(
            self.existing_resources,
            self.previous_system_resources,
            (),
        )
        self.scan_thread: VisaScanThread | None = None
        self._scan_results: tuple[VisaScanResult, ...] = ()
        self._resource_rows: list[dict[str, Any]] = []
        self._instrument_pages: dict[str, dict[str, Any]] = {}
        self._simulation_checks: dict[str, QCheckBox] = {}
        self._simulation_existing_orders: dict[str, int] = {}
        self._order_by_key: dict[str, int] = {}

        self.setWindowTitle("OpenLab Control Instrument Scanner")
        self.setStyleSheet(SCANNER_STYLE)
        self.setMinimumSize(1040, 680)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame(central)
        header.setObjectName("scannerHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(22, 13, 22, 11)
        title = QLabel("Instrument Setup")
        title.setStyleSheet("font-size:18px; font-weight:700;")
        header_layout.addWidget(title)
        paths = QLabel(
            "Measurement resources: configs/visa.resources.toml   ·   "
            "System Instruments: configs/instruments/"
        )
        paths.setObjectName("mutedText")
        paths.setToolTip(str(self.config_directory))
        header_layout.addWidget(paths)
        layout.addWidget(header)

        body = QWidget(central)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self.navigation_panel = QFrame(body)
        self.navigation_panel.setObjectName("scannerNavigation")
        self.navigation_panel.setFixedWidth(248)
        navigation_layout = QVBoxLayout(self.navigation_panel)
        navigation_layout.setContentsMargins(14, 17, 14, 14)
        navigation_layout.setSpacing(7)
        navigation_title = QLabel("Setup Steps", self.navigation_panel)
        navigation_title.setStyleSheet("font-weight:700;")
        navigation_layout.addWidget(navigation_title)
        navigation_note = QLabel(
            "Complete each step in order, then review every change before saving.",
            self.navigation_panel,
        )
        navigation_note.setObjectName("mutedText")
        navigation_note.setWordWrap(True)
        navigation_layout.addWidget(navigation_note)
        self.step_list = QListWidget(self.navigation_panel)
        self.step_list.setObjectName("stepList")
        self.step_list.setAccessibleName("Instrument setup steps")
        self.step_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.step_list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.step_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        navigation_layout.addWidget(self.step_list, 1)
        body_layout.addWidget(self.navigation_panel)
        self.pages = QStackedWidget(body)
        body_layout.addWidget(self.pages, 1)
        layout.addWidget(body, 1)

        self._build_resource_page()
        for descriptor in self.descriptors:
            self._build_instrument_page(descriptor)
        self._build_review_page()

        footer = QFrame(central)
        footer.setObjectName("scannerFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(22, 11, 22, 11)
        self.footer_status = QLabel()
        self.footer_status.setObjectName("mutedText")
        footer_layout.addWidget(self.footer_status, 1)
        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("primaryButton")
        self.save_button = QPushButton("Save Complete Configuration")
        self.save_button.setObjectName("primaryButton")
        footer_layout.addWidget(self.back_button)
        footer_layout.addWidget(self.next_button)
        footer_layout.addWidget(self.save_button)
        layout.addWidget(footer)

        self.back_button.clicked.connect(
            lambda: self.pages.setCurrentIndex(self.pages.currentIndex() - 1)
        )
        self.next_button.clicked.connect(
            lambda: self.pages.setCurrentIndex(self.pages.currentIndex() + 1)
        )
        self.save_button.clicked.connect(self._save)
        self.step_list.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.pages.currentChanged.connect(self._page_changed)
        self.setCentralWidget(central)
        self.resize(1180, 820)
        self._show_resource_rows()
        self.step_list.setCurrentRow(0)
        self._page_changed(0)
        QTimer.singleShot(0, self.start_scan)

    def _add_page(self, page: QWidget, label: str) -> int:
        index = self.pages.addWidget(page)
        item = QListWidgetItem(f"{index + 1}  {label}")
        item.setToolTip(label)
        self.step_list.addItem(item)
        return index

    def _build_resource_page(self) -> None:
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        explanation = QLabel(
            "VISA Resources\n"
            "Detected addresses remain available even when *IDN? does not "
            "respond. Saved addresses that are not detected are grey and "
            "remain selected until you remove them."
        )
        explanation.setWordWrap(True)
        heading.addWidget(explanation, 1)
        self.scan_button = QPushButton("Scan VISA Resources")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.clicked.connect(self.start_scan)
        heading.addWidget(self.scan_button)
        layout.addLayout(heading)
        self.resource_summary = QLabel()
        self.resource_summary.setObjectName("mutedText")
        layout.addWidget(self.resource_summary)
        self.resource_scroll = QScrollArea(page)
        self.resource_scroll.setWidgetResizable(True)
        self.resource_widget = QWidget(self.resource_scroll)
        self.resource_layout = QVBoxLayout(self.resource_widget)
        self.resource_layout.setContentsMargins(0, 2, 0, 2)
        self.resource_layout.setSpacing(10)
        self.resource_scroll.setWidget(self.resource_widget)
        layout.addWidget(self.resource_scroll, 1)
        self._add_page(page, "VISA Resources")

    def _build_instrument_page(
        self,
        descriptor: SystemInstrumentDescriptor,
    ) -> None:
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel(descriptor.name)
        title.setStyleSheet("font-size:16px; font-weight:700;")
        heading.addWidget(title)
        heading.addStretch(1)
        add_button = QPushButton("Add Instrument")
        heading.addWidget(add_button)
        layout.addLayout(heading)
        note = QLabel(
            "Configure each physical instrument below. Panels come from the "
            "installed template: they can be enabled or disabled here, and "
            "their global order is set on the Review page."
        )
        note.setWordWrap(True)
        note.setObjectName("mutedText")
        layout.addWidget(note)
        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        container = QWidget(scroll)
        cards_layout = QVBoxLayout(container)
        cards_layout.setContentsMargins(0, 2, 0, 2)
        cards_layout.setSpacing(12)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)
        state: dict[str, Any] = {
            "descriptor": descriptor,
            "page": page,
            "layout": cards_layout,
            "instances": [],
            "add_button": add_button,
        }
        self._instrument_pages[descriptor.id] = state
        add_button.clicked.connect(
            lambda _checked=False, item=descriptor: self._add_instance(item)
        )
        existing = self.existing_documents.get(descriptor.id, {})
        raw_instances = existing.get("instances", [])
        if not isinstance(raw_instances, list):
            raise ScannerConfigurationError(
                f"configs/instruments/{descriptor.id}.toml instances must be an array"
            )
        for values in raw_instances:
            if not isinstance(values, dict):
                raise ScannerConfigurationError(
                    f"configs/instruments/{descriptor.id}.toml has an invalid instance"
                )
            self._add_instance(descriptor, values)
        cards_layout.addStretch(1)
        self._add_page(page, descriptor.name)

    def _build_review_page(self) -> None:
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        title = QLabel("Simulations, Panel Order, and Complete Preview")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        layout.addWidget(title)
        simulations = QHBoxLayout()
        simulations.addWidget(QLabel("Optional simulations"))
        for definition in SIMULATIONS:
            checkbox = QCheckBox(definition.label)
            existing = self.existing_documents.get(definition.file_id)
            checkbox.setChecked(existing is not None)
            if existing is not None:
                entries = existing.get("instances", [])
                if isinstance(entries, list) and entries:
                    panels = entries[0].get("panels", [])
                    if isinstance(panels, list) and panels:
                        self._simulation_existing_orders[definition.file_id] = int(
                            panels[0].get("order", 0)
                        )
            checkbox.toggled.connect(self._refresh_review)
            self._simulation_checks[definition.file_id] = checkbox
            simulations.addWidget(checkbox)
        simulations.addStretch(1)
        layout.addLayout(simulations)

        order_heading = QHBoxLayout()
        order_heading.addWidget(QLabel("Enabled panel order"))
        order_heading.addStretch(1)
        self.move_up_button = QPushButton("Move Up")
        self.move_down_button = QPushButton("Move Down")
        order_heading.addWidget(self.move_up_button)
        order_heading.addWidget(self.move_down_button)
        layout.addLayout(order_heading)
        self.order_list = QListWidget(page)
        self.order_list.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove
        )
        self.order_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.order_list.model().rowsMoved.connect(self._order_changed)
        self.move_up_button.clicked.connect(lambda: self._move_order(-1))
        self.move_down_button.clicked.connect(lambda: self._move_order(1))
        layout.addWidget(self.order_list, 1)

        self.review_warning = QLabel()
        self.review_warning.setWordWrap(True)
        self.review_warning.setStyleSheet("color:#a16207; font-weight:600;")
        layout.addWidget(self.review_warning)
        self.preview = QPlainTextEdit(page)
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview.setStyleSheet("font-family:Consolas; font-size:12px;")
        layout.addWidget(self.preview, 2)
        self.review_page_index = self._add_page(page, "Review & Save")

    def _page_changed(self, index: int) -> None:
        if self.step_list.currentRow() != index:
            self.step_list.setCurrentRow(index)
        last = self.pages.count() - 1
        self.back_button.setEnabled(index > 0)
        self.next_button.setVisible(index < last)
        self.save_button.setVisible(index == last)
        if index == last:
            self._refresh_review()
            self.footer_status.setText(
                "Review every file action before saving the complete configuration."
            )
        else:
            self.footer_status.setText(
                f"Step {index + 1} of {self.pages.count()}"
            )

    def start_scan(self) -> None:
        if self.scan_thread is not None:
            return
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Scanning…")
        self.footer_status.setText("Scanning VISA resources with one *IDN? query…")
        worker = VisaScanThread(self.timeout_seconds, self)
        self.scan_thread = worker
        worker.completed.connect(self._scan_completed)
        worker.failed.connect(self._scan_failed)
        worker.finished.connect(self._scan_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _scan_completed(self, results: object) -> None:
        if not isinstance(results, (tuple, list)):
            raise TypeError("VISA scan results must be a tuple or list")
        edits = {
            row["draft"].address.casefold(): (
                row["id"].text().strip(),
                row["keep"].isChecked(),
            )
            for row in self._resource_rows
        }
        self._scan_results = tuple(results)
        self.resources = merge_resource_drafts(
            self.existing_resources,
            self.previous_system_resources,
            self._scan_results,
            edits,
        )
        self._show_resource_rows()
        self._refresh_resource_choices()

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
            + "<br><br>Windows requires a VISA implementation such as "
            + f'<a href="{NI_VISA_DOWNLOAD_URL}">NI-VISA</a>.'
        )
        dialog.exec()

    def _scan_finished(self) -> None:
        self.scan_thread = None
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Scan VISA Resources")

    def _show_resource_rows(self) -> None:
        while self.resource_layout.count():
            item = self.resource_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._resource_rows.clear()
        if not self.resources:
            placeholder = QLabel(
                "No VISA resources are saved or detected. Choose Scan VISA Resources."
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setObjectName("mutedText")
            placeholder.setContentsMargins(0, 50, 0, 0)
            self.resource_layout.addWidget(placeholder)
            self.resource_layout.addStretch(1)
            self.resource_summary.setText("0 resources")
            return
        for draft in self.resources:
            card = QFrame(self.resource_widget)
            card.setObjectName("resourceCard")
            card.setProperty("missing", "false" if draft.present else "true")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(15, 11, 15, 11)
            card_layout.setSpacing(8)

            top = QHBoxLayout()
            status = QLabel()
            if not draft.present:
                status.setText("Not detected")
                status.setObjectName("warningBadge")
            elif draft.error:
                status.setText("Detected · no IDN response")
                status.setObjectName("errorBadge")
            else:
                status.setText("Detected")
                status.setObjectName("successBadge")
            top.addWidget(status)
            address = QLabel(draft.address)
            address.setStyleSheet("font-family:Consolas; font-weight:600;")
            address.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            top.addWidget(address)
            identity = QLabel(draft.identity or "Identity unavailable")
            identity.setObjectName("mutedText")
            identity.setToolTip(draft.identity or draft.error)
            identity.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            top.addWidget(identity, 1)
            card_layout.addLayout(top)

            controls = QHBoxLayout()
            controls.addWidget(QLabel("Resource ID"))
            resource_id = QLineEdit(draft.id, card)
            resource_id.setMaximumWidth(300)
            controls.addWidget(resource_id)
            keep = QCheckBox("Keep for Measurement Module", card)
            keep.setChecked(draft.keep_for_measurement)
            controls.addWidget(keep)
            assignment = QLabel()
            assignment.setObjectName("mutedText")
            controls.addWidget(assignment, 1)
            card_layout.addLayout(controls)
            row = {
                "draft": draft,
                "card": card,
                "id": resource_id,
                "keep": keep,
                "assignment": assignment,
            }
            self._resource_rows.append(row)
            resource_id.textChanged.connect(self._resource_edit_changed)
            keep.toggled.connect(self._resource_edit_changed)
            self.resource_layout.addWidget(card)
        self.resource_layout.addStretch(1)
        self._refresh_resource_assignments()

    def _resource_edit_changed(self, *_ignored: object) -> None:
        for row in self._resource_rows:
            draft: ResourceDraft = row["draft"]
            draft.id = row["id"].text().strip()
            if row["keep"].isEnabled():
                draft.keep_for_measurement = row["keep"].isChecked()

    def _selected_resource_owners(self) -> dict[str, str]:
        owners: dict[str, str] = {}
        for page in self._instrument_pages.values():
            descriptor: SystemInstrumentDescriptor = page["descriptor"]
            if not descriptor.identity_pattern:
                continue
            for row in page["instances"]:
                combo: QComboBox = row["resource"]
                address = str(combo.currentData() or "").strip()
                if address:
                    owners[address.casefold()] = (
                        f"{descriptor.name} / {row['id'].text().strip() or 'new instance'}"
                    )
        return owners

    def _refresh_resource_assignments(self, *_ignored: object) -> None:
        owners = self._selected_resource_owners()
        for page in self._instrument_pages.values():
            descriptor: SystemInstrumentDescriptor = page["descriptor"]
            for instance in page["instances"]:
                status: QLabel = instance["connection_status"]
                if not descriptor.identity_pattern:
                    status.setText("Manual connection settings")
                    continue
                address = str(
                    instance["resource"].currentData() or ""
                ).strip()
                if not address:
                    status.setText("Choose a detected VISA resource")
                elif self._resource_draft(address).present:
                    status.setText("Detected and ready to save")
                else:
                    status.setText("Not detected — this instance will not be saved")
        for row in self._resource_rows:
            draft: ResourceDraft = row["draft"]
            owner = owners.get(draft.address.casefold(), "")
            keep: QCheckBox = row["keep"]
            assignment: QLabel = row["assignment"]
            if owner:
                keep.blockSignals(True)
                keep.setChecked(False)
                keep.blockSignals(False)
                keep.setEnabled(False)
                assignment.setText(f"Assigned to {owner}")
            else:
                keep.setEnabled(True)
                keep.blockSignals(True)
                keep.setChecked(draft.keep_for_measurement)
                keep.blockSignals(False)
                assignment.setText(
                    "Saved but currently unavailable"
                    if not draft.present
                    else "Available"
                )
        assigned = len(owners)
        detected = sum(item.present for item in self.resources)
        unavailable = len(self.resources) - detected
        self.resource_summary.setText(
            f"{len(self.resources)} resources · {detected} detected · "
            f"{unavailable} not detected · {assigned} assigned to System Instruments"
        )
        self._refresh_resource_choices()

    def _refresh_resource_choices(self) -> None:
        selections: list[tuple[QComboBox, str, str]] = []
        for page in self._instrument_pages.values():
            descriptor: SystemInstrumentDescriptor = page["descriptor"]
            if not descriptor.identity_pattern:
                continue
            for row in page["instances"]:
                combo: QComboBox = row["resource"]
                selections.append(
                    (combo, str(combo.currentData() or ""), descriptor.id)
                )
        selected_by_address = {
            address.casefold(): combo
            for combo, address, _instrument_id in selections
            if address
        }
        for combo, selected, instrument_id in selections:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Select a detected VISA resource", "")
            descriptor = self._instrument_pages[instrument_id]["descriptor"]
            for draft in self.resources:
                matched = bool(
                    draft.identity
                    and descriptor.identity_pattern
                    and re.search(descriptor.identity_pattern, draft.identity)
                )
                suffixes = []
                if not draft.present:
                    suffixes.append("not detected")
                elif draft.error:
                    suffixes.append("no IDN response")
                elif matched:
                    suffixes.append("identity match")
                label = draft.address
                if suffixes:
                    label += " — " + ", ".join(suffixes)
                combo.addItem(label, draft.address)
                item = combo.model().item(combo.count() - 1)
                owner_combo = selected_by_address.get(draft.address.casefold())
                item.setEnabled(
                    draft.present and (owner_combo is None or owner_combo is combo)
                )
            index = combo.findData(selected)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)

    def _number_box(
        self,
        value: float,
        minimum: float = -1.0e100,
        maximum: float = 1.0e100,
    ) -> QDoubleSpinBox:
        box = QDoubleSpinBox(self)
        box.setDecimals(9)
        box.setRange(minimum, maximum)
        box.setValue(value)
        box.setKeyboardTracking(False)
        return box

    def _config_field_control(
        self,
        descriptor: SystemInstrumentDescriptor,
        field: Any,
        value: object,
        instance_row: dict[str, Any],
    ) -> QWidget:
        if field.field_type == "string":
            return QLineEdit(str(value), self)
        if field.field_type == "integer":
            control = QSpinBox(self)
            minimum = (
                int(field.minimum)
                if field.minimum is not None
                else -2147483647
            )
            maximum = (
                int(field.maximum)
                if field.maximum is not None
                else 2147483647
            )
            control.setRange(minimum, maximum)
            control.setValue(int(value))
            return control
        if field.field_type == "number":
            return self._number_box(
                float(value),
                float(field.minimum) if field.minimum is not None else -1.0e100,
                float(field.maximum) if field.maximum is not None else 1.0e100,
            )
        if field.field_type == "boolean":
            control = QCheckBox(self)
            control.setChecked(bool(value))
            return control
        if field.field_type == "choice":
            control = QComboBox(self)
            for option in field.options:
                control.addItem(option, option)
            control.setCurrentIndex(control.findData(str(value)))
            return control
        if field.field_type == "pid_file":
            wrapper = QWidget(self)
            row = QHBoxLayout(wrapper)
            row.setContentsMargins(0, 0, 0, 0)
            display = QLineEdit(wrapper)
            display.setReadOnly(True)
            choose = QPushButton("Choose PID File…", wrapper)
            row.addWidget(display, 1)
            row.addWidget(choose)
            field_state = {
                "wrapper": wrapper,
                "display": display,
                "choose": choose,
                "source": descriptor.path / str(field.default),
                "default_source": descriptor.path / str(field.default),
            }
            instance_row["pid_fields"][field.id] = field_state
            choose.clicked.connect(
                lambda _checked=False, state=field_state: self._choose_pid_file(state)
            )
            return wrapper
        raise ScannerConfigurationError(
            f"Unsupported config field type: {field.field_type}"
        )

    def _choose_pid_file(self, state: dict[str, Any]) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose validated PID configuration",
            str(state["source"]),
            "TOML configuration (*.toml)",
        )
        if not selected:
            return
        source = Path(selected).resolve()
        try:
            source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "Cannot Read PID File", str(exc))
            return
        state["source"] = source
        state["display"].setText(str(source))

    def _update_pid_controls(self, instance_row: dict[str, Any]) -> None:
        instance_id = instance_row["id"].text().strip()
        target = self.pid_directory / f"{instance_id}.toml"
        for state in instance_row["pid_fields"].values():
            if target.is_file():
                state["display"].setText(
                    f"{target}  (existing file will be preserved)"
                )
                state["choose"].setEnabled(False)
            else:
                state["display"].setText(str(state["source"]))
                state["choose"].setEnabled(True)

    def _next_instance_id(
        self,
        descriptor: SystemInstrumentDescriptor,
    ) -> str:
        used = {
            row["id"].text().strip()
            for page in self._instrument_pages.values()
            for row in page["instances"]
        }
        candidate = descriptor.id
        number = 2
        while candidate in used:
            candidate = f"{descriptor.id}_{number}"
            number += 1
        return candidate

    def _add_instance(
        self,
        descriptor: SystemInstrumentDescriptor,
        values: dict[str, Any] | None = None,
    ) -> None:
        values = values or {}
        page = self._instrument_pages[descriptor.id]
        card = QFrame(page["page"])
        card.setObjectName("instrumentCard")
        card.setProperty("invalid", "false")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 13, 16, 13)
        card_layout.setSpacing(11)

        top = QHBoxLayout()
        top.addWidget(QLabel("Instance ID"))
        instance_id = QLineEdit(
            str(values.get("id", self._next_instance_id(descriptor))), card
        )
        instance_id.setMaximumWidth(300)
        top.addWidget(instance_id)
        connection_status = QLabel()
        connection_status.setObjectName("mutedText")
        top.addWidget(connection_status, 1)
        remove = QPushButton("Remove Instrument", card)
        top.addWidget(remove)
        card_layout.addLayout(top)

        form_widget = QWidget(card)
        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        resource_combo: QComboBox | None = None
        if descriptor.identity_pattern:
            resource_combo = QComboBox(form_widget)
            resource_combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            resource_combo.setMinimumContentsLength(32)
            form.addRow("VISA Resource", resource_combo)
        instance_row: dict[str, Any] = {
            "card": card,
            "id": instance_id,
            "resource": resource_combo,
            "connection_status": connection_status,
            "fields": {},
            "pid_fields": {},
            "panels": [],
            "source_values": values,
        }
        for field in descriptor.config_fields:
            raw_value = values.get(field.id, field.default)
            control = self._config_field_control(
                descriptor, field, raw_value, instance_row
            )
            instance_row["fields"][field.id] = (field, control)
            form.addRow(field.label, control)
        card_layout.addWidget(form_widget)

        panels_title = QLabel("Fixed Panels")
        panels_title.setStyleSheet("font-weight:700;")
        card_layout.addWidget(panels_title)
        raw_panels = values.get("panels", [])
        if not isinstance(raw_panels, list):
            raise ScannerConfigurationError(
                f"{descriptor.id}/{values.get('id', '')} panels must be an array"
            )
        existing_panels = {
            str(item["id"]): item
            for item in raw_panels
            if isinstance(item, dict) and "id" in item
        }
        if len(existing_panels) != len(raw_panels):
            raise ScannerConfigurationError(
                f"{descriptor.id}/{values.get('id', '')} has invalid panel entries"
            )
        for panel in descriptor.panels:
            saved = existing_panels.get(panel.id, {})
            panel_card = QFrame(card)
            panel_card.setObjectName("panelCard")
            panel_layout = QVBoxLayout(panel_card)
            panel_layout.setContentsMargins(12, 9, 12, 9)
            panel_layout.setSpacing(8)
            panel_top = QHBoxLayout()
            enabled = QCheckBox(panel.label, panel_card)
            enabled.setChecked(bool(saved.get("enabled", True)))
            panel_top.addWidget(enabled)
            kind = QLabel(panel.template.replace("_", " ").title())
            kind.setObjectName("mutedText")
            panel_top.addWidget(kind)
            panel_top.addStretch(1)
            panel_layout.addLayout(panel_top)
            panel_fields: dict[str, QWidget] = {}
            if panel.template == "controller":
                details = QWidget(panel_card)
                details_form = QFormLayout(details)
                details_form.setContentsMargins(20, 0, 0, 0)
                details_form.setHorizontalSpacing(14)
                reading = QComboBox(details)
                for key in panel.reading_options:
                    metadata = descriptor.reading(key)
                    reading.addItem(metadata.label, key)
                reading.setCurrentIndex(
                    max(
                        0,
                        reading.findData(
                            str(saved.get("reading", panel.default_reading))
                        ),
                    )
                )
                role = QComboBox(details)
                role.addItem("None", "none")
                role.addItem("Sample Temp", "sample_temp")
                role.addItem("Field", "field")
                role.setCurrentIndex(
                    max(0, role.findData(str(saved.get("role", "none"))))
                )
                details_form.addRow("Reading", reading)
                details_form.addRow("Sequence Role", role)
                numeric_specs = (
                    ("min_value", "Minimum", panel.min_value),
                    ("max_value", "Maximum", panel.max_value),
                    (
                        "default_rate_per_minute",
                        "Default Rate / min",
                        panel.default_rate_per_minute,
                    ),
                    (
                        "max_rate_per_minute",
                        "Maximum Rate / min",
                        panel.max_rate_per_minute,
                    ),
                    (
                        "stability_tolerance",
                        "Stability Tolerance",
                        panel.stability_tolerance,
                    ),
                    (
                        "stability_max_slope_per_minute",
                        "Maximum Stable Slope / min",
                        panel.stability_max_slope_per_minute,
                    ),
                    (
                        "stability_dwell_seconds",
                        "Stable Dwell (seconds)",
                        panel.stability_dwell_seconds,
                    ),
                    (
                        "stability_timeout_seconds",
                        "Stability Timeout (seconds)",
                        panel.stability_timeout_seconds,
                    ),
                    (
                        "stability_window_seconds",
                        "Stability Window (seconds)",
                        panel.stability_window_seconds,
                    ),
                )
                panel_fields["reading"] = reading
                panel_fields["role"] = role
                for key, label, default in numeric_specs:
                    box = self._number_box(float(saved.get(key, default)))
                    panel_fields[key] = box
                    details_form.addRow(label, box)
                enabled.toggled.connect(details.setVisible)
                details.setVisible(enabled.isChecked())
                panel_layout.addWidget(details)
            instance_row["panels"].append(
                {
                    "descriptor": panel,
                    "enabled": enabled,
                    "fields": panel_fields,
                    "existing_order": int(saved.get("order", 0)),
                }
            )
            card_layout.addWidget(panel_card)

        page["instances"].append(instance_row)
        insertion = max(0, page["layout"].count() - 1)
        page["layout"].insertWidget(insertion, card)
        remove.clicked.connect(
            lambda _checked=False, item=instance_row, state=page: self._remove_instance(
                state, item
            )
        )
        instance_id.textChanged.connect(
            lambda _text, item=instance_row: self._update_pid_controls(item)
        )
        instance_id.textChanged.connect(self._refresh_resource_assignments)
        if resource_combo is not None:
            resource_combo.currentIndexChanged.connect(
                self._refresh_resource_assignments
            )
        self._update_pid_controls(instance_row)
        self._refresh_resource_choices()
        if resource_combo is not None:
            selected = str(values.get("resource", ""))
            resource_combo.setCurrentIndex(max(0, resource_combo.findData(selected)))
        self._refresh_resource_assignments()

    def _remove_instance(
        self,
        page: dict[str, Any],
        instance_row: dict[str, Any],
    ) -> None:
        page["instances"].remove(instance_row)
        instance_row["card"].deleteLater()
        self._refresh_resource_assignments()

    def _field_value(self, field: Any, control: QWidget) -> object:
        if field.field_type == "string":
            return control.text().strip()
        if field.field_type == "integer":
            return control.value()
        if field.field_type == "number":
            return control.value()
        if field.field_type == "boolean":
            return control.isChecked()
        if field.field_type == "choice":
            return str(control.currentData())
        if field.field_type == "pid_file":
            raise ScannerConfigurationError(
                "PID fields are resolved from their instance ID"
            )
        raise ScannerConfigurationError(
            f"Unsupported config field type: {field.field_type}"
        )

    def _resource_draft(self, address: str) -> ResourceDraft:
        return next(
            item
            for item in self.resources
            if item.address.casefold() == address.casefold()
        )

    def _instance_is_complete(
        self,
        descriptor: SystemInstrumentDescriptor,
        row: dict[str, Any],
    ) -> bool:
        if not _IDENTIFIER.fullmatch(row["id"].text().strip()):
            return False
        if any(
            field.field_type == "string" and not control.text().strip()
            for field, control in row["fields"].values()
        ):
            return False
        if not descriptor.identity_pattern:
            return True
        address = str(row["resource"].currentData() or "").strip()
        return bool(address and self._resource_draft(address).present)

    def _panel_key(
        self,
        descriptor_id: str,
        instance_id: str,
        panel_id: str,
    ) -> str:
        return f"system:{descriptor_id}:{instance_id}:{panel_id}"

    def _panel_candidates(self) -> list[tuple[str, str, int]]:
        candidates: list[tuple[str, str, int]] = []
        for page in self._instrument_pages.values():
            descriptor: SystemInstrumentDescriptor = page["descriptor"]
            for row in page["instances"]:
                if not self._instance_is_complete(descriptor, row):
                    continue
                instance_id = row["id"].text().strip()
                for panel_row in row["panels"]:
                    if not panel_row["enabled"].isChecked():
                        continue
                    panel = panel_row["descriptor"]
                    candidates.append(
                        (
                            self._panel_key(
                                descriptor.id, instance_id, panel.id
                            ),
                            f"{descriptor.name} — {instance_id} — {panel.label}",
                            panel_row["existing_order"],
                        )
                    )
        for definition in SIMULATIONS:
            if self._simulation_checks[definition.file_id].isChecked():
                candidates.append(
                    (
                        f"simulation:{definition.file_id}",
                        definition.label,
                        self._simulation_existing_orders.get(
                            definition.file_id, 0
                        ),
                    )
                )
        return candidates

    def _refresh_order_list(self) -> None:
        candidates = self._panel_candidates()
        previous = {
            self.order_list.item(index).data(Qt.ItemDataRole.UserRole): index
            for index in range(self.order_list.count())
        }
        candidate_by_key = {key: (label, order) for key, label, order in candidates}
        keys = sorted(
            candidate_by_key,
            key=lambda key: (
                0 if key in previous else 1,
                previous.get(key, candidate_by_key[key][1] or 10**9),
                candidate_by_key[key][0].casefold(),
            ),
        )
        self.order_list.blockSignals(True)
        self.order_list.clear()
        for key in keys:
            item = QListWidgetItem(candidate_by_key[key][0])
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.order_list.addItem(item)
        self.order_list.blockSignals(False)
        self._update_order_map()

    def _update_order_map(self) -> None:
        self._order_by_key = {
            str(self.order_list.item(index).data(Qt.ItemDataRole.UserRole)): index
            + 1
            for index in range(self.order_list.count())
        }

    def _order_changed(self, *_ignored: object) -> None:
        self._update_order_map()
        self._refresh_preview()

    def _move_order(self, offset: int) -> None:
        current = self.order_list.currentRow()
        target = current + offset
        if current < 0 or not 0 <= target < self.order_list.count():
            return
        item = self.order_list.takeItem(current)
        self.order_list.insertItem(target, item)
        self.order_list.setCurrentRow(target)
        self._order_changed()

    def _controller_panel_document(
        self,
        descriptor: SystemInstrumentDescriptor,
        instance_id: str,
        panel_row: dict[str, Any],
    ) -> dict[str, Any]:
        panel = panel_row["descriptor"]
        enabled = panel_row["enabled"].isChecked()
        result: dict[str, Any] = {"id": panel.id, "enabled": enabled}
        if not enabled:
            return result
        key = self._panel_key(descriptor.id, instance_id, panel.id)
        result["order"] = self._order_by_key[key]
        if panel.template != "controller":
            result["role"] = "none"
            return result
        fields = panel_row["fields"]
        result.update(
            {
                "role": str(fields["role"].currentData()),
                "reading": str(fields["reading"].currentData()),
                "min_value": fields["min_value"].value(),
                "max_value": fields["max_value"].value(),
                "default_rate_per_minute": fields[
                    "default_rate_per_minute"
                ].value(),
                "max_rate_per_minute": fields[
                    "max_rate_per_minute"
                ].value(),
                "stability_tolerance": fields[
                    "stability_tolerance"
                ].value(),
                "stability_max_slope_per_minute": fields[
                    "stability_max_slope_per_minute"
                ].value(),
                "stability_dwell_seconds": fields[
                    "stability_dwell_seconds"
                ].value(),
                "stability_timeout_seconds": fields[
                    "stability_timeout_seconds"
                ].value(),
                "stability_window_seconds": fields[
                    "stability_window_seconds"
                ].value(),
            }
        )
        if result["min_value"] >= result["max_value"]:
            raise ScannerConfigurationError(
                f"{descriptor.name} / {instance_id} / {panel.label}: "
                "Minimum must be less than Maximum"
            )
        if (
            result["default_rate_per_minute"] <= 0
            or result["max_rate_per_minute"] <= 0
            or result["default_rate_per_minute"]
            > result["max_rate_per_minute"]
        ):
            raise ScannerConfigurationError(
                f"{descriptor.name} / {instance_id} / {panel.label}: "
                "rates must be positive and Default Rate must not exceed Maximum Rate"
            )
        if (
            result["stability_tolerance"] < 0
            or result["stability_max_slope_per_minute"] < 0
            or result["stability_dwell_seconds"] < 0
            or result["stability_timeout_seconds"] <= 0
            or result["stability_window_seconds"] <= 0
        ):
            raise ScannerConfigurationError(
                f"{descriptor.name} / {instance_id} / {panel.label}: "
                "stability tolerance, slope and dwell cannot be negative; "
                "timeout and window must be positive"
            )
        return result

    def _instance_document(
        self,
        descriptor: SystemInstrumentDescriptor,
        row: dict[str, Any],
    ) -> tuple[
        dict[str, Any] | None,
        tuple[PlannedPidCreation, ...],
    ]:
        if not self._instance_is_complete(descriptor, row):
            return None, ()
        instance_id = row["id"].text().strip()
        result: dict[str, Any] = {"id": instance_id}
        if descriptor.identity_pattern:
            address = str(row["resource"].currentData())
            draft = self._resource_draft(address)
            result["resource"] = address
            result["identity"] = draft.identity
        pid_creations: list[PlannedPidCreation] = []
        for field_id, (field, control) in row["fields"].items():
            if field.field_type != "pid_file":
                result[field_id] = self._field_value(field, control)
                continue
            target = self.pid_directory / f"{instance_id}.toml"
            state = row["pid_fields"][field_id]
            source: Path = state["source"]
            if not target.exists():
                source.read_text(encoding="utf-8")
                pid_creations.append(
                    PlannedPidCreation(
                        source=source,
                        destination=target,
                        requires_validated_zones=(
                            source.resolve()
                            == state["default_source"].resolve()
                        ),
                    )
                )
            result[field_id] = f"configs/pid/{instance_id}.toml"
        result["panels"] = [
            self._controller_panel_document(descriptor, instance_id, panel_row)
            for panel_row in row["panels"]
        ]
        return result, tuple(pid_creations)

    def _measurement_resources(
        self,
        assigned_addresses: set[str],
    ) -> tuple[InstrumentResource, ...]:
        self._resource_edit_changed()
        return tuple(
            InstrumentResource(
                id=draft.id,
                address=draft.address,
                identity=draft.identity,
            )
            for draft in self.resources
            if draft.keep_for_measurement
            and draft.address.casefold() not in assigned_addresses
        )

    def _build_save_plan(self) -> SavePlan:
        writes: list[PlannedWrite] = []
        pid_creations: list[PlannedPidCreation] = []
        incomplete: list[str] = []
        instance_ids: set[str] = set()
        assigned_addresses: set[str] = set()
        role_owners: dict[str, str] = {}
        planned_paths: set[Path] = set()

        for page in self._instrument_pages.values():
            descriptor: SystemInstrumentDescriptor = page["descriptor"]
            instances: list[dict[str, Any]] = []
            for row in page["instances"]:
                raw_id = row["id"].text().strip()
                document, creations = self._instance_document(descriptor, row)
                if document is None:
                    incomplete.append(
                        f"{descriptor.name} / {raw_id or 'unnamed instance'}"
                    )
                    continue
                instance_id = str(document["id"])
                if instance_id in instance_ids:
                    raise ScannerConfigurationError(
                        f"Duplicate instance ID: {instance_id}"
                    )
                instance_ids.add(instance_id)
                if "resource" in document:
                    address_key = str(document["resource"]).casefold()
                    if address_key in assigned_addresses:
                        raise ScannerConfigurationError(
                            f"VISA resource is assigned more than once: "
                            f"{document['resource']}"
                        )
                    assigned_addresses.add(address_key)
                for panel in document["panels"]:
                    role = str(panel.get("role", "none"))
                    if not panel["enabled"] or role == "none":
                        continue
                    owner = f"{descriptor.name} / {instance_id} / {panel['id']}"
                    if role in role_owners:
                        raise ScannerConfigurationError(
                            f"Sequence role {role!r} is assigned to both "
                            f"{role_owners[role]} and {owner}"
                        )
                    role_owners[role] = owner
                instances.append(document)
                pid_creations.extend(creations)
            if instances:
                path = self.generated_directory / f"{descriptor.id}.toml"
                writes.append(
                    PlannedWrite(
                        path,
                        render_generated_instrument(descriptor, instances),
                    )
                )
                planned_paths.add(path.resolve())

        for definition in SIMULATIONS:
            if not self._simulation_checks[definition.file_id].isChecked():
                continue
            role = (
                "sample_temp"
                if definition.kind == "temperature"
                else "field" if definition.kind == "field" else "none"
            )
            if role != "none":
                if role in role_owners:
                    raise ScannerConfigurationError(
                        f"Sequence role {role!r} is assigned to both "
                        f"{role_owners[role]} and {definition.label}"
                    )
                role_owners[role] = definition.label
            key = f"simulation:{definition.file_id}"
            path = self.generated_directory / f"{definition.file_id}.toml"
            writes.append(
                PlannedWrite(
                    path,
                    render_simulation(definition, self._order_by_key[key]),
                )
            )
            planned_paths.add(path.resolve())

        resources = self._measurement_resources(assigned_addresses)
        render_instrument_resources(resources)
        existing_paths = (
            {
                path.resolve()
                for path in self.generated_directory.glob("*.toml")
            }
            if self.generated_directory.exists()
            else set()
        )
        return SavePlan(
            resources=resources,
            writes=tuple(writes),
            deletions=tuple(sorted(existing_paths - planned_paths)),
            pid_creations=tuple(pid_creations),
            incomplete_instances=tuple(incomplete),
        )

    def _refresh_review(self, *_ignored: object) -> None:
        self._refresh_order_list()
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        try:
            plan = self._build_save_plan()
            text = self._preview_text(plan)
        except (
            OSError,
            UnicodeError,
            InstrumentResourceError,
            ScannerConfigurationError,
        ) as exc:
            self.preview.setPlainText(f"Configuration cannot be saved:\n\n{exc}")
            self.review_warning.setText(str(exc))
            self.save_button.setEnabled(False)
            return
        warnings: list[str] = []
        if plan.incomplete_instances:
            warnings.append(
                "Incomplete instances will not be saved: "
                + ", ".join(plan.incomplete_instances)
            )
        if any(item.requires_validated_zones for item in plan.pid_creations):
            warnings.append(
                "A copied PID example contains no validated zones. Fill the "
                "new PID file with values validated for this cryostat before "
                "OpenLab Control can start."
            )
        self.review_warning.setText("\n".join(warnings))
        self.preview.setPlainText(text)
        self.save_button.setEnabled(True)

    def _file_action(self, path: Path, text: str) -> str:
        if not path.exists():
            return "CREATE"
        try:
            unchanged = path.read_text(encoding="utf-8") == text
        except UnicodeError as exc:
            raise ScannerConfigurationError(
                f"Configuration file is not UTF-8 text: {path}"
            ) from exc
        return "UNCHANGED" if unchanged else "OVERWRITE"

    def _preview_text(self, plan: SavePlan) -> str:
        rendered_resources = render_instrument_resources(plan.resources)
        lines = [
            "COMPLETE SAVE PLAN",
            "==================",
            f"{self._file_action(self.visa_path, rendered_resources):<10} "
            f"{self.visa_path}",
        ]
        for item in plan.writes:
            lines.append(
                f"{self._file_action(item.path, item.text):<10} {item.path}"
            )
        for path in plan.deletions:
            lines.append(f"DELETE     {path}")
        for item in plan.pid_creations:
            lines.append(
                f"CREATE PID {item.destination}  <-  {item.source}"
            )
        if not plan.writes and not plan.deletions:
            lines.append("No System Instrument files selected.")

        old_by_address = {
            item.address.casefold(): item for item in self.existing_resources
        }
        new_by_address = {
            item.address.casefold(): item for item in plan.resources
        }
        added = [
            item.id
            for key, item in new_by_address.items()
            if key not in old_by_address
        ]
        removed = [
            item.id
            for key, item in old_by_address.items()
            if key not in new_by_address
        ]
        lines.extend(
            [
                "",
                "VISA RESOURCE CHANGES",
                "=====================",
                "Added: " + (", ".join(added) or "none"),
                "Removed or assigned to System Instruments: "
                + (", ".join(removed) or "none"),
                "",
                f"FILE: {self.visa_path}",
                "-" * min(100, len(str(self.visa_path)) + 6),
                rendered_resources.rstrip(),
            ]
        )
        for item in plan.writes:
            lines.extend(
                [
                    "",
                    f"FILE: {item.path}",
                    "-" * min(100, len(str(item.path)) + 6),
                    item.text.rstrip(),
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _save(self) -> None:
        try:
            plan = self._build_save_plan()
        except (
            OSError,
            UnicodeError,
            InstrumentResourceError,
            ScannerConfigurationError,
        ) as exc:
            QMessageBox.warning(self, "Configuration Is Incomplete", str(exc))
            return
        answer = QMessageBox.question(
            self,
            "Save Complete Instrument Configuration",
            (
                "Atomically replace each listed configuration file and "
                "delete the generated files marked DELETE?\n\n"
                "Existing PID files are never overwritten or deleted."
            ),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Save:
            return
        try:
            execute_save_plan(self.visa_path, plan)
        except (OSError, UnicodeError, InstrumentResourceError) as exc:
            QMessageBox.critical(self, "Cannot Save Configuration", str(exc))
            return
        self.existing_resources = plan.resources
        self.existing_documents = load_generated_documents(
            self.generated_directory
        )
        self.previous_system_resources = configured_visa_resources(
            self.existing_documents
        )
        self.footer_status.setText("Complete instrument configuration saved.")
        warning = ""
        if any(item.requires_validated_zones for item in plan.pid_creations):
            warning = (
                "\n\nThe new PID file contains no validated zones. Fill it "
                "with values validated for this cryostat before starting "
                "OpenLab Control."
            )
        QMessageBox.information(
            self,
            "Configuration Saved",
            "The complete instrument configuration was saved." + warning,
        )
        self._refresh_review()

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


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan VISA and configure OpenLab Control instruments"
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
        help="Per-resource *IDN? timeout in seconds",
    )
    values = parser.parse_args(argv)
    if values.timeout <= 0 or values.timeout > 30:
        parser.error("--timeout must be greater than 0 and at most 30")
    return values


def main(argv: list[str] | None = None) -> int:
    values = _arguments(argv)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    window = InstrumentScannerWindow(
        ROOT / "configs",
        values.instruments,
        timeout_seconds=values.timeout,
    )
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
