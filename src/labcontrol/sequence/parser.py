"""SEQ 文本与树形 :class:`SequenceDocument` 之间的严格双向转换。

解析器保留未知命令原文以便用户修复，但把语法不完整、单位不一致、无穷数和结构未闭合记录
为明确问题。扫描使用栈而不是固定层数，因此支持任意嵌套。保存时统一输出 UTF-8/LF，并通过
同目录临时文件原子替换，避免中断写入破坏原 SEQ。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..formatting import field_decimals, fixed_number
from .model import (
    Command,
    CommandType,
    SequenceDocument,
    validate_command_parameters,
)


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
MAX_TEMPERATURE_LIST_POINTS = 100000


@dataclass(frozen=True, slots=True)
class SequenceIssue:
    """带原始行号和文本的解析问题；level 为 warning 或 error。"""

    line_number: int
    level: str
    message: str
    raw_line: str = ""


@dataclass(frozen=True, slots=True)
class ParseResult:
    """解析后的文档及全部问题；警告不会阻止运行。"""

    document: SequenceDocument
    issues: tuple[SequenceIssue, ...]

    @property
    def has_errors(self) -> bool:
        """是否至少包含一个阻止运行的语法或参数错误。"""

        return any(issue.level == "error" for issue in self.issues)


_SET_TEMPERATURE = re.compile(
    rf"^Set\s+Temperature\s+(?P<target>{NUMBER})\s*K\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*K/min\s+in\s+(?P<mode>Settle|Sweep)\s+mode$",
    re.IGNORECASE,
)
_SET_FIELD = re.compile(
    rf"^Set\s+Field\s+(?P<target>{NUMBER})\s*(?P<unit>T|Oe)\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*(?P<rate_unit>T|Oe)/min\s+in\s+"
    rf"(?P<mode>Settle|Sweep)\s+mode$",
    re.IGNORECASE,
)
_SCAN_TEMPERATURE = re.compile(
    rf"^Scan\s+Temperature\s+(?:from\s+)?(?P<start>{NUMBER})\s*K\s+(?:to|through)\s+"
    rf"(?P<stop>{NUMBER})\s*K\s+in\s+(?P<steps>\d+)\s+steps\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*K/min\s*,?\s*(?P<mode>Settle|Sweep)$",
    re.IGNORECASE,
)
_SCAN_TEMPERATURE_LIST = re.compile(
    rf"^Scan\s+Temperature\s+List\s+(?P<points>.+?)\s*K\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*K/min\s*,?\s*(?P<mode>Settle|Sweep)$",
    re.IGNORECASE,
)
_SCAN_FIELD = re.compile(
    rf"^Scan\s+Field\s+(?:from\s+)?(?P<start>{NUMBER})\s*(?P<unit>T|Oe)\s+(?:to|through)\s+"
    rf"(?P<stop>{NUMBER})\s*(?P<stop_unit>T|Oe)\s+in\s+(?P<steps>\d+)\s+steps\s+at\s+"
    rf"(?P<rate>{NUMBER})\s*(?P<rate_unit>T|Oe)/min\s*,?\s*(?P<mode>Settle|Sweep)"
    r"(?:\s*,\s*(?P<nearest_polarity>Nearest\s+(?:\+/-|±)\s+Polarity))?$",
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
_INSTRUMENT_SUFFIX = re.compile(
    r'^(?P<body>.+?)\s+using\s+instrument\s+'
    r'(?P<instrument>"(?:\\.|[^"\\])*"|[^\s]+)\s*$',
    re.IGNORECASE,
)
_INSTRUMENT_COMMAND_PREFIX = re.compile(
    r"^(?:Set|Scan)\s+(?:Temperature|Field)\b",
    re.IGNORECASE,
)
_MODULE_COMMAND_PREFIX = re.compile(
    r"^Module\s+(?P<kind>Command|Scan)\s+",
    re.IGNORECASE,
)
_MODULE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _reject_json_constant(value: str) -> object:
    """JSON 标准不含 NaN/Infinity；显式拒绝 Python 解码器的宽松扩展。"""

    raise ValueError(f"invalid JSON constant {value}")


_STRICT_JSON_DECODER = json.JSONDecoder(parse_constant=_reject_json_constant)


def _parse_module_command(
    text: str,
    line_number: int,
) -> tuple[Command, SequenceIssue | None] | None:
    """解析可在模块缺失时仍完整保存的通用 Module Command/Scan 信封。"""

    prefix = _MODULE_COMMAND_PREFIX.match(text)
    if prefix is None:
        if re.match(r"^Module\s+(?:Command|Scan)\b", text, re.IGNORECASE):
            command = Command(
                CommandType.UNKNOWN,
                {"text": text},
                raw_text=text,
                source_line=line_number,
            )
            return command, SequenceIssue(
                line_number,
                "error",
                "Invalid module command; expected quoted module ID, quoted command ID, and a JSON object",
                text,
            )
        return None
    remainder = text[prefix.end():]
    values: list[object] = []
    try:
        for _ in range(3):
            remainder = remainder.lstrip()
            value, end = _STRICT_JSON_DECODER.raw_decode(remainder)
            values.append(value)
            remainder = remainder[end:]
        if remainder.strip():
            raise ValueError("unexpected text after parameter object")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        command = Command(
            CommandType.UNKNOWN,
            {"text": text},
            raw_text=text,
            source_line=line_number,
        )
        return command, SequenceIssue(
            line_number,
            "error",
            f"Invalid module command syntax or JSON: {exc}",
            text,
        )
    module_id, command_id, parameters = values
    if (
        not isinstance(module_id, str)
        or not _MODULE_IDENTIFIER.fullmatch(module_id)
    ):
        issue = "Module ID must match [a-z][a-z0-9_]*"
    elif (
        not isinstance(command_id, str)
        or not _MODULE_IDENTIFIER.fullmatch(command_id)
    ):
        issue = "Module command ID must match [a-z][a-z0-9_]*"
    elif not isinstance(parameters, dict):
        issue = "Module command parameters must be a JSON object"
    else:
        issue = ""
    if issue:
        command = Command(
            CommandType.UNKNOWN,
            {"text": text},
            raw_text=text,
            source_line=line_number,
        )
        return command, SequenceIssue(line_number, "error", issue, text)
    command_type = (
        CommandType.MODULE_SCAN
        if prefix.group("kind").casefold() == "scan"
        else CommandType.MODULE_COMMAND
    )
    return Command(
        command_type,
        dict(parameters),
        raw_text=text,
        source_line=line_number,
        module_id=module_id,
        module_command_id=command_id,
    ), None


def _temperature_point_items(value: object) -> list[object]:
    """拆分温度点，并兼容旧式裸列表和新的方括号列表。"""

    if isinstance(value, (list, tuple)):
        raw_points = list(value)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("Enter at least one temperature point")
        has_opening_bracket = text.startswith("[")
        has_closing_bracket = text.endswith("]")
        if has_opening_bracket != has_closing_bracket:
            raise ValueError("Temperature list must use matching square brackets")
        if has_opening_bracket:
            text = text[1:-1].strip()
            if not text:
                raise ValueError("Enter at least one temperature point")
        raw_points = [item.strip() for item in text.split(",")]

    if len(raw_points) > MAX_TEMPERATURE_LIST_POINTS:
        raise ValueError(
            f"Temperature lists are limited to {MAX_TEMPERATURE_LIST_POINTS} points"
        )
    return raw_points


def parse_temperature_points(value: object) -> tuple[float, ...]:
    """解析温度列表，并保留输入顺序和重复点。"""

    raw_points = _temperature_point_items(value)
    points: list[float] = []
    for index, raw_point in enumerate(raw_points, start=1):
        if isinstance(raw_point, str) and not raw_point:
            raise ValueError(f"Temperature point {index} is empty")
        try:
            point = float(raw_point)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Temperature point {index} is not a number: {raw_point!s}") from exc
        if not math.isfinite(point):
            raise ValueError(f"Temperature point {index} must be finite")
        points.append(point)

    if not points:
        raise ValueError("Enter at least one temperature point")
    return tuple(points)


def format_temperature_points(value: object) -> str:
    """验证并格式化温度列表，同时保留用户输入的每项精度。

    这里只统一方括号和逗号后的空格，不把 ``299.9`` 扩写为 ``299.900``。数值列表
    没有可恢复的原始文本时使用 Python 的稳定字符串形式。
    """

    raw_points = _temperature_point_items(value)
    parse_temperature_points(raw_points)
    tokens = [
        raw_point.strip() if isinstance(raw_point, str) else str(raw_point)
        for raw_point in raw_points
    ]
    return f"[{', '.join(tokens)}]"


def _parse_base_command(text: str, line_number: int) -> tuple[Command, SequenceIssue | None]:
    """解析不含 ``using instrument`` 后缀的一条规范命令。"""

    module_result = _parse_module_command(text, line_number)
    if module_result is not None:
        return module_result

    lowered = text.lower()
    match = _SET_TEMPERATURE.match(text)
    if match:
        return Command(
            CommandType.SET_TEMPERATURE,
            {
                "instrument_id": "temperature",
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
                "instrument_id": "field",
                "target": float(match.group("target")),
                "unit": unit,
                "rate": float(match.group("rate")),
                "mode": match.group("mode").title(),
            },
            raw_text=text,
            source_line=line_number,
        ), None

    match = _SCAN_TEMPERATURE_LIST.match(text)
    if match:
        try:
            points = format_temperature_points(match.group("points"))
        except ValueError as exc:
            return Command(
                CommandType.UNKNOWN,
                {"text": text},
                raw_text=text,
                source_line=line_number,
            ), SequenceIssue(line_number, "error", f"Invalid temperature list: {exc}", text)
        return Command(
            CommandType.SCAN_TEMPERATURE,
            {
                "instrument_id": "temperature",
                "point_mode": "List",
                "points": points,
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
                "instrument_id": "temperature",
                "point_mode": "Linear",
                "start": float(match.group("start")),
                "stop": float(match.group("stop")),
                "steps": int(match.group("steps")),
                "rate": float(match.group("rate")),
                "mode": match.group("mode").title(),
            },
            raw_text=text,
            source_line=line_number,
        ), None

    if lowered.startswith("scan temperature list"):
        return Command(
            CommandType.UNKNOWN,
            {"text": text},
            raw_text=text,
            source_line=line_number,
        ), SequenceIssue(
            line_number,
            "error",
            "Invalid temperature list scan; expected comma-separated points followed by K at rate K/min, Settle or Sweep",
            text,
        )

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
                "instrument_id": "field",
                "start": float(match.group("start")),
                "stop": float(match.group("stop")),
                "unit": unit,
                "steps": int(match.group("steps")),
                "rate": float(match.group("rate")),
                "mode": match.group("mode").title(),
                "nearest_polarity": bool(match.group("nearest_polarity")),
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

    if lowered.startswith("set datafile "):
        payload = text[len("Set Datafile "):].strip()
        parts = payload.split(maxsplit=1)
        mode = parts[0].casefold() if parts else "open|create"
        if mode not in {"open", "open|create", "create"}:
            return Command(
                CommandType.UNKNOWN,
                {"text": text},
                raw_text=text,
                source_line=line_number,
            ), SequenceIssue(
                line_number,
                "error",
                "Set Datafile mode must be open, open|create, or create",
                text,
            )
        path = parts[1] if len(parts) > 1 else "experiment.dat"
        path_scope = "Run folder"
        if path.lower().startswith("external "):
            path_scope = "Custom folder"
            path = path[len("external "):].strip()
        if not path:
            return Command(
                CommandType.UNKNOWN,
                {"text": text},
                raw_text=text,
                source_line=line_number,
            ), SequenceIssue(
                line_number,
                "error",
                "Set Datafile requires a non-empty file path",
                text,
            )
        return Command(
            CommandType.SET_DATAFILE,
            {"mode": mode, "path_scope": path_scope, "path": path},
            raw_text=text,
            source_line=line_number,
        ), None

    if lowered == "measure":
        return Command(CommandType.MEASURE, {}, raw_text=text, source_line=line_number), None
    if lowered.startswith("measure "):
        return Command(
            CommandType.UNKNOWN,
            {"text": text},
            raw_text=text,
            source_line=line_number,
        ), SequenceIssue(
            line_number,
            "error",
            "Measure has no parameters; use exactly 'Measure'",
            text,
        )
    if lowered.startswith("initialize"):
        return Command(
            CommandType.UNKNOWN,
            {"text": text},
            raw_text=text,
            source_line=line_number,
        ), SequenceIssue(
            line_number,
            "error",
            "Initialize is no longer a SEQ command; enable the module in Modules before Run",
            text,
        )

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


def _parse_command(text: str, line_number: int) -> tuple[Command, SequenceIssue | None]:
    """先安全解析可选仪表后缀，再委托基础命令解析器。"""

    original = text
    instrument_id: str | None = None
    if _INSTRUMENT_COMMAND_PREFIX.match(text):
        suffix = _INSTRUMENT_SUFFIX.match(text)
        if suffix is not None:
            text = suffix.group("body").rstrip()
            raw_instrument = suffix.group("instrument")
            try:
                parsed_instrument = (
                    json.loads(raw_instrument)
                    if raw_instrument.startswith('"')
                    else raw_instrument
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                command = Command(
                    CommandType.UNKNOWN,
                    {"text": original},
                    raw_text=original,
                    source_line=line_number,
                )
                return command, SequenceIssue(
                    line_number,
                    "error",
                    f"Invalid instrument ID suffix: {exc}",
                    original,
                )
            instrument_id = str(parsed_instrument).strip()
            if not instrument_id:
                command = Command(
                    CommandType.UNKNOWN,
                    {"text": original},
                    raw_text=original,
                    source_line=line_number,
                )
                return command, SequenceIssue(
                    line_number,
                    "error",
                    "Instrument ID in a control command cannot be empty",
                    original,
                )

    command, issue = _parse_base_command(text, line_number)
    command.raw_text = original
    if (
        command.type is CommandType.UNKNOWN
        and _INSTRUMENT_COMMAND_PREFIX.match(original)
        and (issue is None or issue.level != "error")
    ):
        issue = SequenceIssue(
            line_number,
            "error",
            "Invalid temperature or field command syntax, mode, or instrument suffix",
            original,
        )
    if instrument_id is not None:
        if command.type not in {
            CommandType.SET_TEMPERATURE,
            CommandType.SET_FIELD,
            CommandType.SCAN_TEMPERATURE,
            CommandType.SCAN_FIELD,
        }:
            return command, SequenceIssue(
                line_number,
                "error",
                "The instrument suffix is valid only on temperature or field commands",
                original,
            )
        command.params["instrument_id"] = instrument_id
    return command, issue


def parse_sequence(text: str, name: str = "Untitled.seq", path: Path | None = None) -> ParseResult:
    """解析完整 SEQ，并使用栈构造任意深度扫描树。

    每行可带 ``T`` 或 ``F``；这里只记录节点自身状态，父扫描禁用后的有效状态由文档模型和
    引擎计算。遇到 ``End Scan`` 只关闭栈顶容器，未配对或在 ``End Sequence`` 前仍未闭合
    都产生 Error。
    """

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
        if issue is None:
            parameter_issues = validate_command_parameters(command)
            if parameter_issues:
                issue = SequenceIssue(
                    line_number,
                    "error",
                    "Invalid command parameter: " + "; ".join(parameter_issues),
                    raw_line,
                )
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
    """使用全框架一致的小数规则格式化 SEQ 数值。"""

    return fixed_number(value, decimals)


def _instrument_suffix(params: dict[str, object], default_id: str) -> str:
    """仅为非默认仪表输出 JSON 转义后的 ``using instrument`` 后缀。"""

    instrument_id = str(params.get("instrument_id", "")).strip()
    if not instrument_id or instrument_id == default_id:
        return ""
    return f" using instrument {json.dumps(instrument_id, ensure_ascii=False)}"


def format_command(command: Command, *, preserve_raw: bool = True) -> str:
    """把一个命令转为规范文本；未编辑的未知命令保持原文。"""

    if preserve_raw and command.raw_text is not None:
        return command.raw_text
    p = command.params
    if command.type is CommandType.SET_DATAFILE:
        scope = "external " if str(p.get("path_scope", "Run folder")) == "Custom folder" else ""
        return (
            f"Set Datafile {p.get('mode', 'open|create')} "
            f"{scope}{p.get('path', 'experiment.dat')}"
        )
    if command.type is CommandType.WAIT:
        return f"Wait For {_format_number(p.get('seconds', 0.0), 1)} secs"
    if command.type is CommandType.SET_TEMPERATURE:
        return (
            f"Set Temperature {_format_number(p.get('target', 300.0))} K at "
            f"{_format_number(p.get('rate', 5.0))} K/min in {p.get('mode', 'Settle')} mode"
            f"{_instrument_suffix(p, 'temperature')}"
        )
    if command.type is CommandType.SET_FIELD:
        unit = p.get("unit", "Oe")
        decimals = field_decimals(unit)
        return (
            f"Set Field {_format_number(p.get('target', 0.0), decimals)} {unit} at "
            f"{_format_number(p.get('rate', 5000.0), decimals)} {unit}/min in {p.get('mode', 'Settle')} mode"
            f"{_instrument_suffix(p, 'field')}"
        )
    if command.type is CommandType.SCAN_TEMPERATURE:
        if str(p.get("point_mode", "Linear")).casefold() == "list":
            try:
                points = format_temperature_points(p.get("points", ""))
            except ValueError:
                points = str(p.get("points", "")).strip()
            return (
                f"Scan Temperature List {points} K at "
                f"{_format_number(p.get('rate', 5.0))} K/min, {p.get('mode', 'Settle')}"
                f"{_instrument_suffix(p, 'temperature')}"
            )
        return (
            f"Scan Temperature {_format_number(p.get('start', 300.0))} K to "
            f"{_format_number(p.get('stop', 10.0))} K in {int(p.get('steps', 10))} steps at "
            f"{_format_number(p.get('rate', 5.0))} K/min, {p.get('mode', 'Settle')}"
            f"{_instrument_suffix(p, 'temperature')}"
        )
    if command.type is CommandType.SCAN_FIELD:
        unit = p.get("unit", "Oe")
        decimals = field_decimals(unit)
        polarity_suffix = (
            ", Nearest +/- Polarity"
            if p.get("nearest_polarity", False) is True
            else ""
        )
        return (
            f"Scan Field {_format_number(p.get('start', 0.0), decimals)} {unit} to "
            f"{_format_number(p.get('stop', 10000.0), decimals)} {unit} in {int(p.get('steps', 11))} steps at "
            f"{_format_number(p.get('rate', 5000.0), decimals)} {unit}/min, {p.get('mode', 'Settle')}"
            f"{polarity_suffix}"
            f"{_instrument_suffix(p, 'field')}"
        )
    if command.type is CommandType.SCAN_TIME:
        return (
            f"Scan Time {_format_number(p.get('duration_seconds', 60.0), 1)} secs in "
            f"{int(p.get('steps', 60))} steps"
        )
    if command.type is CommandType.MEASURE:
        return "Measure"
    if command.type is CommandType.REMARK:
        return f"Remark {p.get('text', '')}".rstrip()
    if command.type is CommandType.CALL_SEQUENCE:
        return f"Call Sequence {p.get('path', '')}".rstrip()
    if command.type is CommandType.INJECT_WARNING:
        return f"Inject Warning {p.get('code', 'SIM_WARNING')} {p.get('message', 'Simulated Warning')}"
    if command.type is CommandType.INJECT_ERROR:
        return f"Inject Error {p.get('code', 'SIM_ERROR')} {p.get('message', 'Simulated Error')}"
    if command.type in {
        CommandType.MODULE_COMMAND,
        CommandType.MODULE_SCAN,
    }:
        kind = "Scan" if command.type is CommandType.MODULE_SCAN else "Command"
        # 非 JSON 或非有限值必须让保存/运行显式失败，不能用空 object 替换后悄悄改变
        # 将要发送给真实仪表的参数。
        parameters = json.dumps(
            p,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return (
            f"Module {kind} "
            f"{json.dumps(command.module_id, ensure_ascii=False)} "
            f"{json.dumps(command.module_command_id, ensure_ascii=False)} "
            f"{parameters}"
        )
    return str(p.get("text", command.raw_text or "Unknown"))


def serialize_sequence(document: SequenceDocument) -> str:
    """递归序列化整棵命令树，并补齐 End Scan 与 End Sequence。"""

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
    """兼容读取 UTF-8、UTF-16 和常见中文旧文件编码。"""

    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_sequence(path: str | Path) -> ParseResult:
    """从磁盘读取并解析 SEQ，保存绝对来源路径供 Call Sequence 解析。"""

    source = Path(path).resolve()
    return parse_sequence(_read_text(source), name=source.name, path=source)


def save_sequence(document: SequenceDocument, path: str | Path) -> Path:
    """原子保存 SEQ，并在成功替换后更新文档的路径和名称。"""

    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(serialize_sequence(document), encoding="utf-8", newline="\n")
    temporary.replace(destination)
    document.path = destination
    document.name = destination.name
    return destination
