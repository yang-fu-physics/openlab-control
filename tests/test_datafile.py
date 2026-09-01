from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SIMULATED_MODULE = (
    ROOT
    / "modules"
    / "simulated_transport"
)
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.models import (  # noqa: E402
    InstrumentActivity,
    InstrumentConnectionState,
    InstrumentControlState,
    InstrumentKind,
    InstrumentMetric,
    InstrumentSnapshot,
    Severity,
    StabilityState,
)
from labcontrol.measurement.manifest import ModuleColumn, load_manifest  # noqa: E402
from tests.configuration_fixtures import write_simulated_configuration  # noqa: E402


class DatafileTests(unittest.TestCase):
    def test_compact_measurement_data_has_control_temperature_and_sparse_module_columns(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config = load_config(write_simulated_configuration(temp_root))
            self.assertTrue(config.logging.compact_measurement_data)
            logger = DatRunLogger(config, EventManager())
            module = load_manifest(SIMULATED_MODULE)
            module.columns = (
                ModuleColumn("Delta_R1", "ohm"),
                ModuleColumn("Delta_R1_StdDev", "ohm"),
                ModuleColumn("Delta_R2", "ohm"),
                ModuleColumn("Delta_R2_StdDev", "ohm"),
                ModuleColumn("Delta_R3", "ohm"),
                ModuleColumn("Delta_R3_StdDev", "ohm"),
                ModuleColumn("Delta_R4", "ohm"),
                ModuleColumn("Delta_R4_StdDev", "ohm"),
                ModuleColumn("Delta_Current"),
                ModuleColumn("Delta_StatusCode"),
            )
            auxiliary = load_manifest(SIMULATED_MODULE)
            auxiliary.id = "simulated_auxiliary"
            auxiliary.columns = (
                ModuleColumn("Simu_StatusCode"),
            )
            paths = logger.open_run(
                "compact.seq",
                "T Measure\nT End Sequence\n",
                (module, auxiliary),
            )
            snapshots = {
                "temperature": InstrumentSnapshot(
                    "temperature",
                    "Temperature",
                    InstrumentKind.TEMPERATURE,
                    time.monotonic(),
                    "K",
                    305.1234,
                )
            }
            logger.write_measurement_row(
                snapshots,
                {
                    module.id: {
                        "Delta_R1": 1.2,
                        "Delta_R1_StdDev": 0.1,
                        "Delta_Current": 0.001,
                        "Delta_StatusCode": 0,
                    },
                    auxiliary.id: {"Simu_StatusCode": 7},
                },
                "Measure",
            )
            logger.write_measurement_row(
                snapshots,
                {
                    module.id: {
                        "Delta_R2": 2.3,
                        "Delta_R2_StdDev": 0.2,
                        "Delta_Current": -0.001,
                        "Delta_StatusCode": 1,
                    },
                    auxiliary.id: {"Simu_StatusCode": 8},
                },
                "Measure",
            )
            logger.close()

            data_section = paths.data_file.read_text(
                encoding="utf-8"
            ).split("[Data]\n", 1)[1]
            records = list(csv.DictReader(data_section.splitlines()))
            self.assertEqual(
                next(csv.reader(data_section.splitlines())),
                [
                    "Timestamp",
                    "Temp",
                    "Delta_R1(ohm)",
                    "Delta_R1_StdDev(ohm)",
                    "Delta_R2(ohm)",
                    "Delta_R2_StdDev(ohm)",
                    "Delta_R3(ohm)",
                    "Delta_R3_StdDev(ohm)",
                    "Delta_R4(ohm)",
                    "Delta_R4_StdDev(ohm)",
                    "Delta_Current",
                    "Delta_StatusCode",
                    "Simu_StatusCode",
                ],
            )
            self.assertEqual(records[0]["Temp"], "305.123")
            self.assertEqual(records[0]["Delta_R1(ohm)"], "1.2")
            self.assertEqual(
                records[0]["Delta_R1_StdDev(ohm)"],
                "0.1",
            )
            self.assertEqual(records[0]["Delta_R2(ohm)"], "")
            self.assertEqual(
                records[0]["Delta_R2_StdDev(ohm)"],
                "",
            )
            self.assertEqual(records[0]["Delta_Current"], "0.001")
            self.assertEqual(
                records[0]["Delta_StatusCode"],
                "0",
            )
            self.assertEqual(
                records[0]["Simu_StatusCode"],
                "7",
            )
            self.assertEqual(records[1]["Delta_R1(ohm)"], "")
            self.assertEqual(
                records[1]["Delta_R1_StdDev(ohm)"],
                "",
            )
            self.assertEqual(records[1]["Delta_R2(ohm)"], "2.3")
            self.assertEqual(
                records[1]["Delta_R2_StdDev(ohm)"],
                "0.2",
            )
            self.assertEqual(
                records[1]["Delta_Current"],
                "-0.001",
            )
            self.assertEqual(
                records[1]["Delta_StatusCode"],
                "1",
            )
            self.assertEqual(
                records[1]["Simu_StatusCode"],
                "8",
            )

    def test_compact_duplicate_module_columns_use_instance_letters(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config = load_config(
                write_simulated_configuration(temp_root)
            )
            config = replace(
                config,
                logging=replace(
                    config.logging,
                    compact_measurement_data=True,
                ),
            )
            columns = (
                ModuleColumn("Delta_R1", "ohm"),
                ModuleColumn("Delta_Current"),
                ModuleColumn("Delta_StatusCode"),
            )
            first = load_manifest(SIMULATED_MODULE)
            first.id = "delta_first"
            first.columns = columns
            second = load_manifest(SIMULATED_MODULE)
            second.id = "delta_second"
            second.columns = columns
            logger = DatRunLogger(config, EventManager())
            paths = logger.open_run(
                "two-delta.seq",
                "T Measure\nT End Sequence\n",
                (first, second),
            )
            logger.write_measurement_row(
                {},
                {
                    first.id: {
                        "Delta_R1": 1.1,
                        "Delta_Current": 0.001,
                        "Delta_StatusCode": 0,
                    },
                    second.id: {
                        "Delta_R1": 2.2,
                        "Delta_Current": -0.001,
                        "Delta_StatusCode": 1,
                    },
                },
                "Measure",
            )
            logger.close()

            data_section = paths.data_file.read_text(
                encoding="utf-8"
            ).split("[Data]\n", 1)[1]
            rows = list(
                csv.DictReader(data_section.splitlines())
            )
            self.assertEqual(
                next(csv.reader(data_section.splitlines())),
                [
                    "Timestamp",
                    "Temp",
                    "DeltaA_R1(ohm)",
                    "DeltaA_Current",
                    "DeltaA_StatusCode",
                    "DeltaB_R1(ohm)",
                    "DeltaB_Current",
                    "DeltaB_StatusCode",
                ],
            )
            self.assertEqual(rows[0]["DeltaA_R1(ohm)"], "1.1")
            self.assertEqual(rows[0]["DeltaA_StatusCode"], "0")
            self.assertEqual(rows[0]["DeltaB_R1(ohm)"], "2.2")
            self.assertEqual(rows[0]["DeltaB_StatusCode"], "1")

    def test_run_snapshot_copies_complete_site_configuration_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            configs = temp_root / "configs"
            instruments = configs / "instruments"
            pid = configs / "pid"
            instruments.mkdir(parents=True)
            pid.mkdir()
            general = configs / "general.toml"
            shutil.copy2(ROOT / "configs" / "general.toml", general)
            visa_text = (
                '[[resources]]\n'
                'id = "meter"\n'
                'address = "GPIB0::1::INSTR"\n'
                'identity = ""\n'
            )
            (configs / "visa.resources.toml").write_text(
                visa_text,
                encoding="utf-8",
            )
            instrument_text = """id = "simulated_second_stage"
name = "Simulated 2nd Stage"
version = "1.0.0"
api_version = "4"
backend = "labcontrol.instruments.simulated:SimulatedReadOnlyMonitor"
kinds = ["monitor"]

[[panels]]
id = "main"
label = "Simulated 2nd Stage"
template = "readout"
readings = ["value"]

[readings.value]
label = "Simulated 2nd Stage"
unit = "K"
decimals = 3

[[instances]]
id = "second_stage"
initial_value = 4.2
noise = 0.002

[[instances.panels]]
id = "main"
enabled = true
order = 1
role = "none"
"""
            instrument_path = instruments / "simulated_second_stage.toml"
            instrument_path.write_text(instrument_text, encoding="utf-8")
            pid_text = "zones = []\n"
            (pid / "unused.toml").write_text(pid_text, encoding="utf-8")

            logger = DatRunLogger(load_config(general), EventManager())
            paths = logger.open_run("snapshot.seq", "T End Sequence\n")
            with self.assertRaisesRegex(ValueError, "reserved run artifact"):
                logger.set_datafile("configuration/overwrite.dat")
            logger.close()

            snapshot = paths.configuration_snapshot
            self.assertTrue(snapshot.is_dir())
            self.assertEqual(
                (snapshot / "general.toml").read_text(encoding="utf-8"),
                general.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (snapshot / "visa.resources.toml").read_text(encoding="utf-8"),
                visa_text,
            )
            self.assertEqual(
                (snapshot / "instruments" / instrument_path.name).read_text(
                    encoding="utf-8"
                ),
                instrument_text,
            )
            self.assertEqual(
                (snapshot / "pid" / "unused.toml").read_text(encoding="utf-8"),
                pid_text,
            )

    def test_instrument_metrics_have_frozen_columns_in_measurement_and_status_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config_path = write_simulated_configuration(temp_root)
            config = load_config(config_path)
            config = replace(
                config,
                logging=replace(
                    config.logging,
                    compact_measurement_data=False,
                ),
            )
            logger = DatRunLogger(config, EventManager())
            now = time.monotonic()
            snapshots = {
                "temperature": InstrumentSnapshot(
                    instrument_id="temperature",
                    display_name="Temperature",
                    kind=InstrumentKind.TEMPERATURE,
                    timestamp=now,
                    unit="K",
                    current=4.2,
                    target=4.0,
                    rate_per_minute=1.0,
                    activity=InstrumentActivity.HOLDING,
                    controls={
                        "main": InstrumentControlState(
                            current=4.2,
                            target=4.0,
                            rate_per_minute=1.0,
                            activity=InstrumentActivity.HOLDING,
                        )
                    },
                    metrics={
                        "second_stage": InstrumentMetric(
                            "2nd Stage",
                            20.1254,
                            "K",
                            3,
                        ),
                        "heater_output": InstrumentMetric(
                            "Heater",
                            12.345,
                            "%",
                            2,
                        ),
                        "heater_range": InstrumentMetric(
                            "Range",
                            "LOW",
                        ),
                    },
                )
            }
            paths = logger.open_run(
                "metrics.seq",
                "T Measure\nT End Sequence\n",
                instrument_snapshots=snapshots,
            )
            logger.write_system_row(snapshots, "Measure")
            logger.write_instrument_status(snapshots, force=True)
            measurement_only = {
                "temperature": InstrumentSnapshot(
                    instrument_id="temperature",
                    display_name="Temperature",
                    kind=InstrumentKind.TEMPERATURE,
                    timestamp=now + 0.1,
                    unit="K",
                    current=4.25,
                    target=4.0,
                    rate_per_minute=1.0,
                    activity=InstrumentActivity.HOLDING,
                    metrics={
                        metric_key: InstrumentMetric(
                            metric.display_name,
                            None,
                            metric.unit,
                            metric.decimals,
                        )
                        for metric_key, metric
                        in snapshots["temperature"].metrics.items()
                    },
                )
            }
            measurement_row = dict(
                zip(
                    logger._build_columns(),
                    logger._row(measurement_only, {}, "Fast Measure"),
                    strict=True,
                )
            )
            self.assertEqual(measurement_row["Temp(K)"], "4.250")
            self.assertEqual(
                measurement_row["temperature.second_stage(K)"],
                "",
            )
            self.assertEqual(
                measurement_row["temperature.heater_output(%)"],
                "",
            )
            snapshots["temperature"].connection_state = (
                InstrumentConnectionState.DISCONNECTED
            )
            disconnected_row = dict(
                zip(
                    logger._build_columns(),
                    logger._row(snapshots, {}, "Disconnected"),
                    strict=True,
                )
            )
            self.assertEqual(disconnected_row["Temp(K)"], "")
            self.assertEqual(
                disconnected_row["temperature.second_stage(K)"],
                "",
            )
            logger.close()

            data = paths.data_file.read_text(encoding="utf-8")
            status = paths.instrument_status_file.read_text(encoding="utf-8")
            for text in (data, status):
                self.assertIn("temperature.second_stage(K)", text)
                self.assertIn("temperature.heater_output(%)", text)
                self.assertIn("temperature.heater_range", text)
                self.assertIn("20.125,12.35,LOW", text)

    def test_instrument_status_logs_each_controller_panel_and_physical_state_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            base_config = load_config(write_simulated_configuration(temp_root))
            instruments = {
                instrument.id: instrument
                for instrument in base_config.instrument_instances
            }
            temperature = instruments["temperature"]
            main_panel = temperature.panel("main")
            temperature = replace(
                temperature,
                panels=(
                    replace(
                        main_panel,
                        id="sample",
                        control_id="sample",
                    ),
                    replace(
                        main_panel,
                        id="shield",
                        control_id="shield",
                        role="none",
                    ),
                    replace(
                        main_panel,
                        id="disabled",
                        control_id="disabled",
                        enabled=False,
                        order=None,
                        role="none",
                    ),
                ),
            )
            config = SimpleNamespace(
                source_path=base_config.source_path,
                logging=base_config.logging,
                instrument_instances=(
                    temperature,
                    instruments["field"],
                    instruments["second_stage"],
                ),
                resolve_project_path=base_config.resolve_project_path,
            )
            now = time.monotonic()
            snapshots = {
                "temperature": InstrumentSnapshot(
                    instrument_id="temperature",
                    display_name="Temperature",
                    kind=InstrumentKind.TEMPERATURE,
                    timestamp=now,
                    unit="K",
                    current=999.0,
                    controls={
                        "sample": InstrumentControlState(
                            current=3.1236,
                            target=3.0,
                            rate_per_minute=1.0,
                            activity=InstrumentActivity.MOVING,
                            stability=StabilityState.SETTLING,
                            ready=False,
                        ),
                        "shield": InstrumentControlState(
                            current=5.6789,
                            target=5.5,
                            rate_per_minute=0.25,
                            activity=InstrumentActivity.HOLDING,
                            stability=StabilityState.STABLE,
                            ready=True,
                        ),
                    },
                    metrics={
                        "heater_output": InstrumentMetric(
                            "Heater",
                            12.345,
                            "%",
                            2,
                        )
                    },
                ),
                "field": InstrumentSnapshot(
                    instrument_id="field",
                    display_name="Field",
                    kind=InstrumentKind.FIELD,
                    timestamp=now,
                    unit="Oe",
                    current=888.0,
                    controls={
                        "main": InstrumentControlState(
                            current=123.456,
                            target=100.0,
                            rate_per_minute=10.0,
                            activity=InstrumentActivity.HOLDING,
                            stability=StabilityState.STABLE,
                            ready=True,
                        )
                    },
                ),
                "second_stage": InstrumentSnapshot(
                    instrument_id="second_stage",
                    display_name="Second Stage",
                    kind=InstrumentKind.MONITOR,
                    timestamp=now,
                    unit="K",
                    current=4.2345,
                ),
            }
            logger = DatRunLogger(config, EventManager())
            paths = logger.open_run(
                "panels.seq",
                "T End Sequence\n",
                instrument_snapshots=snapshots,
            )
            logger.write_instrument_status(snapshots, force=True)
            logger.close()

            status = paths.instrument_status_file.read_text(encoding="utf-8")
            reader = csv.DictReader(
                status.split("[Data]\n", 1)[1].splitlines()
            )
            columns = reader.fieldnames
            self.assertIsNotNone(columns)
            assert columns is not None
            row = next(reader)

            self.assertEqual(row["temperature.sample.Current(K)"], "3.124")
            self.assertEqual(row["temperature.sample.Target(K)"], "3.000")
            self.assertEqual(row["temperature.sample.Rate(K/min)"], "1.000")
            self.assertEqual(row["temperature.sample.Activity"], "moving")
            self.assertEqual(row["temperature.sample.Stability"], "settling")
            self.assertEqual(row["temperature.sample.Ready"], "false")
            self.assertEqual(row["temperature.shield.Current(K)"], "5.679")
            self.assertEqual(row["temperature.shield.Ready"], "true")
            self.assertEqual(row["field.main.Current(Oe)"], "123.456")
            self.assertEqual(row["second_stage.Current(K)"], "4.234")
            self.assertEqual(row["temperature.heater_output(%)"], "12.35")

            self.assertNotIn("temperature.Current(K)", columns)
            self.assertNotIn("field.Current(Oe)", columns)
            self.assertNotIn("temperature.disabled.Current(K)", columns)
            self.assertNotIn("second_stage.Target(K)", columns)
            self.assertEqual(columns.count("temperature.Connection"), 1)
            self.assertEqual(columns.count("temperature.ReadingAge(s)"), 1)
            self.assertEqual(columns.count("temperature.Message"), 1)
            self.assertEqual(columns.count("temperature.heater_output(%)"), 1)
            self.assertNotIn("temperature.sample.Connection", columns)
            self.assertNotIn("temperature.shield.Connection", columns)

    def test_writes_header_sparse_rows_and_event_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config_path = write_simulated_configuration(temp_root)
            config = load_config(config_path)
            config = replace(
                config,
                logging=replace(
                    config.logging,
                    compact_measurement_data=False,
                ),
            )
            events = EventManager()
            logger = DatRunLogger(config, events)
            module = load_manifest(SIMULATED_MODULE)
            module.columns = (
                ModuleColumn("R1", "Ohm"),
                ModuleColumn("R2", "Ohm"),
                ModuleColumn("StatusCode"),
            )
            paths = logger.open_run(
                "test.seq",
                "T Measure\nT End Sequence\n",
                (module,),
                {module.id: {"delay_seconds": 0.01}},
                {module.id: {"Connection": "Connected"}},
            )
            now = time.monotonic()
            snapshots = {
                "temperature": InstrumentSnapshot(
                    "temperature",
                    "温度",
                    InstrumentKind.TEMPERATURE,
                    now,
                    "K",
                    3.1236,
                    3.0,
                    1.0,
                    InstrumentActivity.HOLDING,
                    controls={
                        "main": InstrumentControlState(
                            current=3.1236,
                            target=3.0,
                            rate_per_minute=1.0,
                            activity=InstrumentActivity.HOLDING,
                        )
                    },
                ),
                "field": InstrumentSnapshot(
                    "field",
                    "磁场",
                    InstrumentKind.FIELD,
                    now,
                    "Oe",
                    123.456,
                    100.0,
                    10.0,
                    InstrumentActivity.HOLDING,
                    controls={
                        "main": InstrumentControlState(
                            current=123.456,
                            target=100.0,
                            rate_per_minute=10.0,
                            activity=InstrumentActivity.HOLDING,
                        )
                    },
                ),
                "second_stage": InstrumentSnapshot("second_stage", "2nd Stage", InstrumentKind.MONITOR, now, "K", 4.2345),
            }
            self.assertTrue(
                logger.write_instrument_status(
                    snapshots,
                    force=True,
                )
            )
            self.assertFalse(
                logger.write_instrument_status(snapshots)
            )
            logger.write_module_row(
                snapshots,
                module.id,
                {"R1": 1.2, "StatusCode": 0},
                "Measure",
                raw_values=(1.0e-9, 2.0e-9),
            )
            logger.write_module_row(
                snapshots,
                module.id,
                {"R2": 2.3, "StatusCode": 0},
                "Measure",
                raw_values=(-3.0e-9,),
            )
            events.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
            events.report(Severity.WARNING, "meter", "OVERLOAD", "overload")
            time.sleep(0.001)
            events.resolve("meter", "OVERLOAD")
            logger.close()
            data = paths.data_file.read_text(encoding="utf-8")
            event_data = paths.event_file.read_text(encoding="utf-8")
            instrument_status = paths.instrument_status_file.read_text(
                encoding="utf-8"
            )
            self.assertIn("[Header]", data)
            self.assertIn(
                "TIMESTAMP_EPOCH,labview_1904",
                data,
            )
            self.assertIn("[Data]", data)
            self.assertIn(
                "Module simulated_transport: Simulated Transport; version=2.0.1",
                data,
            )
            self.assertIn("simulated_transport.R1(Ohm)", data)
            self.assertIn("Field(Oe)", data)
            self.assertIn("second_stage(K)", data)
            self.assertIn(",3.124,3.000,123.46,100.00,4.234,", data)
            self.assertEqual(sum(1 for line in data.splitlines() if ",Measure," in line), 2)
            self.assertTrue((paths.module_settings_directory / f"{module.id}.settings.toml").exists())
            self.assertTrue((paths.module_settings_directory / f"{module.id}.status-at-start.json").exists())
            raw_files = tuple(
                paths.raw_data_directory.glob("*.rawdata")
            )
            self.assertEqual(len(raw_files), 1)
            self.assertEqual(
                raw_files[0].read_text(
                    encoding="utf-8"
                ).splitlines(),
                [
                    "1.0000000000000001e-09,"
                    "2.0000000000000001e-09",
                    "-3e-09",
                ],
            )
            self.assertIn("RAISED", event_data)
            self.assertIn("RESOLVED", event_data)
            self.assertIn(",2,", event_data)
            self.assertIn(
                "temperature.main.Current(K),"
                "temperature.main.Target(K),"
                "temperature.main.Rate(K/min),"
                "temperature.main.Activity,"
                "temperature.main.Stability,"
                "temperature.main.Ready,"
                "temperature.Connection,"
                "temperature.ReadingAge(s),"
                "temperature.Message",
                instrument_status,
            )
            self.assertIn(
                ",3.124,3.000,1.000,holding,"
                "not_applicable,,connected,",
                instrument_status,
            )
            self.assertIn(
                "TIMESTAMP_EPOCH,labview_1904",
                instrument_status,
            )
            status_rows = (
                instrument_status.split("[Data]\n", 1)[1]
                .strip()
                .splitlines()
            )
            self.assertEqual(len(status_rows), 2)
            event_rows = [
                line.split(",")
                for line in event_data.splitlines()
                if ",meter,OVERLOAD," in line
            ]
            self.assertEqual(len(event_rows), 2)
            raised_at = datetime.fromisoformat(event_rows[0][1])
            resolved_at = datetime.fromisoformat(event_rows[1][1])
            self.assertGreater(resolved_at, raised_at)

    def test_rawdata_distinguishes_same_stem_and_resets_with_create(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config_path = write_simulated_configuration(temp_root)
            config = load_config(config_path)
            module = load_manifest(SIMULATED_MODULE)
            logger = DatRunLogger(config, EventManager())
            paths = logger.open_run(
                "raw-switch.seq",
                "T End Sequence\n",
                (module,),
            )
            first = temp_root / "first" / "sample.dat"
            second = temp_root / "second" / "sample.dat"

            logger.set_datafile(
                str(first),
                "create",
                allow_external=True,
            )
            logger.write_module_row(
                {},
                module.id,
                {"R1": 1.0},
                "first",
                raw_values=(1.0,),
            )
            logger.set_datafile(
                str(second),
                "create",
                allow_external=True,
            )
            logger.write_module_row(
                {},
                module.id,
                {"R1": 2.0},
                "second",
                raw_values=(2.0,),
            )
            # 回到同一路径并使用 create 会重建正式 DAT；对应的旧 rawdata 也应
            # 清空，而另一个同 stem 文件的 sidecar 保持不变。
            logger.set_datafile(
                str(first),
                "create",
                allow_external=True,
            )
            logger.write_module_row(
                {},
                module.id,
                {"R1": 3.0},
                "first-recreated",
                raw_values=(3.0,),
            )
            logger.close()

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
                {"2", "3"},
            )
            self.assertEqual(
                len({path.name for path in raw_files}),
                2,
            )
            self.assertTrue(
                all(
                    path.name.startswith("sample__")
                    for path in raw_files
                )
            )

    def test_explicit_custom_folder_is_allowed_without_weakening_legacy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config_path = write_simulated_configuration(temp_root)
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
            config_path = write_simulated_configuration(temp_root)
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
            paths = logger.paths
            self.assertIsNotNone(paths)
            protected_status = paths.instrument_status_file.read_text(
                encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValueError,
                "reserved run artifact",
            ):
                logger.set_datafile(
                    str(paths.instrument_status_file),
                    "create",
                    allow_external=True,
                )
            self.assertEqual(
                paths.instrument_status_file.read_text(encoding="utf-8"),
                protected_status,
            )
            protected_module_setting = (
                paths.module_settings_directory
                / "protected.settings.toml"
            )
            protected_module_setting.write_text(
                "keep = true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "reserved run artifact",
            ):
                logger.set_datafile(
                    str(protected_module_setting),
                    "create",
                    allow_external=True,
                )
            self.assertEqual(
                protected_module_setting.read_text(encoding="utf-8"),
                "keep = true\n",
            )
            logger.close()

    def test_matching_schema_appends_and_empty_run_creates_default_dat(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config_path = write_simulated_configuration(temp_root)
            config = load_config(config_path)
            config = replace(
                config,
                logging=replace(
                    config.logging,
                    compact_measurement_data=False,
                ),
            )
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
            self.assertTrue(paths.instrument_status_file.exists())
            self.assertIn("[Data]", paths.data_file.read_text(encoding="utf-8"))

    def test_run_directory_allocation_retries_an_atomic_creation_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config_path = write_simulated_configuration(temp_root)
            config = load_config(config_path)
            runs_root = temp_root / "runs"
            resolved_runs_root = runs_root.resolve()
            original_mkdir = Path.mkdir
            injected_race = False

            def racing_mkdir(path, *args, **kwargs):
                nonlocal injected_race
                if (
                    not injected_race
                    # GitHub 的 Windows runner 可能让 tempfile 返回 8.3 短路径，
                    # 而配置加载会把同一目录规范化成长路径。比较真实路径，避免把
                    # C:\Users\RUNNER~1 与 C:\Users\runneradmin 误判为不同目录。
                    and path.parent.resolve() == resolved_runs_root
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
