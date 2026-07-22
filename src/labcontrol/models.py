from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DeviceKind(str, Enum):
    TEMPERATURE = "temperature"
    FIELD = "field"
    MONITOR = "monitor"


class DeviceActivity(str, Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    MOVING = "moving"
    HOLDING = "holding"
    FAULT = "fault"


class StabilityState(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    MOVING = "moving"
    SETTLING = "settling"
    STABLE = "stable"
    TIMED_OUT = "timed_out"
    STALE = "stale"


class RunState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAULTED = "faulted"


@dataclass(slots=True)
class DeviceSnapshot:
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


@dataclass(slots=True)
class LabEvent:
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
    event: LabEvent
    show_popup: bool
    is_resolution: bool = False


@dataclass(slots=True)
class RuntimeMessage:
    kind: str
    payload: Any


@dataclass(slots=True)
class RunProgress:
    state: RunState
    step_path: str = ""
    message: str = ""
    completed_steps: int = 0
    total_steps: int = 0
