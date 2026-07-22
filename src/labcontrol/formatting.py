from __future__ import annotations

from .models import DeviceKind


def field_decimals(unit: object) -> int:
    """Return standard field precision without reducing legacy T resolution."""
    return 2 if str(unit).strip().lower() == "oe" else 6


def control_decimals(kind: DeviceKind, unit: object) -> int:
    if kind is DeviceKind.FIELD:
        return field_decimals(unit)
    return 3


def fixed_number(value: object, decimals: int) -> str:
    """Format a fixed-point value and suppress a rounded negative zero."""
    rounded = round(float(value), decimals)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:.{decimals}f}"
