from __future__ import annotations

import threading
import time
import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.measurement.api import (
    ModuleOperationCancelled,
    ModuleOperationContext,
)


class ModuleOperationContextTests(unittest.TestCase):
    def test_sample_system_returns_a_fresh_copy(self) -> None:
        samples = iter(
            (
                {
                    "temperature": {
                        "kind": "temperature",
                        "current": 1.0,
                    }
                },
                {
                    "temperature": {
                        "kind": "temperature",
                        "current": 2.0,
                    }
                },
            )
        )
        context = ModuleOperationContext(
            {},
            lambda _kind, _values: None,
            lambda _timeout: next(samples),
            lambda _timeout: "running",
        )

        first = context.sample_system()
        second = context.sample_system()

        self.assertEqual(
            first["temperature"]["current"],
            1.0,
        )
        self.assertEqual(
            second["temperature"]["current"],
            2.0,
        )
        second["temperature"]["current"] = 99.0
        self.assertEqual(
            context.system["temperature"]["current"],
            2.0,
        )

    def test_interruptible_sleep_excludes_paused_time(self) -> None:
        state = {"value": "running"}
        context = ModuleOperationContext(
            {},
            lambda _kind, _values: None,
            _operation_state=lambda _timeout: state["value"],
        )
        finished = threading.Event()
        elapsed: list[float] = []

        def run() -> None:
            started = time.monotonic()
            context.interruptible_sleep(
                0.12,
                poll_interval_seconds=0.02,
            )
            elapsed.append(time.monotonic() - started)
            finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.04)
        state["value"] = "paused"
        time.sleep(0.16)
        self.assertFalse(finished.is_set())
        state["value"] = "running"
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(elapsed[0], 0.24)

    def test_interruptible_sleep_stops_cooperatively(self) -> None:
        state = {"value": "running"}
        context = ModuleOperationContext(
            {},
            lambda _kind, _values: None,
            _operation_state=lambda _timeout: state["value"],
        )
        captured: list[BaseException] = []

        def run() -> None:
            try:
                context.interruptible_sleep(
                    10.0,
                    poll_interval_seconds=0.02,
                )
            except BaseException as exc:
                captured.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.04)
        state["value"] = "stopping"
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(
            captured[0],
            ModuleOperationCancelled,
        )


if __name__ == "__main__":
    unittest.main()
