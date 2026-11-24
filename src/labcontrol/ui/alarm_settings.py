"""Notification settings and an explicit, asynchronous test Warning."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from ..alarm_reporting import read_alarm_token, validate_alarm_token
from ..alarm_settings import AlarmSettingsStore
from ..config import AlarmReportingConfig, validate_alarm_reporting
from ..runtime import RuntimeService
from .window_sizing import fit_initial_window_width
from .scaling import scaled


class AlarmSettingsDialog(QDialog):
    settingsSaved = Signal(object)

    def __init__(self, runtime: RuntimeService, store: AlarmSettingsStore,
                 config: AlarmReportingConfig, parent=None) -> None:
        super().__init__(parent)
        self.runtime, self.store, self.config = runtime, store, config
        self._pending = None
        self._operation = ""
        self._saved_config = None
        self.setWindowTitle("Alarm Notifications")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.enabled = QCheckBox("Enable remote notifications")
        self.enabled.setChecked(config.enabled)
        self.device_name = QLineEdit(config.device_name)
        self.endpoint = QLineEdit(config.endpoint)
        self.endpoint.setPlaceholderText("http://host:3889/alarm/report")
        self.endpoint.setMinimumWidth(self.endpoint.fontMetrics().horizontalAdvance(
            "https://server.example:3889/alarm/report") + scaled(24))
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.qq = QLineEdit(config.target_qq)
        self.qq.setPlaceholderText("Leave blank to turn off notifications")
        self.allow_http = QCheckBox("Allow HTTP")
        self.allow_http.setChecked(config.allow_insecure_http)
        form.addRow(self.enabled)
        form.addRow("Device name", self.device_name)
        form.addRow("Service URL", self.endpoint)
        form.addRow("Token", self.token)
        form.addRow("Recipient QQ", self.qq)
        form.addRow(self.allow_http)
        layout.addLayout(form)
        note = QLabel("Messages: [device name]：error text. QQ blank: notifications are off. Save applies immediately. "
                      "The token is stored separately in your local user settings.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        try:
            self.token.setText(read_alarm_token(config, runtime.config.project_root))
        except (OSError, ValueError) as error:
            self.status.setText(f"Could not read saved token: {error}")
        self.test_button = QPushButton("Send Test Warning")
        self.test_button.clicked.connect(self._test)
        layout.addWidget(self.test_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.qq.textChanged.connect(self._qq_changed)
        self._qq_changed(self.qq.text())
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check_pending)
        fit_initial_window_width(self, preferred_height=360)

    def _qq_changed(self, text: str) -> None:
        has_qq = bool(text.strip())
        self.enabled.setEnabled(has_qq)
        self.test_button.setEnabled(has_qq)
        if not has_qq:
            self.enabled.setChecked(False)

    def _settings(self, *, for_test: bool = False) -> AlarmReportingConfig:
        token = self.token.text().strip()
        config = validate_alarm_reporting(replace(
            self.config, enabled=for_test or self.enabled.isChecked(),
            endpoint=self.endpoint.text().strip(), target_qq=self.qq.text().strip(),
            device_name=self.device_name.text().strip(),
            allow_insecure_http=self.allow_http.isChecked(), token_env="",
            token_file=str(self.store.token_path) if token else "",
        ))
        if config.enabled or for_test:
            validate_alarm_token(token)
        return config

    def _test(self) -> None:
        try:
            config = self._settings(for_test=True)
        except ValueError as error:
            self.status.setText(str(error))
            return
        self._operation = "test"
        self._set_pending(self.runtime.test_alarm_reporting(config, self.token.text().strip()))

    def _save(self) -> None:
        try:
            config = self.store.save(self._settings(), self.token.text().strip())
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self._saved_config = config
        self._operation = "save"
        self._set_pending(self.runtime.configure_alarm_reporting(config))

    def _set_pending(self, future) -> None:
        self._pending = future
        for widget in (self.enabled, self.device_name, self.endpoint, self.token, self.qq, self.allow_http, self.test_button, self.buttons):
            widget.setEnabled(False)
        self.status.setText("Sending test warning…" if self._operation == "test" else "Saving notifications…")
        self._timer.start()

    def _check_pending(self) -> None:
        if not self._pending.done():
            return
        future, self._pending = self._pending, None
        self._timer.stop()
        for widget in (self.device_name, self.endpoint, self.token, self.qq, self.allow_http, self.buttons):
            widget.setEnabled(True)
        self._qq_changed(self.qq.text())
        try:
            future.result()
        except Exception as error:
            self.status.setText(f"Notification operation failed: {error}")
            return
        if self._operation == "save":
            self.settingsSaved.emit(self._saved_config)
            self.accept()
        else:
            self.status.setText("Test warning accepted by the server.")

    def reject(self) -> None:
        if self._pending is None:
            super().reject()
