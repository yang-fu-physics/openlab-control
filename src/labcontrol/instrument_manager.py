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

from .config import AppConfig, InstrumentConfig, InstrumentPanelConfig
from .instruments.base import (
    EventResponseSpec,
    InstrumentError,
    SystemInstrument,
    InstrumentWarning,
    SafetyViolation,
)
from .instruments.manifest import SystemInstrumentDescriptor
from .instruments.worker import (
    InstrumentWorkerClient,
    InstrumentWorkerSpec,
    InProcessInstrumentClient,
    IsolatedInstrumentClient,
)
from .events import EventManager
from .package_support.loading import load_import_object, load_source_object
from .models import (
    EventNotice,
    InstrumentActivity,
    InstrumentConnectionState,
    InstrumentControlState,
    InstrumentKind,
    InstrumentMetric,
    InstrumentSnapshot,
    Severity,
    StabilityState,
)
from .stability import StabilityEvaluator
from .system_instrument_commands import (
    configured_system_instrument_commands,
)


T = TypeVar("T")
_METRIC_KEY = re.compile(r"^[a-z][a-z0-9_]*$")

# 同一台仪表始终只允许一个操作进入后端。控制与安全操作不能被读数越过；
# 测量专用读取可以越过尚未开始的后台状态轮询，但不会中断已经开始的仪表事务。
_INSTRUMENT_CONTROL_PRIORITY = 0
_INSTRUMENT_EVENT_RESPONSE_PRIORITY = -10
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
        self.sequence_commands = configured_system_instrument_commands(
            config,
            descriptors,
        )
        self._sequence_command_specs = {
            (command.instrument_id, command.command_id): command
            for command in self.sequence_commands
        }
        self.isolate_processes = isolate_processes
        self.instruments: dict[str, object] = {}
        self._client_factories: dict[str, Callable[[], object]] = {}
        self.instrument_configs: dict[str, InstrumentConfig] = {
            item.id: item for item in config.instrument_instances
        }
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
        self._expected_targets: dict[tuple[str, str], float | None] = {}
        self._expected_rates: dict[tuple[str, str], float | None] = {}
        self._metric_schemas: dict[
            str,
            tuple[tuple[str, str, str, int | None], ...],
        ] = {}
        self.event_responses: dict[
            tuple[str, str, str],
            tuple[EventResponseSpec, str],
        ] = {}
        self._latched_event_responses: dict[
            str,
            tuple[EventResponseSpec, str],
        ] = {}
        self._event_response_target_locks: dict[str, set[str]] = {}
        self._event_response_tasks: dict[str, asyncio.Task[None]] = {}
        self._shutting_down = False
        self.latest: dict[str, InstrumentSnapshot] = {}
        self._load_instruments()
        self.events.subscribe(self._on_event_response)

    def _register_event_responses(
        self,
        source: str,
        responses: tuple[EventResponseSpec, ...],
    ) -> None:
        """把一台后端的声明绑定到逻辑仪表 ID，并解析唯一目标磁场。"""

        for response in responses:
            key = (source, response.code, response.context)
            if key in self.event_responses:
                raise InstrumentError(
                    f"Duplicate event response for {source}/{response.code}/"
                    f"{response.context}",
                    "INVALID_EVENT_RESPONSES",
                    source,
                )
            try:
                target = self.resolve_instrument_id(
                    InstrumentKind.FIELD,
                    response.target_instrument or None,
                )
            except InstrumentError as exc:
                raise InstrumentError(
                    f"Invalid event response {source}/{response.code}: {exc}",
                    "INVALID_EVENT_RESPONSES",
                    source,
                ) from exc
            self.event_responses[key] = (response, target)

    def _on_event_response(self, notice: EventNotice) -> None:
        """首次事件状态变化时锁存并调度已注册响应。"""

        if notice.is_resolution or notice.event.severity is Severity.INFO:
            return
        event = notice.event
        registered = self.event_responses.get(
            (event.source, event.code, event.context)
        )
        if registered is None or event.key in self._latched_event_responses:
            return
        response, target = registered
        self._latched_event_responses[event.key] = registered
        self._event_response_target_locks.setdefault(target, set()).add(
            event.key
        )
        task = asyncio.create_task(
            self._execute_event_response(event.key, response, target)
        )
        self._event_response_tasks[event.key] = task

    async def _execute_event_response(
        self,
        event_key: str,
        response: EventResponseSpec,
        target: str,
    ) -> None:
        """执行一条锁存响应；zero 使用目标磁场的默认速率。"""

        try:
            if response.action == "zero":
                config = self.instrument_configs[target]
                panel = self.resolve_control_panel(
                    InstrumentKind.FIELD,
                    target,
                )
                applied = await self.set_target(
                    target,
                    0.0,
                    panel.default_rate_per_minute,
                    "Sweep",
                    control=panel.control_id,
                    origin="event_response",
                )
                if not applied:
                    raise InstrumentError(
                        "Target instrument did not apply the event response",
                        "EVENT_RESPONSE_NOT_APPLIED",
                        target,
                    )
                self.events.report(
                    Severity.INFO,
                    "runtime",
                    "EVENT_RESPONSE_EXECUTED",
                    f"Event response set {config.display_name} target to zero",
                    event_key,
                )
        except InstrumentError as exc:
            self.events.report(
                Severity.ERROR,
                "runtime",
                "EVENT_RESPONSE_FAILED",
                f"Event response failed: {exc}",
                event_key,
            )
        finally:
            self._event_response_tasks.pop(event_key, None)

    def reset_event_response(self, event_key: str) -> None:
        """在源事件解除后人工释放该响应对目标仪表的控制锁。"""

        if any(event.key == event_key for event in self.events.active_events()):
            raise InstrumentWarning(
                "The source event is still active",
                "EVENT_RESPONSE_STILL_ACTIVE",
                event_key,
            )
        if event_key in self._event_response_tasks:
            raise InstrumentWarning(
                "The event response is still running",
                "EVENT_RESPONSE_IN_PROGRESS",
                event_key,
            )
        _response, target = self._latched_event_responses.pop(event_key)
        target_locks = self._event_response_target_locks[target]
        target_locks.remove(event_key)
        if not target_locks:
            self._event_response_target_locks.pop(target)
        self.events.report(
            Severity.INFO,
            "runtime",
            "EVENT_RESPONSE_RESET",
            f"Event response lock released for {target}",
            event_key,
        )

    def _ensure_event_response_control(
        self,
        instrument_id: str,
        origin: str,
    ) -> None:
        event_keys = self._event_response_target_locks.get(instrument_id)
        if event_keys and origin != "event_response":
            raise InstrumentWarning(
                f"Instrument {instrument_id} is locked by event response "
                + ", ".join(sorted(event_keys)),
                "EVENT_RESPONSE_LOCKED",
                instrument_id,
            )

    def _load_instruments(self) -> None:
        """根据已验证配置创建仪表客户端，但暂不连接真实仪表。

        外部 System Instrument 必须有有效且兼容的清单；不能通过在配置中直接写任意
        ``module:class`` 来加载第三方代码。
        """

        for instrument_config in self.config.instrument_instances:
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
            if self.isolate_processes:
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
                def in_process_factory(
                    backend_class: type[SystemInstrument] = instrument_class,
                    configured: InstrumentConfig = instrument_config,
                    external: bool = descriptor is not None,
                ) -> InProcessInstrumentClient:
                    return InProcessInstrumentClient(
                        (
                            backend_class(configured)
                            if external
                            else backend_class(
                                configured,
                                simulation_speed=self.config.simulation_speed,
                            )
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
            for panel in instrument_config.panels:
                if (
                    panel.enabled
                    and panel.template == "controller"
                    and panel.stability is not None
                ):
                    self._stability[panel.key] = StabilityEvaluator(
                        panel.stability
                    )

    def connection_state(self, instrument_id: str) -> InstrumentConnectionState:
        """返回指定仪表当前连接生命周期状态。"""

        return self._connection_states[instrument_id]

    @staticmethod
    def _controller_panels(
        config: InstrumentConfig,
    ) -> tuple[InstrumentPanelConfig, ...]:
        """Return enabled controller panels for one physical instance."""

        return tuple(
            panel
            for panel in config.panels
            if panel.enabled and panel.template == "controller"
        )

    @staticmethod
    def _states_for_control(
        config: InstrumentConfig,
        snapshot: InstrumentSnapshot,
        control: str,
    ) -> tuple[InstrumentControlState, ...]:
        """Return every panel state bound to one backend control endpoint."""

        return tuple(
            snapshot.controls[panel.id]
            for panel in InstrumentManager._controller_panels(config)
            if panel.control_id == control
        )

    @staticmethod
    def _sync_primary_control_fields(
        config: InstrumentConfig,
        snapshot: InstrumentSnapshot,
    ) -> None:
        """让顶层字段始终表示该物理仪表承担的标准温度或磁场回路。"""

        panels = InstrumentManager._controller_panels(config)
        primary_role = {
            InstrumentKind.TEMPERATURE: "sample_temp",
            InstrumentKind.FIELD: "field",
        }.get(config.kind)
        primary = next(
            (panel for panel in panels if panel.role == primary_role),
            panels[0] if len(panels) == 1 else None,
        )
        if primary is None:
            if panels:
                snapshot.target = None
                snapshot.rate_per_minute = None
                snapshot.ready = None
                snapshot.stability = StabilityState.NOT_APPLICABLE
                snapshot.activity = (
                    InstrumentActivity.MOVING
                    if any(
                        state.activity is InstrumentActivity.MOVING
                        for state in snapshot.controls.values()
                    )
                    else InstrumentActivity.HOLDING
                )
            return
        state = snapshot.controls[primary.id]
        snapshot.target = state.target
        snapshot.rate_per_minute = state.rate_per_minute
        snapshot.ready = state.ready
        snapshot.activity = state.activity
        snapshot.stability = state.stability

    @property
    def control_ready(self) -> bool:
        """所有参与控制的仪表均已连接且读数新鲜时为真。"""

        return self.control_block_reason() is None

    def control_block_reason(self) -> str | None:
        """返回首个阻止手动控制或启动 SEQ 的原因。"""

        now = time.monotonic()
        for config in self.config.instrument_instances:
            if not any(
                panel.enabled
                and panel.template == "controller"
                and panel.role != "none"
                for panel in config.panels
            ):
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
                unit=config.unit,
                current=None,
                connection_state=state,
                controls={
                    panel.id: InstrumentControlState(
                        target=self._expected_targets.get(
                            (instrument_id, panel.control_id)
                        ),
                        rate_per_minute=self._expected_rates.get(
                            (instrument_id, panel.control_id)
                        ),
                    )
                    for panel in self._controller_panels(config)
                },
            )
            self._sync_primary_control_fields(config, snapshot)
        for control_state in snapshot.controls.values():
            control_state.activity = (
                InstrumentActivity.FAULT
                if state is InstrumentConnectionState.FAULTED
                else InstrumentActivity.DISCONNECTED
            )
            control_state.stability = StabilityState.STALE
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

        config = self.instrument_configs[instrument_id]
        checked_controls: set[str] = set()
        for panel in self._controller_panels(config):
            control = panel.control_id
            if control in checked_controls:
                continue
            checked_controls.add(control)
            expected_target = self._expected_targets.get(
                (instrument_id, control)
            )
            if expected_target is None:
                continue
            states = self._states_for_control(config, snapshot, control)
            actual_target = states[0].target
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
                    f"{config.display_name} control {control!r} reconnected with "
                    f"target {actual_target!r}, expected {expected_target:g}",
                    "INSTRUMENT_STATE_MISMATCH_AFTER_RECONNECT",
                    panel.key,
                )
            expected_rate = self._expected_rates.get(
                (instrument_id, control)
            )
            actual_rate = states[0].rate_per_minute
            if expected_rate is not None and actual_rate is not None:
                rate_tolerance = max(1e-9, abs(expected_rate) * 1e-9)
                if not math.isclose(
                    actual_rate,
                    expected_rate,
                    rel_tol=1e-9,
                    abs_tol=rate_tolerance,
                ):
                    raise InstrumentError(
                        f"{config.display_name} control {control!r} reconnected "
                        f"with rate {actual_rate:g}, expected {expected_rate:g}",
                        "INSTRUMENT_STATE_MISMATCH_AFTER_RECONNECT",
                        panel.key,
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
                            candidate.open(),  # type: ignore[attr-defined]
                            timeout=remaining,
                        )
                        remaining = max(0.0, deadline - time.monotonic())
                        reading = await asyncio.wait_for(
                            candidate.read_status(),  # type: ignore[attr-defined]
                            timeout=remaining,
                        )
                        snapshot = self._snapshot_from_reading(
                            instrument_id,
                            reading,
                            measurement=False,
                        )
                        self._validate_snapshot(instrument_id, snapshot)
                        self._validate_recovered_state(instrument_id, snapshot)
                        self.instruments[instrument_id] = candidate
                        self.latest[instrument_id] = snapshot
                        self._connection_states[instrument_id] = (
                            InstrumentConnectionState.CONNECTED
                        )
                        self._unavailable_after_timeout.pop(instrument_id, None)
                        config = self.instrument_configs[instrument_id]
                        for panel in self._controller_panels(config):
                            state = snapshot.controls[panel.id]
                            evaluator = self._stability.get(panel.key)
                            if evaluator is not None and state.target is not None:
                                evaluator.reset(
                                    state.target,
                                    snapshot.timestamp,
                                )
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
                if operation in {"set_target", "hold"} or operation.startswith(
                    "sequence_command:"
                ):
                    self._ensure_event_response_control(
                        instrument_id,
                        origin or "",
                    )
                if origin == "manual" and self._control_owner == "sequence":
                    raise InstrumentWarning(
                        f"{config.display_name} manual control is blocked while a SEQ owns control",
                        "MANUAL_CONTROL_BLOCKED",
                        instrument_id,
                    )
                if (
                    operation
                    not in {
                        "open",
                        "close",
                        "event_responses",
                        "poll",
                        "poll_measurement",
                    }
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
                responses = await self._operate(
                    instrument_id,
                    "event_responses",
                    instrument.event_responses,
                )
                self._register_event_responses(
                    instrument_id,
                    responses,
                )
                await self._operate(instrument_id, "open", instrument.open)
                self._connection_states[instrument_id] = (
                    InstrumentConnectionState.CONNECTED
                )
                self.events.resolve(instrument_id, "CONNECT_FAILED")
                self.events.report(Severity.INFO, instrument_id, "CONNECTED", "Instrument connected")
            except InstrumentError as exc:
                if exc.code == "INVALID_EVENT_RESPONSES":
                    raise
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
        response_tasks = tuple(self._event_response_tasks.values())
        for task in response_tasks:
            task.cancel()
        if response_tasks:
            await asyncio.gather(
                *response_tasks,
                return_exceptions=True,
            )
        self._event_response_tasks.clear()
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
                    "close",
                    instrument.close,
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
                    await instrument.shutdown()  # type: ignore[attr-defined]
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
        measured: dict[str, InstrumentSnapshot] = {}
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
                measured[instrument_id] = result
                self.events.resolve(instrument_id, "POLL_FAILED")
                for code, context in self._poll_issues[instrument_id]:
                    self.events.resolve(instrument_id, code, context)
                self._poll_issues[instrument_id].clear()
            if not measurement:
                self._update_stale_state(
                    instrument_id,
                    now,
                    poll_succeeded=not isinstance(result, Exception),
                )
        return deepcopy(measured if measurement else self.latest)

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
                reading = await client.read_measurement()  # type: ignore[attr-defined]
            else:
                reading = await client.read_status()  # type: ignore[attr-defined]
            snapshot = self._snapshot_from_reading(
                instrument_id,
                reading,
                measurement=measurement,
            )
            self._validate_snapshot(
                instrument_id,
                snapshot,
                measurement=measurement,
            )
            self._connection_states[instrument_id] = (
                InstrumentConnectionState.CONNECTED
            )
            if not measurement:
                config = self.instrument_configs[instrument_id]
                for panel in self._controller_panels(config):
                    state = snapshot.controls[panel.id]
                    control_key = (instrument_id, panel.control_id)
                    if control_key not in self._expected_targets:
                        self._expected_targets[control_key] = state.target
                        self._expected_rates[control_key] = (
                            state.rate_per_minute
                        )
                    evaluator = self._stability.get(panel.key)
                    if (
                        evaluator is not None
                        and state.current is not None
                        and state.target is not None
                    ):
                        result = evaluator.update(
                            state.current,
                            state.target,
                            snapshot.timestamp,
                            ready=state.ready,
                        )
                        state.stability = result.state
                        timeout_code = "STABILITY_TIMEOUT"
                        if result.state is StabilityState.TIMED_OUT:
                            self.events.report(
                                self.config.alarms.stability_timeout,
                                instrument_id,
                                timeout_code,
                                f"{panel.display_name} did not stabilize within "
                                f"{result.elapsed_seconds:.1f} seconds",
                                panel.key,
                            )
                        else:
                            self.events.resolve(
                                instrument_id,
                                timeout_code,
                                panel.key,
                            )
                self._sync_primary_control_fields(config, snapshot)
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

    def _snapshot_from_reading(
        self,
        instrument_id: str,
        reading: dict[str, object],
        *,
        measurement: bool,
    ) -> InstrumentSnapshot:
        """用框架配置把驱动的纯数值结果组装成内部快照。"""

        config = self.instrument_configs[instrument_id]
        auxiliary = reading.get("auxiliary", {})
        if not isinstance(auxiliary, dict):
            raise InstrumentError(
                f"{config.display_name} returned invalid auxiliary readings",
                "INVALID_INSTRUMENT_READING",
                instrument_id,
            )
        selected = set(config.auxiliary_readings)
        returned = set(auxiliary)
        unexpected = returned - selected
        if unexpected:
            raise InstrumentError(
                f"{config.display_name} returned undeclared auxiliary readings: "
                + ", ".join(sorted(unexpected)),
                "INVALID_INSTRUMENT_READING",
                instrument_id,
            )
        missing = selected - returned
        if missing and not measurement:
            raise InstrumentError(
                f"{config.display_name} omitted selected auxiliary readings: "
                + ", ".join(sorted(missing)),
                "INVALID_INSTRUMENT_READING",
                instrument_id,
            )
        moving_value = reading.get("moving", False)
        if not isinstance(moving_value, bool):
            raise InstrumentError(
                f"{config.display_name} returned a non-boolean moving flag",
                "INVALID_INSTRUMENT_READING",
                instrument_id,
            )
        moving = moving_value
        activity = (
            InstrumentActivity.MOVING
            if moving
            else (
                InstrumentActivity.IDLE
                if config.kind is InstrumentKind.MONITOR
                else InstrumentActivity.HOLDING
            )
        )
        metrics = {
            key: InstrumentMetric(
                display_name=metadata.display_name,
                value=auxiliary.get(key),
                unit=metadata.unit,
                decimals=metadata.decimals,
            )
            for key in config.auxiliary_readings
            for metadata in (config.reading(key),)
        }
        snapshot = InstrumentSnapshot(
            instrument_id=instrument_id,
            display_name=config.display_name,
            kind=config.kind,
            timestamp=time.monotonic(),
            unit=config.unit,
            current=(
                None
                if reading.get("value") is None
                else float(reading["value"])
            ),
            target=(
                None
                if reading.get("target") is None
                else float(reading["target"])
            ),
            rate_per_minute=(
                None
                if reading.get("rate") is None
                else float(reading["rate"])
            ),
            activity=activity,
            connection_state=InstrumentConnectionState.CONNECTED,
            ready=reading.get("ready"),
            metrics=metrics,
        )
        if measurement:
            return snapshot

        panels = self._controller_panels(config)
        required_controls = {panel.control_id for panel in panels}
        declared_controls = {
            panel.control_id
            for panel in config.panels
            if panel.template == "controller"
        }
        raw_controls = reading.get("controls")
        if raw_controls is None:
            if len(required_controls) > 1:
                raise InstrumentError(
                    f"{config.display_name} has multiple controls and must return "
                    "a controls object from read_status",
                    "INVALID_INSTRUMENT_READING",
                    instrument_id,
                )
            control_payloads = (
                {next(iter(required_controls)): reading}
                if required_controls
                else {}
            )
        else:
            if not isinstance(raw_controls, dict):
                raise InstrumentError(
                    f"{config.display_name} returned invalid control states",
                    "INVALID_INSTRUMENT_READING",
                    instrument_id,
                )
            returned_controls = set(raw_controls)
            if (
                any(not isinstance(key, str) for key in returned_controls)
                or returned_controls - declared_controls
                or required_controls - returned_controls
            ):
                raise InstrumentError(
                    f"{config.display_name} returned control states that do not "
                    "match its configured controller panels",
                    "INVALID_INSTRUMENT_READING",
                    instrument_id,
                )
            control_payloads = raw_controls

        for panel in panels:
            payload = control_payloads[panel.control_id]
            if not isinstance(payload, dict):
                raise InstrumentError(
                    f"{config.display_name} returned invalid state for control "
                    f"{panel.control_id!r}",
                    "INVALID_INSTRUMENT_READING",
                    panel.key,
                )
            moving_value = payload.get("moving", False)
            if not isinstance(moving_value, bool):
                raise InstrumentError(
                    f"{config.display_name} control {panel.control_id!r} returned "
                    "a non-boolean moving flag",
                    "INVALID_INSTRUMENT_READING",
                    panel.key,
                )
            current_value = (
                snapshot.current
                if panel.reading == config.main_reading
                else snapshot.metrics[panel.reading].value
            )
            if current_value is not None and (
                isinstance(current_value, bool)
                or not isinstance(current_value, (int, float))
            ):
                raise InstrumentError(
                    f"{config.display_name} controller panel {panel.id!r} "
                    "returned a non-numeric reading",
                    "INVALID_INSTRUMENT_READING",
                    panel.key,
                )
            snapshot.controls[panel.id] = InstrumentControlState(
                current=(
                    None
                    if current_value is None
                    else float(current_value)
                ),
                target=(
                    None
                    if payload.get("target") is None
                    else float(payload["target"])
                ),
                rate_per_minute=(
                    None
                    if payload.get("rate") is None
                    else float(payload["rate"])
                ),
                activity=(
                    InstrumentActivity.MOVING
                    if moving_value
                    else InstrumentActivity.HOLDING
                ),
                ready=payload.get("ready"),
            )
        self._sync_primary_control_fields(config, snapshot)
        return snapshot

    def _validate_snapshot(
        self,
        instrument_id: str,
        snapshot: InstrumentSnapshot,
        *,
        measurement: bool = False,
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
        if snapshot.ready is not None and not isinstance(
            snapshot.ready,
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
        expected_control_panels = {
            panel.id for panel in self._controller_panels(config)
        }
        if not measurement and set(snapshot.controls) != expected_control_panels:
            raise InstrumentError(
                f"{config.display_name} returned incomplete controller states",
                "INVALID_INSTRUMENT_SNAPSHOT",
                instrument_id,
            )
        for panel_id, state in snapshot.controls.items():
            if not isinstance(state, InstrumentControlState):
                raise InstrumentError(
                    f"{config.display_name} returned invalid state for controller "
                    f"panel {panel_id!r}",
                    "INVALID_INSTRUMENT_SNAPSHOT",
                    instrument_id,
                )
            if state.ready is not None and not isinstance(state.ready, bool):
                raise InstrumentError(
                    f"{config.display_name} controller panel {panel_id!r} returned "
                    "a non-boolean ready flag",
                    "INVALID_INSTRUMENT_SNAPSHOT",
                    instrument_id,
                )
            control_values = (
                state.current,
                state.target,
                state.rate_per_minute,
            )
            if any(
                value is not None
                and (
                    isinstance(value, bool)
                    or not math.isfinite(value)
                )
                for value in control_values
            ):
                raise InstrumentError(
                    f"{config.display_name} controller panel {panel_id!r} returned "
                    "a non-finite numeric state",
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
            for control_state in snapshot.controls.values():
                control_state.stability = StabilityState.STALE
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

        return self.resolve_control_panel(kind).instrument_id

    @staticmethod
    def _role_for_kind(kind: InstrumentKind) -> str:
        """把标准 SEQ 控制类型映射到唯一面板角色。"""

        if kind is InstrumentKind.TEMPERATURE:
            return "sample_temp"
        if kind is InstrumentKind.FIELD:
            return "field"
        raise InstrumentError(
            f"No standard control role exists for {kind.value}",
            "INSTRUMENT_NOT_CONFIGURED",
            kind.value,
        )

    def resolve_control_panel(
        self,
        kind: InstrumentKind,
        requested: object | None = None,
    ) -> InstrumentPanelConfig:
        """按全局角色解析标准温度或磁场控制回路。"""

        role = self._role_for_kind(kind)
        candidate = str(requested or "").strip()
        if candidate:
            config = self.instrument_configs.get(candidate)
            if config is None:
                raise InstrumentError(
                    f"Unknown {kind.value} instrument: {candidate}",
                    "UNKNOWN_INSTRUMENT",
                    candidate,
                )
            for panel in config.panels:
                if (
                    panel.enabled
                    and panel.template == "controller"
                    and panel.role == role
                ):
                    return panel
            raise InstrumentError(
                f"Instrument {candidate} is not assigned the {role} role",
                "INSTRUMENT_ROLE_MISMATCH",
                candidate,
            )

        for panel in self.config.panels:
            if panel.template == "controller" and panel.role == role:
                return panel
        raise InstrumentError(
            f"No controller panel is assigned the {role} role",
            "INSTRUMENT_NOT_CONFIGURED",
            role,
        )

    def resolve_instrument_id(
        self,
        kind: InstrumentKind,
        requested: object | None = None,
    ) -> str:
        """按标准角色解析仪表；显式目标仅供内部事件响应校验。"""

        return self.resolve_control_panel(kind, requested).instrument_id

    def _controller_panel(
        self,
        instrument_id: str,
        control: str,
    ) -> InstrumentPanelConfig:
        """返回物理实例中指定的已启用控制回路面板。"""

        config = self.instrument_configs.get(instrument_id)
        if config is None:
            raise InstrumentError(
                f"Unknown instrument: {instrument_id}",
                "UNKNOWN_INSTRUMENT",
                instrument_id,
            )
        for panel in config.panels:
            if (
                panel.enabled
                and panel.template == "controller"
                and panel.control_id == control
            ):
                return panel
        raise InstrumentError(
            f"Instrument {instrument_id} has no enabled control {control!r}",
            "TARGET_NOT_CONTROLLABLE",
            instrument_id,
        )

    def validate_target(
        self,
        instrument_id: str,
        value: float,
        rate_per_minute: float,
        *,
        control: str,
    ) -> None:
        """在任何写入前强制检查有限值、上下限、最大速率和仪表角色。"""

        panel = self._controller_panel(instrument_id, control)
        config = self.instrument_configs[instrument_id]
        reading = config.reading(panel.reading)
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
        if not panel.min_value <= value <= panel.max_value:
            raise SafetyViolation(
                f"{panel.display_name} target {value:g} {reading.unit} is outside the allowed range "
                f"[{panel.min_value:g}, {panel.max_value:g}] {reading.unit}",
                "TARGET_OUT_OF_RANGE",
                instrument_id,
            )
        if rate_per_minute <= 0 or rate_per_minute > panel.max_rate_per_minute:
            raise SafetyViolation(
                f"{panel.display_name} rate {rate_per_minute:g} {reading.unit}/min is outside the allowed range "
                f"(0, {panel.max_rate_per_minute:g}]",
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
        control: str,
        origin: str = "sequence",
    ) -> bool:
        """设置目标；手动来源在 SEQ 持有控制租约时会被运行时拒绝。"""

        try:
            self._ensure_event_response_control(instrument_id, origin)
            self.validate_target(
                instrument_id,
                value,
                rate_per_minute,
                control=control,
            )
            await self._operate(
                instrument_id,
                "set_target",
                lambda: self.instruments[instrument_id].set_target(
                    value,
                    rate_per_minute,
                    mode,
                    control=control,
                ),
                origin=origin,
                priority=(
                    _INSTRUMENT_EVENT_RESPONSE_PRIORITY
                    if origin == "event_response"
                    else _INSTRUMENT_CONTROL_PRIORITY
                ),
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
        control_key = (instrument_id, control)
        self._expected_targets[control_key] = value
        self._expected_rates[control_key] = rate_per_minute
        config = self.instrument_configs[instrument_id]
        snapshot = self.latest.get(instrument_id)
        if snapshot is not None:
            for panel in self._controller_panels(config):
                if panel.control_id != control:
                    continue
                state = snapshot.controls[panel.id]
                state.target = value
                state.rate_per_minute = rate_per_minute
                state.activity = InstrumentActivity.MOVING
                state.stability = StabilityState.MOVING
            self._sync_primary_control_fields(config, snapshot)
        reset_at = time.monotonic()
        for panel in self._controller_panels(config):
            if panel.control_id != control:
                continue
            evaluator = self._stability.get(panel.key)
            if evaluator is not None:
                evaluator.reset(value, reset_at)
        self.events.resolve(instrument_id, "TARGET_OUT_OF_RANGE", instrument_id)
        self.events.resolve(instrument_id, "RATE_OUT_OF_RANGE", instrument_id)
        return True

    async def set_target_by_kind(
        self,
        kind: InstrumentKind,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        origin: str = "sequence",
    ) -> bool:
        """供标准 SEQ 命令按全局唯一角色选择控制面板并设置目标。"""

        panel = self.resolve_control_panel(kind)
        return await self.set_target(
            panel.instrument_id,
            value,
            rate_per_minute,
            mode,
            control=panel.control_id,
            origin=origin,
        )

    def has_sequence_command(
        self,
        instrument_id: str,
        command_id: str,
    ) -> bool:
        """返回逻辑仪表是否声明了该稳定指令 ID。"""

        return (
            instrument_id,
            command_id,
        ) in self._sequence_command_specs

    async def execute_sequence_command(
        self,
        instrument_id: str,
        command_id: str,
        *,
        origin: str = "sequence",
    ) -> bool:
        """串行执行一条清单指令；SEQ 与手动面板共用同一后端入口。"""

        if not self.has_sequence_command(instrument_id, command_id):
            raise InstrumentError(
                f"System Instrument sequence command is unavailable: "
                f"{instrument_id}.{command_id}",
                "INSTRUMENT_SEQUENCE_COMMAND_UNAVAILABLE",
                f"{instrument_id}.{command_id}",
            )
        operation = f"sequence_command:{command_id}"
        try:
            self._ensure_event_response_control(instrument_id, origin)
            await self._operate(
                instrument_id,
                operation,
                lambda: self.instruments[
                    instrument_id
                ].execute_sequence_command(command_id),
                origin=origin,
            )
        except InstrumentWarning as exc:
            self.events.report(
                Severity.WARNING,
                instrument_id,
                exc.code,
                str(exc),
                exc.context,
            )
            return False
        except InstrumentError as exc:
            if self._uncertain_write_error(exc):
                await self._fault_uncertain_write(
                    instrument_id,
                    operation,
                    exc,
                )
                raise InstrumentError(
                    f"{self.instrument_configs[instrument_id].display_name} "
                    f"sequence command {command_id!r} could not be confirmed "
                    "and was not replayed",
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    instrument_id,
                ) from exc
            self.events.report(
                Severity.ERROR,
                instrument_id,
                exc.code,
                str(exc),
                exc.context,
            )
            raise
        return True

    def _record_confirmed_hold(
        self,
        instrument_id: str,
        control: str,
    ) -> None:
        """Update cached state after one backend control endpoint confirms Hold."""

        snapshot = self.latest.get(instrument_id)
        if snapshot is None:
            return
        config = self.instrument_configs[instrument_id]
        panels = tuple(
            panel
            for panel in self._controller_panels(config)
            if panel.control_id == control
        )
        reference = snapshot.controls[panels[0].id]
        control_key = (instrument_id, control)
        self._expected_targets[control_key] = reference.current
        self._expected_rates[control_key] = reference.rate_per_minute
        reset_at = time.monotonic()
        for panel in panels:
            state = snapshot.controls[panel.id]
            state.target = reference.current
            state.activity = InstrumentActivity.HOLDING
            state.stability = StabilityState.SETTLING
            evaluator = self._stability.get(panel.key)
            if evaluator is not None and reference.current is not None:
                evaluator.reset(reference.current, reset_at)
        self._sync_primary_control_fields(config, snapshot)

    async def hold_all(self) -> bool:
        """尽力 Hold 所有可控温度和磁场仪表，并返回是否全部成功。"""

        async def hold(
            instrument_id: str,
            control: str,
        ) -> bool:
            config = self.instrument_configs[instrument_id]
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
                self._ensure_event_response_control(
                    instrument_id,
                    "manual",
                )
                await self._operate(
                    instrument_id,
                    "hold",
                    lambda: self.instruments[instrument_id].hold(  # type: ignore[attr-defined]
                        control=control
                    ),
                )
                self._record_confirmed_hold(instrument_id, control)
                return True
            except InstrumentWarning as exc:
                self.events.report(
                    Severity.WARNING,
                    instrument_id,
                    exc.code,
                    str(exc),
                    exc.context,
                )
                return False
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

        controls = tuple(
            dict.fromkeys(
                (panel.instrument_id, panel.control_id)
                for panel in self.config.panels
                if panel.template == "controller"
            )
        )
        results = await asyncio.gather(
            *(hold(instrument_id, control) for instrument_id, control in controls)
        )
        return all(results)

    async def hold_instrument(
        self,
        instrument_id: str,
        *,
        control: str,
        origin: str = "manual",
    ) -> None:
        """Hold 单台仪表；同样执行控制权与连接状态检查。"""

        if instrument_id not in self.instruments:
            raise InstrumentError(f"Unknown instrument: {instrument_id}", "UNKNOWN_INSTRUMENT", instrument_id)
        self._controller_panel(instrument_id, control)
        try:
            self._ensure_event_response_control(instrument_id, origin)
            await self._operate(
                instrument_id,
                "hold",
                lambda: self.instruments[instrument_id].hold(  # type: ignore[attr-defined]
                    control=control
                ),
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
        self._record_confirmed_hold(instrument_id, control)

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
