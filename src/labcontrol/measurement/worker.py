"""Measurement Module 的进程隔离和双向 IPC 实现。

每个 Enabled 模块拥有一个独立 ``spawn`` 子进程和一条双工 Pipe。同一模块的生命周期
请求严格串行，不同模块可以由 service 并行等待。IPC 只允许有大小上限的 UTF-8 JSON，
避免把 Python 对象、Qt 对象或主进程资源隐式共享给第三方模块。

worker 超时或协议失配后不会继续复用。因为主进程已经无法判断旧请求是否仍在操作仪表，
复用同一进程可能让后续命令与迟到结果交叉；框架因此关闭管道并终止该 worker。该动作只
回收本机资源，不证明外部仪表进入安全状态。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import multiprocessing
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from multiprocessing.connection import Connection
from typing import Any

from ..package_support.loading import load_source_object
from ..module_api import (
    ModuleAPI,
    ModuleError,
    ModuleWarning,
    _ModuleOperationCancelled,
)
from ..module_commands import ModuleCommandSpec, normalize_module_commands
from .manifest import ModuleColumn, ModuleDescriptor


WorkerEventHandler = Callable[
    [dict[str, Any]],
    Mapping[str, Any] | None,
]
_MAX_IPC_BYTES = 1024 * 1024
_MAX_DISPLAY_COLUMNS = 8


class WorkerRequestError(RuntimeError):
    """把 worker/IPC 故障转换为核心可分类的错误信息。"""

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
    """把一条消息编码成受限 JSON frame 后写入 Pipe。"""

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
    """读取并验证一条 JSON object；拒绝超长、损坏或非 object 消息。"""

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
    """统一生命周期返回值，禁止返回任意 Python 对象穿过 IPC。"""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("Module lifecycle methods must return a mapping or None")
    return dict(value)


def _invoke(method: Callable[..., Any], *args: Any) -> dict[str, Any]:
    """调用同步后端，同时兼容后端为了方便返回 awaitable。"""

    value = method(*args)
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    return _result(value)


def _module_slots(value: object) -> list[Any] | None:
    """读取紧凑 ``slots`` 属性；具体数值校验仍由 service 负责。"""

    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 1:
            raise TypeError("Module.slots integer must be positive")
        return list(range(1, value + 1))
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise TypeError("Module.slots must be a positive integer or sequence")
    return list(value)


def _invoke_measure(
    method: Callable[..., Any],
    slot: int,
    api: ModuleAPI,
) -> dict[str, Any]:
    """调用一次测量并统一为 ``values`` + 可选 ``raw_values``。"""

    value = method(slot, api)
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    raw_values: object | None = None
    values: object = value
    if isinstance(value, tuple):
        if len(value) != 2:
            raise TypeError("Module.measure() tuple must be (row, rawdata)")
        values, raw_values = value
    if not isinstance(values, Mapping):
        raise TypeError("Module.measure() must return a row mapping")
    result: dict[str, Any] = {"values": dict(values)}
    if raw_values is not None:
        if (
            not isinstance(raw_values, Sequence)
            or isinstance(raw_values, (str, bytes, bytearray))
        ):
            raise TypeError("Module rawdata must be a sequence of numbers")
        result["raw_values"] = list(raw_values)
    return result


def _normalize_columns(value: object) -> tuple[ModuleColumn, ...]:
    """把后端类中的紧凑 ``{列名: 单位}`` 定义规范化为核心列对象。"""

    if not isinstance(value, Mapping) or not value:
        raise TypeError("Module.columns must be a non-empty mapping of column names to units")
    columns: list[ModuleColumn] = []
    names: set[str] = set()
    for raw_name, raw_unit in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_unit, str):
            raise TypeError("Module.columns names and units must be strings")
        name = raw_name.strip()
        unit = raw_unit.strip()
        if not name or "," in name or "\n" in name or "\r" in name:
            raise TypeError("Module column names must be non-empty single-line values without commas")
        if "\n" in unit or "\r" in unit:
            raise TypeError("Module column units must be single-line values")
        if name in names:
            raise TypeError("Module column names must be unique after trimming")
        names.add(name)
        columns.append(ModuleColumn(name, unit))
    return tuple(columns)


def _normalize_display_columns(
    value: object,
    columns: tuple[ModuleColumn, ...],
) -> tuple[str, ...]:
    """验证主窗口紧凑卡片使用的可选列名。

    模块只能选择自己已经声明的 DAT 列。该元数据不包含格式化回调，因此刷新卡片
    不会执行第三方代码，更不会为了界面显示再次访问仪表。
    """

    if value is None:
        return ()
    if isinstance(value, str):
        raw_names: list[object] = [value]
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (bytes, bytearray),
    ):
        raw_names = list(value)
    else:
        raise TypeError(
            "Module.display_columns must be a column name or sequence of column names"
        )
    if len(raw_names) > _MAX_DISPLAY_COLUMNS:
        raise TypeError(
            f"Module.display_columns may contain at most {_MAX_DISPLAY_COLUMNS} columns"
        )
    available = {column.name for column in columns}
    result: list[str] = []
    for raw_name in raw_names:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise TypeError(
                "Module.display_columns entries must be non-empty strings"
            )
        name = raw_name.strip()
        if name not in available:
            raise TypeError(
                f"Module.display_columns contains undeclared column {name!r}"
            )
        if name in result:
            raise TypeError(
                f"Module.display_columns contains duplicate column {name!r}"
            )
        result.append(name)
    return tuple(result)


def module_worker_main(
    connection: Connection,
    descriptor: ModuleDescriptor,
) -> None:
    """子进程入口：加载 Measurement Module 并串行执行生命周期请求。"""

    backend: Any = None
    sequence_commands: tuple[ModuleCommandSpec, ...] = ()
    # 后端方法和 context 回调可能从不同线程发送消息；Pipe 的多次写必须保持 frame
    # 边界，不能让两个 JSON 字节串互相穿插。
    send_lock = threading.Lock()

    def send(message: dict[str, Any]) -> None:
        with send_lock:
            _send_message(connection, message)

    try:
        backend_class = load_source_object(
            descriptor.path,
            "backend:Module",
            f"backend_{descriptor.id}",
        )
        if not isinstance(backend_class, type):
            raise TypeError("backend:Module is not a class")
        backend = backend_class()
        missing = [
            name
            for name in ("open", "measure", "close")
            if not callable(getattr(backend, name, None))
        ]
        if missing:
            raise TypeError(
                "Module must implement open(), measure() and close(); "
                f"missing: {', '.join(missing)}"
            )
        columns = _normalize_columns(getattr(backend, "columns", None))
        display_columns = _normalize_display_columns(
            getattr(backend, "display_columns", None),
            columns,
        )
        sequence_commands = normalize_module_commands(
            descriptor.id,
            getattr(backend, "sequence_commands", ()),
        )
        if sequence_commands and not callable(
            getattr(backend, "execute_sequence_command", None)
        ):
            raise TypeError(
                "Module declares sequence_commands but does not implement "
                "execute_sequence_command(command_id, parameters, api)"
            )
        # 只有源码、API 和隔离依赖全部验证并成功实例化后才发送 ready。主进程在收到
        # ready 之前不会把模块标记为 Enabled。
        send({
            "type": "ready",
            "columns": [
                {"name": column.name, "unit": column.unit}
                for column in columns
            ],
            "display_columns": list(display_columns),
            "sequence_commands": [
                command.to_payload()
                for command in sequence_commands
            ],
        })
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
        if action == "worker_close":
            # worker_close 只关闭 IPC/进程；模块安全动作必须先由 module_close 完成。
            send({"type": "response", "id": request_id, "ok": True, "result": {}})
            break

        def emit(kind: str, values: dict[str, Any]) -> None:
            send({"type": kind, "id": request_id, **values})

        context_request_number = 0

        def request_context(
            kind: str,
            timeout_seconds: float,
        ) -> dict[str, Any]:
            """在一次后端调用中向核心同步请求新快照或 Pause/Stop 状态。

            外层 request id 与递增的 context_request_id 必须同时匹配，防止迟到响应被
            当前请求误用。等待同样有上限，核心线程异常时模块不能永久卡死。
            """

            nonlocal context_request_number
            context_request_number += 1
            context_request_id = str(context_request_number)
            send({
                "type": "context_request",
                "id": request_id,
                "context_request_id": context_request_id,
                "kind": kind,
            })
            try:
                ready = connection.poll(timeout_seconds)
            except (EOFError, OSError) as exc:
                raise ModuleError(
                    "The core connection closed during a context request",
                    "MODULE_CONTEXT_REQUEST_FAILED",
                    kind,
                ) from exc
            if not ready:
                raise ModuleError(
                    f"Core context request timed out after "
                    f"{timeout_seconds:g} seconds",
                    "MODULE_CONTEXT_REQUEST_TIMEOUT",
                    kind,
                )
            try:
                response = _receive_message(connection)
            except (EOFError, OSError, WorkerRequestError) as exc:
                raise ModuleError(
                    "The core returned an invalid context response",
                    "MODULE_CONTEXT_REQUEST_FAILED",
                    kind,
                ) from exc
            if (
                response.get("type") != "context_response"
                or str(response.get("id", "")) != request_id
                or str(response.get("context_request_id", ""))
                != context_request_id
            ):
                raise ModuleError(
                    "The core returned a mismatched context response",
                    "MODULE_CONTEXT_REQUEST_FAILED",
                    kind,
                )
            if not bool(response.get("ok", False)):
                raise ModuleError(
                    str(
                        response.get(
                            "message",
                            "The core rejected a module context request",
                        )
                    ),
                    "MODULE_CONTEXT_REQUEST_FAILED",
                    kind,
                )
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise ModuleError(
                    "The core returned a non-object context result",
                    "MODULE_CONTEXT_REQUEST_FAILED",
                    kind,
                )
            return dict(result)

        def sample_system(
            timeout_seconds: float,
        ) -> Mapping[str, Mapping[str, Any]]:
            result = request_context("system", timeout_seconds)
            system = result.get("system", {})
            if not isinstance(system, dict):
                raise ModuleError(
                    "The core returned an invalid system snapshot",
                    "MODULE_SYSTEM_SNAPSHOT_INVALID",
                )
            return dict(system)

        def operation_state(timeout_seconds: float) -> str:
            result = request_context("operation_state", timeout_seconds)
            return str(result.get("state", "running"))

        try:
            api = ModuleAPI(
                _initial_instruments=dict(payload.get("system", {})),
                _emit=emit,
                _instrument_resources=dict(payload.get("resources", {})),
                _sample_instruments=sample_system,
                _operation_state=operation_state,
                _operation_timeout_seconds=float(
                    payload.get("operation_timeout_seconds", 120.0)
                ),
            )
        except (TypeError, ValueError):
            send({
                "type": "response",
                "id": request_id,
                "ok": False,
                "severity": "error",
                "message": "The core supplied an invalid operation context",
                "code": "MODULE_OPERATION_CONTEXT_INVALID",
                "context": "",
            })
            continue
        try:
            # 一个 worker 同一时刻只执行这一条分派链。并行 Measure 发生在“不同模块
            # 进程之间”，不是在同一 VISA session 上并发调用后端。
            if action == "open":
                result = _invoke(backend.open, api)
            elif action == "configure":
                method = getattr(backend, "configure", None)
                result = (
                    _invoke(method, dict(payload.get("settings", {})), api)
                    if callable(method)
                    else {}
                )
            elif action == "event":
                event = str(payload.get("name", ""))
                data = payload.get("data", {})
                if not isinstance(data, dict):
                    raise TypeError("Module event payload must be an object")
                method = getattr(backend, "on_event", None)
                if not callable(method):
                    if event == "action":
                        action_name = str(data.get("name", ""))
                        raise ModuleWarning(
                            f"Unsupported manual action: {action_name}",
                            "UNSUPPORTED_ACTION",
                            action_name,
                        )
                    result = {}
                else:
                    result = _invoke(method, event, dict(data), api)
            elif action == "slots":
                result = {"slots": _module_slots(getattr(backend, "slots", None))}
            elif action == "measure":
                slot = int(payload.get("slot", 0))
                if slot < 1:
                    raise ModuleError(
                        "The core supplied an invalid measurement slot",
                        "MODULE_MEASUREMENT_SLOT_INVALID",
                    )
                result = _invoke_measure(backend.measure, slot, api)
            elif action == "sequence_command":
                command_id = str(payload.get("command_id", ""))
                parameters = payload.get("parameters", {})
                if not isinstance(parameters, dict):
                    raise ModuleError(
                        "The core supplied invalid module command parameters",
                        "MODULE_SEQUENCE_COMMAND_INVALID",
                        command_id,
                    )
                known = {
                    command.command_id
                    for command in sequence_commands
                }
                if command_id not in known:
                    raise ModuleError(
                        f"Unknown module sequence command: {command_id}",
                        "MODULE_SEQUENCE_COMMAND_UNKNOWN",
                        command_id,
                    )
                method = getattr(
                    backend,
                    "execute_sequence_command",
                    None,
                )
                if not callable(method):
                    raise ModuleError(
                        "Module sequence command handler is unavailable",
                        "MODULE_SEQUENCE_COMMAND_HANDLER_MISSING",
                        command_id,
                    )
                result = _invoke(
                    method,
                    command_id,
                    dict(parameters),
                    api,
                )
            elif action == "module_close":
                result = _invoke(backend.close, api)
            else:
                raise ModuleError(f"Unknown worker action: {action}", "UNKNOWN_MODULE_ACTION", action)
            send({"type": "response", "id": request_id, "ok": True, "result": result})
        except ModuleWarning as exc:
            # Warning 表示本次调用可恢复；Error 和未处理异常则由主进程进入故障路径。
            send({
                "type": "response",
                "id": request_id,
                "ok": False,
                "severity": "warning",
                "message": str(exc),
                "code": exc.code,
                "context": exc.context,
            })
        except _ModuleOperationCancelled as exc:
            # Stop 的协作取消单独编码，service 不把它重新报告成 Error/Warning。
            send({
                "type": "response",
                "id": request_id,
                "ok": False,
                "severity": "cancelled",
                "message": str(exc),
                "code": "MODULE_OPERATION_CANCELLED",
                "context": action,
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
    """主进程侧的单模块 IPC 客户端。

    ``_lock`` 覆盖一次请求从发送到最终响应的整个期间，保证同一 Pipe 上没有并发请求；
    ``_state_lock`` 只保护 connection/process 引用的替换，不在持有它时等待子进程。
    这种拆分允许关闭线程在请求卡住时废弃进程，而不会和普通请求交叉写 Pipe。
    """

    def __init__(
        self,
        descriptor: ModuleDescriptor,
    ) -> None:
        self.descriptor = descriptor
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        # RLock 是有意选择：close() 获得锁后会调用 request("worker_close")，需要同一线程重入。
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._request_number = 0
        self.sequence_commands: tuple[ModuleCommandSpec, ...] = ()
        self.display_columns: tuple[str, ...] = ()

    @staticmethod
    def _timeout(value: float, operation: str) -> float:
        timeout = float(value)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"{operation} timeout must be a positive finite number")
        return timeout

    @staticmethod
    def _stop_process(process: multiprocessing.Process, timeout_seconds: float) -> None:
        """在总时限内按 terminate → kill 升级并回收进程句柄。

        这是本机资源回收的最后手段。terminate/kill 会跳过模块 ``finally``，因此调用者
        必须在可能时先请求模块 close，并在 close 未确认时向用户保留 Error。
        """

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
        """仅当仍拥有这些对象时使客户端失效，并关闭对应进程。

        ownership 检查防止一个迟到的旧请求把未来可能替换进去的新 connection/process
        一并关闭。
        """

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

    def start(self, timeout_seconds: float = 10.0) -> tuple[ModuleColumn, ...]:
        """使用 spawn 创建干净子进程，并等待经过验证的 ready 握手。"""

        timeout = self._timeout(timeout_seconds, "Module startup")
        with self._state_lock:
            if self._process is not None:
                return self.descriptor.columns
        # Windows 默认也是 spawn；这里显式指定，使源码测试与打包版都不会继承主进程
        # 已打开的 Qt/VISA/线程状态。
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=module_worker_main,
            args=(child, self.descriptor),
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
            # startup timeout 包含第三方源码 import、依赖验证和 backend 构造。
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
        raw_columns = hello.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            self._invalidate(parent, process, min(timeout, 1.0))
            raise WorkerRequestError(
                "Module worker returned invalid column metadata",
                "MODULE_WORKER_START_FAILED",
                self.descriptor.id,
            )
        columns: list[ModuleColumn] = []
        column_names: set[str] = set()
        for item in raw_columns:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("unit", ""), str)
            ):
                self._invalidate(parent, process, min(timeout, 1.0))
                raise WorkerRequestError(
                    "Module worker returned invalid column metadata",
                    "MODULE_WORKER_START_FAILED",
                    self.descriptor.id,
                )
            name = item["name"]
            unit = item.get("unit", "")
            if (
                not name
                or "," in name
                or "\n" in name
                or "\r" in name
                or "\n" in unit
                or "\r" in unit
                or name in column_names
            ):
                self._invalidate(parent, process, min(timeout, 1.0))
                raise WorkerRequestError(
                    "Module worker returned invalid column metadata",
                    "MODULE_WORKER_START_FAILED",
                    self.descriptor.id,
                )
            column_names.add(name)
            columns.append(ModuleColumn(name, unit))
        raw_display_columns = hello.get("display_columns", [])
        if (
            not isinstance(raw_display_columns, list)
            or len(raw_display_columns) > _MAX_DISPLAY_COLUMNS
        ):
            self._invalidate(parent, process, min(timeout, 1.0))
            raise WorkerRequestError(
                "Module worker returned invalid display column metadata",
                "MODULE_WORKER_START_FAILED",
                self.descriptor.id,
            )
        display_columns: list[str] = []
        for name in raw_display_columns:
            if (
                not isinstance(name, str)
                or name not in column_names
                or name in display_columns
            ):
                self._invalidate(parent, process, min(timeout, 1.0))
                raise WorkerRequestError(
                    "Module worker returned invalid display column metadata",
                    "MODULE_WORKER_START_FAILED",
                    self.descriptor.id,
                )
            display_columns.append(name)
        try:
            sequence_commands = normalize_module_commands(
                self.descriptor.id,
                hello.get("sequence_commands", []),
            )
        except (TypeError, ValueError) as exc:
            self._invalidate(parent, process, min(timeout, 1.0))
            raise WorkerRequestError(
                f"Module worker returned invalid sequence command metadata: {exc}",
                "MODULE_WORKER_START_FAILED",
                self.descriptor.id,
            ) from exc
        self.sequence_commands = sequence_commands
        self.display_columns = tuple(display_columns)
        return tuple(columns)

    def request(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        event_handler: WorkerEventHandler | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        """发送一个串行请求，并在同一总 deadline 内处理事件和最终响应。

        deadline 从等待 ``_lock`` 前开始，因此排队、IPC、核心事件处理和后端执行共同
        消耗一个时限。任一阶段超时都会废弃 worker；不能简单重发写操作，因为旧请求
        可能已经到达仪表并产生副作用。
        """

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
                    # 超时后不尝试读取“迟到响应”，直接使 worker 失效，避免下一次请求
                    # 把旧响应当成自己的结果。
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
                    # 正常串行协议不应出现其他 id。忽略它并继续受同一 deadline 约束，
                    # 不允许异常消息延长请求。
                    continue
                if message.get("type") != "response":
                    event_result: Mapping[str, Any] | None = None
                    if event_handler is not None:
                        try:
                            event_result = event_handler(dict(message))
                        except Exception as exc:
                            event_error = exc
                    if message.get("type") == "context_request":
                        # worker 在执行长测量时可同步询问新温场快照或 Pause/Stop 状态。
                        # 响应沿同一 Pipe 返回，仍由 request id 和子请求 id 双重关联。
                        context_response = {
                            "type": "context_response",
                            "id": request_id,
                            "context_request_id": str(
                                message.get(
                                    "context_request_id",
                                    "",
                                )
                            ),
                            "ok": event_error is None
                            and event_handler is not None,
                            "result": dict(event_result or {}),
                        }
                        if event_error is not None:
                            context_response["message"] = (
                                f"{type(event_error).__name__}: "
                                f"{event_error}"
                            )
                        elif event_handler is None:
                            context_response["message"] = (
                                "No core context handler is available"
                            )
                        try:
                            _send_message(
                                connection,
                                context_response,
                            )
                        except (
                            EOFError,
                            OSError,
                            WorkerRequestError,
                        ) as exc:
                            self._invalidate(
                                connection,
                                process,
                                min(timeout, 1.0),
                            )
                            raise WorkerRequestError(
                                "Module worker context response "
                                "could not be sent",
                                "MODULE_WORKER_DISCONNECTED",
                                action,
                            ) from exc
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
        """立即废弃管道并回收进程，不声称外部仪表已经安全。"""

        with self._state_lock:
            connection = self._connection
            process = self._process
        self._invalidate(
            connection,
            process,
            max(0.0, timeout_seconds),
        )

    def close(self, timeout_seconds: float = 3.0) -> None:
        """优先请求正常 close；被活动请求占用时在总时限内强制回收。

        如果短时间拿不到请求锁，说明后端可能仍在阻塞调用。此时先终止进程让 Pipe
        解阻，再尝试取得锁关闭父端 connection。所有分支共用一个 deadline。
        """

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
                    self.request("worker_close", timeout_seconds=remaining)
        except Exception:
            pass
        finally:
            self._invalidate(connection, process, max(0.0, deadline - time.monotonic()))
            self._lock.release()
