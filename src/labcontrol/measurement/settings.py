"""Measurement Module 设置的受限 TOML 读写。

这里只支持 TOML 可安全表达的标量、数组和嵌套表，拒绝 NaN、无穷和非法键。写入先生成同目录
临时文件，再原子替换正式文件，避免程序中断留下半份设置。
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def load_settings(path: Path) -> dict[str, Any]:
    """读取模块设置；文件尚不存在时返回空映射。"""

    if not path.exists():
        return {}
    with path.open("rb") as handle:
        value = tomllib.load(handle)
    return dict(value)


def _toml_value(value: Any) -> str:
    """把受支持的 Python 标量或数组编码为 TOML 字面量。"""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Module settings cannot contain NaN or infinity")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported module setting type: {type(value).__name__}")


def _toml_key(value: str) -> str:
    """编码 TOML 键；仅在必要时添加 JSON 兼容引号。"""

    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"Invalid module setting key: {value!r}")
    return value if _BARE_KEY.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _render_table(values: Mapping[str, Any], prefix: tuple[str, ...], lines: list[str]) -> None:
    """递归展开嵌套映射，并保证父表的标量先于子表输出。"""

    scalars = {key: value for key, value in values.items() if not isinstance(value, Mapping)}
    tables = {key: value for key, value in values.items() if isinstance(value, Mapping)}
    if prefix:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[" + ".".join(_toml_key(item) for item in prefix) + "]")
    for key, value in scalars.items():
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, nested in tables.items():
        _render_table(dict(nested), prefix + (key,), lines)


def save_settings(path: Path, settings: Mapping[str, Any]) -> None:
    """以 UTF-8/LF 和同目录原子替换方式保存模块设置。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _render_table(dict(settings), (), lines)
    text = "\n".join(lines).rstrip() + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
