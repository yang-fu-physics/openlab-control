"""发现 Measurement Module 的最小清单。

模块作者只需在 ``module.toml`` 中填写 ``name`` 和 ``version``。目录名就是模块 ID，
后端固定从 ``backend.py`` 的 ``Module`` 类加载；存在 ``frontend.py`` 时，可选界面固定
从其中的 ``Frontend`` 类加载。DAT 列由后端类的 ``columns`` 属性提供，避免同一信息在
清单和代码中维护两份。
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from ..config import AppConfig


MAX_MODULE_ID_LENGTH = 64
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
MODULE_INSTANCES_FILENAME = "module_instances.toml"


def is_valid_module_id(value: str) -> bool:
    """校验模块/实例 ID：小写字母开头，仅含小写字母/数字/下划线，且不超过 64 字符。"""

    return (
        bool(value)
        and len(value) <= MAX_MODULE_ID_LENGTH
        and _IDENTIFIER.fullmatch(value) is not None
    )


@dataclass(frozen=True, slots=True)
class ModuleColumn:
    name: str
    unit: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}({self.unit})" if self.unit else self.name


@dataclass(frozen=True, slots=True)
class ModuleInstanceConfig:
    """``configs/module_instances.toml`` 中声明的同源码复用实例。"""

    id: str
    module: str
    name: str


@dataclass(slots=True)
class ModuleDescriptor:
    id: str
    name: str
    version: str
    path: Path
    columns: tuple[ModuleColumn, ...] = ()
    # ``display_columns`` 只选择主窗口紧凑卡片要显示的既有 DAT 列。它在
    # Enable 后由可信 worker 握手填入；不会触发额外测量，也不改变 DAT Schema。
    display_columns: tuple[str, ...] = ()
    valid: bool = True
    error: str = ""
    template_id: str = ""

    @property
    def can_enable(self) -> bool:
        return self.valid

    @property
    def is_instance(self) -> bool:
        return bool(self.template_id)


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
    """读取最小清单；仪表策略和生命周期细节均不属于清单。"""

    manifest_path = path / "module.toml"
    try:
        with manifest_path.open("rb") as handle:
            raw = tomllib.load(handle)
        module_id = path.name.casefold()
        name = str(raw["name"]).strip()
        version = str(raw["version"]).strip()
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as exc:
        return _invalid(path, f"Cannot read module.toml: {exc}")

    descriptor = ModuleDescriptor(
        id=module_id,
        name=name,
        version=version,
        path=path.resolve(),
    )
    errors: list[str] = []
    unknown_fields = sorted(set(raw) - {"name", "version"})
    if unknown_fields:
        errors.append(
            "unknown module.toml fields: " + ", ".join(unknown_fields)
        )
    if path.name != module_id or not _IDENTIFIER.fullmatch(module_id):
        errors.append("module directory name must match [a-z][a-z0-9_]*")
    elif len(module_id) > MAX_MODULE_ID_LENGTH:
        errors.append(
            f"module directory name must be at most {MAX_MODULE_ID_LENGTH} characters"
        )
    if not name:
        errors.append("name must not be empty")
    try:
        Version(version)
    except InvalidVersion:
        errors.append(f"version {version!r} is invalid")
    if not (path / "backend.py").is_file():
        errors.append("backend.py does not exist")
    if errors:
        descriptor.valid = False
        descriptor.error = "; ".join(dict.fromkeys(errors))
    return descriptor


def module_instances_path(config: AppConfig) -> Path:
    """返回同源码多实例配置文件路径（与 ``general.toml`` 同目录）。"""

    return config.source_path.parent / MODULE_INSTANCES_FILENAME


def _validate_instance_fields(instance: ModuleInstanceConfig) -> list[str]:
    errors: list[str] = []
    if not _IDENTIFIER.fullmatch(instance.id):
        errors.append("instance id must match [a-z][a-z0-9_]*")
    elif len(instance.id) > MAX_MODULE_ID_LENGTH:
        errors.append(
            f"instance id must be at most {MAX_MODULE_ID_LENGTH} characters"
        )
    if not _IDENTIFIER.fullmatch(instance.module):
        errors.append("source module id must match [a-z][a-z0-9_]*")
    elif len(instance.module) > MAX_MODULE_ID_LENGTH:
        errors.append(
            f"source module id must be at most {MAX_MODULE_ID_LENGTH} characters"
        )
    name = instance.name.strip()
    if not name or len(name) > 100 or any(ch in name for ch in "\r\n\0"):
        errors.append("display name must be a single line of 1–100 characters")
    return errors


def load_module_instances(path: Path) -> tuple[ModuleInstanceConfig, ...]:
    """读取用户配置的测量模块额外实例。文件不存在时返回空元组。"""

    if not path.is_file():
        return ()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    unknown_root = sorted(set(raw) - {"instances"})
    if unknown_root:
        raise ValueError(
            "unknown module_instances.toml fields: " + ", ".join(unknown_root)
        )
    entries = raw.get("instances", [])
    if not isinstance(entries, list):
        raise ValueError("instances must be an array of tables")
    result: list[ModuleInstanceConfig] = []
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"instances[{index}] must be a table")
        unknown = sorted(set(item) - {"id", "module", "name"})
        if unknown:
            raise ValueError(
                f"instances[{index}] has unknown fields: " + ", ".join(unknown)
            )
        result.append(
            ModuleInstanceConfig(
                id=str(item.get("id", "")).strip().casefold(),
                module=str(item.get("module", "")).strip().casefold(),
                name=str(item.get("name", "")).strip(),
            )
        )
    return tuple(result)


def save_module_instances(
    path: Path,
    instances: Sequence[ModuleInstanceConfig],
) -> Path:
    """校验并原子写入 ``configs/module_instances.toml``；列表为空时删除空文件。"""

    seen: set[str] = set()
    normalized: list[ModuleInstanceConfig] = []
    for raw in instances:
        entry = ModuleInstanceConfig(
            id=str(raw.id).strip().casefold(),
            module=str(raw.module).strip().casefold(),
            name=str(raw.name).strip(),
        )
        errors = _validate_instance_fields(entry)
        if errors:
            raise ValueError("; ".join(errors))
        if entry.id in seen:
            raise ValueError(f"Duplicate instance id: {entry.id}")
        seen.add(entry.id)
        normalized.append(entry)

    if not normalized:
        path.unlink(missing_ok=True)
        return path

    lines = [
        "# Extra measurement module instances sharing source code under modules/.",
        "# Managed via Measurement Modules -> Add Instance / Remove Instance.",
        "",
    ]
    for entry in normalized:
        lines.extend((
            "[[instances]]",
            f"id = {json.dumps(entry.id, ensure_ascii=False)}",
            f"module = {json.dumps(entry.module, ensure_ascii=False)}",
            f"name = {json.dumps(entry.name, ensure_ascii=False)}",
            "",
        ))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    temporary.replace(path)
    return path


def build_instance_descriptor(
    template: ModuleDescriptor | None,
    instance: ModuleInstanceConfig,
    *,
    fallback_path: Path,
) -> ModuleDescriptor:
    """从基础目录模块构造一个共享源码但独立运行与命名的实例描述符。"""

    errors = _validate_instance_fields(instance)
    if template is None:
        errors.append(f"Source module {instance.module!r} is not installed")
        return ModuleDescriptor(
            id=instance.id or "invalid_instance",
            name=instance.name or f"{instance.id or 'instance'} (Invalid)",
            version="—",
            path=fallback_path.resolve(),
            valid=False,
            error="; ".join(dict.fromkeys(errors)),
            template_id=instance.module or "unknown",
        )
    if not template.valid:
        errors.append(f"Source module {template.id!r} is invalid: {template.error}")
    descriptor = ModuleDescriptor(
        id=instance.id,
        name=instance.name or instance.id,
        version=template.version,
        path=template.path,
        template_id=template.id,
    )
    if errors:
        descriptor.valid = False
        descriptor.error = "; ".join(dict.fromkeys(errors))
    return descriptor


def discover_modules(config: AppConfig) -> tuple[ModuleDescriptor, ...]:
    root = config.resolve_project_path(config.modules.directory)
    root.mkdir(parents=True, exist_ok=True)
    descriptors = [
        load_manifest(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir() and (path / "module.toml").exists()
    ]
    base_by_id = {descriptor.id: descriptor for descriptor in descriptors}

    instances_file = module_instances_path(config)
    try:
        instances = load_module_instances(instances_file)
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        descriptors.append(
            ModuleDescriptor(
                id="module_instances",
                name="module_instances.toml (Invalid)",
                version="—",
                path=instances_file.resolve(),
                valid=False,
                error=f"Cannot read {MODULE_INSTANCES_FILENAME}: {exc}",
            )
        )
    else:
        for instance in instances:
            descriptors.append(
                build_instance_descriptor(
                    base_by_id.get(instance.module),
                    instance,
                    fallback_path=instances_file,
                )
            )

    seen: dict[str, ModuleDescriptor] = {}
    for descriptor in descriptors:
        duplicate = seen.get(descriptor.id)
        if duplicate is None:
            seen[descriptor.id] = descriptor
            continue
        message = f"Duplicate module id: {descriptor.id}"
        for item in (duplicate, descriptor):
            item.valid = False
            item.error = "; ".join(part for part in (item.error, message) if part)
    return tuple(descriptors)


__all__ = [
    "MAX_MODULE_ID_LENGTH",
    "MODULE_INSTANCES_FILENAME",
    "ModuleColumn",
    "ModuleDescriptor",
    "ModuleInstanceConfig",
    "build_instance_descriptor",
    "discover_modules",
    "is_valid_module_id",
    "load_manifest",
    "load_module_instances",
    "module_instances_path",
    "save_module_instances",
]
