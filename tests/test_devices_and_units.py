from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.devices.base import DeviceError, SafetyViolation  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.formatting import fixed_number  # noqa: E402
from labcontrol.models import DeviceKind  # noqa: E402
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
