from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.instruments.manifest import load_instrument_manifest  # noqa: E402
from labcontrol.measurement.manifest import load_manifest  # noqa: E402


class BundledExamplesTests(unittest.TestCase):
    def test_bundled_measurement_examples_are_valid(self) -> None:
        paths = sorted(path for path in (ROOT / "modules").iterdir() if path.is_dir())
        descriptors = [load_manifest(path) for path in paths]
        self.assertEqual([item.id for item in descriptors], ["simulated_transport", "tutorial_resistance"])
        self.assertTrue(all(item.valid for item in descriptors), [item.error for item in descriptors])

    def test_bundled_system_examples_are_valid(self) -> None:
        paths = sorted(path for path in (ROOT / "system_instruments").iterdir() if path.is_dir())
        self.assertEqual(
            [path.name for path in paths],
            ["example_controller", "example_monitor"],
        )
        descriptors = [
            load_instrument_manifest(path)
            for path in paths
        ]
        self.assertTrue(
            all(descriptor.valid for descriptor in descriptors),
            [descriptor.error for descriptor in descriptors],
        )
        self.assertEqual(
            {descriptor.id for descriptor in descriptors},
            {
                "example_controller",
                "example_monitor",
            },
        )


if __name__ == "__main__":
    unittest.main()
