"""Shared VISA session for Measurement Modules; instrument commands stay in their drivers.

Session lifetime follows PyMeasure's VISAAdapter: close the instrument handle,
not the shared hardware ResourceManager.
https://github.com/pymeasure/pymeasure/blob/master/pymeasure/adapters/visa.py
"""

from importlib import import_module
from typing import Any


class VisaSession:
    def __init__(
        self,
        address: str,
        timeout_seconds: float,
        *,
        read_termination: str | None = "\n",
        write_termination: str = "\n",
        **attributes: Any,
    ) -> None:
        pyvisa = import_module("pyvisa")
        timeout = max(1, int(timeout_seconds * 1000))
        self.handle = pyvisa.ResourceManager().open_resource(address, open_timeout=timeout)
        try:
            self.handle.timeout = timeout
            self.handle.read_termination = read_termination
            self.handle.write_termination = write_termination
            for name, value in attributes.items():
                setattr(self.handle, name, value)
        except Exception:
            self.handle.close()
            raise

    def write(self, command: str) -> None:
        self.handle.write(command)

    def query(self, command: str, timeout_seconds: float | None = None) -> str:
        if timeout_seconds is None:
            return str(self.handle.query(command))
        original = self.handle.timeout
        self.handle.timeout = max(1, int(timeout_seconds * 1000))
        try:
            return str(self.handle.query(command))
        finally:
            self.handle.timeout = original

    def close(self) -> None:
        self.handle.close()
