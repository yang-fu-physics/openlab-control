"""模块 QWidget 与核心之间的最小、只向后端发请求的 UI 桥。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QObject, Signal


class ModuleUIAPI(QObject):
    """可选 ``Frontend`` 收到的请求接口，不暴露温场或 SEQ 控制。"""

    actionRequested = Signal(str, dict)
    refreshRequested = Signal()

    def action(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.actionRequested.emit(str(name), dict(payload or {}))

    def refresh(self) -> None:
        self.refreshRequested.emit()


__all__ = ["ModuleUIAPI"]
