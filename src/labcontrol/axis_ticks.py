"""Data Browser 的整齐刻度与绝对时间轴转换。

外部 DAT 的绝对秒值并不只有一种 epoch。OpenLab 文件明确标记 Unix 或 LabVIEW 1904；
Quantum Design 文件则用 ``FILEOPENTIME`` 同时给出原始秒值和仪表本地日期时间。本模块
优先采用文件自带校准，只有缺少元数据时才使用保守的数值范围推断。

线性主刻度采用 1、2、5 × 10ⁿ，时间主刻度采用秒、分钟、小时和天的常用整步长。绘图
范围与原始数据保持不变，仅选择落在范围内的整齐网格线，因而不会改变框选缩放或点命中。
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .datafile import LABVIEW_UNIX_OFFSET_SECONDS


_TIMESTAMP_COLUMN = re.compile(
    r"^timestamp(?:s|sec|secs|second|seconds)?$",
    re.IGNORECASE,
)
_TIME_STEPS_SECONDS = (
    0.001,
    0.002,
    0.005,
    0.01,
    0.02,
    0.05,
    0.1,
    0.2,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
    15.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    900.0,
    1_800.0,
    3_600.0,
    7_200.0,
    10_800.0,
    21_600.0,
    43_200.0,
    86_400.0,
    172_800.0,
    432_000.0,
    604_800.0,
    1_209_600.0,
    2_592_000.0,
    7_776_000.0,
    15_552_000.0,
    31_536_000.0,
)


@dataclass(frozen=True, slots=True)
class AxisTicks:
    """一组主刻度值及其标准步长。"""

    values: tuple[float, ...]
    step: float | None


@dataclass(frozen=True, slots=True)
class TimestampReference:
    """把文件中的原始秒值映射到文件记录的墙上时间。

    ``wall_origin`` 故意保存为无时区 datetime。Quantum Design 头部只声明仪表当地
    日期时间，没有可验证的地理时区；强行转换到运行电脑时区反而会改变实验记录。
    """

    raw_origin: float
    wall_origin: datetime
    zone_label: str
    source: str

    def datetime_at(self, raw_value: float) -> datetime:
        """按与原始秒值的差量返回实际记录时间。"""

        return self.wall_origin + timedelta(
            seconds=raw_value - self.raw_origin
        )


def is_timestamp_column(name: str | None) -> bool:
    """识别 ``Timestamp(s)``、``Time Stamp (sec)`` 等绝对时间列。"""

    if not name:
        return False
    # 重复列会由 DAT reader 添加 ``#2``，识别时先移除该显示后缀。
    without_duplicate_suffix = re.sub(
        r"\s+#\d+$",
        "",
        name.strip(),
    )
    normalized = re.sub(
        r"[^A-Za-z0-9]",
        "",
        without_duplicate_suffix,
    )
    return _TIMESTAMP_COLUMN.fullmatch(normalized) is not None


def _header_fields(line: str) -> tuple[str, ...]:
    """按 CSV 规则拆一行头部；畸形行安静退化为空记录。"""

    try:
        return tuple(
            field.strip()
            for field in next(csv.reader([line]))
        )
    except (csv.Error, StopIteration):
        return ()


def _parse_instrument_datetime(
    date_text: str,
    time_text: str,
) -> datetime | None:
    """兼容 Quantum Design 常见的 12/24 小时日期时间文本。"""

    combined = f"{date_text.strip()} {time_text.strip()}"
    for pattern in (
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(combined, pattern)
        except ValueError:
            continue
    return None


def _format_utc_offset(value: datetime) -> str:
    """把 ISO 时间中的偏移压缩为 ``UTC+08:00``；未知时称为本地时间。"""

    offset = value.utcoffset()
    if offset is None:
        return "local time"
    seconds = int(offset.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3_600)
    minutes = remainder // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def timestamp_reference(
    header_lines: Iterable[str],
    column_name: str | None,
    sample_value: float | None,
) -> TimestampReference | None:
    """从 DAT 头部和一个样本值确定绝对时间映射。

    优先级：

    1. ``FILEOPENTIME`` 的原始值与仪表时间成对校准；
    2. OpenLab ``Started`` + 明确的 ``TIMESTAMP_EPOCH``/注释；
    3. ``Started`` 与样本差值对 Unix/1904 的近似匹配；
    4. 最后才按现代 Unix 或 LabVIEW 数值范围推断。
    """

    if (
        not is_timestamp_column(column_name)
        or sample_value is None
        or not math.isfinite(sample_value)
    ):
        return None

    lines = tuple(header_lines)
    records = [
        fields
        for line in lines
        if (fields := _header_fields(line))
    ]
    for fields in records:
        if (
            len(fields) >= 4
            and fields[0].casefold() == "fileopentime"
        ):
            try:
                raw_origin = float(fields[1])
            except ValueError:
                continue
            wall_origin = _parse_instrument_datetime(
                fields[2],
                fields[3],
            )
            if wall_origin is not None:
                return TimestampReference(
                    raw_origin=raw_origin,
                    wall_origin=wall_origin,
                    zone_label="instrument time",
                    source="FILEOPENTIME",
                )

    started: datetime | None = None
    epoch: str | None = None
    for fields in records:
        if (
            len(fields) >= 2
            and fields[0].casefold() == "timestamp_epoch"
        ):
            candidate = fields[1].strip().casefold()
            if candidate in {"labview_1904", "unix"}:
                epoch = candidate
        for field in fields:
            if field.casefold().startswith("started:"):
                text = field.split(":", 1)[1].strip()
                try:
                    started = datetime.fromisoformat(
                        text.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
    combined_header = "\n".join(lines).casefold()
    if epoch is None and "labview 1904" in combined_header:
        epoch = "labview_1904"
    if (
        epoch is None
        and "timestamp" in combined_header
        and "unix" in combined_header
    ):
        epoch = "unix"

    if started is not None:
        zone_label = _format_utc_offset(started)
        wall_origin = started.replace(tzinfo=None)
        if started.tzinfo is not None:
            started_unix = started.timestamp()
            difference = sample_value - started_unix
            # 0.11.1 及更早版本的 DAT 注释曾固定写成 LabVIEW 1904，即使配置
            # 实际选择 Unix。用 Started 与首个样本交叉校验，兼容这批旧文件。
            plausible_run_span = 366 * 86_400
            if (
                epoch == "labview_1904"
                and abs(
                    difference
                    - LABVIEW_UNIX_OFFSET_SECONDS
                )
                > plausible_run_span
                and abs(difference) <= plausible_run_span
            ):
                epoch = "unix"
            elif (
                epoch == "unix"
                and abs(difference) > plausible_run_span
                and abs(
                    difference
                    - LABVIEW_UNIX_OFFSET_SECONDS
                )
                <= plausible_run_span
            ):
                epoch = "labview_1904"
            if epoch is None:
                if abs(
                    difference - LABVIEW_UNIX_OFFSET_SECONDS
                ) <= 86_400:
                    epoch = "labview_1904"
                elif abs(difference) <= 86_400:
                    epoch = "unix"
            if epoch == "labview_1904":
                return TimestampReference(
                    raw_origin=(
                        started_unix
                        + LABVIEW_UNIX_OFFSET_SECONDS
                    ),
                    wall_origin=wall_origin,
                    zone_label=zone_label,
                    source="OpenLab Started/labview_1904",
                )
            if epoch == "unix":
                return TimestampReference(
                    raw_origin=started_unix,
                    wall_origin=wall_origin,
                    zone_label=zone_label,
                    source="OpenLab Started/unix",
                )

    # 没有可靠头部时只接受现代实验常见范围。这个分支不覆盖带 FILEOPENTIME 的
    # Quantum Design 文件，避免把其厂商 epoch 误当成 LabVIEW。
    if 3_000_000_000 <= sample_value <= 5_000_000_000:
        unix_value = (
            sample_value
            - LABVIEW_UNIX_OFFSET_SECONDS
        )
        local = datetime.fromtimestamp(
            unix_value
        ).astimezone()
        return TimestampReference(
            raw_origin=sample_value,
            wall_origin=local.replace(tzinfo=None),
            zone_label=_format_utc_offset(local),
            source="numeric LabVIEW inference",
        )
    if 946_684_800 <= sample_value <= 2_999_999_999:
        local = datetime.fromtimestamp(
            sample_value
        ).astimezone()
        return TimestampReference(
            raw_origin=sample_value,
            wall_origin=local.replace(tzinfo=None),
            zone_label=_format_utc_offset(local),
            source="numeric Unix inference",
        )
    return None


def _nice_step(raw_step: float) -> float:
    """把任意正步长舍入到最近的 1、2、5 × 10ⁿ。"""

    if not math.isfinite(raw_step) or raw_step <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw_step))
    fraction = raw_step / magnitude
    if fraction < 1.5:
        nice_fraction = 1.0
    elif fraction < 3.0:
        nice_fraction = 2.0
    elif fraction < 7.0:
        nice_fraction = 5.0
    else:
        nice_fraction = 10.0
    return nice_fraction * magnitude


def _ticks_for_step(
    low: float,
    high: float,
    step: float,
) -> tuple[float, ...]:
    """生成范围内、相对于零点对齐的有限刻度。"""

    if (
        not all(math.isfinite(value) for value in (low, high, step))
        or high <= low
        or step <= 0
    ):
        return ()
    tolerance = abs(step) * 1e-9
    first_index = math.ceil(
        (low - tolerance) / step
    )
    last_index = math.floor(
        (high + tolerance) / step
    )
    if last_index < first_index:
        return ()
    count = min(last_index - first_index + 1, 1_000)
    values: list[float] = []
    digits = max(
        0,
        min(15, -math.floor(math.log10(step)) + 2),
    )
    for index in range(count):
        value = (first_index + index) * step
        if abs(value) <= tolerance:
            value = 0.0
        values.append(round(value, digits))
    return tuple(values)


def linear_ticks(
    low: float,
    high: float,
    target_intervals: int = 6,
) -> AxisTicks:
    """返回约 4–8 条、步长属于 1/2/5 系列的线性主刻度。"""

    if (
        not math.isfinite(low)
        or not math.isfinite(high)
        or high <= low
    ):
        return AxisTicks((), None)
    step = _nice_step(
        (high - low) / max(2, target_intervals)
    )
    values = _ticks_for_step(low, high, step)
    # 极窄边界刚好只含一个整数倍时，细化一次以免只画一条网格线。
    if len(values) < 2:
        step = _nice_step(step / 2)
        values = _ticks_for_step(low, high, step)
    return AxisTicks(values, step)


def logarithmic_ticks(
    low: float,
    high: float,
) -> AxisTicks:
    """用 1/2/5 × 10ⁿ 生成对数主刻度，跨度大时只保留 10ⁿ。"""

    if (
        not math.isfinite(low)
        or not math.isfinite(high)
        or low <= 0
        or high <= low
    ):
        return AxisTicks((), None)
    minimum_power = math.floor(math.log10(low)) - 1
    maximum_power = math.ceil(math.log10(high)) + 1
    candidates = tuple(
        multiplier * 10**power
        for power in range(
            minimum_power,
            maximum_power + 1,
        )
        for multiplier in (1.0, 2.0, 5.0)
        if low <= multiplier * 10**power <= high
    )
    if len(candidates) <= 10:
        return AxisTicks(candidates, None)
    powers = tuple(
        10.0**power
        for power in range(
            math.ceil(math.log10(low)),
            math.floor(math.log10(high)) + 1,
        )
    )
    if len(powers) <= 10:
        return AxisTicks(powers, None)
    stride = math.ceil(len(powers) / 8)
    return AxisTicks(powers[::stride], None)


def _wall_microseconds(value: datetime) -> int:
    """用整数微秒表示墙上时间，避免大 epoch 对齐时落到整秒前几微秒。"""

    return (
        (
            value.toordinal() * 86_400
            + value.hour * 3_600
            + value.minute * 60
            + value.second
        )
        * 1_000_000
        + value.microsecond
    )


def timestamp_ticks(
    low: float,
    high: float,
    reference: TimestampReference,
    target_intervals: int = 6,
) -> AxisTicks:
    """生成与仪表整秒、整分钟、整小时或整天对齐的时间刻度。"""

    if (
        not math.isfinite(low)
        or not math.isfinite(high)
        or high <= low
    ):
        return AxisTicks((), None)
    desired = (
        (high - low)
        / max(2, target_intervals)
    )
    step = next(
        (
            candidate
            for candidate in _TIME_STEPS_SECONDS
            if candidate >= desired
        ),
        _nice_step(desired),
    )
    step_microseconds = max(
        1,
        round(step * 1_000_000),
    )
    origin_microseconds = _wall_microseconds(
        reference.wall_origin
    )
    low_microseconds = _wall_microseconds(
        reference.datetime_at(low)
    )
    high_microseconds = _wall_microseconds(
        reference.datetime_at(high)
    )
    first = (
        (
            low_microseconds
            + step_microseconds
            - 1
        )
        // step_microseconds
        * step_microseconds
    )
    values: list[float] = []
    tick = first
    while tick <= high_microseconds and len(values) < 1_000:
        values.append(
            reference.raw_origin
            + (
                tick - origin_microseconds
            )
            / 1_000_000
        )
        tick += step_microseconds
    return AxisTicks(tuple(values), step)


def numeric_tick_label(
    value: float,
    step: float | None,
) -> str:
    """按主步长决定小数位，避免 ``200.333`` 和无意义尾零。"""

    if not math.isfinite(value):
        return ""
    if value == 0:
        return "0"
    absolute = abs(value)
    if absolute >= 10_000_000 or absolute < 0.0001:
        return f"{value:.4g}"
    if step is None:
        decimals = (
            0
            if absolute >= 1
            else max(
                0,
                min(
                    9,
                    math.ceil(
                        -math.log10(absolute)
                    ),
                ),
            )
        )
    else:
        decimals = (
            0
            if step >= 1
            else max(
                0,
                min(
                    9,
                    math.ceil(-math.log10(step)),
                ),
            )
        )
    text = f"{value:,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def timestamp_tick_label(
    raw_value: float,
    reference: TimestampReference,
    axis_low: float,
    axis_high: float,
    step_seconds: float | None,
) -> str:
    """按显示跨度选择紧凑但真实的日期时间标签。"""

    value = reference.datetime_at(raw_value)
    start = reference.datetime_at(axis_low)
    end = reference.datetime_at(axis_high)
    span_seconds = axis_high - axis_low
    same_date = (
        start.date()
        == end.date()
        == value.date()
    )
    step = step_seconds or 0
    if step < 1:
        decimals = max(
            1,
            min(
                3,
                math.ceil(-math.log10(max(step, 0.001))),
            ),
        )
        base = value.strftime(
            "%H:%M:%S"
            if same_date
            else "%m-%d %H:%M:%S"
        )
        fraction = f"{value.microsecond / 1_000_000:.{decimals}f}"[1:]
        return base + fraction
    if same_date and span_seconds < 86_400:
        return value.strftime(
            "%H:%M:%S"
            if step < 60
            else "%H:%M"
        )
    if start.year == value.year:
        return value.strftime(
            "%m-%d %H:%M:%S"
            if step < 60
            else "%m-%d %H:%M"
        )
    return value.strftime("%Y-%m-%d")


def full_timestamp_label(
    raw_value: float,
    reference: TimestampReference,
) -> str:
    """为点详情生成含毫秒和时间来源的完整实际时间。"""

    value = reference.datetime_at(raw_value)
    milliseconds = value.microsecond // 1_000
    return (
        value.strftime("%Y-%m-%d %H:%M:%S")
        + f".{milliseconds:03d}"
        + f" ({reference.zone_label})"
    )


def timestamp_axis_title(
    column_name: str,
    low: float,
    high: float,
    reference: TimestampReference,
) -> str:
    """在轴标题中补充日期和时间基准，避免只看到时分而误认日期。"""

    first = reference.datetime_at(low)
    last = reference.datetime_at(high)
    if first.date() == last.date():
        context = (
            f"{first:%Y-%m-%d}, "
            f"{reference.zone_label}"
        )
    elif first.year == last.year:
        context = (
            f"{first.year}, "
            f"{reference.zone_label}"
        )
    else:
        context = (
            f"{first.year}–{last.year}, "
            f"{reference.zone_label}"
        )
    return f"{column_name} — {context}"
