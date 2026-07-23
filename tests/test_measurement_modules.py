from __future__ import annotations

import asyncio
import csv
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from labcontrol.config import load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.devices.base import DeviceError  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.measurement.manifest import ModuleColumn, ModuleDescriptor, discover_modules  # noqa: E402
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.measurement.settings import load_settings, save_settings  # noqa: E402
from labcontrol.measurement.worker import WorkerRequestError  # noqa: E402
from labcontrol.plugins import DeviceManager  # noqa: E402
from labcontrol.ui.measurement_modules import (  # noqa: E402
    MODULE_WINDOW_MIN_HEIGHT,
    MODULE_WINDOW_MIN_WIDTH,
    ModuleWindow,
)
from labcontrol.ui.scaling import scaled  # noqa: E402


def copied_project(temp_root: Path):
    (temp_root / "configs").mkdir()
    shutil.copy2(ROOT / "configs" / "default.toml", temp_root / "configs" / "default.toml")
    shutil.copytree(ROOT / "modules", temp_root / "modules")
    return load_config(temp_root / "configs" / "default.toml")


class ManifestAndSettingsTests(unittest.TestCase):
    def test_discovers_simulated_module_and_round_trips_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = copied_project(Path(temp))
            descriptors = discover_modules(config)
            self.assertEqual([item.id for item in descriptors], ["simulated_transport"])
            descriptor = descriptors[0]
            self.assertTrue(descriptor.valid)
            self.assertEqual(
                [column.name for column in descriptor.columns],
                ["R1", "R2", "R3", "R4", "Status", "Warning"],
            )
            path = Path(temp) / "module_data" / descriptor.id / "settings.toml"
            original = {"range": 10.0, "enabled": True, "channels": [1, 2], "nested": {"name": "R1"}}
            save_settings(path, original)
            self.assertEqual(load_settings(path), original)

    def test_marks_incompatible_shared_dependency_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            shutil.copy2(ROOT / "configs" / "default.toml", root / "configs" / "default.toml")
            for module_id, dependency in (("first", "demo-package<2"), ("second", "demo-package>=2")):
                folder = root / "modules" / module_id
                folder.mkdir(parents=True)
                (folder / "module.toml").write_text(
                    "\n".join([
                        f'id = "{module_id}"',
                        f'name = "{module_id.title()}"',
                        'version = "1.0.0"',
                        'api_version = "1.0"',
                        'frontend = "frontend:Frontend"',
                        'backend = "backend:Backend"',
                        f'dependencies = ["{dependency}"]',
                        "[[columns]]",
                        'name = "Value"',
                    ]) + "\n",
                    encoding="utf-8",
                )
            descriptors = discover_modules(load_config(root / "configs" / "default.toml"))
            self.assertEqual(len(descriptors), 2)
            self.assertTrue(all("Dependency conflict" in item.dependency_error for item in descriptors))
            self.assertTrue(all(not item.can_enable for item in descriptors))


class ModuleServiceTests(unittest.TestCase):
    class _FailingClient:
        def __init__(self, failing_action: str) -> None:
            self.failing_action = failing_action
            self.actions: list[str] = []

        def request(self, action, payload=None, event_handler=None):
            del payload, event_handler
            self.actions.append(action)
            if action == self.failing_action:
                raise WorkerRequestError(
                    f"{action} failed", f"{action.upper()}_FAILED", "test"
                )
            return {}

        def close(self) -> None:
            self.actions.append("close")

    class _BarrierClient:
        def __init__(self, barrier: threading.Barrier, value: float) -> None:
            self.barrier = barrier
            self.value = value

        def request(self, action, payload=None, event_handler=None):
            del payload
            if action == "measure":
                self.barrier.wait(timeout=2.0)
                assert event_handler is not None
                event_handler({"type": "row", "values": {"Value": self.value}})
            return {}

        def close(self) -> None:
            return None

    def test_full_lifecycle_streams_four_ordered_rows_and_disables_cleanly(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            devices = DeviceManager(config, events)
            modules = MeasurementModuleService(discover_modules(config), events, devices)
            logger = DatRunLogger(config, events)
            await devices.connect_all()
            await devices.poll_all()
            settings = {
                "delay_seconds": 0.001,
                "noise_ohm": 0.0,
                "warning_threshold_ohm": 1e9,
            }
            try:
                await modules.enable("simulated_transport", settings)
                record = modules.records["simulated_transport"]
                self.assertTrue(record.enabled)
                self.assertEqual(record.status["Applied Settings"], "Not applied")
                await modules.apply_settings("simulated_transport", settings)
                await modules.manual_action("simulated_transport", "measure_now", {})
                descriptors, statuses = await modules.prepare_sequence({"simulated_transport": settings})
                paths = logger.open_run(
                    "module.seq",
                    "T Measure\nT End Sequence\n",
                    descriptors,
                    {"simulated_transport": settings},
                    statuses,
                )
                await modules.begin_sequence()
                with self.assertRaises(DeviceError):
                    await modules.manual_action("simulated_transport", "measure_now", {})
                await modules.measure_all(logger, "1:Measure")
                self.assertTrue(await modules.end_sequence("completed"))
                logger.close()
                data = paths.data_file.read_text(encoding="utf-8")
                rows = [line for line in data.splitlines() if ",1:Measure," in line]
                self.assertEqual(len(rows), 4)
                lines = data.splitlines()
                header = next(csv.reader([lines[lines.index("[Data]") + 1]]))
                parsed_rows = [next(csv.reader([line])) for line in rows]
                for index, row in enumerate(parsed_rows, start=1):
                    column = header.index(f"simulated_transport.R{index}(Ohm)")
                    self.assertNotEqual(row[column], "")
                self.assertIn("MANUAL_ACTION_COMPLETED", [item.event.code for item in notices])
                await modules.disable("simulated_transport")
                self.assertFalse(record.enabled)
                self.assertIsNone(record.client)
            finally:
                logger.close()
                await modules.shutdown()
                await devices.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_measure_without_enabled_modules_warns_and_writes_system_row(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            devices = DeviceManager(config, events)
            modules = MeasurementModuleService(discover_modules(config), events, devices)
            logger = DatRunLogger(config, events)
            await devices.connect_all()
            await devices.poll_all()
            try:
                descriptors, statuses = await modules.prepare_sequence({})
                paths = logger.open_run("empty.seq", "T Measure\nT End Sequence\n", descriptors, {}, statuses)
                await modules.begin_sequence()
                await modules.measure_all(logger, "1:Measure")
                self.assertTrue(await modules.end_sequence("completed"))
                logger.close()
                rows = [
                    line for line in paths.data_file.read_text(encoding="utf-8").splitlines()
                    if ",1:Measure," in line
                ]
                self.assertEqual(len(rows), 1)
                warnings = [
                    item for item in notices
                    if item.event.code == "NO_ENABLED_MODULES" and not item.is_resolution
                ]
                self.assertEqual(len(warnings), 1)
            finally:
                logger.close()
                await modules.shutdown()
                await devices.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_end_and_abort_failures_keep_module_enabled_without_automatic_abort(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            devices = DeviceManager(config, events)

            end_service = MeasurementModuleService(discover_modules(config), events, devices)
            end_record = end_service.records["simulated_transport"]
            end_client = self._FailingClient("end_sequence")
            end_record.client = end_client  # type: ignore[assignment]
            end_record.enabled = True
            end_record.state = "enabled"
            end_service._sequence_modules = ("simulated_transport",)
            end_service._sequence_active = True
            self.assertFalse(await end_service.end_sequence("completed"))
            self.assertTrue(end_record.enabled)
            self.assertEqual(end_record.state, "faulted")
            self.assertEqual(end_client.actions, ["end_sequence"])

            abort_service = MeasurementModuleService(discover_modules(config), events, devices)
            abort_record = abort_service.records["simulated_transport"]
            abort_client = self._FailingClient("abort")
            abort_record.client = abort_client  # type: ignore[assignment]
            abort_record.enabled = True
            abort_record.state = "enabled"
            with self.assertRaises(DeviceError):
                await abort_service.disable("simulated_transport")
            self.assertTrue(abort_record.enabled)
            self.assertEqual(abort_record.state, "faulted")
            self.assertIs(abort_record.client, abort_client)
            self.assertEqual(abort_client.actions, ["abort"])

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_measure_starts_multiple_enabled_modules_concurrently(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            devices = DeviceManager(config, events)
            descriptors = tuple(
                ModuleDescriptor(
                    id=module_id,
                    name=module_id,
                    version="1.0.0",
                    path=temp_root,
                    api_version="1.0",
                    frontend="frontend:Frontend",
                    backend="backend:Backend",
                    columns=(ModuleColumn("Value", "V"),),
                )
                for module_id in ("module_a", "module_b")
            )
            modules = MeasurementModuleService(descriptors, events, devices)
            barrier = threading.Barrier(2)
            for index, module_id in enumerate(("module_a", "module_b"), start=1):
                record = modules.records[module_id]
                record.enabled = True
                record.state = "enabled"
                record.client = self._BarrierClient(barrier, float(index))  # type: ignore[assignment]
            logger = DatRunLogger(config, events)
            await devices.connect_all()
            await devices.poll_all()
            discovered, statuses = await modules.prepare_sequence({})
            paths = logger.open_run("parallel.seq", "T Measure\n", discovered, {}, statuses)
            await modules.begin_sequence()
            await modules.measure_all(logger, "1:Measure")
            self.assertTrue(await modules.end_sequence("completed"))
            logger.close()
            data = paths.data_file.read_text(encoding="utf-8")
            self.assertEqual(
                sum(1 for line in data.splitlines() if ",1:Measure," in line), 2
            )
            self.assertIn("module_a.Value(V)", data)
            self.assertIn("module_b.Value(V)", data)
            await devices.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))


class ModuleWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_window_uses_settings_and_status_pages_and_ignores_user_close(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        descriptor = discover_modules(config)[0]
        owner = QWidget()
        window = ModuleWindow(descriptor, owner)
        window.load_settings({"delay_seconds": 0.25})
        self.assertEqual(window.tabs.tabText(0), "Settings")
        self.assertEqual(window.tabs.tabText(1), "Status")
        self.assertEqual(window.tabs.currentIndex(), 0)
        self.assertAlmostEqual(window.settings()["delay_seconds"], 0.25)
        window.frontend.delay.setValue(0.5)
        self.assertTrue(window.has_unapplied_edits())
        window.show()
        self.application.processEvents()
        self.assertTrue(window.apply_button.isVisible())
        window.tabs.setCurrentIndex(1)
        self.application.processEvents()
        self.assertFalse(window.apply_button.isVisible())
        self.assertGreaterEqual(window.minimumWidth(), scaled(MODULE_WINDOW_MIN_WIDTH))
        self.assertGreaterEqual(window.minimumHeight(), scaled(MODULE_WINDOW_MIN_HEIGHT))
        window.resize(1, 1)
        self.assertGreaterEqual(window.width(), window.minimumWidth())
        self.assertGreaterEqual(window.height(), window.minimumHeight())
        window.close()
        self.application.processEvents()
        self.assertTrue(window.isVisible())
        window.allow_application_close()
        window.close()
        owner.close()

    def test_window_uses_compact_content_minimum_at_4k_scale(self) -> None:
        previous_scale = self.application.property("openlabUiScale")
        self.application.setProperty("openlabUiScale", 1.4)
        owner = QWidget()
        window: ModuleWindow | None = None
        try:
            config = load_config(ROOT / "configs" / "default.toml")
            window = ModuleWindow(discover_modules(config)[0], owner)
            self.assertLess(window.minimumWidth(), scaled(560))
            self.assertLess(window.minimumHeight(), scaled(460))
        finally:
            if window is not None:
                window.allow_application_close()
                window.close()
            owner.close()
            self.application.setProperty("openlabUiScale", previous_scale)


if __name__ == "__main__":
    unittest.main()
