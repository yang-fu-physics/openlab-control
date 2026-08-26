from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import ConfigurationError, load_config  # noqa: E402
from labcontrol.instrument_resources import (  # noqa: E402
    InstrumentResource,
    InstrumentResourceError,
    load_instrument_resources,
    render_instrument_resources,
    write_instrument_resources,
)
from labcontrol.instruments.manifest import (  # noqa: E402
    discover_system_instruments,
)
from labcontrol.paths import default_config_path  # noqa: E402


EXTERNAL_MANIFEST = """\
id = "dual_controller"
name = "Dual Controller"
version = "1.0.0"
api_version = "4"
backend = "backend:Driver"
kinds = ["temperature"]

[discovery]
identity_pattern = "^ACME,DUAL,"

[[config_fields]]
id = "pid_file"
label = "PID File"
type = "pid_file"
default = "configs/pid/default.toml"

[[controls]]
id = "loop1"
label = "Loop 1"

[[panels]]
id = "control"
label = "Sample Temperature"
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

[readings.temperature]
label = "Temperature"
unit = "K"
decimals = 3

[readings.heater]
label = "Heater"
unit = "%"
decimals = 1
"""


EXTERNAL_GENERATED = """\
id = "dual_controller"
name = "Dual Controller"
version = "1.0.0"
api_version = "4"
backend = "backend:Driver"
kinds = ["temperature"]

[discovery]
identity_pattern = "^ACME,DUAL,"

[[config_fields]]
id = "pid_file"
label = "PID File"
type = "pid_file"
default = "configs/pid/default.toml"

[[controls]]
id = "loop1"
label = "Loop 1"

[readings.temperature]
label = "Temperature"
unit = "K"
decimals = 3

[readings.heater]
label = "Heater"
unit = "%"
decimals = 1

[[instances]]
id = "sample_controller"
resource = "GPIB0::1::INSTR"
identity = "ACME,DUAL,123"
pid_file = "configs/pid/sample_controller.toml"

[[instances.panels]]
id = "control"
enabled = true
order = 1
role = "sample_temp"
reading = "temperature"

[[instances.panels]]
id = "heater"
enabled = true
order = 2
role = "none"
"""


def _copy_general(root: Path) -> Path:
    configs = root / "configs"
    configs.mkdir(parents=True)
    destination = configs / "general.toml"
    shutil.copy2(ROOT / "configs" / "general.toml", destination)
    return destination


def _write_external_configuration(root: Path) -> Path:
    general = _copy_general(root)
    instrument = root / "system_instruments" / "dual_controller"
    instrument.mkdir(parents=True)
    (instrument / "instrument.toml").write_text(
        EXTERNAL_MANIFEST,
        encoding="utf-8",
    )
    (instrument / "backend.py").write_text(
        "class Driver: pass\n",
        encoding="utf-8",
    )
    pid = root / "configs" / "pid" / "sample_controller.toml"
    pid.parent.mkdir()
    pid.write_text("zones = []\n", encoding="utf-8")
    generated = root / "configs" / "instruments"
    generated.mkdir()
    (generated / "dual_controller.toml").write_text(
        EXTERNAL_GENERATED,
        encoding="utf-8",
    )
    return general


def _simulation_document(
    file_id: str,
    instance_id: str,
    label: str,
    kind: str,
    backend: str,
    order: int,
    role: str,
) -> str:
    unit = "Oe" if kind == "field" else "K"
    if kind == "monitor":
        template = f"""\
[[panels]]
id = "main"
label = "{label}"
template = "readout"
readings = ["value"]
"""
        configured_panel = f"""\
[[instances.panels]]
id = "main"
enabled = true
order = {order}
role = "none"
"""
    else:
        minimum, maximum, default_rate, maximum_rate = (
            (1.8, 400.0, 10.0, 30.0)
            if kind == "temperature"
            else (-90000.0, 90000.0, 5000.0, 10000.0)
        )
        template = f"""\
[[controls]]
id = "main"
label = "{label}"

[[panels]]
id = "main"
label = "{label}"
template = "controller"
control = "main"
reading_options = ["value"]
default_reading = "value"
min_value = {minimum}
max_value = {maximum}
default_rate_per_minute = {default_rate}
max_rate_per_minute = {maximum_rate}
stability_tolerance = 0.05
stability_max_slope_per_minute = 0.03
stability_dwell_seconds = 1.0
stability_timeout_seconds = 120.0
stability_window_seconds = 1.0
"""
        configured_panel = f"""\
[[instances.panels]]
id = "main"
enabled = true
order = {order}
role = "{role}"
reading = "value"
"""
    initial = (
        4.2 if kind == "monitor" else 300.0 if kind == "temperature" else 0.0
    )
    return f"""\
id = "{file_id}"
name = "{label}"
version = "1.0.0"
api_version = "4"
backend = "{backend}"
kinds = ["{kind}"]

{template}
[readings.value]
label = "{label}"
unit = "{unit}"
decimals = 3

[[instances]]
id = "{instance_id}"
initial_value = {initial}
noise = 0.0

{configured_panel}"""


class InstrumentResourceTests(unittest.TestCase):
    def test_default_path_is_only_general_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = root / "configs"
            configs.mkdir()
            (configs / "site.local.toml").write_text("", encoding="utf-8")
            (configs / "default.toml").write_text("", encoding="utf-8")
            with patch("labcontrol.paths.project_root", return_value=root):
                self.assertEqual(default_config_path(), configs / "general.toml")

    def test_clean_general_loads_without_local_or_system_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _copy_general(root)

            config = load_config(general)

            self.assertEqual(config.instrument_instances, ())
            self.assertEqual(config.instrument_resources, ())
            self.assertEqual(config.panels, ())
            self.assertEqual(discover_system_instruments(config), ())
            self.assertFalse((root / "system_instruments").exists())

    def test_general_rejects_old_inline_instruments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _copy_general(root)
            general.write_text(
                general.read_text(encoding="utf-8")
                + '\n[[instruments]]\nid = "legacy"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "Unknown general configuration fields: instruments",
            ):
                load_config(general)

    def test_resources_are_a_standalone_measurement_inventory(self) -> None:
        resources = (
            InstrumentResource("meter", "TCPIP0::meter::INSTR", "ACME,METER"),
        )
        rendered = render_instrument_resources(resources)
        self.assertNotIn("purpose", rendered)
        self.assertNotIn("system_instrument", rendered)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "visa.resources.toml"
            write_instrument_resources(path, resources)
            self.assertEqual(load_instrument_resources(path), resources)
            self.assertEqual(
                resources[0].public_payload(),
                {
                    "id": "meter",
                    "address": "TCPIP0::meter::INSTR",
                    "identity": "ACME,METER",
                    "purpose": "measurement",
                },
            )

    def test_resource_inventory_rejects_old_assignment_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "visa.resources.toml"
            path.write_text(
                "[[resources]]\n"
                'id = "legacy"\n'
                'address = "GPIB0::1::INSTR"\n'
                'purpose = "system"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                InstrumentResourceError,
                "unknown fields: purpose",
            ):
                load_instrument_resources(path)

    def test_measurement_payload_always_carries_purpose_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _copy_general(root)
            write_instrument_resources(
                root / "configs" / "visa.resources.toml",
                (InstrumentResource("meter", "GPIB0::8::INSTR", "METER"),),
            )
            config = load_config(general)
            self.assertEqual(
                config.resource_payload("measurement")["meter"]["purpose"],
                "measurement",
            )
            self.assertEqual(config.resource_payload("system"), {})

    def test_generated_metadata_is_used_and_panels_come_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            manifest = root / "system_instruments" / "dual_controller" / "instrument.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'label = "Temperature"', 'label = "Changed Source Label"'
                ),
                encoding="utf-8",
            )

            config = load_config(general)

            self.assertEqual(len(config.instrument_instances), 1)
            instance = config.instrument("sample_controller")
            self.assertEqual(instance.reading("temperature").display_name, "Temperature")
            self.assertEqual([panel.id for panel in instance.panels], ["control", "heater"])
            self.assertEqual(instance.panel("control").control_id, "loop1")
            self.assertEqual(
                [(panel.id, panel.order, panel.role) for panel in config.panels],
                [("control", 1, "sample_temp"), ("heater", 2, "none")],
            )

    def test_blank_identity_is_allowed_but_wrong_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            generated = root / "configs" / "instruments" / "dual_controller.toml"
            original = generated.read_text(encoding="utf-8")
            generated.write_text(
                original.replace("ACME,DUAL,123", ""),
                encoding="utf-8",
            )
            self.assertEqual(
                len(load_config(general).instrument_instances),
                1,
            )

            generated.write_text(
                original.replace("ACME,DUAL,123", "OTHER,MODEL,123"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "identity does not match"):
                load_config(general)

    def test_instance_id_uses_the_scanner_identifier_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            generated = root / "configs" / "instruments" / "dual_controller.toml"
            generated.write_text(
                generated.read_text(encoding="utf-8").replace(
                    'id = "sample_controller"', 'id = "Sample Controller"'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, r"must match \[a-z\]"):
                load_config(general)

    def test_string_config_fields_must_be_filled_in(self) -> None:
        field = """\
[[config_fields]]
id = "host"
label = "Host"
type = "string"
default = ""

"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            manifest = root / "system_instruments" / "dual_controller" / "instrument.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "[[controls]]", field + "[[controls]]", 1
                ),
                encoding="utf-8",
            )
            generated = root / "configs" / "instruments" / "dual_controller.toml"
            generated.write_text(
                generated.read_text(encoding="utf-8").replace(
                    "[[controls]]", field + "[[controls]]", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "host must not be empty"):
                load_config(general)

    def test_pid_paths_are_project_relative_confined_and_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            general = _write_external_configuration(root)
            generated = root / "configs" / "instruments" / "dual_controller.toml"
            original = generated.read_text(encoding="utf-8")
            config = load_config(general)
            self.assertEqual(
                config.instrument_instances[0].extras["pid_file"],
                str((root / "configs" / "pid" / "sample_controller.toml").resolve()),
            )

            generated.write_text(
                original.replace(
                    "configs/pid/sample_controller.toml", "configs/pid/missing.toml"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "does not exist"):
                load_config(general)

            outside = base / "outside.toml"
            outside.write_text("zones = []\n", encoding="utf-8")
            generated.write_text(
                original.replace(
                    "configs/pid/sample_controller.toml",
                    str(outside).replace("\\", "/"),
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "project root"):
                load_config(general)

    def test_generated_template_must_be_api_v4_and_match_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            generated = root / "configs" / "instruments" / "dual_controller.toml"
            original = generated.read_text(encoding="utf-8")
            generated.write_text(
                original.replace('api_version = "4"', 'api_version = "3"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "invalid generated"):
                load_config(general)

            generated.write_text(
                original.replace('backend = "backend:Driver"', 'backend = "other:Driver"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "backend does not match"):
                load_config(general)

    def test_panel_order_and_roles_are_global_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            generated = root / "configs" / "instruments" / "dual_controller.toml"
            original = generated.read_text(encoding="utf-8")
            generated.write_text(
                original.replace("order = 2", "order = 3"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "continuous from 1"):
                load_config(general)

            generated.write_text(
                original.replace('role = "none"', 'role = "sample_temp"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "non-controller panels require role none",
            ):
                load_config(general)

    def test_disabled_panel_cannot_keep_order_or_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            generated = root / "configs" / "instruments" / "dual_controller.toml"
            generated.write_text(
                generated.read_text(encoding="utf-8").replace(
                    'enabled = true\norder = 2\nrole = "none"',
                    'enabled = false\norder = 2\nrole = "none"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "must not declare order"):
                load_config(general)

    def test_disabled_controller_keeps_its_default_physical_main_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            generated = root / "configs" / "instruments" / "dual_controller.toml"
            generated.write_text(
                generated.read_text(encoding="utf-8")
                .replace(
                    'enabled = true\norder = 1\nrole = "sample_temp"\n'
                    'reading = "temperature"',
                    "enabled = false",
                )
                .replace("order = 2", "order = 1"),
                encoding="utf-8",
            )

            instance = load_config(general).instrument_instances[0]

            self.assertEqual(instance.main_reading, "temperature")
            self.assertEqual(instance.panel("control").template, "controller")
            self.assertFalse(instance.control_enabled)

    def test_three_generated_builtin_simulations_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _copy_general(root)
            generated = root / "configs" / "instruments"
            generated.mkdir()
            documents = (
                (
                    "simulated_temperature",
                    "temperature",
                    "Simulated Temperature",
                    "temperature",
                    "labcontrol.instruments.simulated:SimulatedTemperatureController",
                    1,
                    "sample_temp",
                ),
                (
                    "simulated_field",
                    "field",
                    "Simulated Magnetic Field",
                    "field",
                    "labcontrol.instruments.simulated:SimulatedFieldController",
                    2,
                    "field",
                ),
                (
                    "simulated_second_stage",
                    "second_stage",
                    "Simulated 2nd Stage",
                    "monitor",
                    "labcontrol.instruments.simulated:SimulatedReadOnlyMonitor",
                    3,
                    "none",
                ),
            )
            for values in documents:
                (generated / f"{values[0]}.toml").write_text(
                    _simulation_document(*values),
                    encoding="utf-8",
                )

            config = load_config(general)

            self.assertEqual(
                [instance.id for instance in config.instrument_instances],
                ["temperature", "field", "second_stage"],
            )
            self.assertEqual([panel.order for panel in config.panels], [1, 2, 3])

    def test_system_and_measurement_addresses_cannot_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            general = _write_external_configuration(root)
            write_instrument_resources(
                root / "configs" / "visa.resources.toml",
                (InstrumentResource("meter", "GPIB0::1::INSTR", "METER"),),
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "assigned to both System Instrument",
            ):
                load_config(general)


if __name__ == "__main__":
    unittest.main()
