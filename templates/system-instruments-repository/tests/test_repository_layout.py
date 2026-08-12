from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_every_instrument_has_manifest_and_backend(self) -> None:
        instrument_paths = sorted((ROOT / "instruments").iterdir())
        self.assertTrue(instrument_paths)
        for instrument_path in instrument_paths:
            with self.subTest(instrument=instrument_path.name):
                self.assertTrue((instrument_path / "instrument.toml").is_file())
                self.assertTrue((instrument_path / "backend.py").is_file())
                self.assertFalse((instrument_path / "runtime.json").exists())


if __name__ == "__main__":
    unittest.main()
