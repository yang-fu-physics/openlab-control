"""主窗口的轻量实时趋势监视器。

每条曲线保留最近 900 个快照并独立归一化，只用于快速观察变化；它不替代 DAT，也不把曲线
值反馈到控制逻辑。支持勾选显示/隐藏各曲线（含主读数、多控面板与数值型辅助指标）、清晰
展示每条曲线的名称/单位/当前值与量程，并支持在画布上点击查看精确时间戳与各曲线数值。
窗口由主窗口持有并复用，关闭后隐藏，避免反复重建信号和历史缓冲。
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
import math
import time

from PySide6.QtCore import QPointF, QRect, QRectF, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..formatting import fixed_number
from ..models import InstrumentKind, InstrumentSnapshot
from .scaling import scaled
from .window_sizing import fit_initial_window_width


@dataclass(slots=True)
class TrendSeriesMeta:
    """一条实时趋势曲线的稳定标识、单位、配色和默认显示状态。"""

    key: str
    label: str
    unit: str = ""
    decimals: int | None = None
    color: str = "#2d6cdf"
    default_visible: bool = True


@dataclass(frozen=True, slots=True)
class TrendInspectionEntry:
    """某次点击检视时刻单条可见曲线的精确读数。"""

    key: str
    label: str
    unit: str
    color: str
    timestamp: float
    value: float
    formatted_value: str
    is_primary: bool = False


@dataclass(frozen=True, slots=True)
class TrendInspection:
    """一次画布点击检视的锚点时间与各可见曲线数值快照。"""

    anchor_timestamp: float
    wall_time_text: str
    relative_seconds: float
    primary_key: str
    primary_point_px: tuple[float, float]
    entries: tuple[TrendInspectionEntry, ...]


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _format_trend_value(
    value: float,
    unit: str = "",
    decimals: int | None = None,
) -> str:
    number_text = (
        fixed_number(value, decimals)
        if decimals is not None
        else f"{value:.6g}"
    )
    return f"{number_text} {unit}".strip()


def _nearest_sample_index(
    points: list[tuple[float, float]],
    target_timestamp: float,
) -> int:
    """在按单调时间升序排列的点序列中二分查找最接近目标时刻的下标。"""

    timestamps = [point[0] for point in points]
    idx = bisect_left(timestamps, target_timestamp)
    if idx <= 0:
        return 0
    if idx >= len(points):
        return len(points) - 1
    before = points[idx - 1][0]
    after = points[idx][0]
    return idx - 1 if abs(target_timestamp - before) <= abs(after - target_timestamp) else idx


class TrendCanvas(QWidget):
    """直接用 QPainter 绘制可勾选、独立量程、支持点击读数的实时曲线画布。"""

    COLORS = (
        "#2d6cdf",
        "#d64545",
        "#2a9d55",
        "#9b51e0",
        "#e08b24",
        "#008c99",
        "#c23b8b",
        "#5c6bc0",
        "#7c6a0a",
        "#1e88e5",
    )
    REDRAW_INTERVAL_MS = 250

    seriesCatalogChanged = Signal()
    inspectionChanged = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(scaled(540), scaled(360))
        self.history: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=900)
        )
        self.series_meta: dict[str, TrendSeriesMeta] = {}
        self.selected_keys: set[str] = set()
        self._base_name_owner: dict[str, str] = {}
        self._clock_offset: float | None = None
        self._pinned_timestamp: float | None = None
        self._pinned_primary_key: str | None = None
        self._current_inspection: TrendInspection | None = None
        self._last_plot_rect = QRect()
        self.setAutoFillBackground(True)
        # 仪表通常每 200 ms 推送一次快照。直接在每次推送后重绘，会反复遍历全部
        # 历史点并在 GUI 线程创建 QPainterPath。单次定时器把短时间内的多次更新
        # 合并为最多 4 FPS；它只影响显示，不降低仪表轮询和 DAT 的采样频率。
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(self.REDRAW_INTERVAL_MS)
        self._redraw_timer.timeout.connect(self.update)

    def _base_series_name(self, snapshot: InstrumentSnapshot) -> str:
        owner = self._base_name_owner.get(snapshot.display_name)
        if owner is None:
            self._base_name_owner[snapshot.display_name] = snapshot.instrument_id
            return snapshot.display_name
        if owner == snapshot.instrument_id:
            return snapshot.display_name
        return f"{snapshot.display_name} ({snapshot.instrument_id})"

    def _register_series(
        self,
        key: str,
        label: str,
        *,
        unit: str = "",
        decimals: int | None = None,
        default_visible: bool = True,
    ) -> bool:
        existing = self.series_meta.get(key)
        if existing is not None:
            if unit and not existing.unit:
                existing.unit = unit
            if decimals is not None and existing.decimals is None:
                existing.decimals = decimals
            return False
        color = self.COLORS[len(self.series_meta) % len(self.COLORS)]
        self.series_meta[key] = TrendSeriesMeta(
            key=key,
            label=label,
            unit=unit,
            decimals=decimals,
            color=color,
            default_visible=default_visible,
        )
        if default_visible:
            self.selected_keys.add(key)
        return True

    def add_snapshots(self, snapshots: dict[str, InstrumentSnapshot]) -> None:
        """追加有数值的仪表主读数与辅助读数快照，并合并安排下一次可见重绘。"""

        appended = False
        catalog_changed = False
        latest_monotonic: float | None = None
        for snapshot in snapshots.values():
            if snapshot.kind not in (
                InstrumentKind.TEMPERATURE,
                InstrumentKind.FIELD,
                InstrumentKind.MONITOR,
            ):
                continue
            base_name = self._base_series_name(snapshot)
            if _is_finite_number(snapshot.current):
                if self._register_series(
                    base_name,
                    base_name,
                    unit=snapshot.unit,
                    default_visible=True,
                ):
                    catalog_changed = True
                # 使用读数本身的单调时间，避免 GUI 消息排队时把延迟错误画成采样时间。
                self.history[base_name].append(
                    (snapshot.timestamp, float(snapshot.current))
                )
                appended = True
                latest_monotonic = (
                    snapshot.timestamp
                    if latest_monotonic is None
                    else max(latest_monotonic, snapshot.timestamp)
                )
            if len(snapshot.controls) > 1:
                for panel_id, control in snapshot.controls.items():
                    if not _is_finite_number(control.current):
                        continue
                    key = f"{base_name} · {panel_id}"
                    if self._register_series(
                        key,
                        key,
                        unit=snapshot.unit,
                        default_visible=False,
                    ):
                        catalog_changed = True
                    self.history[key].append(
                        (snapshot.timestamp, float(control.current))
                    )
                    appended = True
                    latest_monotonic = (
                        snapshot.timestamp
                        if latest_monotonic is None
                        else max(latest_monotonic, snapshot.timestamp)
                    )
            for metric in snapshot.metrics.values():
                if not _is_finite_number(metric.value):
                    continue
                key = f"{base_name} · {metric.display_name}"
                if self._register_series(
                    key,
                    key,
                    unit=metric.unit,
                    decimals=metric.decimals,
                    default_visible=False,
                ):
                    catalog_changed = True
                self.history[key].append(
                    (snapshot.timestamp, float(metric.value))
                )
                appended = True
                latest_monotonic = (
                    snapshot.timestamp
                    if latest_monotonic is None
                    else max(latest_monotonic, snapshot.timestamp)
                )
        if latest_monotonic is not None:
            self._clock_offset = time.time() - latest_monotonic
        if catalog_changed:
            self.seriesCatalogChanged.emit()
        if appended:
            if self._pinned_timestamp is not None:
                self._refresh_pinned_inspection()
            self._schedule_redraw()

    def set_series_visible(self, key: str, visible: bool) -> None:
        """切换单条曲线是否显示，不影响其固定配色和后台历史采集。"""

        if key not in self.series_meta:
            return
        if visible:
            self.selected_keys.add(key)
        else:
            self.selected_keys.discard(key)
        if self._pinned_timestamp is not None:
            self._refresh_pinned_inspection()
        if self.isVisible():
            self.update()

    def set_all_series_visible(self, visible: bool) -> None:
        """全选或全不选当前已发现的全部趋势曲线。"""

        if visible:
            self.selected_keys = set(self.series_meta)
        else:
            self.selected_keys.clear()
        if self._pinned_timestamp is not None:
            self._refresh_pinned_inspection()
        if self.isVisible():
            self.update()

    def visible_series(self) -> list[tuple[str, list[tuple[float, float]]]]:
        """返回当前勾选且已有至少一个点的曲线列表，按注册顺序排列。"""

        result: list[tuple[str, list[tuple[float, float]]]] = []
        for key in self.series_meta:
            if key not in self.selected_keys:
                continue
            points = list(self.history.get(key, ()))
            if points:
                result.append((key, points))
        return result

    def current_inspection(self) -> TrendInspection | None:
        return self._current_inspection

    def clear_inspection(self) -> None:
        """清除当前点击固定的数值检视标线。"""

        changed = (
            self._pinned_timestamp is not None
            or self._current_inspection is not None
        )
        self._pinned_timestamp = None
        self._pinned_primary_key = None
        self._current_inspection = None
        if changed:
            self.inspectionChanged.emit(None)
            if self.isVisible():
                self.update()

    def _schedule_redraw(self) -> None:
        """仅在可见且没有待处理重绘时启动一次合并定时器。"""

        if self.isVisible() and not self._redraw_timer.isActive():
            self._redraw_timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        """重新显示窗口时绘制隐藏期间积累的最新历史。"""

        super().showEvent(event)
        if any(self.history.values()):
            self._schedule_redraw()

    def hideEvent(self, event) -> None:  # noqa: N802
        """隐藏时取消无意义的待处理重绘，但继续保留有界历史。"""

        self._redraw_timer.stop()
        super().hideEvent(event)

    def _format_wall_time(self, monotonic_ts: float) -> str:
        if self._clock_offset is None:
            return f"t={monotonic_ts:.1f} s"
        return datetime.fromtimestamp(monotonic_ts + self._clock_offset).strftime(
            "%H:%M:%S.%f"
        )[:-5]

    @staticmethod
    def _time_bounds(
        series: list[tuple[str, list[tuple[float, float]]]],
    ) -> tuple[float, float]:
        t_min = min(points[0][0] for _, points in series)
        t_max = max(points[-1][0] for _, points in series)
        if abs(t_max - t_min) < 1e-12:
            t_max = t_min + 1.0
        return t_min, t_max

    @staticmethod
    def _value_bounds(points: list[tuple[float, float]]) -> tuple[float, float]:
        values = [value for _, value in points]
        y_min, y_max = min(values), max(values)
        if abs(y_max - y_min) < 1e-12:
            y_min -= 0.5
            y_max += 0.5
        return y_min, y_max

    def _plot_rect_for_series(
        self,
        series: list[tuple[str, list[tuple[float, float]]]],
        legend_rows: int = 1,
    ) -> QRect:
        bottom_margin = scaled(34) + max(1, legend_rows) * scaled(20)
        left_margin = scaled(72)
        top_margin = scaled(18)
        right_margin = scaled(18)
        rect = self.rect()
        width = max(scaled(120), rect.width() - left_margin - right_margin)
        height = max(scaled(100), rect.height() - top_margin - bottom_margin)
        return QRect(left_margin, top_margin, width, height)

    def _point_to_pixel(
        self,
        plot: QRect,
        timestamp: float,
        value: float,
        t_min: float,
        t_max: float,
        y_min: float,
        y_max: float,
    ) -> tuple[float, float]:
        x = plot.left() + (timestamp - t_min) / (t_max - t_min) * plot.width()
        y = plot.bottom() - (value - y_min) / (y_max - y_min) * plot.height()
        return (x, y)

    def _build_inspection(
        self,
        series: list[tuple[str, list[tuple[float, float]]]],
        plot: QRect,
        anchor_timestamp: float,
        primary_key: str,
    ) -> TrendInspection | None:
        if not series:
            return None
        t_min, t_max = self._time_bounds(series)
        # 若固定点已经随 900 点滚动窗口滑出视野，自动解除固定。
        if anchor_timestamp < t_min - 1e-9:
            return None
        entries: list[TrendInspectionEntry] = []
        primary_px: tuple[float, float] | None = None
        for key, points in series:
            meta = self.series_meta[key]
            idx = _nearest_sample_index(points, anchor_timestamp)
            sample_ts, sample_val = points[idx]
            y_min, y_max = self._value_bounds(points)
            px = self._point_to_pixel(plot, sample_ts, sample_val, t_min, t_max, y_min, y_max)
            is_primary = key == primary_key
            if is_primary:
                primary_px = px
            entries.append(
                TrendInspectionEntry(
                    key=key,
                    label=meta.label,
                    unit=meta.unit,
                    color=meta.color,
                    timestamp=sample_ts,
                    value=sample_val,
                    formatted_value=_format_trend_value(
                        sample_val,
                        meta.unit,
                        meta.decimals,
                    ),
                    is_primary=is_primary,
                )
            )
        if not entries:
            return None
        if primary_px is None:
            first_key, first_points = series[0]
            first_idx = _nearest_sample_index(first_points, anchor_timestamp)
            y_min, y_max = self._value_bounds(first_points)
            primary_px = self._point_to_pixel(
                plot,
                first_points[first_idx][0],
                first_points[first_idx][1],
                t_min,
                t_max,
                y_min,
                y_max,
            )
            primary_key = first_key
            entries[0] = TrendInspectionEntry(
                key=entries[0].key,
                label=entries[0].label,
                unit=entries[0].unit,
                color=entries[0].color,
                timestamp=entries[0].timestamp,
                value=entries[0].value,
                formatted_value=entries[0].formatted_value,
                is_primary=True,
            )
        latest_t = max(points[-1][0] for _, points in series)
        return TrendInspection(
            anchor_timestamp=anchor_timestamp,
            wall_time_text=self._format_wall_time(anchor_timestamp),
            relative_seconds=anchor_timestamp - latest_t,
            primary_key=primary_key,
            primary_point_px=primary_px,
            entries=tuple(entries),
        )

    def _refresh_pinned_inspection(self) -> None:
        if self._pinned_timestamp is None:
            return
        series = self.visible_series()
        plot = (
            self._last_plot_rect
            if self._last_plot_rect.width() > 0
            else self._plot_rect_for_series(series)
        )
        inspection = self._build_inspection(
            series,
            plot,
            self._pinned_timestamp,
            self._pinned_primary_key or "",
        )
        if inspection is None:
            self.clear_inspection()
            return
        self._current_inspection = inspection
        self.inspectionChanged.emit(inspection)

    def inspect_at(self, pos: QPointF) -> TrendInspection | None:
        """在画布坐标 pos 处拾取最近点（或同时间截面），返回各可见曲线精确值。"""

        series = self.visible_series()
        if not series:
            self.clear_inspection()
            return None
        plot = (
            self._last_plot_rect
            if self._last_plot_rect.width() > 0
            else self._plot_rect_for_series(series)
        )
        if not plot.contains(int(pos.x()), int(pos.y())):
            self.clear_inspection()
            return None

        t_min, t_max = self._time_bounds(series)
        best_distance = float("inf")
        best_key = series[0][0]
        best_timestamp = series[0][1][-1][0]
        for key, points in series:
            y_min, y_max = self._value_bounds(points)
            for sample_ts, sample_val in points:
                px, py = self._point_to_pixel(
                    plot, sample_ts, sample_val, t_min, t_max, y_min, y_max
                )
                distance = math.hypot(px - pos.x(), py - pos.y())
                if distance < best_distance:
                    best_distance = distance
                    best_key = key
                    best_timestamp = sample_ts

        # 若点击在曲线空白区，则按点击 X 位置对齐到最近的采样时刻。
        if best_distance > scaled(24):
            fraction = max(
                0.0,
                min(1.0, (pos.x() - plot.left()) / max(1.0, float(plot.width()))),
            )
            target_t = t_min + fraction * (t_max - t_min)
            best_dt = float("inf")
            for key, points in series:
                idx = _nearest_sample_index(points, target_t)
                dt = abs(points[idx][0] - target_t)
                if dt < best_dt:
                    best_dt = dt
                    best_key = key
                    best_timestamp = points[idx][0]

        self._pinned_timestamp = best_timestamp
        self._pinned_primary_key = best_key
        inspection = self._build_inspection(series, plot, best_timestamp, best_key)
        self._current_inspection = inspection
        self.inspectionChanged.emit(inspection)
        if self.isVisible():
            self.update()
        return inspection

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.clear_inspection()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.inspect_at(event.position())
            event.accept()
            return
        super().mousePressEvent(event)

    def _legend_entries(
        self,
        series: list[tuple[str, list[tuple[float, float]]]],
    ) -> list[tuple[QColor, str]]:
        items: list[tuple[QColor, str]] = []
        for key, points in series:
            meta = self.series_meta[key]
            latest = points[-1][1]
            values = [value for _, value in points]
            lo, hi = min(values), max(values)
            latest_text = _format_trend_value(latest, meta.unit, meta.decimals)
            lo_text = _format_trend_value(lo, "", meta.decimals)
            hi_text = _format_trend_value(hi, "", meta.decimals)
            unit_suffix = f" {meta.unit}" if meta.unit else ""
            text = f"{meta.label}: {latest_text}  ({lo_text}–{hi_text}{unit_suffix})"
            items.append((QColor(meta.color), text))
        return items

    def _wrap_legend_rows(
        self,
        painter: QPainter,
        items: list[tuple[QColor, str]],
        max_width: int,
    ) -> list[list[tuple[QColor, str, int]]]:
        metrics = painter.fontMetrics()
        rows: list[list[tuple[QColor, str, int]]] = []
        current_row: list[tuple[QColor, str, int]] = []
        current_width = 0
        swatch_pad = scaled(22)
        gap = scaled(18)
        for color, text in items:
            item_width = swatch_pad + metrics.horizontalAdvance(text) + gap
            if current_row and current_width + item_width > max_width:
                rows.append(current_row)
                current_row = [(color, text, item_width)]
                current_width = item_width
            else:
                current_row.append((color, text, item_width))
                current_width += item_width
        if current_row:
            rows.append(current_row)
        return rows

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fbfbfc"))

        series = self.visible_series()
        legend_items = self._legend_entries(series)
        legend_max_width = max(scaled(160), self.width() - scaled(90))
        legend_rows = self._wrap_legend_rows(painter, legend_items, legend_max_width)
        plot = self._plot_rect_for_series(series, legend_rows=max(1, len(legend_rows)))
        self._last_plot_rect = plot

        painter.setPen(QPen(QColor("#c9cdd3"), 1))
        painter.drawRect(plot)
        grid_pen = QPen(QColor("#e5e8ec"), 1)
        painter.setPen(grid_pen)
        for fraction in (0.25, 0.5, 0.75):
            y = int(plot.top() + plot.height() * fraction)
            painter.drawLine(plot.left() + 1, y, plot.right() - 1, y)

        if not series:
            message = (
                "No traces selected — check items in the Traces panel"
                if self.series_meta
                else "Waiting for live data"
            )
            painter.setPen(QColor("#777"))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, message)
            return

        t_min, t_max = self._time_bounds(series)

        # 左侧 Y 轴标注：单条曲线显示真实数值，多条独立归一化曲线显示相对刻度。
        painter.setPen(QColor("#5d6672"))
        if len(series) == 1:
            only_key, only_points = series[0]
            only_meta = self.series_meta[only_key]
            y_min, y_max = self._value_bounds(only_points)
            y_mid = (y_min + y_max) / 2.0
            axis_labels = (
                (plot.top(), _format_trend_value(y_max, only_meta.unit, only_meta.decimals)),
                (plot.center().y(), _format_trend_value(y_mid, only_meta.unit, only_meta.decimals)),
                (plot.bottom(), _format_trend_value(y_min, only_meta.unit, only_meta.decimals)),
            )
        else:
            axis_labels = (
                (plot.top(), "100%"),
                (plot.center().y(), "50%"),
                (plot.bottom(), "0%"),
            )
        for y_pos, label_text in axis_labels:
            painter.drawText(
                QRect(scaled(4), int(y_pos) - scaled(8), plot.left() - scaled(10), scaled(16)),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label_text,
            )

        # 底部 X 轴起止时间标注。
        start_text = self._format_wall_time(t_min)
        end_text = self._format_wall_time(t_max)
        axis_y = plot.bottom() + scaled(4)
        painter.drawText(
            QRect(plot.left(), axis_y, plot.width() // 2, scaled(16)),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            start_text,
        )
        painter.drawText(
            QRect(plot.left() + plot.width() // 2, axis_y, plot.width() - plot.width() // 2, scaled(16)),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            end_text,
        )

        # 绘制各可见曲线。
        for key, points in series:
            meta = self.series_meta[key]
            color = QColor(meta.color)
            y_min, y_max = self._value_bounds(points)
            if len(points) == 1:
                px, py = self._point_to_pixel(
                    plot, points[0][0], points[0][1], t_min, t_max, y_min, y_max
                )
                painter.setPen(QPen(color, 1.8))
                painter.setBrush(color)
                painter.drawEllipse(QPointF(px, py), scaled(3), scaled(3))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                continue
            path = QPainterPath()
            for point_index, (timestamp, value) in enumerate(points):
                x, y = self._point_to_pixel(
                    plot, timestamp, value, t_min, t_max, y_min, y_max
                )
                if point_index == 0:
                    path.moveTo(QPointF(x, y))
                else:
                    path.lineTo(QPointF(x, y))
            painter.setPen(QPen(color, 1.8))
            painter.drawPath(path)

        # 若存在点击固定检视点，重算当前屏幕位置并绘制十字标线、圆点与浮窗卡片。
        if self._pinned_timestamp is not None:
            inspection = self._build_inspection(
                series,
                plot,
                self._pinned_timestamp,
                self._pinned_primary_key or "",
            )
            self._current_inspection = inspection
            if inspection is not None:
                self._draw_inspection_overlay(painter, plot, series, t_min, t_max, inspection)

        # 绘制自动换行的底部图例。
        legend_y = plot.bottom() + scaled(26)
        for row in legend_rows:
            legend_x = plot.left()
            for color, text, item_width in row:
                painter.fillRect(
                    legend_x,
                    legend_y + scaled(6),
                    scaled(14),
                    scaled(3),
                    color,
                )
                painter.setPen(QColor("#24292f"))
                painter.drawText(
                    legend_x + scaled(18),
                    legend_y + scaled(12),
                    text,
                )
                legend_x += item_width
            legend_y += scaled(20)

    def _draw_inspection_overlay(
        self,
        painter: QPainter,
        plot: QRect,
        series: list[tuple[str, list[tuple[float, float]]]],
        t_min: float,
        t_max: float,
        inspection: TrendInspection,
    ) -> None:
        anchor_x = plot.left() + (
            (inspection.anchor_timestamp - t_min) / (t_max - t_min) * plot.width()
        )
        anchor_x = max(float(plot.left()), min(float(plot.right()), anchor_x))
        crosshair_pen = QPen(QColor("#57606a"), 1, Qt.PenStyle.DashLine)
        painter.setPen(crosshair_pen)
        painter.drawLine(int(anchor_x), plot.top(), int(anchor_x), plot.bottom())

        series_map = dict(series)
        for entry in inspection.entries:
            points = series_map.get(entry.key)
            if not points:
                continue
            y_min, y_max = self._value_bounds(points)
            px, py = self._point_to_pixel(
                plot, entry.timestamp, entry.value, t_min, t_max, y_min, y_max
            )
            radius = scaled(5) if entry.is_primary else scaled(3.5)
            painter.setPen(QPen(QColor(entry.color), 2.0))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(QPointF(px, py), radius, radius)

        # 绘制防裁切浮窗数值卡。
        header = (
            f"{inspection.wall_time_text}  ({inspection.relative_seconds:+.1f} s)"
            if inspection.relative_seconds < -0.05
            else f"{inspection.wall_time_text}  (latest)"
        )
        metrics = painter.fontMetrics()
        line_height = max(scaled(17), metrics.height() + scaled(3))
        row_texts = [
            (QColor(entry.color), f"{entry.label}: {entry.formatted_value}", entry.is_primary)
            for entry in inspection.entries
        ]
        max_text_width = max(
            [metrics.horizontalAdvance(header)]
            + [metrics.horizontalAdvance(text) + scaled(16) for _, text, _ in row_texts]
        )
        pad_x = scaled(10)
        pad_y = scaled(8)
        box_w = min(plot.width() - scaled(12), max_text_width + pad_x * 2)
        box_h = pad_y * 2 + line_height * (1 + len(row_texts))

        box_x = int(anchor_x) + scaled(12)
        if box_x + box_w > plot.right() - scaled(4):
            box_x = int(anchor_x) - box_w - scaled(12)
        box_x = max(plot.left() + scaled(4), min(box_x, plot.right() - box_w - scaled(4)))
        box_y = int(inspection.primary_point_px[1]) - box_h // 2
        box_y = max(plot.top() + scaled(4), min(box_y, plot.bottom() - box_h - scaled(4)))

        box_rect = QRectF(box_x, box_y, box_w, box_h)
        painter.setPen(QPen(QColor("#8b949e"), 1))
        painter.setBrush(QColor(255, 255, 255, 240))
        painter.drawRoundedRect(box_rect, scaled(5), scaled(5))

        base_font = painter.font()
        bold_font = QFont(base_font)
        bold_font.setBold(True)
        text_y = box_y + pad_y + metrics.ascent()
        painter.setFont(bold_font)
        painter.setPen(QColor("#24292f"))
        painter.drawText(box_x + pad_x, text_y, header)
        text_y += line_height
        for color, text, is_primary in row_texts:
            painter.fillRect(
                box_x + pad_x,
                text_y - metrics.ascent() + scaled(3),
                scaled(10),
                scaled(10),
                color,
            )
            painter.setFont(bold_font if is_primary else base_font)
            painter.setPen(QColor("#24292f"))
            painter.drawText(box_x + pad_x + scaled(14), text_y, text)
            text_y += line_height
        painter.setFont(base_font)


class TrendDialog(QDialog):
    """可重复显示的实时趋势浮动窗口，支持曲线勾选、图例与点击数值检视。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Live Trend")
        self.trace_checkboxes: dict[str, QCheckBox] = {}
        self.trace_value_labels: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Select traces on the right. Each trace uses its own scale; "
            "click on the plot to inspect exact values (right-click to clear). "
            "For monitoring only; the DAT file remains authoritative."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        body = QHBoxLayout()
        self.canvas = TrendCanvas()
        body.addWidget(self.canvas, 1)

        sidebar = QFrame()
        sidebar.setFrameShape(QFrame.Shape.StyledPanel)
        sidebar.setMinimumWidth(scaled(240))
        sidebar.setMaximumWidth(scaled(320))
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(scaled(8), scaled(8), scaled(8), scaled(8))

        header_row = QHBoxLayout()
        title_label = QLabel("Traces")
        title_font = QFont(title_label.font())
        title_font.setBold(True)
        title_label.setFont(title_font)
        self.select_all_button = QPushButton("All")
        self.select_none_button = QPushButton("None")
        self.select_all_button.clicked.connect(lambda checked=False: self._set_all_traces(True))
        self.select_none_button.clicked.connect(lambda checked=False: self._set_all_traces(False))
        header_row.addWidget(title_label)
        header_row.addStretch()
        header_row.addWidget(self.select_all_button)
        header_row.addWidget(self.select_none_button)
        sidebar_layout.addLayout(header_row)

        self._trace_list_widget = QWidget()
        self._trace_list_layout = QVBoxLayout(self._trace_list_widget)
        self._trace_list_layout.setContentsMargins(0, 0, 0, 0)
        self._trace_list_layout.setSpacing(scaled(6))
        self._trace_list_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._trace_list_widget)
        sidebar_layout.addWidget(scroll, 1)
        body.addWidget(sidebar)
        layout.addLayout(body, 1)

        footer = QHBoxLayout()
        self.inspection_label = QLabel(
            "Click on the plot to inspect exact values (right-click to clear)."
        )
        self.inspection_label.setWordWrap(True)
        self.inspection_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.clear_inspection_button = QPushButton("Clear")
        self.clear_inspection_button.setEnabled(False)
        self.clear_inspection_button.clicked.connect(
            lambda checked=False: self.canvas.clear_inspection()
        )
        footer.addWidget(self.inspection_label, 1)
        footer.addWidget(self.clear_inspection_button)
        layout.addLayout(footer)

        self.canvas.seriesCatalogChanged.connect(self._sync_trace_controls)
        self.canvas.inspectionChanged.connect(self._update_inspection_label)

        fit_initial_window_width(
            self,
            preferred_height=scaled(560),
        )

    def add_snapshots(self, snapshots: dict[str, InstrumentSnapshot]) -> None:
        """把主窗口收到的快照转交给画布，可见时同步刷新侧栏实时值。"""

        self.canvas.add_snapshots(snapshots)
        if self.isVisible():
            self._refresh_trace_values()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_trace_values()

    def _sync_trace_controls(self) -> None:
        for key, meta in self.canvas.series_meta.items():
            if key in self.trace_checkboxes:
                continue
            row_widget = QWidget()
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(scaled(2))

            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            top.setSpacing(scaled(6))
            swatch = QFrame()
            swatch.setFixedSize(scaled(12), scaled(12))
            swatch.setStyleSheet(
                f"background-color: {meta.color}; border-radius: 2px;"
            )
            display_label = f"{meta.label} ({meta.unit})" if meta.unit else meta.label
            box = QCheckBox(display_label)
            box.setChecked(key in self.canvas.selected_keys)
            box.toggled.connect(
                lambda checked, series_key=key: self.canvas.set_series_visible(
                    series_key,
                    checked,
                )
            )
            top.addWidget(swatch)
            top.addWidget(box, 1)
            row_layout.addLayout(top)

            value_label = QLabel("—")
            value_label.setStyleSheet("color: #57606a; padding-left: 18px;")
            value_label.setWordWrap(True)
            row_layout.addWidget(value_label)

            # 插入到末尾 stretch 之前，保持注册顺序。
            insert_at = max(0, self._trace_list_layout.count() - 1)
            self._trace_list_layout.insertWidget(insert_at, row_widget)
            self.trace_checkboxes[key] = box
            self.trace_value_labels[key] = value_label
        self._refresh_trace_values()

    def _refresh_trace_values(self) -> None:
        for key, label in self.trace_value_labels.items():
            points = self.canvas.history.get(key)
            meta = self.canvas.series_meta.get(key)
            if not points or meta is None:
                continue
            latest = points[-1][1]
            values = [value for _, value in points]
            lo, hi = min(values), max(values)
            latest_text = _format_trend_value(latest, meta.unit, meta.decimals)
            lo_text = _format_trend_value(lo, "", meta.decimals)
            hi_text = _format_trend_value(hi, "", meta.decimals)
            label.setText(f"{latest_text}  [{lo_text} … {hi_text}]")

    def _set_all_traces(self, visible: bool) -> None:
        self.canvas.set_all_series_visible(visible)
        for box in self.trace_checkboxes.values():
            blocker = QSignalBlocker(box)
            box.setChecked(visible)
            del blocker

    def _update_inspection_label(self, inspection: TrendInspection | None) -> None:
        if inspection is None:
            self.inspection_label.setText(
                "Click on the plot to inspect exact values (right-click to clear)."
            )
            self.clear_inspection_button.setEnabled(False)
            return
        parts = [
            f"{inspection.wall_time_text} ({inspection.relative_seconds:+.1f} s)"
            if inspection.relative_seconds < -0.05
            else f"{inspection.wall_time_text} (latest)"
        ]
        for entry in inspection.entries:
            marker = "*" if entry.is_primary else ""
            parts.append(f"{marker}{entry.label}: {entry.formatted_value}")
        self.inspection_label.setText("  |  ".join(parts))
        self.clear_inspection_button.setEnabled(True)
