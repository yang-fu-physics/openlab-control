from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import InstrumentConfig, load_config  # noqa: E402
from labcontrol.instruments.manifest import (  # noqa: E402
    SYSTEM_INSTRUMENT_API_VERSION,
    configured_system_instruments,
    discover_system_instruments,
    load_instrument_manifest,
)
from labcontrol.models import InstrumentKind  # noqa: E402


MANIFEST = """\
id = "test_controller"
name = "Test Controller"
version = "1.2.3"
api_version = "4"
core_requires = ">=0.19,<0.20"
backend = "backend:Driver"
kinds = ["temperature"]

[discovery]
identity_pattern = "^ACME,CTRL,"

[[config_fields]]
id = "host"
label = "Host"
type = "string"
default = ""

[[config_fields]]
id = "channel"
label = "Channel"
type = "integer"
default = 1
min = 1
max = 4

[[config_fields]]
id = "mode"
label = "Mode"
type = "choice"
default = "auto"
options = ["auto", "manual"]

[[controls]]
id = "loop1"
label = "Loop 1"

[[panels]]
id = "control"
label = "Control"
template = "controller"
control = "loop1"
reading_options = ["temperature"]
default_reading = "temperature"
min_value = 1.8
max_value = 400.0
default_rate_per_minute = 1.0
max_rate_per_minute = 30.0
stability_tolerance = 0.05
stability_max_slope_per_minute = 0.03
stability_dwell_seconds = 1.5
stability_timeout_seconds = 120.0
stability_window_seconds = 1.0

[[panels]]
id = "heater"
label = "Heater"
template = "readout"
readings = ["heater"]

[[sequence_commands]]
id = "reset"
label = "Reset Controller"

[readings.temperature]
label = "Temperature"
unit = "K"
decimals = 3

[readings.heater]
label = "Heater"
unit = "%"
decimals = 1
"""


def _write_instrument(
    root: Path,
    *,
    name: str = "test_controller",
    manifest: str = MANIFEST,
    backend: str = "class Driver: pass\n",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "instrument.toml").write_text(manifest, encoding="utf-8")
    (directory / "backend.py").write_text(backend, encoding="utf-8")
    return directory


def _copy_general(root: Path):
    configs = root / "configs"
    configs.mkdir()
    general = configs / "general.toml"
    shutil.copy2(ROOT / "configs" / "general.toml", general)
    return load_config(general)


class SystemInstrumentManifestTests(unittest.TestCase):
    def test_api_v4_manifest_declares_static_configuration_and_fixed_panels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            descriptor = load_instrument_manifest(
                _write_instrument(Path(temporary))
            )

            self.assertTrue(descriptor.valid, descriptor.error)
            self.assertEqual(SYSTEM_INSTRUMENT_API_VERSION, "4")
            self.assertEqual(descriptor.id, "test_controller")
            self.assertEqual(descriptor.identity_pattern, "^ACME,CTRL,")
            self.assertEqual(
                [field.id for field in descriptor.config_fields],
                ["host", "channel", "mode"],
            )
            self.assertEqual([control.id for control in descriptor.controls], ["loop1"])
            self.assertEqual([panel.id for panel in descriptor.panels], ["control", "heater"])
            self.assertEqual(descriptor.panel("control").control, "loop1")
            self.assertEqual(descriptor.panel("heater").readings, ("heater",))
            self.assertEqual(
                [command.id for command in descriptor.sequence_commands],
                ["reset"],
            )

    def test_bundled_templates_are_valid_api_v4(self) -> None:
        for name in ("example_controller", "example_monitor"):
            descriptor = load_instrument_manifest(ROOT / "system_instruments" / name)
            self.assertTrue(descriptor.valid, descriptor.error)
            self.assertEqual(descriptor.api_version, "4")

    def test_obsolete_manifest_shapes_are_rejected(self) -> None:
        replacements = (
            ('api_version = "4"', 'api_version = "3"', "incompatible"),
            (
                'backend = "backend:Driver"',
                'backend = "backend:Driver"\nmain_reading = "temperature"',
                "unknown instrument.toml fields",
            ),
            (
                "[discovery]",
                "[settings]\nvisa_timeout_ms = 1000\n\n[discovery]",
                "unknown instrument.toml fields",
            ),
            (
                "[[panels]]",
                "[panel]\ntemplate = \"controller\"\n\n[[panels]]",
                "unknown instrument.toml fields",
            ),
            (
                'core_requires = ">=0.19,<0.20"',
                'core_requires = ">=0.19,<0.20"\ndependencies = ["vendor"]',
                "unknown instrument.toml fields",
            ),
        )
        for old, new, message in replacements:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as temporary:
                descriptor = load_instrument_manifest(
                    _write_instrument(
                        Path(temporary),
                        manifest=MANIFEST.replace(old, new, 1),
                    )
                )
                self.assertFalse(descriptor.valid)
                self.assertIn(message, descriptor.error)

    def test_manifest_rejects_unknown_or_missing_backend_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unknown = load_instrument_manifest(
                _write_instrument(
                    root,
                    manifest=MANIFEST.replace(
                        'backend = "backend:Driver"',
                        'backend = "package.backend:Driver"',
                    ),
                )
            )
            self.assertFalse(unknown.valid)
            self.assertIn("module:ClassName", unknown.error)

            missing_path = _write_instrument(root, name="missing")
            (missing_path / "backend.py").unlink()
            missing = load_instrument_manifest(missing_path)
            self.assertFalse(missing.valid)
            self.assertIn("backend source does not exist", missing.error)

    def test_config_field_types_defaults_ranges_and_choices_are_validated(self) -> None:
        replacements = (
            ('default = 1\nmin = 1', 'default = true\nmin = 1', "does not match"),
            ('default = 1\nmin = 1', 'default = 8\nmin = 1', "outside min/max"),
            ('options = ["auto", "manual"]', 'options = ["auto", "auto"]', "non-empty and unique"),
            ('type = "choice"', 'type = "mystery"', "unknown type"),
        )
        for old, new, message in replacements:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as temporary:
                descriptor = load_instrument_manifest(
                    _write_instrument(
                        Path(temporary),
                        manifest=MANIFEST.replace(old, new, 1),
                    )
                )
                self.assertFalse(descriptor.valid)
                self.assertIn(message, descriptor.error)

    def test_panel_references_and_safety_defaults_are_validated(self) -> None:
        replacements = (
            ('control = "loop1"', 'control = "missing"', "unknown control"),
            ('reading_options = ["temperature"]', 'reading_options = ["missing"]', "declared readings"),
            ('readings = ["heater"]', 'readings = ["missing"]', "declared reading"),
            ('min_value = 1.8', 'min_value = 500.0', "less than max_value"),
            ('default_rate_per_minute = 1.0', 'default_rate_per_minute = 40.0', "positive and ordered"),
            ('stability_timeout_seconds = 120.0', 'stability_timeout_seconds = 0.0', "stability values"),
        )
        for old, new, message in replacements:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as temporary:
                descriptor = load_instrument_manifest(
                    _write_instrument(
                        Path(temporary),
                        manifest=MANIFEST.replace(old, new, 1),
                    )
                )
                self.assertFalse(descriptor.valid)
                self.assertIn(message, descriptor.error)

    def test_switch_panel_references_generated_static_commands(self) -> None:
        switch = """\
[[panels]]
id = "switch"
label = "Output"
template = "switch"
reading = "heater"
commands = ["reset"]

"""
        with tempfile.TemporaryDirectory() as temporary:
            descriptor = load_instrument_manifest(
                _write_instrument(
                    Path(temporary),
                    manifest=MANIFEST.replace(
                        "[[sequence_commands]]",
                        switch + "[[sequence_commands]]",
                    ),
                )
            )
            self.assertTrue(descriptor.valid, descriptor.error)
            self.assertEqual(descriptor.panel("switch").commands, ("reset",))

        with tempfile.TemporaryDirectory() as temporary:
            descriptor = load_instrument_manifest(
                _write_instrument(
                    Path(temporary),
                    manifest=MANIFEST.replace(
                        "[[sequence_commands]]",
                        switch.replace('commands = ["reset"]', 'commands = ["missing"]')
                        + "[[sequence_commands]]",
                    ),
                )
            )
            self.assertFalse(descriptor.valid)
            self.assertIn("declared commands", descriptor.error)

    def test_discovery_validates_without_importing_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _copy_general(root)
            marker = root / "imported.txt"
            source = (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
                "class Driver: pass\n"
            )
            _write_instrument(root / "system_instruments", backend=source)

            descriptors = discover_system_instruments(config)

            self.assertEqual(len(descriptors), 1)
            self.assertTrue(descriptors[0].valid, descriptors[0].error)
            self.assertFalse(marker.exists())

    def test_discovery_marks_duplicate_ids_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _copy_general(root)
            instruments = root / "system_instruments"
            _write_instrument(instruments, name="first")
            _write_instrument(instruments, name="second")

            descriptors = discover_system_instruments(config)

            self.assertEqual(len(descriptors), 2)
            self.assertTrue(all(not descriptor.valid for descriptor in descriptors))
            self.assertTrue(all("Duplicate" in descriptor.error for descriptor in descriptors))

    def test_configured_descriptors_are_selected_once_per_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _copy_general(root)
            descriptor = load_instrument_manifest(_write_instrument(root))
            first = InstrumentConfig(
                id="first",
                display_name="First",
                kind=InstrumentKind.TEMPERATURE,
                backend="test_controller",
            )
            second = replace(first, id="second", display_name="Second")
            configured = replace(
                config,
                instrument_instances=(first, second),
            )

            self.assertEqual(
                configured_system_instruments(configured, (descriptor,)),
                (descriptor,),
            )

            unknown = replace(
                config,
                instrument_instances=(replace(first, backend="missing"),),
            )
            with self.assertRaisesRegex(ValueError, "unknown System Instrument"):
                configured_system_instruments(unknown, (descriptor,))


if __name__ == "__main__":
    unittest.main()
