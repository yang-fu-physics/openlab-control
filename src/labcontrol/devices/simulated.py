from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Mapping

from ..models import DeviceActivity, DeviceKind, DeviceSnapshot
from .base import DeviceError, DevicePlugin


class _SimulatedRampController(DevicePlugin):
    expected_kind: DeviceKind

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        if config.kind is not self.expected_kind:
            raise ValueError(f"{type(self).__name__} cannot be used for {config.kind.value}")
        self._connected = False
        self._current = config.initial_value
        self._target = config.initial_value
        self._rate = config.default_rate_per_minute
        self._activity = DeviceActivity.DISCONNECTED
        self._last_poll = time.monotonic()
        self._random = random.Random(f"{config.id}-openlab")
        self._noise = float(config.extras.get("noise", 0.0))

    async def connect(self) -> None:
        await asyncio.sleep(0.03)
        self._connected = True
        self._activity = DeviceActivity.HOLDING
        self._last_poll = time.monotonic()

    async def disconnect(self) -> None:
        self._connected = False
        self._activity = DeviceActivity.DISCONNECTED

    async def poll(self) -> DeviceSnapshot:
        if not self._connected:
            raise DeviceError("Device is not connected", "NOT_CONNECTED")
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_poll)
        self._last_poll = now
        difference = self._target - self._current
        max_step = self._rate / 60.0 * elapsed * max(self.simulation_speed, 0.001)
        if abs(difference) <= max_step or max_step == 0:
            self._current = self._target
            self._activity = DeviceActivity.HOLDING
        else:
            self._current += math.copysign(max_step, difference)
            self._activity = DeviceActivity.MOVING
        observed = self._current + self._random.gauss(0.0, self._noise)
        return DeviceSnapshot(
            device_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=now,
            connected=True,
            unit=self.config.unit,
            current=observed,
            target=self._target,
            rate_per_minute=self._rate,
            activity=self._activity,
        )

    async def set_target(self, value: float, rate_per_minute: float, mode: str = "Settle") -> None:
        if not self._connected:
            raise DeviceError("Device is not connected", "NOT_CONNECTED")
        self._target = value
        self._rate = rate_per_minute
        self._activity = DeviceActivity.MOVING

    async def hold(self) -> None:
        self._target = self._current
        self._activity = DeviceActivity.HOLDING


class SimulatedTemperatureController(_SimulatedRampController):
    expected_kind = DeviceKind.TEMPERATURE


class SimulatedFieldController(_SimulatedRampController):
    expected_kind = DeviceKind.FIELD


class SimulatedReadOnlyMonitor(DevicePlugin):
    """A numerical readback with no target, hold, or measurement command."""

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        if config.kind is not DeviceKind.MONITOR:
            raise ValueError("SimulatedReadOnlyMonitor can only be used for monitor devices")
        self._connected = False
        self._value = config.initial_value
        self._random = random.Random(f"{config.id}-openlab")
        self._noise = float(config.extras.get("noise", 0.0))

    async def connect(self) -> None:
        await asyncio.sleep(0.03)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def poll(self) -> DeviceSnapshot:
        if not self._connected:
            raise DeviceError("Device is not connected", "NOT_CONNECTED")
        return DeviceSnapshot(
            device_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=time.monotonic(),
            connected=True,
            unit=self.config.unit,
            current=self._value + self._random.gauss(0.0, self._noise),
            activity=DeviceActivity.IDLE,
        )


class SimulatedResistanceMeter(DevicePlugin):
    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        if config.kind is not DeviceKind.MEASUREMENT:
            raise ValueError("SimulatedResistanceMeter can only be used for measurement devices")
        self._connected = False
        self._channels = {channel: None for channel in config.channels}
        self._random = random.Random(f"{config.id}-openlab")
        self._noise = float(config.extras.get("noise", 0.0005))
        self._delay = float(config.extras.get("measurement_delay_seconds", 0.02))
        self._measuring = False

    async def connect(self) -> None:
        await asyncio.sleep(0.03)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def poll(self) -> DeviceSnapshot:
        if not self._connected:
            raise DeviceError("Device is not connected", "NOT_CONNECTED")
        return DeviceSnapshot(
            device_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=time.monotonic(),
            connected=True,
            unit=self.config.unit,
            activity=DeviceActivity.MEASURING if self._measuring else DeviceActivity.IDLE,
            channels=dict(self._channels),
        )

    async def measure(self, context: Mapping[str, DeviceSnapshot]) -> dict[str, float | None]:
        if not self._connected:
            raise DeviceError("Device is not connected", "NOT_CONNECTED")
        self._measuring = True
        try:
            await asyncio.sleep(self._delay)
            temperature = next(
                (item.current for item in context.values() if item.kind is DeviceKind.TEMPERATURE and item.current is not None),
                300.0,
            )
            field = next(
                (item.current for item in context.values() if item.kind is DeviceKind.FIELD and item.current is not None),
                0.0,
            )
            for index, channel in enumerate(self.config.channels, start=1):
                base = (0.05 * index) + (0.003 * float(temperature))
                magnetoresistance = (0.01 * index) * float(field) ** 2
                self._channels[channel] = base + magnetoresistance + self._random.gauss(0.0, self._noise)
            return dict(self._channels)
        finally:
            self._measuring = False
