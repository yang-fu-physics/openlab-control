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
    """返回默认主配置文件位置，不受启动时工作目录影响。"""

    return project_root() / "configs" / "default.toml"
