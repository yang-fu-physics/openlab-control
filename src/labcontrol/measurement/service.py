"""主运行时中的 Measurement Module 生命周期协调器。

service 位于核心 asyncio 线程，负责把多个独立 worker 组织成固定的 SEQ 模块集合，
校验所有跨进程状态和测量行，并把结果写入 DAT。它不在主线程执行第三方模块代码；
阻塞 IPC 通过 ``asyncio.to_thread`` 移出事件循环。
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..datafile import DatRunLogger
from ..instruments.base import InstrumentError
from ..events import EventManager
from ..package_support.trust import (
    ContentTrustStore,
    content_tree_digest,
)
from ..models import InstrumentSnapshot, Severity
from ..module_commands import (
    ModuleCommandSpec,
    module_command_key,
    validate_module_command_parameters,
)
from ..instrument_manager import InstrumentManager
from ..sequence.model import Command, CommandType
from .manifest import ModuleDescriptor, module_dependency_errors, module_dependency_directory
from .worker import ModuleWorkerClient, WorkerRequestError


ModuleMessageCallback = Callable[[str, dict[str, Any]], None]
_MAX_RAW_VALUES = 32_768
_MAX_LOGICAL_SLOTS = 1024
_MEASUREMENT_SAMPLE_REUSE_SECONDS = 0.1


@dataclass(slots=True)
class ModuleRuntimeRecord:
    """一个模块在当前应用进程中的易失运行状态；不会跨启动恢复 Enabled。"""

    descriptor: ModuleDescriptor
    enabled: bool = False
    state: str = "disabled"
    status: dict[str, Any] = field(default_factory=dict)
    client: ModuleWorkerClient | None = None
    sequence_commands: tuple[ModuleCommandSpec, ...] = ()


@dataclass(slots=True)
class _ModuleSlotResult:
    """一个模块在当前逻辑槽位的已验证结果；仅在核心事件循环内使用。"""

    module_id: str
    values: dict[str, Any] | None = None
    raw_values: tuple[float, ...] | None = None
    error: InstrumentError | None = None
    cancelled: bool = False


class MeasurementModuleService:
    """从核心事件循环协调模块信任、worker、SEQ 和 DAT 写入。

    关键不变量：

    - 只有 worker 启动并完成 ``open`` 后，``enabled`` 才能变为 True；
    - Run 开始时冻结模块 ID 集合，运行中不能 Enable/Disable/Refresh；
    - 同一 Measure 并行等待所有冻结模块，但每个模块自己的 IPC 保持串行；
    - worker 返回的每一行都先验证固定 Schema 和有限数值，再进入 DAT；
    - close 失败后仍回收本机 worker，但保留 Error，不能把 Disabled 当作仪表安全确认。
    """

    def __init__(
        self,
        descriptors: tuple[ModuleDescriptor, ...],
        events: EventManager,
        instruments: InstrumentManager,
        message_callback: ModuleMessageCallback | None = None,
    ) -> None:
        self.events = events
        self.instruments = instruments
        self.app_config = instruments.config
        self.config = instruments.config.modules
        self.trust_store = ContentTrustStore(
            instruments.config.resolve_project_path(
                instruments.config.modules.state_directory
            )
            / "trusted_content.json"
        )
        self.message_callback = message_callback or (lambda _kind, _payload: None)
        self.records = {
            item.id: ModuleRuntimeRecord(item)
            for item in descriptors
            if item.valid
        }
        self._sequence_modules: tuple[str, ...] = ()
        self._module_slots: dict[str, frozenset[int]] = {}
        self._logical_slots: tuple[int, ...] = (1,)
        self._sequence_active = False
        self._operation_state = "idle"
        self._operation_state_lock = threading.RLock()
        self._fresh_system_task: asyncio.Task[
            dict[str, InstrumentSnapshot]
        ] | None = None
        self._fresh_system_completed_at: float | None = None

    def _ensure_sequence_idle(self) -> None:
        if self._sequence_active:
            raise InstrumentError(
                "Module changes and manual actions are unavailable while a SEQ is running",
                "MODULE_OPERATION_DURING_SEQUENCE",
            )

    def _ensure_descriptor_ready(
        self,
        descriptor: ModuleDescriptor,
    ) -> None:
        """在每次 Enable 前重新核对内容、信任记录和隔离依赖。"""

        if not descriptor.can_enable:
            raise InstrumentError(
                descriptor.error or descriptor.dependency_error,
                "MODULE_NOT_ENABLEABLE",
                descriptor.id,
            )
        # UI 发现与用户点击 Enable 之间可能隔很久；不能只相信发现阶段缓存的摘要。
        current_fingerprint = content_tree_digest(
            descriptor.path
        )
        if current_fingerprint != descriptor.fingerprint:
            raise InstrumentError(
                f"Measurement module {descriptor.id} changed "
                "after discovery",
                "MODULE_CHANGED_AFTER_DISCOVERY",
                descriptor.id,
            )
        # UI 的首次信任发生在 RuntimeService 启动之后。UI 与 runtime 各自拥有
        # 一个信任存储实例，因此这里必须刷新磁盘上的原子记录，不能继续使用
        # runtime 启动时缓存的空快照。
        self.trust_store.reload()
        if not self.trust_store.is_trusted(
            "module",
            descriptor,
        ):
            raise InstrumentError(
                f"Measurement module {descriptor.id} has not "
                "been trusted",
                "MODULE_NOT_TRUSTED",
                descriptor.id,
            )
        dependency_errors = module_dependency_errors(
            self.app_config,
            descriptor,
        )
        if dependency_errors:
            raise InstrumentError(
                "Invalid isolated module dependencies: "
                + "; ".join(dependency_errors),
                "MODULE_DEPENDENCIES_INVALID",
                descriptor.id,
            )

    def replace_descriptors(self, descriptors: tuple[ModuleDescriptor, ...]) -> None:
        """在 SEQ Idle 且全部模块 Disabled 时替换发现列表，不做热加载。"""

        self._ensure_sequence_idle()
        if any(record.enabled or record.client is not None for record in self.records.values()):
            raise InstrumentError(
                "Disable every module before refreshing module sources",
                "MODULE_REFRESH_BLOCKED",
            )
        self.records = {
            item.id: ModuleRuntimeRecord(item)
            for item in descriptors
            if item.valid
        }

    def _publish(self, record: ModuleRuntimeRecord, message: str = "") -> None:
        self.message_callback("module_state", {
            "module_id": record.descriptor.id,
            "enabled": record.enabled,
            "state": record.state,
            "status": deepcopy(record.status),
            "message": message,
            "display_columns": (
                list(record.descriptor.display_columns)
                if record.enabled
                else []
            ),
            # 只有 Enabled 状态才允许 UI 注册这些指令。初始化和 Disabled 消息始终
            # 发送空列表，避免 open 尚未成功时出现一个实际不可执行的菜单项。
            "sequence_commands": (
                [command.to_payload() for command in record.sequence_commands]
                if record.enabled
                else []
            ),
        })

    def _publish_measurement_result(
        self,
        record: ModuleRuntimeRecord,
        logical_slot: int,
        values: Mapping[str, Any] | None,
    ) -> None:
        """把已校验结果的只读子集发给主窗口监视卡。

        ``display_columns`` 只引用现有 DAT 列。这里不会调用 worker 或仪表；无效或
        缺失的测量值以 ``None`` 发送，让界面显示空值而不是沿用旧通道结果。
        """

        names = record.descriptor.display_columns
        if not names:
            return
        columns = {
            column.name: column
            for column in record.descriptor.columns
        }
        self.message_callback(
            "module_result",
            {
                "module_id": record.descriptor.id,
                "slot": logical_slot,
                "multi_slot": (
                    record.descriptor.id
                    in self._module_slots
                ),
                "items": [
                    {
                        "name": name,
                        "unit": columns[name].unit,
                        "value": (
                            None
                            if values is None
                            else values.get(name)
                        ),
                    }
                    for name in names
                ],
            },
        )

    def _system_payload(self) -> dict[str, dict[str, Any]]:
        """把核心 InstrumentSnapshot 转成只读、可 JSON 化的模块视图。"""

        payload: dict[str, dict[str, Any]] = {}
        for instrument_id, snapshot in self.instruments.snapshots().items():
            instrument_config = self.instruments.instrument_configs.get(
                instrument_id
            )
            payload[instrument_id] = {
                "display_name": snapshot.display_name,
                "kind": snapshot.kind.value,
                "control_enabled": (
                    False
                    if instrument_config is None
                    else instrument_config.control_enabled
                ),
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
                "ready": snapshot.ready,
                "metrics": {
                    metric_key: {
                        "display_name": metric.display_name,
                        "value": metric.value,
                        "unit": metric.unit,
                        "decimals": metric.decimals,
                    }
                    for metric_key, metric in snapshot.metrics.items()
                },
            }
        return payload

    async def _fresh_system_payload(
        self,
        *,
        reuse_within_seconds: float = 0.0,
    ) -> dict[str, dict[str, Any]]:
        """立即采样仪表，并合并同一时刻多个模块发起的并发请求。

        模块显式调用 ``api.instruments()`` 时 ``reuse_within_seconds`` 保持为零，因此连续
        两次调用一定代表两个采样点。核心写测量行前允许复用最多 0.1 秒前由模块触发的
        样本，既避免紧接着重复查询，也不会退回约 1 秒一次的前面板缓存。
        """

        completed_at = self._fresh_system_completed_at
        if (
            reuse_within_seconds > 0.0
            and completed_at is not None
            and time.monotonic() - completed_at <= reuse_within_seconds
        ):
            return self._system_payload()

        task = self._fresh_system_task
        if task is None or task.done():
            task = asyncio.create_task(
                self.instruments.poll_measurement_all()
            )
            self._fresh_system_task = task
        try:
            await asyncio.shield(task)
        finally:
            if self._fresh_system_task is task and task.done():
                self._fresh_system_task = None
        self._fresh_system_completed_at = time.monotonic()
        return self._system_payload()

    async def _worker_event(
        self,
        module_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        """处理 worker 在最终响应前流式发回的状态、事件、数据和上下文请求。"""

        record = self.records[module_id]
        kind = str(message.get("type", ""))
        if kind == "context_request":
            request_kind = str(message.get("kind", ""))
            if request_kind == "system":
                return {"system": await self._fresh_system_payload()}
            if request_kind == "operation_state":
                with self._operation_state_lock:
                    state = self._operation_state
                return {"state": state}
            raise WorkerRequestError(
                f"Unknown module context request: {request_kind}",
                "MODULE_CONTEXT_REQUEST_UNKNOWN",
                module_id,
            )
        if kind == "status":
            values = dict(message.get("values", {}))
            # 状态会继续发送给 UI，也可能保存到运行快照；及早拒绝不可 JSON 化对象，
            # 避免第三方驱动对象泄漏到主进程其他层。
            try:
                json.dumps(values, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise WorkerRequestError(
                    f"Module emitted a non-JSON status: {exc}",
                    "MODULE_STATUS_TYPE_ERROR",
                    module_id,
                ) from exc
            record.status.update(values)
            self._publish(record)
        elif kind == "warning":
            self.events.report(
                Severity.WARNING,
                f"module:{module_id}",
                str(message.get("code", "MODULE_WARNING")),
                str(message.get("message", "Module warning")),
                str(message.get("context", "")),
            )
        elif kind == "resolve":
            self.events.resolve(
                f"module:{module_id}",
                str(message.get("code", "MODULE_WARNING")),
                str(message.get("context", "")),
            )

    async def _request(
        self,
        record: ModuleRuntimeRecord,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """把阻塞 client.request 桥接到 asyncio，并提供反向 context RPC。

        worker 事件由 IPC 辅助线程收到；``on_event`` 再用
        ``run_coroutine_threadsafe`` 切回唯一的核心事件循环。它与后端操作共用 deadline，
        所以 UI 状态处理或 DAT 写入也不能无限占用一次模块请求。
        """

        if record.client is None:
            raise WorkerRequestError("Module worker is unavailable", "MODULE_WORKER_NOT_RUNNING")
        loop = asyncio.get_running_loop()
        timeout = (
            self.config.operation_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        deadline = time.monotonic() + timeout

        def on_event(
            message: dict[str, Any],
        ) -> Mapping[str, Any] | None:
            future = asyncio.run_coroutine_threadsafe(
                self._worker_event(record.descriptor.id, message), loop
            )
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Module event processing timed out")
                return future.result(timeout=remaining)
            except Exception:
                future.cancel()
                raise

        request_payload = dict(payload or {})
        # 初始快照随每个请求发送；模块需要第二个时间点时再通过 context_request 获取
        # 新快照。这样既支持两点平均，也不会把可变 InstrumentManager 暴露给子进程。
        request_payload["system"] = self._system_payload()
        request_payload["resources"] = self.app_config.resource_payload(
            "measurement"
        )
        request_payload["operation_timeout_seconds"] = timeout
        result = await asyncio.to_thread(
            record.client.request,
            action,
            request_payload,
            on_event,
            timeout,
        )
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise WorkerRequestError(
                f"Module returned a non-JSON status/result: {exc}",
                "MODULE_RESULT_TYPE_ERROR",
                action,
            ) from exc
        return result

    def _operation_error(
        self,
        record: ModuleRuntimeRecord,
        error: WorkerRequestError,
        *,
        warning_allowed: bool = False,
        disable_failed_worker: bool = True,
    ) -> InstrumentError | None:
        """报告 worker 错误，并在连接状态不可信时同步失效模块记录。"""

        severity = Severity.WARNING if error.severity == "warning" else Severity.ERROR
        self.events.report(
            severity,
            f"module:{record.descriptor.id}",
            error.code,
            str(error),
            error.context,
        )
        if disable_failed_worker and error.code in {
            "MODULE_OPERATION_TIMEOUT",
            "MODULE_WORKER_DISCONNECTED",
            "MODULE_WORKER_EXITED",
            "MODULE_WORKER_NOT_RUNNING",
        }:
            # 这些错误意味着 worker 已被 client 回收或无法继续通信。保留 enabled=True
            # 会让后续 Measure 误以为模块仍可用，因此必须转为 faulted/Disabled。
            record.client = None
            record.enabled = False
            record.state = "faulted"
            record.sequence_commands = ()
            self._publish(record, str(error))
        if warning_allowed and severity is Severity.WARNING:
            return None
        return InstrumentError(str(error), error.code, error.context)

    async def _reset_failed_enable(
        self,
        record: ModuleRuntimeRecord,
        client: ModuleWorkerClient | None,
        message: str,
    ) -> None:
        """回收部分启动的 worker，并保证 UI 一定收到终止状态。

        Enable 包含信任复核、依赖路径解析、spawn、握手和 open。任何一步都可能
        在 worker 正式可用前失败；若只处理预期的 IPC 异常，意外的文件或进程错误会把
        record 永久留在 ``initializing``。这里集中关闭可能存在的进程并发布 Disabled，
        使同一应用进程内可以修正问题后重试。
        """

        if client is not None:
            try:
                await asyncio.to_thread(
                    client.close,
                    self.config.shutdown_timeout_seconds,
                )
            except Exception:
                # close 自身若异常，仍要强制回收本机进程；这不声称外部仪表已安全。
                try:
                    await asyncio.to_thread(
                        client.force_stop,
                        min(
                            self.config.shutdown_timeout_seconds,
                            1.0,
                        ),
                    )
                except Exception:
                    # 状态恢复不能再被第二个本机回收异常打断；原始 Enable 错误仍会
                    # 继续上抛，shutdown 的最终兜底还会检查全部 record。
                    pass
        record.client = None
        record.enabled = False
        record.state = "disabled"
        record.sequence_commands = ()
        self._publish(record, message)

    async def enable(self, module_id: str) -> None:
        """启动模块；保存的设置只加载到 UI，不会在 Enable 时自动应用。"""

        self._ensure_sequence_idle()
        record = self.records[module_id]
        if record.enabled or record.state == "initializing":
            return
        record.state = "initializing"
        self._publish(record, f"Initializing {record.descriptor.name}...")
        client: ModuleWorkerClient | None = None
        try:
            self._ensure_descriptor_ready(
                record.descriptor
            )
            client = ModuleWorkerClient(
                record.descriptor,
                module_dependency_directory(
                    self.app_config,
                    record.descriptor,
                ),
            )
            record.client = client
            columns = await asyncio.to_thread(
                client.start,
                self.config.startup_timeout_seconds,
            )
            # DAT schema 由已验证 worker 返回。只有握手成功才写入 descriptor，避免
            # 发现阶段 import 第三方代码，也避免清单和实际输出维护两份列定义。
            record.descriptor.columns = columns
            record.descriptor.display_columns = (
                client.display_columns
            )
            record.sequence_commands = client.sequence_commands
            result = await self._request(record, "open")
        except WorkerRequestError as exc:
            await self._reset_failed_enable(
                record,
                client,
                str(exc),
            )
            error = self._operation_error(
                record,
                exc,
                disable_failed_worker=False,
            )
            assert error is not None
            raise error from exc
        except InstrumentError as exc:
            await self._reset_failed_enable(
                record,
                client,
                str(exc),
            )
            raise
        except asyncio.CancelledError:
            await self._reset_failed_enable(
                record,
                client,
                f"Initializing {record.descriptor.name} was cancelled",
            )
            raise
        except Exception as exc:
            message = (
                f"Could not enable {record.descriptor.name}: "
                f"{type(exc).__name__}: {exc}"
            )
            await self._reset_failed_enable(
                record,
                client,
                message,
            )
            self.events.report(
                Severity.ERROR,
                f"module:{module_id}",
                "MODULE_ENABLE_FAILED",
                message,
                module_id,
            )
            raise InstrumentError(
                message,
                "MODULE_ENABLE_FAILED",
                module_id,
            ) from exc
        # 只有 start + open 全部成功后才发布 Enabled，避免 UI 短暂显示一个无法
        # 使用的模块。
        record.status.update(result)
        record.enabled = True
        record.state = "enabled"
        self.events.resolve_source(f"module:{module_id}")
        self._publish(record, f"{record.descriptor.name} enabled")

    async def disable(self, module_id: str) -> None:
        """先请求模块 close，再有界关闭 worker；close 失败仍必须回收本机资源。

        失败分支最终也会显示 Disabled，但同时抛出并锁存 Error。Disabled 在这里仅表示
        本机模块进程不可用，不表示仪表输出已被确认关闭。
        """

        self._ensure_sequence_idle()
        record = self.records[module_id]
        if not record.enabled or record.client is None:
            return
        record.state = "disabling"
        self._publish(record, f"Stopping {record.descriptor.name}...")
        client = record.client
        failure: InstrumentError | None = None
        try:
            result = await self._request(
                record,
                "module_close",
                timeout_seconds=self.config.shutdown_timeout_seconds,
            )
        except WorkerRequestError as exc:
            failure = self._operation_error(
                record,
                exc,
                disable_failed_worker=False,
            )
            assert failure is not None
        else:
            record.status.update(result)
        await asyncio.to_thread(
            # 无论模块 close 成功与否都关闭进程和 Pipe，避免退出后残留 worker。
            client.close,
            self.config.shutdown_timeout_seconds,
        )
        record.client = None
        record.enabled = False
        record.state = "disabled"
        record.sequence_commands = ()
        if failure is None:
            self.events.resolve_source(f"module:{module_id}")
            self._publish(record, f"{record.descriptor.name} disabled")
        else:
            self._publish(record, f"{record.descriptor.name} forced closed: {failure}")
            raise failure

    async def apply_settings(self, module_id: str, settings: Mapping[str, Any]) -> None:
        """仅在 SEQ Idle 且模块 Enabled 时执行用户确认过的 Apply。"""

        self._ensure_sequence_idle()
        record = self.records[module_id]
        if not record.enabled:
            raise InstrumentError("Module is disabled", "MODULE_DISABLED", module_id)
        try:
            result = await self._request(record, "configure", {"settings": dict(settings)})
        except WorkerRequestError as exc:
            error = self._operation_error(record, exc)
            assert error is not None
            raise error from exc
        record.status.update(result)
        self._publish(record, "Settings applied")

    async def refresh_status(self, module_id: str) -> dict[str, Any]:
        """读取实际状态；可恢复 Warning 返回上一次状态，Error 继续上抛。"""

        record = self.records[module_id]
        if not record.enabled:
            return deepcopy(record.status)
        try:
            result = await self._request(
                record,
                "event",
                {"name": "status", "data": {}},
            )
        except WorkerRequestError as exc:
            error = self._operation_error(record, exc, warning_allowed=True)
            if error is not None:
                raise error from exc
            return deepcopy(record.status)
        record.status.update(result)
        self._publish(record)
        return deepcopy(record.status)

    async def action(
        self, module_id: str, name: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """执行模块自定义动作；运行中由 ``_ensure_sequence_idle`` 拒绝。"""

        self._ensure_sequence_idle()
        record = self.records[module_id]
        if not record.enabled:
            raise InstrumentError("Module is disabled", "MODULE_DISABLED", module_id)
        try:
            result = await self._request(
                record,
                "event",
                {
                    "name": "action",
                    "data": {"name": name, "payload": dict(payload)},
                },
            )
        except WorkerRequestError as exc:
            error = self._operation_error(record, exc, warning_allowed=True)
            if error is not None:
                raise error from exc
            return {}
        record.status.update(result)
        self.events.report(
            Severity.INFO,
            f"module:{module_id}",
            "MANUAL_ACTION_COMPLETED",
            f"Manual action completed: {name}",
            name,
        )
        self._publish(record, f"Manual action completed: {name}")
        return result

    def enabled_descriptors(self) -> tuple[ModuleDescriptor, ...]:
        return tuple(
            record.descriptor for record in self.records.values() if record.enabled
        )

    def sequence_command_spec(
        self,
        module_id: str,
        command_id: str,
    ) -> ModuleCommandSpec | None:
        """返回当前 Enabled 模块声明的指令；Disabled 模块不形成可执行注册表。"""

        record = self.records.get(module_id)
        if record is None or not record.enabled:
            return None
        return next(
            (
                spec
                for spec in record.sequence_commands
                if spec.command_id == command_id
            ),
            None,
        )

    def sequence_command_issues(self, command: Command) -> tuple[str, ...]:
        """执行前按当前 Enabled 注册表验证通用 SEQ 信封和模块参数。"""

        key = module_command_key(command)
        if key is None:
            return ()
        module_id, command_id = key
        record = self.records.get(module_id)
        if record is None:
            return (f"Measurement module {module_id!r} is not installed",)
        if not record.enabled:
            return (f"Measurement module {module_id!r} must be Enabled",)
        spec = self.sequence_command_spec(module_id, command_id)
        if spec is None:
            return (
                f"Enabled module {module_id!r} does not declare sequence command {command_id!r}",
            )
        if command.type is not spec.command_type:
            expected = "Module Scan" if spec.kind == "scan" else "Module Command"
            return (
                f"Module command {module_id}.{command_id} must use {expected}",
            )
        return validate_module_command_parameters(spec, command.params)

    async def execute_sequence_command(
        self,
        command: Command,
        parameters: Mapping[str, Any] | None = None,
    ) -> bool:
        """在冻结模块自己的串行 worker 中执行一次模块指令。

        返回 False 表示模块以 Warning 中止了当前调用；SEQ 继续，但扫描不会在未成功
        设置该点时执行其子命令。Error/IPC 故障继续抛给 SequenceEngine 中止整次运行。
        """

        key = module_command_key(command)
        if key is None:
            raise InstrumentError(
                "The core supplied a non-module sequence command",
                "MODULE_SEQUENCE_COMMAND_INVALID",
            )
        module_id, command_id = key
        if not self._sequence_active or module_id not in self._sequence_modules:
            raise InstrumentError(
                f"Module sequence command requires the Enabled run snapshot: {module_id}",
                "MODULE_SEQUENCE_COMMAND_UNAVAILABLE",
                module_id,
            )
        record = self.records[module_id]
        spec = self.sequence_command_spec(module_id, command_id)
        if spec is None:
            raise InstrumentError(
                f"Module sequence command is unavailable: {module_id}.{command_id}",
                "MODULE_SEQUENCE_COMMAND_UNAVAILABLE",
                f"{module_id}.{command_id}",
            )
        values = dict(command.params if parameters is None else parameters)
        allowed_names = {field.name for field in spec.fields}
        if spec.kind == "scan":
            allowed_names.add(spec.point_parameter)
        extra_names = sorted(
            str(name)
            for name in values
            if str(name) not in allowed_names
        )
        if extra_names:
            raise InstrumentError(
                "Invalid module sequence command parameters: Unknown parameters: "
                + ", ".join(extra_names),
                "MODULE_SEQUENCE_COMMAND_INVALID",
                f"{module_id}.{command_id}",
            )
        if (
            spec.kind == "scan"
            and spec.point_parameter not in values
        ):
            raise InstrumentError(
                "Invalid module sequence command parameters: Missing scan point parameter "
                f"{spec.point_parameter}",
                "MODULE_SEQUENCE_COMMAND_INVALID",
                f"{module_id}.{command_id}",
            )
        # 扫描执行时会额外注入 point_parameter；先只验证 SEQ 中作者声明的字段。
        declared_values = {
            field.name: values[field.name]
            for field in spec.fields
            if field.name in values
        }
        issues = validate_module_command_parameters(spec, declared_values)
        if issues:
            raise InstrumentError(
                "Invalid module sequence command parameters: " + "; ".join(issues),
                "MODULE_SEQUENCE_COMMAND_INVALID",
                f"{module_id}.{command_id}",
            )
        try:
            result = await self._request(
                record,
                "sequence_command",
                {
                    "command_id": command_id,
                    "parameters": values,
                },
            )
        except WorkerRequestError as exc:
            if exc.severity == "cancelled":
                return False
            error = self._operation_error(
                record,
                exc,
                warning_allowed=True,
            )
            if error is not None:
                raise error from exc
            record.state = "enabled"
            self._publish(
                record,
                f"Module command warning: {command_id}",
            )
            return False
        record.status.update(result)
        record.state = "enabled"
        self._publish(record, f"Module command completed: {command_id}")
        return True

    async def prepare_sequence(
        self,
    ) -> tuple[tuple[ModuleDescriptor, ...], dict[str, dict[str, Any]]]:
        """冻结本次 Run 的 Enabled 模块集合并采集起始状态快照。"""

        descriptors = self.enabled_descriptors()
        self._sequence_active = True
        self.resume_operations()
        self._sequence_modules = tuple(item.id for item in descriptors)
        self._module_slots = {}
        self._logical_slots = (1,)
        statuses: dict[str, dict[str, Any]] = {}
        for descriptor in descriptors:
            try:
                statuses[descriptor.id] = await self.refresh_status(descriptor.id)
            except InstrumentError:
                # Error 事件会阻止 Run 继续，但仍返回最后已知状态，让运行目录留下可诊断
                # 的 status-at-start 快照，而不是在准备失败时丢掉现场。
                statuses[descriptor.id] = deepcopy(self.records[descriptor.id].status)
        return descriptors, statuses

    async def begin_sequence(self) -> None:
        """并行通知全部冻结模块 Run 开始；任一失败终止 Run。"""

        begin_cancelled = False

        async def begin(module_id: str) -> InstrumentError | None:
            nonlocal begin_cancelled
            record = self.records[module_id]
            try:
                result = await self._request(
                    record,
                    "event",
                    {"name": "run_start", "data": {}},
                )
            except WorkerRequestError as exc:
                if exc.severity == "cancelled":
                    # Stop 可能恰好发生在模块的 ARM/settle checkpoint。它是正常
                    # 控制流，不应把模块标成 Faulted 或额外报告 Error；SequenceEngine
                    # 会在 begin 返回后的统一 checkpoint 转入 stopped/error 收尾。
                    record.state = "enabled"
                    begin_cancelled = True
                    self._publish(
                        record,
                        "Sequence start cancelled",
                    )
                    return None
                record.state = "faulted"
                self._publish(record, str(exc))
                return self._operation_error(record, exc)
            record.status.update(result)
            record.state = "enabled"
            self._publish(record, "Sequence started")
            return None

        failures = await asyncio.gather(*(begin(item) for item in self._sequence_modules))
        first = next((item for item in failures if item is not None), None)
        if first is not None:
            raise first
        if begin_cancelled:
            return
        await self._prepare_slots()

    async def _prepare_slots(self) -> None:
        """读取可选 ``slots`` 属性并冻结本次 SEQ 的逻辑通道计划。

        返回槽位的模块只在这些槽位参与测量；没有实现钩子（worker 返回 ``None``）
        的模块跟随所有槽位。若所有模块都未定义槽位，则本次 T Measure 只有一行。
        """

        async def read_slots(
            module_id: str,
        ) -> tuple[str, frozenset[int] | None, InstrumentError | None]:
            record = self.records[module_id]
            try:
                result = await self._request(
                    record,
                    "slots",
                )
                raw_slots = result.get("slots")
                if raw_slots is None:
                    return module_id, None, None
                if not isinstance(raw_slots, list):
                    raise InstrumentError(
                        "Module.slots must be a JSON array",
                        "MODULE_MEASUREMENT_SLOTS_INVALID",
                        module_id,
                    )
                if not raw_slots or len(raw_slots) > _MAX_LOGICAL_SLOTS:
                    raise InstrumentError(
                        "Module.slots must expose 1 to "
                        f"{_MAX_LOGICAL_SLOTS} logical slots",
                        "MODULE_MEASUREMENT_SLOTS_INVALID",
                        module_id,
                    )
                normalized: list[int] = []
                for value in raw_slots:
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 1
                    ):
                        raise InstrumentError(
                            "Logical slots must be positive integers",
                            "MODULE_MEASUREMENT_SLOTS_INVALID",
                            module_id,
                        )
                    normalized.append(value)
                if len(normalized) != len(set(normalized)):
                    raise InstrumentError(
                        "Logical slots must not contain duplicates",
                        "MODULE_MEASUREMENT_SLOTS_INVALID",
                        module_id,
                    )
                return module_id, frozenset(normalized), None
            except WorkerRequestError as exc:
                return module_id, None, self._operation_error(
                    record,
                    exc,
                )
            except InstrumentError as exc:
                self.events.report(
                    Severity.ERROR,
                    f"module:{module_id}",
                    exc.code,
                    str(exc),
                    exc.context,
                )
                return module_id, None, exc

        results = await asyncio.gather(
            *(read_slots(module_id) for module_id in self._sequence_modules)
        )
        first = next(
            (error for _module, _slots, error in results if error),
            None,
        )
        if first is not None:
            raise first
        self._module_slots = {
            module_id: slots
            for module_id, slots, _error in results
            if slots is not None
        }
        declared_slots = {
            slot
            for slots in self._module_slots.values()
            for slot in slots
        }
        self._logical_slots = tuple(sorted(declared_slots)) if declared_slots else (1,)

    def _validated_row(self, descriptor: ModuleDescriptor, values: Mapping[str, Any]) -> dict[str, Any]:
        """按后端声明的 Schema 校验一行，拒绝未知列、复杂对象和 NaN/Infinity。"""

        allowed = {column.name for column in descriptor.columns}
        unknown = set(values) - allowed
        if unknown:
            raise InstrumentError(
                f"Module emitted undeclared columns: {', '.join(sorted(unknown))}",
                "MODULE_SCHEMA_VIOLATION",
                descriptor.id,
            )
        result: dict[str, Any] = {}
        for key, value in values.items():
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise InstrumentError(
                    f"Column {key} has unsupported value type {type(value).__name__}",
                    "MODULE_ROW_TYPE_ERROR",
                    descriptor.id,
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise InstrumentError(
                    f"Column {key} contains NaN or infinity",
                    "MODULE_ROW_VALUE_ERROR",
                    descriptor.id,
                )
            result[key] = value
        return result

    @staticmethod
    def _validated_raw_values(
        module_id: str,
        values: object | None,
    ) -> tuple[float, ...] | None:
        """验证与一个正式 DAT 行绑定的有限原始数值序列。

        原始序列不进入动态 DAT Schema，但仍通过同一 IPC 和运行日志边界。限制为
        32,768 点保证即使每个 Python float 都采用最长 JSON 表示，整条事件仍能留在
        worker 的 1 MiB IPC 上限内，也防止第三方模块借此发送无界对象占满主进程内存。
        """

        if values is None:
            return None
        if not isinstance(values, list):
            raise InstrumentError(
                "Module raw data must be a JSON array",
                "MODULE_RAW_DATA_TYPE_ERROR",
                module_id,
            )
        if len(values) > _MAX_RAW_VALUES:
            raise InstrumentError(
                "Module raw data must contain at most "
                f"{_MAX_RAW_VALUES} values",
                "MODULE_RAW_DATA_SIZE_ERROR",
                module_id,
            )
        result: list[float] = []
        for index, value in enumerate(values, start=1):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InstrumentError(
                    f"Raw value {index} is not numeric",
                    "MODULE_RAW_DATA_TYPE_ERROR",
                    module_id,
                )
            normalized = float(value)
            if not math.isfinite(normalized):
                raise InstrumentError(
                    f"Raw value {index} contains NaN or infinity",
                    "MODULE_RAW_DATA_VALUE_ERROR",
                    module_id,
                )
            result.append(normalized)
        return tuple(result)

    async def measure_all(self, logger: DatRunLogger, sequence_step: str) -> None:
        """展开一次 ``T Measure``，按逻辑槽位逐行写入同轮模块结果。

        声明 ``slots`` 的模块限定自己参与的逻辑槽位；未声明的模块
        跟随每个槽位。一次 ``measure()`` 最多产生一行，未参与或只报告 Warning
        的模块在该行保留空列。
        """

        if not self._sequence_modules:
            self.events.report(
                Severity.WARNING,
                "modules",
                "NO_ENABLED_MODULES",
                "Measure continued without an enabled measurement module",
            )
            await self._fresh_system_payload(
                reuse_within_seconds=_MEASUREMENT_SAMPLE_REUSE_SECONDS,
            )
            logger.write_system_row(self.instruments.snapshots(), sequence_step)
            return
        self.events.resolve("modules", "NO_ENABLED_MODULES")
        # 每个 T Measure 都是一组新的可见结果。先清空上一组缓存，防止本轮某个
        # 通道返回空值或 Warning 时，主窗口继续展示上一次的旧数字。
        for module_id in self._sequence_modules:
            record = self.records[module_id]
            if record.descriptor.display_columns:
                self.message_callback(
                    "module_results_reset",
                    {"module_id": module_id},
                )

        async def wait_for_slot() -> bool:
            """在相邻槽位之间响应 Pause/Stop，不启动新的仪表事务。"""

            while True:
                with self._operation_state_lock:
                    state = self._operation_state
                if state in {"stopping", "cancelled"}:
                    return False
                if state != "paused":
                    return True
                await asyncio.sleep(0.05)

        async def measure_one(
            module_id: str,
            logical_slot: int,
            slot_index: int,
            slot_count: int,
        ) -> _ModuleSlotResult:
            record = self.records[module_id]
            descriptor = record.descriptor
            record.state = "measuring"
            self._publish(
                record,
                f"Measuring logical slot {logical_slot} "
                f"({slot_index}/{slot_count})",
            )
            try:
                result = await self._request(
                    record,
                    "measure",
                    {"slot": logical_slot},
                )
                raw_values = result.get("values")
                if not isinstance(raw_values, dict):
                    missing = InstrumentError(
                        "Module.measure() did not return a row mapping",
                        "MODULE_MEASUREMENT_ROW_MISSING",
                        module_id,
                    )
                    self.events.report(
                        Severity.ERROR,
                        f"module:{module_id}",
                        missing.code,
                        str(missing),
                        missing.context,
                    )
                    self._publish_measurement_result(
                        record,
                        logical_slot,
                        None,
                    )
                    return _ModuleSlotResult(
                        module_id,
                        error=missing,
                    )
                values = self._validated_row(descriptor, raw_values)
                validated_raw = self._validated_raw_values(
                    module_id,
                    result.get("raw_values"),
                )
                self._publish_measurement_result(
                    record,
                    logical_slot,
                    values,
                )
                return _ModuleSlotResult(
                    module_id,
                    values,
                    validated_raw,
                )
            except WorkerRequestError as exc:
                if exc.severity == "cancelled":
                    return _ModuleSlotResult(
                        module_id,
                        cancelled=True,
                    )
                self._publish_measurement_result(
                    record,
                    logical_slot,
                    None,
                )
                return _ModuleSlotResult(
                    module_id,
                    error=self._operation_error(
                        record,
                        exc,
                        warning_allowed=True,
                    ),
                )
            except InstrumentError as exc:
                self.events.report(Severity.ERROR, f"module:{module_id}", exc.code, str(exc), exc.context)
                self._publish_measurement_result(
                    record,
                    logical_slot,
                    None,
                )
                return _ModuleSlotResult(
                    module_id,
                    error=exc,
                )
            finally:
                if record.state == "measuring":
                    record.state = "enabled"
                self._publish(
                    record,
                    f"Logical slot {logical_slot} complete",
                )

        slot_count = len(self._logical_slots)
        for slot_index, logical_slot in enumerate(
            self._logical_slots,
            start=1,
        ):
            if not await wait_for_slot():
                return
            participants = tuple(
                module_id
                for module_id in self._sequence_modules
                if (
                    module_id not in self._module_slots
                    or logical_slot
                    in self._module_slots.get(module_id, frozenset())
                )
            )
            results = await asyncio.gather(
                *(
                    measure_one(
                        module_id,
                        logical_slot,
                        slot_index,
                        slot_count,
                    )
                    for module_id in participants
                )
            )
            if any(item.cancelled for item in results):
                # Stop 期间可能有较快模块先完成，但这一通道槽位不是完整事务；不把部分
                # 结果写成看似成功的正式行。SequenceEngine 的 checkpoint 负责收尾。
                return
            values = {
                item.module_id: item.values
                for item in results
                if item.values is not None
            }
            raw_values = {
                item.module_id: item.raw_values
                for item in results
                if item.raw_values is not None
            }
            await self._fresh_system_payload(
                reuse_within_seconds=_MEASUREMENT_SAMPLE_REUSE_SECONDS,
            )
            logger.write_measurement_row(
                # 每行使用写入前的即时系统快照；同一时刻并行模块发起的请求会合并，
                # 且刚在 0.1 秒内读过时不会为了写行再次敲击慢速仪表。
                self.instruments.snapshots(),
                values,
                sequence_step,
                raw_values=raw_values,
            )
            first = next(
                (item.error for item in results if item.error is not None),
                None,
            )
            if first is not None:
                raise first

    async def end_sequence(self, reason: str) -> bool:
        """并行结束本次模块运行，并在任何结果下解除冻结状态。

        先切回 running 是为了释放仍停在 Pause checkpoint 的 worker，使其能够执行
        ``run_end``。返回 False 由 SequenceEngine 转为 Faulted；SEQ 不控制 System Instrument。
        """

        async def end(module_id: str) -> bool:
            record = self.records[module_id]
            try:
                result = await self._request(
                    record,
                    "event",
                    {"name": "run_end", "data": {"reason": reason}},
                )
            except WorkerRequestError as exc:
                record.state = "faulted"
                self._publish(record, str(exc))
                self._operation_error(record, exc)
                return False
            record.status.update(result)
            record.state = "enabled"
            self._publish(record, f"Sequence ended: {reason}")
            return True

        try:
            self.resume_operations()
            results = await asyncio.gather(*(end(item) for item in self._sequence_modules))
            return all(results)
        finally:
            # 即使某个 end 请求异常，也必须允许 UI 之后 Disable/恢复模块。
            self._sequence_modules = ()
            self._module_slots = {}
            self._logical_slots = (1,)
            self._sequence_active = False
            with self._operation_state_lock:
                self._operation_state = "idle"

    def pause_operations(self) -> None:
        """让模块下一次 context checkpoint 进入 Paused；不直接改变仪表输出。"""

        with self._operation_state_lock:
            if self._operation_state == "running":
                self._operation_state = "paused"

    def resume_operations(self) -> None:
        """让模块 checkpoint 继续，并用于 end_sequence 前解除 Pause。"""

        with self._operation_state_lock:
            self._operation_state = "running"

    def cancel_operations(self) -> None:
        """让模块 checkpoint 抛出协作取消；不能中断正在阻塞的第三方驱动调用。"""

        with self._operation_state_lock:
            self._operation_state = "stopping"

    async def shutdown(self) -> None:
        """并行关闭全部 worker，每个模块共用一个 module-close 总时限。"""

        async def stop(record: ModuleRuntimeRecord) -> None:
            if record.client is None:
                return
            client = record.client
            deadline = (
                time.monotonic()
                + self.config.shutdown_timeout_seconds
            )
            if record.enabled:
                try:
                    remaining = max(
                        0.001,
                        deadline - time.monotonic(),
                    )
                    await self._request(
                        record,
                        "module_close",
                        timeout_seconds=remaining,
                    )
                except WorkerRequestError as exc:
                    # 记录 Error 后仍继续 close。这里的目标是保证应用进程可退出，而不是
                    # 把未确认的 close 包装成成功。
                    self._operation_error(
                        record,
                        exc,
                        disable_failed_worker=False,
                    )
            remaining = max(
                0.001,
                deadline - time.monotonic(),
            )
            await asyncio.to_thread(client.close, remaining)
            record.client = None
            record.enabled = False
            record.state = "disabled"
            record.sequence_commands = ()
            self._publish(record, "Application closing")

        await asyncio.gather(*(stop(record) for record in self.records.values()))
