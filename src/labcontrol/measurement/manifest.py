"""发现 Measurement Module 的最小清单。

模块作者只需在 ``module.toml`` 中填写 ``name`` 和 ``version``。目录名就是模块 ID，
后端固定从 ``backend.py`` 的 ``Module`` 类加载；存在 ``frontend.py`` 时，可选界面固定
从其中的 ``Frontend`` 类加载。DAT 列由后端类的 ``columns`` 属性提供，避免同一信息在
清单和代码中维护两份。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from ..config import AppConfig


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
    columns: tuple[ModuleColumn, ...] = ()
    # ``display_columns`` 只选择主窗口紧凑卡片要显示的既有 DAT 列。它在
    # Enable 后由可信 worker 握手填入；不会触发额外测量，也不改变 DAT Schema。
    display_columns: tuple[str, ...] = ()
    valid: bool = True
    error: str = ""

    @property
    def can_enable(self) -> bool:
        return self.valid


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
        if duplicate is None:
            seen[descriptor.id] = descriptor
            continue
        message = f"Duplicate module id: {descriptor.id}"
        for item in (duplicate, descriptor):
            item.valid = False
            item.error = "; ".join(part for part in (item.error, message) if part)
    return tuple(descriptors)
__all__ = [
    "ModuleColumn",
    "ModuleDescriptor",
    "discover_modules",
    "load_manifest",
]
