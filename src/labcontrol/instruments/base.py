"""System Instrument 必须实现的最小同步协议和统一异常语义。

驱动只负责把协议命令转换为仪表操作；上下限、速率、超时、并发串行化和 SEQ 控制权由核心
``InstrumentManager`` 再强制执行。系统仪表后端不得创建 Qt 对象，也不得从工作进程直接修改
主程序状态。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..config import InstrumentConfig


class InstrumentError(RuntimeError):
    """不可恢复或安全相关的仪表错误；活动 SEQ 应中止。"""

    def __init__(self, message: str, code: str = "INSTRUMENT_ERROR", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class InstrumentWarning(RuntimeError):
    """可恢复的仪表警告；展示并去重，但允许当前 SEQ 继续。"""

    def __init__(self, message: str, code: str = "INSTRUMENT_WARNING", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class SafetyViolation(InstrumentError):
    """目标值、速率或操作违反框架安全边界。"""


class SystemInstrument(ABC):
    """每个系统仪表后端必须实现的基础契约。

    后端运行在独立进程中，所以作者只需编写普通同步 Python。协议、端口和仪表命令必须
    封装在本接口之后，不能创建 GUI 对象或把可变底层句柄暴露给主程序。
    """

    def __init__(self, config: InstrumentConfig) -> None:
        """保存已验证配置；真实连接应推迟到 :meth:`open`。"""

        self.config = config

    @abstractmethod
    def open(self) -> None:
        """建立通信并完成最小身份/状态检查。"""

        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """释放仪表会话；实现应允许在部分初始化后调用。"""

        raise NotImplementedError

    @abstractmethod
    def read_status(self) -> dict[str, Any]:
        """返回主值、控制状态和选中的附加读数。"""

        raise NotImplementedError

    def read_measurement(self) -> dict[str, Any]:
        """返回写测量数据前使用的即时主值。

        默认仍执行完整 :meth:`read_status`，所以普通仪表无需实现第二套读取。若完整状态查询
        很慢，后端可以覆盖本方法，只返回 ``value``；后台 ``read_status()`` 仍负责安全检查。
        """

        return self.read_status()

    def set_target(self, value: float, rate_per_minute: float, mode: str = "Settle") -> None:
        """设置受控量；默认实现明确拒绝只读仪表。"""

        raise InstrumentError(f"Instrument {self.config.id} does not support setting a target", "UNSUPPORTED_SET_TARGET")

    def hold(self) -> None:
        """停止改变受控量并保持当前状态；默认实现明确拒绝不支持的仪表。"""
        raise InstrumentError(f"Instrument {self.config.id} does not support hold", "UNSUPPORTED_HOLD")
