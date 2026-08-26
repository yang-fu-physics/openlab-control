"""源码版与冻结版共用的项目路径解析。"""

from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    """返回源码检出根目录，或冻结应用的 EXE 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    """Return the single application configuration entry point."""

    return project_root() / "configs" / "general.toml"
