from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.app import _headless_demo  # noqa: E402
from labcontrol.measurement.settings import save_settings  # noqa: E402
from labcontrol.models import (  # noqa: E402
    RunProgress,
    RunState,
    RuntimeMessage,
)
from labcontrol.sequence.module_settings import (  # noqa: E402
    load_sequence_module_settings,
    save_sequence_module_settings,
    sequence_module_settings_path,
)


class SequenceModuleSettingsTests(unittest.TestCase):
    def test_sidecar_round_trip_preserves_nested_module_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sequence = Path(temp) / "experiment.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )
            expected = {
                "lakeshore_372a": {
                    "resource": "GPIB0::12::INSTR",
                    "pause_seconds": 3,
                    "channels": {
                        "r1": {
                            "enabled": True,
                            "input_channel": 1,
                        },
                        "r2": {
                            "enabled": False,
                            "input_channel": 2,
                        },
                    },
                },
                "simulated_transport": {
                    "delay_seconds": 0.125,
                    "labels": ["R1", "R2"],
                },
            }

            sidecar = save_sequence_module_settings(
                sequence,
                expected,
                {
                    "lakeshore_372a": "0.1.0b3",
                    "simulated_transport": "1.0.1",
                },
            )
            loaded = load_sequence_module_settings(
                sequence
            )

            self.assertEqual(
                sidecar,
                sequence.with_suffix(
                    ".modules.toml"
                ).resolve(),
            )
            self.assertEqual(
                loaded.settings,
                expected,
            )
            self.assertEqual(
                loaded.versions,
                {
                    "lakeshore_372a": "0.1.0b3",
                    "simulated_transport": "1.0.1",
                },
            )
            self.assertEqual(loaded.source, sidecar)
            self.assertEqual(loaded.issues, ())

    def test_legacy_sequence_without_settings_remains_valid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sequence = Path(temp) / "legacy.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )

            loaded = load_sequence_module_settings(
                sequence
            )

            self.assertEqual(loaded.settings, {})
            self.assertEqual(loaded.versions, {})
            self.assertIsNone(loaded.source)
            self.assertEqual(loaded.issues, ())

    def test_invalid_sidecar_is_rejected_as_a_whole(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sequence = Path(temp) / "invalid.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )
            sequence_module_settings_path(
                sequence
            ).write_text(
                "\n".join(
                    (
                        "format_version = 1",
                        "",
                        "[modules.good]",
                        'version = "1.0.0"',
                        "",
                        "[modules.good.settings]",
                        "value = 1",
                        "",
                        '[modules."Bad-id"]',
                        'version = "1.0.0"',
                        "",
                        '[modules."Bad-id".settings]',
                        "value = 2",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            loaded = load_sequence_module_settings(
                sequence
            )

            self.assertEqual(loaded.settings, {})
            self.assertTrue(loaded.issues)
            self.assertIn(
                "Invalid module id",
                "\n".join(loaded.issues),
            )

    def test_run_snapshot_settings_are_imported_for_sequence_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sequence = root / "sequence.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )
            snapshot = root / "module_settings"
            save_settings(
                snapshot
                / "simulated_transport.settings.toml",
                {"delay_seconds": 0.25},
            )
            (
                snapshot
                / "simulated_transport.status-at-start.json"
            ).write_text(
                "{}\n",
                encoding="utf-8",
            )

            loaded = load_sequence_module_settings(
                sequence
            )

            self.assertEqual(
                loaded.settings,
                {
                    "simulated_transport": {
                        "delay_seconds": 0.25,
                    }
                },
            )
            self.assertEqual(
                loaded.source,
                snapshot,
            )
            self.assertEqual(loaded.issues, ())

    def test_named_sidecar_takes_priority_over_run_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sequence = root / "sequence.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )
            save_settings(
                root
                / "module_settings"
                / "simulated_transport.settings.toml",
                {"delay_seconds": 2.0},
            )
            save_sequence_module_settings(
                sequence,
                {
                    "simulated_transport": {
                        "delay_seconds": 0.5,
                    }
                },
                {"simulated_transport": "1.0.1"},
            )

            loaded = load_sequence_module_settings(
                sequence
            )

            self.assertEqual(
                loaded.settings[
                    "simulated_transport"
                ]["delay_seconds"],
                0.5,
            )
            self.assertEqual(
                loaded.source,
                sequence.with_suffix(
                    ".modules.toml"
                ).resolve(),
            )

    def test_headless_load_uses_imported_settings_without_auto_enabling_others(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sequence = root / "headless.seq"
            sequence.write_text(
                "T Measure\nT End Sequence\n",
                encoding="utf-8",
            )
            save_sequence_module_settings(
                sequence,
                {
                    "requested": {
                        "gain": 7,
                    },
                    "not_requested": {
                        "gain": 99,
                    },
                },
                {
                    "requested": "1.0.0",
                    "not_requested": "1.0.0",
                },
            )

            class Config:
                project_root = root
                modules = SimpleNamespace(
                    data_directory="module_data"
                )

                def resolve_project_path(
                    self,
                    value: str,
                ) -> Path:
                    return self.project_root / value

            class FakeRuntime:
                def __init__(self) -> None:
                    self.module_descriptors = (
                        SimpleNamespace(
                            id="requested",
                            version="1.0.0",
                        ),
                        SimpleNamespace(
                            id="not_requested",
                            version="1.0.0",
                        ),
                    )
                    self.enabled: list[
                        tuple[str, dict[str, object]]
                    ] = []
                    self.run_settings: dict[
                        str,
                        dict[str, object],
                    ] = {}
                    self._messages = [
                        RuntimeMessage(
                            "progress",
                            RunProgress(
                                RunState.COMPLETED,
                                message="completed",
                            ),
                        )
                    ]

                def start(self) -> None:
                    return None

                def enable_module(
                    self,
                    module_id: str,
                    settings: dict[str, object],
                ) -> Future[None]:
                    self.enabled.append(
                        (module_id, dict(settings))
                    )
                    future: Future[None] = Future()
                    future.set_result(None)
                    return future

                def run_sequence(
                    self,
                    _document,
                    settings: dict[
                        str,
                        dict[str, object],
                    ],
                ) -> Future[None]:
                    self.run_settings = settings
                    future: Future[None] = Future()
                    future.set_result(None)
                    return future

                def drain_messages(
                    self,
                ) -> list[RuntimeMessage]:
                    messages = self._messages
                    self._messages = []
                    return messages

                def shutdown(self) -> None:
                    return None

            runtime = FakeRuntime()
            with patch(
                "labcontrol.app.RuntimeService",
                return_value=runtime,
            ):
                exit_code = _headless_demo(
                    Config(),
                    sequence,
                    2.0,
                    ["requested"],
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                runtime.enabled,
                [
                    (
                        "requested",
                        {"gain": 7},
                    )
                ],
            )
            self.assertEqual(
                runtime.run_settings,
                {
                    "requested": {
                        "gain": 7,
                    }
                },
            )


if __name__ == "__main__":
    unittest.main()
