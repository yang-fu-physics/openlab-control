"""Data Browser 的可移植 ``.plt`` 绘图格式。

PLT 只保存所选 DAT、列、叠加/分图布局、线性/对数坐标与当前缩放范围，不绑定正在测量的
DAT。读取时严格验证版本、唯一列和有限递增范围；保存采用同目录原子替换。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PLOT_FORMAT_MARKER = "OpenLab Control Plot Format"
PLOT_FORMAT_VERSION = 2
SUPPORTED_PLOT_FORMAT_VERSIONS = {1, PLOT_FORMAT_VERSION}
PLOT_LAYOUTS = {"overlay", "stacked"}
LINEAR_SCALE = "linear"
LOG_SCALE = "log"
PLOT_SCALES = {LINEAR_SCALE, LOG_SCALE}


class PlotFormatError(ValueError):
    """PLT 内容无效、不兼容或无法安全读写。"""


Range = tuple[float, float]


def _validated_range(value: Any, field_name: str) -> Range | None:
    """解析一个可选的有限递增轴范围。"""

    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PlotFormatError(f"{field_name} must contain exactly two numbers")
    try:
        low, high = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise PlotFormatError(f"{field_name} must contain numbers") from exc
    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        raise PlotFormatError(f"{field_name} must be a finite increasing range")
    return low, high


@dataclass(frozen=True, slots=True)
class PlotFormat:
    """一份经过验证、可序列化的 Data Browser 绘图状态。"""

    data_file: str
    layout: str
    x_column: str | None
    y_columns: tuple[str, ...]
    x_range: Range | None = None
    overlay_y_range: Range | None = None
    stacked_y_ranges: dict[str, Range] = field(default_factory=dict)
    x_scale: str = LINEAR_SCALE
    y_scale: str = LINEAR_SCALE

    def __post_init__(self) -> None:
        """在对象创建时统一执行布局、坐标类型和范围约束。"""

        if self.layout not in PLOT_LAYOUTS:
            raise PlotFormatError(f"Unknown plot layout: {self.layout}")
        if not self.y_columns:
            raise PlotFormatError("At least one Y column is required")
        if len(self.y_columns) != len(set(self.y_columns)):
            raise PlotFormatError("Y columns must be unique")
        if self.x_scale not in PLOT_SCALES:
            raise PlotFormatError(f"Unknown X scale: {self.x_scale}")
        if self.y_scale not in PLOT_SCALES:
            raise PlotFormatError(f"Unknown Y scale: {self.y_scale}")
        _validated_range(self.x_range, "x_range")
        _validated_range(self.overlay_y_range, "overlay_y_range")
        for name, value in self.stacked_y_ranges.items():
            _validated_range(value, f"stacked_y_ranges.{name}")
        if self.x_scale == LOG_SCALE and self.x_range is not None and self.x_range[0] <= 0:
            raise PlotFormatError("x_range must be positive for logarithmic X scale")
        if self.y_scale == LOG_SCALE:
            if self.overlay_y_range is not None and self.overlay_y_range[0] <= 0:
                raise PlotFormatError(
                    "overlay_y_range must be positive for logarithmic Y scale"
                )
            if any(value[0] <= 0 for value in self.stacked_y_ranges.values()):
                raise PlotFormatError(
                    "stacked_y_ranges must be positive for logarithmic Y scale"
                )

    def to_dict(self) -> dict[str, Any]:
        """转换为包含格式标记和版本号的 JSON 对象。"""

        return {
            "format": PLOT_FORMAT_MARKER,
            "version": PLOT_FORMAT_VERSION,
            "data_file": self.data_file,
            "layout": self.layout,
            "x_axis": self.x_column,
            "y_axes": list(self.y_columns),
            "x_scale": self.x_scale,
            "y_scale": self.y_scale,
            "zoom": {
                "x_range": list(self.x_range) if self.x_range is not None else None,
                "overlay_y_range": (
                    list(self.overlay_y_range) if self.overlay_y_range is not None else None
                ),
                "stacked_y_ranges": {
                    name: list(value) for name, value in self.stacked_y_ranges.items()
                },
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlotFormat:
        """从受支持版本的 JSON 对象构造并再次验证格式。"""

        if raw.get("format") != PLOT_FORMAT_MARKER:
            raise PlotFormatError("Not an OpenLab Control PLT file")
        version = raw.get("version")
        if version not in SUPPORTED_PLOT_FORMAT_VERSIONS:
            raise PlotFormatError(f"Unsupported PLT version: {raw.get('version')}")
        layout = str(raw.get("layout", "overlay")).lower()
        x_raw = raw.get("x_axis")
        x_column = None if x_raw is None else str(x_raw)
        y_raw = raw.get("y_axes")
        if not isinstance(y_raw, list):
            raise PlotFormatError("y_axes must be a list")
        y_columns = tuple(str(value) for value in y_raw if str(value))
        zoom = raw.get("zoom", {})
        if not isinstance(zoom, dict):
            raise PlotFormatError("zoom must be an object")
        stacked_raw = zoom.get("stacked_y_ranges", {})
        if not isinstance(stacked_raw, dict):
            raise PlotFormatError("stacked_y_ranges must be an object")
        stacked = {
            str(name): value
            for name, raw_range in stacked_raw.items()
            if (value := _validated_range(raw_range, f"stacked_y_ranges.{name}")) is not None
        }
        return cls(
            data_file=str(raw.get("data_file", "")),
            layout=layout,
            x_column=x_column,
            y_columns=y_columns,
            x_range=_validated_range(zoom.get("x_range"), "x_range"),
            overlay_y_range=_validated_range(
                zoom.get("overlay_y_range"), "overlay_y_range"
            ),
            stacked_y_ranges=stacked,
            x_scale=str(raw.get("x_scale", LINEAR_SCALE)).casefold(),
            y_scale=str(raw.get("y_scale", LINEAR_SCALE)).casefold(),
        )


def plot_format_path(data_path: str | Path) -> Path:
    """返回规范同名路径，例如 ``sample.dat`` 对应 ``sample.plt``。"""

    return Path(data_path).resolve().with_suffix(".plt")


def find_plot_format(data_path: str | Path) -> Path | None:
    """查找规范路径，并兼容旧版 ``sample.dat.plt`` 命名。"""

    source = Path(data_path).resolve()
    canonical = plot_format_path(source)
    if canonical.exists():
        return canonical
    additive = Path(str(source) + ".plt")
    return additive if additive.exists() else None


def save_plot_format(data_path: str | Path, plot_format: PlotFormat) -> Path:
    """以 UTF-8 JSON 原子保存 DAT 对应的 PLT。"""

    destination = plot_format_path(data_path)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    payload = json.dumps(plot_format.to_dict(), ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        temporary.replace(destination)
    except OSError as exc:
        raise PlotFormatError(f"Unable to save PLT file: {destination}") from exc
    return destination


def load_plot_format(path: str | Path) -> PlotFormat:
    """读取、检查根对象并验证 PLT 内容。"""

    source = Path(path).resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlotFormatError(f"Unable to read PLT file: {source}") from exc
    if not isinstance(raw, dict):
        raise PlotFormatError("PLT root must be an object")
    return PlotFormat.from_dict(raw)
