"""Manual System Instrument Log controls, one tab per physical instance."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                              QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
                              QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from ..config import AppConfig
from ..system_log import system_log_fields
from .scaling import scaled


class SystemLogDialog(QDialog):
    startRequested = Signal(str, int, object)
    stopRequested = Signal()
    viewRequested = Signal(str)

    def __init__(self, config: AppConfig, default_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Instrument Log")
        self._active = False
        self._pending = None
        self._state = {"active": False, "path": "", "interval_seconds": 30, "fields": []}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.path_input = QLineEdit(default_path)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        self.view_button = QPushButton("View")
        self.view_button.setToolTip("Open this System Log file in Data Browser")
        self.view_button.clicked.connect(self._view)
        self.path_input.textChanged.connect(self._update_view_button)
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(self.browse_button)
        path_layout.addWidget(self.view_button)
        form.addRow("Log file", path_row)
        self.interval_input = QSpinBox()
        self.interval_input.setRange(1, 2_000_000_000)
        self.interval_input.setValue(30)
        self.interval_input.setSuffix(" s")
        form.addRow("Interval", self.interval_input)
        layout.addLayout(form)
        hint = QLabel("Records existing cached values only; no extra instrument queries. Closing this window keeps recording.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.tabs = QTabWidget()
        self.checkboxes = {}
        fields = system_log_fields(config)
        for instrument in config.instrument_instances:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            buttons = QHBoxLayout()
            all_button, none_button = QPushButton("Select All"), QPushButton("Select None")
            buttons.addWidget(all_button)
            buttons.addWidget(none_button)
            buttons.addStretch()
            page_layout.addLayout(buttons)
            boxes = []
            for field in fields:
                if field.instrument_id != instrument.id:
                    continue
                suffix = f" ({field.unit})" if field.unit else ""
                box = QCheckBox(field.label + suffix)
                box.setChecked(True)
                box.setToolTip(field.id)
                page_layout.addWidget(box)
                self.checkboxes[field.id] = box
                boxes.append(box)
            all_button.clicked.connect(lambda checked=False, boxes=boxes: self._select(boxes, True))
            none_button.clicked.connect(lambda checked=False, boxes=boxes: self._select(boxes, False))
            page_layout.addStretch()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            tab_label = instrument.id if instrument.display_name == instrument.id else f"{instrument.display_name} ({instrument.id})"
            self.tabs.addTab(scroll, tab_label)
        layout.addWidget(self.tabs)
        self.status_label = QLabel("Stopped")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        footer = QHBoxLayout()
        self.start_button, self.stop_button = QPushButton("Start Log"), QPushButton("Stop Log")
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self.stopRequested)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.hide)
        footer.addWidget(self.start_button)
        footer.addWidget(self.stop_button)
        footer.addStretch()
        footer.addWidget(close_button)
        layout.addLayout(footer)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check_operation)
        self.resize(scaled(650), scaled(560))
        self.set_state(self._state)

    @staticmethod
    def _select(boxes, checked):
        for box in boxes:
            box.setChecked(checked)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "Select System Log file (append or create)", self.path_input.text(),
                                             "Log files (*.log);;All files (*)",
                                             options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.path_input.setText(path)

    def _selected_log_path(self) -> str:
        if self._active and self._state.get("path"):
            return str(self._state["path"]).strip()
        return self.path_input.text().strip()

    def _update_view_button(self, *_args) -> None:
        self.view_button.setEnabled(bool(self._selected_log_path()))

    def _view(self, _checked: bool = False):
        path = self._selected_log_path()
        if not path:
            QMessageBox.warning(self, "System Log", "Choose a System Log file first.")
            return
        self.viewRequested.emit(path)

    def _start(self):
        fields = tuple(key for key, box in self.checkboxes.items() if box.isChecked())
        if not fields or not self.path_input.text().strip():
            QMessageBox.warning(self, "System Log", "Choose a log file and at least one item.")
            return
        self.startRequested.emit(self.path_input.text(), self.interval_input.value(), fields)

    def set_pending(self, future):
        self._pending = future
        self.set_state(self._state)
        self.status_label.setText("Starting/stopping System Log…")
        self._timer.start()

    def _check_operation(self):
        if not self._pending.done():
            return
        future, self._pending = self._pending, None
        self._timer.stop()
        try:
            state = future.result()
        except (OSError, ValueError, RuntimeError) as exc:
            self.set_state(self._state)
            QMessageBox.warning(self, "System Log Failed", str(exc))
        else:
            self.set_state(state)

    def set_state(self, state):
        self._state = dict(state)
        self._active = bool(state["active"])
        editable = not self._active and self._pending is None
        for widget in (self.path_input, self.interval_input, self.browse_button, self.tabs):
            widget.setEnabled(editable)
        self.start_button.setEnabled(editable and bool(self.checkboxes))
        self.stop_button.setEnabled(self._active and self._pending is None)
        if self._active:
            self.path_input.setText(state["path"])
            self.status_label.setText(f"Recording every {state['interval_seconds']} s: {state['path']}")
        else:
            self.status_label.setText("Stopped" + (f": {state['path']}" if state["path"] else ""))
        self._update_view_button()
