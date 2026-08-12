"""模块 QWidget 与核心之间的最小、只向后端发请求的 UI 桥。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QObject, Signal


class ModuleUIAPI(QObject):
    """可选 ``Frontend`` 收到的请求接口，不暴露温场或 SEQ 控制。"""

    actionRequested = Signal(str, dict)
    refreshRequested = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        resources: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._resources: dict[str, dict[str, Any]] = {}
        for resource_id, raw in (resources or {}).items():
            if not isinstance(raw, Mapping):
                raise TypeError(
                    "Measurement resource entries must be mappings"
                )
            values = dict(raw)
            if str(values.get("purpose", "")) != "measurement":
                raise ValueError(
                    "ModuleUIAPI cannot expose System Instrument resources"
                )
            self._resources[str(resource_id)] = values

    def action(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.actionRequested.emit(str(name), dict(payload or {}))

    def refresh(self) -> None:
        self.refreshRequested.emit()

    def resources(self) -> Mapping[str, Mapping[str, Any]]:
        """返回配置界面可用于下拉框的物理仪表资源深拷贝。"""

        return deepcopy(self._resources)

    def resource(self, resource_id: str) -> Mapping[str, Any]:
        """按稳定 ID 返回设置窗口可显示的一项资源。"""

        selected = str(resource_id).strip()
        if selected not in self._resources:
            raise KeyError(selected)
        return deepcopy(self._resources[selected])


__all__ = ["ModuleUIAPI"]
