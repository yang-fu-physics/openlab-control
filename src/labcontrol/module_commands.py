"""Measurement Module 可选 SEQ 指令的声明和验证。

模块用普通 ``dict`` 声明指令，因而无需继承核心类。声明先在隔离 worker 中验证，再以
受限 JSON 元数据发送给主进程；界面和运行时继续各自验证一次，不能把参数窗口当作真实
仪表的安全边界。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .sequence.model import Command, CommandType, FieldSpec


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_FIELD_TYPES = frozenset({"text", "int", "float", "choice", "bool", "list"})
_MAX_COMMANDS = 128
_MAX_FIELDS = 64
MAX_MODULE_SCAN_POINTS = 100_000


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
        decimals,
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

    @property
    def command_type(self) -> CommandType:
        return (
            CommandType.MODULE_SCAN
            if self.kind == "scan"
            else CommandType.MODULE_COMMAND
        )

    def create(self) -> Command:
        return Command(
            self.command_type,
            {
                field.name: deepcopy(field.default)
                for field in self.fields
            },
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
        return payload


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
        points_field = ""
        point_parameter = ""
        if kind == "scan":
            points_field = _identifier(
                raw.get("points_field", "points"),
                f"sequence command {command_id!r} points_field",
            )
            point_parameter = _identifier(
                raw.get("point_parameter", "point"),
                f"sequence command {command_id!r} point_parameter",
            )
            field_by_name = {field.name: field for field in fields}
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
        elif "points_field" in raw or "point_parameter" in raw:
            raise TypeError(
                f"sequence command {command_id!r} point metadata is valid only for scans"
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
        )
        # 确保 ready IPC 自身也满足严格 JSON，尤其拒绝 NaN/Infinity。
        json.dumps(spec.to_payload(), ensure_ascii=False, allow_nan=False)
        specs.append(spec)
    return tuple(specs)


def validate_module_field_value(field: FieldSpec, value: object) -> tuple[str, ...]:
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


def validate_module_command_parameters(
    spec: ModuleCommandSpec,
    parameters: object,
) -> tuple[str, ...]:
    """按 Enabled 模块当前声明验证完整参数对象，包括未知和缺失字段。"""

    if not isinstance(parameters, Mapping):
        return ("Module command parameters must be an object",)
    non_string_names = [name for name in parameters if not isinstance(name, str)]
    expected = {field.name for field in spec.fields}
    supplied = {str(name) for name in parameters}
    issues: list[str] = []
    if non_string_names:
        issues.append("Module command parameter names must be strings")
    missing = sorted(expected - supplied)
    unknown = sorted(supplied - expected)
    if missing:
        issues.append("Missing parameters: " + ", ".join(missing))
    if unknown:
        issues.append("Unknown parameters: " + ", ".join(unknown))
    for field in spec.fields:
        if field.name in parameters:
            issues.extend(validate_module_field_value(field, parameters[field.name]))
    if spec.kind == "scan":
        points = parameters.get(spec.points_field)
        if isinstance(points, list) and not points:
            issues.append(f"{spec.points_field} must contain at least one point")
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
    "module_command_key",
    "normalize_module_commands",
    "validate_module_command_parameters",
]
