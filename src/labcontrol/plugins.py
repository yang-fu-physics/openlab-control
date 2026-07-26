from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import TypeVar

from .config import AppConfig, DeviceConfig
from .devices.base import DeviceError, DevicePlugin, DeviceWarning, SafetyViolation
from .devices.manifest import (
    DevicePluginDescriptor,
    device_dependency_directory,
)
from .devices.worker import (
    DeviceWorkerClient,
    DeviceWorkerSpec,
    InProcessDeviceClient,
    IsolatedDeviceClient,
)
from .events import EventManager
from .extensions.loading import load_import_object, load_source_object
from .extensions.trust import PluginTrustStore, extension_tree_digest
from .models import (
    DeviceActivity,
    DeviceConnectionState,
    DeviceKind,
    DeviceRole,
    DeviceSnapshot,
    Severity,
    StabilityState,
)
from .stability import StabilityEvaluator


T = TypeVar("T")


class DeviceManager:
    def __init__(
        self,
        config: AppConfig,
        events: EventManager,
        descriptors: tuple[DevicePluginDescriptor, ...] = (),
        *,
        isolate_processes: bool = True,
    ) -> None:
        self.config = config
        self.events = events
        self.descriptors = {descriptor.id: descriptor for descriptor in descriptors}
        self.isolate_processes = isolate_processes
        self.devices: dict[str, object] = {}
        self._client_factories: dict[str, Callable[[], object]] = {}
        self.device_configs: dict[str, DeviceConfig] = {item.id: item for item in config.devices}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stability: dict[str, StabilityEvaluator] = {}
        self._poll_issues: dict[str, set[tuple[str, str]]] = {}
        self._stale_devices: set[str] = set()
        self._unavailable_after_timeout: dict[str, str] = {}
        self._control_owner: str | None = None
        self._connection_states: dict[str, DeviceConnectionState] = {}
        self._recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self._recovery_clients: dict[str, object] = {}
        self._generation: dict[str, int] = {}
        self._expected_targets: dict[str, float | None] = {}
        self._expected_rates: dict[str, float | None] = {}
        self._shutting_down = False
        self.latest: dict[str, DeviceSnapshot] = {}
        self._load_plugins()

    def _load_plugins(self) -> None:
        trust_store: PluginTrustStore | None = None
        for device_config in self.config.devices:
            descriptor: DevicePluginDescriptor | None = None
            if ":" in device_config.plugin:
                module_name = device_config.plugin.split(":", 1)[0]
                if not module_name.startswith("labcontrol.devices."):
                    raise PermissionError(
                        "Unmanifested third-party device imports are disabled; "
                        f"copy {device_config.plugin!r} into device_plugins with device.toml"
                    )
            else:
                descriptor = self.descriptors.get(device_config.plugin)
                if descriptor is None:
                    raise ValueError(
                        f"Unknown external device plugin {device_config.plugin!r}"
                    )
                if not descriptor.can_load:
                    raise ValueError(
                        f"Device plugin {descriptor.id} is invalid: {descriptor.error}"
                    )
                if device_config.kind not in descriptor.kinds:
                    raise TypeError(
                        f"Device plugin {descriptor.id} does not support "
                        f"{device_config.kind.value}"
                    )
                current_fingerprint = extension_tree_digest(descriptor.path)
                if current_fingerprint != descriptor.fingerprint:
                    raise PermissionError(
                        f"Device plugin {descriptor.id} changed after discovery"
                    )
                if trust_store is None:
                    trust_store = PluginTrustStore(
                        self.config.resolve_project_path(
                            self.config.plugins.state_directory
                        )
                        / "trusted_plugins.json"
                    )
                if not trust_store.is_trusted("device", descriptor):
                    raise PermissionError(
                        f"Device plugin {descriptor.id} has not been trusted"
                    )
            if self.isolate_processes:
                dependency_directory = (
                    ""
                    if descriptor is None
                    else str(device_dependency_directory(self.config, descriptor))
                )
                worker_spec = DeviceWorkerSpec(
                    device_config=device_config,
                    simulation_speed=self.config.simulation_speed,
                    plugin_id=(
                        "builtin"
                        if descriptor is None
                        else descriptor.id
                    ),
                    backend=(
                        device_config.plugin
                        if descriptor is None
                        else descriptor.backend
                    ),
                    plugin_directory=(
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
                )
                def isolated_factory(
                    spec: DeviceWorkerSpec = worker_spec,
                    configured: DeviceConfig = device_config,
                ) -> IsolatedDeviceClient:
                    return IsolatedDeviceClient(
                        DeviceWorkerClient(spec),
                        startup_timeout_seconds=(
                            self.config.plugins.device_startup_timeout_seconds
                        ),
                        operation_timeout_seconds=(
                            configured.operation_timeout_seconds
                        ),
                        shutdown_timeout_seconds=(
                            configured.shutdown_timeout_seconds
                        ),
                    )

                self._client_factories[device_config.id] = isolated_factory
                self.devices[device_config.id] = isolated_factory()
            else:
                plugin_class = (
                    load_import_object(device_config.plugin)
                    if descriptor is None
                    else load_source_object(
                        descriptor.path,
                        descriptor.backend,
                        f"device_{descriptor.id}",
                    )
                )
                if (
                    not isinstance(plugin_class, type)
                    or not issubclass(plugin_class, DevicePlugin)
                ):
                    raise TypeError(f"{device_config.plugin} is not a DevicePlugin")
                if (
                    str(getattr(plugin_class, "api_version", ""))
                    != DevicePlugin.api_version
                ):
                    raise TypeError(
                        f"{device_config.plugin} uses incompatible device API "
                        f"{getattr(plugin_class, 'api_version', '')!r}"
                    )
                def in_process_factory(
                    backend_class: type[DevicePlugin] = plugin_class,
                    configured: DeviceConfig = device_config,
                ) -> InProcessDeviceClient:
                    return InProcessDeviceClient(
                        backend_class(
                            configured,
                            simulation_speed=self.config.simulation_speed,
                        )
                    )

                self._client_factories[device_config.id] = in_process_factory
                self.devices[device_config.id] = in_process_factory()
            self._locks[device_config.id] = asyncio.Lock()
            self._poll_issues[device_config.id] = set()
            self._connection_states[device_config.id] = (
                DeviceConnectionState.STARTING
            )
            self._generation[device_config.id] = 0
            if device_config.stability is not None:
                self._stability[device_config.id] = StabilityEvaluator(device_config.stability)

    def connection_state(self, device_id: str) -> DeviceConnectionState:
        return self._connection_states[device_id]

    @property
    def control_ready(self) -> bool:
        return self.control_block_reason() is None

    def control_block_reason(self) -> str | None:
        now = time.monotonic()
        for config in self.config.devices:
            if not config.control_enabled:
                continue
            state = self._connection_states[config.id]
            if state is not DeviceConnectionState.CONNECTED:
                return f"{config.display_name} is {state.value}"
            snapshot = self.latest.get(config.id)
            if snapshot is None or not snapshot.connected:
                return f"{config.display_name} has no connected reading"
            if now - snapshot.timestamp > config.stale_after_seconds:
                return f"{config.display_name} reading is stale"
        return None

    def ensure_run_ready(self) -> None:
        reason = self.control_block_reason()
        if reason is not None:
            raise DeviceError(
                f"Cannot run SEQ: {reason}",
                "PRIMARY_DEVICE_NOT_READY",
                reason,
            )

    def _mark_snapshot_unavailable(
        self,
        device_id: str,
        state: DeviceConnectionState,
        message: str,
    ) -> None:
        config = self.device_configs[device_id]
        snapshot = deepcopy(self.latest.get(device_id))
        if snapshot is None:
            snapshot = DeviceSnapshot(
                device_id=device_id,
                display_name=config.display_name,
                kind=config.kind,
                timestamp=time.monotonic(),
                connected=False,
                unit=config.unit,
                current=None,
                target=self._expected_targets.get(device_id),
                rate_per_minute=self._expected_rates.get(device_id),
            )
        snapshot.connected = False
        snapshot.activity = (
            DeviceActivity.FAULT
            if state is DeviceConnectionState.FAULTED
            else DeviceActivity.DISCONNECTED
        )
        snapshot.stability = StabilityState.STALE
        snapshot.message = message
        snapshot.connection_state = state
        self.latest[device_id] = snapshot

    @staticmethod
    def _recoverable_read_error(exc: DeviceError) -> bool:
        return exc.code not in {
            "INVALID_DEVICE_SNAPSHOT",
            "NONFINITE_DEVICE_READING",
            "DEVICE_KIND_MISMATCH",
            "UNKNOWN_DEVICE",
        }

    @staticmethod
    def _uncertain_write_error(exc: DeviceError) -> bool:
        return exc.code in {
            "DEVICE_OPERATION_TIMEOUT",
            "DEVICE_WORKER_DISCONNECTED",
            "DEVICE_WORKER_EXITED",
            "DEVICE_WORKER_NOT_RUNNING",
            "DEVICE_IPC_INVALID_MESSAGE",
        }

    def _begin_recovery(self, device_id: str, exc: DeviceError) -> None:
        if self._shutting_down:
            return
        existing = self._recovery_tasks.get(device_id)
        if existing is not None and not existing.done():
            return
        self._generation[device_id] += 1
        generation = self._generation[device_id]
        self._connection_states[device_id] = DeviceConnectionState.RECONNECTING
        self._unavailable_after_timeout.pop(device_id, None)
        message = (
            f"{self.device_configs[device_id].display_name} lost communication; "
            f"retrying for up to "
            f"{self.config.plugins.device_reconnect_timeout_seconds:g} seconds"
        )
        self._mark_snapshot_unavailable(
            device_id,
            DeviceConnectionState.RECONNECTING,
            message,
        )
        self.events.report(
            Severity.WARNING,
            device_id,
            "DEVICE_RECONNECTING",
            message,
            device_id,
        )
        task = asyncio.create_task(
            self._recover_device(device_id, generation, exc)
        )
        self._recovery_tasks[device_id] = task

    def _validate_recovered_state(
        self,
        device_id: str,
        snapshot: DeviceSnapshot,
    ) -> None:
        expected_target = self._expected_targets.get(device_id)
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
            raise DeviceError(
                f"{self.device_configs[device_id].display_name} reconnected with "
                f"target {actual_target!r}, expected {expected_target:g}",
                "DEVICE_STATE_MISMATCH_AFTER_RECONNECT",
                device_id,
            )
        expected_rate = self._expected_rates.get(device_id)
        if expected_rate is not None and snapshot.rate_per_minute is not None:
            rate_tolerance = max(1e-9, abs(expected_rate) * 1e-9)
            if not math.isclose(
                snapshot.rate_per_minute,
                expected_rate,
                rel_tol=1e-9,
                abs_tol=rate_tolerance,
            ):
                raise DeviceError(
                    f"{self.device_configs[device_id].display_name} reconnected "
                    f"with rate {snapshot.rate_per_minute:g}, expected "
                    f"{expected_rate:g}",
                    "DEVICE_STATE_MISMATCH_AFTER_RECONNECT",
                    device_id,
                )

    async def _recover_device(
        self,
        device_id: str,
        generation: int,
        initial_error: DeviceError,
    ) -> None:
        timeout = self.config.plugins.device_reconnect_timeout_seconds
        interval = self.config.plugins.device_reconnect_interval_seconds
        deadline = time.monotonic() + timeout
        last_error: Exception = initial_error
        failure_code = "DEVICE_RECONNECT_FAILED"
        try:
            while not self._shutting_down and time.monotonic() < deadline:
                candidate = self._client_factories[device_id]()
                self._recovery_clients[device_id] = candidate
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    async with self._locks[device_id]:
                        if (
                            self._generation[device_id] != generation
                            or self._shutting_down
                        ):
                            return
                        previous = self.devices[device_id]
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
                        self._validate_snapshot(device_id, snapshot)
                        self._validate_recovered_state(device_id, snapshot)
                        self.devices[device_id] = candidate
                        snapshot.connected = True
                        snapshot.connection_state = (
                            DeviceConnectionState.CONNECTED
                        )
                        self.latest[device_id] = snapshot
                        self._connection_states[device_id] = (
                            DeviceConnectionState.CONNECTED
                        )
                        self._unavailable_after_timeout.pop(device_id, None)
                        evaluator = self._stability.get(device_id)
                        if evaluator is not None and snapshot.target is not None:
                            evaluator.reset(snapshot.target, snapshot.timestamp)
                    self._recovery_clients.pop(device_id, None)
                    self.events.resolve(
                        device_id,
                        "DEVICE_RECONNECTING",
                        device_id,
                    )
                    self.events.resolve(
                        device_id,
                        "DEVICE_RECONNECT_FAILED",
                        device_id,
                    )
                    self.events.report(
                        Severity.INFO,
                        device_id,
                        "DEVICE_RECONNECTED",
                        f"{snapshot.display_name} reconnected and state was verified",
                        device_id,
                    )
                    return
                except asyncio.CancelledError:
                    await candidate.force_stop(0.25)  # type: ignore[attr-defined]
                    raise
                except Exception as exc:
                    last_error = exc
                    await candidate.force_stop(0.25)  # type: ignore[attr-defined]
                    self._recovery_clients.pop(device_id, None)
                    if (
                        isinstance(exc, DeviceError)
                        and exc.code
                        == "DEVICE_STATE_MISMATCH_AFTER_RECONNECT"
                    ):
                        failure_code = exc.code
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(interval, remaining))
            if self._generation[device_id] != generation or self._shutting_down:
                return
            self._connection_states[device_id] = DeviceConnectionState.FAULTED
            if failure_code == "DEVICE_STATE_MISMATCH_AFTER_RECONNECT":
                message = str(last_error)
            else:
                message = (
                    f"{self.device_configs[device_id].display_name} did not reconnect "
                    f"within {timeout:g} seconds: {last_error}"
                )
            self._mark_snapshot_unavailable(
                device_id,
                DeviceConnectionState.FAULTED,
                message,
            )
            config = self.device_configs[device_id]
            severity = (
                Severity.ERROR
                if config.control_enabled
                else Severity.WARNING
            )
            self.events.report(
                severity,
                device_id,
                failure_code,
                message,
                device_id,
            )
        finally:
            current = self._recovery_tasks.get(device_id)
            if current is asyncio.current_task():
                self._recovery_tasks.pop(device_id, None)
            self._recovery_clients.pop(device_id, None)

    async def _fault_uncertain_write(
        self,
        device_id: str,
        operation: str,
        exc: DeviceError,
    ) -> None:
        self.events.report(
            Severity.ERROR,
            device_id,
            exc.code,
            str(exc),
            exc.context,
        )
        self._generation[device_id] += 1
        task = self._recovery_tasks.pop(device_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._connection_states[device_id] = DeviceConnectionState.FAULTED
        message = (
            f"{self.device_configs[device_id].display_name} {operation} result is "
            f"unknown after communication failure; the command will not be replayed"
        )
        self._mark_snapshot_unavailable(
            device_id,
            DeviceConnectionState.FAULTED,
            message,
        )
        try:
            await self.devices[device_id].force_stop(0.25)  # type: ignore[attr-defined]
        except Exception:
            pass
        self.events.report(
            Severity.ERROR,
            device_id,
            "DEVICE_WRITE_RESULT_UNKNOWN",
            f"{message}: {exc}",
            operation,
        )

    async def _operate(
        self,
        device_id: str,
        operation: str,
        callback: Callable[[], Awaitable[T]],
        *,
        shutdown: bool = False,
        origin: str | None = None,
    ) -> T:
        config = self.device_configs[device_id]
        timeout = (
            config.shutdown_timeout_seconds
            if shutdown
            else config.operation_timeout_seconds
        )

        async def serialized() -> T:
            async with self._locks[device_id]:
                if origin == "manual" and self._control_owner == "sequence":
                    raise DeviceWarning(
                        f"{config.display_name} manual control is blocked while a SEQ owns control",
                        "MANUAL_CONTROL_BLOCKED",
                        device_id,
                    )
                if (
                    operation
                    not in {"connect", "disconnect", "poll"}
                    and self._connection_states[device_id]
                    is not DeviceConnectionState.CONNECTED
                ):
                    state = self._connection_states[device_id]
                    raise DeviceError(
                        f"{config.display_name} is {state.value}; "
                        f"{operation} was not sent",
                        "DEVICE_NOT_READY",
                        device_id,
                    )
                previous = self._unavailable_after_timeout.get(device_id)
                if previous is not None and not shutdown:
                    raise DeviceError(
                        f"{config.display_name} is unavailable after timed-out "
                        f"{previous}; restart OpenLab Control before further I/O",
                        "DEVICE_UNAVAILABLE_AFTER_TIMEOUT",
                        operation,
                    )
                device = self.devices[device_id]
                try:
                    if bool(getattr(device, "enforces_timeouts", False)):
                        return await callback()
                    return await asyncio.wait_for(callback(), timeout=timeout)
                except TimeoutError as exc:
                    if not bool(getattr(device, "enforces_timeouts", False)):
                        self._unavailable_after_timeout[device_id] = operation
                    raise DeviceError(
                        f"{config.display_name} {operation} timed out after "
                        f"{timeout:g} seconds",
                        "DEVICE_OPERATION_TIMEOUT",
                        operation,
                    ) from exc
                except DeviceError as exc:
                    if (
                        exc.code == "DEVICE_OPERATION_TIMEOUT"
                        and not bool(
                            getattr(device, "enforces_timeouts", False)
                        )
                    ):
                        self._unavailable_after_timeout[device_id] = operation
                    raise

        return await serialized()

    async def connect_all(self) -> None:
        async def connect(device_id: str, device: object) -> None:
            try:
                await self._operate(device_id, "connect", device.connect)
                self._connection_states[device_id] = (
                    DeviceConnectionState.CONNECTED
                )
                self.events.resolve(device_id, "CONNECT_FAILED")
                self.events.report(Severity.INFO, device_id, "CONNECTED", "Device connected")
            except DeviceError as exc:
                if self.isolate_processes:
                    self._begin_recovery(device_id, exc)
                    return
                self._connection_states[device_id] = (
                    DeviceConnectionState.FAULTED
                )
                self.events.report(
                    Severity.ERROR,
                    device_id,
                    exc.code,
                    str(exc),
                    exc.context,
                )
            except Exception as exc:
                self._connection_states[device_id] = (
                    DeviceConnectionState.FAULTED
                )
                self.events.report(
                    Severity.ERROR,
                    device_id,
                    "CONNECT_FAILED",
                    str(exc),
                )

        await asyncio.gather(
            *(connect(device_id, device) for device_id, device in self.devices.items())
        )

    async def disconnect_all(self) -> None:
        self._shutting_down = True
        self._generation = {
            device_id: generation + 1
            for device_id, generation in self._generation.items()
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

        async def disconnect(device_id: str, device: object) -> None:
            try:
                await self._operate(
                    device_id,
                    "disconnect",
                    device.disconnect,
                    shutdown=True,
                )
            except Exception as exc:
                self.events.report(
                    Severity.WARNING,
                    device_id,
                    getattr(exc, "code", "DISCONNECT_FAILED"),
                    str(exc),
                    getattr(exc, "context", ""),
                )
            finally:
                try:
                    await device.close()  # type: ignore[attr-defined]
                except Exception as exc:
                    self.events.report(
                        Severity.WARNING,
                        device_id,
                        "DEVICE_WORKER_CLOSE_FAILED",
                        str(exc),
                    )
                self._connection_states[device_id] = (
                    DeviceConnectionState.DISCONNECTED
                )
                self._mark_snapshot_unavailable(
                    device_id,
                    DeviceConnectionState.DISCONNECTED,
                    "Device disconnected",
                )

        await asyncio.gather(
            *(disconnect(device_id, device) for device_id, device in self.devices.items())
        )

    async def poll_all(self) -> dict[str, DeviceSnapshot]:
        device_ids = tuple(
            device_id
            for device_id in self.devices
            if self._connection_states[device_id]
            is DeviceConnectionState.CONNECTED
        )
        results = await asyncio.gather(
            *(self._poll_one(device_id) for device_id in device_ids),
            return_exceptions=True,
        )
        now = time.monotonic()
        for device_id, result in zip(device_ids, results, strict=True):
            if isinstance(result, Exception):
                if (
                    self.isolate_processes
                    and isinstance(result, DeviceError)
                    and not isinstance(result, DeviceWarning)
                    and self._recoverable_read_error(result)
                ):
                    self._begin_recovery(device_id, result)
                    continue
                severity = Severity.WARNING if isinstance(result, DeviceWarning) else Severity.ERROR
                code = getattr(result, "code", "POLL_FAILED")
                context = getattr(result, "context", "")
                self._poll_issues[device_id].add((code, context))
                self.events.report(severity, device_id, code, str(result), context)
                if (
                    not isinstance(result, DeviceWarning)
                    and isinstance(result, DeviceError)
                    and not self._recoverable_read_error(result)
                ):
                    self._connection_states[device_id] = (
                        DeviceConnectionState.FAULTED
                    )
                    self._mark_snapshot_unavailable(
                        device_id,
                        DeviceConnectionState.FAULTED,
                        str(result),
                    )
            else:
                self.events.resolve(device_id, "POLL_FAILED")
                for code, context in self._poll_issues[device_id]:
                    self.events.resolve(device_id, code, context)
                self._poll_issues[device_id].clear()
            self._update_stale_state(
                device_id,
                now,
                poll_succeeded=not isinstance(result, Exception),
            )
        return deepcopy(self.latest)

    async def _poll_one(self, device_id: str) -> DeviceSnapshot:
        async def poll() -> DeviceSnapshot:
            snapshot = await self.devices[device_id].poll()  # type: ignore[attr-defined]
            self._validate_snapshot(device_id, snapshot)
            snapshot.connected = True
            snapshot.connection_state = DeviceConnectionState.CONNECTED
            self._connection_states[device_id] = (
                DeviceConnectionState.CONNECTED
            )
            if device_id not in self._expected_targets:
                self._expected_targets[device_id] = snapshot.target
                self._expected_rates[device_id] = snapshot.rate_per_minute
            evaluator = self._stability.get(device_id)
            if evaluator is not None and snapshot.current is not None and snapshot.target is not None:
                result = evaluator.update(snapshot.current, snapshot.target, snapshot.timestamp)
                snapshot.stability = result.state
                timeout_code = "STABILITY_TIMEOUT"
                if result.state is StabilityState.TIMED_OUT:
                    self.events.report(
                        self.config.alarms.stability_timeout,
                        device_id,
                        timeout_code,
                        f"{snapshot.display_name} did not stabilize within {result.elapsed_seconds:.1f} seconds",
                    )
                else:
                    self.events.resolve(device_id, timeout_code)
            # Publish while the device lock is still held. Otherwise an older
            # concurrent poll can overwrite the target just written by set_target().
            self.latest[device_id] = snapshot
            return snapshot

        return await self._operate(device_id, "poll", poll)

    def _validate_snapshot(
        self,
        device_id: str,
        snapshot: DeviceSnapshot,
    ) -> None:
        config = self.device_configs[device_id]
        if snapshot.device_id != device_id or snapshot.kind is not config.kind:
            raise DeviceError(
                f"{config.display_name} returned a snapshot for the wrong device or kind",
                "INVALID_DEVICE_SNAPSHOT",
                device_id,
            )
        if not snapshot.connected:
            raise DeviceError(
                f"{config.display_name} reported that it is disconnected",
                "DEVICE_REPORTED_DISCONNECTED",
                device_id,
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
            raise DeviceError(
                f"{config.display_name} returned non-finite {', '.join(invalid)}",
                "NONFINITE_DEVICE_READING",
                device_id,
            )

    def _update_stale_state(
        self,
        device_id: str,
        now: float,
        *,
        poll_succeeded: bool,
    ) -> None:
        snapshot = self.latest.get(device_id)
        if snapshot is None:
            return
        config = self.device_configs[device_id]
        age = max(0.0, now - snapshot.timestamp)
        stale = age > config.stale_after_seconds
        if stale:
            snapshot.stability = StabilityState.STALE
            snapshot.message = (
                f"Reading is stale ({age:.1f} s old; "
                f"limit {config.stale_after_seconds:g} s)"
            )
            first_occurrence = device_id not in self._stale_devices
            if first_occurrence:
                self._stale_devices.add(device_id)
            if (
                first_occurrence
                or self.config.alarms.stale_reading is not Severity.INFO
            ):
                self.events.report(
                    self.config.alarms.stale_reading,
                    device_id,
                    "STALE_READING",
                    snapshot.message,
                    device_id,
                )
        elif poll_succeeded and device_id in self._stale_devices:
            self._stale_devices.remove(device_id)
            self.events.resolve(device_id, "STALE_READING", device_id)

    def first_device_id(self, kind: DeviceKind) -> str:
        for config in self.config.devices:
            if (
                config.kind is kind
                and config.role is DeviceRole.PRIMARY
                and config.control_enabled
            ):
                return config.id
        raise DeviceError(
            f"No controllable primary {kind.value} device is configured",
            "DEVICE_NOT_CONFIGURED",
            kind.value,
        )

    def resolve_device_id(
        self,
        kind: DeviceKind,
        requested: object | None = None,
    ) -> str:
        candidate = str(requested or "").strip()
        if not candidate or (
            candidate == kind.value and candidate not in self.device_configs
        ):
            candidate = self.first_device_id(kind)
        config = self.device_configs.get(candidate)
        if config is None:
            raise DeviceError(
                f"Unknown {kind.value} device: {candidate}",
                "UNKNOWN_DEVICE",
                candidate,
            )
        if config.kind is not kind:
            raise DeviceError(
                f"Device {candidate} is {config.kind.value}, not {kind.value}",
                "DEVICE_KIND_MISMATCH",
                candidate,
            )
        if not config.control_enabled:
            raise DeviceError(
                f"Device {candidate} is read-only and cannot be used by control commands",
                "DEVICE_READ_ONLY",
                candidate,
            )
        return candidate

    def validate_target(self, device_id: str, value: float, rate_per_minute: float) -> None:
        config = self.device_configs[device_id]
        if (
            config.kind not in (DeviceKind.TEMPERATURE, DeviceKind.FIELD)
            or not config.control_enabled
        ):
            raise DeviceError(
                f"{config.display_name} is display-only and cannot accept a target",
                "TARGET_NOT_CONTROLLABLE",
                device_id,
            )
        if not math.isfinite(value):
            raise SafetyViolation(
                f"{config.display_name} target must be finite",
                "TARGET_NOT_FINITE",
                device_id,
            )
        if not math.isfinite(rate_per_minute):
            raise SafetyViolation(
                f"{config.display_name} rate must be finite",
                "RATE_NOT_FINITE",
                device_id,
            )
        if not config.min_value <= value <= config.max_value:
            raise SafetyViolation(
                f"{config.display_name} target {value:g} {config.unit} is outside the allowed range "
                f"[{config.min_value:g}, {config.max_value:g}] {config.unit}",
                "TARGET_OUT_OF_RANGE",
                device_id,
            )
        if rate_per_minute <= 0 or rate_per_minute > config.max_rate_per_minute:
            raise SafetyViolation(
                f"{config.display_name} rate {rate_per_minute:g} {config.unit}/min is outside the allowed range "
                f"(0, {config.max_rate_per_minute:g}]",
                "RATE_OUT_OF_RANGE",
                device_id,
            )

    async def set_target(
        self,
        device_id: str,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        origin: str = "sequence",
    ) -> bool:
        try:
            self.validate_target(device_id, value, rate_per_minute)
            await self._operate(
                device_id,
                "set_target",
                lambda: self.devices[device_id].set_target(
                    value,
                    rate_per_minute,
                    mode,
                ),
                origin=origin,
            )
        except DeviceWarning as exc:
            self.events.report(Severity.WARNING, device_id, exc.code, str(exc), exc.context)
            return False
        except DeviceError as exc:
            if self._uncertain_write_error(exc):
                await self._fault_uncertain_write(
                    device_id,
                    "set_target",
                    exc,
                )
                raise DeviceError(
                    f"{self.device_configs[device_id].display_name} set_target "
                    "could not be confirmed and was not replayed",
                    "DEVICE_WRITE_RESULT_UNKNOWN",
                    device_id,
                ) from exc
            self.events.report(Severity.ERROR, device_id, exc.code, str(exc), exc.context)
            raise
        self._expected_targets[device_id] = value
        self._expected_rates[device_id] = rate_per_minute
        snapshot = self.latest.get(device_id)
        if snapshot is not None:
            snapshot.target = value
            snapshot.rate_per_minute = rate_per_minute
            snapshot.stability = StabilityState.MOVING
        evaluator = self._stability.get(device_id)
        if evaluator is not None:
            evaluator.reset(value, time.monotonic())
        self.events.resolve(device_id, "TARGET_OUT_OF_RANGE", device_id)
        self.events.resolve(device_id, "RATE_OUT_OF_RANGE", device_id)
        return True

    async def set_target_by_kind(
        self,
        kind: DeviceKind,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        device_id: str | None = None,
        *,
        origin: str = "sequence",
    ) -> bool:
        selected = self.resolve_device_id(kind, device_id)
        return await self.set_target(
            selected,
            value,
            rate_per_minute,
            mode,
            origin=origin,
        )

    async def hold_all(self) -> bool:
        async def hold(device_id: str) -> bool:
            config = self.device_configs[device_id]
            if not config.control_enabled:
                return True
            if config.kind is DeviceKind.TEMPERATURE:
                strategy = self.config.abort_temperature
            elif config.kind is DeviceKind.FIELD:
                strategy = self.config.abort_field
            else:
                return True
            if strategy != "hold_current":
                self.events.report(
                    Severity.WARNING,
                    "runtime",
                    "UNKNOWN_ABORT_STRATEGY",
                    f"Unknown abort strategy {strategy}; using hold_current",
                    device_id,
                )
            if (
                self._connection_states[device_id]
                is not DeviceConnectionState.CONNECTED
            ):
                state = self._connection_states[device_id]
                self.events.report(
                    Severity.ERROR,
                    device_id,
                    "HOLD_UNCONFIRMED",
                    f"{config.display_name} is {state.value}; Hold Current "
                    "could not be sent or confirmed",
                    device_id,
                )
                return False
            try:
                await self._operate(
                    device_id,
                    "hold",
                    lambda: self.devices[device_id].hold(),  # type: ignore[attr-defined]
                )
                snapshot = self.latest.get(device_id)
                if snapshot is not None:
                    self._expected_targets[device_id] = snapshot.current
                    self._expected_rates[device_id] = (
                        snapshot.rate_per_minute
                    )
                    snapshot.target = snapshot.current
                    snapshot.activity = DeviceActivity.HOLDING
                return True
            except DeviceError as exc:
                if self._uncertain_write_error(exc):
                    await self._fault_uncertain_write(
                        device_id,
                        "hold",
                        exc,
                    )
                    return False
                self.events.report(
                    Severity.ERROR,
                    device_id,
                    exc.code,
                    str(exc),
                    exc.context,
                )
                return False
            except Exception as exc:
                self.events.report(
                    Severity.ERROR,
                    device_id,
                    getattr(exc, "code", "HOLD_FAILED"),
                    str(exc),
                    getattr(exc, "context", ""),
                )
                return False

        results = await asyncio.gather(
            *(hold(device_id) for device_id in self.devices)
        )
        return all(results)

    async def hold_device(
        self,
        device_id: str,
        *,
        origin: str = "manual",
    ) -> None:
        if device_id not in self.devices:
            raise DeviceError(f"Unknown device: {device_id}", "UNKNOWN_DEVICE", device_id)
        if not self.device_configs[device_id].control_enabled:
            raise DeviceError(
                f"{self.device_configs[device_id].display_name} is read-only",
                "DEVICE_READ_ONLY",
                device_id,
            )
        try:
            await self._operate(
                device_id,
                "hold",
                lambda: self.devices[device_id].hold(),  # type: ignore[attr-defined]
                origin=origin,
            )
        except (DeviceError, DeviceWarning) as exc:
            if (
                isinstance(exc, DeviceError)
                and not isinstance(exc, DeviceWarning)
                and self._uncertain_write_error(exc)
            ):
                await self._fault_uncertain_write(
                    device_id,
                    "hold",
                    exc,
                )
                raise DeviceError(
                    f"{self.device_configs[device_id].display_name} hold "
                    "could not be confirmed and was not replayed",
                    "DEVICE_WRITE_RESULT_UNKNOWN",
                    device_id,
                ) from exc
            severity = Severity.WARNING if isinstance(exc, DeviceWarning) else Severity.ERROR
            self.events.report(severity, device_id, exc.code, str(exc), exc.context)
            raise
        snapshot = self.latest.get(device_id)
        if snapshot is not None:
            self._expected_targets[device_id] = snapshot.current
            self._expected_rates[device_id] = snapshot.rate_per_minute
            snapshot.target = snapshot.current
            snapshot.activity = DeviceActivity.HOLDING

    def acquire_sequence_control(self) -> None:
        if self._control_owner is not None:
            raise DeviceError(
                f"Device control is already owned by {self._control_owner}",
                "DEVICE_CONTROL_BUSY",
                self._control_owner,
            )
        self._control_owner = "sequence"

    def release_sequence_control(self) -> None:
        if self._control_owner == "sequence":
            self._control_owner = None

    def snapshots(self) -> dict[str, DeviceSnapshot]:
        return deepcopy(self.latest)
