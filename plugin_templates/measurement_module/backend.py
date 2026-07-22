from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from labcontrol.measurement.api import ModuleBackend, ModuleOperationContext


class MyMeasurementBackend(ModuleBackend):
    """Replace placeholders with real drivers that use bounded I/O timeouts."""

    def initialize(
        self, settings: Mapping[str, Any], context: ModuleOperationContext
    ) -> Mapping[str, Any]:
        # Open and identify every instrument owned by this module. Loading
        # settings here must not send those settings to the instruments.
        self.settings = dict(settings)
        return {"Connection": "Connected", "Applied Settings": "Not applied"}

    def apply_settings(
        self, settings: Mapping[str, Any], context: ModuleOperationContext
    ) -> Mapping[str, Any]:
        self.settings = dict(settings)
        # Send settings to the real instruments here.
        return {"Applied Settings": "Applied"}

    def begin_sequence(self, context: ModuleOperationContext) -> None:
        # Enter the output/measurement state required by a SEQ run.
        return None

    def measure(self, context: ModuleOperationContext) -> None:
        # context.system is a read-only controller and monitor snapshot.
        value = 0.0
        context.emit_row({"Value": value, "Status": "OK", "Warning": ""})

    def end_sequence(self, reason: str, context: ModuleOperationContext) -> None:
        # Leave output states for completed, stopped, and error runs.
        return None

    def abort(self, context: ModuleOperationContext) -> None:
        # Used only by Disable and application exit.
        return None
