from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication
from labcontrol.module_commands import normalize_module_commands, validate_module_command_parameters
from labcontrol.sequence.model import CommandType, SequenceDocument, SPECS_BY_TYPE, validate_command_parameters
from labcontrol.sequence.parser import parse_sequence, serialize_sequence
from labcontrol.ui.dialogs import CommandDialog, ManualControlDialog
from labcontrol.ui.widgets import PrecisionDoubleSpinBox
from tests.configuration_fixtures import load_simulated_config


class ParameterPrecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_time_editor_save_reload_have_the_same_value(self) -> None:
        for kind, field in ((CommandType.WAIT, "seconds"), (CommandType.SCAN_TIME, "duration_seconds")):
            with self.subTest(kind=kind):
                spec = SPECS_BY_TYPE[kind]
                command = spec.create()
                dialog = CommandDialog(command, spec)
                self.addCleanup(dialog.close)
                self.assertEqual(dialog.inputs[field].decimals(), 1)
                dialog.inputs[field].setValue(0.4)
                command.update_params(dialog.values())
                loaded = parse_sequence(serialize_sequence(SequenceDocument([command])))
                self.assertFalse(loaded.has_errors)
                self.assertEqual(loaded.document.commands[0].params[field], 0.4)

    def test_extra_precision_is_reported_and_never_serialized_as_zero(self) -> None:
        for kind, field in ((CommandType.WAIT, "seconds"), (CommandType.SCAN_TIME, "duration_seconds")):
            command = SPECS_BY_TYPE[kind].create()
            command.params[field] = 0.04
            text = serialize_sequence(SequenceDocument([command]))
            self.assertIn("0.04", text)
            self.assertTrue(validate_command_parameters(command))
            self.assertTrue(parse_sequence(text).has_errors)
            with self.assertRaisesRegex(ValueError, "decimal places"):
                CommandDialog(command, SPECS_BY_TYPE[kind])
            self.assertEqual(command.params[field], 0.04)

    def test_control_editors_share_input_precision_but_readout_keeps_metadata(self) -> None:
        config = load_simulated_config()
        for instrument in config.instrument_instances:
            if instrument.kind.value not in {"temperature", "field"}:
                continue
            panel = instrument.panel("main")
            manual = ManualControlDialog(instrument, panel)
            kind = CommandType.SET_TEMPERATURE if instrument.id == "temperature" else CommandType.SET_FIELD
            spec = SPECS_BY_TYPE[kind]
            dialog = CommandDialog(spec.create(), spec)
            self.addCleanup(manual.close)
            self.addCleanup(dialog.close)
            self.assertEqual(manual.target_input.decimals(), dialog.inputs["target"].decimals())
            self.assertEqual(manual.rate_input.decimals(), dialog.inputs["rate"].decimals())

    def test_module_time_and_nplc_precision_leave_filter_precision_unchanged(self) -> None:
        spec = normalize_module_commands("meter", [{
            "id": "configure", "label": "Configure", "kind": "command",
            "fields": [
                {"name": "settle_seconds", "type": "float", "default": 0.0, "unit": "s", "decimals": 3},
                {"name": "nplc", "type": "float", "default": 1.0, "decimals": 3},
                {"name": "filter_time", "type": "float", "default": 0.125, "unit": "s", "decimals": 3},
            ],
        }])[0]
        self.assertEqual([field.decimals for field in spec.fields], [1, 1, 3])
        self.assertFalse(validate_module_command_parameters(spec, {"settle_seconds": 0.1, "nplc": 1.2, "filter_time": 0.125}))
        self.assertTrue(validate_module_command_parameters(spec, {"settle_seconds": 0.01, "nplc": 1.2, "filter_time": 0.125}))

    def test_loaded_values_are_not_silently_rounded(self) -> None:
        box = PrecisionDoubleSpinBox()
        box.setDecimals(1)
        box.setValue(0.3)
        with self.assertRaises(ValueError):
            box.setValue(0.25)
        self.assertEqual(box.value(), 0.3)
        box.setValue(0.1 + 0.2)
        self.assertEqual(box.value(), 0.3)


if __name__ == "__main__":
    unittest.main()
