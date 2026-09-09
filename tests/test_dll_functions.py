from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.dll_functions import (  # noqa: E402
    normalize_dll_functions, validate_dll_parameters, validate_dll_result,
)
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.instruments.base import InstrumentError, InstrumentWarning  # noqa: E402
from labcontrol.instruments.worker import (  # noqa: E402
    InstrumentWorkerClient, InstrumentWorkerSpec, IsolatedInstrumentClient,
)
from labcontrol.measurement.manifest import ModuleDescriptor  # noqa: E402
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.measurement.worker import ModuleWorkerClient, WorkerRequestError  # noqa: E402
from labcontrol.runtime import RuntimeService  # noqa: E402
from labcontrol.sequence.model import SequenceDocument  # noqa: E402
from tests.configuration_fixtures import load_simulated_config  # noqa: E402
from tests.fixtures.dll_functions.backend import FUNCTIONS  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "dll_functions"
PARAMETERS = {"mode": "Read", "range": "Auto", "level": 1e-12, "samples": 10}


class DLLFunctionContractTests(unittest.TestCase):
    def test_metadata_round_trip_preserves_enums_and_small_values(self):
        specs = normalize_dll_functions(FUNCTIONS)
        self.assertEqual(normalize_dll_functions([s.to_payload() for s in specs]), specs)
        self.assertEqual(specs[0].inputs[2].default, 1e-12)
        self.assertEqual(specs[0].inputs[1].choices, ("Auto", "Low", "High"))
        self.assertEqual(validate_dll_parameters(specs[0], PARAMETERS), ())

    def test_invalid_metadata_is_rejected_before_open(self):
        for change in ("duplicate", "unknown_type", "missing_default", "bad_enum"):
            with self.subTest(change=change):
                raw = deepcopy(FUNCTIONS)
                if change == "duplicate":
                    raw.append(deepcopy(raw[0]))
                elif change == "unknown_type":
                    raw[0]["outputs"][0]["type"] = "pointer"
                elif change == "missing_default":
                    del raw[0]["inputs"][0]["default"]
                else:
                    raw[0]["inputs"][0]["default"] = "Undefined"
                with self.assertRaises(TypeError):
                    normalize_dll_functions(raw)

    def test_input_boundary_rejects_wrong_types_unknowns_and_limits(self):
        spec = normalize_dll_functions(FUNCTIONS)[0]
        for overrides in (
            {"samples": "10"}, {"samples": 10.5}, {"samples": True},
            {"level": "1e-12"}, {"level": float("nan")}, {"level": 2.0},
            {"range": "auto"}, {"raw_command": "*RST"},
        ):
            with self.subTest(overrides=overrides):
                self.assertTrue(validate_dll_parameters(spec, PARAMETERS | overrides))
        self.assertTrue(validate_dll_parameters(spec, {}))

    def test_output_boundary_never_converts_bad_data_into_success(self):
        spec = normalize_dll_functions(FUNCTIONS)[0]
        valid = {"value": 1e-12, "calls": 1, "pid": 1}
        self.assertEqual(validate_dll_result(spec, valid), valid)
        for value in (None, {}, valid | {"extra": 1}, valid | {"value": "1"},
                      valid | {"calls": True}, valid | {"value": float("nan")}):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    validate_dll_result(spec, value)


class DLLFunctionWorkerTests(unittest.TestCase):
    def test_module_worker_preserves_session_and_propagates_failures(self):
        descriptor = ModuleDescriptor("dll_test", "DLL Test", "1.0.0", FIXTURE)
        client = ModuleWorkerClient(descriptor)
        handler = lambda message: {"state": "idle"}
        try:
            client.start(5.0)
            self.assertEqual(client.dll_functions[0].function_id, "diagnostic_read")
            client.request("open", timeout_seconds=2.0)
            for call in (1, 2):
                result = client.request("dll_function", {
                    "function_id": "diagnostic_read", "parameters": PARAMETERS,
                }, event_handler=handler, timeout_seconds=2.0)
                self.assertEqual(result["calls"], call)
                self.assertEqual(result["pid"], client._process.pid)
                self.assertNotEqual(result["pid"], os.getpid())
                self.assertEqual(result["value"], 1e-12)
            for mode, code, severity in (
                ("Warning", "DLL_TEST_WARNING", "warning"),
                ("Error", "DLL_TEST_ERROR", "error"),
                ("Invalid", "DLL_FUNCTION_RESULT_INVALID", "error"),
            ):
                with self.subTest(mode=mode):
                    with self.assertRaises(WorkerRequestError) as caught:
                        client.request("dll_function", {
                            "function_id": "diagnostic_read",
                            "parameters": PARAMETERS | {"mode": mode},
                        }, event_handler=handler, timeout_seconds=2.0)
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(caught.exception.severity, severity)
                    self.assertTrue(caught.exception.context)
            with self.assertRaises(WorkerRequestError) as invalid_input:
                client.request("dll_function", {
                    "function_id": "diagnostic_read",
                    "parameters": PARAMETERS | {"level": 2.0},
                }, timeout_seconds=2.0)
            self.assertEqual(invalid_input.exception.code, "DLL_FUNCTION_PARAMETERS_INVALID")
            client.request("module_close", timeout_seconds=2.0)
        finally:
            client.close(1.0)
        self.assertIsNone(client._process)

    def test_missing_module_handler_fails_startup_and_reaps_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = (FIXTURE / "backend.py").read_text(encoding="utf8")
            (root / "backend.py").write_text(
                source.replace("def execute_dll_function(", "def unused_function("),
                encoding="utf8",
            )
            client = ModuleWorkerClient(ModuleDescriptor("missing_handler", "Test", "1", root))
            try:
                with self.assertRaises(WorkerRequestError) as caught:
                    client.start(5.0)
                self.assertIn("execute_dll_function", str(caught.exception))
                self.assertIsNone(client._process)
            finally:
                client.close(1.0)

    def test_system_worker_dispatch_is_serial_and_checks_parameters(self):
        async def scenario():
            config = load_simulated_config().instrument("temperature")
            client = IsolatedInstrumentClient(
                InstrumentWorkerClient(InstrumentWorkerSpec(
                    instrument_config=config, simulation_speed=1.0,
                    instrument_id="dll_test", backend="backend:Driver",
                    instrument_directory=str(FIXTURE),
                )),
                startup_timeout_seconds=5.0, operation_timeout_seconds=2.0,
                shutdown_timeout_seconds=1.0,
            )
            try:
                specs = await client.describe_dll_functions()
                self.assertEqual(specs[0].function_id, "diagnostic_read")
                await client.open()
                results = await asyncio.gather(*(
                    client.execute_dll_function("diagnostic_read", PARAMETERS)
                    for _ in range(3)
                ))
                self.assertEqual(sorted(r["calls"] for r in results), [1, 2, 3])
                self.assertEqual({r["pid"] for r in results}, {client.pid})
                for mode, error_type, code in (
                    ("Warning", InstrumentWarning, "DLL_TEST_WARNING"),
                    ("Error", InstrumentError, "DLL_TEST_ERROR"),
                    ("Invalid", InstrumentError, "DLL_FUNCTION_RESULT_INVALID"),
                ):
                    with self.subTest(mode=mode), self.assertRaises(error_type) as caught:
                        await client.execute_dll_function(
                            "diagnostic_read", PARAMETERS | {"mode": mode}
                        )
                    self.assertEqual(caught.exception.code, code)
                    self.assertTrue(caught.exception.context)
                with self.assertRaises(InstrumentError) as caught:
                    await client.execute_dll_function("diagnostic_read", {})
                self.assertEqual(caught.exception.code, "DLL_FUNCTION_PARAMETERS_INVALID")
                await client.close()
            finally:
                await client.shutdown()
            self.assertIsNone(client.pid)
        asyncio.run(scenario())


class DLLFunctionServiceTests(unittest.TestCase):
    def test_module_registration_idle_guard_logging_and_timeout(self):
        async def scenario():
            config = load_simulated_config()
            events = EventManager(popup_errors=False, popup_warnings=False)
            notices = []
            messages = []
            events.subscribe(notices.append)
            instruments = InstrumentManager(config, events, isolate_processes=False)
            service = MeasurementModuleService(
                (ModuleDescriptor("dll_test", "DLL Test", "1.0.0", FIXTURE),),
                events, instruments, lambda kind, value: messages.append((kind, value)),
            )
            try:
                with self.assertRaises(InstrumentError) as disabled:
                    await service.execute_dll_function("dll_test", "diagnostic_read", PARAMETERS)
                self.assertEqual(disabled.exception.code, "MODULE_DISABLED")
                await service.enable("dll_test")
                record = service.records["dll_test"]
                self.assertTrue(messages[-1][1]["dll_functions"])
                result = await service.execute_dll_function("dll_test", "diagnostic_read", PARAMETERS)
                self.assertEqual(result["value"], 1e-12)
                self.assertEqual(notices[-1].event.code, "DLL_FUNCTION_COMPLETED")
                self.assertNotIn("module_result", [kind for kind, _ in messages])
                service._sequence_active = True
                with self.assertRaises(InstrumentError) as blocked:
                    await service.execute_dll_function("dll_test", "diagnostic_read", PARAMETERS)
                self.assertEqual(blocked.exception.code, "MODULE_OPERATION_DURING_SEQUENCE")
                service._sequence_active = False
                with self.assertRaises(InstrumentWarning) as warning:
                    await service.execute_dll_function(
                        "dll_test", "diagnostic_read", PARAMETERS | {"mode": "Warning"}
                    )
                self.assertEqual(warning.exception.context, "input_a")
                self.assertTrue(record.enabled)
                await service.disable("dll_test")
                self.assertEqual(messages[-1][1]["dll_functions"], [])

                await service.enable("dll_test")
                client = record.client
                service.config = replace(service.config, operation_timeout_seconds=0.1)
                with self.assertRaises(InstrumentError) as timeout:
                    await service.execute_dll_function(
                        "dll_test", "diagnostic_read", PARAMETERS | {"mode": "Slow"}
                    )
                self.assertEqual(timeout.exception.code, "MODULE_OPERATION_TIMEOUT")
                self.assertFalse(record.enabled)
                self.assertEqual(record.dll_functions, ())
                self.assertIsNone(client._process)
            finally:
                await service.shutdown()
        asyncio.run(scenario())

    def test_system_manager_honors_sequence_lease(self):
        async def scenario():
            manager = InstrumentManager(load_simulated_config(), EventManager(), isolate_processes=False)
            client = manager.instruments["temperature"]
            client.backend.dll_functions = FUNCTIONS
            client.backend.execute_dll_function = lambda name, params: {
                "value": params["level"], "calls": 1, "pid": os.getpid(),
            }
            try:
                await manager.connect_all()
                payload = manager.dll_function_payload()
                self.assertEqual(payload[0]["instrument_id"], "temperature")
                self.assertEqual((await manager.execute_dll_function(
                    "temperature", "diagnostic_read", PARAMETERS
                ))["value"], 1e-12)
                manager.acquire_sequence_control()
                with self.assertRaises(InstrumentWarning) as caught:
                    await manager.execute_dll_function("temperature", "diagnostic_read", PARAMETERS)
                self.assertEqual(caught.exception.code, "MANUAL_CONTROL_BLOCKED")
            finally:
                manager.release_sequence_control()
                await manager.disconnect_all()
        asyncio.run(scenario())

    def test_runtime_rejects_overlapping_seq_and_manual_calls_in_both_orders(self):
        async def scenario():
            runtime = RuntimeService(load_simulated_config(), module_descriptors=())
            started = asyncio.Event()
            release = asyncio.Event()

            async def wait_for_release(*args, **kwargs):
                started.set()
                await release.wait()
                return {"value": 1.0}

            runtime.modules = SimpleNamespace(execute_dll_function=wait_for_release)
            manual = asyncio.create_task(runtime._execute_dll_function(
                "module", "dll_test", "diagnostic_read", PARAMETERS
            ))
            await started.wait()
            with self.assertRaises(InstrumentWarning) as busy:
                await runtime._run_sequence(SequenceDocument(), {})
            self.assertEqual(busy.exception.code, "DLL_FUNCTION_BUSY")
            release.set()
            await manual
            self.assertEqual(runtime._active_dll_calls, 0)

            runtime.modules.execute_dll_function = AsyncMock(side_effect=InstrumentError("failed"))
            with self.assertRaises(InstrumentError):
                await runtime._execute_dll_function("module", "dll_test", "diagnostic_read", PARAMETERS)
            self.assertEqual(runtime._active_dll_calls, 0)

            release.clear()
            runtime._sequence_task = asyncio.create_task(release.wait())
            try:
                with self.assertRaises(InstrumentWarning) as running:
                    await runtime._execute_dll_function("module", "dll_test", "diagnostic_read", PARAMETERS)
                self.assertEqual(running.exception.code, "DLL_FUNCTION_DURING_SEQUENCE")
                self.assertEqual(runtime.modules.execute_dll_function.await_count, 1)
            finally:
                release.set()
                await runtime._sequence_task
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
