from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.alarm_reporting import AlarmReporter  # noqa: E402
from labcontrol.config import (  # noqa: E402
    AlarmReportingConfig,
    ConfigurationError,
    load_config,
)
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.models import Severity  # noqa: E402


class _Response:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b'{"status":"success"}',
    ) -> None:
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self) -> int:
        return self.status

    def read(self, maximum: int = -1) -> bytes:
        return self.body[:maximum]


def _raise_os_error(message: str):
    raise OSError(message)


class AlarmReportingConfigTests(unittest.TestCase):
    def test_toml_qq_is_loaded_and_blank_qq_overrides_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "general.toml"
            text = (ROOT / "configs" / "general.default.toml").read_text(encoding="utf-8")
            text = text.replace("enabled = false", "enabled = true", 1)
            path.write_text(text, encoding="utf-8")
            self.assertFalse(load_config(path).alarms.reporting.enabled)
            path.write_text(text.replace('target_qq = ""', 'target_qq = "123456789"', 1), encoding="utf-8")
            loaded = load_config(path).alarms.reporting
            self.assertTrue(loaded.enabled)
            self.assertEqual(loaded.target_qq, "123456789")

    def test_default_reporting_is_disabled_and_bounded(self) -> None:
        config = load_config(
            ROOT / "configs" / "general.default.toml"
        )
        reporting = config.alarms.reporting
        self.assertFalse(reporting.enabled)
        self.assertEqual(
            reporting.endpoint,
            "http://127.0.0.1:3889/alarm/report",
        )
        self.assertEqual(reporting.retry_attempts, 3)
        self.assertGreater(reporting.timeout_seconds, 0)

    def test_remote_plain_http_requires_explicit_opt_in(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            destination = root / "configs" / "general.toml"
            source = (
                ROOT / "configs" / "general.default.toml"
            ).read_text(encoding="utf-8")
            destination.write_text(
                source.replace(
                    "enabled = false",
                    "enabled = true",
                    1,
                ).replace(
                    "http://127.0.0.1:3889/alarm/report",
                    "http://alarm.example.test/alarm/report",
                    1,
                ).replace(
                    'target_qq = ""', 'target_qq = "123456789"', 1,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(
                ConfigurationError
            ) as captured:
                load_config(destination)
            self.assertIn(
                "must use HTTPS",
                str(captured.exception),
            )


class AlarmReporterTests(unittest.TestCase):
    def _config(self, **changes) -> AlarmReportingConfig:
        values = {
            "enabled": True,
            "endpoint": (
                "http://127.0.0.1:3889/alarm/report"
            ),
            "token_env": "OPENLAB_TEST_ALARM_TOKEN",
            "timeout_seconds": 0.2,
            "retry_attempts": 1,
            "retry_delay_seconds": 0.0,
            "queue_size": 10,
            "shutdown_timeout_seconds": 1.0,
            "target_qq": "123456789",
        }
        values.update(changes)
        return AlarmReportingConfig(**values)

    def test_sends_deduplicated_warning_and_error_with_qq(
        self,
    ) -> None:
        requests = []
        states: list[str | None] = []

        def open_request(request, timeout):
            requests.append((request, timeout))
            return _Response()

        with patch.dict(
            os.environ,
            {"OPENLAB_TEST_ALARM_TOKEN": "test-secret"},
            clear=False,
        ):
            reporter = AlarmReporter(
                self._config(device_name="2K Hall"),
                ROOT,
                states.append,
                opener=open_request,
            )
            events = EventManager()
            events.subscribe(reporter.handle_notice)
            reporter.start()
            events.report(
                Severity.WARNING,
                "temperature",
                "STALE",
                "Temperature is stale",
                "primary",
            )
            events.report(
                Severity.WARNING,
                "temperature",
                "STALE",
                "Temperature is still stale",
                "primary",
            )
            events.report(
                Severity.ERROR,
                "field",
                "INTERLOCK",
                "Field interlock opened",
                "primary",
            )
            reporter.close()

        self.assertEqual(len(requests), 2)
        payloads = [
            json.loads(request.data.decode("utf-8"))
            for request, _timeout in requests
        ]
        self.assertEqual(
            [item["level"] for item in payloads],
            ["warning", "error"],
        )
        self.assertTrue(
            all(item["target_qq"] == 123456789 for item in payloads)
        )
        self.assertEqual([item["message"] for item in payloads], [
            "[2K Hall][WARNING][temperature]：Temperature is stale",
            "[2K Hall][ERROR][field]：Field interlock opened",
        ])
        self.assertNotEqual(
            payloads[0]["event_id"],
            payloads[1]["event_id"],
        )
        self.assertEqual(
            requests[0][0].get_header("X-token"),
            "test-secret",
        )
        self.assertTrue(all(state is None for state in states))

    def test_retries_the_same_id_and_recovers(self) -> None:
        payloads: list[dict] = []
        states: list[str | None] = []

        def open_request(request, timeout):
            del timeout
            payloads.append(
                json.loads(
                    request.data.decode("utf-8")
                )
            )
            if len(payloads) < 3:
                raise OSError("receiver offline")
            return _Response()

        with patch.dict(
            os.environ,
            {"OPENLAB_TEST_ALARM_TOKEN": "test-secret"},
            clear=False,
        ):
            reporter = AlarmReporter(
                self._config(retry_attempts=3),
                ROOT,
                states.append,
                opener=open_request,
            )
            events = EventManager()
            events.subscribe(reporter.handle_notice)
            reporter.start()
            events.report(
                Severity.ERROR,
                "module:test",
                "READ_FAILED",
                "Instrument read failed",
            )
            reporter.close()

        self.assertEqual(len(payloads), 3)
        self.assertEqual(
            len(
                {
                    item["event_id"]
                    for item in payloads
                }
            ),
            1,
        )
        self.assertEqual(states, [None])

    def test_missing_token_fails_closed_without_leaking(self) -> None:
        states: list[str | None] = []
        with patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            reporter = AlarmReporter(
                self._config(),
                ROOT,
                states.append,
                opener=lambda *_args, **_kwargs: _Response(),
            )
            reporter.start()
            reporter.close()

        self.assertEqual(len(states), 1)
        self.assertIsNotNone(states[0])
        self.assertIn("no token", states[0] or "")
        self.assertNotIn("test-secret", states[0] or "")

    def test_final_network_failure_is_reported_locally(
        self,
    ) -> None:
        states: list[str | None] = []
        with patch.dict(
            os.environ,
            {"OPENLAB_TEST_ALARM_TOKEN": "test-secret"},
            clear=False,
        ):
            reporter = AlarmReporter(
                self._config(retry_attempts=2),
                ROOT,
                states.append,
                opener=lambda *_args, **_kwargs: (
                    _raise_os_error("receiver offline")
                ),
            )
            events = EventManager()
            events.subscribe(reporter.handle_notice)
            reporter.start()
            events.report(
                Severity.WARNING,
                "temperature",
                "STALE",
                "Temperature is stale",
            )
            reporter.close()

        self.assertEqual(len(states), 1)
        self.assertIn(
            "failed after 2 attempt(s)",
            states[0] or "",
        )
        self.assertNotIn("test-secret", states[0] or "")


if __name__ == "__main__":
    unittest.main()
