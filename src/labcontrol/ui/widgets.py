from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..models import DeviceKind, DeviceSnapshot, StabilityState


class StatusTile(QFrame):
    doubleClicked = Signal(str)

    def __init__(self, device_id: str, title: str, kind: DeviceKind, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device_id = device_id
        self.kind = kind
        self.setObjectName("statusTile")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(205)
        self.setMaximumHeight(105)
        self.setCursor(
            Qt.CursorShape.ArrowCursor
            if kind is DeviceKind.MONITOR
            else Qt.CursorShape.PointingHandCursor
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
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
            precision = 4 if snapshot.kind is DeviceKind.FIELD else 3
            current = "—" if snapshot.current is None else f"{snapshot.current:.{precision}f} {snapshot.unit}"
            target = "—" if snapshot.target is None else f"{snapshot.target:.{precision}f} {snapshot.unit}"
            rate = "—" if snapshot.rate_per_minute is None else f"{snapshot.rate_per_minute:g} {snapshot.unit}/min"
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
        else:
            available = [(key, value) for key, value in snapshot.channels.items() if value is not None]
            if available:
                first_key, first_value = available[0]
                self.value_label.setText(f"{first_key}  {first_value:.6g} {snapshot.unit}")
                remaining = "  ·  ".join(f"{key} {value:.4g}" for key, value in available[1:3])
                self.detail_label.setText(remaining or "Double-click to measure")
            else:
                self.value_label.setText("Awaiting measurement")
                self.detail_label.setText("Double-click to measure")
            self.state_label.setText("Ready")
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
            "QFrame#statusTile { background: #f4f5f7; border: 1px solid #aeb4bd; "
            f"border-bottom: 4px solid {color}; border-radius: 3px; }}"
        )
