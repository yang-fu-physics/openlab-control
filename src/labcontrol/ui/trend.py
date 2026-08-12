"""主窗口的轻量实时趋势监视器。

每条曲线保留最近 900 个快照并独立归一化，只用于快速观察变化；它不替代 DAT，也不把曲线
值反馈到控制逻辑。窗口由主窗口持有并复用，关闭后隐藏，避免反复重建信号和历史缓冲。
"""

from __future__ import annotations

from collections import defaultdict, deque

from PySide6.QtCore import QPointF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from ..models import InstrumentKind, InstrumentSnapshot
from .scaling import scaled
from .window_sizing import fit_initial_window_width


class TrendCanvas(QWidget):
    """直接用 QPainter 绘制最多六条、各自独立量程的实时曲线。"""

    COLORS = ("#2d6cdf", "#d64545", "#2a9d55", "#9b51e0", "#e08b24", "#008c99")
    REDRAW_INTERVAL_MS = 250

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(scaled(760), scaled(430))
        self.history: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=900))
        self.setAutoFillBackground(True)
        # 仪表通常每 200 ms 推送一次快照。直接在每次推送后重绘，会反复遍历全部
        # 历史点并在 GUI 线程创建 QPainterPath。单次定时器把短时间内的多次更新
        # 合并为最多 4 FPS；它只影响显示，不降低仪表轮询和 DAT 的采样频率。
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(self.REDRAW_INTERVAL_MS)
        self._redraw_timer.timeout.connect(self.update)

    def add_snapshots(self, snapshots: dict[str, InstrumentSnapshot]) -> None:
        """追加有数值的仪表快照，并合并安排下一次可见重绘。"""

        appended = False
        for snapshot in snapshots.values():
            if snapshot.kind in (
                InstrumentKind.TEMPERATURE,
                InstrumentKind.FIELD,
                InstrumentKind.MONITOR,
            ) and snapshot.current is not None:
                # 使用读数本身的单调时间，避免 GUI 消息排队时把延迟错误画成采样时间。
                self.history[snapshot.display_name].append(
                    (snapshot.timestamp, snapshot.current)
                )
                appended = True
        if appended:
            self._schedule_redraw()

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
    """可重复显示的趋势浮动窗口。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Live Trend")
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Each trace uses its own scale. For monitoring only; "
            "the DAT file remains authoritative."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.canvas = TrendCanvas()
        layout.addWidget(self.canvas, 1)
        fit_initial_window_width(
            self,
            preferred_height=scaled(540),
        )

    def add_snapshots(self, snapshots: dict[str, InstrumentSnapshot]) -> None:
        """把主窗口收到的快照转交给画布。"""

        self.canvas.add_snapshots(snapshots)
