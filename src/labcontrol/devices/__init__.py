"""Device Plugin 公共接口。

第三方驱动只应从这里或 ``devices.base`` 导入稳定基类与异常类型，不应依赖核心内部实现。
"""

from .base import DevicePlugin, DeviceError, DeviceWarning, SafetyViolation

__all__ = ["DevicePlugin", "DeviceError", "DeviceWarning", "SafetyViolation"]
