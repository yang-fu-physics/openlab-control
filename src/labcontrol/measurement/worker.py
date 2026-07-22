from __future__ import annotations

import asyncio
import inspect
import multiprocessing
import threading
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
        self._request_number = 0

    def start(self) -> None:
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
        process.start()
        child.close()
        hello = parent.recv()
        if hello.get("type") != "ready":
            parent.close()
            process.join()
            raise WorkerRequestError(
                str(hello.get("message", "Module worker failed to start")),
                "MODULE_WORKER_START_FAILED",
                self.descriptor.id,
            )
        self._connection = parent
        self._process = process

    def request(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        event_handler: WorkerEventHandler | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._connection is None or self._process is None:
                raise WorkerRequestError("Module worker is not running", "MODULE_WORKER_NOT_RUNNING")
            if not self._process.is_alive():
                raise WorkerRequestError("Module worker exited unexpectedly", "MODULE_WORKER_EXITED")
            self._request_number += 1
            request_id = str(self._request_number)
            self._connection.send({"id": request_id, "action": action, "payload": dict(payload or {})})
            event_error: Exception | None = None
            while True:
                try:
                    message = self._connection.recv()
                except (EOFError, OSError) as exc:
                    raise WorkerRequestError(
                        "Module worker connection closed unexpectedly",
                        "MODULE_WORKER_DISCONNECTED",
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

    def close(self) -> None:
        connection = self._connection
        process = self._process
        if connection is None or process is None:
            return
        try:
            if process.is_alive():
                self.request("close")
        except Exception:
            pass
        finally:
            connection.close()
            process.join()
            self._connection = None
            self._process = None
