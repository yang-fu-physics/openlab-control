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
from labcontrol.instruments.base import InstrumentError, SafetyViolation  # noqa: E402
from labcontrol.instruments.worker import (  # noqa: E402
    InstrumentWorkerError,
    IsolatedInstrumentClient,
    _snapshot_payload,
    snapshot_from_payload,
)
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.models import (  # noqa: E402
    InstrumentKind,
    InstrumentMetric,
    InstrumentSnapshot,
)
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402


class _FreshInstruments:
    def __init__(self) -> None:
        self.config = load_config(ROOT / "configs" / "default.toml")
        self.instrument_configs = {
            item.id: item for item in self.config.instruments
        }
        self.poll_calls = 0
        self.full_poll_calls = 0
        self.measurement_poll_calls = 0
        self._current = 1.0
        self._snapshots = {
            "temperature": InstrumentSnapshot(
                "temperature",
                "Temperature",
                InstrumentKind.TEMPERATURE,
                time.monotonic(),
                True,
                "K",
                self._current,
                metrics={
                    "second_stage": InstrumentMetric(
                        "2nd Stage", 20.0, "K", 3
                    ),
                },
            )
        }

    async def poll_all(self) -> dict[str, InstrumentSnapshot]:
        self.full_poll_calls += 1
        return await self._advance()

    async def poll_measurement_all(self) -> dict[str, InstrumentSnapshot]:
        self.measurement_poll_calls += 1
        return await self._advance()

    async def _advance(self) -> dict[str, InstrumentSnapshot]:
        self.poll_calls += 1
        await asyncio.sleep(0.01)
        self._current += 1.0
        self._snapshots["temperature"].current = self._current
        self._snapshots["temperature"].timestamp = time.monotonic()
        return self.snapshots()

    def snapshots(self) -> dict[str, InstrumentSnapshot]:
        return deepcopy(self._snapshots)


class InstrumentMetricTests(unittest.TestCase):
    def test_worker_snapshot_round_trip_preserves_metrics_and_instrument_flag(
        self,
    ) -> None:
        original = InstrumentSnapshot(
            "temperature",
            "Temperature",
            InstrumentKind.TEMPERATURE,
            123.0,
            True,
            "K",
            4.2,
            instrument_stable=False,
            metrics={
                "heater_output": InstrumentMetric(
                    "Heater", 12.5, "%", 2
                ),
                "heater_range": InstrumentMetric("Range", "LOW"),
            },
        )

        restored = snapshot_from_payload(_snapshot_payload(original))

        self.assertFalse(restored.instrument_stable)
        self.assertEqual(restored.metrics, original.metrics)

        original.metrics = {
            "bad": InstrumentMetric("Bad", float("nan")),
        }
        with self.assertRaises(InstrumentError) as captured:
            _snapshot_payload(original)
        self.assertEqual(
            captured.exception.code,
            "NONFINITE_INSTRUMENT_READING",
        )

    def test_concurrent_module_samples_share_one_immediate_instrument_poll(self) -> None:
        async def scenario() -> None:
            instruments = _FreshInstruments()
            service = MeasurementModuleService(
                (),
                EventManager(),
                instruments,  # type: ignore[arg-type]
            )

            first, second = await asyncio.gather(
                service._fresh_system_payload(),
                service._fresh_system_payload(),
            )

            self.assertEqual(instruments.poll_calls, 1)
            self.assertEqual(instruments.full_poll_calls, 0)
            self.assertEqual(instruments.measurement_poll_calls, 1)
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
            instruments = _FreshInstruments()
            service = MeasurementModuleService(
                (),
                EventManager(),
                instruments,  # type: ignore[arg-type]
            )
            logger = Logger()

            await service.measure_all(logger, "Measure")  # type: ignore[arg-type]
            self.assertEqual(instruments.poll_calls, 1)
            self.assertEqual(instruments.full_poll_calls, 0)
            self.assertEqual(instruments.measurement_poll_calls, 1)
            self.assertEqual(logger.current, 2.0)

            await service._fresh_system_payload()
            self.assertEqual(instruments.poll_calls, 2)
            self.assertEqual(instruments.measurement_poll_calls, 2)
            await service._fresh_system_payload(
                reuse_within_seconds=0.1,
            )
            self.assertEqual(instruments.poll_calls, 2)

        asyncio.run(scenario())

    def test_safety_faults_do_not_enter_reconnect_and_unknown_writes_do(self) -> None:
        translated = IsolatedInstrumentClient._translate(
            InstrumentWorkerError(
                "sensor fault",
                "SENSOR_FAULT",
                "A",
                "safety",
            )
        )
        self.assertIsInstance(translated, SafetyViolation)
        self.assertFalse(
            InstrumentManager._recoverable_read_error(
                translated
            )
        )
        self.assertTrue(
            InstrumentManager._uncertain_write_error(
                InstrumentError("unknown", "INSTRUMENT_WRITE_RESULT_UNKNOWN")
            )
        )

    def test_metric_schema_is_validated_and_hold_path_remains_independent(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            await manager.connect_all()
            await manager.poll_all()
            await manager.hold_instrument("temperature")
            await manager.disconnect_all()

            instrument = config.instruments[0]
            valid = InstrumentSnapshot(
                instrument.id,
                instrument.display_name,
                instrument.kind,
                time.monotonic(),
                True,
                instrument.unit,
                4.2,
                metrics={
                    "heater": InstrumentMetric("Heater", 1.0, "%", 2),
                },
            )
            manager._metric_schemas.pop(instrument.id, None)
            manager._validate_snapshot(instrument.id, valid)
            measurement = deepcopy(valid)
            measurement.metrics = {
                "heater": InstrumentMetric("Heater", None, "%", 2),
            }
            manager._validate_snapshot(instrument.id, measurement)
            invalid = deepcopy(valid)
            invalid.metrics = {
                "not valid": InstrumentMetric("Heater", 1.0, "%", 2),
            }
            with self.assertRaises(InstrumentError):
                manager._validate_snapshot(instrument.id, invalid)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
