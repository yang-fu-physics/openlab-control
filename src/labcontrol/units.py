"""框架内部允许的显式单位换算。

目前只在 Tesla 与 Oersted 间换算；未知组合必须报错，不能在真实仪表控制前静默猜测单位。
"""

from __future__ import annotations


class UnitConversionError(ValueError):
    """请求了框架未定义的单位换算。"""


def convert_value(value: float, source_unit: str, target_unit: str) -> float:
    """在已知单位间换算；空单位或相同单位保持原值。"""

    source = source_unit.strip().lower()
    target = target_unit.strip().lower()
    if source == target or not source or not target:
        return value
    if source == "t" and target == "oe":
        return value * 10000.0
    if source == "oe" and target == "t":
        return value / 10000.0
    raise UnitConversionError(f"Cannot convert from {source_unit} to {target_unit}")
