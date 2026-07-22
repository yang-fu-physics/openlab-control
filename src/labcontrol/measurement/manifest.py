from __future__ import annotations

import importlib.metadata
import importlib.util
import hashlib
import re
import site
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ..config import AppConfig


MODULE_API_VERSION = "1.0"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


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
    frontend: str = ""
    backend: str = ""
    backend_type: str = "python"
    dependencies: tuple[str, ...] = ()
    columns: tuple[ModuleColumn, ...] = ()
    valid: bool = True
    error: str = ""
    dependency_error: str = ""

    @property
    def can_enable(self) -> bool:
        return self.valid and not self.dependency_error


def _invalid(path: Path, message: str) -> ModuleDescriptor:
    return ModuleDescriptor(
        id=path.name.casefold().replace("-", "_"),
        name=f"{path.name} (Invalid)",
        version="—",
        path=path,
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
        frontend = str(raw["frontend"]).strip()
        backend = str(raw["backend"]).strip()
        backend_type = str(raw.get("backend_type", "python")).strip().casefold()
        dependencies = tuple(str(item).strip() for item in raw.get("dependencies", []))
        columns = tuple(
            ModuleColumn(str(item["name"]).strip(), str(item.get("unit", "")).strip())
            for item in raw.get("columns", [])
        )
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        return _invalid(path, f"Cannot read module.toml: {exc}")

    descriptor = ModuleDescriptor(
        id=module_id,
        name=name,
        version=version,
        path=path.resolve(),
        api_version=api_version,
        frontend=frontend,
        backend=backend,
        backend_type=backend_type,
        dependencies=dependencies,
        columns=columns,
    )
    errors: list[str] = []
    if not _IDENTIFIER.fullmatch(module_id):
        errors.append("id must match [a-z][a-z0-9_]*")
    if api_version != MODULE_API_VERSION:
        errors.append(f"API {api_version!r} is incompatible with {MODULE_API_VERSION}")
    if backend_type != "python":
        errors.append("only backend_type='python' is supported in this release")
    if ":" not in frontend or ":" not in backend:
        errors.append("frontend and backend must use file:ClassName format")
    if not columns:
        errors.append("at least one [[columns]] entry is required")
    column_names = [item.name for item in columns]
    if any(not name or "," in name or "\n" in name for name in column_names):
        errors.append("column names must be non-empty single-line values without commas")
    if len(column_names) != len(set(column_names)):
        errors.append("column names must be unique")
    if errors:
        descriptor.valid = False
        descriptor.error = "; ".join(errors)
    return descriptor


def _requirement_bounds(
    requirement: Requirement,
) -> tuple[tuple[Version, bool] | None, tuple[Version, bool] | None, tuple[Version, ...]]:
    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    exact: list[Version] = []
    for specifier in requirement.specifier:
        operator = specifier.operator
        raw_version = specifier.version
        if operator in {"==", "==="} and "*" not in raw_version:
            try:
                exact.append(Version(raw_version))
            except InvalidVersion:
                continue
            continue
        if "*" in raw_version:
            continue
        try:
            version = Version(raw_version)
        except InvalidVersion:
            continue
        if operator in {">=", ">", "~="}:
            candidate = (version, operator != ">")
            if lower is None or candidate[0] > lower[0] or (
                candidate[0] == lower[0] and not candidate[1]
            ):
                lower = candidate
        elif operator in {"<=", "<"}:
            candidate = (version, operator == "<=")
            if upper is None or candidate[0] < upper[0] or (
                candidate[0] == upper[0] and not candidate[1]
            ):
                upper = candidate
        if operator == "~=":
            release = list(version.release)
            if len(release) == 1:
                compatible = Version(str(release[0] + 1))
            else:
                prefix = release[:-1]
                prefix[-1] += 1
                compatible = Version(".".join(str(item) for item in prefix))
            if upper is None or compatible < upper[0]:
                upper = (compatible, False)
    return lower, upper, tuple(exact)


def _requirements_conflict(first: Requirement, second: Requirement) -> bool:
    first_lower, first_upper, first_exact = _requirement_bounds(first)
    second_lower, second_upper, second_exact = _requirement_bounds(second)
    exact = first_exact + second_exact
    if exact:
        return not any(
            candidate in first.specifier and candidate in second.specifier
            for candidate in exact
        )
    lowers = [item for item in (first_lower, second_lower) if item is not None]
    uppers = [item for item in (first_upper, second_upper) if item is not None]
    if not lowers or not uppers:
        return False
    lower = max(lowers, key=lambda item: (item[0], not item[1]))
    upper = min(uppers, key=lambda item: (item[0], item[1]))
    return lower[0] > upper[0] or (
        lower[0] == upper[0] and not (lower[1] and upper[1])
    )


def _dependency_conflicts(descriptors: list[ModuleDescriptor]) -> None:
    declared: dict[str, list[tuple[Requirement, ModuleDescriptor]]] = {}
    for descriptor in descriptors:
        if not descriptor.valid:
            continue
        for raw_requirement in descriptor.dependencies:
            try:
                requirement = Requirement(raw_requirement)
            except InvalidRequirement:
                descriptor.dependency_error = f"Invalid dependency: {raw_requirement}"
                continue
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            package = canonicalize_name(requirement.name)
            for previous, previous_descriptor in declared.get(package, []):
                if not _requirements_conflict(previous, requirement):
                    continue
                message = (
                    f"Dependency conflict: {raw_requirement} conflicts with "
                    f"{previous_descriptor.id} ({previous})"
                )
                descriptor.dependency_error = message
                previous_descriptor.dependency_error = message
            declared.setdefault(package, []).append((requirement, descriptor))


def discover_modules(config: AppConfig) -> tuple[ModuleDescriptor, ...]:
    root = config.resolve_project_path(config.modules.directory)
    root.mkdir(parents=True, exist_ok=True)
    descriptors = [
        load_manifest(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir() and (path / "module.toml").exists()
    ]
    seen: dict[str, ModuleDescriptor] = {}
    for descriptor in descriptors:
        duplicate = seen.get(descriptor.id)
        if duplicate is not None:
            descriptor.valid = False
            duplicate.valid = False
            message = f"Duplicate module id: {descriptor.id}"
            descriptor.error = message
            duplicate.error = message
        else:
            seen[descriptor.id] = descriptor
    _dependency_conflicts(descriptors)
    return tuple(descriptors)


def activate_shared_dependencies(config: AppConfig) -> Path:
    """Make the one shared module dependency directory importable everywhere."""
    path = config.resolve_project_path(config.modules.site_packages_directory)
    path.mkdir(parents=True, exist_ok=True)
    value = str(path)
    site.addsitedir(value)
    if value in sys.path:
        sys.path.remove(value)
    sys.path.insert(0, value)
    return path


def missing_dependencies(descriptor: ModuleDescriptor) -> tuple[str, ...]:
    missing: list[str] = []
    for requirement in descriptor.dependencies:
        try:
            parsed = Requirement(requirement)
        except InvalidRequirement:
            missing.append(requirement)
            continue
        if parsed.marker is not None and not parsed.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(parsed.name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(requirement)
            continue
        try:
            satisfies = not parsed.specifier or Version(installed) in parsed.specifier
        except InvalidVersion:
            satisfies = False
        if not satisfies:
            missing.append(requirement)
    return tuple(missing)


def load_source_object(directory: Path, specification: str, namespace: str) -> object:
    del namespace
    file_name, object_name = specification.split(":", 1)
    source = directory / (file_name if file_name.endswith(".py") else f"{file_name}.py")
    if not source.exists():
        raise FileNotFoundError(source)
    digest = hashlib.sha1(str(directory.resolve()).encode("utf-8")).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9_]", "_", directory.name)
    package_name = f"_openlab_module_{safe_name}_{digest}"
    module_name = f"{package_name}.{source.stem}"
    importlib.invalidate_caches()
    for loaded_name in tuple(sys.modules):
        if loaded_name == package_name or loaded_name.startswith(package_name + "."):
            sys.modules.pop(loaded_name, None)
    package = ModuleType(package_name)
    package.__path__ = [str(directory)]  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package
    module_spec = importlib.util.spec_from_file_location(module_name, source)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"Cannot load {source}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    module_spec.loader.exec_module(module)
    return getattr(module, object_name)
