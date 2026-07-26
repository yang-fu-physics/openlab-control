"""跨 UI、运行时、设备与日志边界传递的轻量状态模型。

这些对象不持有仪表、线程或文件句柄。运行时通过消息队列传递它们，UI 只消费状态快照，
从而避免把可变设备对象暴露到 Qt 线程。
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


class DeviceKind(str, Enum):
    """框架认识的设备功能类型。"""

    TEMPERATURE = "temperature"
    FIELD = "field"
    MONITOR = "monitor"


class DeviceRole(str, Enum):
    """同类设备中的职责；目前每类最多一个 primary 参与标准控制。"""

    PRIMARY = "primary"
    SECONDARY = "secondary"
    MONITOR = "monitor"


class DeviceActivity(str, Enum):
    """设备当前动作，用于状态展示和运行快照。"""

    DISCONNECTED = "disconnected"
    IDLE = "idle"
    MOVING = "moving"
    HOLDING = "holding"
    FAULT = "fault"


class DeviceConnectionState(str, Enum):
    """设备连接生命周期；与一次读数的 ``connected`` 标志分开保存。"""

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


@dataclass(slots=True)
class DeviceSnapshot:
    """某一时刻的设备读数快照。

    ``timestamp`` 使用 ``time.monotonic()``，只用于新鲜度和超时计算；写入 DAT 的绝对时间由
    日志层另行生成，不能把两种时钟混用。
    """

    device_id: str
    display_name: str
    kind: DeviceKind
    timestamp: float
    connected: bool
    unit: str = ""
    current: float | None = None
    target: float | None = None
    rate_per_minute: float | None = None
    activity: DeviceActivity = DeviceActivity.IDLE
    stability: StabilityState = StabilityState.NOT_APPLICABLE
    message: str = ""
    connection_state: DeviceConnectionState = DeviceConnectionState.CONNECTED


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
