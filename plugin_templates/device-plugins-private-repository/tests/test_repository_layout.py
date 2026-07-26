from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_every_plugin_has_manifest_and_backend(self) -> None:
        plugin_paths = sorted((ROOT / "plugins").iterdir())
        self.assertTrue(plugin_paths)
        for plugin_path in plugin_paths:
            with self.subTest(plugin=plugin_path.name):
                self.assertTrue((plugin_path / "device.toml").is_file())
                self.assertTrue((plugin_path / "backend.py").is_file())
                self.assertFalse((plugin_path / "runtime.json").exists())


if __name__ == "__main__":
    unittest.main()
