"""主窗口复用的小型状态控件。"""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..formatting import control_decimals, fixed_number
from ..models import (
    InstrumentConnectionState,
    InstrumentKind,
    InstrumentMetric,
    InstrumentSnapshot,
    StabilityState,
)
from .scaling import scaled


class ElidedLabel(QLabel):
    """单行中部省略标签；完整文本只放在 tooltip，不撑大布局。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self._full_text = ""
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.setFullText(text)

    def setFullText(self, text: str) -> None:  # noqa: N802
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._refresh_elision()

    def fullText(self) -> str:  # noqa: N802
        return self._full_text

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh_elision()

    def _refresh_elision(self) -> None:
        available = max(0, self.contentsRect().width())
        displayed = self.fontMetrics().elidedText(
            self._full_text,
            Qt.TextElideMode.ElideMiddle,
            available,
        )
        super().setText(displayed)


class StatusTile(QFrame):
    """显示一台物理仪表的主读数及自动展开的附加读数。

    controllable 单独保存，Monitor 或只读温磁仪表不会仅因双击而打开控制路径。
    snapshot.metrics 是有序字典；每个键只创建一个显示格，字典顺序就是界面顺序。
    """

    doubleClicked = Signal(str)

    def __init__(
        self,
        instrument_id: str,
        title: str,
        kind: InstrumentKind,
        controllable: bool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.instrument_id = instrument_id
        self.kind = kind
        self.controllable = (
            kind is not InstrumentKind.MONITOR
            if controllable is None
            else bool(controllable)
        )
        self.setObjectName("statusTile")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(scaled(205))
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.setCursor(
            Qt.CursorShape.ArrowCursor
            if not self.controllable
            else Qt.CursorShape.PointingHandCursor
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            scaled(10),
            scaled(6),
            scaled(10),
            scaled(6),
        )
        layout.setSpacing(scaled(2))
        header = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("tileTitle")
        self.state_label = QLabel("Disconnected")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.state_label)
        layout.addLayout(header)

        self.value_label = QLabel("—")
        self.value_label.setObjectName("tileValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value_label)
        self.detail_label = QLabel(
            "Display only · not used for control"
            if not self.controllable
            else "Double-click to control"
        )
        self.detail_label.setObjectName("tileDetail")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.metrics_widget = QWidget(self)
        self.metrics_layout = QGridLayout(self.metrics_widget)
        self.metrics_layout.setContentsMargins(0, scaled(2), 0, 0)
        self.metrics_layout.setHorizontalSpacing(scaled(8))
        self.metrics_layout.setVerticalSpacing(scaled(2))
        self.metric_labels: dict[str, QLabel] = {}
        self._metric_order: tuple[str, ...] = ()
        self.metrics_widget.hide()
        layout.addWidget(self.metrics_widget)
        self._set_state_style("disconnected")

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        """用一次完整快照更新文本、自动读数格和状态颜色。"""

        if not snapshot.connected:
            self._update_metrics({})
            self.value_label.setText("—")
            state_text = {
                InstrumentConnectionState.STARTING: "Starting",
                InstrumentConnectionState.RECONNECTING: "Reconnecting",
                InstrumentConnectionState.FAULTED: "Faulted",
                InstrumentConnectionState.DISCONNECTED: "Disconnected",
            }.get(snapshot.connection_state, "Disconnected")
            self.state_label.setText(state_text)
            self.detail_label.setText(
                snapshot.message
                or (
                    "Display only · not used for control"
                    if not self.controllable
                    else "Instrument communication unavailable"
                )
            )
            self._set_state_style(snapshot.connection_state.value)
            return

        self._update_metrics(snapshot.metrics)
        if snapshot.kind in (
            InstrumentKind.TEMPERATURE,
            InstrumentKind.FIELD,
        ):
            precision = control_decimals(snapshot.kind, snapshot.unit)
            current = (
                "—"
                if snapshot.current is None
                else f"{fixed_number(snapshot.current, precision)} {snapshot.unit}"
            )
            target = (
                "—"
                if snapshot.target is None
                else f"{fixed_number(snapshot.target, precision)} {snapshot.unit}"
            )
            rate = (
                "—"
                if snapshot.rate_per_minute is None
                else f"{fixed_number(snapshot.rate_per_minute, precision)} "
                f"{snapshot.unit}/min"
            )
            self.value_label.setText(current)
            self.detail_label.setText(
                f"Target {target}  ·  {rate}"
                if self.controllable
                else "Display only · not used for control"
            )
            state_text = {
                StabilityState.STABLE: "Stable",
                StabilityState.SETTLING: "Settling",
                StabilityState.MOVING: "Moving",
                StabilityState.TIMED_OUT: "Timed Out",
                StabilityState.STALE: "Stale",
            }.get(snapshot.stability, snapshot.activity.value)
            self.state_label.setText(state_text)
            self._set_state_style(snapshot.stability.value)
        elif snapshot.kind is InstrumentKind.MONITOR:
            current = (
                "—"
                if snapshot.current is None
                else f"{snapshot.current:.3f} {snapshot.unit}"
            )
            self.value_label.setText(current)
            self.detail_label.setText(
                "Display only · not used for control"
            )
            self.state_label.setText("Monitoring")
            self._set_state_style("stable")

    @staticmethod
    def _metric_text(metric: InstrumentMetric) -> str:
        """把结构化附加读数格式化为紧凑的两行文本。"""

        if metric.value is None:
            value = "—"
        elif isinstance(metric.value, bool):
            value = "On" if metric.value else "Off"
        elif isinstance(metric.value, (int, float)):
            value = (
                fixed_number(float(metric.value), metric.decimals)
                if metric.decimals is not None
                else f"{metric.value:.9g}"
            )
        else:
            value = str(metric.value)
        return (
            f"{metric.display_name}\n{value}"
            + (f" {metric.unit}" if metric.unit else "")
        )

    def _update_metrics(
        self,
        metrics: Mapping[str, InstrumentMetric],
    ) -> None:
        """按字典键自动创建、移除并更新两列读数格。"""

        keys = tuple(metrics)
        if keys != self._metric_order:
            for label in self.metric_labels.values():
                self.metrics_layout.removeWidget(label)
                label.deleteLater()
            self.metric_labels.clear()
            for index, key in enumerate(keys):
                label = QLabel("", self.metrics_widget)
                label.setObjectName("tileDetail")
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                label.setSizePolicy(
                    QSizePolicy.Policy.Ignored,
                    QSizePolicy.Policy.Preferred,
                )
                self.metrics_layout.addWidget(
                    label,
                    index // 2,
                    index % 2,
                )
                self.metric_labels[key] = label
            self._metric_order = keys
        for key, metric in metrics.items():
            self.metric_labels[key].setText(
                self._metric_text(metric)
            )
        self.metrics_widget.setVisible(bool(keys))

    def metric_text(self, key: str) -> str:
        """返回某项完整显示文本，主要供测试与辅助功能使用。"""

        label = self.metric_labels.get(key)
        return "" if label is None else label.text()

    def mouseDoubleClickEvent(
        self,
        event: QMouseEvent,
    ) -> None:  # noqa: N802
        """仅对明确可控仪表发出双击信号。"""

        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.controllable
        ):
            self.doubleClicked.emit(self.instrument_id)
        super().mouseDoubleClickEvent(event)

    def _set_state_style(self, state: str) -> None:
        """把运行状态映射为统一边框和背景色。"""

        color = {
            "stable": "#2e9d55",
            "settling": "#d08a00",
            "moving": "#2e73c5",
            "timed_out": "#c53b3b",
            "stale": "#a55a00",
            "starting": "#777777",
            "reconnecting": "#d08a00",
            "faulted": "#c53b3b",
            "disconnected": "#777777",
        }.get(state, "#777777")
        self.setStyleSheet(
            "QFrame#statusTile { background: #ffffff; "
            "border: 1px solid #c0c0c0; "
            f"border-bottom: 4px solid {color}; "
            "border-radius: 4px; }"
        )


class InstrumentStatusPanel(QWidget):
    """按可用宽度自动换行的多仪表监控面板。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tiles: list[StatusTile] = []
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(
            scaled(5),
            scaled(5),
            scaled(5),
            scaled(5),
        )
        self._grid.setSpacing(scaled(5))

    def add_tile(self, tile: StatusTile) -> None:
        self._tiles.append(tile)
        self._reflow(force=True)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self, *, force: bool = False) -> None:
        tile_width = max(scaled(220), 1)
        spacing = max(self._grid.horizontalSpacing(), 0)
        available = max(self.width(), tile_width)
        columns = max(
            1,
            (available + spacing) // (tile_width + spacing),
        )
        columns = min(columns, max(1, len(self._tiles)))
        if not force and columns == self._columns:
            return
        for column in range(max(self._columns, columns)):
            self._grid.setColumnStretch(column, 0)
        for index, tile in enumerate(self._tiles):
            self._grid.addWidget(
                tile,
                index // columns,
                index % columns,
            )
        for column in range(columns):
            self._grid.setColumnStretch(column, 1)
        self._columns = columns


__all__ = [
    "ElidedLabel",
    "InstrumentStatusPanel",
    "StatusTile",
]
