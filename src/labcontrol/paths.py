"""源码版与冻结版共用的项目路径解析。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


DEFAULT_CONFIG_FILENAME = "general.toml"
DEFAULT_CONFIG_TEMPLATE_FILENAME = "general.default.toml"


def project_root() -> Path:
    """返回源码检出根目录，或冻结应用的 EXE 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    """Return the single application configuration entry point."""

    return project_root() / "configs" / DEFAULT_CONFIG_FILENAME


def default_config_template_path() -> Path:
    """Return the packaged default template used to seed ``configs/general.toml``."""

    return project_root() / "configs" / DEFAULT_CONFIG_TEMPLATE_FILENAME


def ensure_default_config(
    config_path: Path | None = None,
    template_path: Path | None = None,
) -> Path:
    """在首次启动且缺少 ``configs/general.toml`` 时，从模板复制一份本地配置。

    已有配置文件（包括符号链接或目录）永远不会被覆盖；若模板不存在则原样返回目标路径，
    由后续 :func:`~labcontrol.config.load_config` 统一报错。
    """

    target = default_config_path() if config_path is None else Path(config_path)
    template = (
        target.parent / DEFAULT_CONFIG_TEMPLATE_FILENAME
        if template_path is None
        else Path(template_path)
    )
    if target.exists() or target.is_symlink() or not template.is_file():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    content = template.read_bytes()
    temp_path: Path | None = None
    try:
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temp_path = Path(raw_temp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, target)
            return target
        except FileExistsError:
            return target
        except OSError:
            pass
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        with target.open("xb") as handle:
            handle.write(content)
            handle.flush()
    except FileExistsError:
        pass
    return target
