"""随框架提供的只读 System Instrument 骨架。"""

from __future__ import annotations

from labcontrol.instruments.base import InstrumentError, SystemInstrument


class ExampleMonitor(SystemInstrument):
    """Read-only skeleton for a stage thermometer, pressure, or level."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._transport = None
        self._address = config.address

    def open(self) -> None:
        raise InstrumentError(
            "Real read-only transport is not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )

    def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            pass

    def read_status(self) -> dict[str, object]:
        if self._transport is None:
            raise InstrumentError(
                "Instrument is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        # Replace with one bounded read; never send control commands here.
        value = 0.0
        return {"value": value}
