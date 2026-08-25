from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.instruments.base import (  # noqa: E402
    EventResponseSpec,
    InstrumentError,
    InstrumentWarning,
)
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.models import RunState, Severity  # noqa: E402
from labcontrol.sequence.engine import SequenceEngine  # noqa: E402
from labcontrol.sequence.model import (  # noqa: E402
    Command,
    CommandType,
    SequenceDocument,
)


class EventResponseTests(unittest.TestCase):
    def test_info_with_registered_code_does_not_trigger_response(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            temperature = manager.instruments["temperature"].backend
            temperature.event_responses = lambda: (  # type: ignore[method-assign]
                EventResponseSpec(code="COLD_HEAD_ALARM", action="zero"),
            )
            field = manager.instruments["field"].backend
            original_set_target = field.set_target
            writes: list[float] = []

            def record_set_target(
                value: float,
                rate_per_minute: float,
                mode: str = "Settle",
            ) -> None:
                writes.append(value)
                original_set_target(value, rate_per_minute, mode)

            field.set_target = record_set_target  # type: ignore[method-assign]
            await manager.connect_all()
            try:
                events.report(
                    Severity.INFO,
                    "temperature",
                    "COLD_HEAD_ALARM",
                    "Informational status",
                )
                await asyncio.sleep(0)
                self.assertEqual(writes, [])
                self.assertEqual(manager._event_response_target_locks, {})
            finally:
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_response_cannot_reset_while_action_is_running(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            temperature = manager.instruments["temperature"].backend
            temperature.event_responses = lambda: (  # type: ignore[method-assign]
                EventResponseSpec(code="COLD_HEAD_ALARM", action="zero"),
            )
            await manager.connect_all()
            gate = manager._operation_gates["field"]
            await gate.acquire(0)
            gate_held = True
            try:
                event, _changed = events.report(
                    Severity.ERROR,
                    "temperature",
                    "COLD_HEAD_ALARM",
                    "Cold head alarm",
                )
                await asyncio.sleep(0)
                events.resolve("temperature", "COLD_HEAD_ALARM")
                with self.assertRaises(InstrumentWarning) as captured:
                    manager.reset_event_response(event.key)
                self.assertEqual(
                    captured.exception.code,
                    "EVENT_RESPONSE_IN_PROGRESS",
                )
                gate.release()
                gate_held = False
                await asyncio.gather(
                    *tuple(manager._event_response_tasks.values())
                )
                manager.reset_event_response(event.key)
            finally:
                if gate_held:
                    gate.release()
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_queued_set_cannot_override_higher_priority_zero(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            temperature = manager.instruments["temperature"].backend
            temperature.event_responses = lambda: (  # type: ignore[method-assign]
                EventResponseSpec(code="COLD_HEAD_ALARM", action="zero"),
            )
            field = manager.instruments["field"].backend
            original_set_target = field.set_target
            writes: list[float] = []

            def record_set_target(
                value: float,
                rate_per_minute: float,
                mode: str = "Settle",
            ) -> None:
                writes.append(value)
                original_set_target(value, rate_per_minute, mode)

            field.set_target = record_set_target  # type: ignore[method-assign]
            await manager.connect_all()
            try:
                gate = manager._operation_gates["field"]
                await gate.acquire(0)
                queued_set = asyncio.create_task(
                    manager.set_target("field", 100.0, 10.0)
                )
                await asyncio.sleep(0)
                events.report(
                    Severity.ERROR,
                    "temperature",
                    "COLD_HEAD_ALARM",
                    "Cold head alarm",
                )
                await asyncio.sleep(0)
                gate.release()

                self.assertFalse(await queued_set)
                response_tasks = tuple(
                    manager._event_response_tasks.values()
                )
                if response_tasks:
                    await asyncio.gather(*response_tasks)
                self.assertEqual(writes, [0.0])
            finally:
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_registered_error_acts_while_paused_and_faults_sequence(self) -> None:
        async def scenario(root: Path) -> None:
            (root / "configs").mkdir()
            base = load_config(ROOT / "configs" / "default.toml")
            config = replace(
                base,
                source_path=root / "configs" / "default.toml",
            )
            events = EventManager()
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            temperature = manager.instruments["temperature"].backend
            temperature.event_responses = lambda: (  # type: ignore[method-assign]
                EventResponseSpec(
                    code="COLD_HEAD_ALARM",
                    action="zero",
                ),
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
            try:
                await manager.poll_all()
                await manager.set_target(
                    "field",
                    100.0,
                    10.0,
                )
                run_task = asyncio.create_task(
                    engine.run(
                        SequenceDocument(
                            [Command(CommandType.WAIT, {"seconds": 5.0})],
                            "event-response.seq",
                        )
                    )
                )
                await asyncio.sleep(0.03)
                engine.pause()
                events.report(
                    Severity.ERROR,
                    "temperature",
                    "COLD_HEAD_ALARM",
                    "Cold head alarm",
                )
                state = await asyncio.wait_for(run_task, timeout=1.0)
                response_tasks = tuple(
                    manager._event_response_tasks.values()
                )
                if response_tasks:
                    await asyncio.gather(*response_tasks)
                self.assertEqual(state, RunState.FAULTED)
                self.assertEqual(manager.latest["field"].target, 0.0)
            finally:
                await modules.shutdown()
                await manager.disconnect_all()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(scenario(Path(temporary)))

    def test_registered_error_zeros_once_and_locks_until_reset(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            temperature = manager.instruments["temperature"].backend
            temperature.event_responses = lambda: (  # type: ignore[method-assign]
                EventResponseSpec(
                    code="COLD_HEAD_ALARM",
                    context="A",
                    action="zero",
                ),
            )
            field = manager.instruments["field"].backend
            original_set_target = field.set_target
            writes: list[tuple[float, float, str]] = []

            def record_set_target(
                value: float,
                rate_per_minute: float,
                mode: str = "Settle",
            ) -> None:
                writes.append((value, rate_per_minute, mode))
                original_set_target(value, rate_per_minute, mode)

            field.set_target = record_set_target  # type: ignore[method-assign]
            await manager.connect_all()
            try:
                await manager.poll_all()
                event, _changed = events.report(
                    Severity.ERROR,
                    "temperature",
                    "COLD_HEAD_ALARM",
                    "Cold head alarm",
                    "A",
                )
                await asyncio.gather(
                    *tuple(manager._event_response_tasks.values())
                )
                events.report(
                    Severity.ERROR,
                    "temperature",
                    "COLD_HEAD_ALARM",
                    "Cold head alarm persists",
                    "A",
                )
                await asyncio.sleep(0)
                self.assertEqual(
                    writes,
                    [
                        (
                            0.0,
                            config.instrument("field").default_rate_per_minute,
                            "Sweep",
                        )
                    ],
                )

                applied = await manager.set_target(
                    "field",
                    100.0,
                    10.0,
                    origin="manual",
                )
                self.assertFalse(applied)
                with self.assertRaises(InstrumentWarning):
                    manager.reset_event_response(event.key)

                events.resolve(
                    "temperature",
                    "COLD_HEAD_ALARM",
                    "A",
                )
                manager.reset_event_response(event.key)
                applied = await manager.set_target(
                    "field",
                    100.0,
                    10.0,
                    origin="manual",
                )
                self.assertTrue(applied)
                self.assertEqual(writes[-1], (100.0, 10.0, "Settle"))
            finally:
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_each_active_response_keeps_the_shared_target_locked(self) -> None:
        async def scenario() -> None:
            config = load_config(ROOT / "configs" / "default.toml")
            events = EventManager()
            manager = InstrumentManager(
                config,
                events,
                isolate_processes=False,
            )
            temperature = manager.instruments["temperature"].backend
            temperature.event_responses = lambda: (  # type: ignore[method-assign]
                EventResponseSpec(code="ALARM_A", action="zero"),
                EventResponseSpec(code="ALARM_B", action="zero"),
            )
            await manager.connect_all()
            try:
                first, _changed = events.report(
                    Severity.ERROR,
                    "temperature",
                    "ALARM_A",
                    "First alarm",
                )
                second, _changed = events.report(
                    Severity.ERROR,
                    "temperature",
                    "ALARM_B",
                    "Second alarm",
                )
                await asyncio.gather(
                    *tuple(manager._event_response_tasks.values())
                )
                events.resolve("temperature", "ALARM_A")
                manager.reset_event_response(first.key)
                self.assertFalse(
                    await manager.set_target(
                        "field",
                        100.0,
                        10.0,
                        origin="manual",
                    )
                )
                events.resolve("temperature", "ALARM_B")
                manager.reset_event_response(second.key)
                self.assertTrue(
                    await manager.set_target(
                        "field",
                        100.0,
                        10.0,
                        origin="manual",
                    )
                )
            finally:
                await manager.disconnect_all()

        asyncio.run(scenario())

    def test_registration_without_a_controllable_field_fails(self) -> None:
        async def scenario() -> None:
            base = load_config(ROOT / "configs" / "default.toml")
            config = replace(
                base,
                instruments=(base.instrument("temperature"),),
            )
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            backend = manager.instruments["temperature"].backend
            backend.event_responses = lambda: (  # type: ignore[method-assign]
                EventResponseSpec(
                    code="TEMPERATURE_ALARM",
                    action="zero",
                ),
            )
            try:
                with self.assertRaises(InstrumentError) as captured:
                    await manager.connect_all()
                self.assertEqual(
                    captured.exception.code,
                    "INVALID_EVENT_RESPONSES",
                )
            finally:
                await manager.disconnect_all()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
