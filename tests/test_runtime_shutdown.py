from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.models import RunState  # noqa: E402
from labcontrol.runtime import RuntimeService  # noqa: E402
from labcontrol.sequence.model import (  # noqa: E402
    Command,
    CommandType,
    SequenceDocument,
)
from tests.configuration_fixtures import (  # noqa: E402
    load_simulated_config,
    write_simulated_configuration,
)


class RuntimeShutdownTests(unittest.TestCase):
    def test_control_states_use_faster_polling_without_changing_idle_rate(
        self,
    ) -> None:
        runtime = RuntimeService(
            load_simulated_config(),
            module_descriptors=(),
        )
        self.assertEqual(runtime._instrument_poll_interval(), 1.0)

        for state in (
            RunState.RUNNING,
            RunState.PAUSED,
            RunState.STOPPING,
        ):
            with self.subTest(state=state):
                runtime.engine = SimpleNamespace(state=state)
                self.assertEqual(runtime._instrument_poll_interval(), 0.2)

        runtime.engine = SimpleNamespace(state=RunState.COMPLETED)
        self.assertEqual(runtime._instrument_poll_interval(), 1.0)

    def test_shutdown_stops_active_sequence_thread_and_instrument_workers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = write_simulated_configuration(root)
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                .replace(
                    "poll_interval_seconds = 1.0",
                    "poll_interval_seconds = 0.05",
                )
                .replace(
                    "control_poll_interval_seconds = 0.20",
                    "control_poll_interval_seconds = 0.05",
                )
                .replace(
                    "instrument_status_interval_seconds = 1.0",
                    "instrument_status_interval_seconds = 0.05",
                ),
                encoding="utf-8",
            )
            runtime = RuntimeService(
                load_config(config_path),
                module_descriptors=(),
            )
            runtime.start(timeout=5.0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if any(
                    message.kind == "snapshots"
                    for message in runtime.drain_messages()
                ):
                    break
                time.sleep(0.02)
            else:
                self.fail("Runtime did not publish initial snapshots")

            runtime.run_sequence(
                SequenceDocument(
                    [
                        Command(
                            CommandType.WAIT,
                            {"seconds": 10.0},
                        )
                    ],
                    "shutdown.seq",
                )
            )
            time.sleep(0.35)
            instruments = runtime.instruments
            self.assertIsNotNone(instruments)
            clients = tuple(instruments.instruments.values())
            self.assertIsNotNone(runtime.logger)
            self.assertIsNotNone(runtime.logger.paths)
            run_paths = runtime.logger.paths
            runtime.shutdown(timeout=6.0)

            self.assertIsNone(runtime._thread)
            self.assertIsNone(runtime._loop)
            self.assertTrue(
                all(
                    getattr(client, "pid", None) is None
                    for client in clients
                )
            )
            instrument_status = run_paths.instrument_status_file.read_text(
                encoding="utf-8"
            )
            status_rows = (
                instrument_status.split("[Data]\n", 1)[1]
                .strip()
                .splitlines()
            )
            self.assertGreaterEqual(len(status_rows), 3)

    def test_shutdown_shares_total_budget_and_surfaces_cleanup_errors(
        self,
    ) -> None:
        import asyncio
        from labcontrol.events import EventManager

        events = EventManager()
        trace: list[str] = []

        class _SlowModules:
            async def shutdown(self, *, timeout_seconds: float | None = None) -> None:
                trace.append("module.start")
                await asyncio.sleep(3.0)
                trace.append("module.done")

        class _FailingInstruments:
            async def disconnect_all(self) -> None:
                trace.append("instrument.start")
                raise RuntimeError("instrument disconnect failed")

        class _Logger:
            def close(self) -> None:
                trace.append("logger.close")

        async def scenario() -> None:
            runtime = RuntimeService(load_simulated_config(), module_descriptors=())
            runtime._loop = SimpleNamespace(
                call_soon=lambda fn, *args: trace.append("loop.stop"),
                stop=lambda: None,
            )  # type: ignore[assignment]
            runtime.events = events
            runtime.modules = _SlowModules()  # type: ignore[assignment]
            runtime.instruments = _FailingInstruments()  # type: ignore[assignment]
            runtime.logger = _Logger()  # type: ignore[assignment]
            runtime.engine = SimpleNamespace(
                mark_shutting_down=lambda **_: None,
                request_stop=lambda *_: None,
            )  # type: ignore[assignment]
            runtime._sequence_task = asyncio.create_task(asyncio.sleep(3.0))

            started = time.monotonic()
            with self.assertRaisesRegex(
                RuntimeError,
                "modules.shutdown.*instruments.disconnect_all",
            ):
                await runtime._shutdown_async(deadline=started + 1.2)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0)
            self.assertIn("module.start", trace)
            self.assertIn("instrument.start", trace)
            self.assertIn("logger.close", trace)
            self.assertIn("loop.stop", trace)

        asyncio.run(scenario())

    def test_shutdown_bounds_blocked_instrument_close_and_completes_worker_shutdown_and_logger(
        self,
    ) -> None:
        import asyncio
        from labcontrol.events import EventManager
        from labcontrol.instrument_manager import InstrumentManager

        events = EventManager()
        trace: list[str] = []

        class _SlowModules:
            async def shutdown(self, *, timeout_seconds: float | None = None) -> None:
                trace.append("module.start")
                await asyncio.sleep(0.15)
                trace.append("module.done")

        class _BlockedCloseInstrument:
            enforces_timeouts = True

            async def close(self) -> None:
                trace.append("instrument.close.start")
                await asyncio.sleep(10.0)

            async def shutdown(self, timeout_seconds: float | None = None) -> None:
                del timeout_seconds
                trace.append("instrument.worker.shutdown.start")
                await asyncio.sleep(0.15)
                trace.append("instrument.worker.shutdown.done")

            async def force_stop(self, timeout_seconds: float = 0.25) -> None:
                del timeout_seconds
                trace.append("instrument.force_stop")

        class _Logger:
            def close(self) -> None:
                trace.append("logger.close")

        async def scenario() -> None:
            config = load_simulated_config()
            manager = InstrumentManager(config, events, isolate_processes=False)
            first_id = next(iter(manager.instruments))
            manager.instruments = {first_id: _BlockedCloseInstrument()}  # type: ignore[assignment]
            runtime = RuntimeService(config, module_descriptors=())
            runtime._loop = SimpleNamespace(
                call_soon=lambda fn, *args: trace.append("loop.stop"),
                stop=lambda: None,
            )  # type: ignore[assignment]
            runtime.events = events
            runtime.modules = _SlowModules()  # type: ignore[assignment]
            runtime.instruments = manager
            runtime.logger = _Logger()  # type: ignore[assignment]
            runtime._poll_task = asyncio.create_task(asyncio.sleep(1000.0))

            started = time.monotonic()
            await runtime._shutdown_async(deadline=started + 1.5)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(runtime._poll_task.cancelled())
            self.assertEqual(
                trace,
                [
                    "module.start",
                    "module.done",
                    "instrument.close.start",
                    "instrument.worker.shutdown.start",
                    "instrument.worker.shutdown.done",
                    "logger.close",
                    "loop.stop",
                ],
            )

        asyncio.run(scenario())

    def test_shutdown_with_active_poll_task_completes_all_cleanup_steps(
        self,
    ) -> None:
        import asyncio
        from labcontrol.events import EventManager

        trace: list[str] = []

        class _Modules:
            async def shutdown(self, *, timeout_seconds: float | None = None) -> None:
                del timeout_seconds
                trace.append("module.shutdown")

        class _Instruments:
            async def disconnect_all(self, *, timeout_seconds: float | None = None) -> None:
                del timeout_seconds
                trace.append("instrument.disconnect_all")

        class _Logger:
            def close(self) -> None:
                trace.append("logger.close")

        async def scenario() -> None:
            runtime = RuntimeService(load_simulated_config(), module_descriptors=())
            runtime._loop = SimpleNamespace(
                call_soon=lambda fn, *args: trace.append("loop.stop"),
                stop=lambda: None,
            )  # type: ignore[assignment]
            runtime.events = EventManager()
            runtime.modules = _Modules()  # type: ignore[assignment]
            runtime.instruments = _Instruments()  # type: ignore[assignment]
            runtime.logger = _Logger()  # type: ignore[assignment]
            runtime._poll_task = asyncio.create_task(asyncio.sleep(1000.0))

            await runtime._shutdown_async(deadline=time.monotonic() + 2.0)
            self.assertTrue(runtime._poll_task.cancelled())
            self.assertEqual(
                trace,
                [
                    "module.shutdown",
                    "instrument.disconnect_all",
                    "logger.close",
                    "loop.stop",
                ],
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
