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
    SequenceModuleSettings,
    load_sequence_module_settings,
    module_settings_equal,
    plan_sequence_module_sync,
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
                    "simulated_transport": "1.1.0",
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
                    "simulated_transport": "1.1.0",
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
                snapshot.resolve(),
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
                {"simulated_transport": "1.1.0"},
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
                    self.instrument_sequence_commands = ()
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
                    self.enabled: list[str] = []
                    self.applied: list[
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
                ) -> Future[None]:
                    self.enabled.append(module_id)
                    future: Future[None] = Future()
                    future.set_result(None)
                    return future

                def apply_module_settings(
                    self,
                    module_id: str,
                    settings: dict[str, object],
                ) -> Future[None]:
                    self.applied.append((module_id, dict(settings)))
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
                ["requested"],
            )
            self.assertEqual(
                runtime.applied,
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

    def test_module_settings_equal_distinguishes_bool_and_int(self) -> None:
        self.assertTrue(module_settings_equal({"a": 1, "b": [2.0, "x"]}, {"a": 1.0, "b": [2, "x"]}))
        self.assertFalse(module_settings_equal({"enabled": True}, {"enabled": 1}))
        self.assertFalse(module_settings_equal({"enabled": False}, {"enabled": 0}))
        self.assertFalse(module_settings_equal({"a": {"x": 1}}, {"a": {"x": 2}}))

    def test_plan_sequence_module_sync_handles_diffs_empty_legacy_and_corrupt(self) -> None:
        # 1) Legacy SEQ without sidecar produces no changes and never disables active modules.
        legacy = SequenceModuleSettings({}, {}, source=None, issues=())
        plan = plan_sequence_module_sync(
            legacy,
            installed_ids=("mod_a", "mod_b"),
            current_enabled=("mod_a",),
            current_settings={"mod_a": {"gain": 1}},
        )
        self.assertFalse(plan.has_changes)
        self.assertEqual(plan.to_disable, ())

        # 2) Corrupt sidecar with issues never proposes disabling active modules.
        corrupt = SequenceModuleSettings({}, {}, source=Path("bad.modules.toml"), issues=("bad",))
        plan = plan_sequence_module_sync(
            corrupt,
            installed_ids=("mod_a",),
            current_enabled=("mod_a",),
            current_settings={"mod_a": {"gain": 1}},
        )
        self.assertFalse(plan.has_changes)

        # 3) Explicit empty sidecar disables currently enabled modules.
        empty_saved = SequenceModuleSettings({}, {}, source=Path("empty.modules.toml"), issues=())
        plan = plan_sequence_module_sync(
            empty_saved,
            installed_ids=("mod_a",),
            current_enabled=("mod_a",),
            current_settings={"mod_a": {"gain": 1}},
        )
        self.assertTrue(plan.has_changes)
        self.assertEqual(plan.to_disable, ("mod_a",))

        # 4) Mixed diff covers enable, disable, update, unchanged, and unavailable.
        saved = SequenceModuleSettings(
            {
                "mod_enable": {"v": 1},
                "mod_update": {"v": 2},
                "mod_same": {"v": 3.0},
                "mod_missing": {"v": 4},
            },
            {},
            source=Path("mix.modules.toml"),
        )
        plan = plan_sequence_module_sync(
            saved,
            installed_ids=("mod_enable", "mod_update", "mod_same", "mod_disable"),
            current_enabled=("mod_update", "mod_same", "mod_disable"),
            current_settings={"mod_update": {"v": 99}, "mod_same": {"v": 3}, "mod_disable": {}},
        )
        self.assertTrue(plan.has_changes)
        self.assertEqual(plan.to_enable, ("mod_enable",))
        self.assertEqual(plan.to_disable, ("mod_disable",))
        self.assertEqual(plan.to_update_settings, ("mod_update",))
        self.assertEqual(plan.unchanged, ("mod_same",))
        self.assertEqual(plan.unavailable, ("mod_missing",))

        # 5) Exact match produces has_changes == False.
        matching = SequenceModuleSettings(
            {"mod_same": {"v": 3}},
            {},
            source=Path("same.modules.toml"),
        )
        plan = plan_sequence_module_sync(
            matching,
            installed_ids=("mod_same",),
            current_enabled=("mod_same",),
            current_settings={"mod_same": {"v": 3.0}},
        )
        self.assertFalse(plan.has_changes)


if __name__ == "__main__":
    unittest.main()
