from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import DeviceKind, Severity


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StabilityConfig:
    tolerance: float
    max_slope_per_minute: float
    dwell_seconds: float
    timeout_seconds: float
    window_seconds: float
    stale_after_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    id: str
    display_name: str
    kind: DeviceKind
    plugin: str
    unit: str = ""
    initial_value: float = 0.0
    default_rate_per_minute: float = 1.0
    min_value: float = float("-inf")
    max_value: float = float("inf")
    max_rate_per_minute: float = float("inf")
    stability: StabilityConfig | None = None
    channels: tuple[str, ...] = ()
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    directory: str = "runs"
    data_file_name: str = "experiment.dat"
    event_file_name: str = "events.dat"
    timestamp_epoch: str = "labview_1904"
    sparse_channel_rows: bool = True
    flush_every_row: bool = True
    allow_external_paths: bool = False


@dataclass(frozen=True, slots=True)
class AlarmConfig:
    stability_timeout: Severity = Severity.ERROR
    stale_reading: Severity = Severity.WARNING
    popup_warnings: bool = True
    popup_errors: bool = True


@dataclass(frozen=True, slots=True)
class AppConfig:
    source_path: Path
    title: str
    ui_scale: float | None
    ui_refresh_ms: int
    poll_interval_seconds: float
    simulation_speed: float
    default_sequence: str
    language: str
    logging: LoggingConfig
    alarms: AlarmConfig
    abort_temperature: str
    abort_field: str
    devices: tuple[DeviceConfig, ...]

    @property
    def project_root(self) -> Path:
        return self.source_path.resolve().parent.parent

    def resolve_project_path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()

    def device(self, device_id: str) -> DeviceConfig:
        for item in self.devices:
            if item.id == device_id:
                return item
        raise KeyError(device_id)


def _severity(value: str, key: str) -> Severity:
    try:
        return Severity(value.lower())
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be info, warning, or error") from exc


def _ui_scale(value: object) -> float | None:
    if value is None or (isinstance(value, str) and value.strip().casefold() == "auto"):
        return None
    try:
        scale = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("ui_scale must be 'auto' or a number from 0.75 to 2.0") from exc
    if not 0.75 <= scale <= 2.0:
        raise ConfigurationError("ui_scale must be from 0.75 to 2.0")
    return scale


def _device_config(raw: dict[str, Any]) -> DeviceConfig:
    required = ("id", "display_name", "kind", "plugin")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ConfigurationError(f"Device configuration is missing fields: {', '.join(missing)}")
    try:
        kind = DeviceKind(str(raw["kind"]).lower())
    except ValueError as exc:
        raise ConfigurationError(f"Unknown device kind: {raw['kind']}") from exc

    stability = None
    if kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        stability = StabilityConfig(
            tolerance=float(raw.get("stability_tolerance", 0.01)),
            max_slope_per_minute=float(raw.get("stability_max_slope_per_minute", 0.01)),
            dwell_seconds=float(raw.get("stability_dwell_seconds", 5.0)),
            timeout_seconds=float(raw.get("stability_timeout_seconds", 1800.0)),
            window_seconds=float(raw.get("stability_window_seconds", 5.0)),
            stale_after_seconds=float(raw.get("stale_after_seconds", 3.0)),
        )

    known = {
        "id", "display_name", "kind", "plugin", "unit", "initial_value",
        "default_rate_per_minute", "min_value", "max_value",
        "max_rate_per_minute", "stability_tolerance",
        "stability_max_slope_per_minute", "stability_dwell_seconds",
        "stability_timeout_seconds", "stability_window_seconds",
        "stale_after_seconds", "channels",
    }
    device = DeviceConfig(
        id=str(raw["id"]),
        display_name=str(raw["display_name"]),
        kind=kind,
        plugin=str(raw["plugin"]),
        unit=str(raw.get("unit", "")),
        initial_value=float(raw.get("initial_value", 0.0)),
        default_rate_per_minute=float(raw.get("default_rate_per_minute", 1.0)),
        min_value=float(raw.get("min_value", float("-inf"))),
        max_value=float(raw.get("max_value", float("inf"))),
        max_rate_per_minute=float(raw.get("max_rate_per_minute", float("inf"))),
        stability=stability,
        channels=tuple(str(item) for item in raw.get("channels", [])),
        extras={key: value for key, value in raw.items() if key not in known},
    )
    if device.min_value >= device.max_value:
        raise ConfigurationError(f"Device {device.id}: min_value must be less than max_value")
    if device.default_rate_per_minute <= 0 or device.max_rate_per_minute <= 0:
        raise ConfigurationError(f"Device {device.id}: rates must be greater than zero")
    return device


def load_config(path: str | Path) -> AppConfig:
    source = Path(path).resolve()
    with source.open("rb") as handle:
        raw = tomllib.load(handle)

    application = raw.get("application", {})
    logging_raw = raw.get("logging", {})
    alarm_raw = raw.get("alarms", {})
    abort_raw = raw.get("abort", {})
    devices = tuple(_device_config(item) for item in raw.get("devices", []))
    if not devices:
        raise ConfigurationError("Configuration must contain at least one [[devices]] entry")
    ids = [device.id for device in devices]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("Device IDs must be unique")

    return AppConfig(
        source_path=source,
        title=str(application.get("title", "OpenLab Control")),
        ui_scale=_ui_scale(application.get("ui_scale", "auto")),
        ui_refresh_ms=int(application.get("ui_refresh_ms", 200)),
        poll_interval_seconds=float(application.get("poll_interval_seconds", 0.2)),
        simulation_speed=float(application.get("simulation_speed", 1.0)),
        default_sequence=str(application.get("default_sequence", "")),
        language=str(application.get("language", "en_US")),
        logging=LoggingConfig(
            directory=str(logging_raw.get("directory", "runs")),
            data_file_name=str(logging_raw.get("data_file_name", "experiment.dat")),
            event_file_name=str(logging_raw.get("event_file_name", "events.dat")),
            timestamp_epoch=str(logging_raw.get("timestamp_epoch", "labview_1904")),
            sparse_channel_rows=bool(logging_raw.get("sparse_channel_rows", True)),
            flush_every_row=bool(logging_raw.get("flush_every_row", True)),
            allow_external_paths=bool(logging_raw.get("allow_external_paths", False)),
        ),
        alarms=AlarmConfig(
            stability_timeout=_severity(str(alarm_raw.get("stability_timeout", "error")), "stability_timeout"),
            stale_reading=_severity(str(alarm_raw.get("stale_reading", "warning")), "stale_reading"),
            popup_warnings=bool(alarm_raw.get("popup_warnings", True)),
            popup_errors=bool(alarm_raw.get("popup_errors", True)),
        ),
        abort_temperature=str(abort_raw.get("temperature", "hold_current")),
        abort_field=str(abort_raw.get("field", "hold_current")),
        devices=devices,
    )
