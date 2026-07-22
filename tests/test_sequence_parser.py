from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.sequence.model import CommandType  # noqa: E402
from labcontrol.sequence.parser import load_sequence, parse_sequence, serialize_sequence  # noqa: E402


class SequenceParserTests(unittest.TestCase):
    def test_supplied_template_parses_without_issues(self) -> None:
        result = load_sequence(ROOT / "examples" / "template_original.seq")
        self.assertFalse(result.has_errors)
        self.assertEqual(result.issues, ())
        self.assertEqual(
            [command.type for command in result.document.commands],
            [CommandType.INITIALIZE, CommandType.SET_DATAFILE, CommandType.SCAN_TIME],
        )
        self.assertEqual(result.document.commands[2].children[0].type, CommandType.MEASURE)
        self.assertEqual(result.document.count_commands(), 4)

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


if __name__ == "__main__":
    unittest.main()
