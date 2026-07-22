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
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        value = tomllib.load(handle)
    return dict(value)


def _toml_value(value: Any) -> str:
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
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"Invalid module setting key: {value!r}")
    return value if _BARE_KEY.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _render_table(values: Mapping[str, Any], prefix: tuple[str, ...], lines: list[str]) -> None:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _render_table(dict(settings), (), lines)
    text = "\n".join(lines).rstrip() + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
