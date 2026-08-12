"""系统仪表实例管理、访问串行化、安全限制与断线恢复。

``InstrumentManager`` 是所有温度、磁场和只读 Monitor 的唯一运行时入口。它把每台仪表限制为
一个异步锁，统一执行操作超时、读数校验、目标上下限、速率限制和 1 分钟重连策略。SEQ
运行期间还会取得控制权租约，使手动调用即使绕过 GUI 按钮也无法修改主控目标。

外部 System Instrument 默认在独立进程中运行；内置模拟仪表可以进程内运行。两种客户端在本层
之后使用相同的安全和状态恢复逻辑。
"""

from __future__ import annotations

import asyncio
import heapq
import math
import re
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import TypeVar

from .config import AppConfig, InstrumentConfig
from .instruments.base import InstrumentError, SystemInstrument, InstrumentWarning, SafetyViolation
from .instruments.manifest import (
    SystemInstrumentDescriptor,
    instrument_dependency_directory,
)
from .instruments.worker import (
    InstrumentWorkerClient,
    InstrumentWorkerSpec,
    InProcessInstrumentClient,
    IsolatedInstrumentClient,
)
from .events import EventManager
from .package_support.dependencies import (
    dependency_runtime_errors,
)
from .package_support.loading import load_import_object, load_source_object
from .package_support.trust import ContentTrustStore, content_tree_digest
from .models import (
    InstrumentActivity,
    InstrumentConnectionState,
    InstrumentKind,
    InstrumentMetric,
    InstrumentRole,
    InstrumentSnapshot,
    Severity,
    StabilityState,
)
from .stability import StabilityEvaluator


T = TypeVar("T")
_METRIC_KEY = re.compile(r"^[a-z][a-z0-9_]*$")

# 同一台仪表始终只允许一个操作进入后端。控制与安全操作不能被读数越过；
# 测量专用读取可以越过尚未开始的后台状态轮询，但不会中断已经开始的仪表事务。
_INSTRUMENT_CONTROL_PRIORITY = 0
_INSTRUMENT_MEASUREMENT_PRIORITY = 10
_INSTRUMENT_BACKGROUND_PRIORITY = 20


class _PriorityOperationGate:
    """按优先级串行化单台仪表的操作，并在同优先级内保持先来先执行。

    ``asyncio.Lock`` 只保证互斥，不承诺测量读取能越过已经排队的后台轮询。这里把等待者
    放入小根堆：数值越小优先级越高，递增序号保证同级 FIFO。已经取得执行权的操作绝不
    被抢占，因此底层已经发送的一整条仪表命令一定先正常返回或超时。
    """

    def __init__(self) -> None:
        self._locked = False
        self._sequence = 0
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []

    async def acquire(self, priority: int) -> None:
        """等待执行权；任务取消时从队列移除，避免遗留死锁。"""

        if not self._locked:
            self._locked = True
            return

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._sequence += 1
        heapq.heappush(self._waiters, (priority, self._sequence, waiter))
        try:
            # shield 防止任务取消顺带取消 Future；这样可以区分“尚在等待”和“已经获准”。
            await asyncio.shield(waiter)
        except BaseException:
            if waiter.done():
                # release 已把执行权交给本任务，但任务在恢复运行前被取消。
                self.release()
            else:
                waiter.cancel()
            raise

    def release(self) -> None:
        """把执行权交给当前最高优先级的有效等待者。"""

        if not self._locked:
            raise RuntimeError("Instrument operation gate is not acquired")
        while self._waiters:
            _priority, _sequence, waiter = heapq.heappop(self._waiters)
            if waiter.cancelled():
                continue
            waiter.set_result(None)
            return
        self._locked = False


class InstrumentManager:
    """拥有全部仪表客户端、最新快照和连接恢复状态的异步管理器。"""

    def __init__(
        self,
        config: AppConfig,
        events: EventManager,
        descriptors: tuple[SystemInstrumentDescriptor, ...] = (),
        *,
        isolate_processes: bool = True,
    ) -> None:
        """建立仪表映射和安全状态；此阶段只实例化客户端，不连接真实仪表。"""

        self.config = config
        self.events = events
        self.descriptors = {descriptor.id: descriptor for descriptor in descriptors}
        self.isolate_processes = isolate_processes
        self.instruments: dict[str, object] = {}
        self._client_factories: dict[str, Callable[[], object]] = {}
        self.instrument_configs: dict[str, InstrumentConfig] = {item.id: item for item in config.instruments}
        self._operation_gates: dict[str, _PriorityOperationGate] = {}
        self._stability: dict[str, StabilityEvaluator] = {}
        self._poll_issues: dict[str, set[tuple[str, str]]] = {}
        self._stale_instruments: set[str] = set()
        self._unavailable_after_timeout: dict[str, str] = {}
        self._control_owner: str | None = None
        self._connection_states: dict[str, InstrumentConnectionState] = {}
        self._recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self._recovery_clients: dict[str, object] = {}
        self._generation: dict[str, int] = {}
        self._expected_targets: dict[str, float | None] = {}
        self._expected_rates: dict[str, float | None] = {}
        self._metric_schemas: dict[
            str,
            tuple[tuple[str, str, str, int | None], ...],
        ] = {}
        self._shutting_down = False
        self.latest: dict[str, InstrumentSnapshot] = {}
        self._load_instruments()

    def _load_instruments(self) -> None:
        """根据已验证配置创建仪表客户端，但暂不连接真实仪表。

        外部 System Instrument 必须同时满足清单有效、目录指纹仍匹配信任记录、API 版本兼容
        和隔离依赖完整；
        不能通过在配置中直接写任意 ``module:class`` 来加载第三方代码。
        """

        trust_store: ContentTrustStore | None = None
        for instrument_config in self.config.instruments:
            descriptor: SystemInstrumentDescriptor | None = None
            if ":" in instrument_config.backend:
                module_name = instrument_config.backend.split(":", 1)[0]
                if not module_name.startswith("labcontrol.instruments."):
                    raise PermissionError(
                        "Unmanifested third-party instrument imports are disabled; "
                        f"copy {instrument_config.backend!r} into system_instruments with instrument.toml"
                    )
            else:
                descriptor = self.descriptors.get(instrument_config.backend)
                if descriptor is None:
                    raise ValueError(
                        f"Unknown external system instrument {instrument_config.backend!r}"
                    )
                if not descriptor.can_load:
                    raise ValueError(
                        f"System Instrument {descriptor.id} is invalid: {descriptor.error}"
                    )
                if instrument_config.kind not in descriptor.kinds:
                    raise TypeError(
                        f"System Instrument {descriptor.id} does not support "
                        f"{instrument_config.kind.value}"
                    )
                current_fingerprint = content_tree_digest(descriptor.path)
                if current_fingerprint != descriptor.fingerprint:
                    raise PermissionError(
                        f"System Instrument {descriptor.id} changed after discovery"
                    )
                if trust_store is None:
                    trust_store = ContentTrustStore(
                        self.config.resolve_project_path(
                            self.config.system_instruments.state_directory
                        )
                        / "trusted_content.json"
                    )
                if not trust_store.is_trusted("instrument", descriptor):
                    raise PermissionError(
                        f"System Instrument {descriptor.id} has not been trusted"
                    )
                dependency_directory = instrument_dependency_directory(
                    self.config,
                    descriptor,
                )
                runtime_errors = dependency_runtime_errors(
                    descriptor.dependencies,
                    dependency_directory,
                    descriptor.fingerprint,
                )
                if runtime_errors:
                    raise PermissionError(
                        f"System Instrument {descriptor.id} has invalid isolated "
                        "dependencies: "
                        + "; ".join(runtime_errors)
                    )
            if self.isolate_processes:
                dependency_directory = (
                    ""
                    if descriptor is None
                    else str(instrument_dependency_directory(self.config, descriptor))
                )
                worker_spec = InstrumentWorkerSpec(
                    instrument_config=instrument_config,
                    simulation_speed=self.config.simulation_speed,
                    instrument_id=(
                        "builtin"
                        if descriptor is None
                        else descriptor.id
                    ),
                    backend=(
                        instrument_config.backend
                        if descriptor is None
                        else descriptor.backend
                    ),
                    instrument_directory=(
                        ""
                        if descriptor is None
                        else str(descriptor.path)
                    ),
                    fingerprint=(
                        ""
                        if descriptor is None
                        else descriptor.fingerprint
                    ),
                    dependency_directory=dependency_directory,
                    dependencies=(
                        ()
                        if descriptor is None
                        else descriptor.dependencies
                    ),
                )
                def isolated_factory(
                    spec: InstrumentWorkerSpec = worker_spec,
                    configured: InstrumentConfig = instrument_config,
                ) -> IsolatedInstrumentClient:
                    return IsolatedInstrumentClient(
                        InstrumentWorkerClient(spec),
                        startup_timeout_seconds=(
                            self.config.system_instruments.startup_timeout_seconds
                        ),
                        operation_timeout_seconds=(
                            configured.operation_timeout_seconds
                        ),
                        shutdown_timeout_seconds=(
                            configured.shutdown_timeout_seconds
                        ),
                    )

                self._client_factories[instrument_config.id] = isolated_factory
                self.instruments[instrument_config.id] = isolated_factory()
            else:
                instrument_class = (
                    load_import_object(instrument_config.backend)
                    if descriptor is None
                    else load_source_object(
                        descriptor.path,
                        descriptor.backend,
                        f"instrument_{descriptor.id}",
                    )
                )
                if (
                    not isinstance(instrument_class, type)
                    or not issubclass(instrument_class, SystemInstrument)
                ):
                    raise TypeError(f"{instrument_config.backend} is not a SystemInstrument")
                if (
                    str(getattr(instrument_class, "api_version", ""))
                    != SystemInstrument.api_version
                ):
                    raise TypeError(
                        f"{instrument_config.backend} uses incompatible instrument API "
                        f"{getattr(instrument_class, 'api_version', '')!r}"
                    )
                def in_process_factory(
                    backend_class: type[SystemInstrument] = instrument_class,
                    configured: InstrumentConfig = instrument_config,
                ) -> InProcessInstrumentClient:
                    return InProcessInstrumentClient(
                        backend_class(
                            configured,
                            simulation_speed=self.config.simulation_speed,
                        )
                    )

                self._client_factories[instrument_config.id] = in_process_factory
                self.instruments[instrument_config.id] = in_process_factory()
            self._operation_gates[instrument_config.id] = _PriorityOperationGate()
            self._poll_issues[instrument_config.id] = set()
            self._connection_states[instrument_config.id] = (
                InstrumentConnectionState.STARTING
            )
            self._generation[instrument_config.id] = 0
            if instrument_config.stability is not None:
                self._stability[instrument_config.id] = StabilityEvaluator(instrument_config.stability)

    def connection_state(self, instrument_id: str) -> InstrumentConnectionState:
        """返回指定仪表当前连接生命周期状态。"""

        return self._connection_states[instrument_id]

    @property
    def control_ready(self) -> bool:
        """所有参与控制的仪表均已连接且读数新鲜时为真。"""

        return self.control_block_reason() is None

    def control_block_reason(self) -> str | None:
        """返回首个阻止手动控制或启动 SEQ 的原因。"""

        now = time.monotonic()
        for config in self.config.instruments:
            if not config.control_enabled:
                continue
            state = self._connection_states[config.id]
            if state is not InstrumentConnectionState.CONNECTED:
                return f"{config.display_name} is {state.value}"
            snapshot = self.latest.get(config.id)
            if snapshot is None or not snapshot.connected:
                return f"{config.display_name} has no connected reading"
            if now - snapshot.timestamp > config.stale_after_seconds:
                return f"{config.display_name} reading is stale"
        return None

    def ensure_run_ready(self) -> None:
        """在 SEQ 启动前执行运行时就绪检查，不能只依赖 GUI 灰化。"""

        reason = self.control_block_reason()
        if reason is not None:
            raise InstrumentError(
                f"Cannot run SEQ: {reason}",
                "PRIMARY_INSTRUMENT_NOT_READY",
                reason,
            )

    def _mark_snapshot_unavailable(
        self,
        instrument_id: str,
        state: InstrumentConnectionState,
        message: str,
    ) -> None:
        """保留最后已知目标，但明确把读数标成不可用、过期或故障。"""

        config = self.instrument_configs[instrument_id]
        snapshot = deepcopy(self.latest.get(instrument_id))
        if snapshot is None:
            snapshot = InstrumentSnapshot(
                instrument_id=instrument_id,
                display_name=config.display_name,
                kind=config.kind,
                timestamp=time.monotonic(),
                connected=False,
                unit=config.unit,
                current=None,
                target=self._expected_targets.get(instrument_id),
                rate_per_minute=self._expected_rates.get(instrument_id),
            )
        snapshot.connected = False
        snapshot.activity = (
            InstrumentActivity.FAULT
            if state is InstrumentConnectionState.FAULTED
            else InstrumentActivity.DISCONNECTED
        )
        snapshot.stability = StabilityState.STALE
        snapshot.message = message
        snapshot.connection_state = state
        self.latest[instrument_id] = snapshot

    @staticmethod
    def _recoverable_read_error(exc: InstrumentError) -> bool:
        """判断读失败能否尝试重连；非法数据本身不能靠重连掩盖。"""

        return not isinstance(exc, SafetyViolation) and exc.code not in {
            "INVALID_INSTRUMENT_SNAPSHOT",
            "NONFINITE_INSTRUMENT_READING",
            "INSTRUMENT_KIND_MISMATCH",
            "UNKNOWN_INSTRUMENT",
        }

    @staticmethod
    def _uncertain_write_error(exc: InstrumentError) -> bool:
        """识别“指令可能已送达但回复丢失”的写入失败。"""

        return exc.code in {
            "INSTRUMENT_OPERATION_TIMEOUT",
            "INSTRUMENT_WORKER_DISCONNECTED",
            "INSTRUMENT_WORKER_EXITED",
            "INSTRUMENT_WORKER_NOT_RUNNING",
            "INSTRUMENT_IPC_INVALID_MESSAGE",
            "INSTRUMENT_WRITE_RESULT_UNKNOWN",
        }

    def _begin_recovery(self, instrument_id: str, exc: InstrumentError) -> None:
        """把仪表转入重连状态，并确保同一仪表最多只有一个恢复任务。"""

        if self._shutting_down:
            return
        existing = self._recovery_tasks.get(instrument_id)
        if existing is not None and not existing.done():
            return
        self._generation[instrument_id] += 1
        generation = self._generation[instrument_id]
        self._connection_states[instrument_id] = InstrumentConnectionState.RECONNECTING
        self._unavailable_after_timeout.pop(instrument_id, None)
        message = (
            f"{self.instrument_configs[instrument_id].display_name} lost communication; "
            f"retrying for up to "
            f"{self.config.system_instruments.reconnect_timeout_seconds:g} seconds"
        )
        self._mark_snapshot_unavailable(
            instrument_id,
            InstrumentConnectionState.RECONNECTING,
            message,
        )
        self.events.report(
            Severity.WARNING,
            instrument_id,
            "INSTRUMENT_RECONNECTING",
            message,
            instrument_id,
        )
        task = asyncio.create_task(
            self._recover_instrument(instrument_id, generation, exc)
        )
        self._recovery_tasks[instrument_id] = task

    def _validate_recovered_state(
        self,
        instrument_id: str,
        snapshot: InstrumentSnapshot,
    ) -> None:
        """核对重连后的实际目标和速率，防止带着未知仪表状态继续运行。"""

        expected_target = self._expected_targets.get(instrument_id)
        if expected_target is None:
            return
        actual_target = snapshot.target
        tolerance = max(1e-9, abs(expected_target) * 1e-9)
        if (
            actual_target is None
            or not math.isclose(
                actual_target,
                expected_target,
                rel_tol=1e-9,
                abs_tol=tolerance,
            )
        ):
            raise InstrumentError(
                f"{self.instrument_configs[instrument_id].display_name} reconnected with "
                f"target {actual_target!r}, expected {expected_target:g}",
                "INSTRUMENT_STATE_MISMATCH_AFTER_RECONNECT",
                instrument_id,
            )
        expected_rate = self._expected_rates.get(instrument_id)
        if expected_rate is not None and snapshot.rate_per_minute is not None:
            rate_tolerance = max(1e-9, abs(expected_rate) * 1e-9)
            if not math.isclose(
                snapshot.rate_per_minute,
                expected_rate,
                rel_tol=1e-9,
                abs_tol=rate_tolerance,
            ):
                raise InstrumentError(
                    f"{self.instrument_configs[instrument_id].display_name} reconnected "
                    f"with rate {snapshot.rate_per_minute:g}, expected "
                    f"{expected_rate:g}",
                    "INSTRUMENT_STATE_MISMATCH_AFTER_RECONNECT",
                    instrument_id,
                )

    async def _recover_instrument(
        self,
        instrument_id: str,
        generation: int,
        initial_error: InstrumentError,
    ) -> None:
        """在配置的总时限内反复重建客户端、连接并核对恢复后的状态。

        仅“重新连上”还不够：对于可能已经送达仪表的写操作，必须读取并验证实际目标和速率；
        无法证明状态一致时进入故障路径，绝不自动重发不确定写入。
        """

        timeout = self.config.system_instruments.reconnect_timeout_seconds
        interval = self.config.system_instruments.reconnect_interval_seconds
        deadline = time.monotonic() + timeout
        last_error: Exception = initial_error
        failure_code = "INSTRUMENT_RECONNECT_FAILED"
        try:
            while not self._shutting_down and time.monotonic() < deadline:
                candidate = self._client_factories[instrument_id]()
                self._recovery_clients[instrument_id] = candidate
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    gate = self._operation_gates[instrument_id]
                    await gate.acquire(_INSTRUMENT_CONTROL_PRIORITY)
                    try:
                        if (
                            self._generation[instrument_id] != generation
                            or self._shutting_down
                        ):
                            return
                        previous = self.instruments[instrument_id]
                        await previous.force_stop(  # type: ignore[attr-defined]
                            min(0.25, remaining)
                        )
                        await asyncio.wait_for(
                            candidate.connect(),  # type: ignore[attr-defined]
                            timeout=remaining,
                        )
                        remaining = max(0.0, deadline - time.monotonic())
                        snapshot = await asyncio.wait_for(
                            candidate.poll(),  # type: ignore[attr-defined]
                            timeout=remaining,
                        )
                        self._validate_snapshot(instrument_id, snapshot)
                        self._validate_recovered_state(instrument_id, snapshot)
                        self.instruments[instrument_id] = candidate
                        snapshot.connected = True
                        snapshot.connection_state = (
                            InstrumentConnectionState.CONNECTED
                        )
                        self.latest[instrument_id] = snapshot
                        self._connection_states[instrument_id] = (
                            InstrumentConnectionState.CONNECTED
                        )
                        self._unavailable_after_timeout.pop(instrument_id, None)
                        evaluator = self._stability.get(instrument_id)
                        if evaluator is not None and snapshot.target is not None:
                            evaluator.reset(snapshot.target, snapshot.timestamp)
                    finally:
                        gate.release()
                    self._recovery_clients.pop(instrument_id, None)
                    self.events.resolve(
                        instrument_id,
                        "INSTRUMENT_RECONNECTING",
                        instrument_id,
                    )
                    self.events.resolve(
                        instrument_id,
                        "INSTRUMENT_RECONNECT_FAILED",
                        instrument_id,
                    )
                    self.events.report(
                        Severity.INFO,
                        instrument_id,
                        "INSTRUMENT_RECONNECTED",
                        f"{snapshot.display_name} reconnected and state was verified",
                        instrument_id,
                    )
                    return
                except asyncio.CancelledError:
                    await candidate.force_stop(0.25)  # type: ignore[attr-defined]
                    raise
                except Exception as exc:
                    last_error = exc
                    await candidate.force_stop(0.25)  # type: ignore[attr-defined]
                    self._recovery_clients.pop(instrument_id, None)
                    if (
                        isinstance(exc, InstrumentError)
                        and exc.code
                        == "INSTRUMENT_STATE_MISMATCH_AFTER_RECONNECT"
                    ):
                        failure_code = exc.code
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(interval, remaining))
            if self._generation[instrument_id] != generation or self._shutting_down:
                return
            self._connection_states[instrument_id] = InstrumentConnectionState.FAULTED
            if failure_code == "INSTRUMENT_STATE_MISMATCH_AFTER_RECONNECT":
                message = str(last_error)
            else:
                message = (
                    f"{self.instrument_configs[instrument_id].display_name} did not reconnect "
                    f"within {timeout:g} seconds: {last_error}"
                )
            self._mark_snapshot_unavailable(
                instrument_id,
                InstrumentConnectionState.FAULTED,
                message,
            )
            config = self.instrument_configs[instrument_id]
            severity = (
                Severity.ERROR
                if config.control_enabled
                else Severity.WARNING
            )
            self.events.report(
                severity,
                instrument_id,
                failure_code,
                message,
                instrument_id,
            )
        finally:
            current = self._recovery_tasks.get(instrument_id)
            if current is asyncio.current_task():
                self._recovery_tasks.pop(instrument_id, None)
            self._recovery_clients.pop(instrument_id, None)

    async def _fault_uncertain_write(
        self,
        instrument_id: str,
        operation: str,
        exc: InstrumentError,
    ) -> None:
        """处理结果不确定的写操作，并把仪表置于不可继续控制的故障状态。"""

        self.events.report(
            Severity.ERROR,
            instrument_id,
            exc.code,
            str(exc),
            exc.context,
        )
        self._generation[instrument_id] += 1
        task = self._recovery_tasks.pop(instrument_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._connection_states[instrument_id] = InstrumentConnectionState.FAULTED
        message = (
            f"{self.instrument_configs[instrument_id].display_name} {operation} result is "
            f"unknown after communication failure; the command will not be replayed"
        )
        self._mark_snapshot_unavailable(
            instrument_id,
            InstrumentConnectionState.FAULTED,
            message,
        )
        try:
            await self.instruments[instrument_id].force_stop(0.25)  # type: ignore[attr-defined]
        except Exception:
            pass
        self.events.report(
            Severity.ERROR,
            instrument_id,
            "INSTRUMENT_WRITE_RESULT_UNKNOWN",
            f"{message}: {exc}",
            operation,
        )

    async def _operate(
        self,
        instrument_id: str,
        operation: str,
        callback: Callable[[], Awaitable[T]],
        *,
        shutdown: bool = False,
        origin: str | None = None,
        priority: int = _INSTRUMENT_CONTROL_PRIORITY,
    ) -> T:
        """在仪表专属优先队列内执行一次有时限操作，并按来源和连接状态决定是否放行。

        写超时可能意味着指令已经到达仪表，因此不会盲目重试。对于不自带硬超时的进程内
        驱动，一次超时后禁止后续 I/O，避免仍在执行的底层调用与新指令并发接触同一仪表。
        """

        config = self.instrument_configs[instrument_id]
        timeout = (
            config.shutdown_timeout_seconds
            if shutdown
            else config.operation_timeout_seconds
        )

        async def serialized() -> T:
            gate = self._operation_gates[instrument_id]
            await gate.acquire(priority)
            try:
                if origin == "manual" and self._control_owner == "sequence":
                    raise InstrumentWarning(
                        f"{config.display_name} manual control is blocked while a SEQ owns control",
                        "MANUAL_CONTROL_BLOCKED",
                        instrument_id,
                    )
                if (
                    operation
                    not in {"connect", "disconnect", "poll", "poll_measurement"}
                    and self._connection_states[instrument_id]
                    is not InstrumentConnectionState.CONNECTED
                ):
                    state = self._connection_states[instrument_id]
                    raise InstrumentError(
                        f"{config.display_name} is {state.value}; "
                        f"{operation} was not sent",
                        "INSTRUMENT_NOT_READY",
                        instrument_id,
                    )
                previous = self._unavailable_after_timeout.get(instrument_id)
                if previous is not None and not shutdown:
                    raise InstrumentError(
                        f"{config.display_name} is unavailable after timed-out "
                        f"{previous}; restart OpenLab Control before further I/O",
                        "INSTRUMENT_UNAVAILABLE_AFTER_TIMEOUT",
                        operation,
                    )
                instrument = self.instruments[instrument_id]
                try:
                    if bool(getattr(instrument, "enforces_timeouts", False)):
                        return await callback()
                    return await asyncio.wait_for(callback(), timeout=timeout)
                except TimeoutError as exc:
                    if not bool(getattr(instrument, "enforces_timeouts", False)):
                        self._unavailable_after_timeout[instrument_id] = operation
                    raise InstrumentError(
                        f"{config.display_name} {operation} timed out after "
                        f"{timeout:g} seconds",
                        "INSTRUMENT_OPERATION_TIMEOUT",
                        operation,
                    ) from exc
                except InstrumentError as exc:
                    if (
                        exc.code == "INSTRUMENT_OPERATION_TIMEOUT"
                        and not bool(
                            getattr(instrument, "enforces_timeouts", False)
                        )
                    ):
                        self._unavailable_after_timeout[instrument_id] = operation
                    raise
            finally:
                gate.release()

        return await serialized()

    async def connect_all(self) -> None:
        """并发连接全部配置仪表；失败会反映到连接状态和事件系统。"""

        async def connect(instrument_id: str, instrument: object) -> None:
            try:
                await self._operate(instrument_id, "connect", instrument.connect)
                self._connection_states[instrument_id] = (
                    InstrumentConnectionState.CONNECTED
                )
                self.events.resolve(instrument_id, "CONNECT_FAILED")
                self.events.report(Severity.INFO, instrument_id, "CONNECTED", "Instrument connected")
            except InstrumentError as exc:
                if self.isolate_processes:
                    self._begin_recovery(instrument_id, exc)
                    return
                self._connection_states[instrument_id] = (
                    InstrumentConnectionState.FAULTED
                )
                self.events.report(
                    Severity.ERROR,
                    instrument_id,
                    exc.code,
                    str(exc),
                    exc.context,
                )
            except Exception as exc:
                self._connection_states[instrument_id] = (
                    InstrumentConnectionState.FAULTED
                )
                self.events.report(
                    Severity.ERROR,
                    instrument_id,
                    "CONNECT_FAILED",
                    str(exc),
                )

        await asyncio.gather(
            *(connect(instrument_id, instrument) for instrument_id, instrument in self.instruments.items())
        )

    async def disconnect_all(self) -> None:
        """停止恢复任务并有界断开全部仪表，用于应用关闭。"""

        self._shutting_down = True
        self._generation = {
            instrument_id: generation + 1
            for instrument_id, generation in self._generation.items()
        }
        recovery_tasks = tuple(self._recovery_tasks.values())
        for task in recovery_tasks:
            task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        recovery_clients = tuple(self._recovery_clients.values())
        self._recovery_tasks.clear()
        self._recovery_clients.clear()
        if recovery_clients:
            await asyncio.gather(
                *(
                    client.force_stop(0.25)  # type: ignore[attr-defined]
                    for client in recovery_clients
                ),
                return_exceptions=True,
            )

        async def disconnect(instrument_id: str, instrument: object) -> None:
            try:
                await self._operate(
                    instrument_id,
                    "disconnect",
                    instrument.disconnect,
                    shutdown=True,
                )
            except Exception as exc:
                self.events.report(
                    Severity.WARNING,
                    instrument_id,
                    getattr(exc, "code", "DISCONNECT_FAILED"),
                    str(exc),
                    getattr(exc, "context", ""),
                )
            finally:
                try:
                    await instrument.close()  # type: ignore[attr-defined]
                except Exception as exc:
                    self.events.report(
                        Severity.WARNING,
                        instrument_id,
                        "INSTRUMENT_WORKER_CLOSE_FAILED",
                        str(exc),
                    )
                self._connection_states[instrument_id] = (
                    InstrumentConnectionState.DISCONNECTED
                )
                self._mark_snapshot_unavailable(
                    instrument_id,
                    InstrumentConnectionState.DISCONNECTED,
                    "Instrument disconnected",
                )

        await asyncio.gather(
            *(disconnect(instrument_id, instrument) for instrument_id, instrument in self.instruments.items())
        )

    async def poll_all(self) -> dict[str, InstrumentSnapshot]:
        """并发轮询已连接仪表并返回快照副本；单台失败不会阻塞其他仪表。"""

        return await self._poll_all(measurement=False)

    async def poll_measurement_all(self) -> dict[str, InstrumentSnapshot]:
        """取得写测量行所需的即时主读数；后端可省略慢速附加查询。"""

        return await self._poll_all(measurement=True)

    async def _poll_all(
        self,
        *,
        measurement: bool,
    ) -> dict[str, InstrumentSnapshot]:
        """共用完整轮询与测量轮询的错误、恢复和过期状态处理。"""

        instrument_ids = tuple(
            instrument_id
            for instrument_id in self.instruments
            if self._connection_states[instrument_id]
            is InstrumentConnectionState.CONNECTED
        )
        results = await asyncio.gather(
            *(
                self._poll_one(instrument_id, measurement=measurement)
                for instrument_id in instrument_ids
            ),
            return_exceptions=True,
        )
        now = time.monotonic()
        for instrument_id, result in zip(instrument_ids, results, strict=True):
            if isinstance(result, Exception):
                if (
                    self.isolate_processes
                    and isinstance(result, InstrumentError)
                    and not isinstance(result, InstrumentWarning)
                    and self._recoverable_read_error(result)
                ):
                    self._begin_recovery(instrument_id, result)
                    continue
                severity = Severity.WARNING if isinstance(result, InstrumentWarning) else Severity.ERROR
                code = getattr(result, "code", "POLL_FAILED")
                context = getattr(result, "context", "")
                self._poll_issues[instrument_id].add((code, context))
                self.events.report(severity, instrument_id, code, str(result), context)
                if (
                    not isinstance(result, InstrumentWarning)
                    and isinstance(result, InstrumentError)
                    and not self._recoverable_read_error(result)
                ):
                    self._connection_states[instrument_id] = (
                        InstrumentConnectionState.FAULTED
                    )
                    self._mark_snapshot_unavailable(
                        instrument_id,
                        InstrumentConnectionState.FAULTED,
                        str(result),
                    )
            else:
                self.events.resolve(instrument_id, "POLL_FAILED")
                for code, context in self._poll_issues[instrument_id]:
                    self.events.resolve(instrument_id, code, context)
                self._poll_issues[instrument_id].clear()
            self._update_stale_state(
                instrument_id,
                now,
                poll_succeeded=not isinstance(result, Exception),
            )
        return deepcopy(self.latest)

    async def _poll_one(
        self,
        instrument_id: str,
        *,
        measurement: bool = False,
    ) -> InstrumentSnapshot:
        """读取、校验并在仪表锁内发布一台仪表的最新状态。"""

        async def poll() -> InstrumentSnapshot:
            client = self.instruments[instrument_id]
            if measurement:
                snapshot = await client.poll_measurement()  # type: ignore[attr-defined]
            else:
                snapshot = await client.poll()  # type: ignore[attr-defined]
            self._validate_snapshot(instrument_id, snapshot)
            snapshot.connected = True
            snapshot.connection_state = InstrumentConnectionState.CONNECTED
            self._connection_states[instrument_id] = (
                InstrumentConnectionState.CONNECTED
            )
            if instrument_id not in self._expected_targets:
                self._expected_targets[instrument_id] = snapshot.target
                self._expected_rates[instrument_id] = snapshot.rate_per_minute
            evaluator = self._stability.get(instrument_id)
            if evaluator is not None and snapshot.current is not None and snapshot.target is not None:
                result = evaluator.update(
                    snapshot.current,
                    snapshot.target,
                    snapshot.timestamp,
                    instrument_stable=snapshot.instrument_stable,
                )
                snapshot.stability = result.state
                timeout_code = "STABILITY_TIMEOUT"
                if result.state is StabilityState.TIMED_OUT:
                    self.events.report(
                        self.config.alarms.stability_timeout,
                        instrument_id,
                        timeout_code,
                        f"{snapshot.display_name} did not stabilize within {result.elapsed_seconds:.1f} seconds",
                    )
                else:
                    self.events.resolve(instrument_id, timeout_code)
            # 必须在仍持有仪表锁时发布：否则较早开始的 poll 可能在 set_target 完成后才返回，
            # 用旧目标覆盖刚写入的新目标。
            self.latest[instrument_id] = snapshot
            return snapshot

        return await self._operate(
            instrument_id,
            "poll_measurement" if measurement else "poll",
            poll,
            priority=(
                _INSTRUMENT_MEASUREMENT_PRIORITY
                if measurement
                else _INSTRUMENT_BACKGROUND_PRIORITY
            ),
        )

    def _validate_snapshot(
        self,
        instrument_id: str,
        snapshot: InstrumentSnapshot,
    ) -> None:
        """拒绝仪表 ID、类型、连接标志或数值不合法的后端快照。"""

        config = self.instrument_configs[instrument_id]
        if snapshot.instrument_id != instrument_id or snapshot.kind is not config.kind:
            raise InstrumentError(
                f"{config.display_name} returned a snapshot for the wrong instrument or kind",
                "INVALID_INSTRUMENT_SNAPSHOT",
                instrument_id,
            )
        if not snapshot.connected:
            raise InstrumentError(
                f"{config.display_name} reported that it is disconnected",
                "INSTRUMENT_REPORTED_DISCONNECTED",
                instrument_id,
            )
        if snapshot.instrument_stable is not None and not isinstance(
            snapshot.instrument_stable,
            bool,
        ):
            raise InstrumentError(
                f"{config.display_name} returned a non-boolean instrument stability flag",
                "INVALID_INSTRUMENT_SNAPSHOT",
                instrument_id,
            )
        numeric_values = {
            "timestamp": snapshot.timestamp,
            "current": snapshot.current,
            "target": snapshot.target,
            "rate_per_minute": snapshot.rate_per_minute,
        }
        invalid = [
            name
            for name, value in numeric_values.items()
            if value is not None and not math.isfinite(value)
        ]
        if invalid:
            raise InstrumentError(
                f"{config.display_name} returned non-finite {', '.join(invalid)}",
                "NONFINITE_INSTRUMENT_READING",
                instrument_id,
            )
        metric_schema: list[tuple[str, str, str, int | None]] = []
        if not isinstance(snapshot.metrics, dict):
            raise InstrumentError(
                f"{config.display_name} returned metrics that are not a dictionary",
                "INVALID_INSTRUMENT_SNAPSHOT",
                instrument_id,
            )
        for metric_key, metric in snapshot.metrics.items():
            if not isinstance(metric_key, str) or not _METRIC_KEY.fullmatch(metric_key):
                raise InstrumentError(
                    f"{config.display_name} returned an invalid metric key "
                    f"{metric_key!r}",
                    "INVALID_INSTRUMENT_SNAPSHOT",
                    instrument_id,
                )
            if not isinstance(metric, InstrumentMetric):
                raise InstrumentError(
                    f"{config.display_name} returned an invalid metric descriptor for "
                    f"{metric_key!r}",
                    "INVALID_INSTRUMENT_SNAPSHOT",
                    instrument_id,
                )
            if (
                not metric.display_name.strip()
                or len(metric.display_name) > 64
                or any(character in metric.display_name for character in "\r\n")
                or len(metric.unit) > 24
                or any(character in metric.unit for character in "\r\n")
            ):
                raise InstrumentError(
                    f"{config.display_name} returned invalid metadata for metric "
                    f"{metric_key!r}",
                    "INVALID_INSTRUMENT_SNAPSHOT",
                    instrument_id,
                )
            if metric.decimals is not None and (
                isinstance(metric.decimals, bool)
                or not isinstance(metric.decimals, int)
                or not 0 <= metric.decimals <= 12
            ):
                raise InstrumentError(
                    f"{config.display_name} returned invalid decimals for metric "
                    f"{metric_key!r}",
                    "INVALID_INSTRUMENT_SNAPSHOT",
                    instrument_id,
                )
            value = metric.value
            if not isinstance(value, (int, float, str, bool, type(None))):
                raise InstrumentError(
                    f"{config.display_name} returned a non-scalar metric "
                    f"{metric_key!r}",
                    "INVALID_INSTRUMENT_SNAPSHOT",
                    instrument_id,
                )
            invalid_numeric_metric = False
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    invalid_numeric_metric = not math.isfinite(value)
                except OverflowError:
                    invalid_numeric_metric = True
            if invalid_numeric_metric:
                raise InstrumentError(
                    f"{config.display_name} returned non-finite metric "
                    f"{metric_key!r}",
                    "NONFINITE_INSTRUMENT_READING",
                    instrument_id,
                )
            if isinstance(value, str) and (
                len(value) > 256 or any(character in value for character in "\r\n")
            ):
                raise InstrumentError(
                    f"{config.display_name} returned invalid text for metric "
                    f"{metric_key!r}",
                    "INVALID_INSTRUMENT_SNAPSHOT",
                    instrument_id,
                )
            metric_schema.append(
                (metric_key, metric.display_name, metric.unit, metric.decimals)
            )
        frozen_schema = tuple(metric_schema)
        previous_schema = self._metric_schemas.setdefault(instrument_id, frozen_schema)
        if previous_schema != frozen_schema:
            raise InstrumentError(
                f"{config.display_name} changed its metric schema while running",
                "INVALID_INSTRUMENT_SNAPSHOT",
                instrument_id,
            )

    def _update_stale_state(
        self,
        instrument_id: str,
        now: float,
        *,
        poll_succeeded: bool,
    ) -> None:
        """根据单调时钟锁存或解除读数过期事件。"""

        snapshot = self.latest.get(instrument_id)
        if snapshot is None:
            return
        config = self.instrument_configs[instrument_id]
        age = max(0.0, now - snapshot.timestamp)
        stale = age > config.stale_after_seconds
        if stale:
            snapshot.stability = StabilityState.STALE
            snapshot.message = (
                f"Reading is stale ({age:.1f} s old; "
                f"limit {config.stale_after_seconds:g} s)"
            )
            first_occurrence = instrument_id not in self._stale_instruments
            if first_occurrence:
                self._stale_instruments.add(instrument_id)
            if (
                first_occurrence
                or self.config.alarms.stale_reading is not Severity.INFO
            ):
                self.events.report(
                    self.config.alarms.stale_reading,
                    instrument_id,
                    "STALE_READING",
                    snapshot.message,
                    instrument_id,
                )
        elif poll_succeeded and instrument_id in self._stale_instruments:
            self._stale_instruments.remove(instrument_id)
            self.events.resolve(instrument_id, "STALE_READING", instrument_id)

    def first_instrument_id(self, kind: InstrumentKind) -> str:
        """取得指定类型的标准主控仪表 ID。"""

        for config in self.config.instruments:
            if (
                config.kind is kind
                and config.role is InstrumentRole.PRIMARY
                and config.control_enabled
            ):
                return config.id
        raise InstrumentError(
            f"No controllable primary {kind.value} instrument is configured",
            "INSTRUMENT_NOT_CONFIGURED",
            kind.value,
        )

    def resolve_instrument_id(
        self,
        kind: InstrumentKind,
        requested: object | None = None,
    ) -> str:
        """解析 SEQ 可选仪表后缀，并限制其类型和控制权限。"""

        candidate = str(requested or "").strip()
        if not candidate or (
            candidate == kind.value and candidate not in self.instrument_configs
        ):
            candidate = self.first_instrument_id(kind)
        config = self.instrument_configs.get(candidate)
        if config is None:
            raise InstrumentError(
                f"Unknown {kind.value} instrument: {candidate}",
                "UNKNOWN_INSTRUMENT",
                candidate,
            )
        if config.kind is not kind:
            raise InstrumentError(
                f"Instrument {candidate} is {config.kind.value}, not {kind.value}",
                "INSTRUMENT_KIND_MISMATCH",
                candidate,
            )
        if not config.control_enabled:
            raise InstrumentError(
                f"Instrument {candidate} is read-only and cannot be used by control commands",
                "INSTRUMENT_READ_ONLY",
                candidate,
            )
        return candidate

    def validate_target(self, instrument_id: str, value: float, rate_per_minute: float) -> None:
        """在任何写入前强制检查有限值、上下限、最大速率和仪表角色。"""

        config = self.instrument_configs[instrument_id]
        if (
            config.kind not in (InstrumentKind.TEMPERATURE, InstrumentKind.FIELD)
            or not config.control_enabled
        ):
            raise InstrumentError(
                f"{config.display_name} is display-only and cannot accept a target",
                "TARGET_NOT_CONTROLLABLE",
                instrument_id,
            )
        if not math.isfinite(value):
            raise SafetyViolation(
                f"{config.display_name} target must be finite",
                "TARGET_NOT_FINITE",
                instrument_id,
            )
        if not math.isfinite(rate_per_minute):
            raise SafetyViolation(
                f"{config.display_name} rate must be finite",
                "RATE_NOT_FINITE",
                instrument_id,
            )
        if not config.min_value <= value <= config.max_value:
            raise SafetyViolation(
                f"{config.display_name} target {value:g} {config.unit} is outside the allowed range "
                f"[{config.min_value:g}, {config.max_value:g}] {config.unit}",
                "TARGET_OUT_OF_RANGE",
                instrument_id,
            )
        if rate_per_minute <= 0 or rate_per_minute > config.max_rate_per_minute:
            raise SafetyViolation(
                f"{config.display_name} rate {rate_per_minute:g} {config.unit}/min is outside the allowed range "
                f"(0, {config.max_rate_per_minute:g}]",
                "RATE_OUT_OF_RANGE",
                instrument_id,
            )

    async def set_target(
        self,
        instrument_id: str,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        origin: str = "sequence",
    ) -> bool:
        """设置目标；手动来源在 SEQ 持有控制租约时会被运行时拒绝。"""

        try:
            self.validate_target(instrument_id, value, rate_per_minute)
            await self._operate(
                instrument_id,
                "set_target",
                lambda: self.instruments[instrument_id].set_target(
                    value,
                    rate_per_minute,
                    mode,
                ),
                origin=origin,
            )
        except InstrumentWarning as exc:
            self.events.report(Severity.WARNING, instrument_id, exc.code, str(exc), exc.context)
            return False
        except InstrumentError as exc:
            if self._uncertain_write_error(exc):
                await self._fault_uncertain_write(
                    instrument_id,
                    "set_target",
                    exc,
                )
                raise InstrumentError(
                    f"{self.instrument_configs[instrument_id].display_name} set_target "
                    "could not be confirmed and was not replayed",
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    instrument_id,
                ) from exc
            self.events.report(Severity.ERROR, instrument_id, exc.code, str(exc), exc.context)
            raise
        self._expected_targets[instrument_id] = value
        self._expected_rates[instrument_id] = rate_per_minute
        snapshot = self.latest.get(instrument_id)
        if snapshot is not None:
            snapshot.target = value
            snapshot.rate_per_minute = rate_per_minute
            snapshot.stability = StabilityState.MOVING
        evaluator = self._stability.get(instrument_id)
        if evaluator is not None:
            evaluator.reset(value, time.monotonic())
        self.events.resolve(instrument_id, "TARGET_OUT_OF_RANGE", instrument_id)
        self.events.resolve(instrument_id, "RATE_OUT_OF_RANGE", instrument_id)
        return True

    async def set_target_by_kind(
        self,
        kind: InstrumentKind,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        instrument_id: str | None = None,
        *,
        origin: str = "sequence",
    ) -> bool:
        """供标准 SEQ 命令按类型选择主控仪表并设置目标。"""

        selected = self.resolve_instrument_id(kind, instrument_id)
        return await self.set_target(
            selected,
            value,
            rate_per_minute,
            mode,
            origin=origin,
        )

    async def hold_all(self) -> bool:
        """尽力 Hold 所有可控温度和磁场仪表，并返回是否全部成功。"""

        async def hold(instrument_id: str) -> bool:
            config = self.instrument_configs[instrument_id]
            if not config.control_enabled:
                return True
            if config.kind is InstrumentKind.TEMPERATURE:
                strategy = self.config.abort_temperature
            elif config.kind is InstrumentKind.FIELD:
                strategy = self.config.abort_field
            else:
                return True
            if strategy != "hold_current":
                self.events.report(
                    Severity.WARNING,
                    "runtime",
                    "UNKNOWN_ABORT_STRATEGY",
                    f"Unknown abort strategy {strategy}; using hold_current",
                    instrument_id,
                )
            if (
                self._connection_states[instrument_id]
                is not InstrumentConnectionState.CONNECTED
            ):
                state = self._connection_states[instrument_id]
                self.events.report(
                    Severity.ERROR,
                    instrument_id,
                    "HOLD_UNCONFIRMED",
                    f"{config.display_name} is {state.value}; Hold Current "
                    "could not be sent or confirmed",
                    instrument_id,
                )
                return False
            try:
                await self._operate(
                    instrument_id,
                    "hold",
                    lambda: self.instruments[instrument_id].hold(),  # type: ignore[attr-defined]
                )
                snapshot = self.latest.get(instrument_id)
                if snapshot is not None:
                    self._expected_targets[instrument_id] = snapshot.current
                    self._expected_rates[instrument_id] = (
                        snapshot.rate_per_minute
                    )
                    snapshot.target = snapshot.current
                    snapshot.activity = InstrumentActivity.HOLDING
                return True
            except InstrumentError as exc:
                if self._uncertain_write_error(exc):
                    await self._fault_uncertain_write(
                        instrument_id,
                        "hold",
                        exc,
                    )
                    return False
                self.events.report(
                    Severity.ERROR,
                    instrument_id,
                    exc.code,
                    str(exc),
                    exc.context,
                )
                return False
            except Exception as exc:
                self.events.report(
                    Severity.ERROR,
                    instrument_id,
                    getattr(exc, "code", "HOLD_FAILED"),
                    str(exc),
                    getattr(exc, "context", ""),
                )
                return False

        results = await asyncio.gather(
            *(hold(instrument_id) for instrument_id in self.instruments)
        )
        return all(results)

    async def hold_instrument(
        self,
        instrument_id: str,
        *,
        origin: str = "manual",
    ) -> None:
        """Hold 单台仪表；同样执行控制权与连接状态检查。"""

        if instrument_id not in self.instruments:
            raise InstrumentError(f"Unknown instrument: {instrument_id}", "UNKNOWN_INSTRUMENT", instrument_id)
        if not self.instrument_configs[instrument_id].control_enabled:
            raise InstrumentError(
                f"{self.instrument_configs[instrument_id].display_name} is read-only",
                "INSTRUMENT_READ_ONLY",
                instrument_id,
            )
        try:
            await self._operate(
                instrument_id,
                "hold",
                lambda: self.instruments[instrument_id].hold(),  # type: ignore[attr-defined]
                origin=origin,
            )
        except (InstrumentError, InstrumentWarning) as exc:
            if (
                isinstance(exc, InstrumentError)
                and not isinstance(exc, InstrumentWarning)
                and self._uncertain_write_error(exc)
            ):
                await self._fault_uncertain_write(
                    instrument_id,
                    "hold",
                    exc,
                )
                raise InstrumentError(
                    f"{self.instrument_configs[instrument_id].display_name} hold "
                    "could not be confirmed and was not replayed",
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    instrument_id,
                ) from exc
            severity = Severity.WARNING if isinstance(exc, InstrumentWarning) else Severity.ERROR
            self.events.report(severity, instrument_id, exc.code, str(exc), exc.context)
            raise
        snapshot = self.latest.get(instrument_id)
        if snapshot is not None:
            self._expected_targets[instrument_id] = snapshot.current
            self._expected_rates[instrument_id] = snapshot.rate_per_minute
            snapshot.target = snapshot.current
            snapshot.activity = InstrumentActivity.HOLDING

    def acquire_sequence_control(self) -> None:
        """由运行时在 SEQ 开始前取得唯一控制租约。"""

        if self._control_owner is not None:
            raise InstrumentError(
                f"Instrument control is already owned by {self._control_owner}",
                "INSTRUMENT_CONTROL_BUSY",
                self._control_owner,
            )
        self._control_owner = "sequence"

    def release_sequence_control(self) -> None:
        """在 SEQ 的所有退出路径释放控制租约。"""

        if self._control_owner == "sequence":
            self._control_owner = None

    def snapshots(self) -> dict[str, InstrumentSnapshot]:
        """返回最新状态的深拷贝，防止调用方修改安全判定依据。"""

        return deepcopy(self.latest)
