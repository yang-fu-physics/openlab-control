from __future__ import annotations

import csv
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import __version__
from .config import AppConfig
from .events import EventManager
from .formatting import control_decimals, fixed_number
from .models import DeviceKind, DeviceSnapshot, EventNotice, Severity


LABVIEW_UNIX_OFFSET_SECONDS = 2_082_844_800.0


@dataclass(frozen=True, slots=True)
class RunPaths:
    directory: Path
    data_file: Path
    event_file: Path
    sequence_snapshot: Path
    configuration_snapshot: Path


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
        self._pending_events: list[EventNotice] = []
        self.events.subscribe(self.on_event)

    def open_run(self, sequence_name: str, sequence_text: str) -> RunPaths:
        root = self.config.resolve_project_path(self.config.logging.directory)
        root.mkdir(parents=True, exist_ok=True)
        stem = Path(sequence_name).stem or "sequence"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        directory = root / f"{stamp}_{self._safe_name(stem)}"
        counter = 1
        while directory.exists():
            directory = root / f"{stamp}_{self._safe_name(stem)}_{counter:02d}"
            counter += 1
        directory.mkdir(parents=True)
        data_file = directory / self.config.logging.data_file_name
        event_file = directory / self.config.logging.event_file_name
        sequence_snapshot = directory / "sequence.seq"
        config_snapshot = directory / "configuration.toml"
        sequence_snapshot.write_text(sequence_text, encoding="utf-8", newline="\n")
        shutil.copy2(self.config.source_path, config_snapshot)
        self.paths = RunPaths(directory, data_file, event_file, sequence_snapshot, config_snapshot)
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
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._close_data_file()
        self._open_data_file(destination, mode)
        self.paths = RunPaths(
            self.paths.directory,
            destination,
            self.paths.event_file,
            self.paths.sequence_snapshot,
            self.paths.configuration_snapshot,
        )
        return destination

    def ensure_data_file(self) -> Path:
        if self.paths is None:
            raise RuntimeError("Run directory has not been created")
        if self._data_writer is None:
            self._open_data_file(self.paths.data_file, "open|create")
        return self.paths.data_file

    def _open_data_file(self, path: Path, mode: str) -> None:
        normalized = mode.lower()
        if normalized == "open" and not path.exists():
            raise FileNotFoundError(path)
        append = normalized in ("open", "open|create") and path.exists() and path.stat().st_size > 0
        handle_mode = "a" if append else "w"
        self._data_handle = path.open(handle_mode, encoding="utf-8", newline="")
        self._data_writer = csv.writer(self._data_handle, lineterminator="\n")
        self._columns = self._build_columns()
        if not append:
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
            self._data_handle.write("\n[Data]\n")
            self._data_writer.writerow(self._columns)
        self._flush_data()

    def _build_columns(self) -> list[str]:
        columns = ["Timestamp(s)", "Time(s)", "SequenceStep"]
        for device in self.config.devices:
            if device.kind is DeviceKind.TEMPERATURE:
                columns.extend([f"Temp({device.unit})", f"TempTarget({device.unit})"])
            elif device.kind is DeviceKind.FIELD:
                columns.extend([f"Field({device.unit})", f"FieldTarget({device.unit})"])
            elif device.kind is DeviceKind.MEASUREMENT:
                columns.extend([
                    f"{channel}({device.unit})" if device.unit else channel
                    for channel in device.channels
                ])
        return columns

    def write_measurement(
        self,
        snapshots: dict[str, DeviceSnapshot],
        channels: dict[str, float | None],
        sequence_step: str,
    ) -> None:
        self.ensure_data_file()
        assert self._data_writer is not None
        rows = []
        if self.config.logging.sparse_channel_rows and channels:
            for channel, value in channels.items():
                rows.append(self._row(snapshots, {channel: value}, sequence_step))
        else:
            rows.append(self._row(snapshots, channels, sequence_step))
        for row in rows:
            self._data_writer.writerow(row)
        self._flush_data()

    def _row(
        self,
        snapshots: dict[str, DeviceSnapshot],
        channels: dict[str, float | None],
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
            else:
                for channel in device.channels:
                    value = channels.get(channel)
                    if value is None:
                        value = channels.get(f"{device.id}.{channel}")
                    row.append("" if value is None else f"{value:.9g}")
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
        unix = event.timestamp.timestamp()
        absolute = unix + LABVIEW_UNIX_OFFSET_SECONDS if self.config.logging.timestamp_epoch == "labview_1904" else unix
        self._event_writer.writerow([
            f"{absolute:.2f}",
            event.timestamp.isoformat(),
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
