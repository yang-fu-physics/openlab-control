"""线程安全的 Error/Warning 状态锁存、去重和解除机制。

事件的唯一键由 ``source + code + context`` 构成。同一故障持续存在时只产生一次普通通知，
但每次发生仍会发送到 occurrence 订阅者，使 SEQ 安全中止和远程报警不会被 UI 去重逻辑
意外屏蔽。INFO 是瞬时记录，不进入活动故障表。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import copy
from datetime import datetime, timezone
from threading import RLock

from .models import EventNotice, LabEvent, Severity


EventListener = Callable[[EventNotice], None]


class EventManager:
    """维护活动事件，并在锁存期间抑制重复日志和弹窗。"""

    def __init__(self, popup_warnings: bool = True, popup_errors: bool = True) -> None:
        self._active: dict[str, LabEvent] = {}
        self._listeners: list[EventListener] = []
        self._occurrence_listeners: list[EventListener] = []
        self._lock = RLock()
        self.popup_warnings = popup_warnings
        self.popup_errors = popup_errors

    @staticmethod
    def make_key(source: str, code: str, context: str = "") -> str:
        """构造稳定去重键；context 用于区分不同仪表、通道或远端目标。"""

        return f"{source.strip()}|{code.strip()}|{context.strip()}"

    def subscribe(self, listener: EventListener) -> None:
        """订阅首次出现、升级和解除等需要展示或记录的状态变化。"""

        with self._lock:
            self._listeners.append(listener)

    def subscribe_occurrences(self, listener: EventListener) -> None:
        """订阅每一次报告，包括仍处于锁存状态的重复事件。

        普通订阅者只接收值得写日志或弹窗的状态变化；运行时安全消费者使用本通道，保证重复
        Error 仍可立即中止当前 SEQ。
        """
        with self._lock:
            self._occurrence_listeners.append(listener)

    def report(
        self,
        severity: Severity,
        source: str,
        code: str,
        message: str,
        context: str = "",
    ) -> tuple[LabEvent, bool]:
        """报告事件，并返回事件快照及“普通状态是否发生变化”。

        Warning 重复出现只增加 ``count`` 和 ``last_seen``；若同一键从 Warning 升级为 Error，
        会重新通知普通订阅者。回调始终在释放内部锁后执行，避免监听器回调再次操作事件管理器
        时死锁。
        """

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
                occurrence_listeners = tuple(self._occurrence_listeners)
            notice = EventNotice(copy(event), show_popup=False)
            for listener in listeners:
                listener(notice)
            for listener in occurrence_listeners:
                listener(notice)
            return copy(event), True
        with self._lock:
            existing = self._active.get(key)
            if existing is not None:
                existing.count += 1
                existing.last_seen = now
                existing.message = message
                severity_upgraded = (
                    existing.severity is Severity.WARNING
                    and severity is Severity.ERROR
                )
                if severity_upgraded:
                    existing.severity = Severity.ERROR
                event = copy(existing)
                listeners = tuple(self._listeners) if severity_upgraded else ()
                occurrence_listeners = tuple(self._occurrence_listeners)
            else:
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
                occurrence_listeners = tuple(self._occurrence_listeners)
                severity_upgraded = False

        show_popup = (
            event.severity is Severity.ERROR and self.popup_errors
        ) or (
            event.severity is Severity.WARNING and self.popup_warnings
        )
        notice = EventNotice(copy(event), show_popup=show_popup if listeners else False)
        for listener in listeners:
            listener(notice)
        occurrence_notice = EventNotice(copy(event), show_popup=False)
        for listener in occurrence_listeners:
            listener(occurrence_notice)
        return copy(event), existing is None or severity_upgraded

    def resolve(self, source: str, code: str, context: str = "") -> LabEvent | None:
        """解除一个锁存事件，并向普通订阅者发送一次 resolution。"""

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
        """解除指定来源当前锁存的全部事件。"""

        with self._lock:
            keys = [key for key, event in self._active.items() if event.source == source]
        for key in keys:
            source_name, code, context = key.split("|", 2)
            self.resolve(source_name, code, context)

    def active_events(self) -> tuple[LabEvent, ...]:
        """返回活动事件副本，调用方不能修改管理器内部状态。"""

        with self._lock:
            return tuple(copy(event) for event in self._active.values())
