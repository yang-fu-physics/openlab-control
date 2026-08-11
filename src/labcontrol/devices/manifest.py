"""发现并严格验证外部 Device Plugin 清单。

每个插件目录必须包含 ``device.toml``，声明稳定 ID、版本、核心兼容范围、支持的设备类型、
后端入口和仅属于插件的额外依赖。目录指纹由信任层计算；PyVISA 等框架通用依赖不得由插件
覆盖版本。
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
from ..extensions.dependencies import (
    partition_extension_dependencies,
    validate_requirements_lock,
)
from ..extensions.trust import ExtensionTrustError, extension_tree_digest
from ..models import DeviceKind


DEVICE_PLUGIN_API_VERSION = "1.1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTRYPOINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$"
)


@dataclass(slots=True)
class DevicePluginDescriptor:
    id: str
    name: str
    version: str
    path: Path
    api_version: str = ""
    core_requires: str = ""
    backend: str = ""
    kinds: tuple[DeviceKind, ...] = ()
    framework_dependencies: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    fingerprint: str = ""
    valid: bool = True
    error: str = ""

    @property
    def can_load(self) -> bool:
        return self.valid and bool(self.fingerprint)


def _invalid(path: Path, message: str) -> DevicePluginDescriptor:
    return DevicePluginDescriptor(
        id=path.name.casefold().replace("-", "_"),
        name=f"{path.name} (Invalid)",
        version="—",
        path=path.resolve(),
        valid=False,
        error=message,
    )


def load_device_manifest(path: Path) -> DevicePluginDescriptor:
    manifest_path = path / "device.toml"
    try:
        with manifest_path.open("rb") as handle:
            raw = tomllib.load(handle)
        plugin_id = str(raw["id"]).strip()
        name = str(raw["name"]).strip()
        version = str(raw["version"]).strip()
        api_version = str(raw["api_version"]).strip()
        core_requires = str(raw.get("core_requires", "")).strip()
        backend = str(raw["backend"]).strip()
        kinds = tuple(DeviceKind(str(item).strip().casefold()) for item in raw["kinds"])
        declared_dependencies = tuple(
            str(item).strip()
            for item in raw.get("dependencies", [])
        )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as exc:
        return _invalid(path, f"Cannot read device.toml: {exc}")

    (
        framework_dependencies,
        dependencies,
        dependency_compatibility_errors,
    ) = partition_extension_dependencies(
        declared_dependencies
    )
    descriptor = DevicePluginDescriptor(
        id=plugin_id,
        name=name,
        version=version,
        path=path.resolve(),
        api_version=api_version,
        core_requires=core_requires,
        backend=backend,
        kinds=kinds,
        framework_dependencies=framework_dependencies,
        dependencies=dependencies,
    )
    errors: list[str] = []
    if not _IDENTIFIER.fullmatch(plugin_id):
        errors.append("id must match [a-z][a-z0-9_]*")
    if not name:
        errors.append("name must not be empty")
    try:
        Version(version)
    except InvalidVersion:
        errors.append(f"version {version!r} is invalid")
    if api_version != DEVICE_PLUGIN_API_VERSION:
        errors.append(
            f"API {api_version!r} is incompatible with {DEVICE_PLUGIN_API_VERSION}"
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
        errors.append("at least one supported device kind is required")
    if len(kinds) != len(set(kinds)):
        errors.append("supported device kinds must be unique")
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
        # requirements.lock 只描述扩展自己的额外依赖；PyVISA 等通用包由核心锁定。
        validate_requirements_lock(path, dependencies)
    )
    try:
        descriptor.fingerprint = extension_tree_digest(path)
    except ExtensionTrustError as exc:
        errors.append(str(exc))
    if errors:
        descriptor.valid = False
        descriptor.error = "; ".join(errors)
    return descriptor


def discover_device_plugins(config: AppConfig) -> tuple[DevicePluginDescriptor, ...]:
    root = config.resolve_project_path(config.plugins.device_directory)
    root.mkdir(parents=True, exist_ok=True)
    descriptors = [
        load_device_manifest(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir() and (path / "device.toml").exists()
    ]
    seen: dict[str, DevicePluginDescriptor] = {}
    for descriptor in descriptors:
        duplicate = seen.get(descriptor.id)
        if duplicate is None:
            seen[descriptor.id] = descriptor
            continue
        message = f"Duplicate device plugin id: {descriptor.id}"
        descriptor.valid = False
        descriptor.error = "; ".join(
            item for item in (descriptor.error, message) if item
        )
        duplicate.valid = False
        duplicate.error = "; ".join(
            item for item in (duplicate.error, message) if item
        )
    return tuple(descriptors)


def configured_device_plugins(
    config: AppConfig,
    descriptors: tuple[DevicePluginDescriptor, ...],
) -> tuple[DevicePluginDescriptor, ...]:
    by_id = {descriptor.id: descriptor for descriptor in descriptors}
    selected: list[DevicePluginDescriptor] = []
    seen: set[str] = set()
    for device in config.devices:
        if ":" in device.plugin:
            continue
        descriptor = by_id.get(device.plugin)
        if descriptor is None:
            raise ValueError(
                f"Device {device.id} selects unknown external plugin {device.plugin!r}"
            )
        if not descriptor.can_load:
            raise ValueError(
                f"Device plugin {descriptor.id} is invalid: {descriptor.error}"
            )
        if device.kind not in descriptor.kinds:
            raise ValueError(
                f"Device plugin {descriptor.id} does not support {device.kind.value}"
            )
        if descriptor.id not in seen:
            selected.append(descriptor)
            seen.add(descriptor.id)
    return tuple(selected)


def device_dependency_directory(
    config: AppConfig,
    descriptor: DevicePluginDescriptor,
) -> Path:
    return (
        config.resolve_project_path(config.plugins.runtime_directory)
        / "device"
        / descriptor.id
        / descriptor.fingerprint[:16]
        / "site-packages"
    )
