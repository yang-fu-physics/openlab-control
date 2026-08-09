from __future__ import annotations

import importlib.util
import math
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "modules" / "tutorial_resistance" / "backend.py"
FRONTEND = ROOT / "modules" / "tutorial_resistance" / "frontend.py"
sys.path.insert(0, str(ROOT.parents[1] / "src"))

from labcontrol.module_api import ModuleAPI, ModuleError  # noqa: E402
from labcontrol.measurement.frontend_api import ModuleUIAPI  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def load_module_class():
    spec = importlib.util.spec_from_file_location("tutorial_resistance_backend", BACKEND)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Module


def load_frontend_class():
    spec = importlib.util.spec_from_file_location("tutorial_resistance_frontend", FRONTEND)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Frontend


class TutorialResistanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.api = ModuleAPI(
            {
                "temperature": {"current": 300.0},
                "field": {"current": 0.0},
            },
            lambda kind, values: self.events.append((kind, values)),
        )
        self.module = load_module_class()()
        self.module.open(self.api)
        self.module.configure({}, self.api)

    def test_four_slots_return_one_sparse_row_each(self) -> None:
        self.assertEqual(self.module.slots, 4)
        for slot in range(1, 5):
            row, rawdata = self.module.measure(slot, self.api)
            measured_columns = [key for key in row if key.startswith("R") and key[1:].isdigit()]
            self.assertEqual(measured_columns, [f"R{slot}"])
            self.assertEqual(row["StatusCode"], 0)
            self.assertEqual(row["SampleCount"], 3)
            self.assertEqual(len(rawdata), 3)
            self.assertTrue(all(math.isfinite(value) for value in rawdata))

    def test_over_range_leaves_resistance_empty_and_warns(self) -> None:
        self.module.configure({"over_range_ohm": 1.0}, self.api)
        row, _rawdata = self.module.measure(1, self.api)
        self.assertNotIn("R1", row)
        self.assertEqual(row["StatusCode"], 1)
        self.assertTrue(
            any(kind == "warning" and payload["code"] == "OVER_RANGE" for kind, payload in self.events)
        )

    def test_sequence_commands_accept_si_prefixes(self) -> None:
        result = self.module.execute_sequence_command(
            "set_current",
            {"current": "1 mA", "settle_seconds": 0},
            self.api,
        )
        self.assertAlmostEqual(result["Excitation Current (A)"], 1e-3)
        result = self.module.execute_sequence_command(
            "scan_current",
            {"points": ["2u"], "current": "2u", "settle_seconds": 0},
            self.api,
        )
        self.assertAlmostEqual(result["Excitation Current (A)"], 2e-6)

    def test_requires_apply_and_close_is_idempotent(self) -> None:
        module = load_module_class()()
        module.open(self.api)
        with self.assertRaisesRegex(ModuleError, "Apply Settings"):
            module.measure(1, self.api)
        module.close(self.api)
        self.assertEqual(module.close(self.api)["Connection"], "Closed")

    def test_frontend_load_dump_round_trip_does_not_request_backend(self) -> None:
        app = QApplication.instance() or QApplication([])
        api = ModuleUIAPI()
        actions: list[tuple[str, dict[str, object]]] = []
        api.actionRequested.connect(
            lambda name, payload: actions.append((name, payload))
        )
        frontend = load_frontend_class()(api)
        expected = {
            "base_resistance_ohm": 123.0,
            "channel_step_ohm": 4.0,
            "delay_seconds": 0.25,
            "noise_ohm": 0.002,
            "over_range_ohm": 9999.0,
        }
        frontend.load(expected)
        app.processEvents()
        self.assertEqual(frontend.dump(), expected)
        self.assertEqual(actions, [])
        self.assertIsNotNone(frontend.status_widget)
        frontend.deleteLater()


if __name__ == "__main__":
    unittest.main()
