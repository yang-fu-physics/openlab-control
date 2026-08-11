from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.devices.base import DeviceError, SafetyViolation  # noqa: E402
from labcontrol.devices.worker import (  # noqa: E402
    DeviceWorkerError,
    IsolatedDeviceClient,
    _snapshot_payload,
    snapshot_from_payload,
)
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.models import (  # noqa: E402
    DeviceKind,
    DeviceMetric,
    DeviceSnapshot,
)
from labcontrol.plugins import DeviceManager  # noqa: E402


class _FreshDevices:
    def __init__(self) -> None:
        self.config = load_config(ROOT / "configs" / "default.toml")
        self.device_configs = {
            item.id: item for item in self.config.devices
        }
        self.poll_calls = 0
        self._current = 1.0
        self._snapshots = {
            "temperature": DeviceSnapshot(
                "temperature",
                "Temperature",
                DeviceKind.TEMPERATURE,
                time.monotonic(),
                True,
                "K",
                self._current,
                metrics=(
                    DeviceMetric("second_stage", "2nd Stage", 20.0, "K", 3),
                ),
            )
        }

    async def poll_all(self) -> dict[str, DeviceSnapshot]:
        self.poll_calls += 1
        await asyncio.sleep(0.01)
        self._current += 1.0
        self._snapshots["temperature"].current = self._current
        self._snapshots["temperature"].timestamp = time.monotonic()
        return self.snapshots()

    def snapshots(self) -> dict[str, DeviceSnapshot]:
        return deepcopy(self._snapshots)


class DeviceMetricTests(unittest.TestCase):
    def test_worker_snapshot_round_trip_preserves_metrics_and_instrument_flag(
        self,
    ) -> None:
        original = DeviceSnapshot(
            "temperature",
            "Temperature",
            DeviceKind.TEMPERATURE,
            123.0,
            True,
            "K",
            4.2,
            instrument_stable=False,
            metrics=(
                DeviceMetric("heater_output", "Heater", 12.5, "%", 2),
                DeviceMetric("heater_range", "Range", "LOW"),
            ),
        )

        restored = snapshot_from_payload(_snapshot_payload(original))

        self.assertFalse(restored.instrument_stable)
        self.assertEqual(restored.metrics, original.metrics)

        original.metrics = (  # type: ignore[assignment]
            DeviceMetric("bad", "Bad", float("nan")),
        )
        with self.assertRaises(DeviceError) as captured:
            _snapshot_payload(original)
        self.assertEqual(
            captured.exception.code,
            "NONFINITE_DEVICE_READING",
        )

    def test_concurrent_module_samples_share_one_immediate_device_poll(self) -> None:
        async def scenario() -> None:
            devices = _FreshDevices()
            service = MeasurementModuleService(
                (),
                EventManager(),
                devices,  # type: ignore[arg-type]
            )

            first, second = await asyncio.gather(
                service._fresh_system_payload(),
                service._fresh_system_payload(),
            )

            self.assertEqual(devices.poll_calls, 1)
            self.assertEqual(first["temperature"]["current"], 2.0)
            self.assertEqual(second["temperature"]["current"], 2.0)
            self.assertEqual(
                first["temperature"]["metrics"]["second_stage"]["value"],
                20.0,
            )

        asyncio.run(scenario())

    def test_measure_row_forces_fresh_sample_but_reuses_very_recent_context(
        self,
    ) -> None:
        class Logger:
            def __init__(self) -> None:
                self.current: float | None = None

            def write_system_row(self, snapshots, _sequence_step) -> None:
                self.current = snapshots["temperature"].current

        async def scenario() -> None:
            devices = _FreshDevices()
            service = MeasurementModuleService(
                (),
                EventManager(),
                devices,  # type: ignore[arg-type]
            )
            logger = Logger()

            await service.measure_all(logger, "Measure")  # type: ignore[arg-type]
            self.assertEqual(devices.poll_calls, 1)
            self.assertEqual(logger.current, 2.0)

            await service._fresh_system_payload()
            self.assertEqual(devices.poll_calls, 2)
            await service._fresh_system_payload(
                reuse_within_seconds=0.1,
            )
            self.assertEqual(devices.poll_calls, 2)

        asyncio.run(scenario())

    def test_safety_faults_do_not_enter_reconnect_and_unknown_writes_do(self) -> None:
        translated = IsolatedDeviceClient._translate(
            DeviceWorkerError(
                "sensor fault",
                "SENSOR_FAULT",
                "A",
                "safety",
            )
        )
        self.assertIsInstance(translated, SafetyViolation)
        self.assertFalse(
            DeviceManager._recoverable_read_error(
                translated
            )
        )
        self.assertTrue(
            DeviceManager._uncertain_write_error(
                DeviceError("unknown", "DEVICE_WRITE_RESULT_UNKNOWN")
            )
        )

    def test_metric_schema_is_validated_and_hold_path_remains_independent(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            manager = DeviceManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            await manager.connect_all()
            await manager.poll_all()
            await manager.hold_device("temperature")
            await manager.disconnect_all()

            device = config.devices[0]
            valid = DeviceSnapshot(
                device.id,
                device.display_name,
                device.kind,
                time.monotonic(),
                True,
                device.unit,
                4.2,
                metrics=(
                    DeviceMetric("heater", "Heater", 1.0, "%", 2),
                ),
            )
            manager._metric_schemas.pop(device.id, None)
            manager._validate_snapshot(device.id, valid)
            invalid = deepcopy(valid)
            invalid.metrics = (
                DeviceMetric("heater", "Heater", 1.0, "%", 2),
                DeviceMetric("heater", "Duplicate", 2.0, "%", 2),
            )
            with self.assertRaises(DeviceError):
                manager._validate_snapshot(device.id, invalid)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
