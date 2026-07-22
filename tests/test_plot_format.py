from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.plot_format import (  # noqa: E402
    LINEAR_SCALE,
    LOG_SCALE,
    PLOT_FORMAT_MARKER,
    PlotFormat,
    PlotFormatError,
    find_plot_format,
    load_plot_format,
    plot_format_path,
    save_plot_format,
)


class PlotFormatTests(unittest.TestCase):
    def test_round_trip_uses_same_stem_plt_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_path = Path(temp) / "sample.dat"
            data_path.write_text("[Data]\nX,Y\n0,1\n", encoding="utf-8")
            expected = PlotFormat(
                data_file="sample.dat",
                layout="stacked",
                x_column="Time(s)",
                y_columns=("Temperature(K)", "Resistance(Ohm)"),
                x_range=(1.0, 9.0),
                overlay_y_range=(2.0, 12.0),
                stacked_y_ranges={
                    "Temperature(K)": (2.0, 4.0),
                    "Resistance(Ohm)": (100.0, 900.0),
                },
                x_scale=LOG_SCALE,
                y_scale=LOG_SCALE,
            )
            saved = save_plot_format(data_path, expected)
            self.assertEqual(saved, Path(temp) / "sample.plt")
            self.assertEqual(plot_format_path(data_path), saved)
            self.assertEqual(load_plot_format(saved), expected)
            payload = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(payload["layout"], "stacked")
            self.assertEqual(payload["y_axes"], ["Temperature(K)", "Resistance(Ohm)"])
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["x_scale"], LOG_SCALE)
            self.assertEqual(payload["y_scale"], LOG_SCALE)

    def test_version_one_defaults_to_linear_scales(self) -> None:
        legacy = {
            "format": PLOT_FORMAT_MARKER,
            "version": 1,
            "data_file": "legacy.dat",
            "layout": "overlay",
            "x_axis": "X",
            "y_axes": ["Y"],
            "zoom": {
                "x_range": [-5, 5],
                "overlay_y_range": [-10, 10],
                "stacked_y_ranges": {},
            },
        }
        settings = PlotFormat.from_dict(legacy)
        self.assertEqual(settings.x_scale, LINEAR_SCALE)
        self.assertEqual(settings.y_scale, LINEAR_SCALE)

    def test_finds_additive_name_but_prefers_canonical_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_path = Path(temp) / "sample.dat"
            data_path.touch()
            settings = PlotFormat("sample.dat", "overlay", None, ("Y",))
            additive = Path(str(data_path) + ".plt")
            additive.write_text(json.dumps(settings.to_dict()), encoding="utf-8")
            self.assertEqual(find_plot_format(data_path), additive)
            canonical = save_plot_format(data_path, settings)
            self.assertEqual(find_plot_format(data_path), canonical)

    def test_rejects_invalid_marker_layout_and_zoom(self) -> None:
        with self.assertRaises(PlotFormatError):
            PlotFormat("sample.dat", "grid", None, ("Y",))
        with self.assertRaises(PlotFormatError):
            PlotFormat("sample.dat", "overlay", None, ("Y",), x_range=(3.0, 2.0))
        with self.assertRaises(PlotFormatError):
            PlotFormat("sample.dat", "overlay", None, ("Y",), x_scale="square-root")
        with self.assertRaises(PlotFormatError):
            PlotFormat(
                "sample.dat",
                "overlay",
                None,
                ("Y",),
                x_range=(-1.0, 2.0),
                x_scale=LOG_SCALE,
            )
        with self.assertRaises(PlotFormatError):
            PlotFormat(
                "sample.dat",
                "overlay",
                None,
                ("Y",),
                overlay_y_range=(0.0, 2.0),
                y_scale=LOG_SCALE,
            )
        with self.assertRaises(PlotFormatError):
            PlotFormat.from_dict({"format": "foreign", "version": 1})


if __name__ == "__main__":
    unittest.main()
