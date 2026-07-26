from __future__ import annotations

import asyncio
import csv
import math
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
MODULE_REPOSITORY = (
    ROOT
    / "plugin_templates"
    / "measurement-modules-repository"
)
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from labcontrol.config import ConfigurationError, load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.devices.base import DeviceError  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.extensions.trust import PluginTrustStore  # noqa: E402
from labcontrol.measurement.manifest import (  # noqa: E402
    ModuleColumn,
    ModuleDescriptor,
    discover_modules,
    module_dependency_directory,
)
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.measurement.settings import load_settings, save_settings  # noqa: E402
from labcontrol.measurement.worker import ModuleWorkerClient, WorkerRequestError  # noqa: E402
from labcontrol.plugins import DeviceManager  # noqa: E402
from labcontrol.ui.measurement_modules import (  # noqa: E402
    MODULE_WINDOW_MIN_HEIGHT,
    MODULE_WINDOW_MIN_WIDTH,
    ModuleManagerDialog,
    ModuleWindow,
)
from labcontrol.ui.scaling import scaled  # noqa: E402


def copied_project(temp_root: Path):
    (temp_root / "configs").mkdir()
    shutil.copy2(ROOT / "configs" / "default.toml", temp_root / "configs" / "default.toml")
    shutil.copytree(
        MODULE_REPOSITORY / "modules",
        temp_root / "modules",
    )
    config = load_config(temp_root / "configs" / "default.toml")
    store = PluginTrustStore(
        config.resolve_project_path(
            config.plugins.state_directory
        )
        / "trusted_plugins.json"
    )
    for descriptor in discover_modules(config):
        if descriptor.valid:
            store.trust("module", descriptor)
    return config


class ManifestAndSettingsTests(unittest.TestCase):
    def test_module_timeouts_are_loaded_and_must_be_positive(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        self.assertEqual(config.modules.startup_timeout_seconds, 10.0)
        self.assertEqual(config.modules.operation_timeout_seconds, 120.0)
        self.assertEqual(config.modules.shutdown_timeout_seconds, 3.0)
        with tempfile.TemporaryDirectory() as temp:
            invalid = Path(temp) / "invalid.toml"
            source = (ROOT / "configs" / "default.toml").read_text(encoding="utf-8")
            invalid.write_text(
                source.replace(
                    "operation_timeout_seconds = 120.0",
                    "operation_timeout_seconds = 0",
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_config(invalid)

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

    def test_conflicting_dependency_ranges_are_isolated_by_module(self) -> None:
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
                locked_version = (
                    "1.5.0"
                    if module_id == "first"
                    else "2.5.0"
                )
                (folder / "requirements.lock").write_text(
                    "demo-package=="
                    + locked_version
                    + " --hash=sha256:"
                    + "0" * 64
                    + "\n",
                    encoding="utf-8",
                )
                (folder / "frontend.py").write_text(
                    "class Frontend:\n    pass\n",
                    encoding="utf-8",
                )
                (folder / "backend.py").write_text(
                    "class Backend:\n    pass\n",
                    encoding="utf-8",
                )
            config = load_config(root / "configs" / "default.toml")
            descriptors = discover_modules(config)
            self.assertEqual(len(descriptors), 2)
            self.assertTrue(
                all(item.can_enable for item in descriptors),
                [item.error for item in descriptors],
            )
            self.assertNotEqual(
                module_dependency_directory(config, descriptors[0]),
                module_dependency_directory(config, descriptors[1]),
            )

    def test_framework_dependency_needs_no_module_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            shutil.copy2(
                ROOT / "configs" / "default.toml",
                root / "configs" / "default.toml",
            )
            folder = root / "modules" / "shared_visa"
            folder.mkdir(parents=True)
            (folder / "module.toml").write_text(
                "\n".join(
                    [
                        'id = "shared_visa"',
                        'name = "Shared VISA"',
                        'version = "1.0.0"',
                        'api_version = "1.0"',
                        'frontend = "frontend:Frontend"',
                        'backend = "backend:Backend"',
                        (
                            'dependencies = ['
                            '"PyVISA>=1.16,<1.17", '
                            '"typing_extensions>=4.16,<5"]'
                        ),
                        "[[columns]]",
                        'name = "Value"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (folder / "frontend.py").write_text(
                "class Frontend:\n    pass\n",
                encoding="utf-8",
            )
            (folder / "backend.py").write_text(
                "class Backend:\n    pass\n",
                encoding="utf-8",
            )
            config = load_config(
                root / "configs" / "default.toml"
            )
            descriptor = discover_modules(config)[0]
            self.assertTrue(
                descriptor.valid,
                descriptor.error,
            )
            self.assertEqual(descriptor.dependencies, ())
            self.assertEqual(
                descriptor.framework_dependencies,
                (
                    "PyVISA>=1.16,<1.17",
                    "typing_extensions>=4.16,<5",
                ),
            )
            self.assertTrue(descriptor.can_enable)

            manifest = (
                folder / "module.toml"
            ).read_text(encoding="utf-8")
            (folder / "module.toml").write_text(
                manifest.replace(
                    "PyVISA>=1.16,<1.17",
                    "PyVISA>=2",
                ),
                encoding="utf-8",
            )
            incompatible = discover_modules(config)[0]
            self.assertFalse(incompatible.valid)
            self.assertIn(
                "framework-provided version 1.16.2",
                incompatible.error,
            )


class ModuleServiceTests(unittest.TestCase):
    class _FailingClient:
        def __init__(self, failing_action: str) -> None:
            self.failing_action = failing_action
            self.actions: list[str] = []

        def request(
            self,
            action,
            payload=None,
            event_handler=None,
            timeout_seconds=120.0,
        ):
            del payload, event_handler, timeout_seconds
            self.actions.append(action)
            if action == self.failing_action:
                raise WorkerRequestError(
                    f"{action} failed", f"{action.upper()}_FAILED", "test"
                )
            return {}

        def close(self, timeout_seconds=3.0) -> None:
            del timeout_seconds
            self.actions.append("close")

    class _BarrierClient:
        def __init__(self, barrier: threading.Barrier, value: float) -> None:
            self.barrier = barrier
            self.value = value

        def request(
            self,
            action,
            payload=None,
            event_handler=None,
            timeout_seconds=120.0,
        ):
            del payload, timeout_seconds
            if action == "measure":
                self.barrier.wait(timeout=2.0)
                assert event_handler is not None
                event_handler({"type": "row", "values": {"Value": self.value}})
            return {}

        def close(self, timeout_seconds=3.0) -> None:
            del timeout_seconds
            return None

    class _ShutdownClient:
        def __init__(
            self,
            abort_barrier: threading.Barrier,
            close_barrier: threading.Barrier,
        ) -> None:
            self.abort_barrier = abort_barrier
            self.close_barrier = close_barrier
            self.actions: list[str] = []

        def request(
            self,
            action,
            payload=None,
            event_handler=None,
            timeout_seconds=120.0,
        ):
            del payload, event_handler, timeout_seconds
            self.actions.append(action)
            if action == "abort":
                self.abort_barrier.wait(timeout=2.0)
            return {}

        def close(self, timeout_seconds=3.0) -> None:
            del timeout_seconds
            self.actions.append("close")
            self.close_barrier.wait(timeout=2.0)

    def test_enable_requires_trust_and_rechecks_module_content(
        self,
    ) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            devices = DeviceManager(
                config,
                events,
                isolate_processes=False,
            )
            descriptor = discover_modules(config)[0]
            modules = MeasurementModuleService(
                (descriptor,),
                events,
                devices,
            )
            modules.trust_store.revoke(
                "module",
                descriptor.id,
            )
            with self.assertRaises(DeviceError) as untrusted:
                await modules.enable(descriptor.id, {})
            self.assertEqual(
                untrusted.exception.code,
                "MODULE_NOT_TRUSTED",
            )
            modules.trust_store.trust(
                "module",
                descriptor,
            )
            backend = descriptor.path / "backend.py"
            backend.write_text(
                backend.read_text(encoding="utf-8")
                + "\n# changed after trust\n",
                encoding="utf-8",
            )
            with self.assertRaises(DeviceError) as changed:
                await modules.enable(descriptor.id, {})
            self.assertEqual(
                changed.exception.code,
                "MODULE_CHANGED_AFTER_DISCOVERY",
            )

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_measurement_rows_reject_nan_and_infinity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = copied_project(Path(temp))
            events = EventManager()
            devices = DeviceManager(config, events, isolate_processes=False)
            descriptor = ModuleDescriptor(
                id="finite_values",
                name="Finite Values",
                version="1.0.0",
                path=Path(temp),
                api_version="1.0",
                frontend="frontend:Frontend",
                backend="backend:Backend",
                columns=(ModuleColumn("Value", "V"),),
            )
            modules = MeasurementModuleService((descriptor,), events, devices)
            for invalid in (math.nan, math.inf, -math.inf):
                with self.subTest(value=invalid):
                    with self.assertRaises(DeviceError) as captured:
                        modules._validated_row(
                            descriptor,
                            {"Value": invalid},
                        )
                    self.assertEqual(
                        captured.exception.code,
                        "MODULE_ROW_VALUE_ERROR",
                    )
            self.assertEqual(
                modules._validated_row(descriptor, {"Value": 1.25}),
                {"Value": 1.25},
            )

    def test_full_lifecycle_streams_four_ordered_rows_and_disables_cleanly(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            devices = DeviceManager(config, events, isolate_processes=False)
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
            devices = DeviceManager(config, events, isolate_processes=False)
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

    def test_end_failure_keeps_module_faulted_and_disable_failure_forces_cleanup(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            devices = DeviceManager(config, events, isolate_processes=False)

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
            self.assertFalse(abort_record.enabled)
            self.assertEqual(abort_record.state, "disabled")
            self.assertIsNone(abort_record.client)
            self.assertEqual(abort_client.actions, ["abort", "close"])

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_measure_starts_multiple_enabled_modules_concurrently(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            devices = DeviceManager(config, events, isolate_processes=False)
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

    def test_shutdown_aborts_and_closes_module_workers_concurrently(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            devices = DeviceManager(config, events, isolate_processes=False)
            descriptors = tuple(
                ModuleDescriptor(
                    id=module_id,
                    name=module_id,
                    version="1.0.0",
                    path=temp_root,
                    api_version="1.0",
                    frontend="frontend:Frontend",
                    backend="backend:Backend",
                )
                for module_id in ("module_a", "module_b")
            )
            modules = MeasurementModuleService(descriptors, events, devices)
            abort_barrier = threading.Barrier(2)
            close_barrier = threading.Barrier(2)
            clients = []
            for module_id in ("module_a", "module_b"):
                record = modules.records[module_id]
                client = self._ShutdownClient(abort_barrier, close_barrier)
                clients.append(client)
                record.client = client  # type: ignore[assignment]
                record.enabled = True
                record.state = "enabled"

            await modules.shutdown()

            self.assertEqual([client.actions for client in clients], [
                ["abort", "close"],
                ["abort", "close"],
            ])
            self.assertTrue(
                all(
                    not record.enabled
                    and record.client is None
                    and record.state == "disabled"
                    for record in modules.records.values()
                )
            )

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))


class ModuleWorkerTimeoutTests(unittest.TestCase):
    @staticmethod
    def _descriptor(root: Path, startup_delay: float = 0.0) -> ModuleDescriptor:
        (root / "backend.py").write_text(
            "\n".join([
                "import time",
                "from labcontrol.measurement.api import ModuleBackend",
                "",
                "class Backend(ModuleBackend):",
                "    def __init__(self):",
                f"        time.sleep({startup_delay!r})",
                "",
                "    def initialize(self, settings, context):",
                "        time.sleep(float(settings.get('delay_seconds', 0.0)))",
                "        return {}",
                "",
            ]),
            encoding="utf-8",
        )
        return ModuleDescriptor(
            id="timeout_module",
            name="Timeout Module",
            version="1.0.0",
            path=root,
            api_version="1.0",
            backend="backend:Backend",
        )

    def test_startup_timeout_terminates_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = ModuleWorkerClient(
                self._descriptor(Path(temp), startup_delay=5.0)
            )
            with self.assertRaises(WorkerRequestError) as captured:
                client.start(timeout_seconds=0.05)
            self.assertEqual(
                captured.exception.code,
                "MODULE_WORKER_START_TIMEOUT",
            )
            self.assertIsNone(client._process)
            self.assertIsNone(client._connection)
            self.assertNotIn(
                "OpenLabModule-timeout_module",
                [child.name for child in multiprocessing.active_children()],
            )

    def test_request_timeout_terminates_worker_and_rejects_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = ModuleWorkerClient(self._descriptor(Path(temp)))
            client.start(timeout_seconds=2.0)
            with self.assertRaises(WorkerRequestError) as captured:
                client.request(
                    "initialize",
                    {"settings": {"delay_seconds": 5.0}},
                    timeout_seconds=0.05,
                )
            self.assertEqual(captured.exception.code, "MODULE_OPERATION_TIMEOUT")
            self.assertIsNone(client._process)
            self.assertIsNone(client._connection)
            with self.assertRaises(WorkerRequestError) as reused:
                client.request("read_status", timeout_seconds=0.05)
            self.assertEqual(reused.exception.code, "MODULE_WORKER_NOT_RUNNING")
            self.assertNotIn(
                "OpenLabModule-timeout_module",
                [child.name for child in multiprocessing.active_children()],
            )

    def test_close_preempts_an_inflight_worker_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = ModuleWorkerClient(self._descriptor(Path(temp)))
            client.start(timeout_seconds=2.0)
            failures: list[WorkerRequestError] = []

            def request() -> None:
                try:
                    client.request(
                        "initialize",
                        {"settings": {"delay_seconds": 5.0}},
                        timeout_seconds=5.0,
                    )
                except WorkerRequestError as exc:
                    failures.append(exc)

            thread = threading.Thread(target=request)
            thread.start()
            time.sleep(0.05)
            started = time.monotonic()
            client.close(timeout_seconds=0.3)
            elapsed = time.monotonic() - started
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
            self.assertLess(elapsed, 1.0)
            self.assertTrue(failures)
            self.assertIsNone(client._process)
            self.assertIsNone(client._connection)


class ModuleWorkerContextTests(unittest.TestCase):
    @staticmethod
    def _descriptor(root: Path) -> ModuleDescriptor:
        (root / "backend.py").write_text(
            "\n".join([
                "from labcontrol.measurement.api import ModuleBackend",
                "",
                "class Backend(ModuleBackend):",
                "    def measure(self, context):",
                "        first = context.sample_system()",
                "        context.interruptible_sleep(",
                "            0.02, poll_interval_seconds=0.005",
                "        )",
                "        second = context.sample_system()",
                "        return {",
                "            'Average': (",
                "                first['temperature']['current']",
                "                + second['temperature']['current']",
                "            ) / 2.0",
                "        }",
                "",
            ]),
            encoding="utf-8",
        )
        return ModuleDescriptor(
            id="context_module",
            name="Context Module",
            version="1.0.0",
            path=root,
            api_version="1.0",
            backend="backend:Backend",
            columns=(ModuleColumn("Average", "K"),),
        )

    def test_worker_round_trips_live_snapshots_and_control_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = ModuleWorkerClient(
                self._descriptor(Path(temp))
            )
            client.start(timeout_seconds=2.0)
            samples = iter((1.0, 3.0))
            requests: list[str] = []

            def handle(message):
                kind = str(message.get("kind", ""))
                requests.append(kind)
                if kind == "system":
                    return {
                        "system": {
                            "temperature": {
                                "kind": "temperature",
                                "current": next(samples),
                            }
                        }
                    }
                if kind == "operation_state":
                    return {"state": "running"}
                self.fail(f"Unexpected context request: {kind}")

            try:
                result = client.request(
                    "measure",
                    event_handler=handle,
                    timeout_seconds=2.0,
                )
            finally:
                client.close(timeout_seconds=1.0)

            self.assertEqual(result["Average"], 2.0)
            self.assertEqual(requests.count("system"), 2)
            self.assertGreaterEqual(
                requests.count("operation_state"),
                2,
            )

    def test_worker_reports_cooperative_cancellation_separately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = ModuleWorkerClient(
                self._descriptor(Path(temp))
            )
            client.start(timeout_seconds=2.0)

            def handle(message):
                if message.get("kind") == "operation_state":
                    return {"state": "stopping"}
                return {"system": {}}

            try:
                with self.assertRaises(
                    WorkerRequestError
                ) as captured:
                    client.request(
                        "measure",
                        event_handler=handle,
                        timeout_seconds=2.0,
                    )
            finally:
                client.close(timeout_seconds=1.0)

            self.assertEqual(
                captured.exception.code,
                "MODULE_OPERATION_CANCELLED",
            )
            self.assertEqual(
                captured.exception.severity,
                "cancelled",
            )


class ModuleWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_dependency_button_only_appears_for_extra_dependencies(
        self,
    ) -> None:
        owner = QWidget()
        shared = ModuleDescriptor(
            id="shared",
            name="Shared",
            version="1.0.0",
            path=ROOT,
            dependencies=(),
        )
        extra = ModuleDescriptor(
            id="extra",
            name="Extra",
            version="1.0.0",
            path=ROOT,
            dependencies=("module-only-demo==1.0.0",),
        )
        dialog = ModuleManagerDialog(
            (shared, extra),
            owner,
        )
        dialog.table.selectRow(0)
        self.application.processEvents()
        self.assertTrue(
            dialog.install_button.isHidden()
        )
        dialog.table.selectRow(1)
        self.application.processEvents()
        self.assertFalse(
            dialog.install_button.isHidden()
        )
        dialog.close()
        owner.close()

    def test_window_uses_settings_and_status_pages_and_ignores_user_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            descriptor = discover_modules(
                copied_project(Path(temp))
            )[0]
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
            self.assertGreaterEqual(
                window.minimumWidth(),
                scaled(MODULE_WINDOW_MIN_WIDTH),
            )
            self.assertGreaterEqual(
                window.minimumHeight(),
                scaled(MODULE_WINDOW_MIN_HEIGHT),
            )
            window.resize(1, 1)
            self.assertGreaterEqual(window.width(), window.minimumWidth())
            self.assertGreaterEqual(window.height(), window.minimumHeight())
            window.close()
            self.application.processEvents()
            self.assertTrue(window.isVisible())
            window.allow_application_close()
            window.close()
            owner.close()

    def test_imported_sequence_settings_are_marked_unapplied(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            descriptor = discover_modules(
                copied_project(Path(temp))
            )[0]
            owner = QWidget()
            window = ModuleWindow(
                descriptor,
                owner,
            )

            window.load_settings(
                {"delay_seconds": 0.75},
                mark_unapplied=True,
            )

            self.assertAlmostEqual(
                window.settings()["delay_seconds"],
                0.75,
            )
            self.assertTrue(
                window.has_unapplied_edits()
            )
            self.assertIn(
                "not applied",
                window.message_label.text(),
            )
            window.allow_application_close()
            window.close()
            owner.close()

    def test_window_uses_compact_content_minimum_at_4k_scale(self) -> None:
        previous_scale = self.application.property("openlabUiScale")
        self.application.setProperty("openlabUiScale", 1.4)
        owner = QWidget()
        window: ModuleWindow | None = None
        try:
            with tempfile.TemporaryDirectory() as temp:
                descriptor = discover_modules(
                    copied_project(Path(temp))
                )[0]
                window = ModuleWindow(descriptor, owner)
                self.assertTrue(
                    window.testAttribute(
                        Qt.WidgetAttribute.WA_DeleteOnClose
                    )
                )
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
