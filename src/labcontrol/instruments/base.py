"""System Instrument 必须实现的最小同步协议和统一异常语义。

驱动只负责把协议命令转换为仪表操作；上下限、速率、超时、并发串行化和 SEQ 控制权由核心
``InstrumentManager`` 再强制执行。系统仪表后端不得创建 Qt 对象，也不得从工作进程直接修改
主程序状态。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class EventResponseSpec:
    """System Instrument 为自身事件注册的一条核心响应声明。

    ``source`` 由加载该后端的逻辑仪表 ID 自动绑定，仪表代码只声明稳定事件代码和动作。
    第一版仅支持把一个可控磁场仪表设为零；实际写入仍由核心执行安全限制、串行化和超时。
    """

    code: str
    action: str
    context: str = ""
    target_instrument: str = ""

    def __post_init__(self) -> None:
        if not self.code or self.code != self.code.strip():
            raise ValueError("Event response code must be non-empty and trimmed")
        if self.action != "zero":
            raise ValueError("Event response action must be zero")
        if self.context != self.context.strip():
            raise ValueError("Event response context must be trimmed")
        if self.target_instrument != self.target_instrument.strip():
            raise ValueError("Event response target_instrument must be trimmed")

    def to_payload(self) -> dict[str, str]:
        """返回可通过受限 JSON IPC 传递的纯数据。"""

        return {
            "code": self.code,
            "action": self.action,
            "context": self.context,
            "target_instrument": self.target_instrument,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> EventResponseSpec:
        """在 IPC 边界把 JSON 对象恢复为已验证声明。"""

        expected = {"code", "action", "context", "target_instrument"}
        if set(payload) != expected:
            raise ValueError(
                "Event response fields must be code, action, context, and "
                "target_instrument"
            )
        if not all(isinstance(payload[key], str) for key in expected):
            raise TypeError("Event response fields must be text")
        return cls(
            code=str(payload["code"]),
            action=str(payload["action"]),
            context=str(payload["context"]),
            target_instrument=str(payload["target_instrument"]),
        )


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
        """返回主值、附加读数以及一个或多个控制回路的状态。

        只有一个控制端点时，目标、速率、运动和 ready 状态可直接放在顶层。存在多个
        独立控制端点时，必须用 ``controls[control_id]`` 分别返回这四项；各回路的当前值
        仍由其面板选择的 ``value`` 或 ``auxiliary`` 读数提供。
        """

        raise NotImplementedError

    def read_measurement(self) -> dict[str, Any]:
        """返回写测量数据前使用的即时主值。

        默认仍执行完整 :meth:`read_status`，所以普通仪表无需实现第二套读取。若完整状态查询
        很慢，后端可以覆盖本方法，只返回 ``value``；后台 ``read_status()`` 仍负责安全检查。
        """

        return self.read_status()

    def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        control: str,
    ) -> None:
        """设置受控量；默认实现明确拒绝只读仪表。"""

        raise InstrumentError(f"Instrument {self.config.id} does not support setting a target", "UNSUPPORTED_SET_TARGET")

    def hold(self, *, control: str) -> None:
        """停止改变受控量并保持当前状态；默认实现明确拒绝不支持的仪表。"""
        raise InstrumentError(f"Instrument {self.config.id} does not support hold", "UNSUPPORTED_HOLD")

    def execute_sequence_command(self, command_id: str) -> None:
        """执行清单声明的无参数 SEQ 指令；默认实现明确拒绝。"""

        raise InstrumentError(
            f"Instrument {self.config.id} does not support sequence command "
            f"{command_id!r}",
            "UNSUPPORTED_SEQUENCE_COMMAND",
            command_id,
        )

    def event_responses(self) -> tuple[EventResponseSpec, ...]:
        """声明由核心执行的事件响应；默认不注册任何响应。"""

        return ()
