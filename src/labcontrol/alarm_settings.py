"""Per-user notification settings; credentials stay outside run archives."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, fields, replace
from pathlib import Path

from .alarm_reporting import validate_alarm_token
from .config import AlarmReportingConfig, validate_alarm_reporting

_CONFIG_FIELDS = frozenset(item.name for item in fields(AlarmReportingConfig))


class AlarmSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.token_path = path.with_name("alarm-token.txt")

    def load(self, configured: AlarmReportingConfig) -> AlarmReportingConfig:
        if not self.path.exists():
            return validate_alarm_reporting(configured)
        values = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError("Alarm settings file must contain a JSON object")
        known = {key: value for key, value in values.items() if key in _CONFIG_FIELDS}
        return validate_alarm_reporting(replace(configured, **known))

    def save(self, config: AlarmReportingConfig, token: str) -> AlarmReportingConfig:
        config = validate_alarm_reporting(replace(
            config, token_env="", token_file=str(self.token_path) if token else "",
        ))
        if token:
            validate_alarm_token(token)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if token:
            token_tmp = self.token_path.with_name(f".{self.token_path.name}.tmp")
            with os.fdopen(os.open(token_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
                output.write(token)
            os.replace(token_tmp, self.token_path)
        config_tmp = self.path.with_name(f".{self.path.name}.tmp")
        config_tmp.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(config_tmp, self.path)
        if not token:
            self.token_path.unlink(missing_ok=True)
        return config
