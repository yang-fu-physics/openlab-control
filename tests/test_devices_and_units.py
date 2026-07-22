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
from labcontrol.models import DeviceKind  # noqa: E402
from labcontrol.plugins import DeviceManager  # noqa: E402
from labcontrol.units import UnitConversionError, convert_value  # noqa: E402


class DeviceManagerTests(unittest.TestCase):
    def test_simulated_plugins_load_and_measure(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = DeviceManager(config, events)
            await manager.connect_all()
            snapshots = await manager.poll_all()
            self.assertEqual(len(snapshots), 4)
            self.assertEqual(manager.first_device_id(DeviceKind.TEMPERATURE), "temperature")
            second_stage = snapshots["second_stage"]
            self.assertEqual(second_stage.kind, DeviceKind.MONITOR)
            self.assertIsNotNone(second_stage.current)
            self.assertIsNone(second_stage.target)
            self.assertNotIn("second_stage", manager._stability)
            with self.assertRaises(DeviceError) as blocked:
                await manager.set_target("second_stage", 5.0, 1.0)
            self.assertEqual(blocked.exception.code, "TARGET_NOT_CONTROLLABLE")
            values = await manager.measure()
            self.assertEqual(set(values), {"R1", "R2", "R3", "R4"})
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_safety_limits_reject_target(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        manager = DeviceManager(config, EventManager())
        with self.assertRaises(SafetyViolation):
            manager.validate_target("field", 20.0, 0.5)
        with self.assertRaises(SafetyViolation):
            manager.validate_target("temperature", 300.0, 100.0)


class UnitTests(unittest.TestCase):
    def test_field_conversion(self) -> None:
        self.assertAlmostEqual(convert_value(10000.0, "Oe", "T"), 1.0)
        self.assertAlmostEqual(convert_value(2.0, "T", "Oe"), 20000.0)
        with self.assertRaises(UnitConversionError):
            convert_value(1.0, "K", "T")


if __name__ == "__main__":
    unittest.main()
