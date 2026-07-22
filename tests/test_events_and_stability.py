from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import StabilityConfig  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.models import Severity, StabilityState  # noqa: E402
from labcontrol.stability import StabilityEvaluator  # noqa: E402


class EventTests(unittest.TestCase):
    def test_warning_popup_is_latched_until_resolved(self) -> None:
        manager = EventManager()
        notices = []
        manager.subscribe(notices.append)
        first, first_is_new = manager.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
        second, second_is_new = manager.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
        self.assertTrue(first_is_new)
        self.assertFalse(second_is_new)
        self.assertEqual(second.count, 2)
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0].show_popup)
        manager.resolve("meter", "OVERLOAD")
        self.assertEqual(len(notices), 2)
        self.assertTrue(notices[1].is_resolution)
        _, third_is_new = manager.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
        self.assertTrue(third_is_new)
        self.assertEqual(len(notices), 3)

    def test_info_is_not_latched(self) -> None:
        manager = EventManager()
        notices = []
        manager.subscribe(notices.append)
        manager.report(Severity.INFO, "sequence", "STEP", "one")
        manager.report(Severity.INFO, "sequence", "STEP", "two")
        self.assertEqual(len(notices), 2)
        self.assertEqual(manager.active_events(), ())


class StabilityTests(unittest.TestCase):
    def test_requires_tolerance_slope_and_dwell(self) -> None:
        evaluator = StabilityEvaluator(StabilityConfig(
            tolerance=0.1,
            max_slope_per_minute=0.05,
            dwell_seconds=1.0,
            timeout_seconds=10.0,
            window_seconds=1.0,
        ))
        states = [evaluator.update(10.0, 10.0, moment).state for moment in (0.0, 0.5, 1.0, 1.5, 2.0)]
        self.assertIn(StabilityState.SETTLING, states)
        self.assertEqual(states[-1], StabilityState.STABLE)

    def test_timeout(self) -> None:
        evaluator = StabilityEvaluator(StabilityConfig(
            tolerance=0.01,
            max_slope_per_minute=0.01,
            dwell_seconds=1.0,
            timeout_seconds=2.0,
            window_seconds=1.0,
        ))
        evaluator.update(0.0, 10.0, 0.0)
        result = evaluator.update(0.1, 10.0, 2.1)
        self.assertEqual(result.state, StabilityState.TIMED_OUT)


if __name__ == "__main__":
    unittest.main()
