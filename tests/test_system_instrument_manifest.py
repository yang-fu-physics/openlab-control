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
from labcontrol.instruments.manifest import (  # noqa: E402
    configured_system_instruments,
    discover_system_instruments,
)
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.package_support.trust import (  # noqa: E402
    ContentTrustError,
    ContentTrustStore,
    content_tree_digest,
)
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402


def _write_instrument(
    root: Path,
    *,
    instrument_id: str = "example_temperature",
    dependencies: str = "",
    backend_source: str | None = None,
) -> Path:
    instrument = root / instrument_id
    instrument.mkdir(parents=True)
    dependency_block = f"dependencies = [{dependencies}]\n" if dependencies else ""
    (instrument / "instrument.toml").write_text(
        (
            f'id = "{instrument_id}"\n'
            'name = "Example Temperature"\n'
            'version = "0.1.0"\n'
            'api_version = "1.2"\n'
            'backend = "backend:ExampleTemperature"\n'
            'kinds = ["temperature"]\n'
            f"{dependency_block}"
        ),
        encoding="utf-8",
    )
    (instrument / "backend.py").write_text(
        backend_source
        or (
            "import time\n"
            "from labcontrol.instruments.base import SystemInstrument\n"
            "from labcontrol.models import InstrumentActivity, InstrumentSnapshot\n"
            "class ExampleTemperature(SystemInstrument):\n"
            "    async def connect(self): pass\n"
            "    async def disconnect(self): pass\n"
            "    async def poll(self):\n"
            "        return InstrumentSnapshot(self.config.id, self.config.display_name, "
            "self.config.kind, time.monotonic(), True, self.config.unit, "
            "self.config.initial_value, self.config.initial_value, "
            "self.config.default_rate_per_minute, InstrumentActivity.IDLE)\n"
            "    async def set_target(self, value, rate_per_minute, mode='Settle'): pass\n"
            "    async def hold(self): pass\n"
        ),
        encoding="utf-8",
    )
    return instrument


class SystemInstrumentManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "configs" / "default.toml")

    def test_manifest_discovery_validates_content_and_detects_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instrument = _write_instrument(root)
            config = replace(
                self.config,
                system_instruments=replace(self.config.system_instruments, directory=str(root)),
            )
            descriptor = discover_system_instruments(config)[0]
            self.assertTrue(descriptor.can_load, descriptor.error)
            self.assertEqual(descriptor.id, "example_temperature")
            self.assertEqual(descriptor.fingerprint, content_tree_digest(instrument))

            original = descriptor.fingerprint
            (instrument / "backend.py").write_text(
                (instrument / "backend.py").read_text(encoding="utf-8") + "\n# changed\n",
                encoding="utf-8",
            )
            changed = discover_system_instruments(config)[0]
            self.assertNotEqual(changed.fingerprint, original)

    def test_manifest_rejects_url_dependencies_and_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_instrument(
                root,
                dependencies='"driver @ https://example.invalid/driver.whl"',
            )
            duplicate = _write_instrument(root, instrument_id="other_instrument")
            (duplicate / "instrument.toml").write_text(
                (duplicate / "instrument.toml")
                .read_text(encoding="utf-8")
                .replace('id = "other_instrument"', 'id = "example_temperature"'),
                encoding="utf-8",
            )
            config = replace(
                self.config,
                system_instruments=replace(self.config.system_instruments, directory=str(root)),
            )
            descriptors = discover_system_instruments(config)
            self.assertEqual(len(descriptors), 2)
            self.assertTrue(all(not item.valid for item in descriptors))
            self.assertTrue(
                any("dependency URLs are not allowed" in item.error for item in descriptors)
            )
            self.assertTrue(any("Duplicate system instrument id" in item.error for item in descriptors))

    def test_framework_dependency_needs_no_runtime_packages(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_instrument(
                root,
                dependencies=(
                    '"PyVISA>=1.16,<1.17", '
                    '"typing_extensions>=4.16,<5"'
                ),
            )
            config = replace(
                self.config,
                system_instruments=replace(
                    self.config.system_instruments,
                    directory=str(root),
                ),
            )
            descriptor = discover_system_instruments(
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
                / "instrument.toml"
            )
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "PyVISA>=1.16,<1.17",
                    "PyVISA>=2",
                ),
                encoding="utf-8",
            )
            incompatible = discover_system_instruments(
                config
            )[0]
            self.assertFalse(incompatible.valid)
            self.assertIn(
                "framework-provided version 1.16.2",
                incompatible.error,
            )

    def test_configured_instrument_must_exist_support_kind_and_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_instrument(root)
            external_instrument = replace(
                self.config.instruments[0],
                backend="example_temperature",
            )
            config = replace(
                self.config,
                system_instruments=replace(self.config.system_instruments, directory=str(root)),
                instruments=(external_instrument,),
            )
            descriptors = discover_system_instruments(config)
            self.assertEqual(
                configured_system_instruments(config, descriptors),
                descriptors,
            )
            with self.assertRaisesRegex(ValueError, "unknown System Instrument"):
                configured_system_instruments(
                    replace(
                        config,
                        instruments=(replace(external_instrument, backend="missing_instrument"),),
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
                    "from labcontrol.instruments.base import SystemInstrument\n"
                    "from labcontrol.models import InstrumentSnapshot\n"
                    "class ExampleTemperature(SystemInstrument):\n"
                    "    async def connect(self): pass\n"
                    "    async def disconnect(self): pass\n"
                    "    async def poll(self):\n"
                    "        return InstrumentSnapshot(self.config.id, self.config.display_name, "
                    "self.config.kind, time.monotonic(), True, self.config.unit, 3.0)\n"
                    "    async def set_target(self, value, rate_per_minute, mode='Settle'): pass\n"
                    "    async def hold(self): pass\n"
                )
                _write_instrument(root, backend_source=source)
                state = root / "state"
                external_instrument = replace(
                    self.config.instruments[0],
                    backend="example_temperature",
                )
                config = replace(
                    self.config,
                    system_instruments=replace(
                        self.config.system_instruments,
                        directory=str(root),
                        state_directory=str(state),
                    ),
                    instruments=(external_instrument,),
                )
                descriptors = discover_system_instruments(config)
                with self.assertRaisesRegex(PermissionError, "has not been trusted"):
                    InstrumentManager(
                        config,
                        EventManager(),
                        descriptors,
                        isolate_processes=False,
                    )
                self.assertFalse(marker.exists())

                store = ContentTrustStore(state / "trusted_content.json")
                store.trust("instrument", descriptors[0])
                manager = InstrumentManager(
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
            instruments=(
                replace(
                    self.config.instruments[0],
                    backend="third_party.driver:UnsafeDriver",
                ),
            ),
        )
        with self.assertRaisesRegex(PermissionError, "Unmanifested third-party"):
            InstrumentManager(config, EventManager(), isolate_processes=False)


class ContentTrustStoreTests(unittest.TestCase):
    def test_trust_is_bound_to_type_version_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instrument = _write_instrument(root)
            config = replace(
                load_config(ROOT / "configs" / "default.toml"),
                system_instruments=replace(
                    load_config(ROOT / "configs" / "default.toml").system_instruments,
                    directory=str(root),
                ),
            )
            descriptor = discover_system_instruments(config)[0]
            store_path = root / "state" / "trusted_content.json"
            store = ContentTrustStore(store_path)
            self.assertFalse(store.is_trusted("instrument", descriptor))
            store.trust("instrument", descriptor)
            self.assertTrue(ContentTrustStore(store_path).is_trusted("instrument", descriptor))
            self.assertFalse(ContentTrustStore(store_path).is_trusted("module", descriptor))

            (instrument / "backend.py").write_text("# replaced\n", encoding="utf-8")
            changed = discover_system_instruments(config)[0]
            self.assertFalse(ContentTrustStore(store_path).is_trusted("instrument", changed))

    def test_corrupt_store_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trusted_content.json"
            path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            with self.assertRaisesRegex(ContentTrustError, "Cannot read"):
                ContentTrustStore(path)


if __name__ == "__main__":
    unittest.main()
