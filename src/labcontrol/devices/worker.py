"""外部 Device Plugin 的独立进程、受限 IPC 和异步客户端适配层。

每个真实外部设备默认使用一个 ``spawn`` 子进程。父子进程只交换不超过 1 MiB、禁止 NaN 的
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

from ..config import DeviceConfig
from ..extensions.dependencies import dependency_runtime_errors
from ..extensions.loading import load_import_object, load_source_object
from ..extensions.trust import extension_tree_digest
from ..models import (
    DeviceActivity,
    DeviceConnectionState,
    DeviceKind,
    DeviceMetric,
    DeviceSnapshot,
    StabilityState,
)
from .base import DeviceError, DevicePlugin, DeviceWarning, SafetyViolation


MAX_DEVICE_MESSAGE_BYTES = 1024 * 1024


class DeviceWorkerError(RuntimeError):
    """父进程侧的 IPC/worker 错误，保留稳定代码、上下文和严重等级。"""

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
    """创建一个设备 worker 所需的可序列化配置快照。"""

    device_config: DeviceConfig
    simulation_speed: float
    plugin_id: str
    backend: str
    plugin_directory: str = ""
    fingerprint: str = ""
    dependency_directory: str = ""
    dependencies: tuple[str, ...] = ()

    @property
    def external(self) -> bool:
        """是否从受信任的外部插件目录加载，而不是内置包。"""

        return bool(self.plugin_directory)


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
    """通过 Pipe 发送一条已验证消息。"""

    connection.send_bytes(_encode_message(message))


def _receive_message(connection: Connection) -> dict[str, Any]:
    """有上限地接收并解析 JSON 对象，拒绝任意 pickle 对象。"""

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
    """把设备快照转换为仅含 JSON 标量的 IPC 载荷。"""

    if snapshot.instrument_stable is not None and not isinstance(
        snapshot.instrument_stable,
        bool,
    ):
        raise DeviceError(
            "instrument_stable must be boolean or null",
            "INVALID_DEVICE_SNAPSHOT",
        )
    if not isinstance(snapshot.metrics, tuple):
        raise DeviceError(
            "metrics must be a tuple of DeviceMetric values",
            "INVALID_DEVICE_SNAPSHOT",
        )
    metric_payloads: list[dict[str, Any]] = []
    for metric in snapshot.metrics:
        if not isinstance(metric, DeviceMetric):
            raise DeviceError(
                "metrics must contain only DeviceMetric values",
                "INVALID_DEVICE_SNAPSHOT",
            )
        if not isinstance(metric.value, (int, float, str, bool, type(None))):
            raise DeviceError(
                "metric values must be JSON scalars",
                "INVALID_DEVICE_SNAPSHOT",
            )
        if (
            isinstance(metric.value, (int, float))
            and not isinstance(metric.value, bool)
        ):
            try:
                finite = math.isfinite(metric.value)
            except OverflowError:
                finite = False
            if not finite:
                raise DeviceError(
                    "metric numeric values must be finite",
                    "NONFINITE_DEVICE_READING",
                )
        metric_payloads.append(
            {
                "key": metric.key,
                "display_name": metric.display_name,
                "value": metric.value,
                "unit": metric.unit,
                "decimals": metric.decimals,
            }
        )
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
        "connection_state": snapshot.connection_state.value,
        "instrument_stable": snapshot.instrument_stable,
        "metrics": metric_payloads,
    }


def snapshot_from_payload(payload: dict[str, Any]) -> DeviceSnapshot:
    """严格重建父进程使用的设备快照；枚举或必需字段错误即拒绝。"""

    try:
        raw_instrument_stable = payload.get("instrument_stable")
        if raw_instrument_stable is not None and not isinstance(
            raw_instrument_stable,
            bool,
        ):
            raise TypeError("instrument_stable must be boolean or null")
        raw_metrics = payload.get("metrics", [])
        if not isinstance(raw_metrics, list):
            raise TypeError("metrics must be a list")
        metrics: list[DeviceMetric] = []
        for raw_metric in raw_metrics:
            if not isinstance(raw_metric, dict):
                raise TypeError("each metric must be an object")
            value = raw_metric.get("value")
            if not isinstance(value, (int, float, str, bool, type(None))):
                raise TypeError("metric value must be a JSON scalar")
            decimals = raw_metric.get("decimals")
            if decimals is not None and (
                isinstance(decimals, bool) or not isinstance(decimals, int)
            ):
                raise TypeError("metric decimals must be an integer or null")
            metrics.append(
                DeviceMetric(
                    key=str(raw_metric["key"]),
                    display_name=str(raw_metric["display_name"]),
                    value=value,
                    unit=str(raw_metric.get("unit", "")),
                    decimals=decimals,
                )
            )
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
            connection_state=DeviceConnectionState(
                str(
                    payload.get(
                        "connection_state",
                        "connected" if payload.get("connected") else "disconnected",
                    )
                )
            ),
            instrument_stable=raw_instrument_stable,
            metrics=tuple(metrics),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeviceWorkerError(
            f"Device worker returned an invalid snapshot: {exc}",
            "INVALID_DEVICE_SNAPSHOT",
        ) from exc


def _activate_dependency_directory(path: str) -> None:
    """把已验证的插件私有 site-packages 放到 worker 导入路径首位。"""

    if not path:
        return
    directory = Path(path)
    if not directory.is_dir():
        return
    value = str(directory.resolve())
    if value in sys.path:
        sys.path.remove(value)
    sys.path.insert(0, value)


def _load_backend(spec: DeviceWorkerSpec) -> type[DevicePlugin]:
    """在子进程内再次核对指纹、依赖和 API，再返回后端类。"""

    if spec.external:
        directory = Path(spec.plugin_directory)
        current = extension_tree_digest(directory)
        if current != spec.fingerprint:
            raise PermissionError(
                f"Device plugin {spec.plugin_id} changed after trust verification"
            )
        dependency_errors = dependency_runtime_errors(
            spec.dependencies,
            Path(spec.dependency_directory),
            spec.fingerprint,
        )
        if dependency_errors:
            raise PermissionError(
                f"Device plugin {spec.plugin_id} dependency runtime "
                "failed verification: "
                + "; ".join(dependency_errors)
            )
        _activate_dependency_directory(
            spec.dependency_directory
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
    """设备子进程入口：串行分发协议动作，并在退出前清理 event loop。

    子进程不信任父进程载荷的形状；每个动作只提取预定义字段。插件抛出的
    ``DeviceWarning``/``DeviceError`` 会结构化返回，其他异常统一转换为稳定错误代码。
    """

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
            elif action in {"poll", "poll_measurement"}:
                poll_method = (
                    backend.poll_measurement
                    if action == "poll_measurement"
                    else backend.poll
                )
                value = loop.run_until_complete(poll_method())
                if not isinstance(value, DeviceSnapshot):
                    raise TypeError(f"{action}() must return DeviceSnapshot")
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
    """一台独立进程设备的串行、有时限同步 IPC 客户端。"""

    def __init__(self, spec: DeviceWorkerSpec) -> None:
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
        """串行发送一个动作并在同一总时限内等待匹配 ID 的响应。

        等待锁的时间也计入总时限，避免请求排队后获得额外无限执行时间。任何 Pipe 错误、
        超时或响应 ID 不匹配都会立即废弃 worker，后续由 ``DeviceManager`` 决定是否重连。
        """

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
        """尝试协议化关闭；锁竞争或无响应时在剩余时限内强制停止。"""

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
    """把同步 worker IPC 适配为 ``DeviceManager`` 使用的异步设备接口。"""

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
        """暴露 worker PID，便于诊断和退出测试。"""

        return self.worker.pid

    @staticmethod
    def _translate(exc: DeviceWorkerError) -> DeviceError | DeviceWarning:
        """把 IPC 错误恢复为框架统一的 Error/Warning 语义。"""

        error_type = {
            "warning": DeviceWarning,
            "safety": SafetyViolation,
        }.get(exc.severity, DeviceError)
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

    async def poll_measurement(self) -> DeviceSnapshot:
        result = await self._request("poll_measurement")
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

    async def force_stop(self, timeout_seconds: float = 0.25) -> None:
        await asyncio.to_thread(self.worker.force_stop, timeout_seconds)


class InProcessDeviceClient:
    """内置可信设备的轻量适配器；超时由 ``DeviceManager`` 提供。"""

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

    async def poll_measurement(self) -> DeviceSnapshot:
        return await self.plugin.poll_measurement()

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

    async def force_stop(self, timeout_seconds: float = 0.25) -> None:
        del timeout_seconds
