from __future__ import annotations

import hashlib
import json
import os
import queue
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from .config import AlarmReportingConfig
from .models import EventNotice, Severity


DeliveryStateCallback = Callable[[str | None], None]
UrlOpen = Callable[..., Any]
_STOP = object()
_MAX_MESSAGE_CHARACTERS = 3500
_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class AlarmReport:
    event_id: str
    level: str
    message: str

    def payload(self) -> bytes:
        return json.dumps(
            {
                "event_id": self.event_id,
                "level": self.level,
                "message": self.message,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


class AlarmReporter:
    """Non-blocking HTTP sender for deduplicated Warning/Error notices."""

    def __init__(
        self,
        config: AlarmReportingConfig,
        project_root: Path,
        delivery_state_callback: DeliveryStateCallback | None = None,
        *,
        opener: UrlOpen = urlopen,
    ) -> None:
        self.config = config
        self.project_root = project_root.resolve()
        self._delivery_state_callback = delivery_state_callback
        self._opener = opener
        self._queue: queue.Queue[AlarmReport | object] = queue.Queue(
            maxsize=config.queue_size
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._callback_lock = threading.Lock()
        self._token = ""

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        try:
            self._token = self._load_token()
        except (OSError, ValueError) as exc:
            self._notify_failure(str(exc))
            return
        self._thread = threading.Thread(
            target=self._run,
            name="OpenLabAlarmReporter",
            daemon=True,
        )
        self._thread.start()

    def handle_notice(self, notice: EventNotice) -> None:
        if (
            not self.enabled
            or self._thread is None
            or notice.is_resolution
            or notice.event.severity
            not in {Severity.WARNING, Severity.ERROR}
            or notice.event.source == "alarm_reporter"
        ):
            return
        event = notice.event
        identity = (
            f"{event.key}|{event.timestamp.isoformat()}|"
            f"{event.severity.value}"
        )
        event_id = hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        report = AlarmReport(
            event_id=event_id,
            level=event.severity.value,
            message=self._format_message(notice),
        )
        try:
            self._queue.put_nowait(report)
        except queue.Full:
            self._notify_failure(
                "Alarm reporting queue is full; a new alarm "
                "could not be queued"
            )

    def close(self, timeout_seconds: float | None = None) -> None:
        thread = self._thread
        if thread is None:
            self._disable_callback()
            return
        timeout = (
            self.config.shutdown_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        try:
            self._queue.put(
                _STOP,
                timeout=min(timeout, 0.25),
            )
        except queue.Full:
            self._stop.set()
        thread.join(timeout)
        if thread.is_alive():
            self._stop.set()
            thread.join(min(0.25, timeout))
        self._thread = None
        self._disable_callback()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if item is _STOP:
                    return
                assert isinstance(item, AlarmReport)
                self._deliver_with_retries(item)
            finally:
                self._queue.task_done()

    def _deliver_with_retries(self, report: AlarmReport) -> None:
        last_error: BaseException | None = None
        for attempt in range(1, self.config.retry_attempts + 1):
            if self._stop.is_set():
                return
            try:
                self._deliver(report)
            except Exception as exc:
                last_error = exc
                if attempt < self.config.retry_attempts:
                    self._stop.wait(
                        self.config.retry_delay_seconds
                    )
                continue
            self._notify_success()
            return
        assert last_error is not None
        self._notify_failure(
            f"Alarm delivery failed after "
            f"{self.config.retry_attempts} attempt(s): "
            f"{type(last_error).__name__}: {last_error}"
        )

    def _deliver(self, report: AlarmReport) -> None:
        request = Request(
            self.config.endpoint,
            data=report.payload(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Token": self._token,
                "User-Agent": "OpenLab-Control-AlarmReporter",
            },
        )
        with self._opener(
            request,
            timeout=self.config.timeout_seconds,
        ) as response:
            status_value = getattr(
                response,
                "status",
                None,
            )
            if status_value is None:
                status_value = response.getcode()
            status = int(status_value)
            body = response.read(
                _MAX_RESPONSE_BYTES + 1
            )
        if not 200 <= status < 300:
            raise OSError(
                f"Alarm receiver returned HTTP {status}"
            )
        if len(body) > _MAX_RESPONSE_BYTES:
            raise OSError(
                "Alarm receiver response exceeded 64 KiB"
            )

    def _load_token(self) -> str:
        token = ""
        if self.config.token_env:
            token = os.environ.get(
                self.config.token_env,
                "",
            ).strip()
        if not token and self.config.token_file:
            path = Path(self.config.token_file)
            if not path.is_absolute():
                path = self.project_root / path
            if path.stat().st_size > 4096:
                raise ValueError(
                    "Alarm reporting token file exceeds 4 KiB"
                )
            token = path.read_text(
                encoding="utf-8-sig"
            ).strip()
        if not token:
            raise ValueError(
                "Alarm reporting is enabled but no token was "
                "found in token_env or token_file"
            )
        if (
            len(token) > 1024
            or any(
                character.isspace()
                or ord(character) < 33
                or ord(character) == 127
                for character in token
            )
        ):
            raise ValueError(
                "Alarm reporting token must be at most 1024 "
                "non-whitespace printable characters"
            )
        return token

    @staticmethod
    def _format_message(notice: EventNotice) -> str:
        event = notice.event
        lines = [
            f"【OpenLab Control {event.severity.value.upper()}】",
            f"Time: {event.timestamp.astimezone().isoformat()}",
            f"Host: {socket.gethostname()}",
            f"Source: {event.source}",
            f"Code: {event.code}",
        ]
        if event.context:
            lines.append(f"Context: {event.context}")
        lines.append(f"Message: {event.message}")
        message = "\n".join(lines)
        if len(message) <= _MAX_MESSAGE_CHARACTERS:
            return message
        suffix = "\n[message truncated]"
        return (
            message[
                : _MAX_MESSAGE_CHARACTERS - len(suffix)
            ]
            + suffix
        )

    def _notify_success(self) -> None:
        callback = self._callback()
        if callback is not None:
            try:
                callback(None)
            except Exception:
                pass

    def _notify_failure(self, message: str) -> None:
        callback = self._callback()
        if callback is not None:
            try:
                callback(message)
            except Exception:
                pass

    def _callback(self) -> DeliveryStateCallback | None:
        with self._callback_lock:
            return self._delivery_state_callback

    def _disable_callback(self) -> None:
        with self._callback_lock:
            self._delivery_state_callback = None


__all__ = ["AlarmReport", "AlarmReporter"]
