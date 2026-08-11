from __future__ import annotations

import time

from labcontrol.devices.base import DeviceError, DevicePlugin
from labcontrol.models import DeviceActivity, DeviceSnapshot


class ExampleMonitor(DevicePlugin):
    """Read-only skeleton for a stage thermometer, pressure, or level."""

    api_version = "1.1"

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        self._transport = None
        self._address = str(config.extras.get("address", "")).strip()

    async def connect(self) -> None:
        raise DeviceError(
            "Real read-only transport is not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )

    async def disconnect(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            pass

    async def poll(self) -> DeviceSnapshot:
        if self._transport is None:
            raise DeviceError(
                "Device is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        # Replace with one bounded read; never send control commands here.
        value = 0.0
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
