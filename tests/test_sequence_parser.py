from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.sequence.model import SPECS_BY_TYPE, Command, CommandType  # noqa: E402
from labcontrol.sequence.parser import (  # noqa: E402
    format_command,
    load_sequence,
    parse_temperature_points,
    parse_sequence,
    serialize_sequence,
)


class SequenceParserTests(unittest.TestCase):
    def test_original_legacy_template_is_preserved_but_rejected(self) -> None:
        result = load_sequence(ROOT / "examples" / "template_original.seq")
        self.assertTrue(result.has_errors)
        self.assertIn("Initialize is no longer", result.issues[0].message)
        self.assertEqual(result.document.commands[0].type, CommandType.UNKNOWN)
        self.assertEqual(result.document.commands[2].children[0].type, CommandType.MEASURE)
        self.assertEqual(result.document.count_commands(), 4)

    def test_new_module_measurement_example_parses_without_issues(self) -> None:
        result = load_sequence(ROOT / "examples" / "module_measurement.seq")
        self.assertFalse(result.has_errors)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.document.commands[2].children[0].params, {})

    def test_template_round_trip_preserves_lines(self) -> None:
        source = (ROOT / "examples" / "template_original.seq").read_text(encoding="utf-8")
        result = parse_sequence(source, "template.seq")
        self.assertEqual(serialize_sequence(result.document), source)

    def test_arbitrary_nested_scans(self) -> None:
        result = load_sequence(ROOT / "examples" / "nested_scan.seq")
        self.assertFalse(result.has_errors)
        temperature_scan = next(
            item for item in result.document.commands if item.type is CommandType.SCAN_TEMPERATURE
        )
        self.assertEqual(temperature_scan.children[0].type, CommandType.SCAN_FIELD)
        self.assertEqual(temperature_scan.children[0].children[0].type, CommandType.MEASURE)

    def test_unknown_line_is_retained_as_warning(self) -> None:
        result = parse_sequence("T Vendor Specific Command 1 2 3\nT End Sequence\n")
        self.assertFalse(result.has_errors)
        self.assertEqual(result.document.commands[0].type, CommandType.UNKNOWN)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("Vendor Specific", serialize_sequence(result.document))

    def test_unbalanced_scan_is_error(self) -> None:
        result = parse_sequence("T Scan Time 1.0 secs in 2 steps\nT Measure\nT End Sequence\n")
        self.assertTrue(result.has_errors)

    def test_disabled_f_lines_round_trip_and_disable_nested_block(self) -> None:
        source = (
            "F Scan Time 1.0 secs in 2 steps\n"
            "T     Measure\n"
            "T End Scan\n"
            "F Remark disabled note\n"
            "T Wait For 0.0 secs\n"
            "T End Sequence\n"
        )
        result = parse_sequence(source, "disabled.seq")
        self.assertEqual(result.issues, ())
        self.assertFalse(result.document.commands[0].enabled)
        self.assertTrue(result.document.commands[0].children[0].enabled)
        self.assertFalse(result.document.commands[1].enabled)
        self.assertTrue(result.document.commands[2].enabled)
        self.assertEqual(result.document.count_commands(), 4)
        self.assertEqual(result.document.count_commands(enabled_only=True), 1)
        self.assertEqual(serialize_sequence(result.document), source)

    def test_new_field_commands_default_to_oe_with_two_decimals(self) -> None:
        command = SPECS_BY_TYPE[CommandType.SCAN_FIELD].create()
        self.assertEqual(command.params["unit"], "Oe")
        self.assertEqual(
            format_command(command),
            "Scan Field 0.00 Oe to 10000.00 Oe in 11 steps at 5000.00 Oe/min, Settle",
        )

    def test_temperature_and_legacy_t_command_precision(self) -> None:
        temperature = SPECS_BY_TYPE[CommandType.SET_TEMPERATURE].create()
        self.assertEqual(
            format_command(temperature),
            "Set Temperature 300.000 K at 5.000 K/min in Settle mode",
        )
        legacy_t = Command(
            CommandType.SET_FIELD,
            {"target": 0.001234, "unit": "T", "rate": 0.000567, "mode": "Sweep"},
        )
        self.assertEqual(
            format_command(legacy_t),
            "Set Field 0.001234 T at 0.000567 T/min in Sweep mode",
        )

    def test_temperature_list_scan_parses_and_round_trips(self) -> None:
        source = (
            "T Scan Temperature List 300.000, 250.500, 250.500, 20.000 K at 5.000 K/min, Settle\n"
            "T     Measure\n"
            "T End Scan\n"
            "T End Sequence\n"
        )
        result = parse_sequence(source, "temperature-list.seq")
        self.assertEqual(result.issues, ())
        command = result.document.commands[0]
        self.assertEqual(command.type, CommandType.SCAN_TEMPERATURE)
        self.assertEqual(command.params["point_mode"], "List")
        self.assertEqual(
            parse_temperature_points(command.params["points"]),
            (300.0, 250.5, 250.5, 20.0),
        )
        self.assertEqual(command.children[0].type, CommandType.MEASURE)
        self.assertEqual(serialize_sequence(result.document), source)

    def test_measure_parameters_are_rejected(self) -> None:
        result = parse_sequence("T Measure devices=transport\nT End Sequence\n")
        self.assertTrue(result.has_errors)
        self.assertEqual(result.document.commands[0].type, CommandType.UNKNOWN)
        self.assertIn("has no parameters", result.issues[0].message)

    def test_temperature_list_command_is_canonicalized_when_edited(self) -> None:
        command = Command(
            CommandType.SCAN_TEMPERATURE,
            {
                "device_id": "temperature",
                "point_mode": "List",
                "points": "300, 299.9, 300",
                "rate": 10.0,
                "mode": "Sweep",
            },
        )
        self.assertEqual(
            format_command(command),
            "Scan Temperature List 300.000, 299.900, 300.000 K at 10.000 K/min, Sweep",
        )

    def test_invalid_temperature_list_is_a_parse_error(self) -> None:
        result = parse_sequence(
            "T Scan Temperature List 300,,20 K at 5 K/min, Settle\nT End Sequence\n"
        )
        self.assertTrue(result.has_errors)
        self.assertEqual(result.document.commands[0].type, CommandType.UNKNOWN)
        self.assertIn("point 2 is empty", result.issues[0].message)

    def test_custom_datafile_folder_marker_round_trips(self) -> None:
        source = (
            "T Set Datafile open|create external C:\\Experiment Data\\sample.dat\n"
            "T End Sequence\n"
        )
        result = parse_sequence(source, "custom-data.seq")
        self.assertEqual(result.issues, ())
        command = result.document.commands[0]
        self.assertEqual(command.params["path_scope"], "Custom folder")
        self.assertEqual(command.params["path"], "C:\\Experiment Data\\sample.dat")
        self.assertEqual(serialize_sequence(result.document), source)

        command.update_params(command.params)
        self.assertEqual(
            format_command(command),
            "Set Datafile open|create external C:\\Experiment Data\\sample.dat",
        )


if __name__ == "__main__":
    unittest.main()
