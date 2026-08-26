"""跨 UI、运行时、仪表与日志边界传递的轻量状态模型。

这些对象不持有仪表、线程或文件句柄。运行时通过消息队列传递它们，UI 只消费状态快照，
从而避免把可变仪表对象暴露到 Qt 线程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """统一事件等级；Error 中止 SEQ，Warning 锁存但允许继续。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class InstrumentKind(str, Enum):
    """框架认识的仪表功能类型。"""

    TEMPERATURE = "temperature"
    FIELD = "field"
    MONITOR = "monitor"


class InstrumentActivity(str, Enum):
    """仪表当前动作，用于状态展示和运行快照。"""

    DISCONNECTED = "disconnected"
    IDLE = "idle"
    MOVING = "moving"
    HOLDING = "holding"
    FAULT = "fault"


class InstrumentConnectionState(str, Enum):
    """仪表连接生命周期，也是 ``InstrumentSnapshot.connected`` 的唯一来源。"""

    STARTING = "starting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAULTED = "faulted"
    DISCONNECTED = "disconnected"


class StabilityState(str, Enum):
    """由框架稳定性算法计算的状态，而非直接信任仪表状态字。"""

    NOT_APPLICABLE = "not_applicable"
    MOVING = "moving"
    SETTLING = "settling"
    STABLE = "stable"
    TIMED_OUT = "timed_out"
    STALE = "stale"


class RunState(str, Enum):
    """SEQ 从空闲到终态的有限状态集合。"""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAULTED = "faulted"


@dataclass(frozen=True, slots=True)
class InstrumentMetric:
    """同一物理仪表随主快照返回的一项附加读数描述。

    一个 System Instrument 仍只拥有一个连接和一个主 ``InstrumentSnapshot``。温控器的辅助
    温度、加热功率、量程等值通过 ``snapshot.metrics`` 字典附在主快照上，避免为了显示
    第二个数值而对同一 USB/GPIB 仪表再打开一个并发会话。字典键用作稳定的 DAT 列名，
    ``display_name`` 只用于界面；``decimals`` 仅控制显示和写盘格式，不改变原始浮点值。
    """

    display_name: str
    value: float | int | str | bool | None
    unit: str = ""
    decimals: int | None = None


@dataclass(slots=True)
class InstrumentControlState:
    """一个 Controller 面板独立的当前值、目标值和稳定状态。"""

    current: float | None = None
    target: float | None = None
    rate_per_minute: float | None = None
    activity: InstrumentActivity = InstrumentActivity.IDLE
    stability: StabilityState = StabilityState.NOT_APPLICABLE
    ready: bool | None = None


@dataclass(slots=True)
class InstrumentSnapshot:
    """某一时刻的仪表读数快照。

    ``timestamp`` 使用 ``time.monotonic()``，只用于新鲜度和超时计算；写入 DAT 的绝对时间由
    日志层另行生成，不能把两种时钟混用。
    """

    instrument_id: str
    display_name: str
    kind: InstrumentKind
    timestamp: float
    unit: str = ""
    current: float | None = None
    target: float | None = None
    rate_per_minute: float | None = None
    activity: InstrumentActivity = InstrumentActivity.IDLE
    stability: StabilityState = StabilityState.NOT_APPLICABLE
    message: str = ""
    connection_state: InstrumentConnectionState = InstrumentConnectionState.CONNECTED
    ready: bool | None = None
    metrics: dict[str, InstrumentMetric] = field(default_factory=dict)
    controls: dict[str, InstrumentControlState] = field(default_factory=dict)

    @property
    def connected(self) -> bool:
        """连接状态只由 ``connection_state`` 决定，避免两份状态互相矛盾。"""

        return self.connection_state is InstrumentConnectionState.CONNECTED


@dataclass(slots=True)
class LabEvent:
    """一个可锁存事件及其重复次数、首次和最后出现时间。"""

    key: str
    severity: Severity
    source: str
    code: str
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    context: str = ""
    count: int = 1
    active: bool = True
    resolved_at: datetime | None = None


@dataclass(slots=True)
class EventNotice:
    """事件状态变化的投递包装，携带弹窗与解除标记。"""

    event: LabEvent
    show_popup: bool
    is_resolution: bool = False


@dataclass(slots=True)
class RuntimeMessage:
    """后台运行时发往 GUI/无界面调用方的消息信封。"""

    kind: str
    payload: Any


@dataclass(slots=True)
class RunProgress:
    """SEQ 进度快照；``step_path`` 可表示任意嵌套扫描中的当前位置。"""

    state: RunState
    step_path: str = ""
    message: str = ""
    completed_steps: int = 0
    total_steps: int = 0
