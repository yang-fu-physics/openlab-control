from __future__ import annotations

import csv
import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

from . import __version__
from .config import AppConfig
from .events import EventManager
from .formatting import control_decimals, fixed_number
from .models import DeviceKind, DeviceSnapshot, EventNotice, Severity
from .measurement.manifest import ModuleDescriptor
from .measurement.settings import save_settings


LABVIEW_UNIX_OFFSET_SECONDS = 2_082_844_800.0
DATAFILE_MODES = frozenset({"open", "open|create", "create"})


@dataclass(frozen=True, slots=True)
class RunPaths:
    directory: Path
    data_file: Path
    event_file: Path
    sequence_snapshot: Path
    configuration_snapshot: Path
    module_settings_directory: Path


class DatRunLogger:
    """Writes template-compatible data and event files for one sequence run."""

    def __init__(self, config: AppConfig, events: EventManager) -> None:
        self.config = config
        self.events = events
        self.paths: RunPaths | None = None
        self._started_monotonic = 0.0
        self._data_handle: TextIO | None = None
        self._data_writer: csv.writer | None = None
        self._event_handle: TextIO | None = None
        self._event_writer: csv.writer | None = None
        self._columns: list[str] = []
        self._data_file_initialized = False
        self._module_descriptors: tuple[ModuleDescriptor, ...] = ()
        self._pending_events: list[EventNotice] = []
        self.events.subscribe(self.on_event)

    def open_run(
        self,
        sequence_name: str,
        sequence_text: str,
        module_descriptors: tuple[ModuleDescriptor, ...] = (),
        module_settings: Mapping[str, Mapping[str, Any]] | None = None,
        module_status: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> RunPaths:
        root = self.config.resolve_project_path(self.config.logging.directory)
        root.mkdir(parents=True, exist_ok=True)
        stem = Path(sequence_name).stem or "sequence"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_stem = self._safe_name(stem)
        counter = 0
        while True:
            suffix = "" if counter == 0 else f"_{counter:02d}"
            directory = root / f"{stamp}_{safe_stem}{suffix}"
            try:
                directory.mkdir()
            except FileExistsError:
                counter += 1
                continue
            break
        data_file = directory / self.config.logging.data_file_name
        event_file = directory / self.config.logging.event_file_name
        sequence_snapshot = directory / "sequence.seq"
        config_snapshot = directory / "configuration.toml"
        module_settings_directory = directory / "module_settings"
        module_settings_directory.mkdir()
        sequence_snapshot.write_text(sequence_text, encoding="utf-8", newline="\n")
        shutil.copy2(self.config.source_path, config_snapshot)
        self._module_descriptors = tuple(module_descriptors)
        desired = module_settings or {}
        actual = module_status or {}
        for descriptor in self._module_descriptors:
            save_settings(
                module_settings_directory / f"{descriptor.id}.settings.toml",
                dict(desired.get(descriptor.id, {})),
            )
            (module_settings_directory / f"{descriptor.id}.status-at-start.json").write_text(
                json.dumps(dict(actual.get(descriptor.id, {})), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        self.paths = RunPaths(
            directory,
            data_file,
            event_file,
            sequence_snapshot,
            config_snapshot,
            module_settings_directory,
        )
        self._data_file_initialized = False
        self._started_monotonic = time.monotonic()
        self._open_event_file(event_file)
        for notice in self._pending_events:
            self._write_event(notice)
        self._pending_events.clear()
        return self.paths

    def set_datafile(
        self,
        requested: str,
        mode: str = "open|create",
        *,
        allow_external: bool = False,
    ) -> Path:
        if self.paths is None:
            raise RuntimeError("Run directory has not been created")
        normalized_mode = self._normalize_datafile_mode(mode)
        path = Path(requested)
        external_allowed = allow_external or self.config.logging.allow_external_paths
        if path.is_absolute() and not external_allowed:
            destination = self.paths.directory / path.name
            self.events.report(
                Severity.WARNING,
                "logging",
                "DATAFILE_RELOCATED",
                f"External data path redirected to the run directory: {destination.name}",
                str(path),
            )
        elif path.is_absolute():
            destination = path
        else:
            destination = (self.paths.directory / path).resolve()
            if self.paths.directory.resolve() not in destination.parents and destination != self.paths.directory.resolve():
                destination = self.paths.directory / path.name
                self.events.report(
                    Severity.WARNING,
                    "logging",
                    "DATAFILE_RELOCATED",
                    f"Out-of-scope data path redirected to the run directory: {destination.name}",
                    requested,
                )
        if not destination.name:
            raise ValueError("Data file path must name a file")
        columns = self._build_columns()
        append = self._validate_data_file_plan(
            destination,
            normalized_mode,
            columns,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._close_data_file()
        self._open_data_file(
            destination,
            normalized_mode,
            columns=columns,
            append=append,
        )
        self.paths = RunPaths(
            self.paths.directory,
            destination,
            self.paths.event_file,
            self.paths.sequence_snapshot,
            self.paths.configuration_snapshot,
            self.paths.module_settings_directory,
        )
        return destination

    def ensure_data_file(self) -> Path:
        if self.paths is None:
            raise RuntimeError("Run directory has not been created")
        if self._data_writer is None:
            self._open_data_file(self.paths.data_file, "open|create")
        return self.paths.data_file

    def _open_data_file(
        self,
        path: Path,
        mode: str,
        *,
        columns: list[str] | None = None,
        append: bool | None = None,
    ) -> None:
        normalized = self._normalize_datafile_mode(mode)
        planned_columns = columns if columns is not None else self._build_columns()
        should_append = (
            append
            if append is not None
            else self._validate_data_file_plan(path, normalized, planned_columns)
        )
        handle_mode = "a" if should_append else "w"
        self._data_handle = path.open(handle_mode, encoding="utf-8", newline="")
        self._data_writer = csv.writer(self._data_handle, lineterminator="\n")
        self._columns = list(planned_columns)
        self._data_file_initialized = True
        if not should_append:
            self._data_handle.write("[Header]\n")
            self._data_handle.write("; OpenLab Control Data File (default extension .dat)\n")
            self._data_handle.write("; Timestamp(s) uses the LabVIEW 1904 epoch for template compatibility.\n")
            self._data_writer.writerow(["BYAPP", "OpenLab Control", __version__])
            self._data_writer.writerow(["INFO", "Plugin-oriented laboratory control framework"])
            self._data_writer.writerow(["INFO", f"Started: {datetime.now().astimezone().isoformat()}"])
            for device in self.config.devices:
                self._data_writer.writerow([
                    "INFO",
                    f"Device {device.id}: {device.display_name}; kind={device.kind.value}; plugin={device.plugin}",
                ])
            for module in self._module_descriptors:
                self._data_writer.writerow([
                    "INFO",
                    f"Module {module.id}: {module.name}; version={module.version}; api={module.api_version}",
                ])
            self._data_handle.write("\n[Data]\n")
            self._data_writer.writerow(self._columns)
        elif path.stat().st_size > 0 and not self._ends_with_newline(path):
            self._data_handle.write("\n")
        self._flush_data()

    @staticmethod
    def _normalize_datafile_mode(mode: str) -> str:
        normalized = mode.strip().casefold()
        if normalized not in DATAFILE_MODES:
            allowed = ", ".join(sorted(DATAFILE_MODES))
            raise ValueError(f"Unknown data file mode {mode!r}; expected one of: {allowed}")
        return normalized

    def _validate_data_file_plan(
        self,
        path: Path,
        mode: str,
        columns: list[str],
    ) -> bool:
        if mode == "open" and not path.exists():
            raise FileNotFoundError(path)
        append = (
            mode in {"open", "open|create"}
            and path.exists()
            and path.stat().st_size > 0
        )
        if append:
            existing = self._read_data_columns(path)
            if existing != columns:
                raise ValueError(
                    "Cannot append to a DAT file with a different schema: "
                    f"expected {columns!r}, found {existing!r}"
                )
        return append

    @staticmethod
    def _read_data_columns(path: Path) -> list[str]:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for line in handle:
                    if line.strip().casefold() == "[data]":
                        break
                else:
                    raise ValueError(
                        f"Cannot append to {path}: the file has no [Data] section"
                    )
                for record in csv.reader(handle):
                    values = [cell.strip() for cell in record]
                    if not any(values):
                        continue
                    if values[0].lstrip().startswith(";"):
                        continue
                    return values
        except UnicodeError as exc:
            raise ValueError(
                f"Cannot append to {path}: the file is not UTF-8"
            ) from exc
        raise ValueError(f"Cannot append to {path}: the file has no column header")

    @staticmethod
    def _ends_with_newline(path: Path) -> bool:
        with path.open("rb") as handle:
            handle.seek(-1, 2)
            return handle.read(1) in {b"\n", b"\r"}

    def _build_columns(self) -> list[str]:
        columns = ["Timestamp(s)", "Time(s)", "SequenceStep"]
        temperature_count = sum(
            item.kind is DeviceKind.TEMPERATURE for item in self.config.devices
        )
        field_count = sum(item.kind is DeviceKind.FIELD for item in self.config.devices)
        for device in self.config.devices:
            if device.kind is DeviceKind.TEMPERATURE:
                prefix = "Temp" if temperature_count == 1 else f"{device.id}.Temp"
                columns.extend([f"{prefix}({device.unit})", f"{prefix}Target({device.unit})"])
            elif device.kind is DeviceKind.FIELD:
                prefix = "Field" if field_count == 1 else f"{device.id}.Field"
                columns.extend([f"{prefix}({device.unit})", f"{prefix}Target({device.unit})"])
            elif device.kind is DeviceKind.MONITOR:
                columns.append(f"{device.id}({device.unit})" if device.unit else device.id)
        for module in self._module_descriptors:
            columns.extend(f"{module.id}.{column.label}" for column in module.columns)
        return columns

    def write_module_row(
        self,
        snapshots: dict[str, DeviceSnapshot],
        module_id: str,
        values: Mapping[str, Any],
        sequence_step: str,
    ) -> None:
        self.ensure_data_file()
        assert self._data_writer is not None
        self._data_writer.writerow(self._row(snapshots, module_id, values, sequence_step))
        self._flush_data()

    def write_system_row(
        self,
        snapshots: dict[str, DeviceSnapshot],
        sequence_step: str,
    ) -> None:
        self.ensure_data_file()
        assert self._data_writer is not None
        self._data_writer.writerow(self._row(snapshots, None, {}, sequence_step))
        self._flush_data()

    def _row(
        self,
        snapshots: dict[str, DeviceSnapshot],
        module_id: str | None,
        values: Mapping[str, Any],
        sequence_step: str,
    ) -> list[object]:
        unix_now = time.time()
        absolute = (
            unix_now + LABVIEW_UNIX_OFFSET_SECONDS
            if self.config.logging.timestamp_epoch == "labview_1904"
            else unix_now
        )
        row: list[object] = [f"{absolute:.2f}", f"{time.monotonic() - self._started_monotonic:.2f}", sequence_step]
        for device in self.config.devices:
            snapshot = snapshots.get(device.id)
            if device.kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
                decimals = control_decimals(device.kind, device.unit)
                row.extend([
                    "" if snapshot is None or snapshot.current is None else fixed_number(snapshot.current, decimals),
                    "" if snapshot is None or snapshot.target is None else fixed_number(snapshot.target, decimals),
                ])
            elif device.kind is DeviceKind.MONITOR:
                row.append(
                    ""
                    if snapshot is None or snapshot.current is None
                    else fixed_number(snapshot.current, 3)
                )
        for module in self._module_descriptors:
            for column in module.columns:
                value = values.get(column.name) if module.id == module_id else None
                if value is None:
                    row.append("")
                elif isinstance(value, float):
                    row.append(f"{value:.9g}")
                else:
                    row.append(str(value))
        return row

    def _open_event_file(self, path: Path) -> None:
        self._event_handle = path.open("w", encoding="utf-8", newline="")
        self._event_handle.write("[Header]\n")
        self._event_handle.write("; OpenLab Control Event Log\n\n[Events]\n")
        self._event_writer = csv.writer(self._event_handle, lineterminator="\n")
        self._event_writer.writerow([
            "Timestamp(s)", "ISO8601", "Severity", "Source", "Code", "State", "Count", "Context", "Message"
        ])
        self._event_handle.flush()

    def on_event(self, notice: EventNotice) -> None:
        if self._event_writer is None:
            self._pending_events.append(notice)
        else:
            self._write_event(notice)

    def _write_event(self, notice: EventNotice) -> None:
        if self._event_writer is None:
            return
        event = notice.event
        timestamp = (
            event.resolved_at
            if notice.is_resolution and event.resolved_at is not None
            else event.timestamp
        )
        unix = timestamp.timestamp()
        absolute = unix + LABVIEW_UNIX_OFFSET_SECONDS if self.config.logging.timestamp_epoch == "labview_1904" else unix
        self._event_writer.writerow([
            f"{absolute:.2f}",
            timestamp.isoformat(),
            event.severity.value,
            event.source,
            event.code,
            "RESOLVED" if notice.is_resolution else "RAISED",
            event.count,
            event.context,
            event.message,
        ])
        if self._event_handle is not None:
            self._event_handle.flush()

    def _flush_data(self) -> None:
        if self.config.logging.flush_every_row and self._data_handle is not None:
            self._data_handle.flush()

    def _close_data_file(self) -> None:
        if self._data_handle is not None:
            self._data_handle.flush()
            self._data_handle.close()
        self._data_handle = None
        self._data_writer = None

    def close(self) -> None:
        if self.paths is not None and not self._data_file_initialized:
            self.ensure_data_file()
        self._close_data_file()
        if self._event_handle is not None:
            self._event_handle.flush()
            self._event_handle.close()
        self._event_handle = None
        self._event_writer = None

    @staticmethod
    def _safe_name(value: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        cleaned = "".join(character if character in allowed else "_" for character in value)
        return cleaned.strip("_") or "sequence"
