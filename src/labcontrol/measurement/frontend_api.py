from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class ModuleFrontendContext(QObject):
    """Safe UI-side bridge. It exposes no temperature or field control method."""

    manualActionRequested = Signal(str, dict)
    statusRefreshRequested = Signal()

    def request_manual_action(self, action: str, payload: Mapping[str, Any] | None = None) -> None:
        self.manualActionRequested.emit(action, dict(payload or {}))

    def request_status_refresh(self) -> None:
        self.statusRefreshRequested.emit()


class ModuleFrontend(QObject):
    """Base class for a module's custom Settings and Status pages."""

    settingsChanged = Signal()

    def __init__(self, context: ModuleFrontendContext) -> None:
        super().__init__()
        self.context = context

    def create_settings_page(self, parent: QWidget | None = None) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("This module does not provide settings."))
        layout.addStretch(1)
        return page

    def create_status_page(self, parent: QWidget | None = None) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("This module does not provide a status view."))
        layout.addStretch(1)
        return page

    def settings(self) -> dict[str, Any]:
        return {}

    def load_settings(self, settings: Mapping[str, Any]) -> None:
        del settings

    def update_status(self, status: Mapping[str, Any]) -> None:
        del status

    def set_sequence_running(self, running: bool) -> None:
        del running
