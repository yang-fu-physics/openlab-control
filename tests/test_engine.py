from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.models import (  # noqa: E402
    InstrumentActivity,
    InstrumentConnectionState,
    InstrumentControlState,
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
from tests.configuration_fixtures import (  # noqa: E402
    load_simulated_config,
    write_simulated_configuration,
)


class SequenceEngineTests(unittest.TestCase):
    def _fast_config(
        self,
        temp_root: Path,
        *,
        blocking_module: bool = False,
    ):
        target = write_simulated_configuration(temp_root)
        shutil.copytree(
            ROOT / "modules",
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
        instruments = []
        for instrument in config.instrument_instances:
            panels = []
            for panel in instrument.panels:
                panel_stability = panel.stability
                if panel_stability is not None:
                    panel_stability = replace(
                        panel_stability,
                        tolerance=max(panel_stability.tolerance, 0.005),
                        max_slope_per_minute=100.0,
                        dwell_seconds=0.05,
                        timeout_seconds=3.0,
                        window_seconds=0.05,
                    )
                panels.append(replace(panel, stability=panel_stability))
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
            instruments.append(
                replace(
                    instrument,
                    panels=tuple(panels),
                    stability=stability,
                    extras=extras,
                )
            )
        return replace(
            config,
            simulation_speed=1000.0,
            poll_interval_seconds=0.01,
            instrument_instances=tuple(instruments),
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

    def test_saving_called_sequence_during_run_only_affects_the_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._fast_config(root)
            child = root / "child.seq"
            child.write_text("T Remark original child\nT End Sequence\n", encoding="utf-8")
            marker = Command(CommandType.REMARK, {"text": "edit marker"})
            document = SequenceDocument([marker, Command(CommandType.CALL_SEQUENCE, {"path": "child.seq"})])
            edited = []
            def progress_changed(progress):
                if progress.command_id == marker.id and not edited:
                    child.write_text("T Remark updated child\nT End Sequence\n", encoding="utf-8")
                    edited.append(True)
            notices = []
            state, _, _ = asyncio.run(self._run(config, document, notices, SimpleNamespace(append=progress_changed)))
            self.assertEqual(state, RunState.COMPLETED)
            self.assertTrue(edited)
            messages = [notice.event.message for notice in notices]
            self.assertIn("original child", messages)
            self.assertNotIn("updated child", messages)
            next_notices = []
            state, _, _ = asyncio.run(self._run(config, document, next_notices))
            self.assertEqual(state, RunState.COMPLETED)
            self.assertIn("updated child", [notice.event.message for notice in next_notices])

    def test_continuous_sweep_with_simulated_controller_records_initial_and_continues(self):
        from unittest.mock import patch

        original_set_target = InstrumentManager.set_target
        writes = []
        initial_stability = []

        async def observe(manager, instrument_id, value, rate_per_minute, mode="Settle", *, control, origin="sequence"):
            if instrument_id == "temperature":
                writes.append((value, mode))
                if len(writes) == 2:
                    initial_stability.append(manager.latest[instrument_id].controls["main"].stability)
            return await original_set_target(manager, instrument_id, value, rate_per_minute,
                                             mode, control=control, origin=origin)

        with tempfile.TemporaryDirectory() as temporary:
            config = self._fast_config(Path(temporary))
            document = SequenceDocument([
                Command(CommandType.SCAN_TEMPERATURE,
                        {"start": 299.8, "stop": 299.0, "steps": 9, "rate": 10.0, "mode": "Sweep"},
                        [Command(CommandType.MEASURE)]),
                Command(CommandType.REMARK, {"text": "after continuous sweep"}),
            ])
            notices, progresses = [], []
            with patch.object(InstrumentManager, "set_target", observe):
                state, snapshots, paths = asyncio.run(self._run(config, document, notices, progresses))
            self.assertEqual(state, RunState.COMPLETED)
            self.assertEqual(writes, [(299.8, "Settle"), (299.0, "Sweep")])
            self.assertEqual(initial_stability, [StabilityState.STABLE])
            self.assertEqual(snapshots["temperature"].controls["main"].target, 299.0)
            self.assertTrue(any(n.event.code == "SWEEP_END_REACHED" for n in notices))
            self.assertTrue(any(n.event.message == "after continuous sweep" for n in notices))
            self.assertTrue(paths.data_file.is_file())
            self.assertEqual(progresses[-1].completed_steps, progresses[-1].total_steps)

    def test_nested_temperature_field_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            measure = Command(CommandType.MEASURE)
            field_scan = Command(CommandType.SCAN_FIELD, {
                "start": 0.0, "stop": 0.01, "unit": "T",
                "steps": 2, "rate": 0.5, "mode": "Settle",
            }, [measure])
            temperature_scan = Command(CommandType.SCAN_TEMPERATURE, {
                "start": 300.0, "stop": 299.9,
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
                "temperature.main.Stability",
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
                            "target": -6.0,
                            "unit": "T",
                            "rate": 1.0,
                        },
                    ),
                    Command(
                        CommandType.WAIT,
                        {
                            "wait_temperature": False,
                            "wait_field": True,
                            "seconds": 0.0,
                        },
                    ),
                    Command(
                        CommandType.SCAN_FIELD,
                        {
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
            before = manager.latest["field"].controls["main"].target
            manager.latest["field"].controls["main"].current = None
            engine.state = RunState.RUNNING
            scan = Command(
                CommandType.SCAN_FIELD,
                {
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
                self.assertEqual(
                    manager.latest["field"].controls["main"].target,
                    before,
                )
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
                    "target": 1.0, "unit": "T", "rate": 0.5,
                }),
                Command(CommandType.WAIT, {"seconds": 0.1}),
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

        asyncio.run(scenario(load_simulated_config()))

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
            panels = tuple(
                replace(
                    panel,
                    stability=replace(
                        panel.stability,
                        timeout_seconds=0.05,
                    ),
                )
                if panel.role == "sample_temp"
                else panel
                for panel in temperature.panels
            )
            temperature = replace(
                temperature,
                panels=panels,
                stability=replace(
                    temperature.stability,
                    timeout_seconds=0.05,
                ),
            )
            config = replace(
                config,
                poll_interval_seconds=0.005,
                instrument_instances=tuple(
                    temperature if instrument.id == temperature.id else instrument
                    for instrument in config.instrument_instances
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
                activity=InstrumentActivity.HOLDING,
                stability=StabilityState.MOVING,
                controls={
                    "main": InstrumentControlState(
                        current=300.0,
                        target=299.0,
                        rate_per_minute=1.0,
                        activity=InstrumentActivity.HOLDING,
                        stability=StabilityState.MOVING,
                    )
                },
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
            panel = manager.resolve_control_panel(InstrumentKind.TEMPERATURE)

            for wait in (engine._wait_for_stability, engine._wait_for_target):
                engine.state = RunState.RUNNING
                engine._abort_requested = False
                engine._fatal_abort = False
                events.resolve("temperature", "STABILITY_TIMEOUT")
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        wait("temperature", panel),
                        timeout=0.25,
                    )

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

    def test_wait_timers_follow_moving_not_ready(self) -> None:
        async def scenario(config) -> None:
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            panel = manager.resolve_control_panel(InstrumentKind.TEMPERATURE)
            panel = replace(panel, stability=replace(panel.stability, timeout_seconds=0.04))
            engine = SequenceEngine(
                replace(config, control_poll_interval_seconds=0.005), manager, events,
                object(), object(),
            )
            engine.state = RunState.RUNNING
            control = manager.latest["temperature"].controls[panel.id]
            for wait in (engine._wait_for_stability, engine._wait_for_target):
                with self.subTest(wait=wait.__name__):
                    events.resolve("temperature", "STABILITY_TIMEOUT")
                    notices.clear()
                    control.current, control.target = 300.0, 299.0
                    control.activity = InstrumentActivity.MOVING
                    control.stability = StabilityState.MOVING
                    control.ready = None
                    task = asyncio.create_task(wait("temperature", panel))
                    try:
                        await asyncio.sleep(0.1)
                        self.assertFalse(task.done())
                        self.assertFalse(any(n.event.code == "STABILITY_TIMEOUT" for n in notices))
                        control.activity = InstrumentActivity.HOLDING
                        control.ready = False
                        await asyncio.sleep(0.1)
                        self.assertFalse(task.done())
                        warnings = [n for n in notices if n.event.code == "STABILITY_TIMEOUT"
                                    and not n.is_resolution]
                        self.assertEqual(len(warnings), 1)
                        self.assertEqual(warnings[0].event.severity, Severity.WARNING)
                        control.current = control.target
                        control.ready = True
                        control.stability = StabilityState.STABLE
                        await asyncio.wait_for(task, timeout=0.3)
                    finally:
                        if not task.done():
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(self._fast_config(Path(temp))))

    def test_runtime_rejects_programmatic_parameter_limit_bypasses(self) -> None:
        cases = (
            Command(CommandType.WAIT, {"seconds": -1.0}),
            Command(CommandType.WAIT, {"wait_temperature": "yes", "seconds": 0.0}),
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

    def test_temperature_log_scan_executes_logarithmic_points(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            measure = Command(CommandType.MEASURE)
            temperature_scan = Command(
                CommandType.SCAN_TEMPERATURE,
                {
                    "point_mode": "Log",
                    "start": 300.0,
                    "stop": 3.0,
                    "steps": 3,
                    "rate": 30.0,
                    "mode": "Settle",
                },
                [measure],
            )
            notices = []
            state, _, paths = asyncio.run(
                self._run(config, SequenceDocument([temperature_scan], "temperature-log.seq"), notices)
            )
            self.assertEqual(state, RunState.COMPLETED)
            data = paths.data_file.read_text(encoding="utf-8")
            p1 = data.index("point 1/3=300.000 K")
            p2 = data.index("point 2/3=30.000 K", p1)
            p3 = data.index("point 3/3=3.000 K", p2)
            self.assertLess(p1, p2)
            self.assertLess(p2, p3)

    def test_temperature_list_is_fully_validated_before_first_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._fast_config(Path(temp))
            temperature_scan = Command(
                CommandType.SCAN_TEMPERATURE,
                {
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

    def test_progress_command_id_tracks_scan_points_and_keeps_call_sequence_highlighted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            config = self._fast_config(temp_root)
            measure = Command(CommandType.MEASURE)
            scan = Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 0.0, "steps": 2},
                [measure],
            )
            child_file = temp_root / "sub.seq"
            child_file.write_text(
                "T Wait For 0.0 secs\n"
                "T Scan Time 0.0 secs in 2 steps\n"
                "T     Measure\n"
                "T End Scan\n"
                "T End Sequence\n",
                encoding="utf-8",
            )
            call_cmd = Command(CommandType.CALL_SEQUENCE, {"path": "sub.seq"})
            document = SequenceDocument(
                [scan, call_cmd],
                "highlight.seq",
                temp_root / "highlight.seq",
            )
            notices = []
            progresses = []
            state, _, _ = asyncio.run(
                self._run(config, document, notices, progresses)
            )
            self.assertEqual(state, RunState.COMPLETED)
            scan_and_child_ids = [
                p.command_id for p in progresses if p.command_id in {scan.id, measure.id}
            ]
            # Must return to scan.id before point 2 executes measure.id.
            first_measure = scan_and_child_ids.index(measure.id)
            second_scan = scan_and_child_ids.index(scan.id, first_measure)
            second_measure = scan_and_child_ids.index(measure.id, second_scan)
            self.assertLess(first_measure, second_scan)
            self.assertLess(second_scan, second_measure)
            # While inside Call Sequence, command_id must never leak subsequence UUIDs.
            known_ids = {"", scan.id, measure.id, call_cmd.id}
            self.assertTrue(all(p.command_id in known_ids for p in progresses))
            self.assertIn(call_cmd.id, [p.command_id for p in progresses])

    def test_set_temperature_and_field_are_nonblocking_and_wait_settles_both_then_delays(
        self,
    ) -> None:
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

            original_set_target = manager.set_target
            temp_stability_when_field_started: list[StabilityState | None] = []

            async def observing_set_target(
                instrument_id: str,
                value: float,
                rate_per_minute: float,
                mode: str = "Settle",
                *,
                control: str,
                origin: str = "sequence",
            ) -> bool:
                if instrument_id == "field":
                    temp_snap = manager.latest.get("temperature")
                    temp_stability_when_field_started.append(
                        None if temp_snap is None else temp_snap.controls["main"].stability
                    )
                return await original_set_target(
                    instrument_id,
                    value,
                    rate_per_minute,
                    mode,
                    control=control,
                    origin=origin,
                )

            manager.set_target = observing_set_target  # type: ignore[method-assign]

            async def poll():
                while True:
                    await manager.poll_all()
                    await asyncio.sleep(config.poll_interval_seconds)

            document = SequenceDocument(
                [
                    Command(CommandType.SET_TEMPERATURE, {"target": 299.5, "rate": 10.0}),
                    Command(CommandType.SET_FIELD, {"target": 5000.0, "unit": "Oe", "rate": 5000.0}),
                    Command(
                        CommandType.WAIT,
                        {"wait_temperature": True, "wait_field": True, "seconds": 0.2},
                    ),
                ],
                "parallel-ramp-wait.seq",
            )
            poll_task = asyncio.create_task(poll())
            started = time.monotonic()
            try:
                state = await engine.run(document)
                elapsed = time.monotonic() - started
                snapshots = manager.snapshots()
            finally:
                poll_task.cancel()
                await asyncio.gather(poll_task, return_exceptions=True)
                await modules.shutdown()
                await manager.disconnect_all()

            self.assertEqual(state, RunState.COMPLETED)
            self.assertEqual(temp_stability_when_field_started, [StabilityState.MOVING])
            self.assertEqual(snapshots["temperature"].controls["main"].stability, StabilityState.STABLE)
            self.assertEqual(snapshots["field"].controls["main"].stability, StabilityState.STABLE)
            self.assertAlmostEqual(snapshots["temperature"].current or 0.0, 299.5, places=2)
            self.assertAlmostEqual(snapshots["field"].current or 0.0, 5000.0, places=1)
            self.assertGreaterEqual(elapsed, 0.18)

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(self._fast_config(Path(temp))))

    def test_wait_requires_simultaneous_stability_after_disturbance(self) -> None:
        async def scenario(config) -> None:
            config = replace(config, control_poll_interval_seconds=0.01)
            events = EventManager()
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            engine = SequenceEngine(
                config,
                manager,
                events,
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
            engine.state = RunState.RUNNING

            def set_stability(instrument_id: str, state: StabilityState) -> None:
                snap = manager.latest[instrument_id]
                snap.controls["main"].stability = state
                snap.stability = state

            # T stable, H moving -> must keep waiting
            set_stability("temperature", StabilityState.STABLE)
            set_stability("field", StabilityState.MOVING)

            temp_panel = manager.resolve_control_panel(InstrumentKind.TEMPERATURE)
            field_panel = manager.resolve_control_panel(InstrumentKind.FIELD)
            wait_task = asyncio.create_task(
                engine._wait_for_all_stability(
                    (("temperature", temp_panel), ("field", field_panel))
                )
            )
            await asyncio.sleep(0.03)
            self.assertFalse(wait_task.done())

            # Eddy heating knocks T back to SETTLING, then H reaches STABLE -> still must wait!
            set_stability("temperature", StabilityState.SETTLING)
            set_stability("field", StabilityState.STABLE)
            await asyncio.sleep(0.03)
            self.assertFalse(wait_task.done())

            # Both STABLE simultaneously -> wait completes!
            set_stability("temperature", StabilityState.STABLE)
            await asyncio.wait_for(wait_task, timeout=0.3)
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(self._fast_config(Path(temp))))

    def test_wait_does_not_spuriously_timeout_already_stable_controller(self) -> None:
        async def scenario(config) -> None:
            temperature = config.instrument("temperature")
            temperature = replace(
                temperature,
                panels=tuple(
                    replace(panel, stability=replace(panel.stability, timeout_seconds=0.04))
                    if panel.role == "sample_temp"
                    else panel
                    for panel in temperature.panels
                ),
            )
            config = replace(
                config,
                control_poll_interval_seconds=0.01,
                instrument_instances=tuple(
                    temperature if inst.id == "temperature" else inst
                    for inst in config.instrument_instances
                ),
            )
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            engine = SequenceEngine(
                config,
                manager,
                events,
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
            engine.state = RunState.RUNNING

            manager.latest["temperature"].controls["main"].stability = StabilityState.STABLE
            manager.latest["field"].controls["main"].stability = StabilityState.MOVING

            temp_panel = manager.resolve_control_panel(InstrumentKind.TEMPERATURE)
            field_panel = manager.resolve_control_panel(InstrumentKind.FIELD)
            wait_task = asyncio.create_task(
                engine._wait_for_all_stability(
                    (("temperature", temp_panel), ("field", field_panel))
                )
            )
            await asyncio.sleep(0.09)
            self.assertFalse(wait_task.done())
            manager.latest["field"].controls["main"].stability = StabilityState.STABLE
            await asyncio.wait_for(wait_task, timeout=0.3)
            timeout_events = [
                notice for notice in notices if notice.event.code == "STABILITY_TIMEOUT"
            ]
            self.assertEqual(timeout_events, [])
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(self._fast_config(Path(temp))))

    def test_wait_warning_timeout_on_one_controller_still_waits_for_other(self) -> None:
        async def scenario(config) -> None:
            temperature = config.instrument("temperature")
            temperature = replace(
                temperature,
                panels=tuple(
                    replace(panel, stability=replace(panel.stability, timeout_seconds=0.04))
                    if panel.role == "sample_temp"
                    else panel
                    for panel in temperature.panels
                ),
            )
            config = replace(
                config,
                control_poll_interval_seconds=0.01,
                alarms=replace(config.alarms, stability_timeout=Severity.WARNING),
                instrument_instances=tuple(
                    temperature if inst.id == "temperature" else inst
                    for inst in config.instrument_instances
                ),
            )
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(config, events, isolate_processes=False)
            await manager.connect_all()
            await manager.poll_all()
            engine = SequenceEngine(
                config,
                manager,
                events,
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
            engine.state = RunState.RUNNING

            manager.latest["temperature"].controls["main"].stability = StabilityState.MOVING
            manager.latest["field"].controls["main"].stability = StabilityState.MOVING

            temp_panel = manager.resolve_control_panel(InstrumentKind.TEMPERATURE)
            field_panel = manager.resolve_control_panel(InstrumentKind.FIELD)
            wait_task = asyncio.create_task(
                engine._wait_for_all_stability(
                    (("temperature", temp_panel), ("field", field_panel))
                )
            )
            await asyncio.sleep(0.09)
            self.assertFalse(wait_task.done())
            manager.latest["field"].controls["main"].stability = StabilityState.STABLE
            await asyncio.sleep(0.03)
            self.assertFalse(wait_task.done())
            manager.latest["temperature"].controls["main"].stability = StabilityState.STABLE
            await asyncio.wait_for(wait_task, timeout=0.3)
            timeout_events = [
                notice
                for notice in notices
                if notice.event.code == "STABILITY_TIMEOUT" and not notice.is_resolution
            ]
            self.assertEqual(len(timeout_events), 1)
            self.assertEqual(timeout_events[0].event.severity, Severity.WARNING)
            await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(self._fast_config(Path(temp))))

    def test_wait_for_unconfigured_controller_faults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = self._fast_config(Path(temp))
            no_field = replace(
                base,
                instrument_instances=tuple(
                    inst for inst in base.instrument_instances if inst.id != "field"
                ),
            )
            notices = []
            state, _, _ = asyncio.run(
                self._run(
                    no_field,
                    SequenceDocument(
                        [Command(CommandType.WAIT, {"wait_field": True, "seconds": 0.0})],
                        "wait-no-field.seq",
                    ),
                    notices,
                )
            )
            self.assertEqual(state, RunState.FAULTED)
            self.assertIn(
                "INSTRUMENT_NOT_CONFIGURED",
                [notice.event.code for notice in notices if not notice.is_resolution],
            )


if __name__ == "__main__":
    unittest.main()
