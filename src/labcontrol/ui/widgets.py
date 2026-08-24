"""主窗口复用的小型状态控件。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


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


__all__ = ["ElidedLabel"]
