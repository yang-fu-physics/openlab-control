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
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QSizePolicy,
    QWidget,
)

from labcontrol.config import ConfigurationError, load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.instruments.base import InstrumentError  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.measurement.manifest import (  # noqa: E402
    ModuleColumn,
    ModuleDescriptor,
    discover_modules,
)
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.measurement.settings import load_settings, save_settings  # noqa: E402
from labcontrol.measurement.worker import ModuleWorkerClient, WorkerRequestError  # noqa: E402
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.ui.measurement_modules import (  # noqa: E402
    MODULE_WINDOW_MIN_HEIGHT,
    MODULE_WINDOW_MIN_WIDTH,
    ModuleManagerDialog,
    ModuleWindow,
)
from labcontrol.ui.module_monitor import (  # noqa: E402
    ModuleMonitorCard,
    ModuleMonitorPanel,
    format_compact_result,
)
from labcontrol.ui.scaling import scaled  # noqa: E402
from tests.configuration_fixtures import write_simulated_configuration  # noqa: E402


def copied_project(temp_root: Path):
    general = write_simulated_configuration(temp_root)
    shutil.copytree(
        ROOT / "modules",
        temp_root / "modules",
    )
    return load_config(general)


class ManifestAndSettingsTests(unittest.TestCase):
    def test_module_timeouts_are_loaded_and_must_be_positive(self) -> None:
        config = load_config(ROOT / "configs" / "general.toml")
        self.assertEqual(config.modules.startup_timeout_seconds, 10.0)
        self.assertEqual(config.modules.operation_timeout_seconds, 120.0)
        self.assertEqual(config.modules.shutdown_timeout_seconds, 3.0)
        with tempfile.TemporaryDirectory() as temp:
            invalid = Path(temp) / "invalid.toml"
            source = (ROOT / "configs" / "general.toml").read_text(encoding="utf-8")
            invalid.write_text(
                source.replace(
                    "operation_timeout_seconds = 120.0",
                    "operation_timeout_seconds = 0",
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_config(invalid)

    def test_discovers_template_modules_and_round_trips_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = copied_project(Path(temp))
            descriptors = discover_modules(config)
            self.assertEqual(
                [item.id for item in descriptors],
                ["simulated_transport", "tutorial_resistance"],
            )
            self.assertTrue(all(item.valid for item in descriptors))
            descriptor = next(
                item for item in descriptors if item.id == "simulated_transport"
            )
            self.assertTrue(descriptor.valid)
            self.assertEqual(descriptor.columns, ())
            path = Path(temp) / "module_data" / descriptor.id / "settings.toml"
            original = {"range": 10.0, "enabled": True, "channels": [1, 2], "nested": {"name": "R1"}}
            save_settings(path, original)
            self.assertEqual(load_settings(path), original)

    def test_module_manifest_rejects_dependency_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "backend.py").write_text(
                "class Module:\n    pass\n", encoding="utf-8"
            )
            (root / "module.toml").write_text(
                'name = "Shared VISA"\nversion = "1.0.0"\n'
                'dependencies = ["PyVISA==1.16.2"]\n',
                encoding="utf-8",
            )
            from labcontrol.measurement.manifest import load_manifest

            descriptor = load_manifest(root)
            self.assertFalse(descriptor.valid)
            self.assertIn(
                "unknown module.toml fields: dependencies",
                descriptor.error,
            )

    def test_old_manifest_policy_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "backend.py").write_text("class Module:\n    pass\n", encoding="utf-8")
            (root / "module.toml").write_text(
                'name = "Old"\nversion = "1.0.0"\nmeasurement_mode = "aligned_slots"\n',
                encoding="utf-8",
            )
            from labcontrol.measurement.manifest import load_manifest

            descriptor = load_manifest(root)
            self.assertFalse(descriptor.valid)
            self.assertIn("unknown module.toml fields: measurement_mode", descriptor.error)


class ModuleServiceTests(unittest.TestCase):
    class _FailingClient:
        def __init__(
            self,
            failing_action: str,
            severity: str = "error",
            failing_event: str = "",
        ) -> None:
            self.failing_action = failing_action
            self.severity = severity
            self.failing_event = failing_event
            self.actions: list[str] = []

        def request(
            self,
            action,
            payload=None,
            event_handler=None,
            timeout_seconds=120.0,
        ):
            del event_handler, timeout_seconds
            self.actions.append(action)
            event_matches = (
                not self.failing_event
                or (
                    action == "event"
                    and isinstance(payload, dict)
                    and payload.get("name") == self.failing_event
                )
            )
            if action == self.failing_action and event_matches:
                raise WorkerRequestError(
                    f"{action} failed",
                    f"{action.upper()}_FAILED",
                    "test",
                    self.severity,
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
            del payload, event_handler, timeout_seconds
            if action == "measure":
                self.barrier.wait(timeout=2.0)
                return {
                    "values": {"Value": self.value},
                    "raw_values": [self.value * 1.0e-9],
                }
            return {}

        def close(self, timeout_seconds=3.0) -> None:
            del timeout_seconds
            return None

    class _PlannedClient:
        def __init__(
            self,
            slots: tuple[int, ...] | None,
            value: float,
        ) -> None:
            self.slots = slots
            self.value = value
            self.measured_slots: list[int] = []
            self.actions: list[str] = []

        def request(
            self,
            action,
            payload=None,
            event_handler=None,
            timeout_seconds=120.0,
        ):
            del timeout_seconds
            self.actions.append(action)
            if action == "slots":
                return {
                    "slots": None if self.slots is None else list(self.slots)
                }
            if action == "measure":
                assert payload is not None
                slot = int(payload["slot"])
                self.measured_slots.append(slot)
                return {"values": {"Value": self.value + slot}}
            return {}

        def close(self, timeout_seconds=3.0) -> None:
            del timeout_seconds
            return None

    class _RowContractClient:
        def __init__(self, behavior: str) -> None:
            self.behavior = behavior

        def request(
            self,
            action,
            payload=None,
            event_handler=None,
            timeout_seconds=120.0,
        ):
            del payload, event_handler, timeout_seconds
            if action != "measure":
                return {}
            if self.behavior == "wrong_type":
                return {"values": [1.0]}
            if self.behavior == "unknown_column":
                return {"values": {"Other": 1.0}}
            return {}

        def close(self, timeout_seconds=3.0) -> None:
            del timeout_seconds
            return None

    class _ShutdownClient:
        def __init__(
            self,
            module_close_barrier: threading.Barrier,
            close_barrier: threading.Barrier,
        ) -> None:
            self.module_close_barrier = module_close_barrier
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
            if action == "module_close":
                self.module_close_barrier.wait(timeout=2.0)
            return {}

        def close(self, timeout_seconds=3.0) -> None:
            del timeout_seconds
            self.actions.append("close")
            self.close_barrier.wait(timeout=2.0)

    def test_measurement_rows_reject_nan_and_infinity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = copied_project(Path(temp))
            events = EventManager()
            instruments = InstrumentManager(config, events, isolate_processes=False)
            descriptor = ModuleDescriptor(
                id="finite_values",
                name="Finite Values",
                version="1.0.0",
                path=Path(temp),
                columns=(ModuleColumn("Value", "V"),),
            )
            modules = MeasurementModuleService((descriptor,), events, instruments)
            for invalid in (math.nan, math.inf, -math.inf):
                with self.subTest(value=invalid):
                    with self.assertRaises(InstrumentError) as captured:
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

    def test_status_code_semantics_belong_to_the_module(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = copied_project(Path(temp))
            events = EventManager()
            instruments = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            descriptor = ModuleDescriptor(
                id="numeric_status",
                name="Numeric Status",
                version="1.0.0",
                path=Path(temp),
                columns=(
                    ModuleColumn("Value", "V"),
                    ModuleColumn("StatusCode", ""),
                ),
            )
            modules = MeasurementModuleService(
                (descriptor,),
                events,
                instruments,
            )
            self.assertEqual(
                modules._validated_row(
                    descriptor,
                    {"Value": 1.25, "StatusCode": 0},
                ),
                {"Value": 1.25, "StatusCode": 0},
            )
            # 核心只保证列/schema/JSON 边界；状态码是否必填以及每个值的含义由
            # 模块 README 和模块测试定义。
            for values in (
                {"Value": 1.25},
                {"Value": 1.25, "StatusCode": "module-defined"},
                {"Value": 1.25, "StatusCode": True},
                {"Value": 1.25, "StatusCode": -1},
            ):
                with self.subTest(values=values):
                    self.assertEqual(modules._validated_row(descriptor, values), values)

    def test_measurement_raw_values_are_finite_bounded_numbers(
        self,
    ) -> None:
        self.assertEqual(
            MeasurementModuleService._validated_raw_values(
                "meter",
                [1, -2.5, 3.0e-9],
            ),
            (1.0, -2.5, 3.0e-9),
        )
        self.assertIsNone(
            MeasurementModuleService._validated_raw_values(
                "meter",
                None,
            )
        )
        self.assertEqual(
            MeasurementModuleService._validated_raw_values(
                "meter",
                [],
            ),
            (),
        )
        for invalid, code in (
            ([True], "MODULE_RAW_DATA_TYPE_ERROR"),
            (["1"], "MODULE_RAW_DATA_TYPE_ERROR"),
            ([math.nan], "MODULE_RAW_DATA_VALUE_ERROR"),
            ([math.inf], "MODULE_RAW_DATA_VALUE_ERROR"),
        ):
            with self.subTest(value=invalid):
                with self.assertRaises(InstrumentError) as captured:
                    MeasurementModuleService._validated_raw_values(
                        "meter",
                        invalid,
                    )
                self.assertEqual(captured.exception.code, code)
        with self.assertRaises(InstrumentError) as captured:
            MeasurementModuleService._validated_raw_values(
                "meter",
                [0.0] * 32_769,
            )
        self.assertEqual(
            captured.exception.code,
            "MODULE_RAW_DATA_SIZE_ERROR",
        )

    def test_full_lifecycle_writes_four_ordered_rows_and_disables_cleanly(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            instruments = InstrumentManager(config, events, isolate_processes=False)
            runtime_messages: list[tuple[str, dict[str, object]]] = []
            modules = MeasurementModuleService(
                discover_modules(config),
                events,
                instruments,
                lambda kind, payload: runtime_messages.append(
                    (kind, payload)
                ),
            )
            logger = DatRunLogger(config, events)
            await instruments.connect_all()
            await instruments.poll_all()
            settings = {}
            try:
                await modules.enable("simulated_transport")
                record = modules.records["simulated_transport"]
                self.assertTrue(record.enabled)
                self.assertEqual(record.status["State"], "Ready")
                self.assertEqual(
                    [column.name for column in record.descriptor.columns],
                    ["R1", "R2", "R3", "R4", "StatusCode"],
                )
                self.assertEqual(
                    record.descriptor.display_columns,
                    ("R1", "R2", "R3", "R4"),
                )
                await modules.apply_settings("simulated_transport", settings)
                descriptors, statuses = await modules.prepare_sequence()
                paths = logger.open_run(
                    "module.seq",
                    "T Measure\nT End Sequence\n",
                    descriptors,
                    {"simulated_transport": settings},
                    statuses,
                )
                await modules.begin_sequence()
                await modules.measure_all(logger, "1:Measure")
                compact_messages = [
                    (kind, payload)
                    for kind, payload in runtime_messages
                    if kind in {
                        "module_result",
                        "module_results_reset",
                    }
                ]
                self.assertEqual(
                    [kind for kind, _ in compact_messages],
                    ["module_results_reset"]
                    + ["module_result"] * 4,
                )
                for slot, (_, payload) in enumerate(
                    compact_messages[1:],
                    start=1,
                ):
                    self.assertEqual(payload["slot"], slot)
                    self.assertTrue(payload["multi_slot"])
                    values = {
                        item["name"]: item["value"]
                        for item in payload["items"]
                    }
                    self.assertEqual(
                        set(values),
                        {"R1", "R2", "R3", "R4"},
                    )
                    self.assertEqual(
                        [
                            name
                            for name, value in values.items()
                            if value is not None
                        ],
                        [f"R{slot}"],
                    )
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
                await modules.disable("simulated_transport")
                self.assertFalse(record.enabled)
                self.assertIsNone(record.client)
            finally:
                logger.close()
                await modules.shutdown()
                await instruments.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_measure_without_enabled_modules_warns_and_writes_system_row(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            instruments = InstrumentManager(config, events, isolate_processes=False)
            modules = MeasurementModuleService(discover_modules(config), events, instruments)
            logger = DatRunLogger(config, events)
            await instruments.connect_all()
            await instruments.poll_all()
            try:
                descriptors, statuses = await modules.prepare_sequence()
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
                await instruments.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_end_failure_keeps_module_faulted_and_disable_failure_forces_cleanup(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            instruments = InstrumentManager(config, events, isolate_processes=False)

            end_service = MeasurementModuleService(discover_modules(config), events, instruments)
            end_record = end_service.records["simulated_transport"]
            end_client = self._FailingClient(
                "event",
                failing_event="run_end",
            )
            end_record.client = end_client  # type: ignore[assignment]
            end_record.enabled = True
            end_record.state = "enabled"
            end_service._sequence_modules = ("simulated_transport",)
            end_service._sequence_active = True
            self.assertFalse(await end_service.end_sequence("completed"))
            self.assertTrue(end_record.enabled)
            self.assertEqual(end_record.state, "faulted")
            self.assertEqual(end_client.actions, ["event"])

            close_service = MeasurementModuleService(discover_modules(config), events, instruments)
            close_record = close_service.records["simulated_transport"]
            close_client = self._FailingClient("module_close")
            close_record.client = close_client  # type: ignore[assignment]
            close_record.enabled = True
            close_record.state = "enabled"
            with self.assertRaises(InstrumentError):
                await close_service.disable("simulated_transport")
            self.assertFalse(close_record.enabled)
            self.assertEqual(close_record.state, "disabled")
            self.assertIsNone(close_record.client)
            self.assertEqual(
                close_client.actions,
                ["module_close", "close"],
            )

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_begin_sequence_cancellation_remains_a_normal_stop(
        self,
    ) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            instruments = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            modules = MeasurementModuleService(
                discover_modules(config),
                events,
                instruments,
            )
            record = modules.records[
                "simulated_transport"
            ]
            client = self._FailingClient(
                "event",
                "cancelled",
                failing_event="run_start",
            )
            record.client = client  # type: ignore[assignment]
            record.enabled = True
            record.state = "enabled"
            modules._sequence_modules = (
                "simulated_transport",
            )
            modules._sequence_active = True

            await modules.begin_sequence()

            self.assertTrue(record.enabled)
            self.assertEqual(record.state, "enabled")
            self.assertFalse(
                any(
                    notice.event.source
                    == "module:simulated_transport"
                    and not notice.is_resolution
                    for notice in notices
                )
            )
            self.assertTrue(
                await modules.end_sequence("stopped")
            )
            self.assertEqual(
                client.actions,
                ["event", "event"],
            )

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_measure_starts_multiple_enabled_modules_concurrently(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            instruments = InstrumentManager(config, events, isolate_processes=False)
            descriptors = tuple(
                ModuleDescriptor(
                    id=module_id,
                    name=module_id,
                    version="1.0.0",
                    path=temp_root,
                    columns=(ModuleColumn("Value", "V"),),
                )
                for module_id in ("module_a", "module_b")
            )
            modules = MeasurementModuleService(descriptors, events, instruments)
            barrier = threading.Barrier(2)
            for index, module_id in enumerate(("module_a", "module_b"), start=1):
                record = modules.records[module_id]
                record.enabled = True
                record.state = "enabled"
                record.client = self._BarrierClient(barrier, float(index))  # type: ignore[assignment]
            logger = DatRunLogger(config, events)
            await instruments.connect_all()
            await instruments.poll_all()
            discovered, statuses = await modules.prepare_sequence()
            paths = logger.open_run("parallel.seq", "T Measure\n", discovered, {}, statuses)
            await modules.begin_sequence()
            await modules.measure_all(logger, "1:Measure")
            self.assertTrue(await modules.end_sequence("completed"))
            logger.close()
            data = paths.data_file.read_text(encoding="utf-8")
            self.assertEqual(
                sum(1 for line in data.splitlines() if ",1:Measure," in line), 1
            )
            self.assertIn("module_a.Value(V)", data)
            self.assertIn("module_b.Value(V)", data)
            raw_files = tuple(
                paths.raw_data_directory.glob("*.rawdata")
            )
            self.assertEqual(len(raw_files), 2)
            self.assertEqual(
                {
                    path.read_text(
                        encoding="utf-8"
                    ).strip()
                    for path in raw_files
                },
                {"1.0000000000000001e-09", "2.0000000000000001e-09"},
            )
            await instruments.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_optional_slot_union_merges_modules_and_repeats_followers(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            instruments = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            descriptors = tuple(
                ModuleDescriptor(
                    id=module_id,
                    name=module_id,
                    version="1.0.0",
                    path=temp_root,
                    columns=(ModuleColumn("Value", "V"),),
                )
                for module_id in ("scan_a", "scan_b", "single_meter")
            )
            modules = MeasurementModuleService(
                descriptors,
                events,
                instruments,
            )
            clients = {
                "scan_a": self._PlannedClient((1, 3, 4), 10.0),
                "scan_b": self._PlannedClient((1, 2, 4), 20.0),
                "single_meter": self._PlannedClient(None, 30.0),
            }
            for module_id, client in clients.items():
                record = modules.records[module_id]
                record.enabled = True
                record.state = "enabled"
                record.client = client  # type: ignore[assignment]
            logger = DatRunLogger(config, events)
            await instruments.connect_all()
            await instruments.poll_all()
            discovered, statuses = await modules.prepare_sequence()
            paths = logger.open_run(
                "aligned.seq",
                "T Measure\n",
                discovered,
                {},
                statuses,
            )

            await modules.begin_sequence()
            await modules.measure_all(logger, "1:Measure")
            self.assertTrue(await modules.end_sequence("completed"))
            logger.close()

            lines = paths.data_file.read_text(
                encoding="utf-8"
            ).splitlines()
            data_index = lines.index("[Data]")
            header = next(csv.reader([lines[data_index + 1]]))
            rows = [
                next(csv.reader([line]))
                for line in lines[data_index + 2 :]
                if ",1:Measure," in line
            ]
            self.assertEqual(len(rows), 4)
            columns = {
                module_id: header.index(f"{module_id}.Value(V)")
                for module_id in clients
            }
            present = [
                (True, True, True),
                (False, True, True),
                (True, False, True),
                (True, True, True),
            ]
            for row, expected in zip(rows, present, strict=True):
                self.assertEqual(
                    tuple(
                        row[columns[module_id]] != ""
                        for module_id in clients
                    ),
                    expected,
                )
            self.assertEqual(clients["scan_a"].measured_slots, [1, 3, 4])
            self.assertEqual(clients["scan_b"].measured_slots, [1, 2, 4])
            self.assertEqual(
                clients["single_meter"].measured_slots,
                [1, 2, 3, 4],
            )
            self.assertIn(
                "slots",
                clients["single_meter"].actions,
            )
            await instruments.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_measure_requires_one_returned_mapping_per_slot(self) -> None:
        async def scenario(
            temp_root: Path,
            behavior: str,
            expected_code: str,
        ) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            instruments = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            descriptor = ModuleDescriptor(
                id="row_contract",
                name="Row Contract",
                version="1.0.0",
                path=temp_root,
                columns=(ModuleColumn("Value", "V"),),
            )
            modules = MeasurementModuleService(
                (descriptor,),
                events,
                instruments,
            )
            record = modules.records[descriptor.id]
            record.enabled = True
            record.state = "enabled"
            record.client = self._RowContractClient(behavior)  # type: ignore[assignment]
            logger = DatRunLogger(config, events)
            await instruments.connect_all()
            await instruments.poll_all()
            discovered, statuses = await modules.prepare_sequence()
            logger.open_run(
                "row-contract.seq",
                "T Measure\n",
                discovered,
                {},
                statuses,
            )
            await modules.begin_sequence()
            with self.assertRaises(InstrumentError) as raised:
                await modules.measure_all(logger, "1:Measure")
            self.assertEqual(
                raised.exception.code,
                expected_code,
            )
            self.assertTrue(await modules.end_sequence("error"))
            logger.close()
            await instruments.disconnect_all()

        for behavior, expected_code in (
            ("no_row", "MODULE_MEASUREMENT_ROW_MISSING"),
            ("wrong_type", "MODULE_MEASUREMENT_ROW_MISSING"),
            ("unknown_column", "MODULE_SCHEMA_VIOLATION"),
        ):
            with self.subTest(behavior=behavior):
                with tempfile.TemporaryDirectory() as temp:
                    asyncio.run(
                        scenario(
                            Path(temp),
                            behavior,
                            expected_code,
                        )
                    )

    def test_shutdown_closes_modules_and_reaps_workers_concurrently(self) -> None:
        async def scenario(temp_root: Path) -> None:
            config = copied_project(temp_root)
            events = EventManager()
            instruments = InstrumentManager(config, events, isolate_processes=False)
            descriptors = tuple(
                ModuleDescriptor(
                    id=module_id,
                    name=module_id,
                    version="1.0.0",
                    path=temp_root,
                )
                for module_id in ("module_a", "module_b")
            )
            modules = MeasurementModuleService(descriptors, events, instruments)
            module_close_barrier = threading.Barrier(2)
            close_barrier = threading.Barrier(2)
            clients = []
            for module_id in ("module_a", "module_b"):
                record = modules.records[module_id]
                client = self._ShutdownClient(module_close_barrier, close_barrier)
                clients.append(client)
                record.client = client  # type: ignore[assignment]
                record.enabled = True
                record.state = "enabled"

            await modules.shutdown()

            self.assertEqual([client.actions for client in clients], [
                ["module_close", "close"],
                ["module_close", "close"],
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
                "",
                "class Module:",
                "    columns = {'Value': ''}",
                "    def __init__(self):",
                f"        time.sleep({startup_delay!r})",
                "",
                "    def open(self, api):",
                "        return {}",
                "",
                "    def configure(self, settings, api):",
                "        time.sleep(float(settings.get('delay_seconds', 0.0)))",
                "        return {}",
                "",
                "    def measure(self, slot, api):",
                "        return {'Value': 0}",
                "",
                "    def close(self, api):",
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

    def test_worker_validates_optional_display_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            descriptor = self._descriptor(root)
            backend = root / "backend.py"
            backend.write_text(
                backend.read_text(encoding="utf-8").replace(
                    "    columns = {'Value': ''}",
                    "    columns = {'Value': ''}\n"
                    "    display_columns = 'Value'",
                ),
                encoding="utf-8",
            )
            client = ModuleWorkerClient(descriptor)
            try:
                client.start(timeout_seconds=2.0)
                self.assertEqual(
                    client.display_columns,
                    ("Value",),
                )
            finally:
                client.close(timeout_seconds=1.0)

    def test_worker_rejects_undeclared_display_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            descriptor = self._descriptor(root)
            backend = root / "backend.py"
            backend.write_text(
                backend.read_text(encoding="utf-8").replace(
                    "    columns = {'Value': ''}",
                    "    columns = {'Value': ''}\n"
                    "    display_columns = ('Missing',)",
                ),
                encoding="utf-8",
            )
            client = ModuleWorkerClient(descriptor)
            with self.assertRaises(WorkerRequestError) as captured:
                client.start(timeout_seconds=2.0)
            self.assertEqual(
                captured.exception.code,
                "MODULE_WORKER_START_FAILED",
            )
            self.assertIn(
                "undeclared column",
                str(captured.exception),
            )

    def test_request_timeout_terminates_worker_and_rejects_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = ModuleWorkerClient(self._descriptor(Path(temp)))
            client.start(timeout_seconds=2.0)
            with self.assertRaises(WorkerRequestError) as captured:
                client.request(
                    "configure",
                    {"settings": {"delay_seconds": 5.0}},
                    timeout_seconds=0.05,
                )
            self.assertEqual(captured.exception.code, "MODULE_OPERATION_TIMEOUT")
            self.assertIsNone(client._process)
            self.assertIsNone(client._connection)
            with self.assertRaises(WorkerRequestError) as reused:
                client.request("event", timeout_seconds=0.05)
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
                        "configure",
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
                "class Module:",
                "    columns = {'Average': 'K'}",
                "    def open(self, api):",
                "        return {}",
                "",
                "    def measure(self, slot, api):",
                "        first = api.instruments()",
                "        api.sleep(",
                "            0.02, poll_interval=0.005",
                "        )",
                "        second = api.instruments()",
                "        return {",
                "            'Average': (",
                "                first['temperature']['current']",
                "                + second['temperature']['current']",
                "            ) / 2.0",
                "        }",
                "",
                "    def close(self, api):",
                "        return {}",
                "",
            ]),
            encoding="utf-8",
        )
        return ModuleDescriptor(
            id="context_module",
            name="Context Module",
            version="1.0.0",
            path=root,
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
                    {"slot": 1},
                    event_handler=handle,
                    timeout_seconds=2.0,
                )
            finally:
                client.close(timeout_seconds=1.0)

            self.assertEqual(result["values"]["Average"], 2.0)
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
                        {"slot": 1},
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

    def test_compact_result_card_formats_slots_and_restores_window(self) -> None:
        owner = QWidget()
        descriptor = ModuleDescriptor(
            id="compact_meter",
            name="Compact Meter With A Long Name",
            version="1.0.0",
            path=ROOT,
        )
        card = ModuleMonitorCard(descriptor, owner)
        opened: list[str] = []
        card.activated.connect(opened.append)
        card.set_display_columns(["Resistance"])
        card.update_result({
            "slot": 1,
            "multi_slot": True,
            "timestamp": 1.0,
            "items": [
                {
                    "name": "Resistance",
                    "unit": "Ohm",
                    "value": 0.001,
                }
            ],
        })
        card.update_result({
            "slot": 2,
            "multi_slot": True,
            "timestamp": 2.0,
            "items": [
                {
                    "name": "Resistance",
                    "unit": "Ohm",
                    "value": None,
                }
            ],
        })
        owner.resize(230, 180)
        card.setGeometry(0, 0, 220, 160)
        owner.show()
        self.application.processEvents()

        self.assertIn("CH1  1 mΩ", card.results_label.text())
        self.assertIn("CH2  —", card.results_label.text())
        self.assertEqual(
            card.name_label.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Ignored,
        )
        self.assertEqual(
            card.results_label.textFormat(),
            Qt.TextFormat.PlainText,
        )
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        self.assertEqual(opened, ["compact_meter"])
        card.set_minimized(True)
        self.assertIn("Minimized", card.state_label.text())
        card.reset_results()
        self.assertEqual(
            card.results_label.text(),
            "Waiting for next Measure",
        )
        card.update_result({
            "slot": 1,
            "multi_slot": False,
            "items": [
                {
                    "name": "Resistance",
                    "unit": "Ohm",
                    "value": 0.001,
                }
            ],
        })
        card.update_result({
            "slot": 4,
            "multi_slot": False,
            "items": [
                {
                    "name": "Resistance",
                    "unit": "Ohm",
                    "value": 0.002,
                }
            ],
        })
        self.assertEqual(
            card.results_label.text(),
            "Resistance 2 mΩ",
        )
        owner.close()

    def test_compact_result_formatter_uses_short_si_units(self) -> None:
        self.assertEqual(format_compact_result(1e-12, "Ohm"), "1 pΩ")
        self.assertEqual(format_compact_result(3e-6, "A"), "3 µA")
        self.assertEqual(format_compact_result(None, "V"), "—")

    def test_monitor_panel_owns_card_and_deduplicated_alert_state(self) -> None:
        panel = ModuleMonitorPanel()
        descriptor = ModuleDescriptor(
            id="panel_meter",
            name="Panel Meter",
            version="1.0.0",
            path=ROOT,
        )
        panel.update_module(
            descriptor,
            enabled=True,
            state="enabled",
            message="",
            minimized=False,
            display_columns=["Resistance"],
        )
        card = panel.cards[descriptor.id]
        self.assertTrue(panel.empty_label.isHidden())
        panel.update_alert(
            descriptor.id,
            "module:panel_meter/OVER_RANGE/R1",
            "warning",
            resolved=False,
        )
        self.assertEqual(card.state_label.text(), "Warning")
        panel.update_alert(
            descriptor.id,
            "module:panel_meter/OVER_RANGE/R1",
            "warning",
            resolved=True,
        )
        self.assertEqual(card.state_label.text(), "Enabled")
        panel.update_module(
            descriptor,
            enabled=False,
            state="disabled",
            message="",
            minimized=False,
            display_columns=[],
        )
        self.assertNotIn(descriptor.id, panel.cards)
        self.assertFalse(panel.empty_label.isHidden())
        panel.close()

    def test_window_uses_generic_pages_and_ignores_user_close(self) -> None:
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
            self.assertEqual(window.settings(), {})
            window.show()
            self.application.processEvents()
            self.assertFalse(window.apply_button.isVisible())
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
            window.reject()
            self.application.processEvents()
            self.assertEqual(window.settings(), {})
            window.allow_application_close()
            window.close()
            owner.close()

    def test_manager_first_width_shows_full_names_without_horizontal_scroll(
        self,
    ) -> None:
        owner = QWidget()
        descriptor = ModuleDescriptor(
            id="long_name",
            name=(
                "Keithley 6221 + 2182A Delta + 3706A "
                "Measurement Module"
            ),
            version="0.1.0b1",
            path=ROOT,
        )
        dialog = ModuleManagerDialog(
            (descriptor,),
            owner,
        )
        dialog.show()
        for _ in range(12):
            self.application.processEvents()

        fitted_width = dialog.width()
        self.assertFalse(
            dialog.table.horizontalScrollBar().isVisible()
        )
        dialog.resize(fitted_width - 1, dialog.height())
        self.application.processEvents()
        self.assertTrue(
            dialog.table.horizontalScrollBar().isVisible()
        )
        dialog.close()
        owner.close()

    def test_custom_frontend_can_implement_only_the_hooks_it_needs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "frontend.py").write_text(
                "from PySide6.QtWidgets import QWidget\n"
                "class Frontend(QWidget):\n"
                "    def __init__(self, api):\n"
                "        super().__init__()\n"
                "        self.api = api\n"
                "        self.values = {'gain': 2}\n"
                "    def load(self, settings):\n"
                "        self.values = dict(settings)\n"
                "    def dump(self):\n"
                "        return dict(self.values)\n",
                encoding="utf-8",
            )
            descriptor = ModuleDescriptor(
                id="partial_frontend",
                name="Partial Frontend",
                version="1.0.0",
                path=root,
            )
            owner = QWidget()
            window = ModuleWindow(descriptor, owner)
            window.load_settings({"gain": 2})
            self.assertEqual(window.settings(), {"gain": 2})
            self.assertIsInstance(window.settings_content, QWidget)
            self.assertIsInstance(window.status_page, QWidget)
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

            self.assertEqual(window.settings(), {})
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
                self.assertFalse(
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
