"""已加载 DLL 向主界面暴露的自描述手动函数。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .module_commands import (
    normalize_module_commands,
    validate_module_field_value,
)
from .sequence.model import FieldSpec


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_OUTPUT_TYPES = frozenset({"text", "int", "float", "bool"})
_MAX_FUNCTIONS = 128
_MAX_OUTPUTS = 64


def _single_line(value: object, label: str, maximum: int) -> str:
    text = str(value).strip()
    if not text or len(text) > maximum or any(c in text for c in "\r\n\0"):
        raise TypeError(
            f"{label} must be a non-empty single-line value of at most "
            f"{maximum} characters"
        )
    return text


def _identifier(value: object, label: str) -> str:
    text = _single_line(value, label, 64)
    if not _IDENTIFIER.fullmatch(text):
        raise TypeError(f"{label} must match [a-z][a-z0-9_]*")
    return text


@dataclass(frozen=True, slots=True)
class DLLFunctionOutput:
    """一个 DLL 手动函数的只读输出。"""

    name: str
    label: str
    value_type: str
    unit: str = ""
    decimals: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.value_type,
            "unit": self.unit,
            "decimals": self.decimals,
        }


@dataclass(frozen=True, slots=True)
class DLLFunctionSpec:
    """一个可由主菜单打开的 DLL 函数及其自动表单。"""

    function_id: str
    label: str
    description: str
    inputs: tuple[FieldSpec, ...]
    outputs: tuple[DLLFunctionOutput, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.function_id,
            "label": self.label,
            "description": self.description,
            "inputs": [
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
                for field in self.inputs
            ],
            "outputs": [output.to_payload() for output in self.outputs],
        }


def _normalize_output(raw: object, function_id: str) -> DLLFunctionOutput:
    if not isinstance(raw, Mapping):
        raise TypeError(f"DLL function {function_id!r} outputs must be objects")
    unknown = sorted(set(raw) - {"name", "label", "type", "unit", "decimals"})
    if unknown:
        raise TypeError(
            f"DLL function {function_id!r} output has unknown keys: "
            + ", ".join(str(item) for item in unknown)
        )
    name = _identifier(raw.get("name", ""), "DLL function output name")
    label = _single_line(
        raw.get("label", name.replace("_", " ").title()),
        "DLL function output label",
        100,
    )
    value_type = str(raw.get("type", "text")).strip().casefold()
    if value_type not in _OUTPUT_TYPES:
        raise TypeError(
            f"DLL function {function_id!r} output {name!r} type must be one of "
            + ", ".join(sorted(_OUTPUT_TYPES))
        )
    unit = str(raw.get("unit", "")).strip()
    if len(unit) > 32 or any(c in unit for c in "\r\n\0"):
        raise TypeError(f"DLL function output {name!r} has an invalid unit")
    decimals_value = raw.get("decimals")
    decimals: int | None = None
    if decimals_value is not None:
        if isinstance(decimals_value, bool):
            raise TypeError(f"DLL function output {name!r} decimals must be 0 to 15")
        try:
            decimals = int(decimals_value)
            exact_value = float(decimals_value)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"DLL function output {name!r} decimals must be 0 to 15"
            ) from exc
        if decimals != exact_value or not 0 <= decimals <= 15:
            raise TypeError(f"DLL function output {name!r} decimals must be 0 to 15")
    if value_type != "float" and decimals is not None:
        raise TypeError(
            f"DLL function output {name!r} decimals is valid only for float"
        )
    return DLLFunctionOutput(name, label, value_type, unit, decimals)


def normalize_dll_functions(value: object) -> tuple[DLLFunctionSpec, ...]:
    """验证 DLL 自描述数据；空值表示该扩展没有额外手动函数。"""

    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("DLL functions must be an array")
    if len(value) > _MAX_FUNCTIONS:
        raise TypeError(f"A DLL may expose at most {_MAX_FUNCTIONS} manual functions")
    result: list[DLLFunctionSpec] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("DLL function entries must be objects")
        unknown = sorted(set(raw) - {"id", "label", "description", "inputs", "outputs"})
        if unknown:
            raise TypeError(
                "DLL function has unknown keys: "
                + ", ".join(str(item) for item in unknown)
            )
        function_id = _identifier(raw.get("id", ""), "DLL function id")
        if function_id in seen:
            raise TypeError(f"Duplicate DLL function id: {function_id}")
        seen.add(function_id)
        label = _single_line(
            raw.get("label", function_id.replace("_", " ").title()),
            "DLL function label",
            100,
        )
        description = str(raw.get("description", "")).strip()
        if len(description) > 500 or "\0" in description:
            raise TypeError("DLL function description is limited to 500 characters")
        raw_inputs = raw.get("inputs", [])
        if not isinstance(raw_inputs, list):
            raise TypeError(f"DLL function {function_id!r} inputs must be an array")
        # DLL 输入沿用已经过测试的 SEQ 字段格式，但不会生成或写入 SEQ。
        input_spec = normalize_module_commands(
            "dll",
            [{
                "id": function_id,
                "label": label,
                "description": description,
                "fields": raw_inputs,
            }],
        )[0]
        raw_outputs = raw.get("outputs", [])
        if not isinstance(raw_outputs, list) or len(raw_outputs) > _MAX_OUTPUTS:
            raise TypeError(
                f"DLL function {function_id!r} outputs must be an array of at most "
                f"{_MAX_OUTPUTS} items"
            )
        outputs = tuple(_normalize_output(item, function_id) for item in raw_outputs)
        output_names = [item.name for item in outputs]
        if len(output_names) != len(set(output_names)):
            raise TypeError(f"DLL function {function_id!r} output names must be unique")
        spec = DLLFunctionSpec(
            function_id,
            label,
            description,
            input_spec.fields,
            outputs,
        )
        issues = validate_dll_parameters(
            spec, {field.name: field.default for field in spec.inputs}
        )
        if issues:
            raise TypeError(f"Invalid DLL function defaults: {'; '.join(issues)}")
        json.dumps(spec.to_payload(), ensure_ascii=False, allow_nan=False)
        result.append(spec)
    return tuple(result)


def validate_dll_parameters(
    spec: DLLFunctionSpec,
    parameters: object,
) -> tuple[str, ...]:
    """在把用户输入送入 worker 前验证完整参数对象。"""

    if not isinstance(parameters, Mapping):
        return ("DLL function parameters must be an object",)
    expected = {field.name for field in spec.inputs}
    supplied = set(parameters)
    issues: list[str] = []
    if any(not isinstance(name, str) for name in supplied):
        issues.append("DLL function parameter names must be strings")
        supplied = {str(name) for name in supplied}
    missing = sorted(expected - supplied)
    unknown = sorted(supplied - expected)
    if missing:
        issues.append("Missing parameters: " + ", ".join(missing))
    if unknown:
        issues.append("Unknown parameters: " + ", ".join(unknown))
    for field in spec.inputs:
        if field.name in parameters:
            value = parameters[field.name]
            if field.field_type == "int" and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                issues.append(f"{field.label} must be a JSON integer")
            elif field.field_type == "float" and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                issues.append(f"{field.label} must be a JSON number")
            elif field.field_type == "choice" and value not in field.choices:
                issues.append(f"{field.label} must match one declared choice exactly")
            else:
                issues.extend(validate_module_field_value(field, value))
    try:
        json.dumps(dict(parameters), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        issues.append(f"Parameters are not valid JSON: {exc}")
    return tuple(dict.fromkeys(issues))


def validate_dll_result(
    spec: DLLFunctionSpec,
    result: object,
) -> dict[str, Any]:
    """验证 DLL 成功结果与自描述输出完全一致。"""

    if not isinstance(result, Mapping):
        raise TypeError("DLL function result must be an object")
    if any(not isinstance(name, str) for name in result):
        raise TypeError("DLL function result field names must be strings")
    expected = {output.name for output in spec.outputs}
    supplied = set(result)
    if supplied != expected:
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(str(item) for item in unknown))
        raise TypeError("DLL function result fields are invalid: " + "; ".join(details))
    values = dict(result)
    for output in spec.outputs:
        value = values[output.name]
        if output.value_type == "text" and not isinstance(value, str):
            raise TypeError(f"DLL function output {output.name!r} must be text")
        if output.value_type == "bool" and not isinstance(value, bool):
            raise TypeError(f"DLL function output {output.name!r} must be boolean")
        if output.value_type == "int" and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise TypeError(f"DLL function output {output.name!r} must be an integer")
        if output.value_type == "float" and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise TypeError(f"DLL function output {output.name!r} must be finite numeric")
    json.dumps(values, ensure_ascii=False, allow_nan=False)
    return values


__all__ = [
    "DLLFunctionOutput",
    "DLLFunctionSpec",
    "normalize_dll_functions",
    "validate_dll_parameters",
    "validate_dll_result",
]
