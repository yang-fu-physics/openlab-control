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
from labcontrol.measurement.manifest import load_manifest  # noqa: E402


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
            module = load_manifest(ROOT / "modules" / "simulated_transport")
            paths = logger.open_run(
                "test.seq",
                "T Measure\nT End Sequence\n",
                (module,),
                {module.id: {"delay_seconds": 0.01}},
                {module.id: {"Connection": "Connected"}},
            )
            now = time.monotonic()
            snapshots = {
                "temperature": DeviceSnapshot("temperature", "温度", DeviceKind.TEMPERATURE, now, True, "K", 3.1236, 3.0, 1.0, DeviceActivity.HOLDING),
                "field": DeviceSnapshot("field", "磁场", DeviceKind.FIELD, now, True, "Oe", 123.456, 100.0, 10.0, DeviceActivity.HOLDING),
                "second_stage": DeviceSnapshot("second_stage", "2nd Stage", DeviceKind.MONITOR, now, True, "K", 4.2345),
            }
            logger.write_module_row(
                snapshots, module.id, {"R1": 1.2, "Status": "OK"}, "Measure"
            )
            logger.write_module_row(
                snapshots, module.id, {"R2": 2.3, "Status": "OK"}, "Measure"
            )
            events.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
            events.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
            events.resolve("meter", "OVERLOAD")
            logger.close()
            data = paths.data_file.read_text(encoding="utf-8")
            event_data = paths.event_file.read_text(encoding="utf-8")
            self.assertIn("[Header]", data)
            self.assertIn("[Data]", data)
            self.assertIn("simulated_transport.R1(Ohm)", data)
            self.assertIn("Field(Oe)", data)
            self.assertIn("second_stage(K)", data)
            self.assertIn(",3.124,3.000,123.46,100.00,4.234,", data)
            self.assertEqual(sum(1 for line in data.splitlines() if ",Measure," in line), 2)
            self.assertTrue((paths.module_settings_directory / f"{module.id}.settings.toml").exists())
            self.assertTrue((paths.module_settings_directory / f"{module.id}.status-at-start.json").exists())
            self.assertIn("RAISED", event_data)
            self.assertIn("RESOLVED", event_data)
            self.assertIn(",2,", event_data)

    def test_explicit_custom_folder_is_allowed_without_weakening_legacy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            (temp_root / "configs").mkdir()
            config_path = temp_root / "configs" / "default.toml"
            shutil.copy2(ROOT / "configs" / "default.toml", config_path)
            config = load_config(config_path)

            custom_events = EventManager()
            custom_notices = []
            custom_events.subscribe(custom_notices.append)
            custom_logger = DatRunLogger(config, custom_events)
            custom_logger.open_run("custom.seq", "T End Sequence\n")
            custom_path = temp_root / "chosen folder" / "custom.dat"
            destination = custom_logger.set_datafile(
                str(custom_path),
                "create",
                allow_external=True,
            )
            custom_logger.close()
            self.assertEqual(destination, custom_path)
            self.assertTrue(custom_path.exists())
            self.assertNotIn(
                "DATAFILE_RELOCATED",
                [notice.event.code for notice in custom_notices if not notice.is_resolution],
            )

            safe_events = EventManager()
            safe_notices = []
            safe_events.subscribe(safe_notices.append)
            safe_logger = DatRunLogger(config, safe_events)
            safe_paths = safe_logger.open_run("legacy.seq", "T End Sequence\n")
            redirected = safe_logger.set_datafile(str(temp_root / "legacy.dat"), "create")
            safe_logger.close()
            self.assertEqual(redirected, safe_paths.directory / "legacy.dat")
            self.assertIn(
                "DATAFILE_RELOCATED",
                [notice.event.code for notice in safe_notices if not notice.is_resolution],
            )


if __name__ == "__main__":
    unittest.main()
