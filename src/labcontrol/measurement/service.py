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
from ..devices.base import DeviceError
from ..events import EventManager
from ..extensions.trust import (
    PluginTrustStore,
    extension_tree_digest,
)
from ..models import DeviceSnapshot, Severity
from ..plugins import DeviceManager
from .manifest import (
    ModuleDescriptor,
    module_dependency_errors,
    module_dependency_directory,
)
from .worker import ModuleWorkerClient, WorkerRequestError


ModuleMessageCallback = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class ModuleRuntimeRecord:
    """一个模块在当前应用进程中的易失运行状态；不会跨启动恢复 Enabled。"""

    descriptor: ModuleDescriptor
    enabled: bool = False
    state: str = "disabled"
    status: dict[str, Any] = field(default_factory=dict)
    client: ModuleWorkerClient | None = None


class MeasurementModuleService:
    """从核心事件循环协调模块信任、worker、SEQ 和 DAT 写入。

    关键不变量：

    - 只有 worker 启动并完成 ``initialize`` 后，``enabled`` 才能变为 True；
    - Run 开始时冻结模块 ID 集合，运行中不能 Enable/Disable/Refresh；
    - 同一 Measure 并行等待所有冻结模块，但每个模块自己的 IPC 保持串行；
    - worker 发出的每一行都先验证固定 Schema 和有限数值，再进入 DAT；
    - abort 失败后仍回收本机 worker，但保留 Error，不能把 Disabled 当作仪表安全确认。
    """

    def __init__(
        self,
        descriptors: tuple[ModuleDescriptor, ...],
        events: EventManager,
        devices: DeviceManager,
        message_callback: ModuleMessageCallback | None = None,
    ) -> None:
        self.events = events
        self.devices = devices
        self.app_config = devices.config
        self.config = devices.config.modules
        self.trust_store = PluginTrustStore(
            devices.config.resolve_project_path(
                devices.config.plugins.state_directory
            )
            / "trusted_plugins.json"
        )
        self.message_callback = message_callback or (lambda _kind, _payload: None)
        self.records = {
            item.id: ModuleRuntimeRecord(item)
            for item in descriptors
            if item.valid
        }
        self._sequence_modules: tuple[str, ...] = ()
        self._sequence_active = False
        self._row_handlers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._operation_state = "idle"
        self._operation_state_lock = threading.RLock()

    def _ensure_sequence_idle(self) -> None:
        if self._sequence_active:
            raise DeviceError(
                "Module changes and manual actions are unavailable while a SEQ is running",
                "MODULE_OPERATION_DURING_SEQUENCE",
            )

    def _ensure_descriptor_ready(
        self,
        descriptor: ModuleDescriptor,
    ) -> None:
        """在每次 Enable 前重新核对内容、信任记录和隔离依赖。"""

        if not descriptor.can_enable:
            raise DeviceError(
                descriptor.error or descriptor.dependency_error,
                "MODULE_NOT_ENABLEABLE",
                descriptor.id,
            )
        # UI 发现与用户点击 Enable 之间可能隔很久；不能只相信发现阶段缓存的摘要。
        current_fingerprint = extension_tree_digest(
            descriptor.path
        )
        if current_fingerprint != descriptor.fingerprint:
            raise DeviceError(
                f"Measurement module {descriptor.id} changed "
                "after discovery",
                "MODULE_CHANGED_AFTER_DISCOVERY",
                descriptor.id,
            )
        if not self.trust_store.is_trusted(
            "module",
            descriptor,
        ):
            raise DeviceError(
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
            raise DeviceError(
                "Invalid isolated module dependencies: "
                + "; ".join(dependency_errors),
                "MODULE_DEPENDENCIES_INVALID",
                descriptor.id,
            )

    def replace_descriptors(self, descriptors: tuple[ModuleDescriptor, ...]) -> None:
        """在 SEQ Idle 且全部模块 Disabled 时替换发现列表，不做热加载。"""

        self._ensure_sequence_idle()
        if any(record.enabled or record.client is not None for record in self.records.values()):
            raise DeviceError(
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
        })

    def _system_payload(self) -> dict[str, dict[str, Any]]:
        """把核心 DeviceSnapshot 转成只读、可 JSON 化的模块视图。"""

        payload: dict[str, dict[str, Any]] = {}
        for device_id, snapshot in self.devices.snapshots().items():
            device_config = self.devices.device_configs.get(
                device_id
            )
            payload[device_id] = {
                "display_name": snapshot.display_name,
                "kind": snapshot.kind.value,
                "role": (
                    ""
                    if device_config is None
                    else device_config.role.value
                ),
                "control_enabled": (
                    False
                    if device_config is None
                    else device_config.control_enabled
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
            }
        return payload

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
                return {"system": self._system_payload()}
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
        elif kind == "row":
            # 只有 measure_one 安装临时 handler 的窗口内才接受行。初始化或手动动作即使
            # 错误地 emit_row，也不会污染实验 DAT。
            handler = self._row_handlers.get(module_id)
            if handler is not None:
                handler(dict(message.get("values", {})))

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
        # 新快照。这样既支持两点平均，也不会把可变 DeviceManager 暴露给子进程。
        request_payload["system"] = self._system_payload()
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
    ) -> DeviceError | None:
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
            self._publish(record, str(error))
        if warning_allowed and severity is Severity.WARNING:
            return None
        return DeviceError(str(error), error.code, error.context)

    async def enable(self, module_id: str, settings: Mapping[str, Any]) -> None:
        """启动并初始化模块；不会自动调用 apply_settings。"""

        self._ensure_sequence_idle()
        record = self.records[module_id]
        if record.enabled or record.state == "initializing":
            return
        self._ensure_descriptor_ready(record.descriptor)
        record.state = "initializing"
        self._publish(record, f"Initializing {record.descriptor.name}...")
        client = ModuleWorkerClient(
            record.descriptor,
            module_dependency_directory(
                self.app_config,
                record.descriptor,
            ),
        )
        record.client = client
        try:
            await asyncio.to_thread(
                client.start,
                self.config.startup_timeout_seconds,
            )
            result = await self._request(record, "initialize", {"settings": dict(settings)})
        except WorkerRequestError as exc:
            await asyncio.to_thread(
                client.close,
                self.config.shutdown_timeout_seconds,
            )
            record.client = None
            record.enabled = False
            record.state = "disabled"
            self._publish(record, str(exc))
            error = self._operation_error(
                record,
                exc,
                disable_failed_worker=False,
            )
            assert error is not None
            raise error from exc
        # 只有 start + initialize 全部成功后才发布 Enabled，避免 UI 短暂显示一个无法
        # 使用的模块。
        record.status.update(result)
        record.enabled = True
        record.state = "enabled"
        self.events.resolve_source(f"module:{module_id}")
        self._publish(record, f"{record.descriptor.name} enabled")

    async def disable(self, module_id: str) -> None:
        """先请求 abort，再有界关闭 worker；abort 失败仍必须回收本机资源。

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
        failure: DeviceError | None = None
        try:
            result = await self._request(
                record,
                "abort",
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
            # 无论 abort 成功与否都关闭进程和 Pipe，避免退出后残留 worker。
            client.close,
            self.config.shutdown_timeout_seconds,
        )
        record.client = None
        record.enabled = False
        record.state = "disabled"
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
            raise DeviceError("Module is disabled", "MODULE_DISABLED", module_id)
        try:
            result = await self._request(record, "apply_settings", {"settings": dict(settings)})
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
            result = await self._request(record, "read_status")
        except WorkerRequestError as exc:
            error = self._operation_error(record, exc, warning_allowed=True)
            if error is not None:
                raise error from exc
            return deepcopy(record.status)
        record.status.update(result)
        self._publish(record)
        return deepcopy(record.status)

    async def manual_action(
        self, module_id: str, name: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """执行模块自定义手动动作；运行中由 ``_ensure_sequence_idle`` 拒绝。"""

        self._ensure_sequence_idle()
        record = self.records[module_id]
        if not record.enabled:
            raise DeviceError("Module is disabled", "MODULE_DISABLED", module_id)
        try:
            result = await self._request(
                record,
                "manual_action",
                {"name": name, "data": dict(payload)},
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

    async def prepare_sequence(
        self, settings: Mapping[str, Mapping[str, Any]]
    ) -> tuple[tuple[ModuleDescriptor, ...], dict[str, dict[str, Any]]]:
        """冻结本次 Run 的 Enabled 模块集合并采集起始状态快照。"""

        descriptors = self.enabled_descriptors()
        self._sequence_active = True
        self.resume_operations()
        self._sequence_modules = tuple(item.id for item in descriptors)
        statuses: dict[str, dict[str, Any]] = {}
        for descriptor in descriptors:
            try:
                statuses[descriptor.id] = await self.refresh_status(descriptor.id)
            except DeviceError:
                # Error 事件会阻止 Run 继续，但仍返回最后已知状态，让运行目录留下可诊断
                # 的 status-at-start 快照，而不是在准备失败时丢掉现场。
                statuses[descriptor.id] = deepcopy(self.records[descriptor.id].status)
        return descriptors, statuses

    async def begin_sequence(self) -> None:
        """并行调用全部冻结模块的 begin_sequence；任一失败终止 Run。"""

        async def begin(module_id: str) -> DeviceError | None:
            record = self.records[module_id]
            try:
                result = await self._request(record, "begin_sequence")
            except WorkerRequestError as exc:
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

    def _validated_row(self, descriptor: ModuleDescriptor, values: Mapping[str, Any]) -> dict[str, Any]:
        """按 manifest 固定 Schema 校验一行，拒绝未知列、复杂对象和 NaN/Infinity。"""

        allowed = {column.name for column in descriptor.columns}
        unknown = set(values) - allowed
        if unknown:
            raise DeviceError(
                f"Module emitted undeclared columns: {', '.join(sorted(unknown))}",
                "MODULE_SCHEMA_VIOLATION",
                descriptor.id,
            )
        result: dict[str, Any] = {}
        for key, value in values.items():
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise DeviceError(
                    f"Column {key} has unsupported value type {type(value).__name__}",
                    "MODULE_ROW_TYPE_ERROR",
                    descriptor.id,
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise DeviceError(
                    f"Column {key} contains NaN or infinity",
                    "MODULE_ROW_VALUE_ERROR",
                    descriptor.id,
                )
            result[key] = value
        return result

    async def measure_all(self, logger: DatRunLogger, sequence_step: str) -> None:
        """并行执行全部模块的一次 Measure，并允许每个模块流式产生多行。

        如果没有 Enabled 模块，写一行系统状态并去重报告 Warning。若模块都没有产生
        有效行，也写系统行，确保该 SEQ Measure 在 DAT 中仍有可追踪记录。
        """

        if not self._sequence_modules:
            self.events.report(
                Severity.WARNING,
                "modules",
                "NO_ENABLED_MODULES",
                "Measure continued without an enabled measurement module",
            )
            logger.write_system_row(self.devices.snapshots(), sequence_step)
            return
        self.events.resolve("modules", "NO_ENABLED_MODULES")
        emitted = 0

        async def measure_one(module_id: str) -> DeviceError | None:
            nonlocal emitted
            record = self.records[module_id]
            descriptor = record.descriptor
            validation_error: DeviceError | None = None

            def write_row(values: dict[str, Any]) -> None:
                nonlocal emitted, validation_error
                try:
                    validated = self._validated_row(descriptor, values)
                except DeviceError as exc:
                    validation_error = exc
                    self.events.report(
                        Severity.ERROR,
                        f"module:{module_id}",
                        exc.code,
                        str(exc),
                        exc.context,
                    )
                    return
                logger.write_module_row(
                    # 每一行使用写入当时的最新系统快照；不同通道或不同模块的多行结果
                    # 因此可以保留各自行发生时的温场状态。
                    self.devices.snapshots(), module_id, validated, sequence_step
                )
                emitted += 1

            self._row_handlers[module_id] = write_row
            record.state = "measuring"
            self._publish(record, "Measuring")
            try:
                result = await self._request(record, "measure")
                if result:
                    write_row(result)
                if validation_error is not None:
                    return validation_error
            except WorkerRequestError as exc:
                if exc.severity == "cancelled":
                    return None
                return self._operation_error(record, exc, warning_allowed=True)
            except DeviceError as exc:
                self.events.report(Severity.ERROR, f"module:{module_id}", exc.code, str(exc), exc.context)
                return exc
            finally:
                self._row_handlers.pop(module_id, None)
                if record.state == "measuring":
                    record.state = "enabled"
                self._publish(record, "Measurement complete")
            return None

        # gather 让不同模块并行等待；ModuleWorkerClient 仍保证每个模块内部请求串行。
        failures = await asyncio.gather(*(measure_one(item) for item in self._sequence_modules))
        if emitted == 0:
            logger.write_system_row(self.devices.snapshots(), sequence_step)
        first = next((item for item in failures if item is not None), None)
        if first is not None:
            raise first

    async def end_sequence(self, reason: str) -> bool:
        """并行结束本次模块运行，并在任何结果下解除冻结状态。

        先切回 running 是为了释放仍停在 Pause checkpoint 的 worker，使其能够执行
        ``end_sequence``。返回 False 由 SequenceEngine 转为 Faulted 并尝试 Hold。
        """

        async def end(module_id: str) -> bool:
            record = self.records[module_id]
            try:
                result = await self._request(record, "end_sequence", {"reason": reason})
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
        """并行关闭全部 worker，每个模块共用一个 abort+close 总时限。"""

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
                        "abort",
                        timeout_seconds=remaining,
                    )
                except WorkerRequestError as exc:
                    # 记录 Error 后仍继续 close。这里的目标是保证应用进程可退出，而不是
                    # 把未确认的 abort 包装成成功。
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
            self._publish(record, "Application closing")

        await asyncio.gather(*(stop(record) for record in self.records.values()))
