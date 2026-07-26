from __future__ import annotations

import asyncio
import inspect
import json
import math
import multiprocessing
import sys
import threading
import time
from collections.abc import Callable, Mapping
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from ..extensions.dependencies import dependency_runtime_errors
from ..extensions.trust import extension_tree_digest
from .api import ModuleBackend, ModuleError, ModuleOperationContext, ModuleWarning
from .manifest import ModuleDescriptor, load_source_object


WorkerEventHandler = Callable[[dict[str, Any]], None]
_MAX_IPC_BYTES = 1024 * 1024


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


def _send_message(
    connection: Connection,
    message: dict[str, Any],
) -> None:
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerRequestError(
            f"Module IPC value is not JSON serializable: {exc}",
            "MODULE_IPC_INVALID_MESSAGE",
        ) from exc
    if len(payload) > _MAX_IPC_BYTES:
        raise WorkerRequestError(
            f"Module IPC message exceeds {_MAX_IPC_BYTES} bytes",
            "MODULE_IPC_MESSAGE_TOO_LARGE",
        )
    connection.send_bytes(payload)


def _receive_message(connection: Connection) -> dict[str, Any]:
    try:
        payload = connection.recv_bytes(_MAX_IPC_BYTES)
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise WorkerRequestError(
            f"Module IPC message is invalid: {exc}",
            "MODULE_IPC_INVALID_MESSAGE",
        ) from exc
    if not isinstance(decoded, dict):
        raise WorkerRequestError(
            "Module IPC message must be a JSON object",
            "MODULE_IPC_INVALID_MESSAGE",
        )
    return dict(decoded)


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
    descriptor: ModuleDescriptor,
    dependency_directory: str,
) -> None:
    backend: ModuleBackend | None = None
    send_lock = threading.Lock()

    def send(message: dict[str, Any]) -> None:
        with send_lock:
            _send_message(connection, message)

    try:
        if (
            descriptor.fingerprint
            and extension_tree_digest(descriptor.path)
            != descriptor.fingerprint
        ):
            raise PermissionError(
                f"Measurement module {descriptor.id} changed after discovery"
            )
        if descriptor.dependencies and not dependency_directory:
            raise PermissionError(
                f"Measurement module {descriptor.id} has no isolated "
                "dependency runtime"
            )
        if dependency_directory:
            dependency_path = Path(dependency_directory)
            dependency_errors = dependency_runtime_errors(
                descriptor.dependencies,
                dependency_path,
                descriptor.fingerprint,
            )
            if dependency_errors:
                raise PermissionError(
                    f"Measurement module {descriptor.id} dependency "
                    "runtime failed verification: "
                    + "; ".join(dependency_errors)
                )
            if dependency_path.is_dir():
                sys.path.insert(
                    0,
                    str(dependency_path.resolve()),
                )
        backend_class = load_source_object(
            descriptor.path,
            descriptor.backend,
            f"backend_{descriptor.id}",
        )
        if not isinstance(backend_class, type) or not issubclass(backend_class, ModuleBackend):
            raise TypeError(
                f"{descriptor.backend} is not a ModuleBackend"
            )
        if (
            str(getattr(backend_class, "api_version", ""))
            != ModuleBackend.api_version
        ):
            raise TypeError(
                f"{descriptor.backend} uses incompatible module API "
                f"{getattr(backend_class, 'api_version', '')!r}"
            )
        backend = backend_class()
        send({"type": "ready"})
    except Exception as exc:
        send({"type": "boot_error", "message": f"{type(exc).__name__}: {exc}"})
        connection.close()
        return

    while True:
        try:
            request = _receive_message(connection)
        except (EOFError, OSError, WorkerRequestError):
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

    def __init__(
        self,
        descriptor: ModuleDescriptor,
        dependency_directory: Path | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.dependency_directory = dependency_directory
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
            args=(
                child,
                self.descriptor,
                (
                    ""
                    if self.dependency_directory is None
                    else str(self.dependency_directory)
                ),
            ),
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
            hello = _receive_message(parent)
        except WorkerRequestError as exc:
            if exc.code == "MODULE_WORKER_START_TIMEOUT":
                raise
            self._invalidate(parent, process, min(timeout, 1.0))
            raise WorkerRequestError(
                "Module worker exited during startup",
                "MODULE_WORKER_START_FAILED",
                self.descriptor.id,
            ) from exc
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
        deadline = time.monotonic() + timeout
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            self.force_stop(min(timeout, 1.0))
            raise WorkerRequestError(
                f"Module operation {action!r} timed out waiting "
                "for another request",
                "MODULE_OPERATION_TIMEOUT",
                action,
            )
        try:
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
                _send_message(
                    connection,
                    {
                        "id": request_id,
                        "action": action,
                        "payload": dict(payload or {}),
                    },
                )
            except (EOFError, OSError, WorkerRequestError) as exc:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise WorkerRequestError(
                    "Module worker connection closed unexpectedly",
                    "MODULE_WORKER_DISCONNECTED",
                    action,
                ) from exc
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
                    message = _receive_message(connection)
                except (EOFError, OSError, WorkerRequestError) as exc:
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
                result = message.get("result", {})
                if not isinstance(result, dict):
                    raise WorkerRequestError(
                        "Module worker result must be an object",
                        "MODULE_IPC_INVALID_MESSAGE",
                        action,
                    )
                return dict(result)
        finally:
            self._lock.release()

    def force_stop(self, timeout_seconds: float = 0.25) -> None:
        with self._state_lock:
            connection = self._connection
            process = self._process
        self._invalidate(
            connection,
            process,
            max(0.0, timeout_seconds),
        )

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
