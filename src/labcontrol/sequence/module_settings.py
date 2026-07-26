"""SEQ 同名 Measurement Module 设置配套文件。

``sample.seq`` 对应 ``sample.modules.toml``。加载只产生“期望设置”快照，不会 Enable、连接
或 Apply；版本不匹配、格式损坏或超过大小/数量限制时整份拒绝，SEQ 文本仍可独立打开。
运行快照中的规范 ``sequence.seq`` 还可回退读取同目录 ``module_settings`` 子目录。
"""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..measurement.settings import load_settings, save_settings


SEQUENCE_MODULE_SETTINGS_FORMAT = 1
MAX_SEQUENCE_MODULE_SETTINGS_BYTES = 1024 * 1024
MAX_SEQUENCE_MODULES = 128
_MODULE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_RUN_SETTINGS_SUFFIX = ".settings.toml"


@dataclass(frozen=True, slots=True)
class SequenceModuleSettings:
    """一次 SEQ 设置导入的不可变结果描述。

    ``settings`` 仅表示要装入模块 Settings 页的期望值，不表示这些值已经发送给仪表。
    ``versions`` 用于在 UI 层提醒模块版本变化；缺少版本时仍可导入，并由模块后端在
    Apply Settings 时执行最终校验。
    """

    settings: dict[str, dict[str, Any]]
    versions: dict[str, str]
    source: Path | None = None
    issues: tuple[str, ...] = ()


def sequence_module_settings_path(sequence_path: str | Path) -> Path:
    """返回与 ``sample.seq`` 对应的 ``sample.modules.toml``。"""

    return Path(sequence_path).resolve().with_suffix(".modules.toml")


def _empty_result(
    source: Path | None = None,
    *issues: str,
) -> SequenceModuleSettings:
    return SequenceModuleSettings({}, {}, source, tuple(issues))


def _setting_issue(
    value: Any,
    location: str,
    *,
    depth: int = 0,
) -> str | None:
    """检查伴随文件中的值是否属于模块设置允许的无代码数据类型。"""

    if depth > 32:
        return f"{location} is nested too deeply"
    if isinstance(value, (bool, int, str)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return f"{location} cannot contain NaN or infinity"
        return None
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or not key or "\n" in key or "\r" in key:
                return f"{location} contains an invalid setting key"
            issue = _setting_issue(
                nested,
                f"{location}.{key}",
                depth=depth + 1,
            )
            if issue:
                return issue
        return None
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            # save_settings 的 TOML 数组只支持标量或嵌套数组；表必须使用 Mapping，
            # 不能藏在数组中，否则保存和再次载入会产生不同结构。
            if isinstance(nested, Mapping):
                return (
                    f"{location}[{index}] is a table inside an array, "
                    "which module settings do not support"
                )
            issue = _setting_issue(
                nested,
                f"{location}[{index}]",
                depth=depth + 1,
            )
            if issue:
                return issue
        return None
    return f"{location} has unsupported type {type(value).__name__}"


def _load_sidecar(path: Path) -> SequenceModuleSettings:
    try:
        if path.stat().st_size > MAX_SEQUENCE_MODULE_SETTINGS_BYTES:
            return _empty_result(
                path,
                f"{path.name} exceeds the 1 MiB module settings limit",
            )
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return _empty_result(
            path,
            f"Cannot read {path.name}: {exc}",
        )

    issues: list[str] = []
    unexpected = sorted(
        set(raw) - {"format_version", "modules"}
    )
    if unexpected:
        issues.append(
            "Unsupported top-level keys: "
            + ", ".join(unexpected)
        )
    version = raw.get("format_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != SEQUENCE_MODULE_SETTINGS_FORMAT
    ):
        issues.append(
            "format_version must be "
            f"{SEQUENCE_MODULE_SETTINGS_FORMAT}"
        )
    raw_modules = raw.get("modules", {})
    if not isinstance(raw_modules, Mapping):
        issues.append("modules must be a TOML table")
        raw_modules = {}
    if len(raw_modules) > MAX_SEQUENCE_MODULES:
        issues.append(
            f"modules contains more than {MAX_SEQUENCE_MODULES} entries"
        )

    settings: dict[str, dict[str, Any]] = {}
    versions: dict[str, str] = {}
    for module_id, raw_entry in sorted(
        raw_modules.items(),
        key=lambda item: str(item[0]),
    ):
        if (
            not isinstance(module_id, str)
            or not _MODULE_ID.fullmatch(module_id)
        ):
            issues.append(
                f"Invalid module id in {path.name}: {module_id!r}"
            )
            continue
        if not isinstance(raw_entry, Mapping):
            issues.append(
                f"modules.{module_id} must be a TOML table"
            )
            continue
        entry_keys = set(raw_entry)
        unexpected_entry = sorted(
            entry_keys - {"version", "settings"}
        )
        if unexpected_entry:
            issues.append(
                f"modules.{module_id} has unsupported keys: "
                + ", ".join(unexpected_entry)
            )
        module_version = raw_entry.get("version", "")
        if (
            not isinstance(module_version, str)
            or "\n" in module_version
            or "\r" in module_version
            or len(module_version) > 128
        ):
            issues.append(
                f"modules.{module_id}.version must be "
                "single-line text no longer than 128 characters"
            )
            continue
        raw_settings = raw_entry.get("settings")
        if not isinstance(raw_settings, Mapping):
            issues.append(
                f"modules.{module_id}.settings must be a TOML table"
            )
            continue
        setting_values = dict(raw_settings)
        issue = _setting_issue(
            setting_values,
            f"modules.{module_id}.settings",
        )
        if issue:
            issues.append(issue)
            continue
        settings[module_id] = deepcopy(setting_values)
        versions[module_id] = module_version.strip()

    # 伴随文件用于切换实验参数。任何结构错误都拒绝整个文件，避免只导入一半模块后
    # 用户误以为所有模块都已恢复。错误只阻止设置导入，不阻止旧 SEQ 本身打开。
    if issues:
        return _empty_result(path, *issues)
    return SequenceModuleSettings(
        settings,
        versions,
        path,
    )


def _load_run_snapshot(
    sequence_path: Path,
) -> SequenceModuleSettings:
    """兼容运行目录中的 ``sequence.seq`` + ``module_settings/`` 快照。"""

    directory = sequence_path.parent / "module_settings"
    if (
        sequence_path.name.casefold() != "sequence.seq"
        or not directory.is_dir()
    ):
        return _empty_result()

    files = sorted(directory.glob(f"*{_RUN_SETTINGS_SUFFIX}"))
    if len(files) > MAX_SEQUENCE_MODULES:
        return _empty_result(
            directory,
            f"module_settings contains more than {MAX_SEQUENCE_MODULES} files",
        )

    settings: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for path in files:
        module_id = path.name[: -len(_RUN_SETTINGS_SUFFIX)]
        if not _MODULE_ID.fullmatch(module_id):
            issues.append(
                f"Ignored invalid run snapshot name: {path.name}"
            )
            continue
        try:
            if path.stat().st_size > MAX_SEQUENCE_MODULE_SETTINGS_BYTES:
                raise ValueError("file exceeds 1 MiB")
            values = load_settings(path)
            issue = _setting_issue(
                values,
                f"{path.name}",
            )
            if issue:
                raise ValueError(issue)
        except (
            OSError,
            TypeError,
            ValueError,
            tomllib.TOMLDecodeError,
        ) as exc:
            issues.append(
                f"Cannot import {path.name}: {exc}"
            )
            continue
        settings[module_id] = deepcopy(values)
    return SequenceModuleSettings(
        settings,
        {},
        directory,
        tuple(issues),
    )


def load_sequence_module_settings(
    sequence_path: str | Path,
) -> SequenceModuleSettings:
    """读取 SEQ 的设置伴随文件；旧 SEQ 没有伴随文件时返回空结果。

    同名 ``.modules.toml`` 始终优先。只有打开运行快照的标准文件名
    ``sequence.seq`` 时，才会回退到同目录的 ``module_settings/``，防止普通目录中
    恰好存在同名文件夹而导入无关实验设置。
    """

    source = Path(sequence_path).resolve()
    sidecar = sequence_module_settings_path(source)
    if sidecar.exists():
        return _load_sidecar(sidecar)
    return _load_run_snapshot(source)


def save_sequence_module_settings(
    sequence_path: str | Path,
    module_settings: Mapping[str, Mapping[str, Any]],
    module_versions: Mapping[str, str] | None = None,
) -> Path:
    """原子保存当前 SEQ 关联的模块设置，但不触发 Enable 或 Apply。"""

    versions = dict(module_versions or {})
    modules: dict[str, dict[str, Any]] = {}
    if len(module_settings) > MAX_SEQUENCE_MODULES:
        raise ValueError(
            f"A sequence can associate at most {MAX_SEQUENCE_MODULES} modules"
        )
    for module_id in sorted(module_settings):
        if not _MODULE_ID.fullmatch(module_id):
            raise ValueError(
                f"Invalid module id for sequence settings: {module_id!r}"
            )
        values = module_settings[module_id]
        if not isinstance(values, Mapping):
            raise TypeError(
                f"Settings for {module_id} must be a mapping"
            )
        copied = deepcopy(dict(values))
        issue = _setting_issue(
            copied,
            f"modules.{module_id}.settings",
        )
        if issue:
            raise ValueError(issue)
        module_version = versions.get(module_id, "")
        if (
            not isinstance(module_version, str)
            or "\n" in module_version
            or "\r" in module_version
            or len(module_version) > 128
        ):
            raise TypeError(
                f"Version for {module_id} must be "
                "single-line text no longer than 128 characters"
            )
        modules[module_id] = {
            "version": module_version.strip(),
            "settings": copied,
        }

    destination = sequence_module_settings_path(sequence_path)
    save_settings(
        destination,
        {
            "format_version": (
                SEQUENCE_MODULE_SETTINGS_FORMAT
            ),
            "modules": modules,
        },
    )
    return destination
