from __future__ import annotations

import time

from labcontrol.devices.base import DeviceError, DevicePlugin
from labcontrol.models import DeviceActivity, DeviceKind, DeviceSnapshot


class ReadOnlyMonitorPlugin(DevicePlugin):
    """Template for a display-only numerical readback such as a stage thermometer."""

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        if config.kind is not DeviceKind.MONITOR:
            raise ValueError("ReadOnlyMonitorPlugin requires kind = 'monitor'")
        self._connected = False
        self._transport = None

    async def connect(self) -> None:
        # Open the real transport and verify instrument identity here.
        self._connected = True

    async def disconnect(self) -> None:
        # Close the real transport here.
        self._connected = False

    async def poll(self) -> DeviceSnapshot:
        if not self._connected:
            raise DeviceError("Device is not connected", "NOT_CONNECTED")
        value = 0.0  # Replace with a read-only instrument query.
        return DeviceSnapshot(
            device_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=time.monotonic(),
            connected=True,
            unit=self.config.unit,
            current=value,
            activity=DeviceActivity.IDLE,
        )
