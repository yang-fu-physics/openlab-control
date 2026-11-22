"""Measurement Module 可选 SEQ 指令的声明和验证。

模块用普通 ``dict`` 声明指令，因而无需继承核心类。声明先在隔离 worker 中验证，再以
受限 JSON 元数据发送给主进程；界面和运行时继续各自验证一次，不能把参数窗口当作真实
仪表的安全边界。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .sequence.model import Command, CommandType, FieldSpec
from .formatting import require_precision


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_FIELD_TYPES = frozenset({"text", "int", "float", "choice", "bool", "list"})
_SCAN_MODES = ("Linear", "Log", "List")
_SCAN_MODE_BY_KEY = {mode.casefold(): mode for mode in _SCAN_MODES}
_RESERVED_SCAN_FIELDS = frozenset({"point_mode", "start", "stop", "steps"})
_MAX_COMMANDS = 128
_MAX_FIELDS = 64
MAX_MODULE_SCAN_POINTS = 100_000
_SI_DECIMAL_SCALE = {
    "": Decimal("1"),
    "p": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "µ": Decimal("1e-6"),
    "μ": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
}
_SI_FORMAT_PREFIXES = (
    (Decimal("1e6"), Decimal("1e-6"), "M"),
    (Decimal("1e3"), Decimal("1e-3"), "k"),
    (Decimal("1"), Decimal("1"), ""),
    (Decimal("1e-3"), Decimal("1e3"), "m"),
    (Decimal("1e-6"), Decimal("1e6"), "u"),
    (Decimal("1e-9"), Decimal("1e9"), "n"),
    (Decimal("1e-12"), Decimal("1e12"), "p"),
)
_NUMBER_PREFIX_PATTERN = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([pnuµμmkM]?)"


def parse_si_quantity(value: object, unit: str = "", label: str = "Value") -> Decimal:
    """解析带可选 SI 前缀与单位后缀的有限标量。"""

    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a finite number")
    text = str(value).strip()
    unit_text = str(unit or "").strip()
    if unit_text:
        unit_pattern = re.escape(unit_text)
        if len(unit_text) > 1:
            unit_pattern = f"(?i:{unit_pattern})"
        pattern = rf"{_NUMBER_PREFIX_PATTERN}\s*(?:{unit_pattern})?"
    else:
        pattern = _NUMBER_PREFIX_PATTERN
    match = re.fullmatch(pattern, text)
    if match is None:
        hint = f", for example 100u{unit_text}" if unit_text else ""
        raise ValueError(f"{label}: invalid quantity {value!r}{hint}")
    try:
        result = Decimal(match[1]) * _SI_DECIMAL_SCALE[match[2]]
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError(f"{label} must be a finite number")
    return Decimal("0") if result == 0 else result


def format_si_quantity(value: Decimal | float | int | str, *, sig_figs: int = 12) -> str:
    """把标量格式化为简洁、无浮点尾数噪声的 SI 文本。"""

    dec = value if isinstance(value, Decimal) else parse_si_quantity(value)
    if dec == 0:
        return "0"
    sign = "-" if dec < 0 else ""
    with localcontext() as ctx:
        ctx.prec = sig_figs
        mag = +abs(dec)
    if mag == 0:
        return "0"
    if mag >= Decimal("1e9") or mag < Decimal("1e-15"):
        return f"{sign}{mag:.{sig_figs}g}".lower()
    for threshold, factor, prefix in _SI_FORMAT_PREFIXES:
        if mag >= threshold:
            scaled = mag * factor
            break
    else:
        scaled = mag * Decimal("1e12")
        prefix = "p"
    text = format(scaled, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{sign}{text}{prefix}"


def format_si_with_unit(value: Decimal | float | int, unit: str = "", *, sig_figs: int = 6) -> str:
    text = format_si_quantity(value, sig_figs=sig_figs)
    if not unit:
        return text
    if text == "0" or "e" in text:
        return f"{text} {unit}"
    return f"{text}{unit}"


def linear_step_from_points(
    start: Decimal | float,
    stop: Decimal | float,
    steps: int,
) -> Decimal | None:
    if steps <= 1:
        return None
    a = start if isinstance(start, Decimal) else Decimal(str(start))
    b = stop if isinstance(stop, Decimal) else Decimal(str(stop))
    return (b - a) / Decimal(steps - 1)


def linear_points_from_step(
    start: Decimal | float,
    stop: Decimal | float,
    step: Decimal | float,
    *,
    maximum: int = MAX_MODULE_SCAN_POINTS,
) -> tuple[int, Decimal]:
    """根据用户输入的步长取整到整数点数，并返回同步修正后的实际步长。"""

    a = start if isinstance(start, Decimal) else Decimal(str(start))
    b = stop if isinstance(stop, Decimal) else Decimal(str(stop))
    s = step if isinstance(step, Decimal) else Decimal(str(step))
    if not s.is_finite() or s == 0:
        raise ValueError("Step must be a non-zero finite number")
    span = b - a
    if span == 0:
        return 1, Decimal("0")
    intervals = int((abs(span) / abs(s)).to_integral_value(rounding=ROUND_HALF_UP))
    steps = max(2, min(maximum, intervals + 1))
    return steps, span / Decimal(steps - 1)


def log_points_per_decade(
    start: Decimal | float,
    stop: Decimal | float,
    steps: int,
) -> float | None:
    if steps <= 1:
        return None
    a, b = float(start), float(stop)
    if a == 0.0 or b == 0.0 or (a > 0.0) != (b > 0.0) or a == b:
        return None
    decades = abs(math.log10(abs(b) / abs(a)))
    if decades == 0.0:
        return None
    return (steps - 1) / decades


def log_points_from_per_decade(
    start: Decimal | float,
    stop: Decimal | float,
    per_decade: float,
    *,
    maximum: int = MAX_MODULE_SCAN_POINTS,
) -> tuple[int, float]:
    """根据每十倍频点数取整到整数点数，并返回同步修正后的实际每十倍频点数。"""

    if not math.isfinite(per_decade) or per_decade <= 0.0:
        raise ValueError("Points per decade must be a positive number")
    a, b = float(start), float(stop)
    if a == 0.0 or b == 0.0 or (a > 0.0) != (b > 0.0):
        raise ValueError("Log scan requires non-zero Start and Stop with the same sign")
    if a == b:
        return 1, 0.0
    decades = abs(math.log10(abs(b) / abs(a)))
    steps = max(2, min(maximum, int(round(per_decade * decades)) + 1))
    return steps, (steps - 1) / decades


def generate_scan_points(
    mode: str,
    start: object,
    stop: object,
    steps: int,
    *,
    unit: str = "",
    decimals: int | None = None,
) -> list[str]:
    """按 Linear 或 Log 模式生成模块扫描点文本列表。"""

    mode_key = str(mode).strip().casefold()
    if mode_key not in {"linear", "log"}:
        raise ValueError(f"Unsupported generated scan mode: {mode!r}")
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= MAX_MODULE_SCAN_POINTS:
        raise ValueError(f"Points must be an integer from 1 to {MAX_MODULE_SCAN_POINTS}")
    start_dec = parse_si_quantity(start, unit, "Start")
    stop_dec = parse_si_quantity(stop, unit, "Stop")
    if mode_key == "log":
        if start_dec == 0 or stop_dec == 0 or (start_dec > 0) != (stop_dec > 0):
            raise ValueError("Log scan requires non-zero Start and Stop with the same sign")

    if steps == 1:
        raw_values = [start_dec]
    elif mode_key == "linear":
        span = stop_dec - start_dec
        scale = max(abs(start_dec), abs(stop_dec))
        raw_values = []
        for index in range(steps):
            if index == 0:
                val = start_dec
            elif index == steps - 1:
                val = stop_dec
            else:
                val = start_dec + (span * Decimal(index)) / Decimal(steps - 1)
                if scale > 0 and abs(val) <= Decimal("1e-14") * scale:
                    val = Decimal("0")
            raw_values.append(Decimal("0") if val == 0 else val)
    else:
        sign = Decimal("-1") if start_dec < 0 else Decimal("1")
        log_a = math.log(abs(float(start_dec)))
        log_b = math.log(abs(float(stop_dec)))
        raw_values = []
        for index in range(steps):
            if index == 0:
                val = start_dec
            elif index == steps - 1:
                val = stop_dec
            else:
                mag = math.exp(log_a + (log_b - log_a) * index / (steps - 1))
                val = sign * Decimal(f"{mag:.12g}")
            raw_values.append(val)

    if decimals is not None:
        quantum = Decimal(10) ** -decimals
        raw_values = [
            Decimal("0") if (q := val.quantize(quantum, rounding=ROUND_HALF_UP)) == 0 else q
            for val in raw_values
        ]
    return [format_si_quantity(val) for val in raw_values]


def _single_line(value: object, label: str, *, maximum: int) -> str:
    text = str(value).strip()
    if not text:
        raise TypeError(f"{label} must not be empty")
    if len(text) > maximum or any(character in text for character in "\r\n\0"):
        raise TypeError(f"{label} must be a single-line value of at most {maximum} characters")
    return text


def _identifier(value: object, label: str) -> str:
    text = _single_line(value, label, maximum=64)
    if not _IDENTIFIER.fullmatch(text):
        raise TypeError(f"{label} must match [a-z][a-z0-9_]*")
    return text


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise TypeError(f"{label} must be a finite number")
    return number


def _json_scalar_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a JSON array")
    if len(value) > MAX_MODULE_SCAN_POINTS:
        raise TypeError(
            f"{label} is limited to {MAX_MODULE_SCAN_POINTS} items"
        )
    result: list[Any] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, (dict, list)):
            raise TypeError(f"{label} item {index} must be a JSON scalar")
        if isinstance(item, float) and not math.isfinite(item):
            raise TypeError(f"{label} item {index} must be finite")
        if not isinstance(item, (str, int, float, bool, type(None))):
            raise TypeError(f"{label} item {index} must be a JSON scalar")
        result.append(item)
    return result


def _normalize_field(raw: object, command_id: str) -> FieldSpec:
    if not isinstance(raw, Mapping):
        raise TypeError(f"sequence command {command_id!r} fields must be objects")
    unknown = sorted(
        set(raw)
        - {
            "name",
            "label",
            "type",
            "default",
            "minimum",
            "maximum",
            "choices",
            "unit",
            "decimals",
        }
    )
    if unknown:
        raise TypeError(
            f"sequence command {command_id!r} field has unknown keys: "
            + ", ".join(str(item) for item in unknown)
        )
    name = _identifier(raw.get("name", ""), "sequence command field name")
    label = _single_line(
        raw.get("label", name.replace("_", " ").title()),
        "sequence command field label",
        maximum=100,
    )
    field_type = str(raw.get("type", "text")).strip().casefold()
    if field_type not in _FIELD_TYPES:
        raise TypeError(
            f"sequence command {command_id!r} field {name!r} type must be one of "
            + ", ".join(sorted(_FIELD_TYPES))
        )
    if "default" not in raw:
        raise TypeError(
            f"sequence command {command_id!r} field {name!r} requires a default"
        )
    default = raw["default"]
    minimum = (
        None
        if raw.get("minimum") is None
        else _finite_number(raw["minimum"], f"{name}.minimum")
    )
    maximum = (
        None
        if raw.get("maximum") is None
        else _finite_number(raw["maximum"], f"{name}.maximum")
    )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise TypeError(f"{name}.minimum must not exceed {name}.maximum")
    choices_value = raw.get("choices", [])
    if not isinstance(choices_value, list):
        raise TypeError(f"{name}.choices must be an array")
    choices = tuple(
        _single_line(item, f"{name}.choices", maximum=100)
        for item in choices_value
    )
    if len(choices) != len(set(choices)):
        raise TypeError(f"{name}.choices must not contain duplicates")
    if field_type == "choice" and not choices:
        raise TypeError(f"{name}.choices must not be empty for a choice field")
    if field_type != "choice" and choices:
        raise TypeError(f"{name}.choices is valid only for a choice field")
    if field_type not in {"int", "float"} and (
        minimum is not None
        or maximum is not None
        or raw.get("decimals") is not None
    ):
        raise TypeError(
            f"{name}.minimum, maximum, and decimals are valid only for numeric fields"
        )
    unit_value = str(raw.get("unit", "")).strip()
    if len(unit_value) > 32 or any(character in unit_value for character in "\r\n\0"):
        raise TypeError(f"{name}.unit must be a single-line value of at most 32 characters")
    decimals_value = raw.get("decimals")
    decimals: int | None = None
    if decimals_value is not None:
        if isinstance(decimals_value, bool):
            raise TypeError(f"{name}.decimals must be an integer from 0 to 15")
        try:
            decimals = int(decimals_value)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{name}.decimals must be an integer from 0 to 15") from exc
        if decimals != float(decimals_value) or not 0 <= decimals <= 15:
            raise TypeError(f"{name}.decimals must be an integer from 0 to 15")

    field = FieldSpec(
        name,
        label,
        field_type,
        default,
        minimum,
        maximum,
        choices,
        unit_value,
        (
            1
            if field_type == "float" and (
                (unit_value == "s" and "filter" not in name)
                or unit_value.upper() in {"PLC", "NPLC"}
                or name == "nplc"
            )
            else decimals
        ),
    )
    issues = validate_module_field_value(field, default)
    if issues:
        raise TypeError(
            f"invalid default for sequence command {command_id!r} field {name!r}: "
            + "; ".join(issues)
        )
    if field_type == "choice" and default not in choices:
        raise TypeError(
            f"invalid default for sequence command {command_id!r} field {name!r}: "
            "choice defaults must exactly match one declared choice"
        )
    return field


@dataclass(frozen=True, slots=True)
class ModulePointLimitSpec:
    """模块扫参或单点文本字段的可选限幅声明，支持与模块 Settings 同步。"""

    setting: str = ""
    symmetric: bool = False
    minimum: float | None = None
    maximum: float | None = None
    field: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.setting:
            payload["setting"] = self.setting
        if self.symmetric:
            payload["symmetric"] = True
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        if self.field:
            payload["field"] = self.field
        return payload


@dataclass(frozen=True, slots=True)
class ModuleCommandSpec:
    """一个 Enabled 模块向 Sequence Command Bar 暴露的稳定指令。"""

    module_id: str
    command_id: str
    label: str
    description: str
    kind: str
    fields: tuple[FieldSpec, ...]
    custom_editor: bool = False
    points_field: str = ""
    point_parameter: str = ""
    scan_modes: tuple[str, ...] = ()
    point_unit: str = ""
    point_decimals: int | None = None
    point_limits: ModulePointLimitSpec | None = None

    @property
    def command_type(self) -> CommandType:
        return (
            CommandType.MODULE_SCAN
            if self.kind == "scan"
            else CommandType.MODULE_COMMAND
        )

    def allowed_parameter_names(self) -> frozenset[str]:
        names = {field.name for field in self.fields}
        if self.scan_modes:
            names |= _RESERVED_SCAN_FIELDS
        return frozenset(names)

    def default_scan_bounds(self) -> tuple[str, str, int]:
        points_spec = next(
            (field for field in self.fields if field.name == self.points_field),
            None,
        )
        default = points_spec.default if points_spec is not None and isinstance(points_spec.default, list) else []
        if default:
            return str(default[0]), str(default[-1]), max(1, len(default))
        return "0", "1", 5

    def create(self) -> Command:
        if self.scan_modes and self.scan_modes[0] in {"Linear", "Log"}:
            start, stop, steps = self.default_scan_bounds()
            params: dict[str, Any] = {
                "point_mode": self.scan_modes[0],
                "start": start,
                "stop": stop,
                "steps": steps,
            }
            for field in self.fields:
                if field.name != self.points_field:
                    params[field.name] = deepcopy(field.default)
        else:
            params = {
                field.name: deepcopy(field.default)
                for field in self.fields
            }
            if self.scan_modes:
                params = {"point_mode": self.scan_modes[0], **params}
        return Command(
            self.command_type,
            params,
            module_id=self.module_id,
            module_command_id=self.command_id,
        )

    def to_payload(self) -> dict[str, Any]:
        """转换为可重复验证的受限 JSON 声明，不携带 Python/Qt 对象。"""

        payload: dict[str, Any] = {
            "id": self.command_id,
            "label": self.label,
            "description": self.description,
            "kind": self.kind,
            "custom_editor": self.custom_editor,
            "fields": [
                {
                    "name": field.name,
                    "label": field.label,
                    "type": field.field_type,
                    "default": field.default,
                    "minimum": field.minimum,
                    "maximum": field.maximum,
                    "choices": list(field.choices),
                    "unit": field.unit,
                    "decimals": field.decimals,
                }
                for field in self.fields
            ],
        }
        if self.kind == "scan":
            payload["points_field"] = self.points_field
            payload["point_parameter"] = self.point_parameter
            if self.scan_modes:
                payload["scan_modes"] = list(self.scan_modes)
            if self.point_decimals is not None:
                payload["point_decimals"] = self.point_decimals
        if self.point_unit:
            payload["point_unit"] = self.point_unit
        if self.point_limits is not None:
            payload["point_limits"] = self.point_limits.to_payload()
        return payload


def _normalize_point_limits(
    raw: object,
    command_id: str,
    kind: str,
    field_by_name: Mapping[str, FieldSpec],
) -> ModulePointLimitSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError(f"sequence command {command_id!r} point_limits must be an object")
    unknown = sorted(set(raw) - {"setting", "symmetric", "minimum", "maximum", "field"})
    if unknown:
        raise TypeError(
            f"sequence command {command_id!r} point_limits has unknown keys: "
            + ", ".join(str(item) for item in unknown)
        )
    setting = ""
    if raw.get("setting") is not None and str(raw.get("setting")).strip():
        setting = _single_line(
            raw["setting"],
            f"sequence command {command_id!r} point_limits.setting",
            maximum=100,
        )
    symmetric = raw.get("symmetric", False)
    if not isinstance(symmetric, bool):
        raise TypeError(f"sequence command {command_id!r} point_limits.symmetric must be a boolean")
    minimum = (
        None
        if raw.get("minimum") is None
        else _finite_number(raw["minimum"], f"{command_id}.point_limits.minimum")
    )
    maximum = (
        None
        if raw.get("maximum") is None
        else _finite_number(raw["maximum"], f"{command_id}.point_limits.maximum")
    )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise TypeError(f"{command_id}.point_limits.minimum must not exceed maximum")
    target_field = ""
    if raw.get("field") is not None and str(raw.get("field")).strip():
        target_field = _identifier(
            raw["field"],
            f"sequence command {command_id!r} point_limits.field",
        )
    if kind == "scan" and target_field:
        raise TypeError(f"sequence command {command_id!r} point_limits.field is only valid for non-scan commands")
    if kind != "scan":
        if not target_field or target_field not in field_by_name:
            raise TypeError(
                f"sequence command {command_id!r} point_limits.field must name a declared field"
            )
    return ModulePointLimitSpec(setting, symmetric, minimum, maximum, target_field)


def normalize_module_commands(
    module_id: str,
    value: object,
) -> tuple[ModuleCommandSpec, ...]:
    """严格规范化模块声明；任何歧义都会让该模块 Enable 失败。"""

    normalized_module_id = _identifier(module_id, "module id")
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("Module.sequence_commands must be a sequence of objects")
    if len(value) > _MAX_COMMANDS:
        raise TypeError(f"A module may declare at most {_MAX_COMMANDS} sequence commands")
    specs: list[ModuleCommandSpec] = []
    seen_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("Module.sequence_commands entries must be objects")
        unknown = sorted(
            set(raw)
            - {
                "id",
                "label",
                "description",
                "kind",
                "fields",
                "custom_editor",
                "points_field",
                "point_parameter",
                "scan_modes",
                "point_unit",
                "point_decimals",
                "point_limits",
            }
        )
        if unknown:
            raise TypeError(
                "sequence command has unknown keys: "
                + ", ".join(str(item) for item in unknown)
            )
        command_id = _identifier(raw.get("id", ""), "sequence command id")
        if command_id in seen_ids:
            raise TypeError(f"duplicate sequence command id: {command_id}")
        seen_ids.add(command_id)
        label = _single_line(
            raw.get("label", command_id.replace("_", " ").title()),
            f"sequence command {command_id!r} label",
            maximum=100,
        )
        description = str(raw.get("description", "")).strip()
        if len(description) > 500 or "\0" in description:
            raise TypeError(
                f"sequence command {command_id!r} description is limited to 500 characters"
            )
        kind = str(raw.get("kind", "command")).strip().casefold()
        if kind not in {"command", "scan"}:
            raise TypeError(
                f"sequence command {command_id!r} kind must be command or scan"
            )
        custom_editor = raw.get("custom_editor", False)
        if not isinstance(custom_editor, bool):
            raise TypeError(
                f"sequence command {command_id!r} custom_editor must be a boolean"
            )
        raw_fields = raw.get("fields", [])
        if not isinstance(raw_fields, list):
            raise TypeError(f"sequence command {command_id!r} fields must be an array")
        if len(raw_fields) > _MAX_FIELDS:
            raise TypeError(
                f"sequence command {command_id!r} may declare at most {_MAX_FIELDS} fields"
            )
        fields = tuple(_normalize_field(item, command_id) for item in raw_fields)
        names = [field.name for field in fields]
        if len(names) != len(set(names)):
            raise TypeError(
                f"sequence command {command_id!r} field names must be unique"
            )
        field_by_name = {field.name: field for field in fields}
        points_field = ""
        point_parameter = ""
        scan_modes: tuple[str, ...] = ()
        point_decimals: int | None = None
        point_unit = str(raw.get("point_unit", "")).strip()
        if len(point_unit) > 32 or any(character in point_unit for character in "\r\n\0"):
            raise TypeError(f"sequence command {command_id!r} point_unit must be a short single-line string")
        if kind == "scan":
            points_field = _identifier(
                raw.get("points_field", "points"),
                f"sequence command {command_id!r} points_field",
            )
            point_parameter = _identifier(
                raw.get("point_parameter", "point"),
                f"sequence command {command_id!r} point_parameter",
            )
            points_spec = field_by_name.get(points_field)
            if points_spec is None or points_spec.field_type != "list":
                raise TypeError(
                    f"sequence command {command_id!r} points_field must name a list field"
                )
            if point_parameter in field_by_name:
                raise TypeError(
                    f"sequence command {command_id!r} point_parameter must not collide with a field"
                )
            if not points_spec.default:
                raise TypeError(
                    f"sequence command {command_id!r} default point list must not be empty"
                )
            raw_modes = raw.get("scan_modes")
            if raw_modes is not None:
                if not isinstance(raw_modes, list) or not raw_modes:
                    raise TypeError(
                        f"sequence command {command_id!r} scan_modes must be a non-empty array"
                    )
                normalized_modes: list[str] = []
                for item in raw_modes:
                    canonical = _SCAN_MODE_BY_KEY.get(str(item).strip().casefold())
                    if canonical is None:
                        raise TypeError(
                            f"sequence command {command_id!r} scan_modes entries must be in "
                            + ", ".join(_SCAN_MODES)
                        )
                    if canonical not in normalized_modes:
                        normalized_modes.append(canonical)
                scan_modes = tuple(normalized_modes)
                collisions = sorted((set(field_by_name) | {point_parameter}) & _RESERVED_SCAN_FIELDS)
                if collisions:
                    raise TypeError(
                        f"sequence command {command_id!r} conflicts with reserved scan fields: "
                        + ", ".join(collisions)
                    )
            decimals_value = raw.get("point_decimals")
            if decimals_value is not None:
                if isinstance(decimals_value, bool):
                    raise TypeError(f"{command_id}.point_decimals must be an integer from 0 to 15")
                try:
                    point_decimals = int(decimals_value)
                except (TypeError, ValueError) as exc:
                    raise TypeError(f"{command_id}.point_decimals must be an integer from 0 to 15") from exc
                if point_decimals != float(decimals_value) or not 0 <= point_decimals <= 15:
                    raise TypeError(f"{command_id}.point_decimals must be an integer from 0 to 15")
        elif any(key in raw for key in ("points_field", "point_parameter", "scan_modes", "point_decimals")):
            raise TypeError(
                f"sequence command {command_id!r} point metadata is valid only for scans"
            )
        point_limits = _normalize_point_limits(
            raw.get("point_limits"),
            command_id,
            kind,
            field_by_name,
        )
        spec = ModuleCommandSpec(
            normalized_module_id,
            command_id,
            label,
            description,
            kind,
            fields,
            custom_editor,
            points_field,
            point_parameter,
            scan_modes,
            point_unit,
            point_decimals,
            point_limits,
        )
        # 确保 ready IPC 自身也满足严格 JSON，尤其拒绝 NaN/Infinity。
        json.dumps(spec.to_payload(), ensure_ascii=False, allow_nan=False)
        specs.append(spec)
    return tuple(specs)


def _format_bounds(low: float | None, high: float | None, unit: str) -> str:
    if low is not None and high is not None:
        return f"{format_si_with_unit(low, unit)} to {format_si_with_unit(high, unit)}"
    if low is not None:
        return f"≥ {format_si_with_unit(low, unit)}"
    if high is not None:
        return f"≤ {format_si_with_unit(high, unit)}"
    return "unbounded"


def resolve_module_point_bounds(
    spec: ModuleCommandSpec,
    module_settings: Mapping[str, Any] | None = None,
) -> tuple[float | None, float | None, str]:
    """结合模块声明和当前 Settings 解析有效限幅及展示文本。"""

    limits = spec.point_limits
    if limits is None:
        return None, None, ""
    hw_min, hw_max = limits.minimum, limits.maximum
    unit = spec.point_unit
    if limits.setting:
        raw_setting = None if module_settings is None else module_settings.get(limits.setting)
        if raw_setting is None or str(raw_setting).strip() == "":
            if hw_min is not None or hw_max is not None:
                return (
                    hw_min,
                    hw_max,
                    f"Module setting '{limits.setting}' is not set; hardware limits: "
                    f"{_format_bounds(hw_min, hw_max, unit)}.",
                )
            return None, None, f"Module setting '{limits.setting}' is not set."
        try:
            cap = float(parse_si_quantity(raw_setting, unit, limits.setting))
        except ValueError:
            cap = math.nan
        if not math.isfinite(cap) or cap <= 0.0:
            if hw_min is not None or hw_max is not None:
                return (
                    hw_min,
                    hw_max,
                    f"Module setting '{limits.setting}' is not a positive limit; hardware limits: "
                    f"{_format_bounds(hw_min, hw_max, unit)}.",
                )
            return None, None, f"Module setting '{limits.setting}' is not a positive limit."
        setting_low = -cap if limits.symmetric else 0.0
        low = setting_low if hw_min is None else max(hw_min, setting_low)
        high = cap if hw_max is None else min(hw_max, cap)
        return (
            low,
            high,
            f"Configured limits ({limits.setting}): {_format_bounds(low, high, unit)}.",
        )
    if hw_min is not None or hw_max is not None:
        return hw_min, hw_max, f"Hardware limits: {_format_bounds(hw_min, hw_max, unit)}."
    return None, None, ""


def _effective_scan_mode(spec: ModuleCommandSpec, parameters: Mapping[str, Any]) -> str:
    if not spec.scan_modes:
        return "List"
    if "point_mode" not in parameters:
        return ""
    raw = str(parameters["point_mode"]).strip().casefold()
    for mode in spec.scan_modes:
        if mode.casefold() == raw:
            return mode
    return ""


def resolve_module_scan_points(
    spec: ModuleCommandSpec,
    parameters: Mapping[str, Any],
) -> list[Any]:
    """把模块扫描指令解析为实际逐点执行序列（兼容 List / Linear / Log）。"""

    if not spec.scan_modes:
        points = parameters.get(spec.points_field)
        if not isinstance(points, list) or not points:
            raise ValueError(f"{spec.points_field} must contain at least one point")
        return list(points)
    mode = _effective_scan_mode(spec, parameters)
    if mode == "List":
        points = parameters.get(spec.points_field)
        if not isinstance(points, list) or not points:
            raise ValueError(f"{spec.points_field} must contain at least one point")
        return list(points)
    if mode in {"Linear", "Log"}:
        steps_raw = parameters.get("steps")
        if isinstance(steps_raw, bool) or not isinstance(steps_raw, int):
            raise ValueError("Points must be an integer")
        return generate_scan_points(
            mode,
            parameters.get("start"),
            parameters.get("stop"),
            steps_raw,
            unit=spec.point_unit,
            decimals=spec.point_decimals,
        )
    raise ValueError(
        "Point definition must be one of " + ", ".join(spec.scan_modes)
    )


def estimate_module_scan_points(
    spec: ModuleCommandSpec,
    parameters: Mapping[str, Any],
) -> int:
    """在 O(1) 内估算模块扫描点数，用于总进度计算。"""

    mode = _effective_scan_mode(spec, parameters)
    if mode in {"Linear", "Log"}:
        steps = parameters.get("steps")
        if isinstance(steps, bool):
            return 0
        try:
            return max(0, int(steps))
        except (TypeError, ValueError):
            return 0
    points = parameters.get(spec.points_field, [])
    return len(points) if isinstance(points, list) else 0


def validate_module_field_value(
    field: FieldSpec, value: object, *, check_precision: bool = False,
) -> tuple[str, ...]:
    """验证一个声明字段的运行值；返回可直接展示给用户的原因。"""

    issues: list[str] = []
    if field.field_type == "text":
        if not isinstance(value, str):
            issues.append(f"{field.label} must be text")
    elif field.field_type == "bool":
        if not isinstance(value, bool):
            issues.append(f"{field.label} must be true or false")
    elif field.field_type == "list":
        try:
            _json_scalar_list(value, field.label)
        except TypeError as exc:
            issues.append(str(exc))
    elif field.field_type in {"int", "float"}:
        try:
            number = _finite_number(value, field.label)
        except TypeError as exc:
            issues.append(str(exc))
        else:
            if field.field_type == "int" and not number.is_integer():
                issues.append(f"{field.label} must be an integer")
            if field.field_type == "float" and check_precision:
                try:
                    require_precision(number, field.decimals if field.decimals is not None else 9, field.label)
                except ValueError as exc:
                    issues.append(str(exc))
            if field.minimum is not None and number < field.minimum:
                issues.append(f"{field.label} must be at least {field.minimum:g}")
            if field.maximum is not None and number > field.maximum:
                issues.append(f"{field.label} must be no more than {field.maximum:g}")
    elif field.field_type == "choice":
        if not isinstance(value, str) or value.casefold() not in {
            choice.casefold() for choice in field.choices
        }:
            issues.append(
                f"{field.label} must be one of " + ", ".join(field.choices)
            )
    else:
        issues.append(f"{field.label} uses an unsupported field type")
    return tuple(issues)


def _check_quantity_bounds(
    value: object,
    unit: str,
    label: str,
    low: float | None,
    high: float | None,
    decimals: int | None = None,
) -> tuple[Decimal | None, str | None]:
    try:
        dec = parse_si_quantity(value, unit, label)
    except ValueError as exc:
        return None, str(exc)
    numeric = float(dec)
    if decimals is not None:
        try:
            require_precision(numeric, decimals, label)
        except ValueError as exc:
            return dec, str(exc)
    if low is not None and numeric < low:
        return dec, f"{label} ({format_si_with_unit(dec, unit)}) is below the minimum {format_si_with_unit(low, unit)}"
    if high is not None and numeric > high:
        return dec, f"{label} ({format_si_with_unit(dec, unit)}) exceeds the maximum {format_si_with_unit(high, unit)}"
    return dec, None


def validate_module_command_parameters(
    spec: ModuleCommandSpec,
    parameters: object,
) -> tuple[str, ...]:
    """按 Enabled 模块当前声明验证完整参数对象，包括未知和缺失字段。"""

    if not isinstance(parameters, Mapping):
        return ("Module command parameters must be an object",)
    non_string_names = [name for name in parameters if not isinstance(name, str)]
    supplied = {str(name) for name in parameters}
    issues: list[str] = []
    if non_string_names:
        issues.append("Module command parameter names must be strings")

    hw_min = None if spec.point_limits is None else spec.point_limits.minimum
    hw_max = None if spec.point_limits is None else spec.point_limits.maximum

    if spec.scan_modes:
        mode = _effective_scan_mode(spec, parameters)
        base_fields = {field.name for field in spec.fields if field.name != spec.points_field}
        if not mode:
            if "point_mode" in parameters:
                issues.append("Point definition must be one of " + ", ".join(spec.scan_modes))
            else:
                issues.append("Missing parameters: point_mode")
        elif mode == "List":
            required = base_fields | {"point_mode", spec.points_field}
            missing = sorted(required - supplied)
            unknown = sorted(supplied - required)
            if missing:
                issues.append("Missing parameters: " + ", ".join(missing))
            if unknown:
                issues.append("Unknown parameters: " + ", ".join(unknown))
        else:
            required = base_fields | {"point_mode", "start", "stop", "steps"}
            missing = sorted(required - supplied)
            unknown = sorted(supplied - required)
            if missing:
                issues.append("Missing parameters: " + ", ".join(missing))
            if unknown:
                issues.append("Unknown parameters: " + ", ".join(unknown))
            start_dec = None
            stop_dec = None
            if "start" in parameters:
                start_dec, err = _check_quantity_bounds(
                    parameters["start"], spec.point_unit, "Start", hw_min, hw_max, spec.point_decimals
                )
                if err:
                    issues.append(err)
            if "stop" in parameters:
                stop_dec, err = _check_quantity_bounds(
                    parameters["stop"], spec.point_unit, "Stop", hw_min, hw_max, spec.point_decimals
                )
                if err:
                    issues.append(err)
            if "steps" in parameters:
                steps_val = parameters["steps"]
                if isinstance(steps_val, bool) or not isinstance(steps_val, int):
                    issues.append("Points must be an integer")
                elif not 1 <= steps_val <= MAX_MODULE_SCAN_POINTS:
                    issues.append(f"Points must be between 1 and {MAX_MODULE_SCAN_POINTS}")
            if (
                mode == "Log"
                and start_dec is not None
                and stop_dec is not None
                and (start_dec == 0 or stop_dec == 0 or (start_dec > 0) != (stop_dec > 0))
            ):
                issues.append("Log scan requires non-zero Start and Stop with the same sign")
        for field in spec.fields:
            if field.name == spec.points_field and mode != "List":
                continue
            if field.name in parameters:
                issues.extend(validate_module_field_value(
                    field, parameters[field.name], check_precision=not spec.custom_editor,
                ))
        if mode == "List":
            points = parameters.get(spec.points_field)
            if isinstance(points, list):
                if not points:
                    issues.append(f"{spec.points_field} must contain at least one point")
                elif spec.point_unit or spec.point_limits is not None or spec.point_decimals is not None:
                    for index, item in enumerate(points, start=1):
                        _, err = _check_quantity_bounds(
                            item, spec.point_unit, f"Point {index}", hw_min, hw_max, spec.point_decimals
                        )
                        if err:
                            issues.append(err)
                            break
    else:
        expected = {field.name for field in spec.fields}
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        if missing:
            issues.append("Missing parameters: " + ", ".join(missing))
        if unknown:
            issues.append("Unknown parameters: " + ", ".join(unknown))
        for field in spec.fields:
            if field.name in parameters:
                issues.extend(validate_module_field_value(
                    field, parameters[field.name], check_precision=not spec.custom_editor,
                ))
        if spec.kind == "scan":
            points = parameters.get(spec.points_field)
            if isinstance(points, list) and not points:
                issues.append(f"{spec.points_field} must contain at least one point")
        elif spec.point_limits is not None and spec.point_limits.field in parameters:
            _, err = _check_quantity_bounds(
                parameters[spec.point_limits.field],
                spec.point_unit,
                spec.point_limits.field.replace("_", " ").title(),
                hw_min,
                hw_max,
            )
            if err:
                issues.append(err)

    try:
        json.dumps(dict(parameters), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        issues.append(f"Parameters are not valid JSON: {exc}")
    return tuple(dict.fromkeys(issues))


def module_command_key(command: Command) -> tuple[str, str] | None:
    if command.type not in {CommandType.MODULE_COMMAND, CommandType.MODULE_SCAN}:
        return None
    return command.module_id, command.module_command_id


__all__ = [
    "MAX_MODULE_SCAN_POINTS",
    "ModuleCommandSpec",
    "ModulePointLimitSpec",
    "estimate_module_scan_points",
    "format_si_quantity",
    "format_si_with_unit",
    "generate_scan_points",
    "linear_points_from_step",
    "linear_step_from_points",
    "log_points_from_per_decade",
    "log_points_per_decade",
    "module_command_key",
    "normalize_module_commands",
    "parse_si_quantity",
    "resolve_module_point_bounds",
    "resolve_module_scan_points",
    "validate_module_command_parameters",
]
