"""发现、校验并描述 Measurement Module 清单。

模块目录通过 ``module.toml`` 声明后端、可选 Qt 前端、固定 DAT 列和额外依赖。发现阶段只
读取文件和计算指纹，不初始化模块；每次点击 Enable 前服务层仍会重新核对目录内容、信任、
API 版本和隔离依赖。
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
    dependency_runtime_errors,
    missing_dependencies as find_missing_dependencies,
    partition_extension_dependencies,
    validate_requirements_lock,
)
from ..extensions.loading import load_source_object
from ..extensions.trust import (
    ExtensionTrustError,
    extension_tree_digest,
)


MODULE_API_VERSION = "1.0"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTRYPOINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$"
)


@dataclass(frozen=True, slots=True)
class ModuleColumn:
    name: str
    unit: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}({self.unit})" if self.unit else self.name


@dataclass(slots=True)
class ModuleDescriptor:
    id: str
    name: str
    version: str
    path: Path
    api_version: str = ""
    core_requires: str = ""
    frontend: str = ""
    backend: str = ""
    backend_type: str = "python"
    framework_dependencies: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    columns: tuple[ModuleColumn, ...] = ()
    fingerprint: str = ""
    valid: bool = True
    error: str = ""
    dependency_error: str = ""

    @property
    def can_enable(self) -> bool:
        return (
            self.valid
            and bool(self.fingerprint)
            and not self.dependency_error
        )


def _invalid(path: Path, message: str) -> ModuleDescriptor:
    return ModuleDescriptor(
        id=path.name.casefold().replace("-", "_"),
        name=f"{path.name} (Invalid)",
        version="—",
        path=path.resolve(),
        valid=False,
        error=message,
    )


def load_manifest(path: Path) -> ModuleDescriptor:
    manifest_path = path / "module.toml"
    try:
        with manifest_path.open("rb") as handle:
            raw = tomllib.load(handle)
        module_id = str(raw["id"]).strip()
        name = str(raw["name"]).strip()
        version = str(raw["version"]).strip()
        api_version = str(raw["api_version"]).strip()
        core_requires = str(
            raw.get("core_requires", "")
        ).strip()
        frontend = str(raw["frontend"]).strip()
        backend = str(raw["backend"]).strip()
        backend_type = str(
            raw.get("backend_type", "python")
        ).strip().casefold()
        declared_dependencies = tuple(
            str(item).strip()
            for item in raw.get("dependencies", [])
        )
        columns = tuple(
            ModuleColumn(
                str(item["name"]).strip(),
                str(item.get("unit", "")).strip(),
            )
            for item in raw.get("columns", [])
        )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as exc:
        return _invalid(
            path,
            f"Cannot read module.toml: {exc}",
        )

    (
        framework_dependencies,
        dependencies,
        dependency_compatibility_errors,
    ) = partition_extension_dependencies(
        declared_dependencies
    )
    descriptor = ModuleDescriptor(
        id=module_id,
        name=name,
        version=version,
        path=path.resolve(),
        api_version=api_version,
        core_requires=core_requires,
        frontend=frontend,
        backend=backend,
        backend_type=backend_type,
        framework_dependencies=framework_dependencies,
        dependencies=dependencies,
        columns=columns,
    )
    errors: list[str] = []
    if not _IDENTIFIER.fullmatch(module_id):
        errors.append("id must match [a-z][a-z0-9_]*")
    if not name:
        errors.append("name must not be empty")
    try:
        Version(version)
    except InvalidVersion:
        errors.append(f"version {version!r} is invalid")
    if api_version != MODULE_API_VERSION:
        errors.append(
            f"API {api_version!r} is incompatible with "
            f"{MODULE_API_VERSION}"
        )
    if core_requires:
        try:
            compatible = (
                Version(__version__)
                in SpecifierSet(core_requires)
            )
        except (InvalidSpecifier, InvalidVersion):
            errors.append(
                f"core_requires {core_requires!r} is invalid"
            )
        else:
            if not compatible:
                errors.append(
                    f"OpenLab Control {__version__} does not "
                    f"satisfy {core_requires}"
                )
    if backend_type != "python":
        errors.append(
            "only backend_type='python' is supported in this release"
        )
    if not _ENTRYPOINT.fullmatch(frontend):
        errors.append(
            "frontend must use module:ClassName without a path"
        )
    if not _ENTRYPOINT.fullmatch(backend):
        errors.append(
            "backend must use module:ClassName without a path"
        )
    for label, entrypoint in (
        ("frontend", frontend),
        ("backend", backend),
    ):
        if _ENTRYPOINT.fullmatch(entrypoint):
            module_name = entrypoint.split(":", 1)[0]
            if not (path / f"{module_name}.py").is_file():
                errors.append(
                    f"{label} source does not exist: "
                    f"{module_name}.py"
                )
    errors.extend(dependency_compatibility_errors)
    for raw_requirement in declared_dependencies:
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement:
            errors.append(
                f"invalid dependency: {raw_requirement}"
            )
            continue
        if requirement.url is not None:
            errors.append(
                f"dependency URLs are not allowed: {raw_requirement}"
            )
    errors.extend(
        # 只有核心尚未提供的额外依赖才需要扩展自己的锁文件；通用依赖由核心
        # requirements-lock.txt 统一锁定，禁止模块私下覆盖版本。
        validate_requirements_lock(path, dependencies)
    )
    if not columns:
        errors.append(
            "at least one [[columns]] entry is required"
        )
    column_names = [item.name for item in columns]
    if any(
        not column or "," in column or "\n" in column
        for column in column_names
    ):
        errors.append(
            "column names must be non-empty single-line values "
            "without commas"
        )
    if len(column_names) != len(set(column_names)):
        errors.append("column names must be unique")
    try:
        descriptor.fingerprint = extension_tree_digest(path)
    except ExtensionTrustError as exc:
        errors.append(str(exc))
    if errors:
        descriptor.valid = False
        descriptor.error = "; ".join(
            dict.fromkeys(errors)
        )
    return descriptor


def discover_modules(
    config: AppConfig,
) -> tuple[ModuleDescriptor, ...]:
    root = config.resolve_project_path(
        config.modules.directory
    )
    root.mkdir(parents=True, exist_ok=True)
    descriptors = [
        load_manifest(path)
        for path in sorted(
            root.iterdir(),
            key=lambda item: item.name.casefold(),
        )
        if path.is_dir()
        and (path / "module.toml").exists()
    ]
    seen: dict[str, ModuleDescriptor] = {}
    for descriptor in descriptors:
        duplicate = seen.get(descriptor.id)
        if duplicate is None:
            seen[descriptor.id] = descriptor
            continue
        message = f"Duplicate module id: {descriptor.id}"
        descriptor.valid = False
        descriptor.error = "; ".join(
            item
            for item in (descriptor.error, message)
            if item
        )
        duplicate.valid = False
        duplicate.error = "; ".join(
            item
            for item in (duplicate.error, message)
            if item
        )
    return tuple(descriptors)


def module_dependency_directory(
    config: AppConfig,
    descriptor: ModuleDescriptor,
) -> Path:
    return (
        config.resolve_project_path(
            config.modules.runtime_directory
        )
        / "module"
        / descriptor.id
        / descriptor.fingerprint[:16]
        / "site-packages"
    )


def missing_dependencies(
    config: AppConfig,
    descriptor: ModuleDescriptor,
) -> tuple[str, ...]:
    return find_missing_dependencies(
        descriptor.dependencies,
        module_dependency_directory(config, descriptor),
    )


def module_dependency_errors(
    config: AppConfig,
    descriptor: ModuleDescriptor,
) -> tuple[str, ...]:
    return dependency_runtime_errors(
        descriptor.dependencies,
        module_dependency_directory(config, descriptor),
        descriptor.fingerprint,
    )


__all__ = [
    "MODULE_API_VERSION",
    "ModuleColumn",
    "ModuleDescriptor",
    "discover_modules",
    "load_manifest",
    "load_source_object",
    "missing_dependencies",
    "module_dependency_errors",
    "module_dependency_directory",
]
