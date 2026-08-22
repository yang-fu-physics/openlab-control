"""内置温度、磁场和只读监视器模拟仪表。

模拟器使用与真实系统仪表完全相同的 ``SystemInstrument`` 接口，可验证斜坡、稳定性、SEQ、DAT 和
关闭流程。随机噪声按仪表 ID 固定种子，确保测试可重复；它不代表具体真实仪表。
"""

from __future__ import annotations
import math
import random
import time
from ..models import InstrumentKind
from .base import InstrumentError, SystemInstrument


class _SimulatedRampController(SystemInstrument):
    """按目标和每分钟速率推进读数的通用模拟斜坡控制器。"""

    expected_kind: InstrumentKind

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config)
        self.simulation_speed = simulation_speed
        if config.kind is not self.expected_kind:
            raise ValueError(f"{type(self).__name__} cannot be used for {config.kind.value}")
        self._connected = False
        self._current = config.initial_value
        self._target = config.initial_value
        self._rate = config.default_rate_per_minute
        self._last_poll = time.monotonic()
        self._random = random.Random(f"{config.id}-openlab")
        self._noise = float(config.extras.get("noise", 0.0))

    def open(self) -> None:
        time.sleep(0.03)
        self._connected = True
        self._last_poll = time.monotonic()

    def close(self) -> None:
        self._connected = False

    def read_status(self) -> dict[str, object]:
        if not self._connected:
            raise InstrumentError("Instrument is not connected", "NOT_CONNECTED")
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_poll)
        self._last_poll = now
        difference = self._target - self._current
        max_step = self._rate / 60.0 * elapsed * max(self.simulation_speed, 0.001)
        if abs(difference) <= max_step or max_step == 0:
            self._current = self._target
        else:
            self._current += math.copysign(max_step, difference)
        observed = self._current + self._random.gauss(0.0, self._noise)
        return {
            "value": observed,
            "target": self._target,
            "rate": self._rate,
            "moving": self._current != self._target,
        }

    def set_target(self, value: float, rate_per_minute: float, mode: str = "Settle") -> None:
        if not self._connected:
            raise InstrumentError("Instrument is not connected", "NOT_CONNECTED")
        self._target = value
        self._rate = rate_per_minute

    def hold(self) -> None:
        if not self._connected:
            raise InstrumentError("Instrument is not connected", "NOT_CONNECTED")
        self._target = self._current


class SimulatedTemperatureController(_SimulatedRampController):
    """温度主控模拟器。"""

    expected_kind = InstrumentKind.TEMPERATURE


class SimulatedFieldController(_SimulatedRampController):
    """磁场主控模拟器。"""

    expected_kind = InstrumentKind.FIELD


class SimulatedReadOnlyMonitor(SystemInstrument):
    """只有数值回读、不支持目标、Hold 或测量命令的显示型仪表。"""

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config)
        if config.kind is not InstrumentKind.MONITOR:
            raise ValueError("SimulatedReadOnlyMonitor can only be used for monitor instruments")
        self._connected = False
        self._value = config.initial_value
        self._random = random.Random(f"{config.id}-openlab")
        self._noise = float(config.extras.get("noise", 0.0))

    def open(self) -> None:
        time.sleep(0.03)
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def read_status(self) -> dict[str, object]:
        if not self._connected:
            raise InstrumentError("Instrument is not connected", "NOT_CONNECTED")
        return {
            "value": self._value + self._random.gauss(0.0, self._noise),
        }
