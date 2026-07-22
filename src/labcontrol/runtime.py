from __future__ import annotations

import asyncio
import queue
import threading
from concurrent.futures import Future
from copy import deepcopy
from typing import Any

from .config import AppConfig
from .datafile import DatRunLogger
from .events import EventManager
from .models import DeviceKind, EventNotice, RunProgress, RuntimeMessage, Severity
from .plugins import DeviceManager
from .sequence.engine import SequenceEngine
from .sequence.model import SequenceDocument


class RuntimeService:
    """Owns the asynchronous device runtime on a background thread."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.messages: queue.Queue[RuntimeMessage] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._sequence_task: asyncio.Task[Any] | None = None
        self._poll_task: asyncio.Task[Any] | None = None
        self.events: EventManager | None = None
        self.devices: DeviceManager | None = None
        self.logger: DatRunLogger | None = None
        self.engine: SequenceEngine | None = None

    def start(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name="OpenLabRuntime", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("Device runtime startup timed out")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self.events = EventManager(
            popup_warnings=self.config.alarms.popup_warnings,
            popup_errors=self.config.alarms.popup_errors,
        )
        self.events.subscribe(self._on_event)
        try:
            self.devices = DeviceManager(self.config, self.events)
            self.logger = DatRunLogger(self.config, self.events)
            self.engine = SequenceEngine(
                self.config,
                self.devices,
                self.events,
                self.logger,
                progress_callback=self._on_progress,
            )
            loop.run_until_complete(self.devices.connect_all())
            self._poll_task = loop.create_task(self._poll_loop())
        except Exception as exc:
            self.messages.put(RuntimeMessage("startup_error", str(exc)))
        finally:
            self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def _poll_loop(self) -> None:
        assert self.devices is not None
        while True:
            try:
                snapshots = await self.devices.poll_all()
                self.messages.put(RuntimeMessage("snapshots", snapshots))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.events is not None:
                    self.events.report(Severity.ERROR, "runtime", "POLL_LOOP_FAILED", str(exc))
            await asyncio.sleep(self.config.poll_interval_seconds)

    def _on_event(self, notice: EventNotice) -> None:
        self.messages.put(RuntimeMessage("event", notice))

    def _on_progress(self, progress: RunProgress) -> None:
        self.messages.put(RuntimeMessage("progress", progress))

    def drain_messages(self, maximum: int = 500) -> list[RuntimeMessage]:
        result: list[RuntimeMessage] = []
        for _ in range(maximum):
            try:
                result.append(self.messages.get_nowait())
            except queue.Empty:
                break
        return result

    def _submit(self, coroutine: Any) -> Future[Any]:
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("Device runtime has not started")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def run_sequence(self, document: SequenceDocument) -> Future[Any]:
        return self._submit(self._run_sequence(deepcopy(document)))

    async def _run_sequence(self, document: SequenceDocument) -> Any:
        if self._sequence_task is not None and not self._sequence_task.done():
            raise RuntimeError("A sequence is already running")
        assert self.engine is not None
        self._sequence_task = asyncio.create_task(self.engine.run(document))
        try:
            return await self._sequence_task
        finally:
            self._sequence_task = None

    def pause_sequence(self) -> None:
        if self._loop is not None and self.engine is not None:
            self._loop.call_soon_threadsafe(self.engine.pause)

    def resume_sequence(self) -> None:
        if self._loop is not None and self.engine is not None:
            self._loop.call_soon_threadsafe(self.engine.resume)

    def stop_sequence(self) -> None:
        if self._loop is not None and self.engine is not None:
            self._loop.call_soon_threadsafe(self.engine.request_stop, False, "Stopped by user")

    def set_target(
        self,
        device_id: str,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
    ) -> Future[Any]:
        assert self.devices is not None
        return self._submit(self.devices.set_target(device_id, value, rate_per_minute, mode))

    def hold_device(self, device_id: str) -> Future[Any]:
        assert self.devices is not None
        return self._submit(self.devices.hold_device(device_id))

    def measure_once(self, device_ids: list[str] | None = None) -> Future[Any]:
        return self._submit(self._measure_once(device_ids))

    async def _measure_once(self, device_ids: list[str] | None) -> dict[str, float | None]:
        assert self.devices is not None
        values = await self.devices.measure(device_ids)
        self.messages.put(RuntimeMessage("manual_measurement", values))
        return values

    def inject_event(self, severity: Severity, code: str, message: str) -> None:
        if self._loop is None or self.events is None:
            return
        self._loop.call_soon_threadsafe(
            self.events.report, severity, "simulation", code, message, "manual"
        )

    def resolve_event(self, source: str, code: str, context: str = "") -> None:
        if self._loop is None or self.events is None:
            return
        self._loop.call_soon_threadsafe(self.events.resolve, source, code, context)

    def shutdown(self, timeout: float = 8.0) -> None:
        if self._loop is None or self._thread is None:
            return
        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)
            try:
                future.result(timeout=timeout)
            except Exception:
                self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        self._thread = None

    async def _shutdown_async(self) -> None:
        if self.engine is not None:
            self.engine.request_stop(False, "Application closing")
        if self._sequence_task is not None:
            try:
                await asyncio.wait_for(self._sequence_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._sequence_task.cancel()
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        if self.devices is not None:
            await self.devices.disconnect_all()
        if self.logger is not None:
            self.logger.close()
        assert self._loop is not None
        self._loop.call_soon(self._loop.stop)
