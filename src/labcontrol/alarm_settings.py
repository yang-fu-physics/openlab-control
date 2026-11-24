"""Per-user notification settings; credentials stay outside run archives."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from pathlib import Path

from .alarm_reporting import validate_alarm_token
from .config import AlarmReportingConfig, validate_alarm_reporting


class AlarmSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.token_path = path.with_name("alarm-token.txt")

    def load(self, configured: AlarmReportingConfig) -> AlarmReportingConfig:
        if not self.path.exists():
            return validate_alarm_reporting(configured)
        values = json.loads(self.path.read_text(encoding="utf-8"))
        return validate_alarm_reporting(replace(configured, **values))

    def save(self, config: AlarmReportingConfig, token: str) -> AlarmReportingConfig:
        config = validate_alarm_reporting(replace(
            config, token_env="", token_file=str(self.token_path) if token else "",
        ))
        if token:
            validate_alarm_token(token)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if token:
            with os.fdopen(os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
                output.write(token)
        else:
            self.token_path.unlink(missing_ok=True)
        self.path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
        return config
