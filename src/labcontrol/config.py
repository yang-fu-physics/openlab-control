from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import DeviceKind, Severity


class ConfigurationError(ValueError):
    pass


_WINDOWS_RESERVED_FILE_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


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
    stale_after_seconds: float = 3.0
    operation_timeout_seconds: float = 10.0
    shutdown_timeout_seconds: float = 3.0
    stability: StabilityConfig | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    directory: str = "runs"
    data_file_name: str = "experiment.dat"
    event_file_name: str = "events.dat"
    timestamp_epoch: str = "labview_1904"
    flush_every_row: bool = True
    allow_external_paths: bool = False


@dataclass(frozen=True, slots=True)
class AlarmConfig:
    stability_timeout: Severity = Severity.ERROR
    stale_reading: Severity = Severity.WARNING
    popup_warnings: bool = True
    popup_errors: bool = True


@dataclass(frozen=True, slots=True)
class ModuleConfig:
    directory: str = "modules"
    data_directory: str = "module_data"
    shared_wheels_directory: str = "wheels"
    python_executable: str = ""
    site_packages_directory: str = "module_runtime/site-packages"
    startup_timeout_seconds: float = 10.0
    operation_timeout_seconds: float = 120.0
    shutdown_timeout_seconds: float = 3.0


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
    modules: ModuleConfig
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


def _finite_float(value: object, key: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be a finite number") from exc
    if not math.isfinite(result):
        raise ConfigurationError(f"{key} must be a finite number")
    return result


def _positive_float(value: object, key: str) -> float:
    result = _finite_float(value, key)
    if result <= 0:
        raise ConfigurationError(f"{key} must be greater than zero")
    return result


def _positive_int(value: object, key: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be a positive integer") from exc
    if result <= 0:
        raise ConfigurationError(f"{key} must be a positive integer")
    return result


def _windows_file_name(value: object, key: str) -> str:
    result = str(value)
    path = Path(result)
    invalid_characters = '<>:"/\\|?*'
    stem = result.split(".", 1)[0].upper()
    if (
        not result
        or result != result.strip()
        or result.rstrip(" .") != result
        or path.is_absolute()
        or path.name != result
        or stem in _WINDOWS_RESERVED_FILE_STEMS
        or any(character in invalid_characters or ord(character) < 32 for character in result)
    ):
        raise ConfigurationError(
            f"{key} must be a plain Windows file name without a directory"
        )
    return result


def _device_config(raw: dict[str, Any]) -> DeviceConfig:
    required = ("id", "display_name", "kind", "plugin")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ConfigurationError(f"Device configuration is missing fields: {', '.join(missing)}")
    try:
        kind = DeviceKind(str(raw["kind"]).lower())
    except ValueError as exc:
        raise ConfigurationError(f"Unknown device kind: {raw['kind']}") from exc

    device_id = str(raw["id"])
    prefix = f"Device {device_id}"
    initial_value = _finite_float(
        raw.get("initial_value", 0.0),
        f"{prefix} initial_value",
    )
    default_rate = _finite_float(
        raw.get("default_rate_per_minute", 1.0),
        f"{prefix} default_rate_per_minute",
    )
    if kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        min_value = _finite_float(
            raw.get("min_value", float("-inf")),
            f"{prefix} min_value",
        )
        max_value = _finite_float(
            raw.get("max_value", float("inf")),
            f"{prefix} max_value",
        )
        max_rate = _finite_float(
            raw.get("max_rate_per_minute", float("inf")),
            f"{prefix} max_rate_per_minute",
        )
    else:
        min_value = float(raw.get("min_value", float("-inf")))
        max_value = float(raw.get("max_value", float("inf")))
        max_rate = float(raw.get("max_rate_per_minute", float("inf")))
    stale_after = _positive_float(
        raw.get("stale_after_seconds", 3.0),
        f"{prefix} stale_after_seconds",
    )
    operation_timeout = _positive_float(
        raw.get("operation_timeout_seconds", 10.0),
        f"{prefix} operation_timeout_seconds",
    )
    shutdown_timeout = _positive_float(
        raw.get("shutdown_timeout_seconds", 3.0),
        f"{prefix} shutdown_timeout_seconds",
    )

    stability = None
    if kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        tolerance = _finite_float(
            raw.get("stability_tolerance", 0.01),
            f"{prefix} stability_tolerance",
        )
        maximum_slope = _finite_float(
            raw.get("stability_max_slope_per_minute", 0.01),
            f"{prefix} stability_max_slope_per_minute",
        )
        dwell = _finite_float(
            raw.get("stability_dwell_seconds", 5.0),
            f"{prefix} stability_dwell_seconds",
        )
        if tolerance < 0 or maximum_slope < 0 or dwell < 0:
            raise ConfigurationError(
                f"{prefix} stability tolerance, slope, and dwell must not be negative"
            )
        stability = StabilityConfig(
            tolerance=tolerance,
            max_slope_per_minute=maximum_slope,
            dwell_seconds=dwell,
            timeout_seconds=_positive_float(
                raw.get("stability_timeout_seconds", 1800.0),
                f"{prefix} stability_timeout_seconds",
            ),
            window_seconds=_positive_float(
                raw.get("stability_window_seconds", 5.0),
                f"{prefix} stability_window_seconds",
            ),
            stale_after_seconds=stale_after,
        )

    known = {
        "id", "display_name", "kind", "plugin", "unit", "initial_value",
        "default_rate_per_minute", "min_value", "max_value",
        "max_rate_per_minute", "stability_tolerance",
        "stability_max_slope_per_minute", "stability_dwell_seconds",
        "stability_timeout_seconds", "stability_window_seconds",
        "stale_after_seconds", "operation_timeout_seconds",
        "shutdown_timeout_seconds",
    }
    device = DeviceConfig(
        id=device_id,
        display_name=str(raw["display_name"]),
        kind=kind,
        plugin=str(raw["plugin"]),
        unit=str(raw.get("unit", "")),
        initial_value=initial_value,
        default_rate_per_minute=default_rate,
        min_value=min_value,
        max_value=max_value,
        max_rate_per_minute=max_rate,
        stale_after_seconds=stale_after,
        operation_timeout_seconds=operation_timeout,
        shutdown_timeout_seconds=shutdown_timeout,
        stability=stability,
        extras={key: value for key, value in raw.items() if key not in known},
    )
    if kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        if device.min_value >= device.max_value:
            raise ConfigurationError(f"Device {device.id}: min_value must be less than max_value")
        if device.default_rate_per_minute <= 0 or device.max_rate_per_minute <= 0:
            raise ConfigurationError(f"Device {device.id}: rates must be greater than zero")
        if device.default_rate_per_minute > device.max_rate_per_minute:
            raise ConfigurationError(
                f"Device {device.id}: default rate must not exceed max_rate_per_minute"
            )
        if not device.min_value <= device.initial_value <= device.max_value:
            raise ConfigurationError(
                f"Device {device.id}: initial_value must be within min_value and max_value"
            )
    return device


def load_config(path: str | Path) -> AppConfig:
    source = Path(path).resolve()
    with source.open("rb") as handle:
        raw = tomllib.load(handle)

    application = raw.get("application", {})
    logging_raw = raw.get("logging", {})
    alarm_raw = raw.get("alarms", {})
    abort_raw = raw.get("abort", {})
    module_raw = raw.get("modules", {})
    devices = tuple(_device_config(item) for item in raw.get("devices", []))
    if not devices:
        raise ConfigurationError("Configuration must contain at least one [[devices]] entry")
    ids = [device.id for device in devices]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("Device IDs must be unique")

    ui_refresh_ms = _positive_int(
        application.get("ui_refresh_ms", 200),
        "application.ui_refresh_ms",
    )
    poll_interval_seconds = _positive_float(
        application.get("poll_interval_seconds", 0.2),
        "application.poll_interval_seconds",
    )
    simulation_speed = _positive_float(
        application.get("simulation_speed", 1.0),
        "application.simulation_speed",
    )
    timestamp_epoch = str(
        logging_raw.get("timestamp_epoch", "labview_1904")
    ).strip().casefold()
    if timestamp_epoch not in {"labview_1904", "unix"}:
        raise ConfigurationError(
            "logging.timestamp_epoch must be labview_1904 or unix"
        )
    abort_temperature = str(
        abort_raw.get("temperature", "hold_current")
    ).strip().casefold()
    abort_field = str(
        abort_raw.get("field", "hold_current")
    ).strip().casefold()
    if abort_temperature != "hold_current" or abort_field != "hold_current":
        raise ConfigurationError(
            "abort.temperature and abort.field currently support only hold_current"
        )
    data_file_name = _windows_file_name(
        logging_raw.get("data_file_name", "experiment.dat"),
        "logging.data_file_name",
    )
    event_file_name = _windows_file_name(
        logging_raw.get("event_file_name", "events.dat"),
        "logging.event_file_name",
    )
    if data_file_name.casefold() == event_file_name.casefold():
        raise ConfigurationError(
            "logging.data_file_name and logging.event_file_name must be different"
        )

    return AppConfig(
        source_path=source,
        title=str(application.get("title", "OpenLab Control")),
        ui_scale=_ui_scale(application.get("ui_scale", "auto")),
        ui_refresh_ms=ui_refresh_ms,
        poll_interval_seconds=poll_interval_seconds,
        simulation_speed=simulation_speed,
        default_sequence=str(application.get("default_sequence", "")),
        language=str(application.get("language", "en_US")),
        logging=LoggingConfig(
            directory=str(logging_raw.get("directory", "runs")),
            data_file_name=data_file_name,
            event_file_name=event_file_name,
            timestamp_epoch=timestamp_epoch,
            flush_every_row=bool(logging_raw.get("flush_every_row", True)),
            allow_external_paths=bool(logging_raw.get("allow_external_paths", False)),
        ),
        alarms=AlarmConfig(
            stability_timeout=_severity(str(alarm_raw.get("stability_timeout", "error")), "stability_timeout"),
            stale_reading=_severity(str(alarm_raw.get("stale_reading", "warning")), "stale_reading"),
            popup_warnings=bool(alarm_raw.get("popup_warnings", True)),
            popup_errors=bool(alarm_raw.get("popup_errors", True)),
        ),
        modules=ModuleConfig(
            directory=str(module_raw.get("directory", "modules")),
            data_directory=str(module_raw.get("data_directory", "module_data")),
            shared_wheels_directory=str(module_raw.get("shared_wheels_directory", "wheels")),
            python_executable=str(module_raw.get("python_executable", "")),
            site_packages_directory=str(
                module_raw.get("site_packages_directory", "module_runtime/site-packages")
            ),
            startup_timeout_seconds=_positive_float(
                module_raw.get("startup_timeout_seconds", 10.0),
                "modules.startup_timeout_seconds",
            ),
            operation_timeout_seconds=_positive_float(
                module_raw.get("operation_timeout_seconds", 120.0),
                "modules.operation_timeout_seconds",
            ),
            shutdown_timeout_seconds=_positive_float(
                module_raw.get("shutdown_timeout_seconds", 3.0),
                "modules.shutdown_timeout_seconds",
            ),
        ),
        abort_temperature=abort_temperature,
        abort_field=abort_field,
        devices=devices,
    )
