"""Discover and validate System Instrument API v4 manifests."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..models import InstrumentKind

if TYPE_CHECKING:
    from ..config import AppConfig


SYSTEM_INSTRUMENT_API_VERSION = "4"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTRYPOINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$"
)
_CONFIG_FIELD_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "choice", "pid_file"}
)
_PANEL_TEMPLATES = frozenset(
    {"controller", "readout", "readout_grid", "switch"}
)
_CONTROLLER_FIELDS = frozenset(
    {
        "id",
        "label",
        "template",
        "control",
        "reading_options",
        "default_reading",
        "min_value",
        "max_value",
        "default_rate_per_minute",
        "max_rate_per_minute",
        "stability_tolerance",
        "stability_max_slope_per_minute",
        "stability_dwell_seconds",
        "stability_timeout_seconds",
        "stability_window_seconds",
    }
)


@dataclass(frozen=True, slots=True)
class InstrumentReadingDescriptor:
    key: str
    label: str
    unit: str = ""
    decimals: int | None = None


@dataclass(frozen=True, slots=True)
class InstrumentSequenceCommandDescriptor:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class InstrumentConfigFieldDescriptor:
    id: str
    label: str
    field_type: str
    default: object
    options: tuple[str, ...] = ()
    minimum: float | int | None = None
    maximum: float | int | None = None


@dataclass(frozen=True, slots=True)
class InstrumentControlDescriptor:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class InstrumentPanelDescriptor:
    id: str
    label: str
    template: str
    control: str = ""
    reading_options: tuple[str, ...] = ()
    default_reading: str = ""
    readings: tuple[str, ...] = ()
    reading: str = ""
    commands: tuple[str, ...] = ()
    min_value: float = float("-inf")
    max_value: float = float("inf")
    default_rate_per_minute: float = 1.0
    max_rate_per_minute: float = float("inf")
    stability_tolerance: float = 0.0
    stability_max_slope_per_minute: float = 0.0
    stability_dwell_seconds: float = 0.0
    stability_timeout_seconds: float = 1800.0
    stability_window_seconds: float = 5.0


@dataclass(slots=True)
class SystemInstrumentDescriptor:
    id: str
    name: str
    version: str
    path: Path
    api_version: str = ""
    core_requires: str = ""
    backend: str = ""
    kinds: tuple[InstrumentKind, ...] = ()
    identity_pattern: str = ""
    config_fields: tuple[InstrumentConfigFieldDescriptor, ...] = ()
    controls: tuple[InstrumentControlDescriptor, ...] = ()
    panels: tuple[InstrumentPanelDescriptor, ...] = ()
    readings: tuple[InstrumentReadingDescriptor, ...] = ()
    sequence_commands: tuple[InstrumentSequenceCommandDescriptor, ...] = ()
    valid: bool = True
    error: str = ""

    @property
    def can_load(self) -> bool:
        return self.valid

    @property
    def auxiliary_readings(self) -> tuple[str, ...]:
        return tuple(reading.key for reading in self.readings)

    def reading(self, key: str) -> InstrumentReadingDescriptor:
        for reading in self.readings:
            if reading.key == key:
                return reading
        raise KeyError(key)

    def panel(self, panel_id: str) -> InstrumentPanelDescriptor:
        for panel in self.panels:
            if panel.id == panel_id:
                return panel
        raise KeyError(panel_id)

    def control(self, control_id: str) -> InstrumentControlDescriptor:
        for control in self.controls:
            if control.id == control_id:
                return control
        raise KeyError(control_id)

    def config_field(self, field_id: str) -> InstrumentConfigFieldDescriptor:
        for config_field in self.config_fields:
            if config_field.id == field_id:
                return config_field
        raise KeyError(field_id)


def _invalid(path: Path, message: str) -> SystemInstrumentDescriptor:
    return SystemInstrumentDescriptor(
        id=path.name.casefold().replace("-", "_"),
        name=f"{path.name} (Invalid)",
        version="—",
        path=path.resolve(),
        valid=False,
        error=message,
    )


def _array_of_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise TypeError(f"{label} must be an array of strings")
    return tuple(item.strip() for item in value)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _parse_config_fields(
    raw_fields: object,
) -> tuple[InstrumentConfigFieldDescriptor, ...]:
    if not isinstance(raw_fields, list):
        raise TypeError("config_fields must be an array of tables")
    fields: list[InstrumentConfigFieldDescriptor] = []
    for index, raw in enumerate(raw_fields, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"config_fields entry {index} must be a table")
        unknown = sorted(
            set(raw) - {"id", "label", "type", "default", "options", "min", "max"}
        )
        if unknown:
            raise ValueError(
                f"unknown config_fields entry {index} fields: "
                + ", ".join(unknown)
            )
        field_type = str(raw["type"]).strip()
        options = _array_of_strings(
            raw.get("options", []),
            f"config_fields entry {index} options",
        )
        minimum = raw.get("min")
        maximum = raw.get("max")
        if field_type in {"integer", "number"}:
            if minimum is not None:
                minimum = _finite_number(
                    minimum,
                    f"config_fields entry {index} min",
                )
            if maximum is not None:
                maximum = _finite_number(
                    maximum,
                    f"config_fields entry {index} max",
                )
        elif minimum is not None or maximum is not None:
            raise ValueError(
                f"config_fields entry {index} min/max require a numeric type"
            )
        fields.append(
            InstrumentConfigFieldDescriptor(
                id=str(raw["id"]).strip(),
                label=str(raw["label"]).strip(),
                field_type=field_type,
                default=raw["default"],
                options=options,
                minimum=minimum,
                maximum=maximum,
            )
        )
    return tuple(fields)


def _parse_controls(
    raw_controls: object,
) -> tuple[InstrumentControlDescriptor, ...]:
    if not isinstance(raw_controls, list):
        raise TypeError("controls must be an array of tables")
    controls: list[InstrumentControlDescriptor] = []
    for index, raw in enumerate(raw_controls, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"controls entry {index} must be a table")
        unknown = sorted(set(raw) - {"id", "label"})
        if unknown:
            raise ValueError(
                f"unknown controls entry {index} fields: " + ", ".join(unknown)
            )
        controls.append(
            InstrumentControlDescriptor(
                id=str(raw["id"]).strip(),
                label=str(raw["label"]).strip(),
            )
        )
    return tuple(controls)


def _parse_panels(raw_panels: object) -> tuple[InstrumentPanelDescriptor, ...]:
    if not isinstance(raw_panels, list):
        raise TypeError("panels must be an array of tables")
    panels: list[InstrumentPanelDescriptor] = []
    for index, raw in enumerate(raw_panels, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"panels entry {index} must be a table")
        template = str(raw["template"]).strip()
        common = {"id", "label", "template"}
        if template == "controller":
            unknown = sorted(set(raw) - _CONTROLLER_FIELDS)
        elif template in {"readout", "readout_grid"}:
            unknown = sorted(set(raw) - common - {"readings"})
        elif template == "switch":
            unknown = sorted(set(raw) - common - {"reading", "commands"})
        else:
            unknown = []
        if unknown:
            raise ValueError(
                f"unknown panels entry {index} fields: " + ", ".join(unknown)
            )
        values: dict[str, Any] = {
            "id": str(raw["id"]).strip(),
            "label": str(raw["label"]).strip(),
            "template": template,
        }
        if template == "controller":
            values.update(
                control=str(raw["control"]).strip(),
                reading_options=_array_of_strings(
                    raw["reading_options"],
                    f"panels entry {index} reading_options",
                ),
                default_reading=str(raw["default_reading"]).strip(),
                min_value=_finite_number(
                    raw["min_value"],
                    f"panels entry {index} min_value",
                ),
                max_value=_finite_number(
                    raw["max_value"],
                    f"panels entry {index} max_value",
                ),
                default_rate_per_minute=_finite_number(
                    raw["default_rate_per_minute"],
                    f"panels entry {index} default_rate_per_minute",
                ),
                max_rate_per_minute=_finite_number(
                    raw["max_rate_per_minute"],
                    f"panels entry {index} max_rate_per_minute",
                ),
                stability_tolerance=_finite_number(
                    raw["stability_tolerance"],
                    f"panels entry {index} stability_tolerance",
                ),
                stability_max_slope_per_minute=_finite_number(
                    raw["stability_max_slope_per_minute"],
                    f"panels entry {index} stability_max_slope_per_minute",
                ),
                stability_dwell_seconds=_finite_number(
                    raw["stability_dwell_seconds"],
                    f"panels entry {index} stability_dwell_seconds",
                ),
                stability_timeout_seconds=_finite_number(
                    raw["stability_timeout_seconds"],
                    f"panels entry {index} stability_timeout_seconds",
                ),
                stability_window_seconds=_finite_number(
                    raw["stability_window_seconds"],
                    f"panels entry {index} stability_window_seconds",
                ),
            )
        elif template in {"readout", "readout_grid"}:
            values["readings"] = _array_of_strings(
                raw["readings"],
                f"panels entry {index} readings",
            )
        elif template == "switch":
            values.update(
                reading=str(raw["reading"]).strip(),
                commands=_array_of_strings(
                    raw["commands"],
                    f"panels entry {index} commands",
                ),
            )
        panels.append(InstrumentPanelDescriptor(**values))
    return tuple(panels)


def _parse_readings(
    raw_readings: object,
) -> tuple[InstrumentReadingDescriptor, ...]:
    if not isinstance(raw_readings, dict):
        raise TypeError("readings must be a table")
    readings: list[InstrumentReadingDescriptor] = []
    for raw_key, raw in raw_readings.items():
        if not isinstance(raw, dict):
            raise TypeError(f"readings.{raw_key} must be a table")
        unknown = sorted(set(raw) - {"decimals", "label", "unit"})
        if unknown:
            raise ValueError(
                f"unknown readings.{raw_key} fields: " + ", ".join(unknown)
            )
        readings.append(
            InstrumentReadingDescriptor(
                key=str(raw_key).strip(),
                label=str(raw["label"]).strip(),
                unit=str(raw.get("unit", "")).strip(),
                decimals=raw.get("decimals"),
            )
        )
    return tuple(readings)


def _parse_sequence_commands(
    raw_commands: object,
) -> tuple[InstrumentSequenceCommandDescriptor, ...]:
    if not isinstance(raw_commands, list):
        raise TypeError("sequence_commands must be an array of tables")
    commands: list[InstrumentSequenceCommandDescriptor] = []
    for index, raw in enumerate(raw_commands, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"sequence_commands entry {index} must be a table")
        unknown = sorted(set(raw) - {"id", "label"})
        if unknown:
            raise ValueError(
                f"unknown sequence_commands entry {index} fields: "
                + ", ".join(unknown)
            )
        commands.append(
            InstrumentSequenceCommandDescriptor(
                id=str(raw["id"]).strip(),
                label=str(raw["label"]).strip(),
            )
        )
    return tuple(commands)


def load_instrument_manifest(
    path: Path,
    *,
    raw_document: dict[str, Any] | None = None,
    panels: tuple[InstrumentPanelDescriptor, ...] | None = None,
) -> SystemInstrumentDescriptor:
    manifest_path = path / "instrument.toml"
    try:
        if raw_document is None:
            with manifest_path.open("rb") as handle:
                raw = tomllib.load(handle)
        else:
            raw = dict(raw_document)
            raw.pop("instances", None)
        unknown_fields = sorted(
            set(raw)
            - {
                "api_version",
                "backend",
                "config_fields",
                "controls",
                "core_requires",
                "discovery",
                "id",
                "kinds",
                "name",
                "panels",
                "readings",
                "sequence_commands",
                "version",
            }
        )
        if unknown_fields:
            raise ValueError(
                "unknown instrument.toml fields: " + ", ".join(unknown_fields)
            )
        discovery = raw.get("discovery", {})
        if not isinstance(discovery, dict):
            raise TypeError("discovery must be a table")
        unknown_discovery = sorted(set(discovery) - {"identity_pattern"})
        if unknown_discovery:
            raise ValueError(
                "unknown discovery fields: " + ", ".join(unknown_discovery)
            )
        descriptor = SystemInstrumentDescriptor(
            id=str(raw["id"]).strip(),
            name=str(raw["name"]).strip(),
            version=str(raw["version"]).strip(),
            path=path.resolve(),
            api_version=str(raw["api_version"]).strip(),
            core_requires=str(raw.get("core_requires", "")).strip(),
            backend=str(raw["backend"]).strip(),
            kinds=tuple(
                InstrumentKind(str(item).strip().casefold())
                for item in raw["kinds"]
            ),
            identity_pattern=str(
                discovery.get("identity_pattern", "")
            ).strip(),
            config_fields=_parse_config_fields(raw.get("config_fields", [])),
            controls=_parse_controls(raw.get("controls", [])),
            panels=_parse_panels(raw["panels"]) if panels is None else panels,
            readings=_parse_readings(raw["readings"]),
            sequence_commands=_parse_sequence_commands(
                raw.get("sequence_commands", [])
            ),
        )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as exc:
        return _invalid(path, f"Cannot read instrument.toml: {exc}")

    errors: list[str] = []
    if not _IDENTIFIER.fullmatch(descriptor.id):
        errors.append("id must match [a-z][a-z0-9_]*")
    if not descriptor.name:
        errors.append("name must not be empty")
    try:
        Version(descriptor.version)
    except InvalidVersion:
        errors.append(f"version {descriptor.version!r} is invalid")
    if descriptor.api_version != SYSTEM_INSTRUMENT_API_VERSION:
        errors.append(
            f"API {descriptor.api_version!r} is incompatible with "
            f"{SYSTEM_INSTRUMENT_API_VERSION}"
        )
    if descriptor.core_requires:
        try:
            compatible = Version(__version__) in SpecifierSet(
                descriptor.core_requires
            )
        except (InvalidSpecifier, InvalidVersion):
            errors.append(
                f"core_requires {descriptor.core_requires!r} is invalid"
            )
        else:
            if not compatible:
                errors.append(
                    f"OpenLab Control {__version__} does not satisfy "
                    f"{descriptor.core_requires}"
                )
    if not _ENTRYPOINT.fullmatch(descriptor.backend):
        errors.append("backend must use module:ClassName without a path")
    else:
        module_name = descriptor.backend.split(":", 1)[0]
        if not (path / f"{module_name}.py").is_file():
            errors.append(f"backend source does not exist: {module_name}.py")
    if not descriptor.kinds:
        errors.append("at least one supported instrument kind is required")
    if len(descriptor.kinds) != len(set(descriptor.kinds)):
        errors.append("supported instrument kinds must be unique")
    if descriptor.identity_pattern:
        if len(descriptor.identity_pattern) > 256:
            errors.append("discovery.identity_pattern is too long")
        else:
            try:
                re.compile(descriptor.identity_pattern)
            except re.error as exc:
                errors.append(
                    f"discovery.identity_pattern is invalid: {exc}"
                )

    if not descriptor.readings:
        errors.append("at least one reading is required")
    reading_ids = {reading.key for reading in descriptor.readings}
    if len(reading_ids) != len(descriptor.readings):
        errors.append("reading ids must be unique")
    for reading in descriptor.readings:
        if not _IDENTIFIER.fullmatch(reading.key):
            errors.append("reading ids must match [a-z][a-z0-9_]*")
        if (
            not reading.label
            or len(reading.label) > 80
            or any(not character.isprintable() for character in reading.label)
        ):
            errors.append("reading labels must contain 1-80 characters")
        if len(reading.unit) > 24 or any(
            not character.isprintable() for character in reading.unit
        ):
            errors.append("reading units must contain at most 24 characters")
        if reading.decimals is not None and (
            isinstance(reading.decimals, bool)
            or not isinstance(reading.decimals, int)
            or not 0 <= reading.decimals <= 12
        ):
            errors.append("reading decimals must be an integer from 0 to 12")

    config_field_ids = [field.id for field in descriptor.config_fields]
    if len(config_field_ids) != len(set(config_field_ids)):
        errors.append("config_field ids must be unique")
    for field in descriptor.config_fields:
        if not _IDENTIFIER.fullmatch(field.id):
            errors.append("config_field ids must match [a-z][a-z0-9_]*")
        if not field.label:
            errors.append("config_field labels must not be empty")
        if field.field_type not in _CONFIG_FIELD_TYPES:
            errors.append(
                f"config_field {field.id} has unknown type {field.field_type!r}"
            )
            continue
        default = field.default
        if field.field_type in {"string", "pid_file"}:
            valid_default = isinstance(default, str)
        elif field.field_type == "integer":
            valid_default = isinstance(default, int) and not isinstance(default, bool)
        elif field.field_type == "number":
            valid_default = (
                isinstance(default, (int, float))
                and not isinstance(default, bool)
                and math.isfinite(float(default))
            )
        elif field.field_type == "boolean":
            valid_default = isinstance(default, bool)
        else:
            valid_default = (
                isinstance(default, str)
                and bool(field.options)
                and default in field.options
            )
        if not valid_default:
            errors.append(
                f"config_field {field.id} default does not match "
                f"type {field.field_type}"
            )
        if field.field_type != "choice" and field.options:
            errors.append(
                f"config_field {field.id} options require choice type"
            )
        if field.field_type == "choice" and (
            not field.options or len(field.options) != len(set(field.options))
        ):
            errors.append(
                f"config_field {field.id} choice options must be non-empty and unique"
            )
        if field.minimum is not None and field.maximum is not None:
            if field.minimum > field.maximum:
                errors.append(
                    f"config_field {field.id} min must not exceed max"
                )
        if field.field_type in {"integer", "number"} and valid_default:
            numeric_default = float(default)
            if (
                field.minimum is not None
                and numeric_default < float(field.minimum)
            ) or (
                field.maximum is not None
                and numeric_default > float(field.maximum)
            ):
                errors.append(
                    f"config_field {field.id} default is outside min/max"
                )

    control_ids = [control.id for control in descriptor.controls]
    if len(control_ids) != len(set(control_ids)):
        errors.append("control ids must be unique")
    for control in descriptor.controls:
        if not _IDENTIFIER.fullmatch(control.id):
            errors.append("control ids must match [a-z][a-z0-9_]*")
        if not control.label:
            errors.append("control labels must not be empty")

    command_ids = {command.id for command in descriptor.sequence_commands}
    if len(command_ids) != len(descriptor.sequence_commands):
        errors.append("sequence command ids must be unique")
    for command in descriptor.sequence_commands:
        if not _IDENTIFIER.fullmatch(command.id):
            errors.append(
                "sequence command ids must match [a-z][a-z0-9_]*"
            )
        if not command.label:
            errors.append("sequence command labels must not be empty")

    if not descriptor.panels:
        errors.append("at least one panel is required")
    panel_ids = [panel.id for panel in descriptor.panels]
    if len(panel_ids) != len(set(panel_ids)):
        errors.append("panel ids must be unique")
    for panel in descriptor.panels:
        if not _IDENTIFIER.fullmatch(panel.id):
            errors.append("panel ids must match [a-z][a-z0-9_]*")
        if not panel.label:
            errors.append("panel labels must not be empty")
        if panel.template not in _PANEL_TEMPLATES:
            errors.append(
                f"panel {panel.id} has unknown template {panel.template!r}"
            )
            continue
        if panel.template == "controller":
            if panel.control not in control_ids:
                errors.append(
                    f"panel {panel.id} references unknown control "
                    f"{panel.control!r}"
                )
            if (
                not panel.reading_options
                or len(panel.reading_options)
                != len(set(panel.reading_options))
                or any(key not in reading_ids for key in panel.reading_options)
            ):
                errors.append(
                    f"panel {panel.id} reading_options must contain unique "
                    "declared readings"
                )
            if panel.default_reading not in panel.reading_options:
                errors.append(
                    f"panel {panel.id} default_reading must be in reading_options"
                )
            if panel.min_value >= panel.max_value:
                errors.append(
                    f"panel {panel.id} min_value must be less than max_value"
                )
            if (
                panel.default_rate_per_minute <= 0
                or panel.max_rate_per_minute <= 0
                or panel.default_rate_per_minute
                > panel.max_rate_per_minute
            ):
                errors.append(
                    f"panel {panel.id} rates must be positive and ordered"
                )
            if (
                panel.stability_tolerance < 0
                or panel.stability_max_slope_per_minute < 0
                or panel.stability_dwell_seconds < 0
                or panel.stability_timeout_seconds <= 0
                or panel.stability_window_seconds <= 0
            ):
                errors.append(
                    f"panel {panel.id} stability values are invalid"
                )
        elif panel.template == "readout":
            if len(panel.readings) != 1 or panel.readings[0] not in reading_ids:
                errors.append(
                    f"panel {panel.id} readout requires one declared reading"
                )
        elif panel.template == "readout_grid":
            if (
                not 1 <= len(panel.readings) <= 4
                or len(panel.readings) != len(set(panel.readings))
                or any(key not in reading_ids for key in panel.readings)
            ):
                errors.append(
                    f"panel {panel.id} readout_grid requires one to four "
                    "unique declared readings"
                )
        elif (
            panel.reading not in reading_ids
            or not panel.commands
            or len(panel.commands) != len(set(panel.commands))
            or any(command not in command_ids for command in panel.commands)
        ):
            errors.append(
                f"panel {panel.id} switch must reference one reading and "
                "declared commands"
            )
    if errors:
        descriptor.valid = False
        descriptor.error = "; ".join(errors)
    return descriptor


def discover_system_instruments(
    config: "AppConfig",
) -> tuple[SystemInstrumentDescriptor, ...]:
    root = config.resolve_project_path(config.system_instruments.directory)
    if not root.exists():
        return ()
    if not root.is_dir():
        raise ValueError(f"System Instrument directory is not a directory: {root}")
    descriptors = [
        load_instrument_manifest(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir() and (path / "instrument.toml").is_file()
    ]
    seen: dict[str, SystemInstrumentDescriptor] = {}
    for descriptor in descriptors:
        duplicate = seen.get(descriptor.id)
        if duplicate is None:
            seen[descriptor.id] = descriptor
            continue
        message = f"Duplicate system instrument id: {descriptor.id}"
        descriptor.valid = False
        descriptor.error = "; ".join(
            item for item in (descriptor.error, message) if item
        )
        duplicate.valid = False
        duplicate.error = "; ".join(
            item for item in (duplicate.error, message) if item
        )
    return tuple(descriptors)


def configured_system_instruments(
    config: "AppConfig",
    descriptors: tuple[SystemInstrumentDescriptor, ...],
) -> tuple[SystemInstrumentDescriptor, ...]:
    by_id = {descriptor.id: descriptor for descriptor in descriptors}
    selected: list[SystemInstrumentDescriptor] = []
    seen: set[str] = set()
    for instance in config.instrument_instances:
        if ":" in instance.backend:
            continue
        descriptor = by_id.get(instance.backend)
        if descriptor is None:
            raise ValueError(
                f"Instrument {instance.id} selects unknown System Instrument "
                f"{instance.backend!r}"
            )
        if not descriptor.can_load:
            raise ValueError(
                f"System Instrument {descriptor.id} is invalid: "
                f"{descriptor.error}"
            )
        if descriptor.id not in seen:
            selected.append(descriptor)
            seen.add(descriptor.id)
    return tuple(selected)
