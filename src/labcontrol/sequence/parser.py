from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..formatting import field_decimals, fixed_number
from .model import Command, CommandType, SequenceDocument


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"


@dataclass(frozen=True, slots=True)
class SequenceIssue:
    line_number: int
    level: str
    message: str
    raw_line: str = ""


@dataclass(frozen=True, slots=True)
class ParseResult:
    document: SequenceDocument
    issues: tuple[SequenceIssue, ...]

    @property
    def has_errors(self) -> bool:
        return any(issue.level == "error" for issue in self.issues)


_SET_TEMPERATURE = re.compile(
    rf"^Set\s+Temperature\s+(?P<target>{NUMBER})\s*K\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*K/min\s+in\s+(?P<mode>.+?)\s+mode$",
    re.IGNORECASE,
)
_SET_FIELD = re.compile(
    rf"^Set\s+Field\s+(?P<target>{NUMBER})\s*(?P<unit>T|Oe)\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*(?P<rate_unit>T|Oe)/min\s+in\s+(?P<mode>.+?)\s+mode$",
    re.IGNORECASE,
)
_SCAN_TEMPERATURE = re.compile(
    rf"^Scan\s+Temperature\s+(?:from\s+)?(?P<start>{NUMBER})\s*K\s+(?:to|through)\s+"
    rf"(?P<stop>{NUMBER})\s*K\s+in\s+(?P<steps>\d+)\s+steps\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*K/min\s*,?\s*(?P<mode>Settle|Sweep)$",
    re.IGNORECASE,
)
_SCAN_FIELD = re.compile(
    rf"^Scan\s+Field\s+(?:from\s+)?(?P<start>{NUMBER})\s*(?P<unit>T|Oe)\s+(?:to|through)\s+"
    rf"(?P<stop>{NUMBER})\s*(?P<stop_unit>T|Oe)\s+in\s+(?P<steps>\d+)\s+steps\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*(?P<rate_unit>T|Oe)/min\s*,?\s*(?P<mode>Settle|Sweep)$",
    re.IGNORECASE,
)
_SCAN_TIME = re.compile(
    rf"^Scan\s+Time\s+(?P<duration>{NUMBER})\s*(?:secs?|seconds?)\s+in\s+"
    r"(?P<steps>\d+)\s+steps$",
    re.IGNORECASE,
)
_WAIT = re.compile(
    rf"^Wait(?:\s+For)?\s+(?P<seconds>{NUMBER})\s*(?:secs?|seconds?)$",
    re.IGNORECASE,
)


def _parse_command(text: str, line_number: int) -> tuple[Command, SequenceIssue | None]:
    lowered = text.lower()
    match = _SET_TEMPERATURE.match(text)
    if match:
        return Command(
            CommandType.SET_TEMPERATURE,
            {
                "device_id": "temperature",
                "target": float(match.group("target")),
                "rate": float(match.group("rate")),
                "mode": match.group("mode").title(),
            },
            raw_text=text,
            source_line=line_number,
        ), None

    match = _SET_FIELD.match(text)
    if match:
        unit = match.group("unit")
        if match.group("rate_unit").lower() != unit.lower():
            return Command(CommandType.UNKNOWN, {"text": text}, raw_text=text, source_line=line_number), SequenceIssue(
                line_number, "error", "Field target and rate units must match", text
            )
        return Command(
            CommandType.SET_FIELD,
            {
                "device_id": "field",
                "target": float(match.group("target")),
                "unit": unit,
                "rate": float(match.group("rate")),
                "mode": match.group("mode").title(),
            },
            raw_text=text,
            source_line=line_number,
        ), None

    match = _SCAN_TEMPERATURE.match(text)
    if match:
        return Command(
            CommandType.SCAN_TEMPERATURE,
            {
                "device_id": "temperature",
                "start": float(match.group("start")),
                "stop": float(match.group("stop")),
                "steps": int(match.group("steps")),
                "rate": float(match.group("rate")),
                "mode": match.group("mode").title(),
            },
            raw_text=text,
            source_line=line_number,
        ), None

    match = _SCAN_FIELD.match(text)
    if match:
        unit = match.group("unit")
        if any(match.group(name).lower() != unit.lower() for name in ("stop_unit", "rate_unit")):
            return Command(CommandType.UNKNOWN, {"text": text}, raw_text=text, source_line=line_number), SequenceIssue(
                line_number, "error", "Field scan start, stop, and rate units must match", text
            )
        return Command(
            CommandType.SCAN_FIELD,
            {
                "device_id": "field",
                "start": float(match.group("start")),
                "stop": float(match.group("stop")),
                "unit": unit,
                "steps": int(match.group("steps")),
                "rate": float(match.group("rate")),
                "mode": match.group("mode").title(),
            },
            raw_text=text,
            source_line=line_number,
        ), None

    match = _SCAN_TIME.match(text)
    if match:
        return Command(
            CommandType.SCAN_TIME,
            {"duration_seconds": float(match.group("duration")), "steps": int(match.group("steps"))},
            raw_text=text,
            source_line=line_number,
        ), None

    match = _WAIT.match(text)
    if match:
        return Command(
            CommandType.WAIT,
            {"seconds": float(match.group("seconds"))},
            raw_text=text,
            source_line=line_number,
        ), None

    if lowered.startswith("initialize "):
        payload = text[len("Initialize "):].strip()
        if " model " in payload:
            model, config_path = payload.split(" model ", 1)
        else:
            model, config_path = payload, ""
        return Command(
            CommandType.INITIALIZE,
            {"model": model.strip(), "config_path": config_path.strip()},
            raw_text=text,
            source_line=line_number,
        ), None

    if lowered.startswith("set datafile "):
        payload = text[len("Set Datafile "):].strip()
        parts = payload.split(maxsplit=1)
        mode = parts[0] if parts else "open|create"
        path = parts[1] if len(parts) > 1 else "experiment.dat"
        return Command(
            CommandType.SET_DATAFILE,
            {"mode": mode, "path": path},
            raw_text=text,
            source_line=line_number,
        ), None

    if lowered == "measure" or lowered.startswith("measure "):
        params: dict[str, object] = {"devices": "all", "repeats": 1, "interval_seconds": 0.0}
        for token in text.split()[1:]:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.lower()
            if key == "devices":
                params[key] = value
            elif key == "repeats":
                params[key] = int(value)
            elif key in ("interval", "interval_seconds"):
                params["interval_seconds"] = float(value.rstrip("sS"))
        return Command(CommandType.MEASURE, params, raw_text=text, source_line=line_number), None

    if lowered.startswith("remark"):
        return Command(
            CommandType.REMARK,
            {"text": text[len("Remark"):].strip()},
            raw_text=text,
            source_line=line_number,
        ), None

    if lowered.startswith("call sequence "):
        return Command(
            CommandType.CALL_SEQUENCE,
            {"path": text[len("Call Sequence "):].strip()},
            raw_text=text,
            source_line=line_number,
        ), None

    if lowered.startswith("inject warning"):
        payload = text[len("Inject Warning"):].strip()
        code, _, message = payload.partition(" ")
        return Command(
            CommandType.INJECT_WARNING,
            {"code": code or "SIM_WARNING", "message": message or "Simulated Warning"},
            raw_text=text,
            source_line=line_number,
        ), None

    if lowered.startswith("inject error"):
        payload = text[len("Inject Error"):].strip()
        code, _, message = payload.partition(" ")
        return Command(
            CommandType.INJECT_ERROR,
            {"code": code or "SIM_ERROR", "message": message or "Simulated Error"},
            raw_text=text,
            source_line=line_number,
        ), None

    return Command(
        CommandType.UNKNOWN,
        {"text": text},
        raw_text=text,
        source_line=line_number,
    ), SequenceIssue(line_number, "warning", "Unknown command will be preserved and skipped at runtime", text)


def parse_sequence(text: str, name: str = "Untitled.seq", path: Path | None = None) -> ParseResult:
    document = SequenceDocument(name=name, path=path)
    stack: list[list[Command]] = [document.commands]
    containers: list[Command] = []
    issues: list[SequenceIssue] = []
    saw_end_sequence = False

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        prefix_match = re.match(
            r"^\s*(?P<flag>[TF])(?:\s|$)(?P<payload>.*)$",
            raw_line,
            re.IGNORECASE,
        )
        payload = prefix_match.group("payload") if prefix_match else raw_line
        enabled = prefix_match is None or prefix_match.group("flag").upper() == "T"
        command_text = payload.strip()
        if not command_text:
            continue
        lowered = command_text.lower()
        if lowered == "end sequence":
            saw_end_sequence = True
            if len(stack) > 1:
                issues.append(SequenceIssue(
                    line_number, "error", "A Scan remains open before End Sequence", raw_line
                ))
            break
        if lowered == "end scan":
            if len(stack) == 1:
                issues.append(SequenceIssue(line_number, "error", "Unexpected End Scan", raw_line))
            else:
                stack.pop()
                containers.pop()
            continue

        command, issue = _parse_command(command_text, line_number)
        command.enabled = enabled
        stack[-1].append(command)
        if issue is not None:
            issues.append(issue)
        if command.type.is_container:
            containers.append(command)
            stack.append(command.children)

    if len(stack) > 1:
        for command in containers:
            issues.append(SequenceIssue(
                command.source_line or 0,
                "error",
                f"{command.type.value} is missing End Scan",
                command.raw_text or "",
            ))
    if not saw_end_sequence:
        issues.append(SequenceIssue(0, "warning", "End Sequence is missing; it will be added when saving"))
    return ParseResult(document, tuple(issues))


def _format_number(value: object, decimals: int = 3) -> str:
    return fixed_number(value, decimals)


def format_command(command: Command) -> str:
    if command.raw_text is not None:
        return command.raw_text
    p = command.params
    if command.type is CommandType.INITIALIZE:
        suffix = f" model {p.get('config_path', '')}" if p.get("config_path") else ""
        return f"Initialize {p.get('model', 'device')}{suffix}"
    if command.type is CommandType.SET_DATAFILE:
        return f"Set Datafile {p.get('mode', 'open|create')} {p.get('path', 'experiment.dat')}"
    if command.type is CommandType.WAIT:
        return f"Wait For {_format_number(p.get('seconds', 0.0), 1)} secs"
    if command.type is CommandType.SET_TEMPERATURE:
        return (
            f"Set Temperature {_format_number(p.get('target', 300.0))} K at "
            f"{_format_number(p.get('rate', 5.0))} K/min in {p.get('mode', 'Settle')} mode"
        )
    if command.type is CommandType.SET_FIELD:
        unit = p.get("unit", "Oe")
        decimals = field_decimals(unit)
        return (
            f"Set Field {_format_number(p.get('target', 0.0), decimals)} {unit} at "
            f"{_format_number(p.get('rate', 5000.0), decimals)} {unit}/min in {p.get('mode', 'Settle')} mode"
        )
    if command.type is CommandType.SCAN_TEMPERATURE:
        return (
            f"Scan Temperature {_format_number(p.get('start', 300.0))} K to "
            f"{_format_number(p.get('stop', 10.0))} K in {int(p.get('steps', 10))} steps at "
            f"{_format_number(p.get('rate', 5.0))} K/min, {p.get('mode', 'Settle')}"
        )
    if command.type is CommandType.SCAN_FIELD:
        unit = p.get("unit", "Oe")
        decimals = field_decimals(unit)
        return (
            f"Scan Field {_format_number(p.get('start', 0.0), decimals)} {unit} to "
            f"{_format_number(p.get('stop', 10000.0), decimals)} {unit} in {int(p.get('steps', 11))} steps at "
            f"{_format_number(p.get('rate', 5000.0), decimals)} {unit}/min, {p.get('mode', 'Settle')}"
        )
    if command.type is CommandType.SCAN_TIME:
        return (
            f"Scan Time {_format_number(p.get('duration_seconds', 60.0), 1)} secs in "
            f"{int(p.get('steps', 60))} steps"
        )
    if command.type is CommandType.MEASURE:
        extras: list[str] = []
        if str(p.get("devices", "all")) != "all":
            extras.append(f"devices={p['devices']}")
        if int(p.get("repeats", 1)) != 1:
            extras.append(f"repeats={int(p['repeats'])}")
        if float(p.get("interval_seconds", 0.0)) > 0:
            extras.append(f"interval={float(p['interval_seconds']):g}s")
        return "Measure" + (" " + " ".join(extras) if extras else "")
    if command.type is CommandType.REMARK:
        return f"Remark {p.get('text', '')}".rstrip()
    if command.type is CommandType.CALL_SEQUENCE:
        return f"Call Sequence {p.get('path', '')}".rstrip()
    if command.type is CommandType.INJECT_WARNING:
        return f"Inject Warning {p.get('code', 'SIM_WARNING')} {p.get('message', 'Simulated Warning')}"
    if command.type is CommandType.INJECT_ERROR:
        return f"Inject Error {p.get('code', 'SIM_ERROR')} {p.get('message', 'Simulated Error')}"
    return str(p.get("text", command.raw_text or "Unknown"))


def serialize_sequence(document: SequenceDocument) -> str:
    lines: list[str] = []

    def visit(commands: list[Command], depth: int) -> None:
        indent = "    " * depth
        for command in commands:
            flag = "T" if command.enabled else "F"
            lines.append(f"{flag} {indent}{format_command(command)}")
            if command.type.is_container:
                visit(command.children, depth + 1)
                lines.append(f"T {indent}End Scan")

    visit(document.commands, 0)
    lines.append("T End Sequence")
    return "\n".join(lines) + "\n"


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_sequence(path: str | Path) -> ParseResult:
    source = Path(path).resolve()
    return parse_sequence(_read_text(source), name=source.name, path=source)


def save_sequence(document: SequenceDocument, path: str | Path) -> Path:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(serialize_sequence(document), encoding="utf-8", newline="\n")
    temporary.replace(destination)
    document.path = destination
    document.name = destination.name
    return destination
