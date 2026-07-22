from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from pathlib import Path


class DatReadError(ValueError):
    """Raised when a file does not contain a readable OpenLab-style DAT section."""


@dataclass(frozen=True, slots=True)
class DatPoint:
    """One plottable point linked back to its complete DAT data row."""

    x: float
    y: float
    row_index: int
    row: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatDocument:
    path: Path
    header_lines: tuple[str, ...]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    modified_ns: int
    size_bytes: int

    def column_index(self, name: str) -> int:
        try:
            return self.columns.index(name)
        except ValueError as exc:
            raise KeyError(name) from exc

    def numeric_columns(self) -> tuple[str, ...]:
        result: list[str] = []
        for index, name in enumerate(self.columns):
            if any(_as_float(row[index]) is not None for row in self.rows):
                result.append(name)
        return tuple(result)

    def numeric_series(
        self,
        y_column: str,
        x_column: str | None = None,
    ) -> tuple[tuple[float, float], ...]:
        return tuple((point.x, point.y) for point in self.numeric_points(y_column, x_column))

    def numeric_points(
        self,
        y_column: str,
        x_column: str | None = None,
    ) -> tuple[DatPoint, ...]:
        y_index = self.column_index(y_column)
        x_index = None if x_column is None else self.column_index(x_column)
        result: list[DatPoint] = []
        for row_index, row in enumerate(self.rows):
            y_value = _as_float(row[y_index])
            if y_value is None:
                continue
            x_value = float(row_index + 1) if x_index is None else _as_float(row[x_index])
            if x_value is not None:
                result.append(DatPoint(x_value, y_value, row_index, row))
        return tuple(result)


def _as_float(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _unique_columns(values: list[str]) -> list[str]:
    result: list[str] = []
    counts: dict[str, int] = {}
    for index, raw in enumerate(values, start=1):
        base = raw.strip() or f"Column {index}"
        counts[base] = counts.get(base, 0) + 1
        result.append(base if counts[base] == 1 else f"{base} #{counts[base]}")
    return result


def read_dat(path: str | Path) -> DatDocument:
    source = Path(path).resolve()
    try:
        payload = source.read_bytes()
        stat = source.stat()
    except OSError as exc:
        raise DatReadError(f"Unable to read DAT file: {source}") from exc

    lines = _decode(payload).replace("\x00", "").splitlines()
    marker = next(
        (index for index, line in enumerate(lines) if line.strip().casefold() == "[data]"),
        None,
    )
    if marker is None:
        raise DatReadError("The file does not contain a [Data] section")

    header_lines = tuple(lines[:marker])
    parsed = csv.reader(io.StringIO("\n".join(lines[marker + 1 :])))
    records = [
        [cell.strip() for cell in record]
        for record in parsed
        if any(cell.strip() for cell in record)
        and not (record and record[0].lstrip().startswith(";"))
    ]
    if not records:
        raise DatReadError("The [Data] section does not contain a column header")

    columns = _unique_columns(records[0])
    body = records[1:]
    widest = max([len(columns), *(len(row) for row in body)], default=len(columns))
    columns.extend(f"Extra {index}" for index in range(len(columns) + 1, widest + 1))
    rows = tuple(
        tuple((row + [""] * (widest - len(row)))[:widest])
        for row in body
    )
    return DatDocument(
        path=source,
        header_lines=header_lines,
        columns=tuple(columns),
        rows=rows,
        modified_ns=stat.st_mtime_ns,
        # Use the number of bytes actually parsed. If the writer appended while
        # this read was in progress, the monitor sees the size mismatch and
        # performs another refresh instead of silently missing the new tail.
        size_bytes=len(payload),
    )
