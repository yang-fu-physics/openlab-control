"""外部 System Instrument 的独立进程、受限 IPC 和异步客户端适配层。

每个真实外部仪表默认使用一个 ``spawn`` 子进程。父子进程只交换不超过 1 MiB、禁止 NaN 的
JSON 对象，并为每个请求核对递增 ID。同步 Pipe 操作由 ``asyncio.to_thread`` 移出 runtime
event loop；启动、操作和关闭均有独立硬时限，超时后终止整个 worker，避免失控驱动继续占用
GPIB、串口或网络会话。
"""

from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import sys
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from ..config import InstrumentConfig
from ..package_support.dependencies import dependency_runtime_errors
from ..package_support.loading import load_import_object, load_source_object
from ..package_support.trust import content_tree_digest
from .base import (
    EventResponseSpec,
    InstrumentError,
    SystemInstrument,
    InstrumentWarning,
    SafetyViolation,
)


MAX_INSTRUMENT_MESSAGE_BYTES = 1024 * 1024


class InstrumentWorkerError(RuntimeError):
    """父进程侧的 IPC/worker 错误，保留稳定代码、上下文和严重等级。"""

    def __init__(
        self,
        message: str,
        code: str = "INSTRUMENT_WORKER_ERROR",
        context: str = "",
        severity: str = "error",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = context
        self.severity = severity


@dataclass(frozen=True, slots=True)
class InstrumentWorkerSpec:
    """创建一个仪表 worker 所需的可序列化配置快照。"""

    instrument_config: InstrumentConfig
    simulation_speed: float
    instrument_id: str
    backend: str
    instrument_directory: str = ""
    fingerprint: str = ""
    dependency_directory: str = ""
    dependencies: tuple[str, ...] = ()

    @property
    def external(self) -> bool:
        """是否从受信任的 System Instrument 目录加载，而不是内置包。"""

        return bool(self.instrument_directory)


def _encode_message(message: dict[str, Any]) -> bytes:
    """把 IPC 消息编码为严格 JSON，并在发送前限制总大小。"""

    try:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InstrumentWorkerError(
            f"Instrument IPC message is not JSON serializable: {exc}",
            "INSTRUMENT_IPC_SERIALIZATION_FAILED",
        ) from exc
    if len(encoded) > MAX_INSTRUMENT_MESSAGE_BYTES:
        raise InstrumentWorkerError(
            f"Instrument IPC message exceeds {MAX_INSTRUMENT_MESSAGE_BYTES} bytes",
            "INSTRUMENT_IPC_MESSAGE_TOO_LARGE",
        )
    return encoded


def _send_message(connection: Connection, message: dict[str, Any]) -> None:
    """通过 Pipe 发送一条已验证消息。"""

    connection.send_bytes(_encode_message(message))


def _receive_message(connection: Connection) -> dict[str, Any]:
    """有上限地接收并解析 JSON 对象，拒绝任意 pickle 对象。"""

    try:
        raw = connection.recv_bytes(MAX_INSTRUMENT_MESSAGE_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstrumentWorkerError(
            f"Invalid instrument IPC message: {exc}",
            "INSTRUMENT_IPC_INVALID_MESSAGE",
        ) from exc
    if not isinstance(value, dict):
        raise InstrumentWorkerError(
            "Instrument IPC message must be a JSON object",
            "INSTRUMENT_IPC_INVALID_MESSAGE",
        )
    return dict(value)


def _reading_payload(reading: object) -> dict[str, Any]:
    """验证驱动边界，只允许文档声明的 JSON 标量读数。"""

    if not isinstance(reading, dict):
        raise InstrumentError(
            "Instrument read methods must return a dictionary",
            "INVALID_INSTRUMENT_READING",
        )
    allowed = {"value", "target", "rate", "moving", "ready", "auxiliary"}
    unknown = set(reading) - allowed
    if unknown:
        raise InstrumentError(
            "Instrument reading contains unknown fields: " + ", ".join(sorted(unknown)),
            "INVALID_INSTRUMENT_READING",
        )
    if "value" not in reading:
        raise InstrumentError(
            "Instrument reading is missing value",
            "INVALID_INSTRUMENT_READING",
        )
    result = dict(reading)
    for key in ("value", "target", "rate"):
        value = result.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise InstrumentError(
                f"Instrument reading {key} must be a finite number or null",
                "NONFINITE_INSTRUMENT_READING",
            )
    for key in ("moving", "ready"):
        value = result.get(key)
        if value is not None and not isinstance(value, bool):
            raise InstrumentError(
                f"Instrument reading {key} must be boolean or null",
                "INVALID_INSTRUMENT_READING",
            )
    auxiliary = result.get("auxiliary", {})
    if not isinstance(auxiliary, dict):
        raise InstrumentError(
            "Instrument reading auxiliary must be a dictionary",
            "INVALID_INSTRUMENT_READING",
        )
    for key, value in auxiliary.items():
        if not isinstance(key, str):
            raise InstrumentError(
                "Auxiliary reading keys must be text",
                "INVALID_INSTRUMENT_READING",
            )
        if not isinstance(value, (int, float, str, bool, type(None))):
            raise InstrumentError(
                f"Auxiliary reading {key!r} must be a JSON scalar",
                "INVALID_INSTRUMENT_READING",
            )
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and not math.isfinite(value)
        ):
            raise InstrumentError(
                f"Auxiliary reading {key!r} must be finite",
                "NONFINITE_INSTRUMENT_READING",
            )
        if isinstance(value, str) and (
            len(value) > 256 or any(character in value for character in "\r\n")
        ):
            raise InstrumentError(
                f"Auxiliary reading {key!r} contains invalid text",
                "INVALID_INSTRUMENT_READING",
            )
    result["auxiliary"] = dict(auxiliary)
    return result


def _event_response_payload(responses: object) -> dict[str, Any]:
    """验证 System Instrument 注册边界并生成 JSON 结果。"""

    if not isinstance(responses, tuple) or not all(
        isinstance(item, EventResponseSpec) for item in responses
    ):
        raise InstrumentError(
            "event_responses() must return a tuple of EventResponseSpec",
            "INVALID_EVENT_RESPONSES",
        )
    return {"responses": [item.to_payload() for item in responses]}


def _event_responses_from_payload(
    payload: dict[str, Any],
) -> tuple[EventResponseSpec, ...]:
    """验证 worker 返回的响应声明，拒绝扩展 IPC 对象。"""

    if set(payload) != {"responses"} or not isinstance(
        payload["responses"],
        list,
    ):
        raise InstrumentError(
            "Instrument worker returned invalid event responses",
            "INVALID_EVENT_RESPONSES",
        )
    items = payload["responses"]
    if not all(isinstance(item, dict) for item in items):
        raise InstrumentError(
            "Instrument worker returned invalid event responses",
            "INVALID_EVENT_RESPONSES",
        )
    try:
        return tuple(
            EventResponseSpec.from_payload(item)
            for item in items
        )
    except (TypeError, ValueError) as exc:
        raise InstrumentError(
            f"Instrument worker returned invalid event responses: {exc}",
            "INVALID_EVENT_RESPONSES",
        ) from exc


def _activate_dependency_directory(path: str) -> None:
    """把已验证 System Instrument 的私有 site-packages 放到 worker 导入路径首位。"""

    if not path:
        return
    directory = Path(path)
    if not directory.is_dir():
        return
    value = str(directory.resolve())
    if value in sys.path:
        sys.path.remove(value)
    sys.path.insert(0, value)


def _load_backend(spec: InstrumentWorkerSpec) -> type[SystemInstrument]:
    """在子进程内再次核对指纹、依赖和 API，再返回后端类。"""

    if spec.external:
        directory = Path(spec.instrument_directory)
        current = content_tree_digest(directory)
        if current != spec.fingerprint:
            raise PermissionError(
                f"System Instrument {spec.instrument_id} changed after trust verification"
            )
        dependency_errors = dependency_runtime_errors(
            spec.dependencies,
            Path(spec.dependency_directory),
            spec.fingerprint,
        )
        if dependency_errors:
            raise PermissionError(
                f"System Instrument {spec.instrument_id} dependency runtime "
                "failed verification: "
                + "; ".join(dependency_errors)
            )
        _activate_dependency_directory(
            spec.dependency_directory
        )
        backend = load_source_object(
            directory,
            spec.backend,
            f"instrument_worker_{spec.instrument_id}",
        )
    else:
        module_name = spec.backend.split(":", 1)[0]
        if not module_name.startswith("labcontrol.instruments."):
            raise PermissionError("Only built-in instrument imports may bypass a manifest")
        backend = load_import_object(spec.backend)
    if not isinstance(backend, type) or not issubclass(backend, SystemInstrument):
        raise TypeError(f"{spec.backend} is not a SystemInstrument")
    return backend


def instrument_worker_main(connection: Connection, spec: InstrumentWorkerSpec) -> None:
    """仪表子进程入口：在一个线程中串行执行普通同步驱动方法。

    子进程不信任父进程载荷的形状；每个动作只提取预定义字段。后端抛出的
    ``InstrumentWarning``/``InstrumentError`` 会结构化返回，其他异常统一转换为稳定错误代码。
    """

    try:
        backend_class = _load_backend(spec)
        backend = (
            backend_class(
                spec.instrument_config,
                simulation_speed=spec.simulation_speed,
            )
            if not spec.external
            else backend_class(spec.instrument_config)
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
        return

    while True:
        try:
            request = _receive_message(connection)
        except (InstrumentWorkerError, EOFError, OSError):
            break
        request_id = str(request.get("id", ""))
        action = str(request.get("action", ""))
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        if action == "stop_worker":
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
            if action == "open":
                value = backend.open()
            elif action == "close_instrument":
                value = backend.close()
            elif action in {"read_status", "read_measurement"}:
                read_method = (
                    backend.read_measurement
                    if action == "read_measurement"
                    else backend.read_status
                )
                value = _reading_payload(read_method())
            elif action == "set_target":
                value = backend.set_target(
                    float(payload["value"]),
                    float(payload["rate_per_minute"]),
                    str(payload.get("mode", "Settle")),
                )
            elif action == "hold":
                value = backend.hold()
            elif action == "execute_sequence_command":
                value = backend.execute_sequence_command(
                    str(payload["command_id"])
                )
            elif action == "event_responses":
                try:
                    value = _event_response_payload(
                        backend.event_responses()
                    )
                except Exception as exc:
                    raise InstrumentError(
                        f"Invalid event responses: {exc}",
                        "INVALID_EVENT_RESPONSES",
                    ) from exc
            else:
                raise InstrumentError(
                    f"Unknown instrument worker action: {action}",
                    "UNKNOWN_INSTRUMENT_ACTION",
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
        except SafetyViolation as exc:
            _send_message(
                connection,
                {
                    "type": "response",
                    "id": request_id,
                    "ok": False,
                    "severity": "safety",
                    "message": str(exc),
                    "code": exc.code,
                    "context": exc.context,
                },
            )
        except InstrumentWarning as exc:
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
        except InstrumentError as exc:
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
                        "code": "UNHANDLED_INSTRUMENT_EXCEPTION",
                        "context": action,
                    },
                )
            except Exception:
                break
    connection.close()


class InstrumentWorkerClient:
    """一台独立进程仪表的串行、有时限同步 IPC 客户端。"""

    def __init__(self, spec: InstrumentWorkerSpec) -> None:
        """保存不可变启动规范；真正创建进程推迟到 :meth:`start`。"""

        self.spec = spec
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._request_number = 0

    @property
    def pid(self) -> int | None:
        """返回仍存活 worker 的 PID，否则返回 ``None``。"""

        with self._state_lock:
            process = self._process
            return process.pid if process is not None and process.is_alive() else None

    @staticmethod
    def _timeout(value: float, operation: str) -> float:
        """统一拒绝非有限或非正的 worker 时限。"""

        timeout = float(value)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"{operation} timeout must be a positive finite number")
        return timeout

    @staticmethod
    def _stop_process(
        process: multiprocessing.Process,
        timeout_seconds: float,
    ) -> None:
        """先 terminate、必要时 kill，并在总时限内回收进程句柄。"""

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
        """仅当句柄仍属于当前客户端时清空状态并强制回收。"""

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
        """无条件使当前 Pipe 和 worker 失效，供超时与应用关闭兜底。"""

        with self._state_lock:
            connection = self._connection
            process = self._process
        self._invalidate(connection, process, timeout_seconds)

    def start(self, timeout_seconds: float) -> None:
        """以 ``spawn`` 创建 worker，并等待明确的 ready 握手。"""

        timeout = self._timeout(timeout_seconds, "Instrument worker startup")
        with self._state_lock:
            if self._process is not None:
                return
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=instrument_worker_main,
            args=(child, self.spec),
            name=f"OpenLabInstrument-{self.spec.instrument_config.id}",
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
                raise InstrumentWorkerError(
                    f"Instrument worker startup timed out after {timeout:g} seconds",
                    "INSTRUMENT_WORKER_START_TIMEOUT",
                    self.spec.instrument_config.id,
                )
            hello = _receive_message(parent)
        except (InstrumentWorkerError, EOFError, OSError) as exc:
            self._invalidate(parent, process, min(timeout, 1.0))
            if isinstance(exc, InstrumentWorkerError):
                raise
            raise InstrumentWorkerError(
                "Instrument worker exited during startup",
                "INSTRUMENT_WORKER_START_FAILED",
                self.spec.instrument_config.id,
            ) from exc
        if hello.get("type") != "ready":
            self._invalidate(parent, process, min(timeout, 1.0))
            raise InstrumentWorkerError(
                str(hello.get("message", "Instrument worker failed to start")),
                "INSTRUMENT_WORKER_START_FAILED",
                self.spec.instrument_config.id,
            )

    def request(
        self,
        action: str,
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """串行发送一个动作并在同一总时限内等待匹配 ID 的响应。

        等待锁的时间也计入总时限，避免请求排队后获得额外无限执行时间。任何 Pipe 错误、
        超时或响应 ID 不匹配都会立即废弃 worker，后续由 ``InstrumentManager`` 决定是否重连。
        """

        timeout = self._timeout(timeout_seconds, "Instrument operation")
        started = time.monotonic()
        deadline = started + timeout
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            self.force_stop(min(timeout, 1.0))
            raise InstrumentWorkerError(
                f"Instrument operation {action!r} timed out waiting for another request",
                "INSTRUMENT_OPERATION_TIMEOUT",
                action,
            )
        try:
            with self._state_lock:
                connection = self._connection
                process = self._process
            if connection is None or process is None:
                raise InstrumentWorkerError(
                    "Instrument worker is not running",
                    "INSTRUMENT_WORKER_NOT_RUNNING",
                    action,
                )
            if not process.is_alive():
                self._invalidate(connection, process, min(timeout, 1.0))
                raise InstrumentWorkerError(
                    "Instrument worker exited unexpectedly",
                    "INSTRUMENT_WORKER_EXITED",
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
            except (InstrumentWorkerError, EOFError, OSError) as exc:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise InstrumentWorkerError(
                    "Instrument worker connection closed unexpectedly",
                    "INSTRUMENT_WORKER_DISCONNECTED",
                    action,
                ) from exc
            remaining = deadline - time.monotonic()
            try:
                ready = remaining > 0 and connection.poll(remaining)
            except (EOFError, OSError) as exc:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise InstrumentWorkerError(
                    "Instrument worker connection closed unexpectedly",
                    "INSTRUMENT_WORKER_DISCONNECTED",
                    action,
                ) from exc
            if not ready:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise InstrumentWorkerError(
                    f"Instrument operation {action!r} timed out after {timeout:g} seconds",
                    "INSTRUMENT_OPERATION_TIMEOUT",
                    action,
                )
            try:
                message = _receive_message(connection)
            except (InstrumentWorkerError, EOFError, OSError) as exc:
                self._invalidate(connection, process, min(timeout, 1.0))
                if isinstance(exc, InstrumentWorkerError):
                    raise
                raise InstrumentWorkerError(
                    "Instrument worker connection closed unexpectedly",
                    "INSTRUMENT_WORKER_DISCONNECTED",
                    action,
                ) from exc
            if str(message.get("id", "")) != request_id:
                self._invalidate(connection, process, min(timeout, 1.0))
                raise InstrumentWorkerError(
                    "Instrument worker returned a mismatched response",
                    "INSTRUMENT_IPC_INVALID_MESSAGE",
                    action,
                )
            if message.get("type") != "response":
                raise InstrumentWorkerError(
                    "Instrument worker returned an invalid response",
                    "INSTRUMENT_IPC_INVALID_MESSAGE",
                    action,
                )
            if not bool(message.get("ok", False)):
                raise InstrumentWorkerError(
                    str(message.get("message", "Instrument operation failed")),
                    str(message.get("code", "INSTRUMENT_OPERATION_FAILED")),
                    str(message.get("context", "")),
                    str(message.get("severity", "error")),
                )
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise InstrumentWorkerError(
                    "Instrument worker result must be an object",
                    "INSTRUMENT_IPC_INVALID_MESSAGE",
                    action,
                )
            return dict(result)
        finally:
            self._lock.release()

    def close(self, timeout_seconds: float) -> None:
        """尝试协议化关闭；锁竞争或无响应时在剩余时限内强制停止。"""

        timeout = self._timeout(timeout_seconds, "Instrument worker shutdown")
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
                        self.request("stop_worker", {}, remaining)
                    except Exception:
                        pass
            self._invalidate(
                connection,
                process,
                max(0.0, deadline - time.monotonic()),
            )
        finally:
            self._lock.release()


class IsolatedInstrumentClient:
    """把同步 worker IPC 适配为 ``InstrumentManager`` 使用的异步仪表接口。"""

    enforces_timeouts = True

    def __init__(
        self,
        worker: InstrumentWorkerClient,
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
        """暴露 worker PID，便于诊断和退出测试。"""

        return self.worker.pid

    @staticmethod
    def _translate(exc: InstrumentWorkerError) -> InstrumentError | InstrumentWarning:
        """把 IPC 错误恢复为框架统一的 Error/Warning 语义。"""

        error_type = {
            "warning": InstrumentWarning,
            "safety": SafetyViolation,
        }.get(exc.severity, InstrumentError)
        return error_type(str(exc), exc.code, exc.context)

    async def _request(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        shutdown: bool = False,
    ) -> dict[str, Any]:
        """在线程池执行阻塞 Pipe 请求，保持 runtime event loop 可响应 Stop。"""

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
        except InstrumentWorkerError as exc:
            raise self._translate(exc) from exc

    async def open(self) -> None:
        try:
            await asyncio.to_thread(
                self.worker.start,
                self.startup_timeout_seconds,
            )
        except InstrumentWorkerError as exc:
            raise self._translate(exc) from exc
        await self._request("open")

    async def event_responses(self) -> tuple[EventResponseSpec, ...]:
        """启动 worker 但不连接仪表，并读取后端的纯数据响应声明。"""

        try:
            await asyncio.to_thread(
                self.worker.start,
                self.startup_timeout_seconds,
            )
        except InstrumentWorkerError as exc:
            raise self._translate(exc) from exc
        return _event_responses_from_payload(
            await self._request("event_responses")
        )

    async def close(self) -> None:
        await self._request("close_instrument", shutdown=True)

    async def read_status(self) -> dict[str, Any]:
        return await self._request("read_status")

    async def read_measurement(self) -> dict[str, Any]:
        return await self._request("read_measurement")

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

    async def execute_sequence_command(self, command_id: str) -> None:
        await self._request(
            "execute_sequence_command",
            {"command_id": command_id},
        )

    async def shutdown(self) -> None:
        await asyncio.to_thread(
            self.worker.close,
            self.shutdown_timeout_seconds,
        )

    async def force_stop(self, timeout_seconds: float = 0.25) -> None:
        await asyncio.to_thread(self.worker.force_stop, timeout_seconds)


class InProcessInstrumentClient:
    """内置可信仪表的轻量适配器；超时由 ``InstrumentManager`` 提供。"""

    enforces_timeouts = False
    pid: int | None = None

    def __init__(self, instrument: SystemInstrument) -> None:
        self.backend = instrument

    async def open(self) -> None:
        await asyncio.to_thread(self.backend.open)

    async def event_responses(self) -> tuple[EventResponseSpec, ...]:
        try:
            return _event_responses_from_payload(
                _event_response_payload(
                    await asyncio.to_thread(
                        self.backend.event_responses
                    )
                )
            )
        except Exception as exc:
            if isinstance(exc, InstrumentError):
                raise
            raise InstrumentError(
                f"Invalid event responses: {exc}",
                "INVALID_EVENT_RESPONSES",
            ) from exc

    async def close(self) -> None:
        await asyncio.to_thread(self.backend.close)

    async def read_status(self) -> dict[str, Any]:
        return _reading_payload(await asyncio.to_thread(self.backend.read_status))

    async def read_measurement(self) -> dict[str, Any]:
        return _reading_payload(await asyncio.to_thread(self.backend.read_measurement))

    async def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
    ) -> None:
        await asyncio.to_thread(self.backend.set_target, value, rate_per_minute, mode)

    async def hold(self) -> None:
        await asyncio.to_thread(self.backend.hold)

    async def execute_sequence_command(self, command_id: str) -> None:
        await asyncio.to_thread(
            self.backend.execute_sequence_command,
            command_id,
        )

    async def shutdown(self) -> None:
        return None

    async def force_stop(self, timeout_seconds: float = 0.25) -> None:
        del timeout_seconds
