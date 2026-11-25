"""不依赖外部绘图库的 DAT 多曲线画布。

画布支持叠加和共享 X 的分图、线性/对数坐标、框选缩放、点命中以及 PLT 导入导出。所有
数据坐标与屏幕坐标转换集中在本类，缩放状态按布局分别保存；文件刷新只重建点列表，不会在
用户查看过程中擅自重置手动视图。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QContextMenuEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from ..axis_ticks import (
    TimestampReference,
    full_timestamp_label,
    is_timestamp_column,
    linear_ticks,
    logarithmic_ticks,
    numeric_tick_label,
    timestamp_axis_title,
    timestamp_reference,
    timestamp_tick_label,
    timestamp_ticks,
)
from ..dat_reader import DatDocument, DatPoint
from ..plot_format import (
    LINEAR_SCALE,
    LOG_SCALE,
    PLOT_LAYOUTS,
    PLOT_SCALES,
    PlotFormat,
    PlotFormatError,
)
from .scaling import scaled, scaled_float
from .window_sizing import fit_initial_window_width


ROW_NUMBER_AXIS = "Row Number"
OVERLAY_LAYOUT = "overlay"
STACKED_LAYOUT = "stacked"
PLOT_COLORS = (
    "#1b5ea8",
    "#c03546",
    "#1e8259",
    "#6f42c1",
    "#c96a12",
    "#0b7d8a",
    "#9c3386",
    "#4b5563",
)


@dataclass(frozen=True, slots=True)
class PlotHit:
    """被选中的绘图点及其所属 Y 曲线。"""

    series: str
    point: DatPoint

    # 这些便捷属性保留旧集成面向 DatPoint 的简单接口，同时新增所属曲线信息。
    @property
    def x(self) -> float:
        return self.point.x

    @property
    def y(self) -> float:
        return self.point.y

    @property
    def row_index(self) -> int:
        return self.point.row_index

    @property
    def row(self) -> tuple[str, ...]:
        return self.point.row


class YSeriesSelectionDialog(QDialog):
    """一次勾选多条 Y 曲线，避免每选择一条就关闭窗口。"""

    def __init__(
        self,
        columns: tuple[str, ...] | list[str],
        selected: tuple[str, ...] | list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Y Series")
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Select one or more numeric columns, then choose OK."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.series_list = QListWidget()
        self.series_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        selected_names = set(selected)
        for column in columns:
            item = QListWidgetItem(column)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if column in selected_names
                else Qt.CheckState.Unchecked
            )
            self.series_list.addItem(item)
        layout.addWidget(self.series_list, 1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.series_list.itemChanged.connect(self._update_ok_button)
        self._update_ok_button()
        fit_initial_window_width(
            self,
            preferred_height=scaled(470),
        )

    def selected_columns(self) -> tuple[str, ...]:
        """按列表顺序返回所有勾选列。"""

        return tuple(
            self.series_list.item(index).text()
            for index in range(self.series_list.count())
            if self.series_list.item(index).checkState() == Qt.CheckState.Checked
        )

    def _update_ok_button(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self.selected_columns())
        )


class DatPlotCanvas(QWidget):
    """支持叠加与共享 X 分图的纯 Qt DAT 绘图控件。"""

    openRequested = Signal()
    reloadRequested = Signal()
    saveFormatRequested = Signal()
    reloadFormatRequested = Signal()
    copyPathRequested = Signal()
    axesChanged = Signal(str, object)
    displayChanged = Signal()
    pointActivated = Signal(object)
    pointHovered = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(scaled(240), scaled(160))
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.document: DatDocument | None = None
        self.x_column: str | None = None
        self.y_columns: tuple[str, ...] = ()
        self.layout_mode = OVERLAY_LAYOUT
        self.x_scale = LINEAR_SCALE
        self.y_scale = LINEAR_SCALE
        self.points_by_series: dict[str, tuple[DatPoint, ...]] = {}
        self._axes_initialized = False
        self._x_view: tuple[float, float] | None = None
        self._overlay_y_view: tuple[float, float] | None = None
        self._stacked_y_views: dict[str, tuple[float, float]] = {}
        self._manual_view = False
        self._drag_origin: QPointF | None = None
        self._drag_current: QPointF | None = None
        self._drag_series: str | None = None
        self._hover_hit: PlotHit | None = None
        self._timestamp_reference: TimestampReference | None = None

    @property
    def x_label(self) -> str:
        """返回实际 X 列，未选择时使用一开始计数的行号轴。"""

        return self.x_column or ROW_NUMBER_AXIS

    @property
    def y_column(self) -> str | None:
        """兼容旧调用方：返回第一条 Y 曲线。"""

        return self.y_columns[0] if self.y_columns else None

    @property
    def points(self) -> tuple[DatPoint, ...]:
        """兼容旧调用方：返回第一条 Y 曲线的点。"""

        return self.points_by_series.get(self.y_column or "", ())

    @property
    def _view_range(self) -> tuple[float, float, float, float] | None:
        """兼容旧调用方：返回当前活动面板的手动视图范围。"""

        if not self._manual_view:
            return None
        return self._ranges(self.y_column)

    def set_document(self, document: DatDocument, preserve_view: bool = False) -> None:
        """替换数据快照，选择有效数值列，并按请求保留或重置手动缩放。"""

        self.document = document
        numeric = document.numeric_columns()
        if not self._axes_initialized or (
            self.x_column is not None and self.x_column not in numeric
        ):
            self.x_column = next(
                (
                    name
                    for name in ("Time(s)", "Timestamp(s)")
                    if name in numeric
                ),
                next(
                    (
                        name
                        for name in numeric
                        if is_timestamp_column(name)
                    ),
                    None,
                ),
            )

        valid_y = tuple(name for name in self.y_columns if name in numeric)
        if not valid_y:
            preferred = [
                name
                for name in numeric
                if (
                    name not in {self.x_column, "Time(s)"}
                    and not is_timestamp_column(name)
                )
            ]
            valid_y = tuple(preferred[:1] or numeric[:1])
        self.y_columns = valid_y
        self._axes_initialized = True
        self._rebuild_points()
        if not preserve_view:
            self.reset_zoom(notify=False)
        else:
            self._discard_invalid_views()
            self.update()
        self._emit_axes_changed()

    def set_axes(
        self,
        x_column: str | None,
        y_columns: str | tuple[str, ...] | list[str],
        *,
        notify: bool = True,
    ) -> bool:
        """原子验证并切换 X 与多条 Y；任何列无效时保持原状态。"""

        if self.document is None:
            return False
        numeric = self.document.numeric_columns()
        if x_column is not None and x_column not in numeric:
            return False
        requested = (y_columns,) if isinstance(y_columns, str) else tuple(y_columns)
        requested = tuple(dict.fromkeys(requested))
        if not requested or any(name not in numeric for name in requested):
            return False
        changed = self.x_column != x_column or self.y_columns != requested
        self.x_column = x_column
        self.y_columns = requested
        self._rebuild_points()
        self.reset_zoom(notify=False)
        self._emit_axes_changed()
        if notify and changed:
            self.displayChanged.emit()
        return True

    def set_x_column(self, x_column: str | None) -> bool:
        return self.set_axes(x_column, self.y_columns)

    def set_y_columns(self, y_columns: tuple[str, ...] | list[str]) -> bool:
        return self.set_axes(self.x_column, y_columns)

    def toggle_y_column(self, column: str, enabled: bool) -> bool:
        """切换单条 Y 曲线，但始终至少保留一条。"""

        selected = list(self.y_columns)
        if enabled and column not in selected:
            selected.append(column)
        elif not enabled and column in selected:
            if len(selected) == 1:
                return False
            selected.remove(column)
        return self.set_y_columns(tuple(selected))

    def select_y_series(self) -> None:
        """打开多选窗口，并确保窗口关闭后释放 Qt 对象。"""

        if self.document is None:
            return
        dialog = YSeriesSelectionDialog(
            self.document.numeric_columns(),
            self.y_columns,
            self,
        )
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.set_y_columns(dialog.selected_columns())
        finally:
            dialog.deleteLater()

    def set_x_scale(self, scale: str, *, notify: bool = True) -> bool:
        return self._set_scale("x", scale, notify=notify)

    def set_y_scale(self, scale: str, *, notify: bool = True) -> bool:
        return self._set_scale("y", scale, notify=notify)

    def _set_scale(self, axis: str, scale: str, *, notify: bool) -> bool:
        """切换线性/对数坐标；坐标语义改变时清除旧缩放范围。"""

        normalized = scale.casefold()
        if normalized not in PLOT_SCALES or axis not in {"x", "y"}:
            return False
        attribute = f"{axis}_scale"
        changed = getattr(self, attribute) != normalized
        if not changed:
            return True
        setattr(self, attribute, normalized)
        self.reset_zoom(notify=False)
        self.update()
        if notify:
            self.displayChanged.emit()
        return True

    def set_layout(self, layout: str, *, notify: bool = True) -> bool:
        """切换叠加或分图布局，保持已选列。"""

        normalized = layout.casefold()
        if normalized not in PLOT_LAYOUTS:
            return False
        changed = normalized != self.layout_mode
        self.layout_mode = normalized
        self.update()
        if notify and changed:
            self.displayChanged.emit()
        return True

    def apply_plot_format(self, plot_format: PlotFormat) -> None:
        """验证 PLT 中所有列都存在后，一次性应用显示和缩放状态。"""

        if self.document is None:
            raise PlotFormatError("Load a DAT file before applying a PLT file")
        numeric = self.document.numeric_columns()
        if plot_format.x_column is not None and plot_format.x_column not in numeric:
            raise PlotFormatError(f"X column is not available: {plot_format.x_column}")
        missing = [name for name in plot_format.y_columns if name not in numeric]
        if missing:
            raise PlotFormatError("Y columns are not available: " + ", ".join(missing))
        self.x_column = plot_format.x_column
        self.y_columns = plot_format.y_columns
        self.layout_mode = plot_format.layout
        self.x_scale = plot_format.x_scale
        self.y_scale = plot_format.y_scale
        self._rebuild_points()
        self._x_view = plot_format.x_range
        self._overlay_y_view = plot_format.overlay_y_range
        self._stacked_y_views = {
            name: value
            for name, value in plot_format.stacked_y_ranges.items()
            if name in self.y_columns
        }
        self._manual_view = any(
            (
                self._x_view is not None,
                self._overlay_y_view is not None,
                bool(self._stacked_y_views),
            )
        )
        self._axes_initialized = True
        self._emit_axes_changed()
        self.update()

    def to_plot_format(self, data_file: str) -> PlotFormat:
        """把当前显示状态转换为经过验证的 PLT 模型。"""

        return PlotFormat(
            data_file=data_file,
            layout=self.layout_mode,
            x_column=self.x_column,
            y_columns=self.y_columns,
            x_range=self._x_view,
            overlay_y_range=self._overlay_y_view,
            stacked_y_ranges=dict(self._stacked_y_views),
            x_scale=self.x_scale,
            y_scale=self.y_scale,
        )

    def reset_zoom(self, *, notify: bool = True) -> None:
        """清除叠加和所有分图的手动范围，恢复数据自然范围。"""

        changed = self._manual_view or any(
            (
                self._x_view is not None,
                self._overlay_y_view is not None,
                bool(self._stacked_y_views),
            )
        )
        self._x_view = None
        self._overlay_y_view = None
        self._stacked_y_views = {}
        self._manual_view = False
        self.update()
        if notify and changed:
            self.displayChanged.emit()

    def _emit_axes_changed(self) -> None:
        self.axesChanged.emit(self.x_label, self.y_columns)

    def _discard_invalid_views(self) -> None:
        """文件刷新或列变化后丢弃已不存在曲线的分图范围。"""

        self._stacked_y_views = {
            name: value
            for name, value in self._stacked_y_views.items()
            if name in self.y_columns
        }
        self._manual_view = any(
            (
                self._x_view is not None,
                self._overlay_y_view is not None,
                bool(self._stacked_y_views),
            )
        )

    def _clear_hover(self) -> None:
        if self._hover_hit is not None:
            self._hover_hit = None
            self.pointHovered.emit(None)

    def _rebuild_points(self) -> None:
        """从不可变 DAT 文档重建每条曲线的有限数值点。"""

        self._clear_hover()
        if self.document is None:
            self.points_by_series = {}
            self._timestamp_reference = None
            return
        self.points_by_series = {
            name: tuple(
                point
                for point in self.document.numeric_points(name, self.x_column)
                if math.isfinite(point.x) and math.isfinite(point.y)
            )
            for name in self.y_columns
        }
        sample_x = next(
            (
                point.x
                for points in self.points_by_series.values()
                for point in points
            ),
            None,
        )
        self._timestamp_reference = timestamp_reference(
            self.document.header_lines,
            self.x_column,
            sample_x,
        )

    def format_x_value(
        self,
        value: float,
        *,
        full: bool = False,
    ) -> str:
        """按当前 X 轴语义格式化点值，绝对时间优先显示实际日期时间。"""

        reference = self._timestamp_reference
        if (
            reference is not None
            and self.x_scale == LINEAR_SCALE
        ):
            return full_timestamp_label(value, reference)
        return (
            f"{value:.12g}"
            if full
            else numeric_tick_label(value, None)
        )

    @staticmethod
    def _padded_range(
        values: list[float],
        fraction: float,
        scale: str,
    ) -> tuple[float, float] | None:
        """为自然数据范围添加留白；对数坐标在 log10 空间计算。"""

        if scale == LOG_SCALE:
            values = [value for value in values if value > 0]
        if not values:
            return None
        transformed = [math.log10(value) for value in values] if scale == LOG_SCALE else values
        low, high = min(transformed), max(transformed)
        if low == high:
            padding = max(abs(low) * 0.02, 0.05) if scale == LOG_SCALE else max(abs(low) * 0.02, 0.5)
        else:
            padding = (high - low) * fraction
        low -= padding
        high += padding
        if scale == LOG_SCALE:
            return 10**low, 10**high
        return low, high

    def _point_is_plottable(self, point: DatPoint) -> bool:
        """对数轴下过滤零和负值，线性轴接受所有有限点。"""

        return not (
            (self.x_scale == LOG_SCALE and point.x <= 0)
            or (self.y_scale == LOG_SCALE and point.y <= 0)
        )

    def _natural_x_range(self) -> tuple[float, float] | None:
        values = [
            point.x
            for points in self.points_by_series.values()
            for point in points
            if self._point_is_plottable(point)
        ]
        return self._padded_range(values, 0.035, self.x_scale)

    def _natural_y_range(self, series: str | None) -> tuple[float, float] | None:
        if self.layout_mode == OVERLAY_LAYOUT:
            values = [
                point.y
                for points in self.points_by_series.values()
                for point in points
                if self._point_is_plottable(point)
            ]
        else:
            values = [
                point.y
                for point in self.points_by_series.get(series or "", ())
                if self._point_is_plottable(point)
            ]
        return self._padded_range(values, 0.06, self.y_scale)

    def _ranges(self, series: str | None = None) -> tuple[float, float, float, float] | None:
        """合并手动范围与自然范围，得到一个面板的完整数据坐标。"""

        x_range = self._x_view or self._natural_x_range()
        if self.layout_mode == OVERLAY_LAYOUT:
            y_range = self._overlay_y_view or self._natural_y_range(None)
        else:
            effective_series = series or self.y_column
            y_range = self._stacked_y_views.get(effective_series or "")
            y_range = y_range or self._natural_y_range(effective_series)
        if x_range is None or y_range is None:
            return None
        return x_range[0], x_range[1], y_range[0], y_range[1]

    def _plot_rects(self) -> list[tuple[str | None, QRectF]]:
        """按布局和曲线数分配实际绘图面板，所有分图共享 X。"""

        show_overlay_legend = (
            self.layout_mode == OVERLAY_LAYOUT and len(self.y_columns) > 1
        )
        show_vertical_y_title = (
            self.layout_mode == STACKED_LAYOUT or len(self.y_columns) == 1
        )
        left_margin = scaled(98 if show_vertical_y_title else 78)
        top_margin = scaled(26 if show_overlay_legend else 10)
        right_margin = scaled(16)
        bottom_margin = scaled(42)
        frame = QRectF(
            self.rect().adjusted(
                left_margin,
                top_margin,
                -right_margin,
                -bottom_margin,
            )
        )
        if self.layout_mode == OVERLAY_LAYOUT or len(self.y_columns) <= 1:
            series = None if self.layout_mode == OVERLAY_LAYOUT else self.y_column
            return [(series, frame)]
        count = len(self.y_columns)
        gap = scaled_float(6.0)
        height = max(1.0, (frame.height() - gap * (count - 1)) / count)
        return [
            (
                name,
                QRectF(frame.left(), frame.top() + index * (height + gap), frame.width(), height),
            )
            for index, name in enumerate(self.y_columns)
        ]

    def _plot_rect(self) -> QRectF:
        """兼容旧调用方：返回第一个或唯一面板。"""

        panels = self._plot_rects()
        return panels[0][1] if panels else QRectF()

    def _panel_at(self, position: QPointF) -> tuple[str | None, QRectF] | None:
        return next(
            ((series, rect) for series, rect in self._plot_rects() if rect.contains(position)),
            None,
        )

    def _screen_point(
        self,
        x: float,
        y: float,
        plot: QRectF,
        ranges: tuple[float, float, float, float],
    ) -> QPointF | None:
        """把数据点转换为屏幕坐标；对数轴非法点返回 ``None``。"""

        x_min, x_max, y_min, y_max = ranges
        if (self.x_scale == LOG_SCALE and (x <= 0 or x_min <= 0)) or (
            self.y_scale == LOG_SCALE and (y <= 0 or y_min <= 0)
        ):
            return None
        x_value = math.log10(x) if self.x_scale == LOG_SCALE else x
        x_low = math.log10(x_min) if self.x_scale == LOG_SCALE else x_min
        x_high = math.log10(x_max) if self.x_scale == LOG_SCALE else x_max
        y_value = math.log10(y) if self.y_scale == LOG_SCALE else y
        y_low = math.log10(y_min) if self.y_scale == LOG_SCALE else y_min
        y_high = math.log10(y_max) if self.y_scale == LOG_SCALE else y_max
        return QPointF(
            plot.left() + (x_value - x_low) / (x_high - x_low) * plot.width(),
            plot.bottom() - (y_value - y_low) / (y_high - y_low) * plot.height(),
        )

    @staticmethod
    def _axis_fraction(
        value: float,
        low: float,
        high: float,
        scale: str,
    ) -> float:
        """返回刻度在轴上的 0–1 位置；对数轴在 log10 空间计算。"""

        if scale == LOG_SCALE:
            return (
                math.log10(value)
                - math.log10(low)
            ) / (
                math.log10(high)
                - math.log10(low)
            )
        return (value - low) / (high - low)

    def _data_x(
        self,
        pixel_x: float,
        plot: QRectF,
        ranges: tuple[float, float, float, float],
    ) -> float:
        x_min, x_max, _, _ = ranges
        fraction = (pixel_x - plot.left()) / plot.width()
        if self.x_scale == LOG_SCALE:
            return 10 ** (
                math.log10(x_min) + fraction * (math.log10(x_max) - math.log10(x_min))
            )
        return x_min + fraction * (x_max - x_min)

    def _data_y(
        self,
        pixel_y: float,
        plot: QRectF,
        ranges: tuple[float, float, float, float],
    ) -> float:
        _, _, y_min, y_max = ranges
        fraction = 1.0 - (pixel_y - plot.top()) / plot.height()
        if self.y_scale == LOG_SCALE:
            return 10 ** (
                math.log10(y_min) + fraction * (math.log10(y_max) - math.log10(y_min))
            )
        return y_min + fraction * (y_max - y_min)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if self.document is None:
            painter.setPen(QColor("#657080"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Drop a DAT file here\nor use Open",
            )
            return
        if not self.y_columns:
            painter.setPen(QColor("#657080"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No numeric Y series")
            return

        panels = self._plot_rects()
        has_points = False
        for panel_index, (series, plot) in enumerate(panels):
            ranges = self._ranges(series)
            self._draw_panel(
                painter,
                series,
                plot,
                ranges,
                show_x_labels=panel_index == len(panels) - 1,
                stacked_multi=len(panels) > 1,
            )
            has_points = has_points or ranges is not None

        if not has_points:
            painter.setPen(QColor("#657080"))
            message = (
                "No positive points for the selected logarithmic axes"
                if LOG_SCALE in {self.x_scale, self.y_scale}
                else "No numeric points for the selected axes"
            )
            painter.drawText(
                panels[0][1],
                Qt.AlignmentFlag.AlignCenter,
                message,
            )
        elif self.layout_mode == OVERLAY_LAYOUT and len(self.y_columns) > 1:
            self._draw_legend(painter, panels[0][1])

        if self._hover_hit is not None and self._drag_origin is None and has_points:
            self._draw_hover(painter, panels, self._hover_hit)

        if self._drag_origin is not None and self._drag_current is not None:
            panel = self._panel_for_series(self._drag_series)
            if panel is not None:
                selection = QRectF(self._drag_origin, self._drag_current).normalized().intersected(panel)
                painter.setBrush(QColor(27, 94, 168, 32))
                painter.setPen(QPen(QColor("#1b5ea8"), 1, Qt.PenStyle.DashLine))
                painter.drawRect(selection)

    def _draw_panel(
        self,
        painter: QPainter,
        series: str | None,
        plot: QRectF,
        ranges: tuple[float, float, float, float] | None,
        *,
        show_x_labels: bool,
        stacked_multi: bool = False,
    ) -> None:
        painter.fillRect(plot, QColor("#ffffff"))
        frame_pen = QPen(QColor("#2c3440"), 1.2)
        if ranges is None:
            painter.setPen(frame_pen)
            painter.drawRect(plot)
            return
        x_min, x_max, y_min, y_max = ranges
        timestamp_axis = (
            self._timestamp_reference
            if self.x_scale == LINEAR_SCALE
            else None
        )
        x_target = max(3, min(7, int(plot.width() // scaled_float(95.0))))
        y_target = max(3, min(5, int(plot.height() // scaled_float(30.0))))
        if timestamp_axis is not None:
            x_ticks = timestamp_ticks(
                x_min,
                x_max,
                timestamp_axis,
            )
        elif self.x_scale == LOG_SCALE:
            x_ticks = logarithmic_ticks(x_min, x_max)
        else:
            x_ticks = linear_ticks(x_min, x_max, target_intervals=x_target)
        y_ticks = (
            logarithmic_ticks(y_min, y_max)
            if self.y_scale == LOG_SCALE
            else linear_ticks(y_min, y_max, target_intervals=y_target)
        )

        grid_pen = QPen(QColor("#dce2ea"), 1.0, Qt.PenStyle.DashLine)
        tick_pen = QPen(QColor("#2c3440"), 1.0)
        tick_len = scaled_float(4.5)
        tick_font = painter.font()
        tick_font.setPointSizeF(max(7.0, tick_font.pointSizeF() * 0.92))
        metrics = painter.fontMetrics()

        last_x_right = -1e9
        for x_value in x_ticks.values:
            fraction = self._axis_fraction(
                x_value,
                x_min,
                x_max,
                self.x_scale,
            )
            x = plot.left() + plot.width() * fraction
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(tick_pen)
            painter.drawLine(QPointF(x, plot.bottom()), QPointF(x, plot.bottom() - tick_len))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.top() + tick_len))
            if show_x_labels:
                label = (
                    timestamp_tick_label(
                        x_value,
                        timestamp_axis,
                        x_min,
                        x_max,
                        x_ticks.step,
                    )
                    if timestamp_axis is not None
                    else numeric_tick_label(
                        x_value,
                        x_ticks.step,
                    )
                )
                painter.save()
                painter.setFont(tick_font)
                t_metrics = painter.fontMetrics()
                text_w = t_metrics.horizontalAdvance(label)
                left_edge = x - text_w / 2.0
                if left_edge >= last_x_right + scaled_float(8.0):
                    painter.setPen(QColor("#2c3440"))
                    label_width = max(text_w + scaled_float(6.0), scaled_float(40.0))
                    painter.drawText(
                        QRectF(
                            x - label_width / 2.0,
                            plot.bottom() + scaled_float(3.0),
                            label_width,
                            scaled_float(18.0),
                        ),
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                        label,
                    )
                    last_x_right = x + text_w / 2.0
                painter.restore()

        painter.save()
        painter.setFont(tick_font)
        t_metrics = painter.fontMetrics()
        min_y_sep = t_metrics.height() + scaled_float(2.0)
        y_edge_pad = scaled_float(4.0) if stacked_multi else scaled_float(0.0)
        y_entries: list[tuple[float, float]] = []
        for y_value in y_ticks.values:
            fraction = self._axis_fraction(
                y_value,
                y_min,
                y_max,
                self.y_scale,
            )
            y = plot.bottom() - plot.height() * fraction
            y_entries.append((y, y_value))
        y_entries.sort(key=lambda item: item[0])
        last_drawn_y = -1e9
        y_label_left = scaled_float(23.0) if (stacked_multi or len(self.y_columns) == 1) else scaled_float(2.0)
        y_label_width = max(scaled_float(30.0), plot.left() - y_label_left - scaled_float(6.0))
        for y, y_value in y_entries:
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(tick_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.left() + tick_len, y))
            painter.drawLine(QPointF(plot.right(), y), QPointF(plot.right() - tick_len, y))
            if (
                y - last_drawn_y >= min_y_sep
                and y >= plot.top() + y_edge_pad
                and y <= plot.bottom() - y_edge_pad
            ):
                painter.setPen(QColor("#2c3440"))
                painter.drawText(
                    QRectF(
                        y_label_left,
                        y - scaled_float(10.0),
                        y_label_width,
                        scaled_float(20.0),
                    ),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    numeric_tick_label(
                        y_value,
                        y_ticks.step,
                    ),
                )
                last_drawn_y = y
        painter.restore()

        if show_x_labels:
            painter.save()
            title_font = painter.font()
            title_font.setWeight(title_font.Weight.DemiBold)
            painter.setFont(title_font)
            painter.setPen(QColor("#1f2630"))
            x_axis_title = self.x_label
            if timestamp_axis is not None:
                x_axis_title = timestamp_axis_title(
                    self.x_label,
                    x_min,
                    x_max,
                    timestamp_axis,
                )
            painter.drawText(
                QRectF(
                    plot.left(),
                    plot.bottom() + scaled_float(22.0),
                    plot.width(),
                    scaled_float(18.0),
                ),
                Qt.AlignmentFlag.AlignCenter,
                x_axis_title
                + (
                    " [Log]"
                    if self.x_scale == LOG_SCALE
                    else ""
                ),
            )
            painter.restore()

        y_title_series = series if (self.layout_mode == STACKED_LAYOUT and series is not None) else (
            self.y_columns[0] if len(self.y_columns) == 1 else None
        )
        if y_title_series is not None:
            color = QColor(PLOT_COLORS[self.y_columns.index(y_title_series) % len(PLOT_COLORS)])
            y_title_text = y_title_series + (" [Log]" if self.y_scale == LOG_SCALE else "")
            painter.save()
            title_font = painter.font()
            title_font.setPointSizeF(max(7.5, title_font.pointSizeF() * 0.94))
            title_font.setWeight(title_font.Weight.DemiBold)
            painter.setFont(title_font)
            painter.setPen(color)
            elided = painter.fontMetrics().elidedText(
                y_title_text,
                Qt.TextElideMode.ElideRight,
                max(20, int(plot.height() - scaled_float(6.0))),
            )
            painter.translate(scaled_float(13.0), plot.center().y())
            painter.rotate(-90.0)
            painter.drawText(
                QRectF(
                    -plot.height() / 2.0,
                    -scaled_float(10.0),
                    plot.height(),
                    scaled_float(20.0),
                ),
                Qt.AlignmentFlag.AlignCenter,
                elided,
            )
            painter.restore()

        draw_series = self.y_columns if self.layout_mode == OVERLAY_LAYOUT else (series,)
        for name in draw_series:
            if name is None:
                continue
            points = self.points_by_series.get(name, ())
            if not points:
                continue
            color = QColor(PLOT_COLORS[self.y_columns.index(name) % len(PLOT_COLORS)])
            self._draw_series(painter, points, color, plot, ranges)

        # Draw crisp 4-sided scientific axis box last so edges remain sharp over curves.
        painter.setPen(frame_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(plot)

    def _draw_hover(
        self,
        painter: QPainter,
        panels: list[tuple[str | None, QRectF]],
        hit: PlotHit,
    ) -> None:
        guide_pen = QPen(QColor("#64748b"), 1.0, Qt.PenStyle.DotLine)
        for series, plot in panels:
            ranges = self._ranges(series)
            if ranges is None:
                continue
            x_min, x_max, _, _ = ranges
            if self.x_scale == LOG_SCALE and (hit.x <= 0 or x_min <= 0):
                continue
            frac = self._axis_fraction(hit.x, x_min, x_max, self.x_scale)
            if 0.0 <= frac <= 1.0:
                x = plot.left() + plot.width() * frac
                painter.save()
                painter.setPen(guide_pen)
                painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                painter.restore()
            if (
                (self.layout_mode == OVERLAY_LAYOUT or series == hit.series)
                and hit.series in self.y_columns
            ):
                screen = self._screen_point(hit.x, hit.y, plot, ranges)
                if screen is not None and plot.contains(screen):
                    color = QColor(PLOT_COLORS[self.y_columns.index(hit.series) % len(PLOT_COLORS)])
                    painter.save()
                    painter.setPen(QPen(color, 2.0))
                    painter.setBrush(QColor("#ffffff"))
                    painter.drawEllipse(screen, scaled_float(4.5), scaled_float(4.5))
                    painter.restore()

    def _draw_series(
        self,
        painter: QPainter,
        points: tuple[DatPoint, ...],
        color: QColor,
        plot: QRectF,
        ranges: tuple[float, float, float, float],
    ) -> None:
        stride = max(1, len(points) // 12_000)
        path = QPainterPath()
        segment_started = False
        for index, point in enumerate(points):
            if not self._point_is_plottable(point):
                segment_started = False
                continue
            if segment_started and index % stride != 0 and index != len(points) - 1:
                continue
            screen = self._screen_point(point.x, point.y, plot, ranges)
            if screen is None:
                segment_started = False
                continue
            if segment_started:
                path.lineTo(screen)
            else:
                path.moveTo(screen)
                segment_started = True
        painter.save()
        painter.setClipRect(plot)
        painter.setPen(QPen(color, 1.65))
        painter.drawPath(path)
        if len(points) <= 3_000:
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            for point in points:
                screen = self._screen_point(point.x, point.y, plot, ranges)
                if screen is not None:
                    painter.drawEllipse(screen, scaled_float(2.3), scaled_float(2.3))
        painter.restore()

    def _draw_legend(self, painter: QPainter, plot: QRectF) -> None:
        cursor_x = plot.left() + scaled_float(4)
        y = max(scaled_float(2.0), plot.top() - scaled_float(22))
        for index, name in enumerate(self.y_columns):
            width = max(
                scaled_float(76.0),
                painter.fontMetrics().horizontalAdvance(name) + scaled_float(30.0),
            )
            if cursor_x + width > plot.right():
                break
            color = QColor(PLOT_COLORS[index % len(PLOT_COLORS)])
            painter.setPen(QPen(color, 2.5))
            painter.drawLine(
                QPointF(cursor_x, y + scaled_float(9)),
                QPointF(cursor_x + scaled_float(16), y + scaled_float(9)),
            )
            painter.setPen(QColor("#1f2630"))
            painter.drawText(
                QRectF(
                    cursor_x + scaled_float(20),
                    y,
                    width - scaled_float(20),
                    scaled_float(18),
                ),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                name,
            )
            cursor_x += width

    def _panel_for_series(self, series: str | None) -> QRectF | None:
        panels = self._plot_rects()
        if self.layout_mode == OVERLAY_LAYOUT:
            return panels[0][1] if panels else None
        return next((rect for name, rect in panels if name == series), None)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """左键在面板内开始框选，并记录分图所属曲线。"""

        panel = self._panel_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and panel is not None:
            self._clear_hover()
            self._drag_series, _ = panel
            self._drag_origin = event.position()
            self._drag_current = event.position()
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_origin is not None:
            plot = self._panel_for_series(self._drag_series)
            if plot is not None:
                self._drag_current = QPointF(
                    min(max(event.position().x(), plot.left()), plot.right()),
                    min(max(event.position().y(), plot.top()), plot.bottom()),
                )
                self.update()
            event.accept()
            return
        hit = self._nearest_point(event.position()) if self.document is not None else None
        if hit != self._hover_hit:
            self._hover_hit = hit
            self.pointHovered.emit(hit)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_hit is not None:
            self._clear_hover()
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """完成足够大的框选，写入共享 X 和对应面板的 Y 范围。"""

        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            plot = self._panel_for_series(self._drag_series)
            current = self._drag_current or event.position()
            selection = (
                QRectF(self._drag_origin, current).normalized().intersected(plot)
                if plot is not None
                else QRectF()
            )
            ranges = self._ranges(self._drag_series)
            series = self._drag_series
            self._drag_origin = None
            self._drag_current = None
            self._drag_series = None
            if (
                ranges is not None
                and plot is not None
                and selection.width() >= scaled_float(10)
                and selection.height() >= scaled_float(10)
            ):
                self._x_view = (
                    self._data_x(selection.left(), plot, ranges),
                    self._data_x(selection.right(), plot, ranges),
                )
                y_range = (
                    self._data_y(selection.bottom(), plot, ranges),
                    self._data_y(selection.top(), plot, ranges),
                )
                if self.layout_mode == OVERLAY_LAYOUT:
                    self._overlay_y_view = y_range
                elif series is not None:
                    self._stacked_y_views[series] = y_range
                self._manual_view = True
                self.displayChanged.emit()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """双击最近点时发出完整行命中结果。"""

        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._nearest_point(event.position())
            if hit is not None:
                self.pointActivated.emit(hit)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _nearest_point(self, position: QPointF) -> PlotHit | None:
        """在 12 个缩放像素半径内查找当前面板最近的可绘制点。"""

        panel = self._panel_at(position)
        if panel is None:
            return None
        panel_series, plot = panel
        names = self.y_columns if self.layout_mode == OVERLAY_LAYOUT else (panel_series,)
        nearest: PlotHit | None = None
        nearest_distance = scaled_float(12.0) ** 2
        for name in names:
            if name is None:
                continue
            ranges = self._ranges(name)
            if ranges is None:
                continue
            for point in self.points_by_series.get(name, ()):
                screen = self._screen_point(point.x, point.y, plot, ranges)
                if screen is None:
                    continue
                distance = (screen.x() - position.x()) ** 2 + (screen.y() - position.y()) ** 2
                if distance <= nearest_distance:
                    nearest = PlotHit(name, point)
                    nearest_distance = distance
        return nearest

    def build_context_menu(self) -> QMenu:
        """单独构造绘图菜单，使完整交互契约可被 UI 测试验证。"""

        menu = QMenu(self)
        open_action = menu.addAction("Open DAT...")
        reload_action = menu.addAction("Reload DAT")
        copy_path_action = menu.addAction("Copy File Path")
        menu.addSeparator()
        save_action = menu.addAction("Save Plot Format")
        load_action = menu.addAction("Reload Plot Format")
        reset_action = menu.addAction("Reset Zoom")
        open_action.triggered.connect(lambda checked=False: self.openRequested.emit())
        reload_action.triggered.connect(lambda checked=False: self.reloadRequested.emit())
        copy_path_action.triggered.connect(lambda checked=False: self.copyPathRequested.emit())
        copy_path_action.setEnabled(self.document is not None)
        save_action.triggered.connect(lambda checked=False: self.saveFormatRequested.emit())
        load_action.triggered.connect(lambda checked=False: self.reloadFormatRequested.emit())
        reset_action.triggered.connect(lambda checked=False: self.reset_zoom())
        if self.document is not None:
            numeric = self.document.numeric_columns()
            menu.addSeparator()
            layout_menu = menu.addMenu("Layout")
            layout_group = QActionGroup(layout_menu)
            layout_group.setExclusive(True)
            for label, value in (("Overlay Y Series", OVERLAY_LAYOUT), ("Stacked Shared X", STACKED_LAYOUT)):
                action = layout_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.layout_mode == value)
                action.triggered.connect(
                    lambda checked=False, selected=value: self.set_layout(selected)
                )
                layout_group.addAction(action)

            x_menu = menu.addMenu("X Axis")
            x_group = QActionGroup(x_menu)
            x_group.setExclusive(True)
            row_action = x_menu.addAction(ROW_NUMBER_AXIS)
            row_action.setCheckable(True)
            row_action.setChecked(self.x_column is None)
            row_action.triggered.connect(
                lambda checked=False: self.set_x_column(None)
            )
            x_group.addAction(row_action)
            for column in numeric:
                action = x_menu.addAction(column)
                action.setCheckable(True)
                action.setChecked(column == self.x_column)
                action.triggered.connect(
                    lambda checked=False, name=column: self.set_x_column(name)
                )
                x_group.addAction(action)

            select_y_action = menu.addAction("Select Y Series...")
            select_y_action.triggered.connect(
                lambda checked=False: self.select_y_series()
            )

            x_scale_menu = menu.addMenu("X Scale")
            x_scale_group = QActionGroup(x_scale_menu)
            x_scale_group.setExclusive(True)
            for label, value in (("Linear", LINEAR_SCALE), ("Logarithmic", LOG_SCALE)):
                action = x_scale_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.x_scale == value)
                action.triggered.connect(
                    lambda checked=False, selected=value: self.set_x_scale(selected)
                )
                x_scale_group.addAction(action)

            y_scale_menu = menu.addMenu("Y Scale")
            y_scale_group = QActionGroup(y_scale_menu)
            y_scale_group.setExclusive(True)
            for label, value in (("Linear", LINEAR_SCALE), ("Logarithmic", LOG_SCALE)):
                action = y_scale_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.y_scale == value)
                action.triggered.connect(
                    lambda checked=False, selected=value: self.set_y_scale(selected)
                )
                y_scale_group.addAction(action)
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        menu = self.build_context_menu()
        menu.exec(event.globalPos())
