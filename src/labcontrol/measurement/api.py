from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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


@dataclass(slots=True)
class ModuleOperationContext:
    """Read-only system context and event emitters available inside a worker process."""

    system: Mapping[str, Mapping[str, Any]]
    _emit: Callable[[str, dict[str, Any]], None]

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
