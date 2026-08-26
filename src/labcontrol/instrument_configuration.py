"""Load scanner-generated System Instrument instance configurations."""

from __future__ import annotations

import math
import re
import tomllib
from pathlib import Path
from typing import Any

from .config import (
    ConfigurationError,
    InstrumentCommandConfig,
    InstrumentConfig,
    InstrumentPanelConfig,
    InstrumentReadingConfig,
    StabilityConfig,
)
from .instrument_resources import InstrumentResource
from .models import InstrumentKind


_SIMULATED_TEMPLATES = {
    "simulated_temperature": (
        "labcontrol.instruments.simulated:SimulatedTemperatureController",
        InstrumentKind.TEMPERATURE,
    ),
    "simulated_field": (
        "labcontrol.instruments.simulated:SimulatedFieldController",
        InstrumentKind.FIELD,
    ),
    "simulated_second_stage": (
        "labcontrol.instruments.simulated:SimulatedReadOnlyMonitor",
        InstrumentKind.MONITOR,
    ),
}
_DOCUMENT_FIELDS = {
    "id",
    "name",
    "version",
    "api_version",
    "core_requires",
    "backend",
    "kinds",
    "config_fields",
    "controls",
    "panels",
    "discovery",
    "readings",
    "sequence_commands",
    "instances",
}
_CONTROLLER_FIELDS = {
    "id",
    "enabled",
    "order",
    "role",
    "reading",
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
_ROLES = frozenset({"none", "sample_temp", "field"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _identifier(value: object, label: str) -> str:
    result = str(value)
    if not _IDENTIFIER.fullmatch(result):
        raise ConfigurationError(
            f"{label} must match [a-z][a-z0-9_]*"
        )
    return result


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ConfigurationError(f"{label} must be a finite number")
    return result


def _positive(value: object, label: str) -> float:
    result = _finite(value, label)
    if result <= 0:
        raise ConfigurationError(f"{label} must be greater than zero")
    return result


def _nonnegative(value: object, label: str) -> float:
    result = _finite(value, label)
    if result < 0:
        raise ConfigurationError(
            f"{label} must be greater than or equal to zero"
        )
    return result


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{label} must be true or false")
    return value


def _config_field_value(
    field: Any,
    value: object,
    project_root: Path,
    label: str,
) -> object:
    field_type = field.field_type
    if field_type in {"string", "pid_file", "choice"}:
        if not isinstance(value, str):
            raise ConfigurationError(f"{label} must be text")
        checked: object = value
    elif field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(f"{label} must be an integer")
        checked = value
    elif field_type == "number":
        checked = _finite(value, label)
    elif field_type == "boolean":
        checked = _boolean(value, label)
    else:
        raise ConfigurationError(
            f"{label} has unsupported config field type {field_type!r}"
        )

    if field_type == "string":
        checked = str(checked).strip()
        if not checked:
            raise ConfigurationError(f"{label} must not be empty")
    if field_type == "choice" and checked not in field.options:
        raise ConfigurationError(
            f"{label} must be one of: " + ", ".join(field.options)
        )
    if field_type in {"integer", "number"}:
        numeric = float(checked)
        if field.minimum is not None and numeric < float(field.minimum):
            raise ConfigurationError(
                f"{label} must be at least {field.minimum:g}"
            )
        if field.maximum is not None and numeric > float(field.maximum):
            raise ConfigurationError(
                f"{label} must be at most {field.maximum:g}"
            )
    if field_type == "pid_file":
        configured = Path(str(checked))
        resolved = (
            configured.resolve()
            if configured.is_absolute()
            else (project_root / configured).resolve()
        )
        if not resolved.is_relative_to(project_root):
            raise ConfigurationError(
                f"{label} must stay inside the project root"
            )
        if not resolved.is_file():
            raise ConfigurationError(
                f"{label} does not exist or is not a file: {resolved}"
            )
        checked = str(resolved)
    return checked


def _embedded_simulation_descriptor(raw: dict[str, Any], path: Path) -> Any:
    from .instruments.manifest import (
        InstrumentControlDescriptor,
        InstrumentPanelDescriptor,
        InstrumentReadingDescriptor,
        SystemInstrumentDescriptor,
    )

    template_id = str(raw.get("id", "")).strip()
    expected = _SIMULATED_TEMPLATES.get(template_id)
    if expected is None or path.stem != template_id:
        raise ConfigurationError(
            f"{path} is not a built-in simulation configuration"
        )
    backend, kind = expected
    if (
        raw.get("api_version") != "4"
        or raw.get("backend") != backend
        or raw.get("kinds") != [kind.value]
    ):
        raise ConfigurationError(
            f"{path} has invalid built-in simulation metadata"
        )

    raw_readings = raw.get("readings")
    if not isinstance(raw_readings, dict) or not raw_readings:
        raise ConfigurationError(f"{path} requires [readings] metadata")
    readings = []
    for key, metadata in raw_readings.items():
        if not isinstance(metadata, dict):
            raise ConfigurationError(f"{path} readings.{key} must be a table")
        unknown = sorted(set(metadata) - {"label", "unit", "decimals"})
        if unknown:
            raise ConfigurationError(
                f"{path} readings.{key} has unknown fields: "
                + ", ".join(unknown)
            )
        readings.append(
            InstrumentReadingDescriptor(
                key=str(key),
                label=str(metadata["label"]),
                unit=str(metadata.get("unit", "")),
                decimals=metadata.get("decimals"),
            )
        )

    raw_controls = raw.get("controls", [])
    if not isinstance(raw_controls, list) or any(
        not isinstance(item, dict) for item in raw_controls
    ):
        raise ConfigurationError(f"{path} controls must be an array of tables")
    controls = tuple(
        InstrumentControlDescriptor(
            id=str(item["id"]),
            label=str(item["label"]),
        )
        for item in raw_controls
    )

    raw_panels = raw.get("panels")
    if not isinstance(raw_panels, list) or not raw_panels or any(
        not isinstance(item, dict) for item in raw_panels
    ):
        raise ConfigurationError(f"{path} panels must be a non-empty array")
    panels = []
    for item in raw_panels:
        template = str(item.get("template", ""))
        values: dict[str, Any] = {
            "id": str(item["id"]),
            "label": str(item["label"]),
            "template": template,
        }
        if template == "controller":
            values.update(
                control=str(item["control"]),
                reading_options=tuple(
                    str(value) for value in item["reading_options"]
                ),
                default_reading=str(item["default_reading"]),
                min_value=_finite(item["min_value"], f"{path} min_value"),
                max_value=_finite(item["max_value"], f"{path} max_value"),
                default_rate_per_minute=_finite(
                    item["default_rate_per_minute"],
                    f"{path} default_rate_per_minute",
                ),
                max_rate_per_minute=_finite(
                    item["max_rate_per_minute"],
                    f"{path} max_rate_per_minute",
                ),
                stability_tolerance=_finite(
                    item["stability_tolerance"],
                    f"{path} stability_tolerance",
                ),
                stability_max_slope_per_minute=_finite(
                    item["stability_max_slope_per_minute"],
                    f"{path} stability_max_slope_per_minute",
                ),
                stability_dwell_seconds=_finite(
                    item["stability_dwell_seconds"],
                    f"{path} stability_dwell_seconds",
                ),
                stability_timeout_seconds=_finite(
                    item["stability_timeout_seconds"],
                    f"{path} stability_timeout_seconds",
                ),
                stability_window_seconds=_finite(
                    item["stability_window_seconds"],
                    f"{path} stability_window_seconds",
                ),
            )
        elif template in {"readout", "readout_grid"}:
            values["readings"] = tuple(
                str(value) for value in item["readings"]
            )
        else:
            raise ConfigurationError(
                f"{path} has unsupported simulation panel {template!r}"
            )
        panels.append(InstrumentPanelDescriptor(**values))

    return SystemInstrumentDescriptor(
        id=template_id,
        name=str(raw.get("name", template_id)),
        version=str(raw.get("version", "")),
        path=path.parent,
        api_version="4",
        backend=backend,
        kinds=(kind,),
        controls=controls,
        panels=tuple(panels),
        readings=tuple(readings),
    )


def _configured_panel(
    raw: dict[str, Any],
    template: Any,
    descriptor: Any,
    instrument_id: str,
) -> InstrumentPanelConfig:
    prefix = f"Panel {instrument_id}.{template.id}"
    if str(raw.get("id", "")).strip() != template.id:
        raise ConfigurationError(f"{prefix} has a mismatched panel id")
    if "enabled" not in raw:
        raise ConfigurationError(f"{prefix} requires enabled")
    enabled = _boolean(raw["enabled"], f"{prefix} enabled")
    allowed = (
        _CONTROLLER_FIELDS
        if template.template == "controller"
        else {"id", "enabled", "order", "role"}
    )
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigurationError(
            f"{prefix} has unknown fields: " + ", ".join(unknown)
        )

    order: int | None = None
    if enabled:
        value = raw.get("order")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigurationError(
                f"{prefix} order must be a positive integer"
            )
        order = value
        role = str(raw.get("role", "")).strip()
        if role not in _ROLES:
            raise ConfigurationError(
                f"{prefix} role must be none, sample_temp, or field"
            )
        if template.template != "controller" and role != "none":
            raise ConfigurationError(
                f"{prefix} non-controller panels require role none"
            )
    elif "order" in raw:
        raise ConfigurationError(
            f"{prefix} disabled panels must not declare order"
        )
    elif "role" in raw:
        raise ConfigurationError(
            f"{prefix} disabled panels must not declare role"
        )

    role = role if enabled else "none"
    control_id = ""
    reading = ""
    readings = tuple(template.readings)
    commands = tuple(template.commands)
    minimum = float("-inf")
    maximum = float("inf")
    default_rate = 1.0
    maximum_rate = float("inf")
    stability = None
    if template.template == "controller":
        control_id = template.control
        if enabled:
            if "role" not in raw or "reading" not in raw:
                raise ConfigurationError(
                    f"{prefix} enabled controller requires role and reading"
                )
            if (
                role == "sample_temp"
                and InstrumentKind.TEMPERATURE not in descriptor.kinds
            ):
                raise ConfigurationError(
                    f"{prefix} sample_temp role requires temperature support"
                )
            if role == "field" and InstrumentKind.FIELD not in descriptor.kinds:
                raise ConfigurationError(
                    f"{prefix} field role requires field support"
                )
            reading = str(raw["reading"]).strip()
            if reading not in template.reading_options:
                raise ConfigurationError(
                    f"{prefix} reading must be one of: "
                    + ", ".join(template.reading_options)
                )
        else:
            reading = template.default_reading

        minimum = _finite(
            raw.get("min_value", template.min_value),
            f"{prefix} min_value",
        )
        maximum = _finite(
            raw.get("max_value", template.max_value),
            f"{prefix} max_value",
        )
        default_rate = _positive(
            raw.get(
                "default_rate_per_minute",
                template.default_rate_per_minute,
            ),
            f"{prefix} default_rate_per_minute",
        )
        maximum_rate = _positive(
            raw.get("max_rate_per_minute", template.max_rate_per_minute),
            f"{prefix} max_rate_per_minute",
        )
        if minimum >= maximum:
            raise ConfigurationError(
                f"{prefix} min_value must be less than max_value"
            )
        if default_rate > maximum_rate:
            raise ConfigurationError(
                f"{prefix} default rate must not exceed max rate"
            )
        stability = StabilityConfig(
            tolerance=_nonnegative(
                raw.get(
                    "stability_tolerance",
                    template.stability_tolerance,
                ),
                f"{prefix} stability_tolerance",
            ),
            max_slope_per_minute=_nonnegative(
                raw.get(
                    "stability_max_slope_per_minute",
                    template.stability_max_slope_per_minute,
                ),
                f"{prefix} stability_max_slope_per_minute",
            ),
            dwell_seconds=_nonnegative(
                raw.get(
                    "stability_dwell_seconds",
                    template.stability_dwell_seconds,
                ),
                f"{prefix} stability_dwell_seconds",
            ),
            timeout_seconds=_positive(
                raw.get(
                    "stability_timeout_seconds",
                    template.stability_timeout_seconds,
                ),
                f"{prefix} stability_timeout_seconds",
            ),
            window_seconds=_positive(
                raw.get(
                    "stability_window_seconds",
                    template.stability_window_seconds,
                ),
                f"{prefix} stability_window_seconds",
            ),
        )
        readings = (reading,) if reading else ()

    return InstrumentPanelConfig(
        id=template.id,
        instrument_id=instrument_id,
        display_name=template.label,
        template=template.template,
        enabled=enabled,
        order=order,
        role=role,
        control_id=control_id,
        reading=reading or template.reading,
        readings=readings,
        commands=commands,
        min_value=minimum,
        max_value=maximum,
        default_rate_per_minute=default_rate,
        max_rate_per_minute=maximum_rate,
        stability=stability,
    )


def _configured_instance(
    raw: dict[str, Any],
    descriptor: Any,
    project_root: Path,
    document_path: Path,
    *,
    builtin: bool,
) -> InstrumentConfig:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{document_path} instances must contain tables")
    if "id" not in raw or "panels" not in raw:
        raise ConfigurationError(
            f"{document_path} instance requires id and panels"
        )
    instrument_id = _identifier(raw["id"], "Instrument id")
    prefix = f"Instrument {instrument_id}"

    config_fields = {field.id: field for field in descriptor.config_fields}
    allowed = {"id", "panels", *config_fields}
    if descriptor.identity_pattern:
        allowed.update({"resource", "identity"})
    if builtin:
        allowed.update({"initial_value", "noise"})
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigurationError(
            f"{prefix} has unknown fields: " + ", ".join(unknown)
        )

    address = ""
    if descriptor.identity_pattern:
        resource = raw.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            raise ConfigurationError(f"{prefix} requires a VISA resource address")
        address = resource.strip()
        identity = raw.get("identity", "")
        if not isinstance(identity, str):
            raise ConfigurationError(f"{prefix} identity must be text")
        if identity and re.search(descriptor.identity_pattern, identity) is None:
            raise ConfigurationError(
                f"{prefix} identity does not match its System Instrument"
            )

    extras: dict[str, Any] = {}
    for field_id, field in config_fields.items():
        extras[field_id] = _config_field_value(
            field,
            raw.get(field_id, field.default),
            project_root,
            f"{prefix} {field_id}",
        )
    if builtin:
        extras["noise"] = _finite(raw.get("noise", 0.0), f"{prefix} noise")

    raw_panels = raw["panels"]
    if not isinstance(raw_panels, list):
        raise ConfigurationError(f"{prefix} panels must be an array of tables")
    template_by_id = {panel.id: panel for panel in descriptor.panels}
    configured_panels: list[InstrumentPanelConfig] = []
    seen_panels: set[str] = set()
    for raw_panel in raw_panels:
        if not isinstance(raw_panel, dict):
            raise ConfigurationError(f"{prefix} panels must contain tables")
        panel_id = str(raw_panel.get("id", "")).strip()
        template = template_by_id.get(panel_id)
        if template is None:
            raise ConfigurationError(
                f"{prefix} selects unknown fixed panel {panel_id!r}"
            )
        if panel_id in seen_panels:
            raise ConfigurationError(f"{prefix} repeats fixed panel {panel_id!r}")
        seen_panels.add(panel_id)
        configured_panels.append(
            _configured_panel(raw_panel, template, descriptor, instrument_id)
        )
    missing_panels = sorted(set(template_by_id) - seen_panels)
    if missing_panels:
        raise ConfigurationError(
            f"{prefix} is missing fixed panels: " + ", ".join(missing_panels)
        )
    panels = tuple(configured_panels)
    enabled = sorted(
        (panel for panel in panels if panel.enabled),
        key=lambda panel: panel.order if panel.order is not None else -1,
    )
    controllers = [
        panel for panel in enabled if panel.template == "controller"
    ]
    all_controllers = [
        panel for panel in panels if panel.template == "controller"
    ]
    primary = next(
        (panel for panel in controllers if panel.role != "none"),
        controllers[0]
        if controllers
        else all_controllers[0]
        if all_controllers
        else enabled[0]
        if enabled
        else panels[0],
    )

    main_reading = (
        primary.reading
        if primary.reading
        else primary.readings[0]
        if primary.readings
        else descriptor.readings[0].key
    )
    selected_readings = [main_reading]
    for panel in enabled:
        panel_readings = (
            panel.readings
            if panel.readings
            else (panel.reading,) if panel.reading else ()
        )
        for key in panel_readings:
            if key not in selected_readings:
                selected_readings.append(key)
    reading_metadata = {
        reading.key: InstrumentReadingConfig(
            key=reading.key,
            display_name=reading.label,
            unit=reading.unit,
            decimals=reading.decimals,
        )
        for reading in descriptor.readings
    }
    missing_readings = [
        key for key in selected_readings if key not in reading_metadata
    ]
    if missing_readings:
        raise ConfigurationError(
            f"{prefix} panels reference unknown readings: "
            + ", ".join(missing_readings)
        )
    readings = tuple(reading_metadata[key] for key in selected_readings)
    main_metadata = reading_metadata[main_reading]

    role_kind = {
        "sample_temp": InstrumentKind.TEMPERATURE,
        "field": InstrumentKind.FIELD,
    }.get(primary.role)
    kind = role_kind or descriptor.kinds[0]
    controller = primary if primary.template == "controller" else None
    initial_value = _finite(
        raw.get("initial_value", 0.0),
        f"{prefix} initial_value",
    )
    if (
        builtin
        and controller is not None
        and not controller.min_value <= initial_value <= controller.max_value
    ):
        raise ConfigurationError(
            f"{prefix} initial_value must be within its controller range"
        )

    return InstrumentConfig(
        id=instrument_id,
        display_name=instrument_id,
        kind=kind,
        backend=descriptor.backend if builtin else descriptor.id,
        panels=panels,
        address=address,
        control_enabled=bool(controllers),
        unit=main_metadata.unit,
        main_reading=main_reading,
        auxiliary_readings=tuple(selected_readings[1:]),
        readings=readings,
        sequence_commands=tuple(
            InstrumentCommandConfig(command.id, command.label)
            for command in descriptor.sequence_commands
        ),
        initial_value=initial_value,
        default_rate_per_minute=(
            controller.default_rate_per_minute if controller else 1.0
        ),
        min_value=controller.min_value if controller else float("-inf"),
        max_value=controller.max_value if controller else float("inf"),
        max_rate_per_minute=(
            controller.max_rate_per_minute if controller else float("inf")
        ),
        stability=controller.stability if controller else None,
        extras=extras,
    )


def load_instrument_instances(
    configs_directory: Path,
    instrument_directory: Path,
    project_root: Path,
    resources: tuple[InstrumentResource, ...],
) -> tuple[InstrumentConfig, ...]:
    """Load all generated instances and enforce global role/order constraints."""

    from .instruments.manifest import load_instrument_manifest

    descriptors: dict[str, Any] = {}
    if instrument_directory.is_dir():
        for candidate in sorted(
            instrument_directory.iterdir(),
            key=lambda item: item.name.casefold(),
        ):
            if not candidate.is_dir() or not (
                candidate / "instrument.toml"
            ).is_file():
                continue
            descriptor = load_instrument_manifest(candidate)
            if descriptor.id in descriptors:
                raise ConfigurationError(
                    f"Duplicate System Instrument id: {descriptor.id}"
                )
            descriptors[descriptor.id] = descriptor

    generated_directory = configs_directory / "instruments"
    if not generated_directory.exists():
        return ()
    if not generated_directory.is_dir():
        raise ConfigurationError(f"{generated_directory} must be a directory")

    instances: list[InstrumentConfig] = []
    for path in sorted(
        generated_directory.glob("*.toml"),
        key=lambda item: item.name.casefold(),
    ):
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(
                f"Cannot read instrument configuration {path}: {exc}"
            ) from exc
        unknown = sorted(set(raw) - _DOCUMENT_FIELDS)
        if unknown:
            raise ConfigurationError(
                f"{path} has unknown fields: " + ", ".join(unknown)
            )
        template_id = str(raw.get("id", "")).strip()
        if template_id != path.stem:
            raise ConfigurationError(f"{path} id must match its file name")
        builtin = template_id in _SIMULATED_TEMPLATES
        if builtin:
            descriptor = _embedded_simulation_descriptor(raw, path)
        else:
            descriptor = descriptors.get(template_id)
            if descriptor is None:
                raise ConfigurationError(
                    f"{path} selects missing System Instrument {template_id!r}"
                )
            if not descriptor.can_load:
                raise ConfigurationError(
                    f"{path} selects invalid System Instrument {template_id!r}: "
                    f"{descriptor.error}"
                )
            if raw.get("backend") != descriptor.backend:
                raise ConfigurationError(
                    f"{path} backend does not match the installed System "
                    "Instrument code entry point"
                )
            generated_descriptor = load_instrument_manifest(
                descriptor.path,
                raw_document=raw,
                panels=descriptor.panels,
            )
            if not generated_descriptor.can_load:
                raise ConfigurationError(
                    f"{path} has invalid generated System Instrument metadata: "
                    f"{generated_descriptor.error}"
                )
            descriptor = generated_descriptor
        raw_instances = raw.get("instances")
        if not isinstance(raw_instances, list):
            raise ConfigurationError(
                f"{path} instances must be an array of tables"
            )
        instances.extend(
            _configured_instance(
                item,
                descriptor,
                project_root,
                path,
                builtin=builtin,
            )
            for item in raw_instances
        )

    ids = [instance.id for instance in instances]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("Physical instrument instance IDs must be unique")
    enabled_panels = [
        panel
        for instance in instances
        for panel in instance.panels
        if panel.enabled
    ]
    orders = [panel.order for panel in enabled_panels]
    if sorted(orders) != list(range(1, len(orders) + 1)):
        raise ConfigurationError(
            "Enabled instrument panel order must be globally unique and "
            "continuous from 1"
        )
    for role in ("sample_temp", "field"):
        owners = [panel.key for panel in enabled_panels if panel.role == role]
        if len(owners) > 1:
            raise ConfigurationError(
                f"Instrument panel role {role!r} must be globally unique: "
                + ", ".join(owners)
            )

    system_addresses: dict[str, str] = {}
    for instance in instances:
        if not instance.address:
            continue
        folded = instance.address.casefold()
        previous = system_addresses.get(folded)
        if previous is not None:
            raise ConfigurationError(
                f"System Instrument address {instance.address!r} is assigned to "
                f"both {previous} and {instance.id}"
            )
        system_addresses[folded] = instance.id
    measurement_addresses = {
        resource.address.casefold(): resource.id for resource in resources
    }
    overlap = sorted(set(system_addresses) & set(measurement_addresses))
    if overlap:
        address = overlap[0]
        raise ConfigurationError(
            "VISA address is assigned to both System Instrument "
            f"{system_addresses[address]} and Measurement resource "
            f"{measurement_addresses[address]}"
        )
    instances.sort(
        key=lambda instance: (
            min(
                (
                    int(panel.order)
                    for panel in instance.panels
                    if panel.enabled
                ),
                default=len(enabled_panels) + 1,
            ),
            instance.id,
        )
    )
    return tuple(instances)


__all__ = ["load_instrument_instances"]
