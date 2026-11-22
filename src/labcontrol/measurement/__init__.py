"""Measurement Module 的核心内部实现；作者只需导入 ``labcontrol.module_api``。"""

from .manifest import (
    MAX_MODULE_ID_LENGTH,
    MODULE_INSTANCES_FILENAME,
    ModuleColumn,
    ModuleDescriptor,
    ModuleInstanceConfig,
    build_instance_descriptor,
    discover_modules,
    is_valid_module_id,
    load_module_instances,
    module_instances_path,
    save_module_instances,
)

__all__ = [
    "MAX_MODULE_ID_LENGTH",
    "MODULE_INSTANCES_FILENAME",
    "ModuleColumn",
    "ModuleDescriptor",
    "ModuleInstanceConfig",
    "build_instance_descriptor",
    "discover_modules",
    "is_valid_module_id",
    "load_module_instances",
    "module_instances_path",
    "save_module_instances",
]
