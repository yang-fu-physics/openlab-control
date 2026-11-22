"""Measurement Module 的核心内部实现；作者只需导入 ``labcontrol.module_api``。"""

from .manifest import (
    MODULE_INSTANCES_FILENAME,
    ModuleColumn,
    ModuleDescriptor,
    ModuleInstanceConfig,
    build_instance_descriptor,
    discover_modules,
    load_module_instances,
    module_instances_path,
    save_module_instances,
)

__all__ = [
    "MODULE_INSTANCES_FILENAME",
    "ModuleColumn",
    "ModuleDescriptor",
    "ModuleInstanceConfig",
    "build_instance_descriptor",
    "discover_modules",
    "load_module_instances",
    "module_instances_path",
    "save_module_instances",
]
