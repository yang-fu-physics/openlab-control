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
    """优先返回本机现场配置，否则返回随程序提供的仿真配置。"""

    configs = project_root() / "configs"
    site_config = configs / "site.local.toml"
    if site_config.is_file():
        return site_config
    return configs / "default.toml"
