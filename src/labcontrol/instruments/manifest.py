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

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..config import AppConfig
from ..package_support.dependencies import (
    partition_package_dependencies,
    validate_requirements_lock,
)
from ..package_support.trust import ContentTrustError, content_tree_digest
from ..models import InstrumentKind


SYSTEM_INSTRUMENT_API_VERSION = "1.2"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTRYPOINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$"
)


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
    framework_dependencies: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    identity_pattern: str = ""
    primary_reading: str = ""
    monitor_readings: tuple[str, ...] = ()
    fingerprint: str = ""
    valid: bool = True
    error: str = ""

    @property
    def can_load(self) -> bool:
        return self.valid and bool(self.fingerprint)


def _invalid(path: Path, message: str) -> SystemInstrumentDescriptor:
    return SystemInstrumentDescriptor(
        id=path.name.casefold().replace("-", "_"),
        name=f"{path.name} (Invalid)",
        version="—",
        path=path.resolve(),
        valid=False,
        error=message,
    )


def load_instrument_manifest(path: Path) -> SystemInstrumentDescriptor:
    manifest_path = path / "instrument.toml"
    try:
        with manifest_path.open("rb") as handle:
            raw = tomllib.load(handle)
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
        identity_pattern = str(
            discovery.get("identity_pattern", "")
        ).strip()
        primary_reading = str(
            discovery.get("primary_reading", "")
        ).strip()
        raw_monitor_readings = discovery.get("monitor_readings", [])
        if not isinstance(raw_monitor_readings, list):
            raise TypeError("discovery.monitor_readings must be an array")
        monitor_readings = tuple(
            str(item).strip()
            for item in raw_monitor_readings
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
        api_version=api_version,
        core_requires=core_requires,
        backend=backend,
        kinds=kinds,
        framework_dependencies=framework_dependencies,
        dependencies=dependencies,
        identity_pattern=identity_pattern,
        primary_reading=primary_reading,
        monitor_readings=monitor_readings,
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
    for label, reading in (
        ("primary_reading", primary_reading),
        *(("monitor_readings", value) for value in monitor_readings),
    ):
        if reading and not _IDENTIFIER.fullmatch(reading):
            errors.append(
                f"discovery.{label} must match [a-z][a-z0-9_]*"
            )
    if len(monitor_readings) != len(set(monitor_readings)):
        errors.append("discovery.monitor_readings must be unique")
    if primary_reading and primary_reading in monitor_readings:
        errors.append(
            "discovery primary_reading cannot also be a monitor_reading"
        )
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
    config: AppConfig,
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
    config: AppConfig,
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
    config: AppConfig,
    descriptor: SystemInstrumentDescriptor,
) -> Path:
    return (
        config.resolve_project_path(config.system_instruments.runtime_directory)
        / "instrument"
        / descriptor.id
        / descriptor.fingerprint[:16]
        / "site-packages"
    )
