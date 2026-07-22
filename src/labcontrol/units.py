from __future__ import annotations


class UnitConversionError(ValueError):
    pass


def convert_value(value: float, source_unit: str, target_unit: str) -> float:
    source = source_unit.strip().lower()
    target = target_unit.strip().lower()
    if source == target or not source or not target:
        return value
    if source == "t" and target == "oe":
        return value * 10000.0
    if source == "oe" and target == "t":
        return value / 10000.0
    raise UnitConversionError(f"Cannot convert from {source_unit} to {target_unit}")
