"""独立读取 OpenLab 风格 DAT，供 Data Browser 与绘图使用。

读取器不依赖当前运行或当前测量文件，可打开任意 DAT。它兼容 UTF-8、UTF-16 和 GB18030，
自动补齐不等长旧数据行并为重复列名生成唯一显示名；只有有限数值才进入绘图序列。
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path


class DatReadError(ValueError):
    """文件不可读或没有有效 OpenLab ``[Data]`` 区段。"""


@dataclass(frozen=True, slots=True)
class DatPoint:
    """一个可绘制点，并保留其完整 DAT 行以供点击查看。"""

    x: float
    y: float
    row_index: int
    row: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatDocument:
    """一次文件读取所得的不可变列、行和文件版本快照。"""

    path: Path
    header_lines: tuple[str, ...]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    modified_ns: int
    size_bytes: int
    _numeric_cache: dict[int, tuple[float | None, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    _numeric_columns: tuple[str, ...] | None = field(
        default=None, init=False, repr=False, compare=False,
    )

    def column_index(self, name: str) -> int:
        """按唯一显示名取得列索引。"""

        try:
            return self.columns.index(name)
        except ValueError as exc:
            raise KeyError(name) from exc

    def numeric_columns(self) -> tuple[str, ...]:
        """返回至少含一个有限数值的列。"""

        if self._numeric_columns is None:
            result = tuple(
                name for name in self.columns
                if any(value is not None for value in self.numeric_values(name))
            )
            object.__setattr__(self, "_numeric_columns", result)
        return self._numeric_columns

    def numeric_values(self, name: str) -> tuple[float | None, ...]:
        """缓存每列的有限数值，供筛选、自动量程和绘图复用。"""

        index = self.column_index(name)
        if index not in self._numeric_cache:
            self._numeric_cache[index] = tuple(_as_float(row[index]) for row in self.rows)
        return self._numeric_cache[index]

    def numeric_series(
        self,
        y_column: str,
        x_column: str | None = None,
    ) -> tuple[tuple[float, float], ...]:
        """返回绘图使用的简化 ``(x, y)`` 序列。"""

        return tuple((point.x, point.y) for point in self.numeric_points(y_column, x_column))

    def numeric_points(
        self,
        y_column: str,
        x_column: str | None = None,
    ) -> tuple[DatPoint, ...]:
        """过滤空白、文本和非有限值，同时保留原始行关联。"""

        return self.numeric_points_by_series((y_column,), x_column)[y_column]

    def numeric_points_by_series(
        self,
        y_columns: Sequence[str],
        x_column: str | None = None,
        row_indices: Iterable[int] | None = None,
    ) -> dict[str, tuple[DatPoint, ...]]:
        """共享 X 与所选原始行，一次遍历构建多条曲线。"""

        x_values = None if x_column is None else self.numeric_values(x_column)
        series = {name: self.numeric_values(name) for name in y_columns}
        result: dict[str, list[DatPoint]] = {name: [] for name in series}
        for index in range(len(self.rows)) if row_indices is None else row_indices:
            x = float(index + 1) if x_values is None else x_values[index]
            if x is None:
                continue
            for name, values in series.items():
                y = values[index]
                if y is not None:
                    result[name].append(DatPoint(x, y, index, self.rows[index]))
        return {name: tuple(points) for name, points in result.items()}


def _as_float(value: str) -> float | None:
    """把有限数值文本转为 float，空值、NaN 和无穷返回 ``None``。"""

    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def _decode(data: bytes) -> str:
    """按现代格式到中文旧格式依次尝试解码。"""

    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _unique_columns(values: list[str]) -> list[str]:
    """为缺失或重复列名生成稳定且唯一的显示名称。"""

    result: list[str] = []
    counts: dict[str, int] = {}
    for index, raw in enumerate(values, start=1):
        base = raw.strip() or f"Column {index}"
        counts[base] = counts.get(base, 0) + 1
        result.append(base if counts[base] == 1 else f"{base} #{counts[base]}")
    return result


def read_raw_dat(path: str | Path) -> str:
    """Read the source text with the same decoder, without parsing or writing it."""

    return _decode(Path(path).read_bytes())


def read_dat(
    path: str | Path, *, allow_unterminated_last_line: bool = True,
) -> DatDocument:
    """读取整个 DAT 文件并返回与本次字节快照一致的文档。"""

    source = Path(path).resolve()
    try:
        payload = source.read_bytes()
        stat = source.stat()
    except OSError as exc:
        raise DatReadError(f"Unable to read DAT file: {source}") from exc

    text = _decode(payload).replace("\x00", "")
    if not allow_unterminated_last_line and not text.endswith(("\n", "\r")):
        raise DatReadError("The final DAT line is incomplete")
    lines = text.splitlines()
    marker = next(
        (index for index, line in enumerate(lines) if line.strip().casefold() == "[data]"),
        None,
    )
    if marker is None:
        raise DatReadError("The file does not contain a [Data] section")

    header_lines = tuple(lines[:marker])
    parsed = csv.reader(
        io.StringIO("\n".join(lines[marker + 1 :])),
        strict=not allow_unterminated_last_line,
    )
    try:
        records = [
            [cell.strip() for cell in record]
            for record in parsed
            if any(cell.strip() for cell in record)
            and not (record and record[0].lstrip().startswith(";"))
        ]
    except csv.Error as exc:
        raise DatReadError(f"Incomplete or invalid DAT record: {exc}") from exc
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
        # 记录实际解析的字节数，而不是稍后再次 stat 的值。若写入器恰在读取期间追加，自动
        # 刷新监视器会发现大小不一致并再读一次，不会悄悄漏掉新增尾部。
        size_bytes=len(payload),
    )
