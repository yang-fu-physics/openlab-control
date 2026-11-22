from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.dat_reader import read_dat  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instruments.base import InstrumentError  # noqa: E402
from labcontrol.measurement.manifest import (  # noqa: E402
    ModuleColumn,
    ModuleDescriptor,
    ModuleInstanceConfig,
    build_instance_descriptor,
    discover_modules,
    load_module_instances,
    module_instances_path,
    save_module_instances,
)
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from tests.configuration_fixtures import write_simulated_configuration  # noqa: E402


def _copied_project(temp_root: Path):
    general = write_simulated_configuration(temp_root)
    shutil.copytree(ROOT / "modules", temp_root / "modules")
    return load_config(general)


class ModuleInstanceManifestAndServiceTests(unittest.TestCase):
    def test_save_load_and_discover_instances_sharing_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _copied_project(Path(temp))
            instances_file = module_instances_path(config)
            self.assertEqual(load_module_instances(instances_file), ())

            entries = (
                ModuleInstanceConfig("sim_gate", "simulated_transport", "Sim - Gate"),
                ModuleInstanceConfig("sim_sample", "simulated_transport", "Sim - Sample"),
            )
            save_module_instances(instances_file, entries)
            self.assertEqual(load_module_instances(instances_file), entries)

            descriptors = {item.id: item for item in discover_modules(config)}
            base = descriptors["simulated_transport"]
            gate = descriptors["sim_gate"]
            sample = descriptors["sim_sample"]
            self.assertFalse(base.is_instance)
            self.assertTrue(gate.valid and gate.is_instance)
            self.assertTrue(sample.valid and sample.is_instance)
            self.assertEqual(gate.template_id, "simulated_transport")
            self.assertEqual(gate.path, base.path)
            self.assertEqual(sample.path, base.path)
            self.assertEqual(gate.version, base.version)
            self.assertEqual(gate.name, "Sim - Gate")

            save_module_instances(instances_file, ())
            self.assertFalse(instances_file.exists())

    def test_invalid_duplicate_and_missing_template_instances_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _copied_project(Path(temp))
            instances_file = module_instances_path(config)
            save_module_instances(
                instances_file,
                (
                    ModuleInstanceConfig("dangling_inst", "missing_module", "Missing"),
                    ModuleInstanceConfig("simulated_transport", "simulated_transport", "Collision"),
                ),
            )
            descriptors = discover_modules(config)
            dangling = next(item for item in descriptors if item.id == "dangling_inst")
            self.assertFalse(dangling.valid)
            self.assertTrue(dangling.is_instance)
            self.assertIn("not installed", dangling.error)

            colliding = [item for item in descriptors if item.id == "simulated_transport"]
            self.assertEqual(len(colliding), 2)
            self.assertTrue(all(not item.valid and "Duplicate module id" in item.error for item in colliding))

            instances_file.write_text("[[instances]\nbroken = [", encoding="utf-8")
            broken = discover_modules(config)
            self.assertTrue(any(not item.valid and "module_instances.toml" in item.error for item in broken))

    def test_service_add_and_remove_descriptor_while_other_module_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _copied_project(Path(temp))
            descriptors = discover_modules(config)
            base = next(item for item in descriptors if item.id == "simulated_transport")

            class _Instruments:
                def __init__(self) -> None:
                    self.config = config

            service = MeasurementModuleService(
                descriptors,
                EventManager(),
                _Instruments(),  # type: ignore[arg-type]
            )
            service.records["simulated_transport"].enabled = True

            extra = build_instance_descriptor(
                base,
                ModuleInstanceConfig("sim_extra", "simulated_transport", "Sim Extra"),
                fallback_path=module_instances_path(config),
            )
            service.add_descriptor(extra)
            self.assertIn("sim_extra", service.records)
            self.assertTrue(service.records["simulated_transport"].enabled)

            with self.assertRaises(InstrumentError) as duplicate_exc:
                service.add_descriptor(extra)
            self.assertEqual(duplicate_exc.exception.code, "MODULE_INSTANCE_DUPLICATE")

            service.records["sim_extra"].enabled = True
            with self.assertRaises(InstrumentError) as enabled_exc:
                service.remove_descriptor("sim_extra")
            self.assertEqual(enabled_exc.exception.code, "MODULE_INSTANCE_REMOVE_BLOCKED")

            service.records["sim_extra"].enabled = False
            service.remove_descriptor("sim_extra")
            self.assertNotIn("sim_extra", service.records)
            # Removing a non-existent / invalid descriptor is a clean no-op.
            service.remove_descriptor("missing_invalid_instance")

            service._sequence_active = True
            with self.assertRaises(InstrumentError) as active_exc:
                service.add_descriptor(extra)
            self.assertEqual(active_exc.exception.code, "MODULE_OPERATION_DURING_SEQUENCE")

    def test_datafile_snapshots_module_instances_and_disambiguates_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _copied_project(Path(temp))
            config = replace(
                config,
                logging=replace(config.logging, compact_measurement_data=True),
            )
            instances_file = module_instances_path(config)
            save_module_instances(
                instances_file,
                (
                    ModuleInstanceConfig(
                        "simulated_transport_gate",
                        "simulated_transport",
                        "Simulated Transport - Gate",
                    ),
                    ModuleInstanceConfig(
                        "simulated_transport_sample",
                        "simulated_transport",
                        "Simulated Transport - Sample",
                    ),
                ),
            )
            descriptors = {item.id: item for item in discover_modules(config)}
            base = descriptors["simulated_transport"]
            gate = descriptors["simulated_transport_gate"]
            sample = descriptors["simulated_transport_sample"]
            for descriptor in (base, gate, sample):
                descriptor.columns = (ModuleColumn("R1", "Ohm"), ModuleColumn("Voltage", "V"))

            logger = DatRunLogger(config, EventManager())
            paths = logger.open_run(
                "multi-instance.seq",
                "T Measure\nT End Sequence\n",
                module_descriptors=(base, gate, sample),
            )
            logger.write_measurement_row(
                {},
                {
                    base.id: {"R1": 100.0, "Voltage": 0.1},
                    gate.id: {"R1": 200.0, "Voltage": 5.0},
                    sample.id: {"R1": 300.0, "Voltage": 0.2},
                },
                "Measure",
            )
            logger.close()
            snapshot = paths.directory / "configuration" / "module_instances.toml"
            self.assertTrue(snapshot.is_file())
            self.assertIn('id = "simulated_transport_gate"', snapshot.read_text(encoding="utf-8"))
            raw_dat = paths.data_file.read_text(encoding="utf-8")
            self.assertIn("template=simulated_transport", raw_dat)
            document = read_dat(paths.data_file)
            self.assertEqual(
                document.columns,
                (
                    "Timestamp",
                    "Temp",
                    "R1(Ohm)",
                    "Voltage(V)",
                    "gate.R1(Ohm)",
                    "gate.Voltage(V)",
                    "sample.R1(Ohm)",
                    "sample.Voltage(V)",
                ),
            )
            self.assertEqual(document.numeric_points("gate.Voltage(V)")[0].y, 5.0)
            self.assertEqual(document.numeric_points("sample.R1(Ohm)")[0].y, 300.0)


class ModuleInstanceUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.application = QApplication.instance() or QApplication([])

    def test_add_instance_dialog_suggests_unique_identity_and_validates(self) -> None:
        from PySide6.QtWidgets import QDialog

        from labcontrol.ui.measurement_modules import AddModuleInstanceDialog

        base = ModuleDescriptor("keithley_2400", "Keithley 2400", "1.0.0", Path("."))
        existing = {"keithley_2400", "keithley_2400_2"}
        dialog = AddModuleInstanceDialog((base,), existing)
        try:
            self.assertEqual(dialog.id_input.text(), "keithley_2400_3")
            self.assertEqual(dialog.name_input.text(), "Keithley 2400 #3")

            for bad_id in ("Bad-ID", "keithley_2400_2", ""):
                dialog.reject()
                dialog.id_input.setText(bad_id)
                with patch("labcontrol.ui.measurement_modules.QMessageBox.warning") as warn:
                    dialog.accept()
                warn.assert_called_once()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

            dialog.id_input.setText("keithley_2400_gate")
            dialog.name_input.setText("Keithley 2400 - Gate")
            dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(
                dialog.values(),
                ("keithley_2400", "keithley_2400_gate", "Keithley 2400 - Gate"),
            )
        finally:
            dialog.close()

    def test_module_manager_preserves_enabled_state_and_controls_remove_button(self) -> None:
        from PySide6.QtWidgets import QWidget

        from labcontrol.ui.measurement_modules import ModuleManagerDialog

        owner = QWidget()
        base = ModuleDescriptor("keithley_2400", "Keithley 2400", "1.0.0", Path("."))
        extra = ModuleDescriptor(
            "keithley_2400_gate",
            "Keithley 2400 - Gate",
            "1.0.0",
            Path("."),
            template_id="keithley_2400",
        )
        manager = ModuleManagerDialog((base,), owner)
        try:
            self.assertTrue(manager.add_instance_button.isEnabled())
            self.assertFalse(manager.remove_instance_button.isEnabled())

            manager.update_state("keithley_2400", True, "enabled", "Ready")
            manager.set_descriptors((base, extra), select_id="keithley_2400_gate")
            self.assertTrue(manager._checkboxes["keithley_2400"].isChecked())
            self.assertFalse(manager._checkboxes["keithley_2400_gate"].isChecked())
            self.assertTrue(manager.remove_instance_button.isEnabled())
            self.assertIn("Instance of keithley_2400", manager.detail_label.text())

            manager.update_state("keithley_2400_gate", True, "enabled", "Ready")
            self.assertFalse(manager.remove_instance_button.isEnabled())

            manager.update_state("keithley_2400_gate", False, "disabled", "Disabled")
            self.assertTrue(manager.remove_instance_button.isEnabled())

            manager.set_operations_enabled(False)
            self.assertFalse(manager.add_instance_button.isEnabled())
            self.assertFalse(manager.remove_instance_button.isEnabled())
        finally:
            manager.close()
            owner.close()


if __name__ == "__main__":
    unittest.main()
