"""受控加载内置对象或外部内容目录中的 Python 对象。

内置组件使用普通 ``package.module:Class`` 导入。外部 System Instrument 和 Measurement
Module 则放入根据绝对目录生成的独立临时包命名空间，既允许目录内相对导入，又避免两个
同名包互相污染 ``sys.modules``。
信任和目录指纹检查必须由调用方在执行本文件前完成。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType


def load_import_object(specification: str) -> object:
    """从已安装包加载 ``package.module:Object``，仅供受信任的内置路径使用。"""

    try:
        module_name, object_name = specification.split(":", 1)
    except ValueError as exc:
        raise ValueError("Backend path must use package.module:ClassName format") from exc
    module = importlib.import_module(module_name)
    return getattr(module, object_name)


def load_source_object(
    directory: Path,
    specification: str,
    namespace: str,
) -> object:
    """从一个已验证内容目录加载对象，并为该目录建立独立包命名空间。

    每次加载前清除相同命名空间的旧模块，保证重新加载或内容更新后不会继续使用缓存代码。
    源文件必须直接位于给定目录，不能通过 ``..`` 或绝对路径逃逸。
    """

    module_stem, object_name = specification.split(":", 1)
    source = directory / f"{module_stem}.py"
    if not source.is_file() or source.parent.resolve() != directory.resolve():
        raise FileNotFoundError(source)
    digest = hashlib.sha256(
        f"{namespace}\0{directory.resolve()}".encode("utf-8")
    ).hexdigest()[:16]
    safe_name = re.sub(r"[^A-Za-z0-9_]", "_", directory.name)
    package_name = f"_openlab_package_{safe_name}_{digest}"
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
