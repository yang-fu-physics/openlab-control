from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_REPOSITORY = (
    ROOT
    / "templates"
    / "measurement-modules-repository"
)
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.package_support.trust import ContentTrustStore  # noqa: E402
from labcontrol.models import (  # noqa: E402
    InstrumentActivity,
    InstrumentConnectionState,
    InstrumentKind,
    InstrumentSnapshot,
    RunState,
    Severity,
    StabilityState,
)
from labcontrol.measurement.manifest import discover_modules  # noqa: E402
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.instruments.base import InstrumentError  # noqa: E402
from labcontrol.sequence.engine import SequenceAbort, SequenceEngine  # noqa: E402
from labcontrol.sequence.model import Command, CommandType, SequenceDocument  # noqa: E402


class SequenceEngineTests(unittest.TestCase):
    def _fast_config(
        self,
        temp_root: Path,
        *,
        blocking_module: bool = False,
    ):
        (temp_root / "configs").mkdir()
        target = temp_root / "configs" / "default.toml"
        shutil.copy2(ROOT / "configs" / "default.toml", target)
        shutil.copytree(
            MODULE_REPOSITORY / "modules",
            temp_root / "modules",
        )
        if blocking_module:
            module_root = temp_root / "modules" / "blocking_module"
            module_root.mkdir()
            (module_root / "module.toml").write_text(
                'name = "Blocking Test Module"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            (module_root / "backend.py").write_text(
                "class Module:\n"
                "    columns = {'Value': ''}\n"
                "    def open(self, api):\n"
                "        return {}\n"
                "    def measure(self, slot, api):\n"
                "        api.sleep(5.0)\n"
                "        return {'Value': 1.0}\n"
                "    def close(self, api):\n"
                "        return {}\n",
                encoding="utf-8",
            )
        config = load_config(target)
        trust_store = ContentTrustStore(
            config.resolve_project_path(
                config.modules.state_directory
            )
            / "trusted_content.json"
        )
        for descriptor in discover_modules(config):
            trust_store.trust("module", descriptor)
        instruments = []
        for instrument in config.instruments:
            stability = instrument.stability
            if stability is not None:
                stability = replace(
                    stability,
                    tolerance=max(stability.tolerance, 0.005),
                    max_slope_per_minute=100.0,
                    dwell_seconds=0.05,
                    timeout_seconds=3.0,
                    window_seconds=0.05,
                )
            extras = dict(instrument.extras)
            extras["noise"] = 0.0
            instruments.append(replace(instrument, stability=stability, extras=extras))
        return replace(
            config,
            simulation_speed=1000.0,
            poll_interval_seconds=0.01,
            instruments=tuple(instruments),
        )

    async def _run(self, config, document, notices, progresses=None):
        events = EventManager()
        events.subscribe(notices.append)
        manager = InstrumentManager(config, events, isolate_processes=False)
        logger = DatRunLogger(config, events)
        modules = MeasurementModuleService(discover_modules(config), events, manager)
        engine = SequenceEngine(
            config,
            manager,
            events,
            logger,
            modules,
            progress_callback=(
                progresses.append
                if progresses is not None
                else None
            ),
        )
        await manager.connect_all()
        await manager.poll_all()
        await modules.enable("simulated_transport")

        async def poll():
            while True:
                await manager.poll_all()
                await asyncio.sleep(config.poll_interval_seconds)

        poll_task = asyncio.create_task(poll())
        try:
            state = await engine.run(document)
            await manager.poll_all()
            return state, manager.snapshots(), logger.paths
        finally:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
            await modules.shutdown()
            await manager.disconnect_all()

    def test_nested_temperature_field_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            measure = Command(CommandType.MEASURE)
            field_scan = Command(CommandType.SCAN_FIELD, {
                "instrument_id": "field", "start": 0.0, "stop": 0.01, "unit": "T",
                "steps": 2, "rate": 0.5, "mode": "Settle",
            }, [measure])
            temperature_scan = Command(CommandType.SCAN_TEMPERATURE, {
                "instrument_id": "temperature", "start": 300.0, "stop": 299.9,
                "steps": 2, "rate": 10.0, "mode": "Settle",
            }, [field_scan])
            document = SequenceDocument([temperature_scan], "nested.seq")
            notices = []
            progresses = []
            state, _, paths = asyncio.run(
                self._run(config, document, notices, progresses)
            )
            self.assertEqual(state, RunState.COMPLETED)
            self.assertIsNotNone(paths)
            data = paths.data_file.read_text(encoding="utf-8")
            instrument_status = paths.instrument_status_file.read_text(
                encoding="utf-8"
            )
            self.assertGreaterEqual(data.count("Measure"), 16)
            self.assertIn(
                "temperature.Stability",
                instrument_status,
            )
            self.assertGreaterEqual(
                len(
                    instrument_status.split("[Data]\n", 1)[1]
                    .strip()
                    .splitlines()
                ),
                2,
            )
            self.assertEqual(progresses[-1].completed_steps, 7)
            self.assertEqual(progresses[-1].total_steps, 7)
            self.assertTrue(
                all(
                    progress.completed_steps <= progress.total_steps
                    for progress in progresses
                )
            )

    def test_field_scan_nearest_polarity_uses_runtime_actual_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = replace(
                self._fast_config(Path(temp)),
                simulation_speed=100000.0,
            )
            document = SequenceDocument(
                [
                    Command(
                        CommandType.SET_FIELD,
                        {
                            "instrument_id": "field",
                            "target": -6.0,
                            "unit": "T",
                            "rate": 1.0,
                            "mode": "Settle",
                        },
                    ),
                    Command(
                        CommandType.SCAN_FIELD,
                        {
                            "instrument_id": "field",
                            "start": 9.0,
                            "stop": 3.0,
                            "unit": "T",
                            "steps": 2,
                            "rate": 1.0,
                            "mode": "Settle",
                            "nearest_polarity": True,
                        },
                    ),
                ],
                "nearest-polarity.seq",
            )
            notices = []
            state, snapshots, _ = asyncio.run(
                self._run(config, document, notices)
            )

            self.assertEqual(state, RunState.COMPLETED)
            self.assertAlmostEqual(
                snapshots["field"].current or 0.0,
                -30000.0,
                places=2,
            )
            selections = [
                notice.event
                for notice in notices
                if notice.event.code == "FIELD_SCAN_POLARITY_SELECTED"
            ]
            self.assertEqual(len(selections), 1)
            self.assertIn("actual field -60000.00 Oe", selections[0].message)
            self.assertIn(
                "selected sign-inverted path -90000.00 to -30000.00 Oe",
                selections[0].message,
            )

    def test_field_scan_nearest_polarity_tie_keeps_entered_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = replace(
                self._fast_config(Path(temp)),
                simulation_speed=100000.0,
            )
            scan = Command(
                CommandType.SCAN_FIELD,
                {
                    "instrument_id": "field",
                    "start": 1.0,
                    "stop": 0.5,
                    "unit": "T",
                    "steps": 2,
                    "rate": 1.0,
                    "mode": "Settle",
                    "nearest_polarity": True,
                },
            )
            notices = []
            state, snapshots, _ = asyncio.run(
                self._run(
                    config,
                    SequenceDocument([scan], "polarity-tie.seq"),
                    notices,
                )
            )

            self.assertEqual(state, RunState.COMPLETED)
            self.assertAlmostEqual(
                snapshots["field"].current or 0.0,
                5000.0,
                places=2,
            )
            selection = next(
                notice.event
                for notice in notices
                if notice.event.code == "FIELD_SCAN_POLARITY_SELECTED"
            )
            self.assertIn("selected entered path", selection.message)

    def test_field_scan_nearest_polarity_refuses_missing_actual_field(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            manager = InstrumentManager(config, events, isolate_processes=False)
            engine = SequenceEngine(
                config,
                manager,
                events,
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
            await manager.connect_all()
            await manager.poll_all()
            before = manager.latest["field"].target
            manager.latest["field"].current = None
            engine.state = RunState.RUNNING
            scan = Command(
                CommandType.SCAN_FIELD,
                {
                    "instrument_id": "field",
                    "start": 9.0,
                    "stop": 3.0,
                    "unit": "T",
                    "steps": 2,
                    "rate": 1.0,
                    "mode": "Settle",
                    "nearest_polarity": True,
                },
            )
            try:
                with self.assertRaises(InstrumentError) as raised:
                    await engine._scan_controlled(
                        scan,
                        InstrumentKind.FIELD,
                        ["1:Scan Field"],
                    )
                self.assertEqual(
                    raised.exception.code,
                    "FIELD_SCAN_CURRENT_UNAVAILABLE",
                )
                self.assertEqual(manager.latest["field"].target, before)
            finally:
                await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(self._fast_config(Path(temp))))

    def test_duplicate_warning_continues_and_only_notifies_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            document = SequenceDocument([
                Command(CommandType.INJECT_WARNING, {"code": "SAME", "message": "same"}),
                Command(CommandType.INJECT_WARNING, {"code": "SAME", "message": "same"}),
                Command(CommandType.MEASURE),
            ], "warning.seq")
            notices = []
            state, _, _ = asyncio.run(self._run(config, document, notices))
            warning_notices = [
                notice for notice in notices
                if notice.event.severity is Severity.WARNING and notice.event.code == "SAME" and not notice.is_resolution
            ]
            self.assertEqual(state, RunState.COMPLETED)
            self.assertEqual(len(warning_notices), 1)
            self.assertTrue(warning_notices[0].show_popup)

    def test_error_aborts_without_changing_system_instrument_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            document = SequenceDocument([
                Command(CommandType.SET_FIELD, {
                    "instrument_id": "field", "target": 1.0, "unit": "T", "rate": 0.5, "mode": "Sweep",
                }),
                Command(CommandType.WAIT, {"seconds": 0.03}),
                Command(CommandType.INJECT_ERROR, {"code": "FATAL", "message": "fatal"}),
                Command(CommandType.MEASURE),
            ], "error.seq")
            notices = []
            state, snapshots, _ = asyncio.run(self._run(config, document, notices))
            field = snapshots["field"]
            self.assertEqual(state, RunState.FAULTED)
            self.assertAlmostEqual(field.target or 0.0, 10_000.0, places=4)

    def test_latched_idle_error_repeated_while_running_requests_fatal_stop(self) -> None:
        events = EventManager()
        engine = SequenceEngine(
            object(), object(), events, object(), object()  # type: ignore[arg-type]
        )
        events.report(Severity.ERROR, "instrument", "FAULT", "fault")
        self.assertEqual(engine.state, RunState.IDLE)
        engine.state = RunState.RUNNING
        events.report(Severity.ERROR, "instrument", "FAULT", "fault persists")
        self.assertEqual(engine.state, RunState.STOPPING)
        self.assertTrue(engine._abort_requested)
        self.assertTrue(engine._fatal_abort)

    def test_pause_freezes_interruptible_wait_deadline(self) -> None:
        async def scenario() -> None:
            events = EventManager()
            engine = SequenceEngine(
                object(), object(), events, object(), object()  # type: ignore[arg-type]
            )
            engine.state = RunState.RUNNING
            wait_task = asyncio.create_task(engine._interruptible_sleep(0.16))
            await asyncio.sleep(0.03)
            engine.pause()
            await asyncio.sleep(0.20)
            self.assertFalse(wait_task.done())
            resumed_at = asyncio.get_running_loop().time()
            engine.resume()
            await asyncio.wait_for(wait_task, timeout=0.5)
            self.assertGreaterEqual(
                asyncio.get_running_loop().time() - resumed_at,
                0.09,
            )

        asyncio.run(scenario())

    def test_instrument_recovery_freezes_interruptible_wait_deadline(
        self,
    ) -> None:
        class RecoveringInstruments:
            control_ready = True

            @staticmethod
            def control_block_reason():
                return "primary temperature is reconnecting"

        async def scenario() -> None:
            events = EventManager()
            instruments = RecoveringInstruments()
            engine = SequenceEngine(
                object(), instruments, events, object(), object()  # type: ignore[arg-type]
            )
            engine.state = RunState.RUNNING
            wait_task = asyncio.create_task(
                engine._interruptible_sleep(0.16)
            )
            await asyncio.sleep(0.03)
            instruments.control_ready = False
            await asyncio.sleep(0.20)
            self.assertFalse(wait_task.done())
            restored_at = asyncio.get_running_loop().time()
            instruments.control_ready = True
            await asyncio.wait_for(wait_task, timeout=0.5)
            self.assertGreaterEqual(
                asyncio.get_running_loop().time() - restored_at,
                0.09,
            )

        asyncio.run(scenario())

    def test_run_preflight_rejects_missing_primary_readback(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            await manager.connect_all()
            engine = SequenceEngine(
                config,
                manager,
                events,
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
            state = await engine.run(
                SequenceDocument([], "preflight.seq")
            )
            self.assertEqual(state, RunState.FAULTED)
            self.assertIn(
                "PRIMARY_INSTRUMENT_NOT_READY",
                [notice.event.code for notice in notices],
            )
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(
                scenario(self._fast_config(Path(temp)))
            )

    def test_task_cancellation_does_not_hold_system_instruments(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            logger = DatRunLogger(config, events)
            modules = MeasurementModuleService((), events, manager)
            engine = SequenceEngine(
                config,
                manager,
                events,
                logger,
                modules,
            )
            await manager.connect_all()
            await manager.poll_all()
            held: list[str] = []
            for instrument_id in ("temperature", "field"):
                async def record_hold(selected=instrument_id):
                    held.append(selected)

                manager.instruments[instrument_id].hold = record_hold  # type: ignore[method-assign]
            task = asyncio.create_task(
                engine.run(
                    SequenceDocument(
                        [
                            Command(
                                CommandType.WAIT,
                                {"seconds": 5.0},
                            )
                        ],
                        "cancel.seq",
                    )
                )
            )
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(held, [])
            await modules.shutdown()
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(
                scenario(self._fast_config(Path(temp)))
            )

    def test_control_waits_timeout_without_new_instrument_readings(self) -> None:
        async def scenario(config) -> None:
            temperature = config.instrument("temperature")
            temperature = replace(
                temperature,
                stability=replace(
                    temperature.stability,
                    timeout_seconds=0.05,
                ),
            )
            config = replace(
                config,
                poll_interval_seconds=0.005,
                instruments=tuple(
                    temperature if instrument.id == temperature.id else instrument
                    for instrument in config.instruments
                ),
            )
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            now = asyncio.get_running_loop().time()
            manager.latest["temperature"] = InstrumentSnapshot(
                instrument_id="temperature",
                display_name=temperature.display_name,
                kind=InstrumentKind.TEMPERATURE,
                timestamp=now,
                unit=temperature.unit,
                current=300.0,
                target=299.0,
                rate_per_minute=1.0,
                activity=InstrumentActivity.MOVING,
                stability=StabilityState.MOVING,
            )
            manager._connection_states["temperature"] = (
                InstrumentConnectionState.CONNECTED
            )
            engine = SequenceEngine(
                config,
                manager,
                events,
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )

            for wait in (engine._wait_for_stability, engine._wait_for_target):
                engine.state = RunState.RUNNING
                engine._abort_requested = False
                engine._fatal_abort = False
                events.resolve("temperature", "STABILITY_TIMEOUT")
                with self.assertRaises(SequenceAbort):
                    await asyncio.wait_for(wait("temperature"), timeout=0.25)

            timeout_notices = [
                notice
                for notice in notices
                if notice.event.code == "STABILITY_TIMEOUT"
                and not notice.is_resolution
            ]
            self.assertEqual(len(timeout_notices), 2)
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            asyncio.run(scenario(config))

    def test_runtime_rejects_programmatic_parameter_limit_bypasses(self) -> None:
        cases = (
            Command(CommandType.WAIT, {"seconds": -1.0}),
            Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 1.0, "steps": 1_000_001},
            ),
            Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 1.0, "steps": float("inf")},
            ),
            Command(
                CommandType.SCAN_FIELD,
                {
                    "instrument_id": "field",
                    "start": 0.0,
                    "stop": 1.0,
                    "unit": "Oe",
                    "steps": 2,
                    "rate": 0.0,
                    "mode": "Settle",
                },
            ),
            Command(
                CommandType.SCAN_FIELD,
                {
                    "instrument_id": "field",
                    "start": 0.0,
                    "stop": 1.0,
                    "unit": "Oe",
                    "steps": 2,
                    "rate": 1.0,
                    "mode": "Settle",
                    "nearest_polarity": "yes",
                },
            ),
        )
        for command in cases:
            with self.subTest(command=command.type.value):
                with tempfile.TemporaryDirectory() as temp:
                    config = self._fast_config(Path(temp))
                    notices = []
                    state, _, _ = asyncio.run(
                        self._run(
                            config,
                            SequenceDocument([command], "invalid-parameter.seq"),
                            notices,
                        )
                    )
                    self.assertEqual(state, RunState.FAULTED)
                    self.assertIn(
                        "INVALID_SEQUENCE_PARAMETER",
                        [
                            notice.event.code
                            for notice in notices
                            if not notice.is_resolution
                        ],
                    )

    def test_stop_does_not_call_hold_all(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            logger = DatRunLogger(config, events)
            modules = MeasurementModuleService((), events, manager)
            engine = SequenceEngine(config, manager, events, logger, modules)
            await manager.connect_all()
            await manager.poll_all()

            async def unexpected_hold() -> bool:
                raise AssertionError("SEQ Stop must not control System Instruments")

            manager.hold_all = unexpected_hold  # type: ignore[method-assign]
            run_task = asyncio.create_task(
                engine.run(
                    SequenceDocument(
                        [Command(CommandType.WAIT, {"seconds": 5.0})],
                        "hold-failed.seq",
                    )
                )
            )
            await asyncio.sleep(0.03)
            engine.request_stop(False, "Stopped by test")
            state = await asyncio.wait_for(run_task, timeout=1.0)
            self.assertEqual(state, RunState.STOPPED)
            self.assertEqual(engine._abort_message, "Stopped by test")
            self.assertIn(
                "RUN_STOPPED",
                [notice.event.code for notice in notices],
            )
            await modules.shutdown()
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            asyncio.run(scenario(config))

    def test_stop_interrupts_an_inflight_module_measurement(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            logger = DatRunLogger(config, events)
            modules = MeasurementModuleService(
                discover_modules(config),
                events,
                manager,
            )
            engine = SequenceEngine(
                config,
                manager,
                events,
                logger,
                modules,
            )
            await manager.connect_all()
            await manager.poll_all()
            await modules.enable("blocking_module")
            try:
                run_task = asyncio.create_task(
                    engine.run(
                        SequenceDocument(
                            [Command(CommandType.MEASURE)],
                            "stop-module-measure.seq",
                        )
                    )
                )
                deadline = time.monotonic() + 2.0
                while (
                    modules.records[
                        "blocking_module"
                    ].state
                    != "measuring"
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.01)
                self.assertEqual(
                    modules.records[
                        "blocking_module"
                    ].state,
                    "measuring",
                )
                started = time.monotonic()
                engine.request_stop(
                    False,
                    "Stopped during module measurement",
                )
                state = await asyncio.wait_for(
                    run_task,
                    timeout=2.0,
                )
                self.assertLess(
                    time.monotonic() - started,
                    1.0,
                )
                self.assertEqual(state, RunState.STOPPED)
            finally:
                await modules.shutdown()
                await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(
                scenario(
                    self._fast_config(
                        Path(temp),
                        blocking_module=True,
                    )
                )
            )

    def test_progress_expands_called_sequence_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config = self._fast_config(temp_root)
            child = temp_root / "child.seq"
            child.write_text(
                "T Wait For 0.0 secs\n"
                "T Scan Time 0.0 secs in 2 steps\n"
                "T     Measure\n"
                "T End Scan\n"
                "T End Sequence\n",
                encoding="utf-8",
            )
            document = SequenceDocument(
                [Command(CommandType.CALL_SEQUENCE, {"path": "child.seq"})],
                "main.seq",
                temp_root / "main.seq",
            )
            notices = []
            progresses = []
            state, _, _ = asyncio.run(
                self._run(config, document, notices, progresses)
            )
            self.assertEqual(state, RunState.COMPLETED)
            self.assertEqual(progresses[-1].completed_steps, 5)
            self.assertEqual(progresses[-1].total_steps, 5)

    def test_disabled_command_and_scan_block_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            disabled_scan = Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 0.0, "steps": 1},
                [Command(CommandType.INJECT_ERROR, {"code": "NESTED_FATAL", "message": "must not run"})],
                enabled=False,
            )
            document = SequenceDocument([
                Command(
                    CommandType.INJECT_ERROR,
                    {"code": "DIRECT_FATAL", "message": "must not run"},
                    enabled=False,
                ),
                disabled_scan,
                Command(CommandType.MEASURE),
            ], "disabled.seq")
            notices = []
            state, _, paths = asyncio.run(self._run(config, document, notices))
            self.assertEqual(state, RunState.COMPLETED)
            self.assertTrue(paths.data_file.exists())
            codes = [notice.event.code for notice in notices if not notice.is_resolution]
            self.assertNotIn("DIRECT_FATAL", codes)
            self.assertNotIn("NESTED_FATAL", codes)
            self.assertEqual(codes.count("STEP_SKIPPED_DISABLED"), 2)

    def test_temperature_list_executes_in_declared_order_with_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            measure = Command(CommandType.MEASURE)
            temperature_scan = Command(
                CommandType.SCAN_TEMPERATURE,
                {
                    "instrument_id": "temperature",
                    "point_mode": "List",
                    "points": "300, 299.9, 300",
                    "rate": 10.0,
                    "mode": "Settle",
                },
                [measure],
            )
            notices = []
            state, _, paths = asyncio.run(
                self._run(config, SequenceDocument([temperature_scan], "temperature-list.seq"), notices)
            )
            self.assertEqual(state, RunState.COMPLETED)
            data = paths.data_file.read_text(encoding="utf-8")
            first = data.index("point 1/3=300.000 K")
            second = data.index("point 2/3=299.900 K", first)
            third = data.index("point 3/3=300.000 K", second)
            self.assertLess(first, second)
            self.assertLess(second, third)

    def test_temperature_list_is_fully_validated_before_first_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            temperature_scan = Command(
                CommandType.SCAN_TEMPERATURE,
                {
                    "instrument_id": "temperature",
                    "point_mode": "List",
                    "points": "299.9, 500",
                    "rate": 10.0,
                    "mode": "Settle",
                },
            )
            notices = []
            state, snapshots, _ = asyncio.run(
                self._run(config, SequenceDocument([temperature_scan], "unsafe-list.seq"), notices)
            )
            self.assertEqual(state, RunState.FAULTED)
            temperature = snapshots["temperature"]
            self.assertAlmostEqual(temperature.current or 0.0, 300.0, places=3)
            self.assertAlmostEqual(temperature.target or 0.0, 300.0, places=3)
            self.assertIn(
                "TARGET_OUT_OF_RANGE",
                [notice.event.code for notice in notices if not notice.is_resolution],
            )

    def test_custom_datafile_command_writes_to_selected_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config = self._fast_config(temp_root)
            custom_path = temp_root / "selected output" / "measurement.dat"
            document = SequenceDocument([
                Command(CommandType.SET_DATAFILE, {
                    "mode": "create",
                    "path_scope": "Custom folder",
                    "path": str(custom_path),
                }),
                Command(CommandType.MEASURE),
            ], "custom-output.seq")
            notices = []
            state, _, paths = asyncio.run(self._run(config, document, notices))
            self.assertEqual(state, RunState.COMPLETED)
            self.assertEqual(paths.data_file, custom_path)
            self.assertTrue(custom_path.exists())
            self.assertIn(
                "DATAFILE_SELECTED",
                [notice.event.code for notice in notices if not notice.is_resolution],
            )

    def test_module_measure_error_calls_end_error_without_abort(self) -> None:
        class FailingModules:
            def __init__(self) -> None:
                self.begin_called = False
                self.end_reasons: list[str] = []
                self.abort_called = False

            async def prepare_sequence(self):
                return (), {}

            async def begin_sequence(self):
                self.begin_called = True

            async def measure_all(self, logger, sequence_step):
                del logger, sequence_step
                raise InstrumentError("module equipment alarm", "MODULE_EQUIPMENT_ALARM")

            async def end_sequence(self, reason):
                self.end_reasons.append(reason)
                return True

        async def scenario(config):
            events = EventManager()
            manager = InstrumentManager(config, events, isolate_processes=False)
            logger = DatRunLogger(config, events)
            modules = FailingModules()
            engine = SequenceEngine(config, manager, events, logger, modules)  # type: ignore[arg-type]
            await manager.connect_all()
            await manager.poll_all()
            try:
                state = await engine.run(
                    SequenceDocument([Command(CommandType.MEASURE)], "module-error.seq")
                )
            finally:
                await manager.disconnect_all()
            self.assertEqual(state, RunState.FAULTED)
            self.assertTrue(modules.begin_called)
            self.assertEqual(modules.end_reasons, ["error"])
            self.assertFalse(modules.abort_called)

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(self._fast_config(Path(temp))))


if __name__ == "__main__":
    unittest.main()
