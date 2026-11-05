"""控制界面与 DAT 展示共用的数值精度规则。"""

from __future__ import annotations

import math

from .models import InstrumentKind


def field_decimals(unit: object) -> int:
    """返回磁场标准小数位，同时保留旧版 Tesla 显示精度。"""
    return 2 if str(unit).strip().lower() == "oe" else 6


def control_decimals(kind: InstrumentKind, unit: object) -> int:
    """按仪表类型和单位选择手动控制输入的小数位。"""

    if kind is InstrumentKind.FIELD:
        return field_decimals(unit)
    return 3


def fixed_number(value: object, decimals: int) -> str:
    """格式化定点数，并把舍入后的负零统一显示为正零。"""
    rounded = round(float(value), decimals)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:.{decimals}f}"


def require_precision(value: object, decimals: int, label: str = "Value") -> float:
    """在参数输入边界拒绝会被控件舍入的值，容许浮点换算的末位误差。"""
    number = float(value)
    rounded = round(number, decimals)
    if not math.isfinite(number) or not math.isclose(number, rounded, rel_tol=1e-14, abs_tol=0.0):
        raise ValueError(f"{label}: {value} must have at most {decimals} decimal places")
    return rounded
