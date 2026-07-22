from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


class CommandType(str, Enum):
    INITIALIZE = "initialize"
    SET_DATAFILE = "set_datafile"
    WAIT = "wait"
    SET_TEMPERATURE = "set_temperature"
    SET_FIELD = "set_field"
    SCAN_TEMPERATURE = "scan_temperature"
    SCAN_FIELD = "scan_field"
    SCAN_TIME = "scan_time"
    MEASURE = "measure"
    REMARK = "remark"
    CALL_SEQUENCE = "call_sequence"
    INJECT_WARNING = "inject_warning"
    INJECT_ERROR = "inject_error"
    UNKNOWN = "unknown"

    @property
    def is_container(self) -> bool:
        return self in {
            CommandType.SCAN_TEMPERATURE,
            CommandType.SCAN_FIELD,
            CommandType.SCAN_TIME,
        }


@dataclass(slots=True)
class Command:
    type: CommandType
    params: dict[str, Any] = field(default_factory=dict)
    children: list["Command"] = field(default_factory=list)
    raw_text: str | None = None
    source_line: int | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    enabled: bool = True

    def update_params(self, values: dict[str, Any]) -> None:
        self.params = dict(values)
        self.raw_text = None

    def clone(self) -> "Command":
        duplicate = deepcopy(self)

        def refresh(command: Command) -> None:
            command.id = uuid4().hex
            for child in command.children:
                refresh(child)

        refresh(duplicate)
        return duplicate


@dataclass(frozen=True, slots=True)
class FlatRow:
    command_id: str | None
    depth: int
    is_end: bool
    is_sequence_end: bool = False
    effective_enabled: bool = True


@dataclass(slots=True)
class SequenceDocument:
    commands: list[Command] = field(default_factory=list)
    name: str = "Untitled.seq"
    path: Path | None = None

    def clone(self) -> "SequenceDocument":
        return deepcopy(self)

    def flat_rows(self) -> list[FlatRow]:
        rows: list[FlatRow] = []

        def visit(commands: list[Command], depth: int, parent_enabled: bool) -> None:
            for command in commands:
                effective_enabled = parent_enabled and command.enabled
                rows.append(FlatRow(command.id, depth, False, effective_enabled=effective_enabled))
                if command.type.is_container:
                    visit(command.children, depth + 1, effective_enabled)
                    rows.append(FlatRow(command.id, depth, True, effective_enabled=effective_enabled))

        visit(self.commands, 0, True)
        rows.append(FlatRow(None, 0, False, is_sequence_end=True))
        return rows

    def find(self, command_id: str) -> Command | None:
        def search(commands: list[Command]) -> Command | None:
            for command in commands:
                if command.id == command_id:
                    return command
                found = search(command.children)
                if found is not None:
                    return found
            return None

        return search(self.commands)

    def _locate(self, command_id: str) -> tuple[list[Command], int] | None:
        def search(commands: list[Command]) -> tuple[list[Command], int] | None:
            for index, command in enumerate(commands):
                if command.id == command_id:
                    return commands, index
                found = search(command.children)
                if found is not None:
                    return found
            return None

        return search(self.commands)

    def insert(self, command: Command, selected: FlatRow | None = None) -> None:
        if selected is None or selected.is_sequence_end:
            self.commands.append(command)
            return
        if selected.command_id is None:
            self.commands.append(command)
            return
        selected_command = self.find(selected.command_id)
        if selected.is_end or (selected_command is not None and selected_command.type.is_container):
            if selected_command is not None:
                selected_command.children.append(command)
                return
        location = self._locate(selected.command_id)
        if location is None:
            self.commands.append(command)
            return
        parent, index = location
        parent.insert(index + 1, command)

    def delete(self, command_id: str) -> bool:
        location = self._locate(command_id)
        if location is None:
            return False
        parent, index = location
        del parent[index]
        return True

    def move(self, command_id: str, offset: int) -> bool:
        location = self._locate(command_id)
        if location is None:
            return False
        parent, index = location
        new_index = index + offset
        if new_index < 0 or new_index >= len(parent):
            return False
        parent[index], parent[new_index] = parent[new_index], parent[index]
        return True

    def set_enabled(self, command_id: str, enabled: bool) -> bool:
        command = self.find(command_id)
        if command is None or command.enabled is enabled:
            return False
        command.enabled = enabled
        return True

    def count_commands(self, *, enabled_only: bool = False) -> int:
        def count(commands: list[Command], parent_enabled: bool) -> int:
            total = 0
            for command in commands:
                effective_enabled = parent_enabled and command.enabled
                if not enabled_only or effective_enabled:
                    total += 1
                if command.type.is_container and (not enabled_only or effective_enabled):
                    total += count(command.children, effective_enabled)
            return total

        return count(self.commands, True)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    label: str
    field_type: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command_type: CommandType
    label: str
    category: str
    fields: tuple[FieldSpec, ...]

    def create(self) -> Command:
        return Command(self.command_type, {item.name: item.default for item in self.fields})


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(CommandType.MEASURE, "Measure", "Measurement Commands", (
        FieldSpec("devices", "Measurement devices (comma-separated or all)", "text", "all"),
        FieldSpec("repeats", "Repeat count", "int", 1, 1, 100000),
        FieldSpec("interval_seconds", "Repeat interval (s)", "float", 0.0, 0.0, 86400.0),
    )),
    CommandSpec(CommandType.INITIALIZE, "Initialize", "System Commands", (
        FieldSpec("model", "Device / model", "text", "transport"),
        FieldSpec("config_path", "Device configuration path", "text", ""),
    )),
    CommandSpec(CommandType.SET_DATAFILE, "Set Datafile", "System Commands", (
        FieldSpec("mode", "Mode", "choice", "open|create", choices=("open|create", "create", "open")),
        FieldSpec("path", "Data file", "text", "experiment.dat"),
    )),
    CommandSpec(CommandType.CALL_SEQUENCE, "Call Sequence", "System Commands", (
        FieldSpec("path", "SEQ file", "text", "subsequence.seq"),
    )),
    CommandSpec(CommandType.REMARK, "Remark", "System Commands", (
        FieldSpec("text", "Remark", "text", "Remark"),
    )),
    CommandSpec(CommandType.SCAN_FIELD, "Scan Field", "System Commands", (
        FieldSpec("device_id", "Field device", "text", "field"),
        FieldSpec("start", "Start", "float", 0.0),
        FieldSpec("stop", "Stop", "float", 1.0),
        FieldSpec("unit", "Unit", "choice", "T", choices=("T", "Oe")),
        FieldSpec("steps", "Points", "int", 11, 1, 100000),
        FieldSpec("rate", "Rate per minute", "float", 0.5, 0.000001),
        FieldSpec("mode", "Mode", "choice", "Settle", choices=("Settle", "Sweep")),
    )),
    CommandSpec(CommandType.SCAN_TEMPERATURE, "Scan Temperature", "System Commands", (
        FieldSpec("device_id", "Temperature device", "text", "temperature"),
        FieldSpec("start", "Start (K)", "float", 300.0),
        FieldSpec("stop", "Stop (K)", "float", 10.0),
        FieldSpec("steps", "Points", "int", 10, 1, 100000),
        FieldSpec("rate", "Rate (K/min)", "float", 5.0, 0.000001),
        FieldSpec("mode", "Mode", "choice", "Settle", choices=("Settle", "Sweep")),
    )),
    CommandSpec(CommandType.SCAN_TIME, "Scan Time", "System Commands", (
        FieldSpec("duration_seconds", "Duration (s)", "float", 60.0, 0.0, 31536000.0),
        FieldSpec("steps", "Points", "int", 60, 1, 1000000),
    )),
    CommandSpec(CommandType.SET_FIELD, "Set Field", "System Commands", (
        FieldSpec("device_id", "Field device", "text", "field"),
        FieldSpec("target", "Target", "float", 0.0),
        FieldSpec("unit", "Unit", "choice", "T", choices=("T", "Oe")),
        FieldSpec("rate", "Rate per minute", "float", 0.5, 0.000001),
        FieldSpec("mode", "Mode", "choice", "Settle", choices=("Settle", "Sweep")),
    )),
    CommandSpec(CommandType.SET_TEMPERATURE, "Set Temperature", "System Commands", (
        FieldSpec("device_id", "Temperature device", "text", "temperature"),
        FieldSpec("target", "Target (K)", "float", 300.0),
        FieldSpec("rate", "Rate (K/min)", "float", 5.0, 0.000001),
        FieldSpec("mode", "Mode", "choice", "Settle", choices=("Settle", "Sweep")),
    )),
    CommandSpec(CommandType.WAIT, "Wait", "System Commands", (
        FieldSpec("seconds", "Wait time (s)", "float", 10.0, 0.0, 31536000.0),
    )),
    CommandSpec(CommandType.INJECT_WARNING, "Inject Warning", "Advanced Commands (Simulation)", (
        FieldSpec("code", "Code", "text", "SIM_WARNING"),
        FieldSpec("message", "Message", "text", "Simulated Warning"),
    )),
    CommandSpec(CommandType.INJECT_ERROR, "Inject Error", "Advanced Commands (Simulation)", (
        FieldSpec("code", "Code", "text", "SIM_ERROR"),
        FieldSpec("message", "Message", "text", "Simulated Error"),
    )),
)


SPECS_BY_TYPE = {spec.command_type: spec for spec in COMMAND_SPECS}
