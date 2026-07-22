from __future__ import annotations

from abc import ABC, abstractmethod
from ..config import DeviceConfig
from ..models import DeviceSnapshot


class DeviceError(RuntimeError):
    """A fatal device condition that should abort the active sequence."""

    def __init__(self, message: str, code: str = "DEVICE_ERROR", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class DeviceWarning(RuntimeError):
    """A recoverable condition that should be shown while execution continues."""

    def __init__(self, message: str, code: str = "DEVICE_WARNING", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class SafetyViolation(DeviceError):
    pass


class DevicePlugin(ABC):
    """Base contract implemented by every device plugin.

    A plugin instance is owned by one runtime event loop. Drivers should not
    create GUI objects and should keep all protocol details behind this API.
    """

    api_version = "1.0"

    def __init__(self, config: DeviceConfig, simulation_speed: float = 1.0) -> None:
        self.config = config
        self.simulation_speed = simulation_speed

    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def poll(self) -> DeviceSnapshot:
        raise NotImplementedError

    async def set_target(self, value: float, rate_per_minute: float, mode: str = "Settle") -> None:
        raise DeviceError(f"Device {self.config.id} does not support setting a target", "UNSUPPORTED_SET_TARGET")

    async def hold(self) -> None:
        """Stop changing the controlled quantity and maintain the present value."""
        raise DeviceError(f"Device {self.config.id} does not support hold", "UNSUPPORTED_HOLD")
