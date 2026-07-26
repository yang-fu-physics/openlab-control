from __future__ import annotations

import asyncio
import json
import math
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
    descriptor: ModuleDescriptor
    enabled: bool = False
    state: str = "disabled"
    status: dict[str, Any] = field(default_factory=dict)
    client: ModuleWorkerClient | None = None


class MeasurementModuleService:
    """Coordinates enabled measurement workers from the runtime event loop."""

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
        if not descriptor.can_enable:
            raise DeviceError(
                descriptor.error or descriptor.dependency_error,
                "MODULE_NOT_ENABLEABLE",
                descriptor.id,
            )
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
        payload: dict[str, dict[str, Any]] = {}
        for device_id, snapshot in self.devices.snapshots().items():
            payload[device_id] = {
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
            }
        return payload

    async def _worker_event(self, module_id: str, message: dict[str, Any]) -> None:
        record = self.records[module_id]
        kind = str(message.get("type", ""))
        if kind == "status":
            values = dict(message.get("values", {}))
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
        if record.client is None:
            raise WorkerRequestError("Module worker is unavailable", "MODULE_WORKER_NOT_RUNNING")
        loop = asyncio.get_running_loop()
        timeout = (
            self.config.operation_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        deadline = time.monotonic() + timeout

        def on_event(message: dict[str, Any]) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._worker_event(record.descriptor.id, message), loop
            )
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Module event processing timed out")
                future.result(timeout=remaining)
            except Exception:
                future.cancel()
                raise

        request_payload = dict(payload or {})
        request_payload["system"] = self._system_payload()
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
            record.client = None
            record.enabled = False
            record.state = "faulted"
            self._publish(record, str(error))
        if warning_allowed and severity is Severity.WARNING:
            return None
        return DeviceError(str(error), error.code, error.context)

    async def enable(self, module_id: str, settings: Mapping[str, Any]) -> None:
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
        record.status.update(result)
        record.enabled = True
        record.state = "enabled"
        self.events.resolve_source(f"module:{module_id}")
        self._publish(record, f"{record.descriptor.name} enabled")

    async def disable(self, module_id: str) -> None:
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
        descriptors = self.enabled_descriptors()
        self._sequence_active = True
        self._sequence_modules = tuple(item.id for item in descriptors)
        statuses: dict[str, dict[str, Any]] = {}
        for descriptor in descriptors:
            try:
                statuses[descriptor.id] = await self.refresh_status(descriptor.id)
            except DeviceError:
                # The Error event stops the run, but returning the most recent
                # status still lets the run folder capture a diagnostic snapshot.
                statuses[descriptor.id] = deepcopy(self.records[descriptor.id].status)
        return descriptors, statuses

    async def begin_sequence(self) -> None:
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

        failures = await asyncio.gather(*(measure_one(item) for item in self._sequence_modules))
        if emitted == 0:
            logger.write_system_row(self.devices.snapshots(), sequence_step)
        first = next((item for item in failures if item is not None), None)
        if first is not None:
            raise first

    async def end_sequence(self, reason: str) -> bool:
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
            results = await asyncio.gather(*(end(item) for item in self._sequence_modules))
            return all(results)
        finally:
            self._sequence_modules = ()
            self._sequence_active = False

    async def shutdown(self) -> None:
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
