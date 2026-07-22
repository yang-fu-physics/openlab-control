from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.models import DeviceActivity, DeviceKind, DeviceSnapshot, Severity  # noqa: E402


class DatafileTests(unittest.TestCase):
    def test_writes_header_sparse_rows_and_event_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            (temp_root / "configs").mkdir()
            config_path = temp_root / "configs" / "default.toml"
            shutil.copy2(ROOT / "configs" / "default.toml", config_path)
            config = load_config(config_path)
            events = EventManager()
            logger = DatRunLogger(config, events)
            paths = logger.open_run("test.seq", "T Measure\nT End Sequence\n")
            self.assertFalse(any("2nd Stage" in column for column in logger._columns))
            now = time.monotonic()
            snapshots = {
                "temperature": DeviceSnapshot("temperature", "温度", DeviceKind.TEMPERATURE, now, True, "K", 3.1236, 3.0, 1.0, DeviceActivity.HOLDING),
                "field": DeviceSnapshot("field", "磁场", DeviceKind.FIELD, now, True, "Oe", 123.456, 100.0, 10.0, DeviceActivity.HOLDING),
            }
            logger.write_measurement(snapshots, {"R1": 1.2, "R2": 2.3}, "Measure")
            events.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
            events.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
            events.resolve("meter", "OVERLOAD")
            logger.close()
            data = paths.data_file.read_text(encoding="utf-8")
            event_data = paths.event_file.read_text(encoding="utf-8")
            self.assertIn("[Header]", data)
            self.assertIn("[Data]", data)
            self.assertIn("R1(Ohm)", data)
            self.assertIn("Field(Oe)", data)
            self.assertIn(",3.124,3.000,123.46,100.00,", data)
            self.assertEqual(sum(1 for line in data.splitlines() if ",Measure," in line), 2)
            self.assertIn("RAISED", event_data)
            self.assertIn("RESOLVED", event_data)
            self.assertIn(",2,", event_data)


if __name__ == "__main__":
    unittest.main()
