from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "modules" / "simulated_transport" / "backend.py"
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.module_api import ModuleAPI  # noqa: E402


def load_module_class():
    spec = importlib.util.spec_from_file_location("simulated_backend", BACKEND)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Module


class SimulatedTransportTests(unittest.TestCase):
    def test_minimal_lifecycle_and_four_slots(self) -> None:
        module = load_module_class()()
        api = ModuleAPI(
            {"temperature": {"current": 300.0}},
            lambda _kind, _values: None,
        )
        module.open(api)
        self.assertEqual(module.slots, 4)

        rows = [module.measure(slot, api) for slot in range(1, 5)]

        self.assertEqual([next(key for key in row if key.startswith("R")) for row in rows], ["R1", "R2", "R3", "R4"])
        self.assertEqual(module.close(api), {"State": "Disabled"})


if __name__ == "__main__":
    unittest.main()
