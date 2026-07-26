from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import math
import time
from typing import Any


class ModuleError(RuntimeError):
    """Fatal module or instrument condition that stops the active SEQ."""

    def __init__(self, message: str, code: str = "MODULE_ERROR", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class ModuleWarning(RuntimeError):
    """Recoverable measurement alarm; SEQ execution may continue."""

    def __init__(self, message: str, code: str = "MODULE_WARNING", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class ModuleOperationCancelled(RuntimeError):
    """Internal cooperative cancellation raised while a SEQ Measure is stopping."""


@dataclass(slots=True)
class ModuleOperationContext:
    """Read-only live system context and event emitters for a module worker."""

    system: Mapping[str, Mapping[str, Any]]
    _emit: Callable[[str, dict[str, Any]], None]
    _sample_system: (
        Callable[[float], Mapping[str, Mapping[str, Any]]] | None
    ) = None
    _operation_state: Callable[[float], str] | None = None
    operation_timeout_seconds: float = 120.0

    def emit_row(self, values: Mapping[str, Any]) -> None:
        self._emit("row", {"values": dict(values)})

    def update_status(self, values: Mapping[str, Any]) -> None:
        self._emit("status", {"values": dict(values)})

    def warning(self, message: str, code: str = "MODULE_WARNING", context: str = "") -> None:
        self._emit("warning", {"message": message, "code": code, "context": context})

    def resolve_warning(self, code: str = "MODULE_WARNING", context: str = "") -> None:
        self._emit("resolve", {"code": code, "context": context})

    def error(self, message: str, code: str = "MODULE_ERROR", context: str = "") -> None:
        raise ModuleError(message, code, context)

    def sample_system(
        self,
        timeout_seconds: float = 5.0,
    ) -> Mapping[str, Mapping[str, Any]]:
        """Capture the latest core-owned temperature/field/monitor snapshot.

        The returned mapping is a copy and remains read-only from the module's
        perspective. Direct backend unit tests that do not provide a live
        sampler receive a copy of the initial snapshot.
        """

        timeout = self._positive_finite(
            timeout_seconds,
            "System snapshot timeout",
        )
        self.checkpoint(timeout)
        if self._sample_system is None:
            return deepcopy(dict(self.system))
        latest = self._sample_system(timeout)
        if not isinstance(latest, Mapping):
            raise ModuleError(
                "The core returned an invalid system snapshot",
                "MODULE_SYSTEM_SNAPSHOT_INVALID",
            )
        normalized: dict[str, dict[str, Any]] = {}
        for device_id, values in latest.items():
            if not isinstance(values, Mapping):
                raise ModuleError(
                    "The core returned an invalid device snapshot",
                    "MODULE_SYSTEM_SNAPSHOT_INVALID",
                    str(device_id),
                )
            normalized[str(device_id)] = dict(values)
        self.system = normalized
        return deepcopy(normalized)

    def checkpoint(self, timeout_seconds: float = 1.0) -> None:
        """Cooperatively wait through Pause or stop promptly on SEQ cancellation."""

        timeout = self._positive_finite(
            timeout_seconds,
            "Module checkpoint timeout",
        )
        if self._operation_state is None:
            return
        while True:
            state = str(self._operation_state(timeout)).strip().casefold()
            if state in {"stopping", "cancelled"}:
                raise ModuleOperationCancelled("Module measurement was cancelled")
            if state != "paused":
                return
            time.sleep(0.05)

    def interruptible_sleep(
        self,
        seconds: float,
        *,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        """Sleep without counting paused time and with cooperative stop checks."""

        duration = self._nonnegative_finite(seconds, "Module sleep duration")
        poll_interval = self._positive_finite(
            poll_interval_seconds,
            "Module sleep poll interval",
        )
        remaining = duration
        while remaining > 0:
            self.checkpoint(min(1.0, max(poll_interval, 0.05)))
            interval = min(remaining, poll_interval)
            started = time.monotonic()
            time.sleep(interval)
            remaining -= max(0.0, time.monotonic() - started)
            self.checkpoint(min(1.0, max(poll_interval, 0.05)))

    @staticmethod
    def _positive_finite(value: float, label: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a positive finite number") from exc
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{label} must be a positive finite number")
        return result

    @staticmethod
    def _nonnegative_finite(value: float, label: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{label} must be a non-negative finite number"
            ) from exc
        if not math.isfinite(result) or result < 0:
            raise ValueError(
                f"{label} must be a non-negative finite number"
            )
        return result


class ModuleBackend(ABC):
    """Worker-process lifecycle contract for a measurement module.

    Methods are intentionally synchronous. Instrument drivers must configure
    bounded communication timeouts themselves. The framework may also accept an
    awaitable returned by an implementation for convenience.
    """

    api_version = "1.0"

    def initialize(
        self, settings: Mapping[str, Any], context: ModuleOperationContext
    ) -> Mapping[str, Any] | None:
        return None

    def apply_settings(
        self, settings: Mapping[str, Any], context: ModuleOperationContext
    ) -> Mapping[str, Any] | None:
        return None

    def begin_sequence(self, context: ModuleOperationContext) -> Mapping[str, Any] | None:
        return None

    def measure(self, context: ModuleOperationContext) -> Mapping[str, Any] | None:
        return None

    def end_sequence(
        self, reason: str, context: ModuleOperationContext
    ) -> Mapping[str, Any] | None:
        return None

    def abort(self, context: ModuleOperationContext) -> Mapping[str, Any] | None:
        return None

    def read_status(self, context: ModuleOperationContext) -> Mapping[str, Any] | None:
        return None

    def manual_action(
        self,
        action: str,
        payload: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any] | None:
        raise ModuleWarning(f"Unsupported manual action: {action}", "UNSUPPORTED_ACTION", action)
