from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.dat_reader import DatReadError, read_dat  # noqa: E402


class DatReaderTests(unittest.TestCase):
    def test_reads_supplied_sparse_template(self) -> None:
        document = read_dat(ROOT / "examples" / "template_original.dat")
        self.assertEqual(document.columns[:3], ("Timestamp(s)", "Time(s)", "Temp(K)"))
        self.assertEqual(len(document.rows), 2458)
        self.assertIn("R4(Ohm)", document.numeric_columns())
        self.assertGreater(len(document.numeric_series("R1(Ohm)", "Time(s)")), 300)

    def test_pads_short_rows_and_renames_duplicate_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.dat"
            path.write_text(
                "[Header]\nINFO, test\n[Data]\nTime(s),Value,Value\n0,1\n1,,3\n",
                encoding="utf-8",
            )
            document = read_dat(path)
            self.assertEqual(document.columns, ("Time(s)", "Value", "Value #2"))
            self.assertEqual(document.rows[0], ("0", "1", ""))
            self.assertEqual(document.numeric_series("Value #2", "Time(s)"), ((1.0, 3.0),))

    def test_rejects_file_without_data_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "invalid.dat"
            path.write_text("[Header]\nINFO, test\n", encoding="utf-8")
            with self.assertRaises(DatReadError):
                read_dat(path)

    def test_numeric_points_keep_the_source_row(self) -> None:
        document = read_dat(ROOT / "examples" / "template_original.dat")
        points = document.numeric_points("R2(Ohm)", "Time(s)")
        self.assertGreater(len(points), 300)
        first = points[0]
        self.assertEqual(first.row_index, 1)
        self.assertEqual(first.row[4], "0.550700000")
        self.assertEqual((first.x, first.y), (13.23, 0.5507))


if __name__ == "__main__":
    unittest.main()
