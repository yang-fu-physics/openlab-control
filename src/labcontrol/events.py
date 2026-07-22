from __future__ import annotations

from collections.abc import Callable
from copy import copy
from datetime import datetime, timezone
from threading import RLock

from .models import EventNotice, LabEvent, Severity


EventListener = Callable[[EventNotice], None]


class EventManager:
    """Tracks active events and suppresses repeated popups while a fault is active."""

    def __init__(self, popup_warnings: bool = True, popup_errors: bool = True) -> None:
        self._active: dict[str, LabEvent] = {}
        self._listeners: list[EventListener] = []
        self._lock = RLock()
        self.popup_warnings = popup_warnings
        self.popup_errors = popup_errors

    @staticmethod
    def make_key(source: str, code: str, context: str = "") -> str:
        return f"{source.strip()}|{code.strip()}|{context.strip()}"

    def subscribe(self, listener: EventListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def report(
        self,
        severity: Severity,
        source: str,
        code: str,
        message: str,
        context: str = "",
    ) -> tuple[LabEvent, bool]:
        key = self.make_key(source, code, context)
        now = datetime.now(timezone.utc)
        if severity is Severity.INFO:
            event = LabEvent(
                key=key,
                severity=severity,
                source=source,
                code=code,
                message=message,
                context=context,
                timestamp=now,
                last_seen=now,
                active=False,
            )
            with self._lock:
                listeners = tuple(self._listeners)
            notice = EventNotice(copy(event), show_popup=False)
            for listener in listeners:
                listener(notice)
            return copy(event), True
        with self._lock:
            existing = self._active.get(key)
            if existing is not None:
                existing.count += 1
                existing.last_seen = now
                existing.message = message
                return copy(existing), False

            event = LabEvent(
                key=key,
                severity=severity,
                source=source,
                code=code,
                message=message,
                context=context,
                timestamp=now,
                last_seen=now,
            )
            self._active[key] = event
            listeners = tuple(self._listeners)

        show_popup = (
            severity is Severity.ERROR and self.popup_errors
        ) or (
            severity is Severity.WARNING and self.popup_warnings
        )
        notice = EventNotice(copy(event), show_popup=show_popup)
        for listener in listeners:
            listener(notice)
        return copy(event), True

    def resolve(self, source: str, code: str, context: str = "") -> LabEvent | None:
        key = self.make_key(source, code, context)
        with self._lock:
            event = self._active.pop(key, None)
            if event is None:
                return None
            event.active = False
            event.resolved_at = datetime.now(timezone.utc)
            listeners = tuple(self._listeners)
        notice = EventNotice(copy(event), show_popup=False, is_resolution=True)
        for listener in listeners:
            listener(notice)
        return copy(event)

    def resolve_source(self, source: str) -> None:
        with self._lock:
            keys = [key for key, event in self._active.items() if event.source == source]
        for key in keys:
            source_name, code, context = key.split("|", 2)
            self.resolve(source_name, code, context)

    def active_events(self) -> tuple[LabEvent, ...]:
        with self._lock:
            return tuple(copy(event) for event in self._active.values())
