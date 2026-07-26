from __future__ import annotations

import re
from collections.abc import Iterable


def parse_qqs(value: object) -> frozenset[int]:
    if value is None or isinstance(value, bool):
        return frozenset()
    if isinstance(value, int):
        return (
            frozenset((value,))
            if value > 0
            else frozenset()
        )
    if isinstance(value, str):
        values: Iterable[object] = re.split(
            r"[\s,;]+",
            value.strip().strip("[](){}"),
        )
    elif isinstance(value, Iterable):
        values = value
    else:
        values = (value,)
    result: set[int] = set()
    for item in values:
        if item is None or item == "":
            continue
        try:
            qq = int(item)
        except (TypeError, ValueError):
            continue
        if qq > 0:
            result.add(qq)
    return frozenset(result)


def recipients(
    level: str,
    admin_qqs: frozenset[int],
    tester_qqs: frozenset[int],
) -> frozenset[int]:
    if level == "error":
        return admin_qqs | tester_qqs
    if level == "warning":
        return tester_qqs
    return frozenset()


__all__ = ["parse_qqs", "recipients"]
