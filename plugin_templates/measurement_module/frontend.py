from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from labcontrol.measurement.frontend_api import ModuleFrontend


class MyMeasurementFrontend(ModuleFrontend):
    def create_settings_page(self, parent: QWidget | None = None) -> QWidget:
        page = QWidget(parent)
        form = QFormLayout(page)
        self.range_input = QDoubleSpinBox()
        self.range_input.setRange(0.001, 1000.0)
        self.range_input.setValue(10.0)
        self.range_input.valueChanged.connect(self.settingsChanged)
        form.addRow("Range", self.range_input)
        return page

    def create_status_page(self, parent: QWidget | None = None) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        self.status_label = QLabel("—")
        layout.addWidget(self.status_label)
        layout.addStretch(1)
        return page

    def settings(self) -> dict[str, Any]:
        return {"range": self.range_input.value()}

    def load_settings(self, settings: Mapping[str, Any]) -> None:
        self.range_input.setValue(float(settings.get("range", 10.0)))

    def update_status(self, status: Mapping[str, Any]) -> None:
        self.status_label.setText(str(status.get("Connection", "—")))
