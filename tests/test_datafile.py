from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SIMULATED_MODULE = (
    ROOT
    / "plugin_templates"
    / "measurement-modules-repository"
    / "modules"
    / "simulated_transport"
)
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
            module = load_manifest(SIMULATED_MODULE)
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
            time.sleep(0.001)
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
            event_rows = [
                line.split(",")
                for line in event_data.splitlines()
                if ",meter,OVERLOAD," in line
            ]
            self.assertEqual(len(event_rows), 2)
            raised_at = datetime.fromisoformat(event_rows[0][1])
            resolved_at = datetime.fromisoformat(event_rows[1][1])
            self.assertGreater(resolved_at, raised_at)

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

    def test_invalid_mode_and_schema_mismatch_preserve_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            (temp_root / "configs").mkdir()
            config_path = temp_root / "configs" / "default.toml"
            shutil.copy2(ROOT / "configs" / "default.toml", config_path)
            config = load_config(config_path)
            logger = DatRunLogger(config, EventManager())
            logger.open_run("safe.seq", "T End Sequence\n")
            target = temp_root / "existing.dat"
            sentinel = "[Data]\nOnly,Two\nkeep,this\n"
            target.write_text(sentinel, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown data file mode"):
                logger.set_datafile(
                    str(target), "typo", allow_external=True
                )
            self.assertEqual(target.read_text(encoding="utf-8"), sentinel)
            with self.assertRaisesRegex(ValueError, "different schema"):
                logger.set_datafile(
                    str(target), "open", allow_external=True
                )
            self.assertEqual(target.read_text(encoding="utf-8"), sentinel)
            logger.close()

    def test_matching_schema_appends_and_empty_run_creates_default_dat(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            (temp_root / "configs").mkdir()
            config_path = temp_root / "configs" / "default.toml"
            shutil.copy2(ROOT / "configs" / "default.toml", config_path)
            config = load_config(config_path)
            target = temp_root / "shared.dat"

            first = DatRunLogger(config, EventManager())
            first.open_run("first.seq", "T End Sequence\n")
            first.set_datafile(str(target), "create", allow_external=True)
            first.close()

            second = DatRunLogger(config, EventManager())
            second.open_run("second.seq", "T End Sequence\n")
            second.set_datafile(str(target), "open", allow_external=True)
            second.write_system_row({}, "Measure")
            second.close()
            data = target.read_text(encoding="utf-8")
            self.assertEqual(data.count("[Data]"), 1)
            self.assertIn(",Measure,", data)

            empty = DatRunLogger(config, EventManager())
            paths = empty.open_run("empty.seq", "T End Sequence\n")
            empty.close()
            self.assertTrue(paths.data_file.exists())
            self.assertIn("[Data]", paths.data_file.read_text(encoding="utf-8"))

    def test_run_directory_allocation_retries_an_atomic_creation_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            (temp_root / "configs").mkdir()
            config_path = temp_root / "configs" / "default.toml"
            shutil.copy2(ROOT / "configs" / "default.toml", config_path)
            config = load_config(config_path)
            runs_root = temp_root / "runs"
            original_mkdir = Path.mkdir
            injected_race = False

            def racing_mkdir(path, *args, **kwargs):
                nonlocal injected_race
                if (
                    not injected_race
                    and path.parent == runs_root
                    and path.name.endswith("_race")
                ):
                    injected_race = True
                    original_mkdir(path, *args, **kwargs)
                    raise FileExistsError(path)
                return original_mkdir(path, *args, **kwargs)

            logger = DatRunLogger(config, EventManager())
            with patch.object(Path, "mkdir", racing_mkdir):
                paths = logger.open_run("race.seq", "T End Sequence\n")
            logger.close()

            self.assertTrue(injected_race)
            self.assertTrue(paths.directory.name.endswith("_race_01"))
            self.assertTrue(paths.data_file.exists())


if __name__ == "__main__":
    unittest.main()
