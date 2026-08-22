"""主窗口复用的小型状态控件。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
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
    """用固定高度卡片显示一台物理仪表的主读数。

    controllable 单独保存，Monitor 或只读温磁仪表不会仅因双击而打开控制路径。
    同一连接返回的附加读数由 :class:`InstrumentStatusPanel` 在右侧创建独立卡片，不能再
    把主卡向下撑高。
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
        self.setMaximumHeight(scaled(105))
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
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
        self._set_state_style("disconnected")

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        """用一次完整快照更新文本、自动读数格和状态颜色。"""

        if not snapshot.connected:
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

    def update_metric(
        self,
        metric: InstrumentMetric,
        source_title: str,
        *,
        connected: bool,
    ) -> None:
        """把同一连接的附加读数显示成普通只读状态卡。"""

        self.title_label.setText(metric.display_name)
        self.detail_label.setText(f"From {source_title}")
        if not connected:
            self.mark_metric_unavailable()
            return
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
        self.value_label.setText(
            value + (f" {metric.unit}" if metric.unit else "")
        )
        self.state_label.setText(
            "No Reading" if metric.value is None else "Monitoring"
        )
        self._set_state_style(
            "stale" if metric.value is None else "stable"
        )

    def mark_metric_unavailable(self) -> None:
        self.value_label.setText("—")
        self.state_label.setText("Unavailable")
        self._set_state_style("disconnected")

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

        self.setStyleSheet(_status_tile_style(state))


def _status_tile_style(state: str) -> str:
    """返回不受 Windows 深浅主题影响的固定浅色卡片样式。"""

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
    return (
        "QFrame#statusTile { background: #ffffff; "
        "border: 1px solid #c0c0c0; "
        f"border-bottom: 4px solid {color}; "
        "border-radius: 4px; }"
        "QFrame#statusTile QLabel { background: transparent; color: #202124; }"
        "QFrame#statusTile QLabel#tileDetail { color: #6f6f6f; }"
    )


class InstrumentStatusPanel(QWidget):
    """把主仪表和附加监控值保持为一行，新增卡片依次排在右侧。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tiles: list[StatusTile] = []
        self.metric_tiles: dict[tuple[str, str], StatusTile] = {}
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(
            scaled(5),
            scaled(5),
            scaled(5),
            scaled(5),
        )
        self._row.setSpacing(scaled(5))
        self._row.addStretch(1)
        self.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding,
            QSizePolicy.Policy.Preferred,
        )

    def add_tile(self, tile: StatusTile) -> None:
        self._tiles.append(tile)
        self._row.insertWidget(self._row.count() - 1, tile)

    def update_metrics(
        self,
        instrument_id: str,
        instrument_title: str,
        metrics: dict[str, InstrumentMetric],
        *,
        connected: bool,
    ) -> None:
        """为有序 metrics 字典逐项创建右侧卡片，并更新已有卡片。"""

        current_keys = set(metrics)
        for metric_key, metric in metrics.items():
            identity = (instrument_id, metric_key)
            tile = self.metric_tiles.get(identity)
            if tile is None:
                tile = StatusTile(
                    f"{instrument_id}.{metric_key}",
                    metric.display_name,
                    InstrumentKind.MONITOR,
                    False,
                    self,
                )
                self.metric_tiles[identity] = tile
                self._row.insertWidget(
                    self._row.count() - 1,
                    tile,
                )
            tile.update_metric(
                metric,
                instrument_title,
                connected=connected,
            )
        for (owner_id, metric_key), tile in self.metric_tiles.items():
            if owner_id == instrument_id and metric_key not in current_keys:
                tile.mark_metric_unavailable()


__all__ = [
    "ElidedLabel",
    "InstrumentStatusPanel",
    "StatusTile",
]
