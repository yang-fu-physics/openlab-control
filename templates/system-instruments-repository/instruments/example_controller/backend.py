from __future__ import annotations

from labcontrol.instruments.base import InstrumentError, SystemInstrument


class ExampleController(SystemInstrument):
    """Fail-closed skeleton; replace every placeholder before hardware use."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._transport = None
        self._current: float | None = None
        self._target: float | None = None
        self._rate: float | None = None
        self._address = config.address

    def open(self) -> None:
        # Open with a bounded driver timeout, query *IDN? (or equivalent), and
        # verify model/firmware without changing output before setting transport.
        raise InstrumentError(
            "Real transport and identity verification are not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )

    def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            # Close the vendor handle here. This method must be idempotent.
            pass

    def read_status(self) -> dict[str, object]:
        if self._transport is None:
            raise InstrumentError(
                "Instrument is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        # Query current, target, rate, and activity using bounded reads.
        if self._current is None:
            raise InstrumentError(
                "Controller returned no current value",
                "INVALID_READBACK",
                self._address,
            )
        return {
            "value": self._current,
            "target": self._target,
            "rate": self._rate,
            "moving": False,
        }

    def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
    ) -> None:
        if self._transport is None:
            raise InstrumentError(
                "Instrument is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        if not self.config.min_value <= value <= self.config.max_value:
            raise InstrumentError(
                "Target exceeds the instrument safety envelope",
                "LOCAL_TARGET_LIMIT",
                self.config.id,
            )
        if not 0 < rate_per_minute <= self.config.max_rate_per_minute:
            raise InstrumentError(
                "Rate exceeds the instrument safety envelope",
                "LOCAL_RATE_LIMIT",
                self.config.id,
            )
        # Perform one bounded write. Never replay it automatically after an
        # ambiguous timeout; reconnect and verify actual target/rate instead.
        del mode
        raise InstrumentError(
            "Real target command is not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )

    def hold(self) -> None:
        if self._transport is None:
            raise InstrumentError(
                "Instrument is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        # Obtain a fresh readback, validate it, and issue the vendor's hold or
        # current-value command once. Never use a cached/guessed zero.
        raise InstrumentError(
            "Real hold command is not implemented",
            "DRIVER_NOT_IMPLEMENTED",
            self._address,
        )
