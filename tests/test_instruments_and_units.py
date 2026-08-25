from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import ConfigurationError, load_config  # noqa: E402
from labcontrol.instruments.base import InstrumentError, InstrumentWarning, SafetyViolation  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.formatting import fixed_number  # noqa: E402
from labcontrol.models import InstrumentKind, StabilityState  # noqa: E402
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.units import UnitConversionError, convert_value  # noqa: E402


class InstrumentManagerTests(unittest.TestCase):
    def test_configuration_rejects_removed_abort_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "removed-abort.toml"
            config_path.write_text(
                '[abort]\ntemperature = "hold"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                r"\[abort\] is not supported",
            ):
                load_config(config_path)

    def test_simulated_control_and_monitor_instruments_load(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            snapshots = await manager.poll_all()
            self.assertEqual(len(snapshots), 3)
            self.assertEqual(manager.first_instrument_id(InstrumentKind.TEMPERATURE), "temperature")
            second_stage = snapshots["second_stage"]
            self.assertEqual(second_stage.kind, InstrumentKind.MONITOR)
            self.assertIsNotNone(second_stage.current)
            self.assertIsNone(second_stage.target)
            self.assertNotIn("second_stage", manager._stability)
            with self.assertRaises(InstrumentError) as blocked:
                await manager.set_target("second_stage", 5.0, 1.0)
            self.assertEqual(blocked.exception.code, "TARGET_NOT_CONTROLLABLE")
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_safety_limits_reject_target(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        field = next(instrument for instrument in config.instruments if instrument.id == "field")
        self.assertEqual(field.unit, "Oe")
        self.assertEqual(field.min_value, -90000.0)
        self.assertEqual(field.max_value, 90000.0)
        self.assertEqual(field.default_rate_per_minute, 5000.0)
        manager = InstrumentManager(config, EventManager(), isolate_processes=False)
        with self.assertRaises(SafetyViolation):
            manager.validate_target("field", 200000.0, 5000.0)
        with self.assertRaises(SafetyViolation):
            manager.validate_target("temperature", 300.0, 100.0)
        with self.assertRaises(SafetyViolation):
            manager.validate_target("temperature", 300.0, math.nan)
        with self.assertRaises(SafetyViolation):
            manager.validate_target("temperature", math.inf, 10.0)

    def test_kind_aliases_resolve_custom_ids_and_reject_wrong_kinds(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        custom = replace(
            config,
            instruments=tuple(
                replace(
                    instrument,
                    id={
                        "temperature": "cryostat_primary",
                        "field": "magnet_primary",
                    }.get(instrument.id, instrument.id),
                )
                for instrument in config.instruments
            ),
        )
        manager = InstrumentManager(custom, EventManager(), isolate_processes=False)
        self.assertEqual(
            manager.resolve_instrument_id(InstrumentKind.TEMPERATURE, "temperature"),
            "cryostat_primary",
        )
        self.assertEqual(
            manager.resolve_instrument_id(InstrumentKind.FIELD, "magnet_primary"),
            "magnet_primary",
        )
        with self.assertRaises(InstrumentError) as wrong_kind:
            manager.resolve_instrument_id(InstrumentKind.TEMPERATURE, "magnet_primary")
        self.assertEqual(wrong_kind.exception.code, "INSTRUMENT_KIND_MISMATCH")
        with self.assertRaises(InstrumentError) as missing:
            manager.resolve_instrument_id(InstrumentKind.FIELD, "missing")
        self.assertEqual(missing.exception.code, "UNKNOWN_INSTRUMENT")

    def test_second_instrument_is_read_only_when_control_is_disabled(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            primary = config.instrument("temperature")
            secondary = replace(
                primary,
                id="temperature_backup",
                display_name="Backup Temperature",
                control_enabled=False,
            )
            custom = replace(
                config,
                instruments=(*config.instruments, secondary),
            )
            events = EventManager()
            manager = InstrumentManager(
                custom,
                events,
                isolate_processes=False,
            )
            self.assertEqual(
                manager.first_instrument_id(InstrumentKind.TEMPERATURE),
                "temperature",
            )
            with self.assertRaises(InstrumentError) as read_only:
                manager.resolve_instrument_id(
                    InstrumentKind.TEMPERATURE,
                    "temperature_backup",
                )
            self.assertEqual(read_only.exception.code, "INSTRUMENT_READ_ONLY")
            with self.assertRaises(InstrumentError) as target:
                await manager.set_target("temperature_backup", 10.0, 1.0)
            self.assertEqual(target.exception.code, "TARGET_NOT_CONTROLLABLE")
            with self.assertRaises(InstrumentError) as hold:
                await manager.hold_instrument("temperature_backup")
            self.assertEqual(hold.exception.code, "INSTRUMENT_READ_ONLY")

        asyncio.run(scenario())

    def test_only_one_controller_per_kind_and_missing_control_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = (ROOT / "configs" / "default.toml").read_text(encoding="utf-8")
            secondary_block = """

[[instruments]]
id = "temperature_backup"
display_name = "Backup Temperature"
kind = "temperature"
backend = "labcontrol.instruments.simulated:SimulatedTemperatureController"
unit = "K"
initial_value = 300.0
default_rate_per_minute = 5.0
min_value = 1.8
max_value = 400.0
max_rate_per_minute = 10.0
"""
            valid_path = root / "valid.toml"
            valid_path.write_text(source + secondary_block, encoding="utf-8")
            valid = load_config(valid_path)
            backup = valid.instrument("temperature_backup")
            self.assertFalse(backup.control_enabled)

            duplicate_path = root / "duplicate.toml"
            duplicate_path.write_text(
                source + secondary_block.replace(
                    'unit = "K"',
                    'control_enabled = true\nunit = "K"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "Only one controllable"):
                load_config(duplicate_path)

            implicit_path = root / "implicit.toml"
            implicit_path.write_text(
                "\n".join(
                    line
                    for line in source.splitlines()
                    if not line.startswith("control_enabled = ")
                ),
                encoding="utf-8",
            )
            implicit = load_config(implicit_path)
            self.assertFalse(implicit.instrument("temperature").control_enabled)
            self.assertFalse(implicit.instrument("second_stage").control_enabled)

    def test_sequence_control_lease_blocks_manual_writes_without_fatal_error(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            before = manager.latest["temperature"].target
            manager.acquire_sequence_control()
            try:
                applied = await manager.set_target(
                    "temperature",
                    250.0,
                    5.0,
                    origin="manual",
                )
                self.assertFalse(applied)
                with self.assertRaises(InstrumentWarning) as hold:
                    await manager.hold_instrument("temperature", origin="manual")
                self.assertEqual(hold.exception.code, "MANUAL_CONTROL_BLOCKED")
            finally:
                manager.release_sequence_control()
            self.assertEqual(manager.latest["temperature"].target, before)
            warnings = [
                event
                for event in events.active_events()
                if event.code == "MANUAL_CONTROL_BLOCKED"
            ]
            self.assertTrue(warnings)
            self.assertTrue(all(event.severity.value == "warning" for event in warnings))
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_configuration_rejects_nonfinite_and_nonpositive_safety_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            source = (ROOT / "configs" / "default.toml").read_text(encoding="utf-8")
            invalid_rate = temp_root / "invalid-rate.toml"
            invalid_rate.write_text(
                source.replace(
                    "max_rate_per_minute = 30.0",
                    "max_rate_per_minute = nan",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "finite"):
                load_config(invalid_rate)

            invalid_poll = temp_root / "invalid-poll.toml"
            invalid_poll.write_text(
                source.replace(
                    "poll_interval_seconds = 1.0",
                    "poll_interval_seconds = 0",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "greater than zero"):
                load_config(invalid_poll)

            invalid_control_poll = temp_root / "invalid-control-poll.toml"
            invalid_control_poll.write_text(
                source.replace(
                    "control_poll_interval_seconds = 0.20",
                    "control_poll_interval_seconds = 0",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "greater than zero"):
                load_config(invalid_control_poll)

            invalid_timeout = temp_root / "invalid-timeout.toml"
            invalid_timeout.write_text(
                source.replace(
                    "operation_timeout_seconds = 10.0",
                    "operation_timeout_seconds = 0",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "greater than zero"):
                load_config(invalid_timeout)

    def test_configuration_rejects_unaddressable_instrument_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            source = (ROOT / "configs" / "default.toml").read_text(
                encoding="utf-8"
            )
            invalid_ids = ("", " temperature ", "temperature\\nprimary")
            for index, invalid_id in enumerate(invalid_ids):
                target = temp_root / f"invalid-instrument-id-{index}.toml"
                target.write_text(
                    source.replace(
                        'id = "temperature"',
                        f'id = "{invalid_id}"',
                        1,
                    ),
                    encoding="utf-8",
                )
                with self.subTest(instrument_id=invalid_id):
                    with self.assertRaisesRegex(
                        ConfigurationError,
                        "Instrument id must be",
                    ):
                        load_config(target)

            internal_space = temp_root / "internal-space.toml"
            internal_space.write_text(
                source.replace(
                    'id = "temperature"',
                    'id = "cryostat primary"',
                    1,
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                load_config(internal_space).instruments[0].id,
                "cryostat primary",
            )

    def test_configuration_confines_and_separates_run_log_file_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            source = (ROOT / "configs" / "default.toml").read_text(encoding="utf-8")

            escaped = temp_root / "escaped.toml"
            escaped.write_text(
                source.replace(
                    'data_file_name = "experiment.dat"',
                    'data_file_name = "../outside.dat"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "plain Windows file name"):
                load_config(escaped)

            reserved = temp_root / "reserved.toml"
            reserved.write_text(
                source.replace(
                    'data_file_name = "experiment.dat"',
                    'data_file_name = "CON.dat"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "plain Windows file name"):
                load_config(reserved)

            overlapping = temp_root / "overlapping.toml"
            overlapping.write_text(
                source.replace(
                    'event_file_name = "events.dat"',
                    'event_file_name = "EXPERIMENT.DAT"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "must be different"):
                load_config(overlapping)

            overlapping_status = temp_root / "overlapping-status.toml"
            overlapping_status.write_text(
                source.replace(
                    'instrument_status_file_name = "instrument_status.dat"',
                    'instrument_status_file_name = "EVENTS.DAT"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "must be different",
            ):
                load_config(overlapping_status)

            invalid_interval = temp_root / "invalid-status-interval.toml"
            invalid_interval.write_text(
                source.replace(
                    "instrument_status_interval_seconds = 1.0",
                    "instrument_status_interval_seconds = 0",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "must be greater than zero",
            ):
                load_config(invalid_interval)

    def test_completed_poll_cannot_overwrite_a_new_target(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            await manager.connect_all()
            await manager.poll_all()

            monitor = manager.instruments["second_stage"]
            original_poll = monitor.read_status
            monitor_started = asyncio.Event()
            release_monitor = asyncio.Event()

            async def delayed_monitor_poll():
                monitor_started.set()
                await release_monitor.wait()
                return await original_poll()

            monitor.read_status = delayed_monitor_poll  # type: ignore[method-assign]
            poll_task = asyncio.create_task(manager.poll_all())
            await monitor_started.wait()
            await manager.set_target("field", 100.0, 5000.0)
            release_monitor.set()
            await poll_task
            self.assertEqual(manager.latest["field"].target, 100.0)
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_measurement_poll_precedes_queued_background_poll(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            await manager.connect_all()
            await manager.poll_all()

            instrument_id = "temperature"
            instrument = manager.instruments[instrument_id]
            original_poll = instrument.read_status
            original_poll_measurement = instrument.read_measurement
            first_poll_started = asyncio.Event()
            release_first_poll = asyncio.Event()
            order: list[str] = []
            regular_poll_count = 0
            calls_in_flight = 0
            maximum_calls_in_flight = 0

            async def tracked_poll():
                nonlocal regular_poll_count, calls_in_flight, maximum_calls_in_flight
                regular_poll_count += 1
                call_number = regular_poll_count
                calls_in_flight += 1
                maximum_calls_in_flight = max(maximum_calls_in_flight, calls_in_flight)
                order.append(f"poll-{call_number}-start")
                try:
                    if call_number == 1:
                        first_poll_started.set()
                        await release_first_poll.wait()
                    return await original_poll()
                finally:
                    order.append(f"poll-{call_number}-end")
                    calls_in_flight -= 1

            async def tracked_measurement_poll():
                nonlocal calls_in_flight, maximum_calls_in_flight
                calls_in_flight += 1
                maximum_calls_in_flight = max(maximum_calls_in_flight, calls_in_flight)
                order.append("measurement-start")
                try:
                    return await original_poll_measurement()
                finally:
                    order.append("measurement-end")
                    calls_in_flight -= 1

            instrument.read_status = tracked_poll  # type: ignore[method-assign]
            instrument.read_measurement = tracked_measurement_poll  # type: ignore[method-assign]
            active_poll = asyncio.create_task(manager._poll_one(instrument_id))
            await first_poll_started.wait()

            queued_poll = asyncio.create_task(manager._poll_one(instrument_id))
            gate = manager._operation_gates[instrument_id]
            while len(gate._waiters) < 1:
                await asyncio.sleep(0)
            measurement_poll = asyncio.create_task(
                manager._poll_one(instrument_id, measurement=True)
            )
            while len(gate._waiters) < 2:
                await asyncio.sleep(0)

            release_first_poll.set()
            await asyncio.gather(active_poll, queued_poll, measurement_poll)

            self.assertEqual(
                order,
                [
                    "poll-1-start",
                    "poll-1-end",
                    "measurement-start",
                    "measurement-end",
                    "poll-2-start",
                    "poll-2-end",
                ],
            )
            self.assertEqual(maximum_calls_in_flight, 1)
            instrument.read_status = original_poll  # type: ignore[method-assign]
            instrument.read_measurement = original_poll_measurement  # type: ignore[method-assign]
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_poll_timeout_quarantines_instrument_until_restart(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            notices = []
            events.subscribe_occurrences(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            monitor = manager.instruments["second_stage"]
            original_poll = monitor.read_status
            manager.instrument_configs["second_stage"] = replace(
                manager.instrument_configs["second_stage"],
                operation_timeout_seconds=0.02,
            )

            async def hung_poll():
                await asyncio.sleep(5.0)
                return await original_poll()

            monitor.read_status = hung_poll  # type: ignore[method-assign]
            started = asyncio.get_running_loop().time()
            await manager.poll_all()
            self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
            self.assertEqual(
                manager._unavailable_after_timeout["second_stage"],
                "poll",
            )
            await manager.poll_all()
            codes = [notice.event.code for notice in notices]
            self.assertIn("INSTRUMENT_OPERATION_TIMEOUT", codes)
            self.assertIn("INSTRUMENT_UNAVAILABLE_AFTER_TIMEOUT", codes)

            monitor.read_status = original_poll  # type: ignore[method-assign]
            manager._unavailable_after_timeout.clear()
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_set_target_warning_does_not_publish_unconfirmed_target(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            temperature = manager.instruments["temperature"]
            original_set_target = temperature.set_target
            original_target = manager.latest["temperature"].target
            original_rate = manager.latest["temperature"].rate_per_minute

            async def warned_set_target(value, rate_per_minute, mode="Settle"):
                del value, rate_per_minute, mode
                raise InstrumentWarning(
                    "controller did not confirm the target",
                    "TARGET_NOT_CONFIRMED",
                )

            temperature.set_target = warned_set_target  # type: ignore[method-assign]
            applied = await manager.set_target("temperature", 250.0, 5.0)
            self.assertFalse(applied)
            self.assertEqual(manager.latest["temperature"].target, original_target)
            self.assertEqual(
                manager.latest["temperature"].rate_per_minute,
                original_rate,
            )
            self.assertIn(
                "TARGET_NOT_CONFIRMED",
                [notice.event.code for notice in notices],
            )
            temperature.set_target = original_set_target  # type: ignore[method-assign]
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_hold_timeout_is_reported_as_an_unconfirmed_safe_state(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            field = manager.instruments["field"]
            original_hold = field.hold
            manager.instrument_configs["field"] = replace(
                manager.instrument_configs["field"],
                operation_timeout_seconds=0.02,
            )

            async def hung_hold():
                await asyncio.sleep(5.0)

            field.hold = hung_hold  # type: ignore[method-assign]
            self.assertFalse(await manager.hold_all())
            self.assertIn(
                "INSTRUMENT_OPERATION_TIMEOUT",
                [notice.event.code for notice in notices],
            )
            field.hold = original_hold  # type: ignore[method-assign]
            manager._unavailable_after_timeout.clear()
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_explicit_hold_all_holds_controllable_instruments(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            called: list[str] = []

            for instrument_id in ("temperature", "field"):
                async def record_hold(selected=instrument_id):
                    called.append(selected)

                manager.instruments[instrument_id].hold = record_hold  # type: ignore[method-assign]

            await manager.connect_all()
            try:
                await manager.poll_all()
                self.assertTrue(await manager.hold_all())
                self.assertCountEqual(called, ["temperature", "field"])
            finally:
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_failed_poll_marks_old_snapshot_stale_and_recovery_resolves_it(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            config = replace(
                config,
                instruments=tuple(
                    replace(instrument, stale_after_seconds=0.01)
                    for instrument in config.instruments
                ),
            )
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()

            temperature = manager.instruments["temperature"]
            original_poll = temperature.read_status

            async def failed_poll():
                raise InstrumentWarning("temporary read failure", "TEMP_READ")

            temperature.read_status = failed_poll  # type: ignore[method-assign]
            await asyncio.sleep(0.02)
            stale = await manager.poll_all()
            self.assertEqual(
                stale["temperature"].stability,
                StabilityState.STALE,
            )
            self.assertIn("stale", stale["temperature"].message.lower())

            temperature.read_status = original_poll  # type: ignore[method-assign]
            recovered = await manager.poll_all()
            self.assertNotEqual(
                recovered["temperature"].stability,
                StabilityState.STALE,
            )
            stale_notices = [
                notice
                for notice in notices
                if notice.event.code == "STALE_READING"
            ]
            self.assertEqual(len(stale_notices), 2)
            self.assertFalse(stale_notices[0].is_resolution)
            self.assertTrue(stale_notices[1].is_resolution)
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_nonfinite_instrument_snapshot_is_rejected(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            monitor = manager.instruments["second_stage"]
            original_poll = monitor.read_status

            async def invalid_poll():
                reading = await original_poll()
                reading["value"] = math.nan
                return reading

            monitor.read_status = invalid_poll  # type: ignore[method-assign]
            snapshots = await manager.poll_all()
            self.assertTrue(math.isfinite(snapshots["second_stage"].current or math.nan))
            self.assertIn(
                "NONFINITE_INSTRUMENT_READING",
                [notice.event.code for notice in notices],
            )
            monitor.read_status = original_poll  # type: ignore[method-assign]
            await manager.disconnect_all()

        asyncio.run(scenario())


class UnitTests(unittest.TestCase):
    def test_field_conversion(self) -> None:
        self.assertAlmostEqual(convert_value(10000.0, "Oe", "T"), 1.0)
        self.assertAlmostEqual(convert_value(2.0, "T", "Oe"), 20000.0)
        with self.assertRaises(UnitConversionError):
            convert_value(1.0, "K", "T")

    def test_fixed_precision_suppresses_negative_zero(self) -> None:
        self.assertEqual(fixed_number(-0.001, 2), "0.00")
        self.assertEqual(fixed_number(300.1236, 3), "300.124")


if __name__ == "__main__":
    unittest.main()
