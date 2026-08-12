from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.module_api import (  # noqa: E402
    ModuleAPI,
    _ModuleOperationCancelled,
)


class ModuleAPITests(unittest.TestCase):
    @staticmethod
    def _api(
        *,
        emit=lambda _kind, _values: None,
        sample=None,
        state=None,
        timeout: float = 120.0,
    ) -> ModuleAPI:
        return ModuleAPI(
            {},
            emit,
            sample,
            state,
            timeout,
        )

    def test_instruments_returns_each_fresh_snapshot_as_a_copy(self) -> None:
        samples = iter(
            (
                {"temperature": {"kind": "temperature", "current": 1.0}},
                {"temperature": {"kind": "temperature", "current": 2.0}},
            )
        )
        api = self._api(sample=lambda _timeout: next(samples))

        first = api.instruments()
        second = api.instruments()

        self.assertEqual(first["temperature"]["current"], 1.0)
        self.assertEqual(second["temperature"]["current"], 2.0)
        second["temperature"]["current"] = 99.0
        self.assertEqual(api._initial_instruments["temperature"]["current"], 2.0)

    def test_sleep_excludes_paused_time(self) -> None:
        state = {"value": "running"}
        api = self._api(state=lambda _timeout: state["value"])
        finished = threading.Event()
        elapsed: list[float] = []

        def run() -> None:
            started = time.monotonic()
            api.sleep(0.12, poll_interval=0.02)
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

    def test_sleep_stops_cooperatively(self) -> None:
        state = {"value": "running"}
        api = self._api(state=lambda _timeout: state["value"])
        captured: list[BaseException] = []

        def run() -> None:
            try:
                api.sleep(10.0, poll_interval=0.02)
            except BaseException as exc:
                captured.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.04)
        state["value"] = "stopping"
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], _ModuleOperationCancelled)

    def test_sleep_poll_interval_does_not_shorten_context_rpc_timeout(self) -> None:
        timeouts: list[float] = []
        api = self._api(
            state=lambda timeout: timeouts.append(timeout) or "running",
            timeout=120.0,
        )

        api.sleep(0.002, poll_interval=0.001)

        self.assertGreaterEqual(len(timeouts), 2)
        self.assertEqual(set(timeouts), {1.0})
        self.assertEqual(api.timeout, 120.0)

    def test_warn_resolve_and_status_use_small_events(self) -> None:
        events: list[tuple[str, dict]] = []
        api = self._api(emit=lambda kind, values: events.append((kind, values)))

        api.warn("RANGE", "over range", "ch1")
        api.warn("RANGE", None, "ch1")
        api.status({"State": "Ready"})

        self.assertEqual(
            events,
            [
                (
                    "warning",
                    {"message": "over range", "code": "RANGE", "context": "ch1"},
                ),
                ("resolve", {"code": "RANGE", "context": "ch1"}),
                ("status", {"values": {"State": "Ready"}}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
