"""Copy this file to src/labcontrol_plugins and implement the protocol."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping

from labcontrol.devices.base import DeviceError, DevicePlugin, DeviceWarning
from labcontrol.models import DeviceActivity, DeviceSnapshot


class MyMeasurementDevice(DevicePlugin):
    api_version = "1.0"

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        self._connected = False
        self._values = {channel: None for channel in config.channels}
        self._address = str(config.extras.get("address", "GPIB0::1::INSTR"))
        self._transport = None

    async def connect(self) -> None:
        # self._transport = await asyncio.to_thread(open_transport, self._address)
        raise DeviceError("请先实现真实连接", "DRIVER_NOT_IMPLEMENTED", self._address)

    async def disconnect(self) -> None:
        if self._transport is not None:
            # await asyncio.to_thread(self._transport.close)
            self._transport = None
        self._connected = False

    async def poll(self) -> DeviceSnapshot:
        if not self._connected:
            raise DeviceError("设备未连接", "NOT_CONNECTED", self._address)
        return DeviceSnapshot(
            device_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=time.monotonic(),
            connected=True,
            unit=self.config.unit,
            activity=DeviceActivity.IDLE,
            channels=dict(self._values),
        )

    async def measure(self, context: Mapping[str, DeviceSnapshot]) -> dict[str, float | None]:
        if not self._connected:
            raise DeviceError("设备未连接", "NOT_CONNECTED", self._address)
        try:
            # raw = await asyncio.to_thread(self._transport.read_all_channels)
            raw = {}
        except TimeoutError as exc:
            raise DeviceWarning("测量超时，本点将继续", "MEASURE_TIMEOUT", self._address) from exc
        for channel in self.config.channels:
            self._values[channel] = raw.get(channel)
        return dict(self._values)
