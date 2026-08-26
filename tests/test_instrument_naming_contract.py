from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.instruments.base import SystemInstrument  # noqa: E402
from labcontrol.models import InstrumentSnapshot  # noqa: E402
from tests.configuration_fixtures import load_simulated_config  # noqa: E402


class InstrumentNamingContractTests(unittest.TestCase):
    def test_new_public_names_are_available(self) -> None:
        self.assertEqual(InstrumentManager.__name__, "InstrumentManager")
        self.assertEqual(SystemInstrument.__name__, "SystemInstrument")
        self.assertEqual(InstrumentSnapshot.__name__, "InstrumentSnapshot")

    def test_removed_python_names_are_not_importable(self) -> None:
        self.assertIsNone(importlib.util.find_spec("labcontrol.devices"))
        self.assertIsNone(importlib.util.find_spec("labcontrol.plugins"))
        self.assertIsNone(importlib.util.find_spec("labcontrol.extensions"))

    def test_configuration_exposes_only_instrument_names(self) -> None:
        config = load_simulated_config()
        self.assertTrue(config.instrument_instances)
        self.assertEqual(config.system_instruments.directory, "system_instruments")
        self.assertFalse(hasattr(config, "devices"))
        self.assertFalse(hasattr(config, "plugins"))
        self.assertTrue(
            all(hasattr(item, "backend") for item in config.instrument_instances)
        )
        self.assertTrue(
            all(not hasattr(item, "plugin") for item in config.instrument_instances)
        )

    def test_removed_directories_and_manifests_are_absent(self) -> None:
        for relative in (
            "src/labcontrol/devices",
            "src/labcontrol/plugins.py",
            "src/labcontrol/extensions",
            "device_plugins",
            "plugin_runtime",
            "plugin_state",
            "plugin_templates",
        ):
            with self.subTest(relative=relative):
                self.assertFalse((ROOT / relative).exists())
        # build/ 和 dist/ 可能保留用户以前用于发布核对的只读副本；命名契约只约束
        # 当前源码、分发模板和当前安装目录，不能为了测试去删除历史产物。
        for root in (
            ROOT / "src",
            ROOT / "templates",
            ROOT / "system_instruments",
        ):
            with self.subTest(manifest_root=root):
                self.assertFalse(tuple(root.rglob("device.toml")))


if __name__ == "__main__":
    unittest.main()
