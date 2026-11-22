"""System Instrument 统一独立配置窗口（承载驱动可选 ``setup_widget``）。

每个声明了 ``setup_widget`` 的物理系统仪表在该窗口中占一个独立 Tab。窗口关闭仅隐藏，
不在后台轮询曲线或配置；只有在窗口打开或切换 Tab 时才通知当前页按需从仪表查询。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QSize, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import InstrumentConfig
from ..instruments.frontend_api import InstrumentUIAPI
from ..instruments.manifest import SystemInstrumentDescriptor
from ..models import InstrumentSnapshot
from ..package_support.loading import load_source_object
from .scaling import scaled
from .window_sizing import fit_initial_window_width


SETUP_DIALOG_MIN_WIDTH = 440
SETUP_DIALOG_MIN_HEIGHT = 280


class _SetupErrorWidget(QWidget):
    """自定义设置界面加载失败时的安全占位页，防止单个驱动损坏阻断主程序。"""

    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setObjectName("mutedLabel")
        layout.addWidget(label)
        layout.addStretch(1)


class InstrumentSetupDialog(QDialog):
    """承载所有声明了 ``setup_widget`` 的系统仪表的独立浮动配置窗口。"""

    def __init__(
        self,
        entries: tuple[tuple[InstrumentConfig, SystemInstrumentDescriptor], ...],
        ui_api_factory: Callable[[InstrumentConfig, QObject | None], InstrumentUIAPI],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Instrument Setup")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._allow_close = False
        self._tabs_by_id: dict[str, int] = {}
        self._order: list[str] = []
        self._apis: dict[str, InstrumentUIAPI] = {}
        self._widgets: dict[str, QWidget] = {}

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)

        for config, descriptor in entries:
            api = ui_api_factory(config, self)
            widget = self._load_setup_widget(config, descriptor, api)
            tab_label = (
                f"{config.display_name} ({descriptor.name})"
                if config.display_name != descriptor.name
                else config.display_name
            )
            index = self.tabs.addTab(widget, tab_label)
            self._tabs_by_id[config.id] = index
            self._order.append(config.id)
            self._apis[config.id] = api
            self._widgets[config.id] = widget

        if not entries:
            self.tabs.addTab(
                _SetupErrorWidget(
                    "No configured System Instruments declare an Instrument Setup interface.",
                    self.tabs,
                ),
                "Setup",
            )

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.hide)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

        self.setMinimumSize(
            QSize(scaled(SETUP_DIALOG_MIN_WIDTH), scaled(SETUP_DIALOG_MIN_HEIGHT))
        )
        fit_initial_window_width(
            self,
            preferred_height=scaled(360),
        )

    @staticmethod
    def _load_setup_widget(
        config: InstrumentConfig,
        descriptor: SystemInstrumentDescriptor,
        api: InstrumentUIAPI,
    ) -> QWidget:
        try:
            loaded = load_source_object(
                descriptor.path,
                descriptor.setup_widget,
                f"instrument_setup_{descriptor.id}_{config.id}",
            )
            if not isinstance(loaded, type):
                raise TypeError(f"{descriptor.setup_widget} is not a class")
            widget: Any = loaded(api)
            if not isinstance(widget, QWidget):
                raise TypeError(f"{descriptor.setup_widget} must return a QWidget")
            return widget
        except Exception as exc:
            return _SetupErrorWidget(
                f"Could not load setup widget {descriptor.setup_widget!r} for "
                f"{config.display_name}: {exc}"
            )

    def instrument_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    def widget_for(self, instrument_id: str) -> QWidget | None:
        return self._widgets.get(instrument_id)

    def api_for(self, instrument_id: str) -> InstrumentUIAPI | None:
        return self._apis.get(instrument_id)

    def open_for(self, instrument_id: str | None = None) -> None:
        """打开窗口并定位到指定仪表（若提供），同时触发当前页按需刷新。"""

        target_index: int | None = None
        if instrument_id is not None and instrument_id in self._tabs_by_id:
            target_index = self._tabs_by_id[instrument_id]
        if target_index is not None and self.tabs.currentIndex() != target_index:
            self.tabs.blockSignals(True)
            try:
                self.tabs.setCurrentIndex(target_index)
            finally:
                self.tabs.blockSignals(False)
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self._notify_active_tab()

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        api = self._apis.get(snapshot.instrument_id)
        was_editable = api.editable if api is not None else None
        if api is not None:
            api.update_snapshot(snapshot)
        widget = self._widgets.get(snapshot.instrument_id)
        updater = getattr(widget, "update_snapshot", None) if widget is not None else None
        if callable(updater):
            updater(snapshot)
        if api is not None and api.editable != was_editable:
            setter = getattr(widget, "set_editable", None) if widget is not None else None
            if callable(setter):
                setter(api.editable)

    def set_runtime_editable(self, editable: bool) -> None:
        for instrument_id, api in self._apis.items():
            api.set_runtime_editable(editable)
            widget = self._widgets.get(instrument_id)
            setter = getattr(widget, "set_editable", None) if widget is not None else None
            if callable(setter):
                setter(api.editable)

    def allow_application_close(self) -> None:
        self._allow_close = True

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._allow_close:
            event.accept()
        else:
            event.ignore()
            self.hide()

    def _on_tab_changed(self, _index: int) -> None:
        if self.isVisible():
            self._notify_active_tab()

    def _notify_active_tab(self) -> None:
        index = self.tabs.currentIndex()
        if not 0 <= index < len(self._order):
            return
        instrument_id = self._order[index]
        widget = self._widgets.get(instrument_id)
        hook = getattr(widget, "on_activated", None) if widget is not None else None
        if callable(hook):
            hook()


__all__ = ["InstrumentSetupDialog"]
