"""发现并严格验证外部 System Instrument 清单。

每个系统仪表目录必须包含 ``instrument.toml``，声明稳定 ID、版本、核心兼容范围、支持的
仪表类型、后端入口和仅属于该后端的额外依赖。目录指纹由信任层计算；PyVISA 等框架通用
依赖不得由系统仪表覆盖版本。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..package_support.dependencies import (
    partition_package_dependencies,
    validate_requirements_lock,
)
from ..package_support.trust import ContentTrustError, content_tree_digest
from ..models import InstrumentKind

if TYPE_CHECKING:
    from ..config import AppConfig


SYSTEM_INSTRUMENT_API_VERSION = "3"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTRYPOINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$"
)


@dataclass(frozen=True, slots=True)
class InstrumentReadingDescriptor:
    """清单中一个可显示读数的唯一元数据来源。"""

    key: str
    label: str
    unit: str = ""
    decimals: int | None = None


@dataclass(slots=True)
class SystemInstrumentDescriptor:
    id: str
    name: str
    version: str
    path: Path
    panel_template: str
    api_version: str = ""
    core_requires: str = ""
    backend: str = ""
    kinds: tuple[InstrumentKind, ...] = ()
    framework_dependencies: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    identity_pattern: str = ""
    main_reading: str = ""
    readings: tuple[InstrumentReadingDescriptor, ...] = ()
    fingerprint: str = ""
    valid: bool = True
    error: str = ""

    @property
    def can_load(self) -> bool:
        return self.valid and bool(self.fingerprint)

    @property
    def auxiliary_readings(self) -> tuple[str, ...]:
        return tuple(
            reading.key
            for reading in self.readings
            if reading.key != self.main_reading
        )

    def reading(self, key: str) -> InstrumentReadingDescriptor:
        for reading in self.readings:
            if reading.key == key:
                return reading
        raise KeyError(key)


def _invalid(path: Path, message: str) -> SystemInstrumentDescriptor:
    return SystemInstrumentDescriptor(
        id=path.name.casefold().replace("-", "_"),
        name=f"{path.name} (Invalid)",
        version="—",
        path=path.resolve(),
        panel_template="",
        valid=False,
        error=message,
    )


def load_instrument_manifest(path: Path) -> SystemInstrumentDescriptor:
    manifest_path = path / "instrument.toml"
    try:
        with manifest_path.open("rb") as handle:
            raw = tomllib.load(handle)
        unknown_fields = sorted(
            set(raw)
            - {
                "api_version",
                "backend",
                "core_requires",
                "dependencies",
                "discovery",
                "id",
                "kinds",
                "main_reading",
                "name",
                "panel",
                "readings",
                "version",
            }
        )
        if unknown_fields:
            raise ValueError(
                "unknown instrument.toml fields: "
                + ", ".join(unknown_fields)
            )
        instrument_id = str(raw["id"]).strip()
        name = str(raw["name"]).strip()
        version = str(raw["version"]).strip()
        api_version = str(raw["api_version"]).strip()
        core_requires = str(raw.get("core_requires", "")).strip()
        backend = str(raw["backend"]).strip()
        kinds = tuple(InstrumentKind(str(item).strip().casefold()) for item in raw["kinds"])
        declared_dependencies = tuple(
            str(item).strip()
            for item in raw.get("dependencies", [])
        )
        discovery = raw.get("discovery", {})
        if not isinstance(discovery, dict):
            raise TypeError("discovery must be a table")
        unknown_discovery_fields = sorted(
            set(discovery) - {"identity_pattern"}
        )
        if unknown_discovery_fields:
            raise ValueError(
                "unknown discovery fields: "
                + ", ".join(unknown_discovery_fields)
            )
        identity_pattern = str(
            discovery.get("identity_pattern", "")
        ).strip()
        panel = raw.get("panel")
        if not isinstance(panel, dict):
            raise TypeError("panel must be a table")
        unknown_panel_fields = sorted(set(panel) - {"template"})
        if unknown_panel_fields:
            raise ValueError(
                "unknown panel fields: "
                + ", ".join(unknown_panel_fields)
            )
        panel_template = str(panel["template"]).strip()
        main_reading = str(raw.get("main_reading", "")).strip()
        raw_readings = raw.get("readings", {})
        if not isinstance(raw_readings, dict):
            raise TypeError("readings must be a table")
        readings: list[InstrumentReadingDescriptor] = []
        for raw_key, raw_metadata in raw_readings.items():
            if not isinstance(raw_metadata, dict):
                raise TypeError(f"readings.{raw_key} must be a table")
            unknown_reading_fields = sorted(
                set(raw_metadata) - {"decimals", "label", "unit"}
            )
            if unknown_reading_fields:
                raise ValueError(
                    f"unknown readings.{raw_key} fields: "
                    + ", ".join(unknown_reading_fields)
                )
            decimals = raw_metadata.get("decimals")
            readings.append(
                InstrumentReadingDescriptor(
                    key=str(raw_key).strip(),
                    label=str(raw_metadata["label"]).strip(),
                    unit=str(raw_metadata.get("unit", "")).strip(),
                    decimals=decimals,
                )
            )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as exc:
        return _invalid(path, f"Cannot read instrument.toml: {exc}")

    (
        framework_dependencies,
        dependencies,
        dependency_compatibility_errors,
    ) = partition_package_dependencies(
        declared_dependencies
    )
    descriptor = SystemInstrumentDescriptor(
        id=instrument_id,
        name=name,
        version=version,
        path=path.resolve(),
        panel_template=panel_template,
        api_version=api_version,
        core_requires=core_requires,
        backend=backend,
        kinds=kinds,
        framework_dependencies=framework_dependencies,
        dependencies=dependencies,
        identity_pattern=identity_pattern,
        main_reading=main_reading,
        readings=tuple(readings),
    )
    errors: list[str] = []
    if not _IDENTIFIER.fullmatch(instrument_id):
        errors.append("id must match [a-z][a-z0-9_]*")
    if not name:
        errors.append("name must not be empty")
    try:
        Version(version)
    except InvalidVersion:
        errors.append(f"version {version!r} is invalid")
    if api_version != SYSTEM_INSTRUMENT_API_VERSION:
        errors.append(
            f"API {api_version!r} is incompatible with {SYSTEM_INSTRUMENT_API_VERSION}"
        )
    if core_requires:
        try:
            compatible = Version(__version__) in SpecifierSet(core_requires)
        except (InvalidSpecifier, InvalidVersion):
            errors.append(f"core_requires {core_requires!r} is invalid")
        else:
            if not compatible:
                errors.append(
                    f"OpenLab Control {__version__} does not satisfy {core_requires}"
                )
    if not _ENTRYPOINT.fullmatch(backend):
        errors.append("backend must use module:ClassName without a path")
    else:
        module_name = backend.split(":", 1)[0]
        if not (path / f"{module_name}.py").is_file():
            errors.append(
                f"backend source does not exist: {module_name}.py"
            )
    if not kinds:
        errors.append("at least one supported instrument kind is required")
    if len(kinds) != len(set(kinds)):
        errors.append("supported instrument kinds must be unique")
    if panel_template not in {"controller", "readout"}:
        errors.append("panel.template must be controller or readout")
    if (
        panel_template == "controller"
        and InstrumentKind.MONITOR in kinds
    ):
        errors.append("monitor instruments must use the readout panel template")
    if identity_pattern:
        if len(identity_pattern) > 256:
            errors.append("discovery.identity_pattern is too long")
        else:
            try:
                re.compile(identity_pattern)
            except re.error as exc:
                errors.append(
                    f"discovery.identity_pattern is invalid: {exc}"
                )
    if main_reading and not _IDENTIFIER.fullmatch(main_reading):
        errors.append("main_reading must match [a-z][a-z0-9_]*")
    if not main_reading:
        errors.append("main_reading is required")
    metadata_by_reading = {reading.key: reading for reading in readings}
    if main_reading and main_reading not in metadata_by_reading:
        errors.append(
            "readings must define the declared main_reading"
        )
    for reading in readings:
        if not _IDENTIFIER.fullmatch(reading.key):
            errors.append(
                "readings keys must match [a-z][a-z0-9_]*"
            )
        if (
            not reading.label
            or len(reading.label) > 80
            or any(not character.isprintable() for character in reading.label)
        ):
            errors.append(
                "reading labels must be printable text "
                "with 1-80 characters"
            )
        if (
            len(reading.unit) > 24
            or any(not character.isprintable() for character in reading.unit)
        ):
            errors.append("reading units must be printable text with at most 24 characters")
        if reading.decimals is not None and (
            isinstance(reading.decimals, bool)
            or not isinstance(reading.decimals, int)
            or not 0 <= reading.decimals <= 12
        ):
            errors.append("reading decimals must be an integer from 0 to 12")
    if len({item.label.casefold() for item in readings}) != len(
        readings
    ):
        errors.append("reading labels must be unique")
    errors.extend(dependency_compatibility_errors)
    for raw_requirement in declared_dependencies:
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement:
            errors.append(f"invalid dependency: {raw_requirement}")
            continue
        if requirement.url is not None:
            errors.append(f"dependency URLs are not allowed: {raw_requirement}")
    errors.extend(
        # requirements.lock 只描述系统仪表自己的额外依赖；PyVISA 等通用包由核心锁定。
        validate_requirements_lock(path, dependencies)
    )
    try:
        descriptor.fingerprint = content_tree_digest(path)
    except ContentTrustError as exc:
        errors.append(str(exc))
    if errors:
        descriptor.valid = False
        descriptor.error = "; ".join(errors)
    return descriptor


def discover_system_instruments(
    config: "AppConfig",
) -> tuple[SystemInstrumentDescriptor, ...]:
    root = config.resolve_project_path(config.system_instruments.directory)
    root.mkdir(parents=True, exist_ok=True)
    descriptors = [
        load_instrument_manifest(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir() and (path / "instrument.toml").exists()
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
    for instrument in config.instruments:
        if ":" in instrument.backend:
            continue
        descriptor = by_id.get(instrument.backend)
        if descriptor is None:
            raise ValueError(
                f"Instrument {instrument.id} selects unknown System Instrument {instrument.backend!r}"
            )
        if not descriptor.can_load:
            raise ValueError(
                f"System Instrument {descriptor.id} is invalid: {descriptor.error}"
            )
        if instrument.kind not in descriptor.kinds:
            raise ValueError(
                f"System Instrument {descriptor.id} does not support {instrument.kind.value}"
            )
        if descriptor.id not in seen:
            selected.append(descriptor)
            seen.add(descriptor.id)
    return tuple(selected)


def instrument_dependency_directory(
    config: "AppConfig",
    descriptor: SystemInstrumentDescriptor,
) -> Path:
    return (
        config.resolve_project_path(config.system_instruments.runtime_directory)
        / "instrument"
        / descriptor.id
        / descriptor.fingerprint[:16]
        / "site-packages"
    )
