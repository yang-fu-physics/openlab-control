"""Copy this file to src/labcontrol_plugins and rename the class.

This template deliberately contains no real protocol commands.
"""

from __future__ import annotations

import asyncio
import time

from labcontrol.devices.base import DeviceError, DevicePlugin, DeviceWarning
from labcontrol.models import DeviceActivity, DeviceSnapshot


class MyController(DevicePlugin):
    api_version = "1.0"

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        self._connected = False
        self._current: float | None = None
        self._target: float | None = None
        self._rate = config.default_rate_per_minute
        # Read plugin-specific settings from config.extras.
        self._address = str(config.extras.get("address", "TCPIP::example"))
        self._transport = None

    async def connect(self) -> None:
        # If a vendor library is blocking, wrap it with asyncio.to_thread:
        # self._transport = await asyncio.to_thread(open_vendor_device, self._address)
        raise DeviceError("请先实现真实连接", "DRIVER_NOT_IMPLEMENTED", self._address)

    async def disconnect(self) -> None:
        if self._transport is not None:
            # await asyncio.to_thread(self._transport.close)
            self._transport = None
        self._connected = False

    async def poll(self) -> DeviceSnapshot:
        if not self._connected:
            raise DeviceError("设备未连接", "NOT_CONNECTED", self._address)
        try:
            # current = await asyncio.to_thread(self._transport.read_value)
            current = float(self._current or 0.0)
        except TimeoutError as exc:
            raise DeviceWarning("本次读取超时", "READ_TIMEOUT", self._address) from exc
        return DeviceSnapshot(
            device_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=time.monotonic(),
            connected=True,
            unit=self.config.unit,
            current=current,
            target=self._target,
            rate_per_minute=self._rate,
            activity=DeviceActivity.HOLDING,
        )

    async def set_target(self, value: float, rate_per_minute: float, mode: str = "Settle") -> None:
        if not self._connected:
            raise DeviceError("设备未连接", "NOT_CONNECTED", self._address)
        # Safety limits have already been checked by DeviceManager. The plugin
        # should still validate any vendor-specific constraints.
        # await asyncio.to_thread(self._transport.set_target, value, rate_per_minute, mode)
        self._target = value
        self._rate = rate_per_minute

    async def hold(self) -> None:
        if not self._connected:
            raise DeviceError("设备未连接", "NOT_CONNECTED", self._address)
        if self._current is None:
            raise DeviceError("没有有效当前值，无法保持", "NO_CURRENT_VALUE", self._address)
        # await asyncio.to_thread(self._transport.set_target, self._current, self._rate, "Hold")
        self._target = self._current
