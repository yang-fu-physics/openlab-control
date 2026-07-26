from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import os
import site
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from ..config import DeviceConfig
from ..extensions.loading import load_import_object, load_source_object
from ..extensions.trust import extension_tree_digest
from ..models import (
    DeviceActivity,
    DeviceKind,
    DeviceSnapshot,
    StabilityState,
)
from .base import DeviceError, DevicePlugin, DeviceWarning


MAX_DEVICE_MESSAGE_BYTES = 1024 * 1024


class DeviceWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        code: str = "DEVICE_WORKER_ERROR",
        context: str = "",
        severity: str = "error",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = context
        self.severity = severity


@dataclass(frozen=True, slots=True)
class DeviceWorkerSpec:
    device_config: DeviceConfig
    simulation_speed: float
    plugin_id: str
    backend: str
    plugin_directory: str = ""
    fingerprint: str = ""
    dependency_directory: str = ""

    @property
    def external(self) -> bool:
        return bool(self.plugin_directory)


def _encode_message(message: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DeviceWorkerError(
            f"Device IPC message is not JSON serializable: {exc}",
            "DEVICE_IPC_SERIALIZATION_FAILED",
        ) from exc
    if len(encoded) > MAX_DEVICE_MESSAGE_BYTES:
        raise DeviceWorkerError(
            f"Device IPC message exceeds {MAX_DEVICE_MESSAGE_BYTES} bytes",
            "DEVICE_IPC_MESSAGE_TOO_LARGE",
        )
    return encoded


def _send_message(connection: Connection, message: dict[str, Any]) -> None:
    connection.send_bytes(_encode_message(message))


def _receive_message(connection: Connection) -> dict[str, Any]:
    try:
        raw = connection.recv_bytes(MAX_DEVICE_MESSAGE_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeviceWorkerError(
            f"Invalid device IPC message: {exc}",
            "DEVICE_IPC_INVALID_MESSAGE",
        ) from exc
    if not isinstance(value, dict):
        raise DeviceWorkerError(
            "Device IPC message must be a JSON object",
            "DEVICE_IPC_INVALID_MESSAGE",
        )
    return dict(value)


def _snapshot_payload(snapshot: DeviceSnapshot) -> dict[str, Any]:
    return {
        "device_id": snapshot.device_id,
        "display_name": snapshot.display_name,
        "kind": snapshot.kind.value,
        "timestamp": snapshot.timestamp,
        "connected": snapshot.connected,
        "unit": snapshot.unit,
        "current": snapshot.current,
        "target": snapshot.target,
        "rate_per_minute": snapshot.rate_per_minute,
        "activity": snapshot.activity.value,
        "stability": snapshot.stability.value,
        "message": snapshot.message,
    }


def snapshot_from_payload(payload: dict[str, Any]) -> DeviceSnapshot:
    try:
        return DeviceSnapshot(
            device_id=str(payload["device_id"]),
            display_name=str(payload["display_name"]),
            kind=DeviceKind(str(payload["kind"])),
            timestamp=float(payload["timestamp"]),
            connected=bool(payload["connected"]),
            unit=str(payload.get("unit", "")),
            current=(
                None
                if payload.get("current") is None
                else float(payload["current"])
            ),
            target=(
                None
                if payload.get("target") is None
                else float(payload["target"])
            ),
            rate_per_minute=(
                None
                if payload.get("rate_per_minute") is None
                else float(payload["rate_per_minute"])
            ),
            activity=DeviceActivity(str(payload.get("activity", "idle"))),
            stability=StabilityState(
                str(payload.get("stability", "not_applicable"))
            ),
            message=str(payload.get("message", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeviceWorkerError(
            f"Device worker returned an invalid snapshot: {exc}",
            "INVALID_DEVICE_SNAPSHOT",
        ) from exc


def _activate_dependency_directory(path: str) -> None:
    if not path:
        return
    directory = Path(path)
    if not directory.is_dir():
        return
    value = str(directory.resolve())
    site.addsitedir(value)
    if value in os.sys.path:
        os.sys.path.remove(value)
    os.sys.path.insert(1, value)


def _load_backend(spec: DeviceWorkerSpec) -> type[DevicePlugin]:
    _activate_dependency_directory(spec.dependency_directory)
    if spec.external:
        directory = Path(spec.plugin_directory)
        current = extension_tree_digest(directory)
        if current != spec.fingerprint:
            raise PermissionError(
                f"Device plugin {spec.plugin_id} changed after trust verification"
            )
        backend = load_source_object(
            directory,
            spec.backend,
            f"device_worker_{spec.plugin_id}",
        )
    else:
        module_name = spec.backend.split(":", 1)[0]
        if not module_name.startswith("labcontrol.devices."):
            raise PermissionError("Only built-in device imports may bypass a manifest")
        backend = load_import_object(spec.backend)
    if not isinstance(backend, type) or not issubclass(backend, DevicePlugin):
        raise TypeError(f"{spec.backend} is not a DevicePlugin")
    if str(getattr(backend, "api_version", "")) != DevicePlugin.api_version:
        raise TypeError(
            f"{spec.backend} uses incompatible device API "
            f"{getattr(backend, 'api_version', '')!r}"
        )
    return backend


def device_worker_main(connection: Connection, spec: DeviceWorkerSpec) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        backend_class = _load_backend(spec)
        backend = backend_class(
            spec.device_config,
            simulation_speed=spec.simulation_speed,
        )
        _send_message(connection, {"type": "ready"})
    except Exception as exc:
        try:
            _send_message(
                connection,
                {
                    "type": "boot_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            connection.close()
            loop.close()
        return

    while True:
        try:
            request = _receive_message(connection)
        except (DeviceWorkerError, EOFError, OSError):
            break
        request_id = str(request.get("id", ""))
        action = str(request.get("action", ""))
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        if action == "close":
            _send_message(
                connection,
                {
                    "type": "response",
                    "id": request_id,
                    "ok": True,
                    "result": {},
                },
            )
            break
        try:
            if action == "connect":
                value = loop.run_until_complete(backend.connect())
            elif action == "disconnect":
                value = loop.run_until_complete(backend.disconnect())
            elif action == "poll":
                value = loop.run_until_complete(backend.poll())
                if not isinstance(value, DeviceSnapshot):
                    raise TypeError("poll() must return DeviceSnapshot")
                value = _snapshot_payload(value)
            elif action == "set_target":
                value = loop.run_until_complete(
                    backend.set_target(
                        float(payload["value"]),
                        float(payload["rate_per_minute"]),
                        str(payload.get("mode", "Settle")),
                    )
                )
            elif action == "hold":
                value = loop.run_until_complete(backend.hold())
            else:
                raise DeviceError(
                    f"Unknown device worker action: {action}",
                    "UNKNOWN_DEVICE_ACTION",
                    action,
                )
            result = value if isinstance(value, dict) else {}
            _send_message(
                connection,
                {
                    "type": "response",
                    "id": request_id,
                    "ok": True,
                    "result": result,
                },
            )
        except DeviceWarning as exc:
            _send_message(
                connection,
                {
                    "type": "response",
                    "id": request_id,
                    "ok": False,
                    "severity": "warning",
                    "message": str(exc),
                    "code": exc.code,
                    "context": exc.context,
                },
            )
        except DeviceError as exc:
            _send_message(
                connection,
                {
                    "type": "response",
                    "id": request_id,
                    "ok": False,
                    "severity": "error",
                    "message": str(exc),
                    "code": exc.code,
                    "context": exc.context,
                },
            )
        except Exception as exc:
            try:
                _send_message(
                    connection,
                    {
                        "type": "response",
                        "id": request_id,
                        "ok": False,
                        "severity": "error",
                        "message": f"{type(exc).__name__}: {exc}",
                        "code": "UNHANDLED_DEVICE_EXCEPTION",
                        "context": action,
                    },
                )
            except Exception:
                break
    connection.close()
    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()


class DeviceWorkerClient:
    """Serialized, bounded IPC client for one independently spawned device."""

    def __init__(self, spec: DeviceWorkerSpec) -> None:
        self.spec = spec
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._request_number = 0

    @property
    def pid(self) -> int | None:
        with self._state_lock:
            process = self._process
            return process.pid if process is not None and process.is_alive() else None

    @staticmethod
    def _timeout(value: float, operation: str) -> float:
        timeout = float(value)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"{operation} timeout must be a positive finite number")
        return timeout

    @staticmethod
    def _stop_process(
        process: multiprocessing.Process,
        timeout_seconds: float,
    ) -> None:
        timeout = max(0.0, timeout_seconds)
        deadline = time.monotonic() + timeout
        try:
            try:
                alive = process.is_alive()
            except ValueError:
                return
            if alive:
                process.terminate()
                process.join(min(timeout / 2.0, 0.5))
            else:
                process.join(timeout)
            if process.is_alive():
                process.kill()
                process.join(max(0.0, deadline - time.monotonic()))
        finally:
            try:
                if not process.is_alive():
                    process.close()
            except (AttributeError, OSError, ValueError):
                pass

    def _invalidate(
        self,
        connection: Connection | None,
        process: multiprocessing.Process | None,
        timeout_seconds: float,
    ) -> None:
        with self._state_lock:
            owns_connection = self._connection is connection
            owns_process = self._process is process
            if owns_connection:
                self._connection = None
            if owns_process:
                self._process = None
        if connection is not None and owns_connection:
            try:
                connection.close()
            except OSError:
                pass
        if process is not None and owns_process:
            self._stop_process(process, timeout_seconds)

    def force_stop(self, timeout_seconds: float) -> None:
        with self._state_lock:
            connection = self._connection
            process = self._process
        self._invalidate(connection, process, timeout_seconds)

    def start(self, timeout_seconds: float) -> None:
        timeout = self._timeout(timeout_seconds, "Device worker startup")
        with self._state_lock:
            if self._process is not None:
                return
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=device_worker_main,
            args=(child, self.spec),
            name=f"OpenLabDevice-{self.spec.device_config.id}",
            daemon=True,
        )
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        with self._state_lock:
            self._connection = parent
            self._process = process
        try:
            if not parent.poll(timeout):
                self._invalidate(parent, process, min(timeout, 1.0))
                raise DeviceWorkerError(
                    f"Device worker startup timed out after {timeout:g} seconds",
                    "DEVICE_WORKER_START_TIMEOUT",
                    self.spec.device_config.id,
                )
            hello = _receive_message(parent)
        except (DeviceWorkerError, EOFError, OSError) as exc:
            self._invalidate(parent, process, min(timeout, 1.0))
            if isinstance(exc, DeviceWorkerError):
                raise
            raise DeviceWorkerError(
                "Device worker exited during startup",
                "DEVICE_WORKER_START_FAILED",
                self.spec.device_config.id,
            ) from exc
        if hello.get("type") != "ready":
            self._invalidate(parent, process, min(timeout, 1.0))
            raise DeviceWorkerError(
                str(hello.get("message", "Device worker failed to start")),
                "DEVICE_WORKER_START_FAILED",
                self.spec.device_config.id,
            )

    def request(
        self,
        action: str,
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        timeout = self._timeout(timeout_seconds, "Device operation")
        started = time.monotonic()
        deadline = started + timeout
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            self.force_stop(min(timeout, 1.0))
            raise DeviceWorkerError(
                f"Device operation {action!r} timed out waiting for another request",
                "DEVICE_OPERATION_TIMEOUT",
                action,
            )
        try:
            with self._state_lock:
                connection = self._connection
                process = self._process
            if connection is None or process is None:
                raise DeviceWorkerError(
                    "Device worker is not running",
                    "DEVICE_WORKER_NOT_RUNNING",
                    action,
                )
            if not process.is_alive():
                self._invalidate(connection, process, min(timeout, 1.0))
                raise DeviceWorkerError(
                    "Device worker exited unexpectedly",
                    "DEVICE_WORKER_EXITED",
                    action,
                )
            self._request_number += 1
            request_id = str(self._request_number)
            try:
                _send_message(
                    connection,
                    {
                        "id": request_id,
                        "action": action,
                        "payload": dict(payload or {}),
                    },
                )
            except (DeviceWorkerError, EOFError, OSError) as exc:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise DeviceWorkerError(
                    "Device worker connection closed unexpectedly",
                    "DEVICE_WORKER_DISCONNECTED",
                    action,
                ) from exc
            remaining = deadline - time.monotonic()
            try:
                ready = remaining > 0 and connection.poll(remaining)
            except (EOFError, OSError) as exc:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise DeviceWorkerError(
                    "Device worker connection closed unexpectedly",
                    "DEVICE_WORKER_DISCONNECTED",
                    action,
                ) from exc
            if not ready:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise DeviceWorkerError(
                    f"Device operation {action!r} timed out after {timeout:g} seconds",
                    "DEVICE_OPERATION_TIMEOUT",
                    action,
                )
            try:
                message = _receive_message(connection)
            except (DeviceWorkerError, EOFError, OSError) as exc:
                self._invalidate(connection, process, min(timeout, 1.0))
                if isinstance(exc, DeviceWorkerError):
                    raise
                raise DeviceWorkerError(
                    "Device worker connection closed unexpectedly",
                    "DEVICE_WORKER_DISCONNECTED",
                    action,
                ) from exc
            if str(message.get("id", "")) != request_id:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise DeviceWorkerError(
                    "Device worker returned a mismatched response",
                    "DEVICE_IPC_INVALID_MESSAGE",
                    action,
                )
            if message.get("type") != "response":
                raise DeviceWorkerError(
                    "Device worker returned an invalid response",
                    "DEVICE_IPC_INVALID_MESSAGE",
                    action,
                )
            if not bool(message.get("ok", False)):
                raise DeviceWorkerError(
                    str(message.get("message", "Device operation failed")),
                    str(message.get("code", "DEVICE_OPERATION_FAILED")),
                    str(message.get("context", "")),
                    str(message.get("severity", "error")),
                )
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise DeviceWorkerError(
                    "Device worker result must be an object",
                    "DEVICE_IPC_INVALID_MESSAGE",
                    action,
                )
            return dict(result)
        finally:
            self._lock.release()

    def close(self, timeout_seconds: float) -> None:
        timeout = self._timeout(timeout_seconds, "Device worker shutdown")
        deadline = time.monotonic() + timeout
        acquired = self._lock.acquire(timeout=min(timeout / 2.0, 0.25))
        if not acquired:
            self.force_stop(max(0.0, deadline - time.monotonic()))
            return
        try:
            with self._state_lock:
                connection = self._connection
                process = self._process
            if connection is None or process is None:
                return
            if process.is_alive():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    try:
                        self.request("close", {}, remaining)
                    except Exception:
                        pass
            self._invalidate(
                connection,
                process,
                max(0.0, deadline - time.monotonic()),
            )
        finally:
            self._lock.release()


class IsolatedDeviceClient:
    enforces_timeouts = True

    def __init__(
        self,
        worker: DeviceWorkerClient,
        *,
        startup_timeout_seconds: float,
        operation_timeout_seconds: float,
        shutdown_timeout_seconds: float,
    ) -> None:
        self.worker = worker
        self.startup_timeout_seconds = startup_timeout_seconds
        self.operation_timeout_seconds = operation_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds

    @property
    def pid(self) -> int | None:
        return self.worker.pid

    @staticmethod
    def _translate(exc: DeviceWorkerError) -> DeviceError | DeviceWarning:
        error_type = DeviceWarning if exc.severity == "warning" else DeviceError
        return error_type(str(exc), exc.code, exc.context)

    async def _request(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        shutdown: bool = False,
    ) -> dict[str, Any]:
        timeout = (
            self.shutdown_timeout_seconds
            if shutdown
            else self.operation_timeout_seconds
        )
        try:
            return await asyncio.to_thread(
                self.worker.request,
                action,
                payload,
                timeout,
            )
        except DeviceWorkerError as exc:
            raise self._translate(exc) from exc

    async def connect(self) -> None:
        try:
            await asyncio.to_thread(
                self.worker.start,
                self.startup_timeout_seconds,
            )
        except DeviceWorkerError as exc:
            raise self._translate(exc) from exc
        await self._request("connect")

    async def disconnect(self) -> None:
        await self._request("disconnect", shutdown=True)

    async def poll(self) -> DeviceSnapshot:
        result = await self._request("poll")
        return snapshot_from_payload(result)

    async def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
    ) -> None:
        await self._request(
            "set_target",
            {
                "value": value,
                "rate_per_minute": rate_per_minute,
                "mode": mode,
            },
        )

    async def hold(self) -> None:
        await self._request("hold")

    async def close(self) -> None:
        await asyncio.to_thread(
            self.worker.close,
            self.shutdown_timeout_seconds,
        )


class InProcessDeviceClient:
    enforces_timeouts = False
    pid: int | None = None

    def __init__(self, plugin: DevicePlugin) -> None:
        self.plugin = plugin

    async def connect(self) -> None:
        await self.plugin.connect()

    async def disconnect(self) -> None:
        await self.plugin.disconnect()

    async def poll(self) -> DeviceSnapshot:
        return await self.plugin.poll()

    async def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
    ) -> None:
        await self.plugin.set_target(value, rate_per_minute, mode)

    async def hold(self) -> None:
        await self.plugin.hold()

    async def close(self) -> None:
        return None
