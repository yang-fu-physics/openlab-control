"""Qt-independent data-filter state and row-selection helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence


FILTER_ROW_COUNT = 5


def _finite_bound(value: Any, field_name: str) -> float | None:
    """Return one finite numeric bound or reject it."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("%s must be a finite number" % field_name)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a finite number" % field_name) from exc
    if not math.isfinite(number):
        raise ValueError("%s must be a finite number" % field_name)
    return number


@dataclass(frozen=True, slots=True)
class DataFilterRow:
    """One editable filter condition."""

    enabled: bool = False
    column: str | None = None
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("Filter enabled state must be boolean")
        if self.column is not None:
            if not isinstance(self.column, str) or not self.column:
                raise ValueError("Filter column must be a non-empty name or None")
        minimum = _finite_bound(self.minimum, "Filter minimum")
        maximum = _finite_bound(self.maximum, "Filter maximum")
        if self.column is None and (minimum is not None or maximum is not None):
            raise ValueError("Filter bounds require a selected data column")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("Filter minimum cannot be greater than maximum")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    @property
    def has_bounds(self) -> bool:
        """Whether this row contains at least one bound."""

        return self.minimum is not None or self.maximum is not None

    @property
    def is_noop(self) -> bool:
        """Whether this row changes the selected rows.

        ``enabled`` remains in the model only for reading old PLT files. New
        filters are determined by a selected column and at least one bound.
        """

        return self.column is None or not self.has_bounds

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation used by PLT files."""

        return {
            "enabled": self.enabled,
            "column": self.column,
            "min": self.minimum,
            "max": self.maximum,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "DataFilterRow":
        """Validate and construct one row from external JSON data."""

        if not isinstance(raw, dict):
            raise ValueError("Each filter row must be an object")
        if "enabled" in raw and not isinstance(raw["enabled"], bool):
            raise ValueError("Filter enabled state must be boolean")
        column = raw.get("column", raw.get("data"))
        if column is not None and not isinstance(column, str):
            raise ValueError("Filter column must be a string or null")
        minimum = raw.get("min", raw.get("minimum"))
        maximum = raw.get("max", raw.get("maximum"))
        return cls(
            enabled=raw.get("enabled", False),
            column=column,
            minimum=minimum,
            maximum=maximum,
        )


def _empty_rows() -> tuple[DataFilterRow, ...]:
    return tuple(DataFilterRow() for _ in range(FILTER_ROW_COUNT))


@dataclass(frozen=True, slots=True)
class DataFilter:
    """The five-row filter state owned by one data view."""

    rows: tuple[DataFilterRow, ...] = field(default_factory=_empty_rows)
    overflow_rows: tuple[DataFilterRow, ...] = ()

    def __post_init__(self) -> None:
        rows = tuple(self.rows)
        if len(rows) != FILTER_ROW_COUNT:
            raise ValueError(
                "A data filter must contain exactly %d rows" % FILTER_ROW_COUNT
            )
        if any(not isinstance(row, DataFilterRow) for row in rows):
            raise ValueError("Data filter rows must be DataFilterRow values")
        overflow_rows = tuple(self.overflow_rows)
        if any(not isinstance(row, DataFilterRow) for row in overflow_rows):
            raise ValueError("Overflow filter rows must be DataFilterRow values")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "overflow_rows", overflow_rows)

    @classmethod
    def empty(cls) -> "DataFilter":
        """Return a filter with all five rows disabled and blank."""

        return cls()

    @property
    def active_rows(self) -> tuple[DataFilterRow, ...]:
        """Return only rows that impose a bounded condition."""

        return tuple(
            row
            for row in self.rows + self.overflow_rows
            if not row.is_noop
        )

    @property
    def is_active(self) -> bool:
        return bool(self.active_rows)

    def to_list(self) -> list[dict[str, Any]]:
        """Return all five rows for PLT serialization."""

        return [row.to_dict() for row in self.rows]

    @classmethod
    def from_list(
        cls,
        raw: Any,
        overflow_raw: Any = None,
        *,
        legacy: bool = False,
    ) -> "DataFilter":
        """Validate and construct a filter from external JSON data."""

        if not isinstance(raw, list):
            raise ValueError("filters must be a list")
        if len(raw) != FILTER_ROW_COUNT:
            raise ValueError(
                "filters must contain exactly %d rows" % FILTER_ROW_COUNT
            )
        if overflow_raw is None:
            overflow_raw = []
        if not isinstance(overflow_raw, list):
            raise ValueError("filter_overflow must be a list")
        rows = tuple(DataFilterRow.from_dict(item) for item in raw)
        overflow_rows = tuple(
            DataFilterRow.from_dict(item) for item in overflow_raw
        )
        if legacy:
            rows = tuple(
                row if row.enabled else DataFilterRow()
                for row in rows
            )
            overflow_rows = tuple(
                row if row.enabled else DataFilterRow()
                for row in overflow_rows
            )
        return cls(
            rows,
            overflow_rows,
        )

    def reserve_time_column(self, time_column: str | None) -> "DataFilter":
        """Reserve row zero for time without discarding a legacy filter."""

        rows = list(self.rows)
        first = rows[0]
        if time_column is not None:
            if first.column == time_column:
                return self
            for index in range(1, FILTER_ROW_COUNT):
                if rows[index].column == time_column:
                    rows[0], rows[index] = rows[index], first
                    return DataFilter(tuple(rows), self.overflow_rows)
            for index, row in enumerate(self.overflow_rows):
                if row.column == time_column:
                    overflow = list(self.overflow_rows)
                    overflow.pop(index)
                    rows[0] = row
                    if first.column is not None:
                        overflow.insert(0, first)
                    return DataFilter(tuple(rows), tuple(overflow))
        if first.column is None:
            return self
        rows[0] = DataFilterRow()
        for index in range(1, FILTER_ROW_COUNT):
            if rows[index].column is None:
                rows[index] = first
                return DataFilter(tuple(rows), self.overflow_rows)
        return DataFilter(tuple(rows), (first,) + self.overflow_rows)

    def invalid_columns(
        self,
        columns: Sequence[str],
        numeric_columns: Iterable[str],
    ) -> tuple[str, ...]:
        """Return selected filter columns missing or no longer numeric."""

        available = set(columns)
        numeric = set(numeric_columns)
        return tuple(
            dict.fromkeys(
                row.column
                for row in self.rows + self.overflow_rows
                if row.column is not None
                if row.column not in available or row.column not in numeric
            )
        )

    def active_invalid_columns(
        self,
        columns: Sequence[str],
        numeric_columns: Iterable[str],
    ) -> tuple[str, ...]:
        """Return only invalid columns used by bounded filter conditions."""

        invalid = set(self.invalid_columns(columns, numeric_columns))
        return tuple(
            dict.fromkeys(
                row.column
                for row in self.active_rows
                if row.column is not None and row.column in invalid
            )
        )

    def disable_invalid_columns(
        self,
        columns: Sequence[str],
        numeric_columns: Iterable[str],
    ) -> tuple["DataFilter", tuple[str, ...]]:
        """Clear invalid selections and report invalid active conditions."""

        invalid = self.invalid_columns(columns, numeric_columns)
        if not invalid:
            return self, ()
        active_invalid = self.active_invalid_columns(columns, numeric_columns)
        invalid_set = set(invalid)
        return (
            DataFilter(
                tuple(
                    replace(
                        row,
                        enabled=False,
                        column=None,
                        minimum=None,
                        maximum=None,
                    )
                    if row.column in invalid_set
                    else row
                    for row in self.rows
                ),
                tuple(row for row in self.overflow_rows if row.column not in invalid_set),
            ),
            active_invalid,
        )


def matching_row_indices(document: Any, data_filter: DataFilter) -> tuple[int, ...]:
    """Return matching zero-based indices while leaving ``document`` untouched."""

    columns = document.columns
    rows = document.rows
    invalid = data_filter.active_invalid_columns(columns, document.numeric_columns())
    if invalid:
        raise ValueError(
            "Filter columns are missing or non-numeric: %s" % ", ".join(invalid)
        )
    conditions = tuple(
        (columns.index(row.column), row.minimum, row.maximum)
        for row in data_filter.active_rows
    )
    if not conditions:
        return tuple(range(len(rows)))

    condition_values = tuple(
        document.numeric_values(columns[column_index])
        for column_index, _, _ in conditions
    )

    matches = []
    for row_index, row in enumerate(rows):
        matches_row = True
        for values, (_, minimum, maximum) in zip(condition_values, conditions):
            value = values[row_index]
            if value is None:
                matches_row = False
                break
            if minimum is not None and value < minimum:
                matches_row = False
                break
            if maximum is not None and value > maximum:
                matches_row = False
                break
        if matches_row:
            matches.append(row_index)
    return tuple(matches)
