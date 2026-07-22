from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QResizeEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..formatting import control_decimals, fixed_number
from ..models import DeviceKind, DeviceSnapshot, StabilityState
from .scaling import scaled


class ElidedLabel(QLabel):
    """A one-line label whose full value never controls the layout width."""

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
    doubleClicked = Signal(str)

    def __init__(self, device_id: str, title: str, kind: DeviceKind, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device_id = device_id
        self.kind = kind
        self.setObjectName("statusTile")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(scaled(205))
        self.setMaximumHeight(scaled(105))
        self.setCursor(
            Qt.CursorShape.ArrowCursor
            if kind is DeviceKind.MONITOR
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
            if kind is DeviceKind.MONITOR
            else "Double-click to control"
        )
        self.detail_label.setObjectName("tileDetail")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.detail_label)
        self._set_state_style("disconnected")

    def update_snapshot(self, snapshot: DeviceSnapshot) -> None:
        if not snapshot.connected:
            self.value_label.setText("—")
            self.state_label.setText("Disconnected")
            self._set_state_style("disconnected")
            return
        if snapshot.kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
            precision = control_decimals(snapshot.kind, snapshot.unit)
            current = "—" if snapshot.current is None else f"{fixed_number(snapshot.current, precision)} {snapshot.unit}"
            target = "—" if snapshot.target is None else f"{fixed_number(snapshot.target, precision)} {snapshot.unit}"
            rate = "—" if snapshot.rate_per_minute is None else f"{fixed_number(snapshot.rate_per_minute, precision)} {snapshot.unit}/min"
            self.value_label.setText(current)
            self.detail_label.setText(f"Target {target}  ·  {rate}")
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
        if event.button() == Qt.MouseButton.LeftButton and self.kind is not DeviceKind.MONITOR:
            self.doubleClicked.emit(self.device_id)
        super().mouseDoubleClickEvent(event)

    def _set_state_style(self, state: str) -> None:
        color = {
            "stable": "#2e9d55",
            "settling": "#d08a00",
            "moving": "#2e73c5",
            "timed_out": "#c53b3b",
            "stale": "#a55a00",
            "disconnected": "#777777",
        }.get(state, "#777777")
        self.setStyleSheet(
            "QFrame#statusTile { background: #ffffff; border: 1px solid #c0c0c0; "
            f"border-bottom: 4px solid {color}; border-radius: 4px; }}"
        )
