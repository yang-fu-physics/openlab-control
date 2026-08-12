"""Device Plugin 必须实现的最小异步协议和统一异常语义。

驱动只负责把协议命令转换为设备操作；上下限、速率、超时、并发串行化和 SEQ 控制权由核心
``DeviceManager`` 再强制执行。插件不得创建 Qt 对象，也不得从工作进程直接修改主程序状态。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from ..config import DeviceConfig
from ..models import DeviceSnapshot


class DeviceError(RuntimeError):
    """不可恢复或安全相关的设备错误；活动 SEQ 应中止。"""

    def __init__(self, message: str, code: str = "DEVICE_ERROR", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class DeviceWarning(RuntimeError):
    """可恢复的设备警告；展示并去重，但允许当前 SEQ 继续。"""

    def __init__(self, message: str, code: str = "DEVICE_WARNING", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class SafetyViolation(DeviceError):
    """目标值、速率或操作违反框架安全边界。"""


class DevicePlugin(ABC):
    """每个设备后端必须实现的基础契约。

    一个实例只归属于一个 runtime event loop。协议、端口和仪表命令必须封装在本接口之后，
    不能创建 GUI 对象或把可变底层句柄暴露给主程序。
    """

    api_version = "1.1"

    def __init__(self, config: DeviceConfig, simulation_speed: float = 1.0) -> None:
        """保存已验证配置；真实连接应推迟到 :meth:`connect`。"""

        self.config = config
        self.simulation_speed = simulation_speed

    @abstractmethod
    async def connect(self) -> None:
        """建立通信并完成最小身份/状态检查。"""

        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """释放仪表会话；实现应允许在部分初始化后调用。"""

        raise NotImplementedError

    @abstractmethod
    async def poll(self) -> DeviceSnapshot:
        """读取并返回完整状态快照，时间戳必须使用单调时钟。"""

        raise NotImplementedError

    async def poll_measurement(self) -> DeviceSnapshot:
        """返回写测量数据前使用的即时快照。

        默认仍执行完整 :meth:`poll`，所以普通插件无需实现第二套读取。若完整状态查询很慢，
        插件可以覆盖本方法，只读取主测量值；返回值仍须保持与 ``poll()`` 相同的设备和
        ``metrics`` Schema。没有在本次查询中读取的附加值应填 ``None``，不能伪装成同一
        时刻的测量结果。安全状态仍必须由常规 ``poll()`` 持续检查。
        """

        return await self.poll()

    async def set_target(self, value: float, rate_per_minute: float, mode: str = "Settle") -> None:
        """设置受控量；默认实现明确拒绝只读设备。"""

        raise DeviceError(f"Device {self.config.id} does not support setting a target", "UNSUPPORTED_SET_TARGET")

    async def hold(self) -> None:
        """停止改变受控量并保持当前状态；默认实现明确拒绝不支持的设备。"""
        raise DeviceError(f"Device {self.config.id} does not support hold", "UNSUPPORTED_HOLD")
