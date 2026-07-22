from __future__ import annotations

import asyncio
import importlib
import time
from copy import deepcopy
from typing import TypeVar

from .config import AppConfig, DeviceConfig
from .devices.base import DeviceError, DevicePlugin, DeviceWarning, SafetyViolation
from .events import EventManager
from .models import DeviceKind, DeviceSnapshot, Severity, StabilityState
from .stability import StabilityEvaluator


T = TypeVar("T")


def load_object(specification: str) -> object:
    try:
        module_name, object_name = specification.split(":", 1)
    except ValueError as exc:
        raise ValueError("Plugin path must use package.module:ClassName format") from exc
    module = importlib.import_module(module_name)
    return getattr(module, object_name)


class DeviceManager:
    def __init__(self, config: AppConfig, events: EventManager) -> None:
        self.config = config
        self.events = events
        self.devices: dict[str, DevicePlugin] = {}
        self.device_configs: dict[str, DeviceConfig] = {item.id: item for item in config.devices}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stability: dict[str, StabilityEvaluator] = {}
        self._poll_issues: dict[str, set[tuple[str, str]]] = {}
        self.latest: dict[str, DeviceSnapshot] = {}
        self._load_plugins()

    def _load_plugins(self) -> None:
        for device_config in self.config.devices:
            plugin_class = load_object(device_config.plugin)
            if not isinstance(plugin_class, type) or not issubclass(plugin_class, DevicePlugin):
                raise TypeError(f"{device_config.plugin} is not a DevicePlugin")
            self.devices[device_config.id] = plugin_class(
                device_config, simulation_speed=self.config.simulation_speed
            )
            self._locks[device_config.id] = asyncio.Lock()
            self._poll_issues[device_config.id] = set()
            if device_config.stability is not None:
                self._stability[device_config.id] = StabilityEvaluator(device_config.stability)

    async def connect_all(self) -> None:
        for device_id, device in self.devices.items():
            try:
                async with self._locks[device_id]:
                    await device.connect()
                self.events.resolve(device_id, "CONNECT_FAILED")
                self.events.report(Severity.INFO, device_id, "CONNECTED", "Device connected")
            except Exception as exc:
                self.events.report(Severity.ERROR, device_id, "CONNECT_FAILED", str(exc))

    async def disconnect_all(self) -> None:
        for device_id, device in self.devices.items():
            try:
                async with self._locks[device_id]:
                    await device.disconnect()
            except Exception as exc:
                self.events.report(Severity.WARNING, device_id, "DISCONNECT_FAILED", str(exc))

    async def poll_all(self) -> dict[str, DeviceSnapshot]:
        results = await asyncio.gather(
            *(self._poll_one(device_id) for device_id in self.devices),
            return_exceptions=True,
        )
        for device_id, result in zip(self.devices, results, strict=True):
            if isinstance(result, Exception):
                severity = Severity.WARNING if isinstance(result, DeviceWarning) else Severity.ERROR
                code = getattr(result, "code", "POLL_FAILED")
                context = getattr(result, "context", "")
                self._poll_issues[device_id].add((code, context))
                self.events.report(severity, device_id, code, str(result), context)
            else:
                self.events.resolve(device_id, "POLL_FAILED")
                for code, context in self._poll_issues[device_id]:
                    self.events.resolve(device_id, code, context)
                self._poll_issues[device_id].clear()
        return deepcopy(self.latest)

    async def _poll_one(self, device_id: str) -> DeviceSnapshot:
        device = self.devices[device_id]
        async with self._locks[device_id]:
            snapshot = await device.poll()
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

    def first_device_id(self, kind: DeviceKind) -> str:
        for config in self.config.devices:
            if config.kind is kind:
                return config.id
        raise DeviceError(f"No {kind.value} device is configured", "DEVICE_NOT_CONFIGURED", kind.value)

    def validate_target(self, device_id: str, value: float, rate_per_minute: float) -> None:
        config = self.device_configs[device_id]
        if config.kind not in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
            raise DeviceError(
                f"{config.display_name} is display-only and cannot accept a target",
                "TARGET_NOT_CONTROLLABLE",
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
    ) -> None:
        try:
            self.validate_target(device_id, value, rate_per_minute)
            async with self._locks[device_id]:
                await self.devices[device_id].set_target(value, rate_per_minute, mode)
        except DeviceWarning as exc:
            self.events.report(Severity.WARNING, device_id, exc.code, str(exc), exc.context)
        except DeviceError as exc:
            self.events.report(Severity.ERROR, device_id, exc.code, str(exc), exc.context)
            raise
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

    async def set_target_by_kind(
        self,
        kind: DeviceKind,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        device_id: str | None = None,
    ) -> str:
        selected = device_id or self.first_device_id(kind)
        await self.set_target(selected, value, rate_per_minute, mode)
        return selected

    async def hold_all(self) -> None:
        for device_id, device in self.devices.items():
            config = self.device_configs[device_id]
            if config.kind is DeviceKind.TEMPERATURE:
                strategy = self.config.abort_temperature
            elif config.kind is DeviceKind.FIELD:
                strategy = self.config.abort_field
            else:
                continue
            if strategy == "keep_target":
                continue
            if strategy != "hold_current":
                self.events.report(
                    Severity.WARNING,
                    "runtime",
                    "UNKNOWN_ABORT_STRATEGY",
                    f"Unknown abort strategy {strategy}; using hold_current",
                    device_id,
                )
            try:
                async with self._locks[device_id]:
                    await device.hold()
            except DeviceError as exc:
                self.events.report(Severity.ERROR, device_id, exc.code, str(exc), exc.context)

    async def hold_device(self, device_id: str) -> None:
        if device_id not in self.devices:
            raise DeviceError(f"Unknown device: {device_id}", "UNKNOWN_DEVICE", device_id)
        try:
            async with self._locks[device_id]:
                await self.devices[device_id].hold()
        except DeviceError as exc:
            self.events.report(Severity.ERROR, device_id, exc.code, str(exc), exc.context)
            raise

    def snapshots(self) -> dict[str, DeviceSnapshot]:
        return deepcopy(self.latest)
