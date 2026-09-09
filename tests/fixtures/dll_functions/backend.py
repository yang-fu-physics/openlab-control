"""不访问硬件的自描述后端，用于真实 spawn/IPC 回归测试。"""

import os
import time

from labcontrol.instruments.base import SystemInstrument, InstrumentError, InstrumentWarning
from labcontrol.module_api import ModuleError, ModuleWarning


FUNCTIONS = [{
    "id": "diagnostic_read",
    "label": "Diagnostic Read",
    "description": "Read a test value through the existing worker.",
    "inputs": [
        {"name": "mode", "type": "choice", "default": "Read",
         "choices": ["Read", "Warning", "Error", "Invalid", "Slow"]},
        {"name": "range", "type": "choice", "default": "Auto",
         "choices": ["Auto", "Low", "High"]},
        {"name": "level", "type": "float", "default": 1e-12,
         "minimum": -1.0, "maximum": 1.0, "unit": "A"},
        {"name": "samples", "type": "int", "default": 10,
         "minimum": 1, "maximum": 1000},
    ],
    "outputs": [
        {"name": "value", "type": "float", "unit": "V"},
        {"name": "calls", "type": "int"},
        {"name": "pid", "type": "int"},
    ],
}]


def _reply(owner, function_id, parameters, warning, error):
    assert function_id == "diagnostic_read"
    assert owner.opened
    if parameters["mode"] == "Warning":
        raise warning("Test warning", "DLL_TEST_WARNING", "input_a")
    if parameters["mode"] == "Error":
        raise error("Test error", "DLL_TEST_ERROR", "input_b")
    if parameters["mode"] == "Invalid":
        return {"value": "not a number"}
    if parameters["mode"] == "Slow":
        time.sleep(2.0)
    owner.calls += 1
    return {"value": parameters["level"], "calls": owner.calls, "pid": os.getpid()}


class Module:
    columns = {"Value": "V"}
    slots = 1
    dll_functions = FUNCTIONS

    def open(self, api):
        self.opened = True
        self.calls = 0
        return {}

    def measure(self, slot, api):
        return {"Value": 1.0}

    def execute_dll_function(self, function_id, parameters, api):
        api.checkpoint()
        return _reply(self, function_id, parameters, ModuleWarning, ModuleError)

    def close(self, api):
        self.opened = False
        return {}


class Driver(SystemInstrument):
    dll_functions = FUNCTIONS

    def open(self):
        self.opened = True
        self.calls = 0

    def read_status(self):
        return {"value": 300.0, "target": 300.0, "rate": 10.0,
                "moving": False, "ready": True}

    def execute_dll_function(self, function_id, parameters):
        return _reply(self, function_id, parameters, InstrumentWarning, InstrumentError)

    def close(self):
        self.opened = False
