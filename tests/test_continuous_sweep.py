from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from labcontrol.events import EventManager
from labcontrol.instruments.base import InstrumentError
from labcontrol.models import InstrumentActivity, InstrumentControlState, InstrumentKind, InstrumentSnapshot, RunState
from labcontrol.sequence.engine import SequenceAbort, SequenceEngine
from labcontrol.sequence.model import Command, CommandType
from tests.configuration_fixtures import load_simulated_config


class ContinuousSweepTests(unittest.IsolatedAsyncioTestCase):
    def make_scan(self, kind=InstrumentKind.TEMPERATURE, start=10.0, stop=20.0, steps=11):
        config = load_simulated_config()
        instrument_id = "temperature" if kind is InstrumentKind.TEMPERATURE else "field"
        instrument = config.instrument(instrument_id)
        panel = instrument.panel("main")
        panel = replace(panel, stability=replace(panel.stability, tolerance=0.001))
        state = InstrumentControlState(current=start, target=start, activity=InstrumentActivity.HOLDING)
        snapshot = InstrumentSnapshot(instrument_id, instrument.display_name, kind, 0.0,
                                      unit=instrument.unit, controls={panel.id: state})
        writes = []

        async def set_target(inst, value, rate, mode="Settle", *, control):
            writes.append((value, mode))
            state.target = value
            state.activity = InstrumentActivity.MOVING
            return True

        manager = SimpleNamespace(
            instrument_configs={instrument_id: instrument}, latest={instrument_id: snapshot},
            control_ready=True, resolve_control_panel=lambda _: panel,
            validate_target=Mock(), set_target=AsyncMock(side_effect=set_target),
        )
        engine = SequenceEngine(config, manager, EventManager(), object(), object())
        engine.state = RunState.RUNNING
        engine._wait_for_stability = AsyncMock()
        engine._wait_for_target = AsyncMock()
        command = Command(CommandType.SCAN_TEMPERATURE if kind is InstrumentKind.TEMPERATURE else CommandType.SCAN_FIELD,
                          {"start": start, "stop": stop, "steps": steps, "rate": 1.0,
                           "mode": "Sweep", **({"unit": "Oe"} if kind is InstrumentKind.FIELD else {})},
                          [Command(CommandType.MEASURE)])
        engine._total_steps = steps + 1
        return engine, manager, state, writes, command, panel

    async def test_initial_point_settles_and_is_measured_before_one_final_target(self):
        engine, manager, state, writes, command, panel = self.make_scan()
        order = []

        async def stable(*_):
            order.append("stable")

        async def group(children, path):
            order.append(("measure", len(writes), state.current))

        async def advance(_):
            state.current = 20.0

        engine._wait_for_stability.side_effect = stable
        engine._execute_commands = AsyncMock(side_effect=group)
        engine._interruptible_sleep = AsyncMock(side_effect=advance)
        await engine._scan_controlled(command, InstrumentKind.TEMPERATURE, ["scan"])
        self.assertEqual(writes, [(10.0, "Settle"), (20.0, "Sweep")])
        self.assertEqual(order, ["stable", ("measure", 1, 10.0), ("measure", 2, 20.0)])
        engine._wait_for_stability.assert_awaited_once()
        engine._wait_for_target.assert_not_awaited()

    async def test_thresholds_catch_up_in_order_without_intermediate_writes(self):
        for kind, start, stop in ((InstrumentKind.TEMPERATURE, 10.0, 20.0),
                                  (InstrumentKind.TEMPERATURE, 20.0, 10.0),
                                  (InstrumentKind.FIELD, -5.0, 5.0),
                                  (InstrumentKind.FIELD, 5.0, -5.0)):
            with self.subTest(kind=kind, start=start):
                engine, manager, state, writes, command, panel = self.make_scan(kind, start, stop)
                direction = 1 if stop > start else -1
                samples = iter((start + direction * 3.2, start + direction * 6.5, stop))
                groups = []

                async def advance(_):
                    state.current = next(samples)

                async def group(children, path):
                    groups.append((path[-1].split("=")[0], state.current))

                engine._interruptible_sleep = AsyncMock(side_effect=advance)
                engine._execute_commands = AsyncMock(side_effect=group)
                await engine._scan_controlled(command, kind, ["scan"])
                self.assertEqual(writes, [(start, "Settle"), (stop, "Sweep")])
                self.assertEqual(
                    [label for label, _ in groups],
                    [f"point {i}/11" for i in range(1, 8)] + ["point 11/11"],
                )
                self.assertEqual(
                    [value for _, value in groups],
                    [start] + [start + direction * 3.2] * 3 + [start + direction * 6.5] * 3 + [stop],
                )
                self.assertEqual(engine._total_steps, 9)

    async def test_endpoint_during_a_group_finishes_whole_group_then_continues_seq(self):
        engine, manager, state, writes, command, panel = self.make_scan()
        command.children = [Command(CommandType.MEASURE), Command(CommandType.MEASURE)]
        after = Command(CommandType.REMARK, {"text": "after sweep"})
        engine._total_steps = 24
        samples = []
        notices = []
        engine.events.subscribe_occurrences(notices.append)

        async def advance(_):
            state.current = 11.0

        async def measure():
            samples.append(state.current)
            if len(samples) == 3:
                state.current = 20.0

        engine._measure = AsyncMock(side_effect=measure)
        engine._interruptible_sleep = AsyncMock(side_effect=advance)
        await engine._execute_commands([command, after], [])
        self.assertEqual(samples, [10.0, 10.0, 11.0, 20.0])
        self.assertEqual(engine._completed_steps, 6)
        self.assertEqual(engine._total_steps, 6)
        self.assertEqual(engine.state, RunState.RUNNING)
        self.assertTrue(any(n.event.code == "REMARK" and n.event.message == "after sweep"
                            for n in notices))

    async def test_single_point_only_sets_and_measures_initial_point(self):
        engine, manager, state, writes, command, panel = self.make_scan(steps=1)
        engine._execute_commands = AsyncMock()
        await engine._scan_controlled(command, InstrumentKind.TEMPERATURE, [])
        self.assertEqual(writes, [(10.0, "Settle")])
        engine._execute_commands.assert_awaited_once()

    async def test_log_and_monotonic_list_keep_threshold_order_and_duplicates(self):
        for point_mode, points, count in (("Log", None, 3), ("List", "[10, 12, 12, 15, 20]", 5)):
            with self.subTest(point_mode=point_mode):
                engine, manager, state, writes, command, panel = self.make_scan(
                    start=10.0, stop=1000.0 if point_mode == "Log" else 20.0, steps=count)
                command.params.update(point_mode=point_mode)
                if points is not None:
                    command.params["points"] = points
                samples = iter((150.0, 1000.0) if point_mode == "Log" else (15.1, 20.0))
                groups = []

                async def advance(_):
                    state.current = next(samples)

                async def group(children, path):
                    groups.append(path[-1].split("=")[0])

                engine._interruptible_sleep = AsyncMock(side_effect=advance)
                engine._execute_commands = AsyncMock(side_effect=group)
                await engine._scan_controlled(command, InstrumentKind.TEMPERATURE, [])
                self.assertEqual(groups, [f"point {i}/{count}" for i in range(1, count + 1)])
                self.assertEqual(len(writes), 2)

    async def test_two_point_and_full_sweep_measure_final_endpoint(self):
        for steps, readings in (
            (2, (15.0, 20.0)),
            (5, (12.5, 15.0, 17.5, 19.9995)),
        ):
            with self.subTest(steps=steps):
                engine, manager, state, writes, command, panel = self.make_scan(
                    InstrumentKind.TEMPERATURE, 10.0, 20.0, steps=steps
                )
                samples = iter(readings)
                measured = []

                async def advance(_):
                    state.current = next(samples)

                async def group(children, path):
                    measured.append((path[-1].split("=")[0], state.current))

                engine._interruptible_sleep = AsyncMock(side_effect=advance)
                engine._execute_commands = AsyncMock(side_effect=group)
                await engine._scan_controlled(command, InstrumentKind.TEMPERATURE, [])
                self.assertEqual(writes, [(10.0, "Settle"), (20.0, "Sweep")])
                self.assertEqual(
                    [label for label, _ in measured],
                    [f"point {i}/{steps}" for i in range(1, steps + 1)],
                )
                self.assertEqual(measured[-1][1], readings[-1])
                self.assertEqual(engine._total_steps, steps + 1)

    async def test_nonmonotonic_sweep_list_rejected_before_any_write(self):
        engine, manager, state, writes, command, panel = self.make_scan()
        command.params.update(point_mode="List", points="[10, 15, 12, 20]")
        with self.assertRaises(InstrumentError):
            await engine._scan_controlled(command, InstrumentKind.TEMPERATURE, [])
        self.assertEqual(writes, [])

    async def test_stop_interrupts_threshold_wait_without_retargeting(self):
        engine, manager, state, writes, command, panel = self.make_scan()
        engine._execute_commands = AsyncMock()

        async def stop(_):
            engine.request_stop()

        engine._interruptible_sleep = AsyncMock(side_effect=stop)
        with self.assertRaises(SequenceAbort):
            await engine._scan_controlled(command, InstrumentKind.TEMPERATURE, [])
        self.assertEqual(writes, [(10.0, "Settle"), (20.0, "Sweep")])
