"""Selectable System Instrument logging from existing snapshots only."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import Enum
import time
from pathlib import Path
from typing import Mapping

from .config import AppConfig
from .datafile import LABVIEW_UNIX_OFFSET_SECONDS
from .formatting import fixed_number
from .models import InstrumentKind, InstrumentSnapshot


@dataclass(frozen=True, slots=True)
class SystemLogField:
    instrument_id: str
    key: str
    label: str
    unit: str = ""
    decimals: int | None = None

    @property
    def id(self) -> str:
        return f"{self.instrument_id}.{self.key}"

    @property
    def column(self) -> str:
        suffix = f"({self.unit})" if self.unit else ""
        return f"{self.instrument_id}.{self.label}{suffix}"

    def value(self, snapshots: Mapping[str, InstrumentSnapshot], now: float) -> str:
        snapshot = snapshots.get(self.instrument_id)
        if snapshot is None:
            return ""
        if self.key.startswith("control:"):
            _, panel_id, attribute = self.key.split(":")
            state = snapshot.controls.get(panel_id)
            value = None if state is None else getattr(state, attribute)
        elif self.key.startswith("metric:"):
            metric = snapshot.metrics.get(self.key.split(":", 1)[1])
            value = None if metric is None else metric.value
        elif self.key == "reading_age":
            value = max(0.0, now - snapshot.timestamp)
        else:
            value = getattr(snapshot, self.key)
        if value is None:
            return ""
        if isinstance(value, Enum):
            return str(value.value)
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (int, float)):
            return fixed_number(value, self.decimals) if self.decimals is not None else f"{value:.9g}"
        return str(value)


def system_log_fields(config: AppConfig) -> tuple[SystemLogField, ...]:
    fields = []
    for instrument in config.instrument_instances:
        panels = [p for p in instrument.panels if p.enabled and p.template == "controller"]
        for panel in panels:
            reading = instrument.reading(panel.reading)
            for key, label, unit, decimals in (
                ("current", "Current", reading.unit, reading.decimals),
                ("target", "Target", reading.unit, reading.decimals),
                ("rate_per_minute", "Rate", f"{reading.unit}/min" if reading.unit else "", panel.rate_decimals),
                ("activity", "Activity", "", None),
                ("stability", "Stability", "", None),
                ("ready", "Ready", "", None),
            ):
                fields.append(SystemLogField(instrument.id, f"control:{panel.id}:{key}",
                                             f"{panel.display_name}.{label}", unit, decimals))
        if not panels and instrument.kind is InstrumentKind.MONITOR:
            reading = instrument.reading(instrument.main_reading)
            fields.append(SystemLogField(instrument.id, "current", reading.display_name,
                                         reading.unit, reading.decimals))
        for key, label, unit, decimals in (
            ("connection_state", "Connection", "", None),
            ("reading_age", "Reading Age", "s", 3),
            ("message", "Message", "", None),
        ):
            fields.append(SystemLogField(instrument.id, key, label, unit, decimals))
        for key in instrument.auxiliary_readings:
            reading = instrument.reading(key)
            fields.append(SystemLogField(instrument.id, f"metric:{key}", reading.display_name,
                                         reading.unit, reading.decimals))
    return tuple(fields)


class SystemLogRecorder:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.fields = {field.id: field for field in system_log_fields(config)}
        self.path: Path | None = None
        self.interval_seconds = 30
        self.selected: tuple[SystemLogField, ...] = ()
        self._handle = None
        self._writer = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def state(self) -> dict[str, object]:
        return {"active": self.active, "path": str(self.path) if self.path else "",
                "interval_seconds": self.interval_seconds,
                "fields": [field.id for field in self.selected]}

    def start(self, path: str, interval_seconds: int, field_ids: tuple[str, ...],
              snapshots: Mapping[str, InstrumentSnapshot]) -> dict[str, object]:
        if self.active:
            raise RuntimeError("System Log is already recording")
        if type(interval_seconds) is not int or interval_seconds < 1:
            raise ValueError("Log interval must be a positive integer number of seconds")
        if not field_ids or len(set(field_ids)) != len(field_ids) or any(key not in self.fields for key in field_ids):
            raise ValueError("Select at least one valid System Instrument item, without duplicates")
        if not path.strip():
            raise ValueError("Choose a log file path")
        requested = Path(path).expanduser().resolve()
        if requested.is_dir():
            raise ValueError("Choose a log file, not a directory")
        selected = tuple(self.fields[key] for key in field_ids)
        columns = ["Timestamp(s)", *[field.column for field in selected]]
        append = requested.is_file() and requested.stat().st_size > 0
        if append:
            with requested.open("r", encoding="utf-8", newline="") as existing:
                reader = csv.reader(existing)
                epoch = None
                for row in reader:
                    if row == ["[Data]"]:
                        break
                    if len(row) == 2 and row[0] == "TIMESTAMP_EPOCH":
                        epoch = row[1]
                else:
                    raise ValueError("Existing log has no [Data] section")
                if next(reader, []) != columns or epoch != self.config.logging.timestamp_epoch:
                    raise ValueError("Existing log columns or timestamp epoch differ; select the same items or another file")
        requested.parent.mkdir(parents=True, exist_ok=True)
        handle = requested.open("a", encoding="utf-8", newline="")
        self.path = requested
        self.interval_seconds = interval_seconds
        self.selected = selected
        self._handle = handle
        self._writer = csv.writer(handle, lineterminator="\n")
        try:
            if not append:
                handle.write("[Header]\n")
                self._writer.writerow(["TITLE", "System Instrument Log"])
                self._writer.writerow(["TIMESTAMP_EPOCH", self.config.logging.timestamp_epoch])
                self._writer.writerow(["SOURCE", "Cached instrument snapshots; no additional queries"])
                handle.write("\n[Data]\n")
                self._writer.writerow(columns)
            self.write(snapshots)
        except OSError:
            self.close()
            raise
        return self.state()

    def write(self, snapshots: Mapping[str, InstrumentSnapshot]) -> None:
        now = time.monotonic()
        absolute = time.time()
        if self.config.logging.timestamp_epoch == "labview_1904":
            absolute += LABVIEW_UNIX_OFFSET_SECONDS
        self._writer.writerow([f"{absolute:.2f}",
                               *[field.value(snapshots, now) for field in self.selected]])
        self._handle.flush()

    def close(self) -> None:
        handle, self._handle = self._handle, None
        self._writer = None
        if handle is not None:
            handle.close()
