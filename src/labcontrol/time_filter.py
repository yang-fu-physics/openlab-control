"""Time-column discovery and bound conversion for the data-filter dialog."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from .axis_ticks import TimestampReference, timestamp_reference


_TIME_COLUMN_PATTERN = re.compile(
    r"^(?:timestamp|time)(?:s|sec|secs|second|seconds)?\d*$",
    re.IGNORECASE,
)
_DURATION_PATTERN = re.compile(
    r"^(?P<sign>[+-])?(?:(?P<days>\d+(?:\.\d+)?)\s*d(?:ays?)?\s+)?"
    r"(?:(?P<hours>\d+):)?(?P<minutes>\d+):"
    r"(?P<seconds>\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TimeFilterContext:
    """Describe the fixed first filter row for one loaded document."""

    column: str | None
    reference: TimestampReference | None = None
    initial_minimum: float | None = None

    @property
    def has_column(self) -> bool:
        return self.column is not None

    @property
    def is_wall_time(self) -> bool:
        return self.reference is not None

    @property
    def column_label(self) -> str:
        if self.column is None:
            return "No timestamp/time column detected"
        if self.reference is None:
            return "%s (elapsed time; no reliable date mapping)" % self.column
        return "%s (wall time; %s; inferred from %s)" % (
            self.column,
            self.reference.zone_label,
            self.reference.source,
        )

    @property
    def help_text(self) -> str:
        if self.column is None:
            return (
                "Time filter: no timestamp/time column was detected, so this "
                "row is visible but disabled."
            )
        if self.reference is None:
            value_help = (
                "Enter elapsed time as seconds or HH:MM:SS; hours may exceed 24."
            )
        else:
            value_help = (
                "Enter wall time as YYYY-MM-DD HH:MM[:SS[.fff]] using the "
                "displayed instrument time."
            )
        return (
            "Time is the fixed first row for %s. %s Blank Min means <= Max; "
            "blank Max means >= Min; both blank mean no filtering. Bounds are "
            "inclusive. On first use, Min defaults to the first data record "
            "and Max is blank. %s"
            % (
                self.column,
                value_help,
                (
                    "The wall-time mapping is inferred from %s in %s and is "
                    "not verified instrument time."
                    % (self.reference.source, self.reference.zone_label)
                    if self.reference is not None
                    else ""
                ),
            )
        )


def _normalized_name(name: str) -> str:
    without_duplicate_suffix = re.sub(r"\s+#\d+$", "", name.strip())
    return re.sub(r"[^A-Za-z0-9]", "", without_duplicate_suffix).casefold()


def is_time_filter_column(name: str | None) -> bool:
    """Recognize time columns for the filter row without changing axis rules."""

    if not name:
        return False
    normalized = _normalized_name(name)
    return bool(
        _TIME_COLUMN_PATTERN.fullmatch(normalized)
        or normalized in {"elapsed", "elapsedtime", "elapsedseconds"}
    )


def discover_time_column(columns: Sequence[str]) -> str | None:
    """Return the first named timestamp/time column, if one is present."""

    return next((name for name in columns if is_time_filter_column(name)), None)


def _finite_sample(document: Any, column: str) -> float | None:
    column_index = document.columns.index(column)
    for row in document.rows:
        try:
            value = float(row[column_index])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _first_record_value(document: Any, column: str) -> float | None:
    """Return the numeric value from the first parsed data record only."""

    if not document.rows:
        return None
    column_index = document.columns.index(column)
    try:
        value = float(document.rows[0][column_index])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def time_filter_context(document: Any) -> TimeFilterContext:
    """Build a time context without guessing an epoch from small values."""

    column = discover_time_column(document.columns)
    if column is None:
        return TimeFilterContext(None)
    initial_minimum = _first_record_value(document, column)
    reference = None
    if _normalized_name(column).startswith("timestamp"):
        reference = timestamp_reference(
            document.header_lines,
            "Timestamp(s)",
            _finite_sample(document, column),
        )
    return TimeFilterContext(column, reference, initial_minimum)


def _parse_wall_time(text: str, context: TimeFilterContext) -> float:
    try:
        value = datetime.fromisoformat(text.replace("T", " "))
    except ValueError as exc:
        raise ValueError(
            "Enter wall time as YYYY-MM-DD HH:MM[:SS[.fff]]"
        ) from exc
    if value.tzinfo is not None:
        raise ValueError(
            "Wall time bounds must omit a timezone and use the displayed time"
        )
    reference = context.reference
    if reference is None:
        raise ValueError("A wall-time reference is not available")
    return reference.raw_origin + (value - reference.wall_origin).total_seconds()


def _parse_duration(text: str) -> float:
    try:
        direct = float(text)
    except ValueError:
        direct = None
    if direct is not None:
        if not math.isfinite(direct):
            raise ValueError("Enter a finite elapsed duration")
        return direct

    match = _DURATION_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(
            "Enter elapsed time as seconds or HH:MM:SS; hours may exceed 24"
        )
    days = float(match.group("days") or 0.0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = float(match.group("seconds"))
    if minutes >= 60 or seconds >= 60 or not math.isfinite(seconds):
        raise ValueError("Elapsed minutes and seconds must be below 60")
    value = days * 86_400.0 + hours * 3_600.0 + minutes * 60.0 + seconds
    if match.group("sign") == "-":
        value = -value
    if not math.isfinite(value):
        raise ValueError("Enter a finite elapsed duration")
    return value


def parse_time_bound(text: str, context: TimeFilterContext) -> float | None:
    """Parse one blank-or-time bound into the raw numeric column value."""

    stripped = text.strip()
    if not stripped:
        return None
    if context.reference is not None:
        return _parse_wall_time(stripped, context)
    return _parse_duration(stripped)


def format_time_bound(value: float, context: TimeFilterContext) -> str:
    """Format one raw bound as wall time or an unbounded elapsed duration."""

    if context.reference is not None:
        wall = context.reference.datetime_at(value)
        text = wall.strftime("%Y-%m-%d %H:%M:%S")
        milliseconds = wall.microsecond // 1_000
        return text + ".%03d" % milliseconds

    sign = "-" if value < 0 else ""
    total_milliseconds = int(round(abs(value) * 1_000.0))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    result = "%s%02d:%02d:%02d" % (sign, hours, minutes, seconds)
    return result + (".%03d" % milliseconds if milliseconds else "")
