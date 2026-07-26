"""控制界面与 DAT 展示共用的数值精度规则。"""

from __future__ import annotations

from .models import DeviceKind


def field_decimals(unit: object) -> int:
    """返回磁场标准小数位，同时保留旧版 Tesla 显示精度。"""
    return 2 if str(unit).strip().lower() == "oe" else 6


def control_decimals(kind: DeviceKind, unit: object) -> int:
    """按设备类型和单位选择手动控制输入的小数位。"""

    if kind is DeviceKind.FIELD:
        return field_decimals(unit)
    return 3


def fixed_number(value: object, decimals: int) -> str:
    """格式化定点数，并把舍入后的负零统一显示为正零。"""
    rounded = round(float(value), decimals)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:.{decimals}f}"
