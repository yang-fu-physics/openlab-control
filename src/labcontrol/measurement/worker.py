from __future__ import annotations

import asyncio
import inspect
import math
import multiprocessing
import threading
import time
from collections.abc import Callable, Mapping
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from .api import ModuleBackend, ModuleError, ModuleOperationContext, ModuleWarning
from .manifest import ModuleDescriptor, load_source_object


WorkerEventHandler = Callable[[dict[str, Any]], None]


class WorkerRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        code: str = "MODULE_OPERATION_FAILED",
        context: str = "",
        severity: str = "error",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = context
        self.severity = severity


def _result(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("Module lifecycle methods must return a mapping or None")
    return dict(value)


def _invoke(method: Callable[..., Any], *args: Any) -> dict[str, Any]:
    value = method(*args)
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    return _result(value)


def module_worker_main(
    connection: Connection,
    directory: str,
    backend_specification: str,
    module_id: str,
) -> None:
    backend: ModuleBackend | None = None
    send_lock = threading.Lock()

    def send(message: dict[str, Any]) -> None:
        with send_lock:
            connection.send(message)

    try:
        backend_class = load_source_object(Path(directory), backend_specification, f"backend_{module_id}")
        if not isinstance(backend_class, type) or not issubclass(backend_class, ModuleBackend):
            raise TypeError(f"{backend_specification} is not a ModuleBackend")
        backend = backend_class()
        send({"type": "ready"})
    except Exception as exc:
        send({"type": "boot_error", "message": f"{type(exc).__name__}: {exc}"})
        connection.close()
        return

    while True:
        try:
            request = connection.recv()
        except (EOFError, OSError):
            break
        request_id = str(request.get("id", ""))
        action = str(request.get("action", ""))
        payload = dict(request.get("payload", {}))
        if action == "close":
            send({"type": "response", "id": request_id, "ok": True, "result": {}})
            break

        def emit(kind: str, values: dict[str, Any]) -> None:
            send({"type": kind, "id": request_id, **values})

        context = ModuleOperationContext(dict(payload.get("system", {})), emit)
        try:
            if action == "initialize":
                result = _invoke(backend.initialize, dict(payload.get("settings", {})), context)
            elif action == "apply_settings":
                result = _invoke(backend.apply_settings, dict(payload.get("settings", {})), context)
            elif action == "begin_sequence":
                result = _invoke(backend.begin_sequence, context)
            elif action == "measure":
                result = _invoke(backend.measure, context)
            elif action == "end_sequence":
                result = _invoke(backend.end_sequence, str(payload.get("reason", "error")), context)
            elif action == "abort":
                result = _invoke(backend.abort, context)
            elif action == "read_status":
                result = _invoke(backend.read_status, context)
            elif action == "manual_action":
                result = _invoke(
                    backend.manual_action,
                    str(payload.get("name", "")),
                    dict(payload.get("data", {})),
                    context,
                )
            else:
                raise ModuleError(f"Unknown worker action: {action}", "UNKNOWN_MODULE_ACTION", action)
            send({"type": "response", "id": request_id, "ok": True, "result": result})
        except ModuleWarning as exc:
            send({
                "type": "response",
                "id": request_id,
                "ok": False,
                "severity": "warning",
                "message": str(exc),
                "code": exc.code,
                "context": exc.context,
            })
        except ModuleError as exc:
            send({
                "type": "response",
                "id": request_id,
                "ok": False,
                "severity": "error",
                "message": str(exc),
                "code": exc.code,
                "context": exc.context,
            })
        except Exception as exc:
            send({
                "type": "response",
                "id": request_id,
                "ok": False,
                "severity": "error",
                "message": f"{type(exc).__name__}: {exc}",
                "code": "UNHANDLED_MODULE_EXCEPTION",
                "context": action,
            })
    connection.close()


class ModuleWorkerClient:
    """One serialized IPC connection to one independently spawned module backend."""

    def __init__(self, descriptor: ModuleDescriptor) -> None:
        self.descriptor = descriptor
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._request_number = 0

    @staticmethod
    def _timeout(value: float, operation: str) -> float:
        timeout = float(value)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"{operation} timeout must be a positive finite number")
        return timeout

    @staticmethod
    def _stop_process(process: multiprocessing.Process, timeout_seconds: float) -> None:
        timeout = max(0.0, timeout_seconds)
        deadline = time.monotonic() + timeout
        try:
            try:
                alive = process.is_alive()
            except ValueError:
                return
            if alive:
                process.terminate()
                process.join(timeout / 2.0)
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

    def _terminate_process_only(
        self,
        process: multiprocessing.Process,
        timeout_seconds: float,
    ) -> None:
        with self._state_lock:
            owns_process = self._process is process
            if owns_process:
                self._process = None
        if owns_process:
            self._stop_process(process, timeout_seconds)

    def start(self, timeout_seconds: float = 10.0) -> None:
        timeout = self._timeout(timeout_seconds, "Module startup")
        with self._state_lock:
            if self._process is not None:
                return
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=module_worker_main,
            args=(child, str(self.descriptor.path), self.descriptor.backend, self.descriptor.id),
            name=f"OpenLabModule-{self.descriptor.id}",
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
                raise WorkerRequestError(
                    f"Module worker startup timed out after {timeout:g} seconds",
                    "MODULE_WORKER_START_TIMEOUT",
                    self.descriptor.id,
                )
            hello = parent.recv()
        except (EOFError, OSError) as exc:
            self._invalidate(parent, process, min(timeout, 1.0))
            raise WorkerRequestError(
                "Module worker exited during startup",
                "MODULE_WORKER_START_FAILED",
                self.descriptor.id,
            ) from exc
        if hello.get("type") != "ready":
            self._invalidate(parent, process, min(timeout, 1.0))
            raise WorkerRequestError(
                str(hello.get("message", "Module worker failed to start")),
                "MODULE_WORKER_START_FAILED",
                self.descriptor.id,
            )

    def request(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        event_handler: WorkerEventHandler | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        timeout = self._timeout(timeout_seconds, "Module operation")
        with self._lock:
            with self._state_lock:
                connection = self._connection
                process = self._process
            if connection is None or process is None:
                raise WorkerRequestError("Module worker is not running", "MODULE_WORKER_NOT_RUNNING")
            if not process.is_alive():
                self._invalidate(connection, process, min(timeout, 1.0))
                raise WorkerRequestError("Module worker exited unexpectedly", "MODULE_WORKER_EXITED")
            self._request_number += 1
            request_id = str(self._request_number)
            try:
                connection.send({
                    "id": request_id,
                    "action": action,
                    "payload": dict(payload or {}),
                })
            except (EOFError, OSError) as exc:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise WorkerRequestError(
                    "Module worker connection closed unexpectedly",
                    "MODULE_WORKER_DISCONNECTED",
                    action,
                ) from exc
            deadline = time.monotonic() + timeout
            event_error: Exception | None = None
            while True:
                remaining = deadline - time.monotonic()
                try:
                    ready = remaining > 0 and connection.poll(remaining)
                except (EOFError, OSError) as exc:
                    self._invalidate(connection, process, min(timeout, 1.0))
                    raise WorkerRequestError(
                        "Module worker connection closed unexpectedly",
                        "MODULE_WORKER_DISCONNECTED",
                        action,
                    ) from exc
                if not ready:
                    self._invalidate(connection, process, min(timeout, 1.0))
                    raise WorkerRequestError(
                        f"Module operation {action!r} timed out after {timeout:g} seconds",
                        "MODULE_OPERATION_TIMEOUT",
                        action,
                    )
                try:
                    message = connection.recv()
                except (EOFError, OSError) as exc:
                    self._invalidate(connection, process, min(timeout, 1.0))
                    raise WorkerRequestError(
                        "Module worker connection closed unexpectedly",
                        "MODULE_WORKER_DISCONNECTED",
                        action,
                    ) from exc
                if str(message.get("id", "")) != request_id:
                    continue
                if message.get("type") != "response":
                    if event_handler is not None:
                        try:
                            event_handler(dict(message))
                        except Exception as exc:
                            event_error = exc
                    continue
                if not bool(message.get("ok", False)):
                    raise WorkerRequestError(
                        str(message.get("message", "Module operation failed")),
                        str(message.get("code", "MODULE_OPERATION_FAILED")),
                        str(message.get("context", "")),
                        str(message.get("severity", "error")),
                    )
                if event_error is not None:
                    if isinstance(event_error, WorkerRequestError):
                        raise event_error
                    raise WorkerRequestError(
                        f"Module event could not be processed: {event_error}",
                        "MODULE_EVENT_PROCESSING_FAILED",
                        action,
                    ) from event_error
                return dict(message.get("result", {}))

    def close(self, timeout_seconds: float = 3.0) -> None:
        timeout = self._timeout(timeout_seconds, "Module shutdown")
        with self._state_lock:
            connection = self._connection
            process = self._process
        if connection is None or process is None:
            return
        deadline = time.monotonic() + timeout
        acquired = self._lock.acquire(timeout=min(timeout / 2.0, 0.25))
        if not acquired:
            self._terminate_process_only(
                process,
                max(0.0, deadline - time.monotonic()),
            )
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0 and self._lock.acquire(timeout=remaining):
                try:
                    self._invalidate(connection, None, 0.0)
                finally:
                    self._lock.release()
            return
        try:
            if process.is_alive():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self.request("close", timeout_seconds=remaining)
        except Exception:
            pass
        finally:
            self._invalidate(connection, process, max(0.0, deadline - time.monotonic()))
            self._lock.release()
