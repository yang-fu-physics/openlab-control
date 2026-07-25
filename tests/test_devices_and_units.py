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
from labcontrol.devices.base import DeviceError, DeviceWarning, SafetyViolation  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.formatting import fixed_number  # noqa: E402
from labcontrol.models import DeviceKind, StabilityState  # noqa: E402
from labcontrol.plugins import DeviceManager  # noqa: E402
from labcontrol.units import UnitConversionError, convert_value  # noqa: E402


class DeviceManagerTests(unittest.TestCase):
    def test_simulated_control_and_monitor_plugins_load(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = DeviceManager(config, events)
            await manager.connect_all()
            snapshots = await manager.poll_all()
            self.assertEqual(len(snapshots), 3)
            self.assertEqual(manager.first_device_id(DeviceKind.TEMPERATURE), "temperature")
            second_stage = snapshots["second_stage"]
            self.assertEqual(second_stage.kind, DeviceKind.MONITOR)
            self.assertIsNotNone(second_stage.current)
            self.assertIsNone(second_stage.target)
            self.assertNotIn("second_stage", manager._stability)
            with self.assertRaises(DeviceError) as blocked:
                await manager.set_target("second_stage", 5.0, 1.0)
            self.assertEqual(blocked.exception.code, "TARGET_NOT_CONTROLLABLE")
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_safety_limits_reject_target(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        field = next(device for device in config.devices if device.id == "field")
        self.assertEqual(field.unit, "Oe")
        self.assertEqual(field.min_value, -90000.0)
        self.assertEqual(field.max_value, 90000.0)
        self.assertEqual(field.default_rate_per_minute, 5000.0)
        manager = DeviceManager(config, EventManager())
        with self.assertRaises(SafetyViolation):
            manager.validate_target("field", 200000.0, 5000.0)
        with self.assertRaises(SafetyViolation):
            manager.validate_target("temperature", 300.0, 100.0)
        with self.assertRaises(SafetyViolation):
            manager.validate_target("temperature", 300.0, math.nan)
        with self.assertRaises(SafetyViolation):
            manager.validate_target("temperature", math.inf, 10.0)

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
                    "poll_interval_seconds = 0.20",
                    "poll_interval_seconds = 0",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "greater than zero"):
                load_config(invalid_poll)

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

    def test_completed_poll_cannot_overwrite_a_new_target(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            manager = DeviceManager(config, EventManager())
            await manager.connect_all()
            await manager.poll_all()

            monitor = manager.devices["second_stage"]
            original_poll = monitor.poll
            monitor_started = asyncio.Event()
            release_monitor = asyncio.Event()

            async def delayed_monitor_poll():
                monitor_started.set()
                await release_monitor.wait()
                return await original_poll()

            monitor.poll = delayed_monitor_poll  # type: ignore[method-assign]
            poll_task = asyncio.create_task(manager.poll_all())
            await monitor_started.wait()
            await manager.set_target("field", 100.0, 5000.0)
            release_monitor.set()
            await poll_task
            self.assertEqual(manager.latest["field"].target, 100.0)
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_poll_timeout_quarantines_device_until_restart(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            notices = []
            events.subscribe_occurrences(notices.append)
            manager = DeviceManager(config, events)
            await manager.connect_all()
            await manager.poll_all()
            monitor = manager.devices["second_stage"]
            original_poll = monitor.poll
            manager.device_configs["second_stage"] = replace(
                manager.device_configs["second_stage"],
                operation_timeout_seconds=0.02,
            )

            async def hung_poll():
                await asyncio.sleep(5.0)
                return await original_poll()

            monitor.poll = hung_poll  # type: ignore[method-assign]
            started = asyncio.get_running_loop().time()
            await manager.poll_all()
            self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
            self.assertEqual(
                manager._unavailable_after_timeout["second_stage"],
                "poll",
            )
            await manager.poll_all()
            codes = [notice.event.code for notice in notices]
            self.assertIn("DEVICE_OPERATION_TIMEOUT", codes)
            self.assertIn("DEVICE_UNAVAILABLE_AFTER_TIMEOUT", codes)

            monitor.poll = original_poll  # type: ignore[method-assign]
            manager._unavailable_after_timeout.clear()
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_set_target_warning_does_not_publish_unconfirmed_target(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = DeviceManager(config, events)
            await manager.connect_all()
            await manager.poll_all()
            temperature = manager.devices["temperature"]
            original_set_target = temperature.set_target
            original_target = manager.latest["temperature"].target
            original_rate = manager.latest["temperature"].rate_per_minute

            async def warned_set_target(value, rate_per_minute, mode="Settle"):
                del value, rate_per_minute, mode
                raise DeviceWarning(
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
            manager = DeviceManager(config, events)
            await manager.connect_all()
            field = manager.devices["field"]
            original_hold = field.hold
            manager.device_configs["field"] = replace(
                manager.device_configs["field"],
                operation_timeout_seconds=0.02,
            )

            async def hung_hold():
                await asyncio.sleep(5.0)

            field.hold = hung_hold  # type: ignore[method-assign]
            self.assertFalse(await manager.hold_all())
            self.assertIn(
                "DEVICE_OPERATION_TIMEOUT",
                [notice.event.code for notice in notices],
            )
            field.hold = original_hold  # type: ignore[method-assign]
            manager._unavailable_after_timeout.clear()
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_failed_poll_marks_old_snapshot_stale_and_recovery_resolves_it(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            config = replace(
                config,
                devices=tuple(
                    replace(device, stale_after_seconds=0.01)
                    for device in config.devices
                ),
            )
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = DeviceManager(config, events)
            await manager.connect_all()
            await manager.poll_all()

            temperature = manager.devices["temperature"]
            original_poll = temperature.poll

            async def failed_poll():
                raise DeviceWarning("temporary read failure", "TEMP_READ")

            temperature.poll = failed_poll  # type: ignore[method-assign]
            await asyncio.sleep(0.02)
            stale = await manager.poll_all()
            self.assertEqual(
                stale["temperature"].stability,
                StabilityState.STALE,
            )
            self.assertIn("stale", stale["temperature"].message.lower())

            temperature.poll = original_poll  # type: ignore[method-assign]
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

    def test_nonfinite_device_snapshot_is_rejected(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = DeviceManager(config, events)
            await manager.connect_all()
            await manager.poll_all()
            monitor = manager.devices["second_stage"]
            original_poll = monitor.poll

            async def invalid_poll():
                return replace(await original_poll(), current=math.nan)

            monitor.poll = invalid_poll  # type: ignore[method-assign]
            snapshots = await manager.poll_all()
            self.assertTrue(math.isfinite(snapshots["second_stage"].current or math.nan))
            self.assertIn(
                "NONFINITE_DEVICE_READING",
                [notice.event.code for notice in notices],
            )
            monitor.poll = original_poll  # type: ignore[method-assign]
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
