from __future__ import annotations

import time
from collections import defaultdict, deque

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from ..models import DeviceKind, DeviceSnapshot
from .scaling import scaled


class TrendCanvas(QWidget):
    COLORS = ("#2d6cdf", "#d64545", "#2a9d55", "#9b51e0", "#e08b24", "#008c99")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(scaled(760), scaled(430))
        self.history: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=900))
        self.setAutoFillBackground(True)

    def add_snapshots(self, snapshots: dict[str, DeviceSnapshot]) -> None:
        now = time.monotonic()
        for snapshot in snapshots.values():
            if snapshot.kind in (
                DeviceKind.TEMPERATURE,
                DeviceKind.FIELD,
                DeviceKind.MONITOR,
            ) and snapshot.current is not None:
                self.history[snapshot.display_name].append((now, snapshot.current))
            elif snapshot.kind is DeviceKind.MEASUREMENT:
                for channel, value in snapshot.channels.items():
                    if value is not None:
                        self.history[f"{snapshot.display_name}.{channel}"].append((now, value))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fbfbfc"))
        plot = self.rect().adjusted(scaled(55), scaled(25), -scaled(20), -scaled(45))
        painter.setPen(QPen(QColor("#c9cdd3"), 1))
        painter.drawRect(plot)
        for fraction in (0.25, 0.5, 0.75):
            y = plot.top() + plot.height() * fraction
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
        series = [(name, values) for name, values in self.history.items() if len(values) >= 2][:6]
        if not series:
            painter.setPen(QColor("#777"))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Waiting for live data")
            return
        legend_x = plot.left()
        for index, (name, values) in enumerate(series):
            color = QColor(self.COLORS[index % len(self.COLORS)])
            points = list(values)
            t_min, t_max = points[0][0], points[-1][0]
            y_values = [value for _, value in points]
            y_min, y_max = min(y_values), max(y_values)
            if abs(y_max - y_min) < 1e-12:
                y_min -= 0.5
                y_max += 0.5
            if abs(t_max - t_min) < 1e-12:
                t_max = t_min + 1.0
            path = QPainterPath()
            for point_index, (timestamp, value) in enumerate(points):
                x = plot.left() + (timestamp - t_min) / (t_max - t_min) * plot.width()
                y = plot.bottom() - (value - y_min) / (y_max - y_min) * plot.height()
                if point_index == 0:
                    path.moveTo(QPointF(x, y))
                else:
                    path.lineTo(QPointF(x, y))
            painter.setPen(QPen(color, 1.7))
            painter.drawPath(path)
            painter.fillRect(
                legend_x,
                plot.bottom() + scaled(16),
                scaled(12),
                scaled(3),
                color,
            )
            painter.setPen(QColor("#333"))
            painter.drawText(legend_x + scaled(16), plot.bottom() + scaled(22), name)
            legend_x += max(
                scaled(120),
                painter.fontMetrics().horizontalAdvance(name) + scaled(35),
            )


class TrendDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Live Trend")
        self.resize(scaled(900), scaled(540))
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Each trace uses its own scale. For monitoring only; the DAT file remains authoritative."))
        self.canvas = TrendCanvas()
        layout.addWidget(self.canvas, 1)

    def add_snapshots(self, snapshots: dict[str, DeviceSnapshot]) -> None:
        self.canvas.add_snapshots(snapshots)
