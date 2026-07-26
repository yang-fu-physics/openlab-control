from __future__ import annotations

import time

from labcontrol.devices.base import DeviceError, DevicePlugin
from labcontrol.models import DeviceActivity, DeviceSnapshot


class ExampleController(DevicePlugin):
    """Fail-closed skeleton; replace every placeholder before hardware use."""

    api_version = "1.0"

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        self._transport = None
        self._current: float | None = None
        self._target: float | None = None
        self._rate: float | None = None
        self._address = str(config.extras.get("address", "")).strip()

    async def connect(self) -> None:
        # Open with a bounded driver timeout, query *IDN? (or equivalent), and
        # verify model/firmware without changing output before setting transport.
        raise DeviceError(
            "Real transport and identity verification are not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )

    async def disconnect(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            # Close the vendor handle here. This method must be idempotent.
            pass

    async def poll(self) -> DeviceSnapshot:
        if self._transport is None:
            raise DeviceError(
                "Device is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        # Query current, target, rate, and activity using bounded reads.
        if self._current is None:
            raise DeviceError(
                "Controller returned no current value",
                "INVALID_READBACK",
                self._address,
            )
        return DeviceSnapshot(
            device_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=time.monotonic(),
            connected=True,
            unit=self.config.unit,
            current=self._current,
            target=self._target,
            rate_per_minute=self._rate,
            activity=DeviceActivity.HOLDING,
        )

    async def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
    ) -> None:
        if self._transport is None:
            raise DeviceError(
                "Device is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        if not self.config.min_value <= value <= self.config.max_value:
            raise DeviceError(
                "Target exceeds the plugin safety envelope",
                "LOCAL_TARGET_LIMIT",
                self.config.id,
            )
        if not 0 < rate_per_minute <= self.config.max_rate_per_minute:
            raise DeviceError(
                "Rate exceeds the plugin safety envelope",
                "LOCAL_RATE_LIMIT",
                self.config.id,
            )
        # Perform one bounded write. Never replay it automatically after an
        # ambiguous timeout; reconnect and verify actual target/rate instead.
        del mode
        raise DeviceError(
            "Real target command is not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )

    async def hold(self) -> None:
        if self._transport is None:
            raise DeviceError(
                "Device is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        # Obtain a fresh readback, validate it, and issue the vendor's hold or
        # current-value command once. Never use a cached/guessed zero.
        raise DeviceError(
            "Real hold command is not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )
