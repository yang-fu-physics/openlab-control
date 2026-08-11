from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.devices.manifest import (  # noqa: E402
    configured_device_plugins,
    discover_device_plugins,
)
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.extensions.trust import (  # noqa: E402
    ExtensionTrustError,
    PluginTrustStore,
    extension_tree_digest,
)
from labcontrol.plugins import DeviceManager  # noqa: E402


def _write_plugin(
    root: Path,
    *,
    plugin_id: str = "example_temperature",
    dependencies: str = "",
    backend_source: str | None = None,
) -> Path:
    plugin = root / plugin_id
    plugin.mkdir(parents=True)
    dependency_block = f"dependencies = [{dependencies}]\n" if dependencies else ""
    (plugin / "device.toml").write_text(
        (
            f'id = "{plugin_id}"\n'
            'name = "Example Temperature"\n'
            'version = "0.1.0"\n'
            'api_version = "1.1"\n'
            'backend = "backend:ExampleTemperature"\n'
            'kinds = ["temperature"]\n'
            f"{dependency_block}"
        ),
        encoding="utf-8",
    )
    (plugin / "backend.py").write_text(
        backend_source
        or (
            "import time\n"
            "from labcontrol.devices.base import DevicePlugin\n"
            "from labcontrol.models import DeviceActivity, DeviceSnapshot\n"
            "class ExampleTemperature(DevicePlugin):\n"
            "    async def connect(self): pass\n"
            "    async def disconnect(self): pass\n"
            "    async def poll(self):\n"
            "        return DeviceSnapshot(self.config.id, self.config.display_name, "
            "self.config.kind, time.monotonic(), True, self.config.unit, "
            "self.config.initial_value, self.config.initial_value, "
            "self.config.default_rate_per_minute, DeviceActivity.IDLE)\n"
            "    async def set_target(self, value, rate_per_minute, mode='Settle'): pass\n"
            "    async def hold(self): pass\n"
        ),
        encoding="utf-8",
    )
    return plugin


class DevicePluginManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "configs" / "default.toml")

    def test_manifest_discovery_validates_content_and_detects_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = _write_plugin(root)
            config = replace(
                self.config,
                plugins=replace(self.config.plugins, device_directory=str(root)),
            )
            descriptor = discover_device_plugins(config)[0]
            self.assertTrue(descriptor.can_load, descriptor.error)
            self.assertEqual(descriptor.id, "example_temperature")
            self.assertEqual(descriptor.fingerprint, extension_tree_digest(plugin))

            original = descriptor.fingerprint
            (plugin / "backend.py").write_text(
                (plugin / "backend.py").read_text(encoding="utf-8") + "\n# changed\n",
                encoding="utf-8",
            )
            changed = discover_device_plugins(config)[0]
            self.assertNotEqual(changed.fingerprint, original)

    def test_manifest_rejects_url_dependencies_and_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_plugin(
                root,
                dependencies='"driver @ https://example.invalid/driver.whl"',
            )
            duplicate = _write_plugin(root, plugin_id="other_plugin")
            (duplicate / "device.toml").write_text(
                (duplicate / "device.toml")
                .read_text(encoding="utf-8")
                .replace('id = "other_plugin"', 'id = "example_temperature"'),
                encoding="utf-8",
            )
            config = replace(
                self.config,
                plugins=replace(self.config.plugins, device_directory=str(root)),
            )
            descriptors = discover_device_plugins(config)
            self.assertEqual(len(descriptors), 2)
            self.assertTrue(all(not item.valid for item in descriptors))
            self.assertTrue(
                any("dependency URLs are not allowed" in item.error for item in descriptors)
            )
            self.assertTrue(any("Duplicate device plugin id" in item.error for item in descriptors))

    def test_framework_dependency_needs_no_plugin_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_plugin(
                root,
                dependencies=(
                    '"PyVISA>=1.16,<1.17", '
                    '"typing_extensions>=4.16,<5"'
                ),
            )
            config = replace(
                self.config,
                plugins=replace(
                    self.config.plugins,
                    device_directory=str(root),
                ),
            )
            descriptor = discover_device_plugins(
                config
            )[0]
            self.assertTrue(
                descriptor.valid,
                descriptor.error,
            )
            self.assertEqual(descriptor.dependencies, ())
            self.assertEqual(
                descriptor.framework_dependencies,
                (
                    "PyVISA>=1.16,<1.17",
                    "typing_extensions>=4.16,<5",
                ),
            )

            manifest = (
                root
                / "example_temperature"
                / "device.toml"
            )
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "PyVISA>=1.16,<1.17",
                    "PyVISA>=2",
                ),
                encoding="utf-8",
            )
            incompatible = discover_device_plugins(
                config
            )[0]
            self.assertFalse(incompatible.valid)
            self.assertIn(
                "framework-provided version 1.16.2",
                incompatible.error,
            )

    def test_configured_plugin_must_exist_support_kind_and_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_plugin(root)
            external_device = replace(
                self.config.devices[0],
                plugin="example_temperature",
            )
            config = replace(
                self.config,
                plugins=replace(self.config.plugins, device_directory=str(root)),
                devices=(external_device,),
            )
            descriptors = discover_device_plugins(config)
            self.assertEqual(
                configured_device_plugins(config, descriptors),
                descriptors,
            )
            with self.assertRaisesRegex(ValueError, "unknown external plugin"):
                configured_device_plugins(
                    replace(
                        config,
                        devices=(replace(external_device, plugin="missing_plugin"),),
                    ),
                    descriptors,
                )

    def test_external_code_is_not_imported_until_content_is_trusted(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / "imported.txt"
                source = (
                    "import time\n"
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
                    "from labcontrol.devices.base import DevicePlugin\n"
                    "from labcontrol.models import DeviceSnapshot\n"
                    "class ExampleTemperature(DevicePlugin):\n"
                    "    async def connect(self): pass\n"
                    "    async def disconnect(self): pass\n"
                    "    async def poll(self):\n"
                    "        return DeviceSnapshot(self.config.id, self.config.display_name, "
                    "self.config.kind, time.monotonic(), True, self.config.unit, 3.0)\n"
                    "    async def set_target(self, value, rate_per_minute, mode='Settle'): pass\n"
                    "    async def hold(self): pass\n"
                )
                _write_plugin(root, backend_source=source)
                state = root / "state"
                external_device = replace(
                    self.config.devices[0],
                    plugin="example_temperature",
                )
                config = replace(
                    self.config,
                    plugins=replace(
                        self.config.plugins,
                        device_directory=str(root),
                        state_directory=str(state),
                    ),
                    devices=(external_device,),
                )
                descriptors = discover_device_plugins(config)
                with self.assertRaisesRegex(PermissionError, "has not been trusted"):
                    DeviceManager(
                        config,
                        EventManager(),
                        descriptors,
                        isolate_processes=False,
                    )
                self.assertFalse(marker.exists())

                store = PluginTrustStore(state / "trusted_plugins.json")
                store.trust("device", descriptors[0])
                manager = DeviceManager(
                    config,
                    EventManager(),
                    descriptors,
                    isolate_processes=False,
                )
                self.assertTrue(marker.exists())
                await manager.connect_all()
                snapshots = await manager.poll_all()
                self.assertEqual(snapshots["temperature"].current, 3.0)
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_unmanifested_third_party_import_is_rejected(self) -> None:
        config = replace(
            self.config,
            devices=(
                replace(
                    self.config.devices[0],
                    plugin="third_party.driver:UnsafeDriver",
                ),
            ),
        )
        with self.assertRaisesRegex(PermissionError, "Unmanifested third-party"):
            DeviceManager(config, EventManager(), isolate_processes=False)


class PluginTrustStoreTests(unittest.TestCase):
    def test_trust_is_bound_to_type_version_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = _write_plugin(root)
            config = replace(
                load_config(ROOT / "configs" / "default.toml"),
                plugins=replace(
                    load_config(ROOT / "configs" / "default.toml").plugins,
                    device_directory=str(root),
                ),
            )
            descriptor = discover_device_plugins(config)[0]
            store_path = root / "state" / "trusted_plugins.json"
            store = PluginTrustStore(store_path)
            self.assertFalse(store.is_trusted("device", descriptor))
            store.trust("device", descriptor)
            self.assertTrue(PluginTrustStore(store_path).is_trusted("device", descriptor))
            self.assertFalse(PluginTrustStore(store_path).is_trusted("module", descriptor))

            (plugin / "backend.py").write_text("# replaced\n", encoding="utf-8")
            changed = discover_device_plugins(config)[0]
            self.assertFalse(PluginTrustStore(store_path).is_trusted("device", changed))

    def test_corrupt_store_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trusted_plugins.json"
            path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionTrustError, "Cannot read"):
                PluginTrustStore(path)


if __name__ == "__main__":
    unittest.main()
