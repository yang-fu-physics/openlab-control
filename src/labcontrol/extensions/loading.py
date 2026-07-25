from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType


def load_import_object(specification: str) -> object:
    try:
        module_name, object_name = specification.split(":", 1)
    except ValueError as exc:
        raise ValueError("Plugin path must use package.module:ClassName format") from exc
    module = importlib.import_module(module_name)
    return getattr(module, object_name)


def load_source_object(
    directory: Path,
    specification: str,
    namespace: str,
) -> object:
    module_stem, object_name = specification.split(":", 1)
    source = directory / f"{module_stem}.py"
    if not source.is_file() or source.parent.resolve() != directory.resolve():
        raise FileNotFoundError(source)
    digest = hashlib.sha256(
        f"{namespace}\0{directory.resolve()}".encode("utf-8")
    ).hexdigest()[:16]
    safe_name = re.sub(r"[^A-Za-z0-9_]", "_", directory.name)
    package_name = f"_openlab_extension_{safe_name}_{digest}"
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
