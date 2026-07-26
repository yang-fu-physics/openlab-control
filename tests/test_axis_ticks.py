from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.axis_ticks import (  # noqa: E402
    is_timestamp_column,
    linear_ticks,
    numeric_tick_label,
    timestamp_axis_title,
    timestamp_reference,
    timestamp_tick_label,
    timestamp_ticks,
)
from labcontrol.datafile import (  # noqa: E402
    LABVIEW_UNIX_OFFSET_SECONDS,
)


class AxisTickTests(unittest.TestCase):
    def test_linear_ticks_use_only_nice_one_two_five_steps(self) -> None:
        temperature = linear_ticks(-16.0883, 317.812)
        self.assertEqual(temperature.step, 50.0)
        self.assertEqual(
            temperature.values,
            (
                0.0,
                50.0,
                100.0,
                150.0,
                200.0,
                250.0,
                300.0,
            ),
        )

        field = linear_ticks(-78_399.8, 78_399.6)
        self.assertEqual(field.step, 20_000.0)
        self.assertEqual(
            field.values,
            (
                -60_000.0,
                -40_000.0,
                -20_000.0,
                0.0,
                20_000.0,
                40_000.0,
                60_000.0,
            ),
        )
        self.assertEqual(
            numeric_tick_label(200.0, temperature.step),
            "200",
        )
        self.assertEqual(
            numeric_tick_label(20_000.0, field.step),
            "20,000",
        )
        self.assertEqual(
            numeric_tick_label(0.5, 0.1),
            "0.5",
        )
        self.assertEqual(
            numeric_tick_label(0.02, None),
            "0.02",
        )

    def test_quantum_design_file_open_time_calibrates_actual_time(
        self,
    ) -> None:
        header = (
            "[Header]",
            (
                "FILEOPENTIME,3589995867.25658,"
                "04/26/2025,6:56 pm"
            ),
        )
        raw_value = 3_589_998_029.84072
        reference = timestamp_reference(
            header,
            "Time Stamp (sec)",
            raw_value,
        )
        self.assertIsNotNone(reference)
        self.assertEqual(reference.source, "FILEOPENTIME")
        actual = reference.datetime_at(raw_value)
        self.assertEqual(
            (
                actual.year,
                actual.month,
                actual.day,
                actual.hour,
                actual.minute,
                actual.second,
            ),
            (2025, 4, 26, 19, 32, 2),
        )
        self.assertEqual(
            actual.microsecond // 1_000,
            584,
        )

        ticks = timestamp_ticks(
            raw_value,
            raw_value + 4 * 3_600,
            reference,
        )
        self.assertEqual(ticks.step, 3_600.0)
        labels = tuple(
            timestamp_tick_label(
                value,
                reference,
                raw_value,
                raw_value + 4 * 3_600,
                ticks.step,
            )
            for value in ticks.values
        )
        self.assertEqual(
            labels,
            ("20:00", "21:00", "22:00", "23:00"),
        )
        self.assertIn(
            "2025-04-26",
            timestamp_axis_title(
                "Time Stamp (sec)",
                raw_value,
                raw_value + 4 * 3_600,
                reference,
            ),
        )

    def test_openlab_epoch_metadata_supports_labview_and_unix(
        self,
    ) -> None:
        started = datetime.fromisoformat(
            "2025-04-26T18:56:00+08:00"
        )
        labview_sample = (
            started.timestamp()
            + LABVIEW_UNIX_OFFSET_SECONDS
            + 90
        )
        labview = timestamp_reference(
            (
                "[Header]",
                "TIMESTAMP_EPOCH,labview_1904",
                "INFO,Started: 2025-04-26T18:56:00+08:00",
            ),
            "Timestamp(s)",
            labview_sample,
        )
        self.assertIsNotNone(labview)
        self.assertEqual(
            labview.datetime_at(labview_sample),
            datetime(2025, 4, 26, 18, 57, 30),
        )

        unix_sample = started.timestamp() + 90
        unix = timestamp_reference(
            (
                "[Header]",
                "TIMESTAMP_EPOCH,unix",
                "INFO,Started: 2025-04-26T18:56:00+08:00",
            ),
            "Timestamp(s)",
            unix_sample,
        )
        self.assertIsNotNone(unix)
        self.assertEqual(
            unix.datetime_at(unix_sample),
            datetime(2025, 4, 26, 18, 57, 30),
        )

        misleading_legacy_header = timestamp_reference(
            (
                "[Header]",
                (
                    "; Timestamp(s) uses the LabVIEW 1904 "
                    "epoch for template compatibility."
                ),
                "INFO,Started: 2025-04-26T18:56:00+08:00",
            ),
            "Timestamp(s)",
            unix_sample,
        )
        self.assertIsNotNone(misleading_legacy_header)
        self.assertEqual(
            misleading_legacy_header.source,
            "OpenLab Started/unix",
        )
        self.assertEqual(
            misleading_legacy_header.datetime_at(
                unix_sample
            ),
            datetime(2025, 4, 26, 18, 57, 30),
        )

    def test_timestamp_ticks_cover_subsecond_zoom_and_midnight(
        self,
    ) -> None:
        raw_origin = datetime.fromisoformat(
            "2025-04-26T23:59:59+08:00"
        ).timestamp()
        reference = timestamp_reference(
            (
                "[Header]",
                "TIMESTAMP_EPOCH,unix",
                "INFO,Started: 2025-04-26T23:59:59+08:00",
            ),
            "Timestamp(s)",
            raw_origin,
        )
        self.assertIsNotNone(reference)

        subsecond = timestamp_ticks(
            raw_origin + 0.1,
            raw_origin + 0.9,
            reference,
        )
        self.assertEqual(subsecond.step, 0.2)
        self.assertGreaterEqual(len(subsecond.values), 3)
        self.assertTrue(
            all(
                "." in timestamp_tick_label(
                    value,
                    reference,
                    raw_origin + 0.1,
                    raw_origin + 0.9,
                    subsecond.step,
                )
                for value in subsecond.values
            )
        )

        midnight_low = raw_origin
        midnight_high = midnight_low + 90
        labels = tuple(
            timestamp_tick_label(
                value,
                reference,
                midnight_low,
                midnight_high,
                10.0,
            )
            for value in (
                midnight_low,
                midnight_low + 60,
            )
        )
        self.assertEqual(
            labels,
            (
                "04-26 23:59:59",
                "04-27 00:00:59",
            ),
        )

    def test_only_absolute_timestamp_names_are_converted(self) -> None:
        self.assertTrue(is_timestamp_column("Timestamp(s)"))
        self.assertTrue(
            is_timestamp_column("Time Stamp (sec)")
        )
        self.assertTrue(
            is_timestamp_column("Time Stamp (sec) #2")
        )
        self.assertFalse(is_timestamp_column("Time(s)"))
        self.assertFalse(is_timestamp_column("Averaging Time (sec)"))


if __name__ == "__main__":
    unittest.main()
