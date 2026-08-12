from __future__ import annotations

import time

from labcontrol.instruments.base import InstrumentError, SystemInstrument
from labcontrol.models import InstrumentActivity, InstrumentSnapshot


class ExampleMonitor(SystemInstrument):
    """Read-only skeleton for a stage thermometer, pressure, or level."""

    api_version = "1.2"

    def __init__(self, config, simulation_speed: float = 1.0) -> None:
        super().__init__(config, simulation_speed)
        self._transport = None
        self._address = config.address

    async def connect(self) -> None:
        raise InstrumentError(
            "Real read-only transport is not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )

    async def disconnect(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            pass

    async def poll(self) -> InstrumentSnapshot:
        if self._transport is None:
            raise InstrumentError(
                "Instrument is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        # Replace with one bounded read; never send control commands here.
        value = 0.0
        return InstrumentSnapshot(
            instrument_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=time.monotonic(),
            connected=True,
            unit=self.config.unit,
            current=value,
            activity=InstrumentActivity.IDLE,
        )
