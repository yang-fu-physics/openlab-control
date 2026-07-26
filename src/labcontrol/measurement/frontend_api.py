"""Measurement Module 自定义窗口可使用的受限 Qt 前端接口。

前端只在 GUI 线程创建 Settings/Status 页面，并通过信号请求自己的手动动作或状态刷新。
上下文刻意不暴露温度、磁场或 SEQ 控制方法，第三方界面不能借此绕过核心安全限制。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class ModuleFrontendContext(QObject):
    """模块窗口与核心之间的安全 UI 桥；不提供温度或磁场控制入口。"""

    manualActionRequested = Signal(str, dict)
    statusRefreshRequested = Signal()

    def request_manual_action(self, action: str, payload: Mapping[str, Any] | None = None) -> None:
        """请求模块后端执行清单允许的手动动作。"""

        self.manualActionRequested.emit(action, dict(payload or {}))

    def request_status_refresh(self) -> None:
        """请求异步刷新模块状态页。"""

        self.statusRefreshRequested.emit()


class ModuleFrontend(QObject):
    """模块自定义 Settings 与 Status 页面的基类。"""

    settingsChanged = Signal()

    def __init__(self, context: ModuleFrontendContext) -> None:
        super().__init__()
        self.context = context

    def create_settings_page(self, parent: QWidget | None = None) -> QWidget:
        """创建设置页；默认返回明确的“无设置”占位页。"""

        page = QWidget(parent)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("This module does not provide settings."))
        layout.addStretch(1)
        return page

    def create_status_page(self, parent: QWidget | None = None) -> QWidget:
        """创建状态页；默认返回明确的“无状态页”占位页。"""

        page = QWidget(parent)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("This module does not provide a status view."))
        layout.addStretch(1)
        return page

    def settings(self) -> dict[str, Any]:
        """读取当前界面中的期望设置，不代表已应用到仪表。"""

        return {}

    def load_settings(self, settings: Mapping[str, Any]) -> None:
        """把已保存或随 SEQ 导入的设置填入界面，但不触发 Apply。"""

        del settings

    def update_status(self, status: Mapping[str, Any]) -> None:
        """用后端返回的只读状态快照更新状态页。"""

        del status

    def set_sequence_running(self, running: bool) -> None:
        """通知前端当前是否运行 SEQ，以便禁用有冲突的手动动作。"""

        del running
