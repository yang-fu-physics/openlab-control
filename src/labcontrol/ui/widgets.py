"""主窗口复用的小型状态控件。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QResizeEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..formatting import control_decimals, fixed_number
from ..models import (
    DeviceConnectionState,
    DeviceKind,
    DeviceSnapshot,
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
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
    """显示一台设备的连接、当前值、目标和稳定性。

    ``controllable`` 单独保存，Monitor 或只读温磁设备不会仅因双击而打开控制路径。
    """

    doubleClicked = Signal(str)

    def __init__(
        self,
        device_id: str,
        title: str,
        kind: DeviceKind,
        controllable: bool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.device_id = device_id
        self.kind = kind
        self.controllable = (
            kind is not DeviceKind.MONITOR
            if controllable is None
            else bool(controllable)
        )
        self.setObjectName("statusTile")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(scaled(205))
        self.setMaximumHeight(scaled(105))
        self.setCursor(
            Qt.CursorShape.ArrowCursor
            if not self.controllable
            else Qt.CursorShape.PointingHandCursor
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(scaled(10), scaled(6), scaled(10), scaled(6))
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
        layout.addWidget(self.detail_label)
        self._set_state_style("disconnected")

    def update_snapshot(self, snapshot: DeviceSnapshot) -> None:
        """用一次完整快照更新文本与状态颜色。"""

        if not snapshot.connected:
            self.value_label.setText("—")
            state_text = {
                DeviceConnectionState.STARTING: "Starting",
                DeviceConnectionState.RECONNECTING: "Reconnecting",
                DeviceConnectionState.FAULTED: "Faulted",
                DeviceConnectionState.DISCONNECTED: "Disconnected",
            }.get(snapshot.connection_state, "Disconnected")
            self.state_label.setText(state_text)
            self.detail_label.setText(
                snapshot.message
                or (
                    "Display only · not used for control"
                    if not self.controllable
                    else "Device communication unavailable"
                )
            )
            self._set_state_style(snapshot.connection_state.value)
            return
        if snapshot.kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
            precision = control_decimals(snapshot.kind, snapshot.unit)
            current = "—" if snapshot.current is None else f"{fixed_number(snapshot.current, precision)} {snapshot.unit}"
            target = "—" if snapshot.target is None else f"{fixed_number(snapshot.target, precision)} {snapshot.unit}"
            rate = "—" if snapshot.rate_per_minute is None else f"{fixed_number(snapshot.rate_per_minute, precision)} {snapshot.unit}/min"
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
        elif snapshot.kind is DeviceKind.MONITOR:
            current = "—" if snapshot.current is None else f"{snapshot.current:.3f} {snapshot.unit}"
            self.value_label.setText(current)
            self.detail_label.setText("Display only · not used for control")
            self.state_label.setText("Monitoring")
            self._set_state_style("stable")

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """仅对明确可控设备发出双击信号。"""

        if event.button() == Qt.MouseButton.LeftButton and self.controllable:
            self.doubleClicked.emit(self.device_id)
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
            "QFrame#statusTile { background: #ffffff; border: 1px solid #c0c0c0; "
            f"border-bottom: 4px solid {color}; border-radius: 4px; }}"
        )
