from __future__ import annotations

import asyncio
from concurrent.futures import Future
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import ConfigurationError, load_config  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instrument_configuration import (  # noqa: E402
    load_instrument_instances,
    update_instrument_instance_config,
)
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.instruments.base import InstrumentError, InstrumentWarning  # noqa: E402
from labcontrol.instruments.manifest import (  # noqa: E402
    discover_system_instruments,
    load_instrument_manifest,
)
from labcontrol.models import (  # noqa: E402
    InstrumentConnectionState,
    InstrumentControlState,
    InstrumentKind,
    InstrumentSnapshot,
)


_CUSTOM_INSTRUMENT_TOML = """id = "demo_cryo"
name = "Demo Cryo Controller"
version = "1.0.0"
api_version = "4"
backend = "backend:DemoCryo"
setup_widget = "frontend:SetupWidget"
kinds = ["temperature"]

[[config_fields]]
id = "pid_file"
label = "PID File"
type = "pid_file"
default = "configs/pid/default.toml"

[[controls]]
id = "loop_1"
label = "Loop 1"

[[panels]]
id = "control"
label = "Sample Temperature"
template = "controller"
control = "loop_1"
reading_options = ["temp_a"]
default_reading = "temp_a"
min_value = 2.0
max_value = 400.0
default_rate_per_minute = 1.0
max_rate_per_minute = 10.0
stability_tolerance = 0.05
stability_max_slope_per_minute = 0.05
stability_dwell_seconds = 10.0
stability_timeout_seconds = 600.0
stability_window_seconds = 10.0

[[panels]]
id = "custom_card"
label = "Custom Status"
template = "custom"
widget = "frontend:StatusCard"
readings = ["temp_a"]

[readings.temp_a]
label = "Temp A"
unit = "K"
decimals = 3
"""

_CUSTOM_BACKEND_PY = """from __future__ import annotations

from labcontrol.instruments.base import InstrumentError, SystemInstrument


class DemoCryo(SystemInstrument):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.curve = "CX-1050"
        self.pid_file = str(config.extras["pid_file"])

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def read_status(self) -> dict[str, object]:
        return {
            "value": 4.2,
            "target": 4.2,
            "rate": 1.0,
            "moving": False,
            "ready": True,
            "auxiliary": {},
        }

    def execute_action(self, action: str, payload: dict | None = None) -> dict[str, object]:
        data = dict(payload or {})
        if action == "read_setup":
            return {"curve": self.curve, "pid_file": self.pid_file}
        if action == "bad_read_only_config":
            return {"config_updates": {"pid_file": self.pid_file}}
        if action == "apply_setup":
            if "curve" in data:
                self.curve = str(data["curve"])
            updates = {}
            if "pid_file" in data and str(data["pid_file"]) != self.pid_file:
                updates["pid_file"] = str(data["pid_file"])
            return {
                "curve": self.curve,
                "pid_file": self.pid_file,
                "config_updates": updates,
                "summary": f"Switched curve={self.curve}",
            }
        if action == "uncertain_write":
            raise InstrumentError("timeout after write", "INSTRUMENT_WRITE_RESULT_UNKNOWN", self.config.id)
        return super().execute_action(action, payload)

    def apply_config_updates(self, updates: dict[str, object]) -> dict[str, object]:
        if "pid_file" in updates:
            self.pid_file = str(updates["pid_file"])
            self.config.extras["pid_file"] = self.pid_file
        return {"pid_file": self.pid_file}
"""

_CUSTOM_FRONTEND_PY = """from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class StatusCard(QWidget):
    def __init__(self, api) -> None:
        super().__init__()
        self.api = api
        self.snapshots = []
        self.actions_enabled_history = []
        layout = QVBoxLayout(self)
        self.label = QLabel("Init")
        layout.addWidget(self.label)

    def update_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot)
        self.label.setText(f"T={snapshot.current}")

    def set_actions_enabled(self, enabled: bool) -> None:
        self.actions_enabled_history.append(enabled)


class SetupWidget(QWidget):
    def __init__(self, api) -> None:
        super().__init__()
        self.api = api
        self.activated_count = 0
        self.editable_history = []
        self.snapshots = []
        layout = QVBoxLayout(self)
        self.label = QLabel("Setup")
        layout.addWidget(self.label)

    def on_activated(self) -> None:
        self.activated_count += 1

    def set_editable(self, editable: bool) -> None:
        self.editable_history.append(editable)

    def update_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot)
"""


def _write_demo_project(temp_root: Path) -> Path:
    configs = temp_root / "configs"
    instruments_cfg = configs / "instruments"
    pid_dir = configs / "pid"
    sys_dir = temp_root / "system_instruments" / "demo_cryo"
    instruments_cfg.mkdir(parents=True)
    pid_dir.mkdir(parents=True)
    sys_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "configs" / "general.toml", configs / "general.toml")
    (pid_dir / "default.toml").write_text("zones = []\n", encoding="utf-8")
    (pid_dir / "rod_b.toml").write_text("zones = []\n", encoding="utf-8")
    (sys_dir / "instrument.toml").write_text(_CUSTOM_INSTRUMENT_TOML, encoding="utf-8")
    (sys_dir / "backend.py").write_text(_CUSTOM_BACKEND_PY, encoding="utf-8")
    (sys_dir / "frontend.py").write_text(_CUSTOM_FRONTEND_PY, encoding="utf-8")

    generated_toml = """id = "demo_cryo"
name = "Demo Cryo Controller"
version = "1.0.0"
api_version = "4"
backend = "backend:DemoCryo"
setup_widget = "frontend:SetupWidget"
kinds = ["temperature"]

[[config_fields]]
id = "pid_file"
label = "PID File"
type = "pid_file"
default = "configs/pid/default.toml"

[[controls]]
id = "loop_1"
label = "Loop 1"

[readings.temp_a]
label = "Temp A"
unit = "K"
decimals = 3

[[instances]]
id = "cryo_main"
pid_file = "configs/pid/default.toml"

[[instances.panels]]
id = "control"
enabled = true
order = 1
role = "sample_temp"
reading = "temp_a"

[[instances.panels]]
id = "custom_card"
enabled = true
order = 2
role = "none"
"""
    (instruments_cfg / "demo_cryo.toml").write_text(generated_toml, encoding="utf-8")
    return configs / "general.toml"


class InstrumentCustomManifestAndRuntimeTests(unittest.TestCase):
    def test_manifest_validates_custom_panel_and_setup_widget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = _write_demo_project(root)
            manifest_dir = root / "system_instruments" / "demo_cryo"
            descriptor = load_instrument_manifest(manifest_dir)
            self.assertTrue(descriptor.valid, descriptor.error)
            self.assertEqual(descriptor.setup_widget, "frontend:SetupWidget")
            custom_panel = descriptor.panel("custom_card")
            self.assertEqual(custom_panel.template, "custom")
            self.assertEqual(custom_panel.widget, "frontend:StatusCard")
            self.assertEqual(custom_panel.readings, ("temp_a",))

            config = load_config(general)
            self.assertEqual(len(config.instrument_instances), 1)
            inst = config.instrument("cryo_main")
            self.assertEqual(inst.panel("custom_card").template, "custom")
            self.assertEqual(inst.panel("custom_card").widget, "frontend:StatusCard")

            # Missing frontend.py invalidates both setup_widget and custom panel.
            (manifest_dir / "frontend.py").unlink()
            broken = load_instrument_manifest(manifest_dir)
            self.assertFalse(broken.valid)
            self.assertIn("setup_widget source does not exist", broken.error)
            self.assertIn("custom widget source does not exist", broken.error)

    def test_update_instrument_instance_config_validates_and_persists_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = _write_demo_project(root)
            config = load_config(general)
            descriptor = discover_system_instruments(config)[0]

            normalized = update_instrument_instance_config(
                root / "configs",
                root,
                descriptor,
                "cryo_main",
                {"pid_file": "configs/pid/rod_b.toml"},
            )
            expected_path = str((root / "configs" / "pid" / "rod_b.toml").resolve())
            self.assertEqual(normalized["pid_file"], expected_path)

            persisted_text = (root / "configs" / "instruments" / "demo_cryo.toml").read_text(encoding="utf-8")
            self.assertIn('pid_file = "configs/pid/rod_b.toml"', persisted_text)
            reloaded = load_instrument_instances(
                root / "configs",
                root / "system_instruments",
                root,
                (),
            )
            self.assertEqual(reloaded[0].extras["pid_file"], expected_path)

            # Equivalent non-canonical roots (e.g. Windows short TEMP paths) must
            # not make a valid in-project PID file look like a path traversal.
            reloaded = load_instrument_instances(
                root / "configs", root / "system_instruments",
                root / "configs" / "..", (),
            )
            self.assertEqual(reloaded[0].extras["pid_file"], expected_path)

            # Undeclared field and path traversal are both rejected.
            with self.assertRaises(ConfigurationError):
                update_instrument_instance_config(
                    root / "configs", root, descriptor, "cryo_main", {"id": "hijack"}
                )
            with self.assertRaises(ConfigurationError):
                update_instrument_instance_config(
                    root / "configs", root, descriptor, "cryo_main", {"pid_file": "../escape.toml"}
                )

    def test_manager_execute_action_enforces_seq_lock_and_persists_config_updates(self) -> None:
        async def scenario(root: Path) -> None:
            general = _write_demo_project(root)
            config = load_config(general)
            descriptors = discover_system_instruments(config)
            events = EventManager()
            notices = []
            events.subscribe(notices.append)
            manager = InstrumentManager(
                config,
                events,
                descriptors,
                isolate_processes=False,
            )
            manager._connection_states["cryo_main"] = InstrumentConnectionState.CONNECTED

            # Read-only action works even while SEQ holds control.
            manager.acquire_sequence_control()
            setup = await manager.execute_action("cryo_main", "read_setup", read_only=True)
            self.assertEqual(setup["curve"], "CX-1050")

            # Read-only action cannot smuggle config_updates.
            with self.assertRaises(InstrumentError) as bad_ro:
                await manager.execute_action("cryo_main", "bad_read_only_config", read_only=True)
            self.assertEqual(bad_ro.exception.code, "INVALID_INSTRUMENT_ACTION_RESULT")

            # Write action is blocked while SEQ owns control.
            with self.assertRaises(InstrumentWarning) as blocked:
                await manager.execute_action("cryo_main", "apply_setup", {"curve": "DT-670"})
            self.assertEqual(blocked.exception.code, "MANUAL_CONTROL_BLOCKED")
            manager.release_sequence_control()

            # Simulated disk persistence failure must NOT mutate backend or config.extras,
            # and retrying apply_setup once disk recovers must succeed.
            default_pid = str((root / "configs" / "pid" / "default.toml").resolve())
            rod_b = str((root / "configs" / "pid" / "rod_b.toml").resolve())
            with patch(
                "labcontrol.instrument_manager.update_instrument_instance_config",
                side_effect=PermissionError("disk read-only"),
            ):
                with self.assertRaises(InstrumentError) as save_failed:
                    await manager.execute_action(
                        "cryo_main",
                        "apply_setup",
                        {"pid_file": rod_b},
                    )
            self.assertEqual(save_failed.exception.code, "INSTRUMENT_CONFIG_SAVE_FAILED")
            self.assertIs(
                manager._connection_states["cryo_main"],
                InstrumentConnectionState.CONNECTED,
            )
            after_failure = await manager.execute_action("cryo_main", "read_setup", read_only=True)
            self.assertEqual(after_failure["pid_file"], default_pid)
            self.assertEqual(manager.instrument_configs["cryo_main"].extras["pid_file"], default_pid)
            self.assertIn(
                'pid_file = "configs/pid/default.toml"',
                (root / "configs" / "instruments" / "demo_cryo.toml").read_text(encoding="utf-8"),
            )

            # Write action succeeds when Idle, persists pid_file, activates backend config, updates extras, and logs audit event.
            result = await manager.execute_action(
                "cryo_main",
                "apply_setup",
                {"curve": "DT-670", "pid_file": rod_b},
            )
            self.assertEqual(result["curve"], "DT-670")
            self.assertEqual(result["pid_file"], rod_b)
            self.assertEqual(result["persisted_config"]["pid_file"], rod_b)
            self.assertEqual(manager.instrument_configs["cryo_main"].extras["pid_file"], rod_b)
            self.assertIn(
                'pid_file = "configs/pid/rod_b.toml"',
                (root / "configs" / "instruments" / "demo_cryo.toml").read_text(encoding="utf-8"),
            )
            self.assertTrue(
                any(
                    notice.event.code == "INSTRUMENT_ACTION_COMPLETED"
                    and "DT-670" in notice.event.message
                    for notice in notices
                )
            )

            # Uncertain write faults the instrument and is never silently ignored.
            with self.assertRaises(InstrumentError) as uncertain:
                await manager.execute_action("cryo_main", "uncertain_write")
            self.assertEqual(uncertain.exception.code, "INSTRUMENT_WRITE_RESULT_UNKNOWN")
            self.assertIs(
                manager._connection_states["cryo_main"],
                InstrumentConnectionState.FAULTED,
            )

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))


class InstrumentCustomUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.application = QApplication.instance() or QApplication([])

    def test_custom_panel_and_setup_dialog_bridge_snapshots_and_actions(self) -> None:
        from PySide6.QtWidgets import QWidget

        from labcontrol.instruments.frontend_api import InstrumentUIAPI
        from labcontrol.ui.instrument_panels import CustomPanel, InstrumentPanelHost
        from labcontrol.ui.instrument_setup import InstrumentSetupDialog

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = _write_demo_project(root)
            config = load_config(general)
            descriptors = discover_system_instruments(config)
            inst_cfg = config.instrument("cryo_main")
            descriptor = descriptors[0]

            dispatched: list[tuple[str, str, dict, bool]] = []

            def _dispatcher(inst_id: str, action: str, payload: dict, read_only: bool) -> Future:
                dispatched.append((inst_id, action, payload, read_only))
                fut: Future = Future()
                fut.set_result({"ok": True, "persisted_config": {"pid_file": "updated.toml"}})
                return fut

            def _api_factory(cfg, parent):
                return InstrumentUIAPI(cfg, _dispatcher, parent)

            owner = QWidget()
            host = InstrumentPanelHost(
                config.instrument_instances,
                config.panels,
                parent=owner,
                descriptors=descriptors,
                ui_api_factory=_api_factory,
            )
            setup_requests: list[str] = []
            host.setupRequested.connect(setup_requests.append)
            dialog = InstrumentSetupDialog(
                ((inst_cfg, descriptor),),
                _api_factory,
                owner,
            )
            try:
                custom_panel = host.panels["cryo_main.custom_card"]
                self.assertIsInstance(custom_panel, CustomPanel)
                self.assertIsNotNone(custom_panel.content)

                snapshot = InstrumentSnapshot(
                    instrument_id="cryo_main",
                    display_name="cryo_main",
                    kind=InstrumentKind.TEMPERATURE,
                    timestamp=1.0,
                    unit="K",
                    current=4.25,
                    connection_state=InstrumentConnectionState.CONNECTED,
                    controls={
                        "control": InstrumentControlState(
                            current=4.25,
                            target=4.25,
                            rate_per_minute=1.0,
                        )
                    },
                )
                host.update_snapshot(snapshot)
                host.set_actions_enabled(False)
                self.assertEqual(len(custom_panel.content.snapshots), 1)
                self.assertEqual(custom_panel.content.actions_enabled_history[-1], False)

                assert custom_panel.api is not None
                custom_panel.api.open_setup()
                self.assertEqual(setup_requests, ["cryo_main"])

                # Setup dialog calls on_activated when opened and delivers async action results.
                setup_widget = dialog.widget_for("cryo_main")
                self.assertIsNotNone(setup_widget)
                dialog.update_snapshot(snapshot)
                dialog.set_runtime_editable(True)
                dialog.open_for("cryo_main")
                self.assertEqual(setup_widget.activated_count, 1)
                self.assertEqual(setup_widget.editable_history[-1], True)

                results: list[dict] = []
                setup_api = dialog.api_for("cryo_main")
                assert setup_api is not None
                setup_api.call_action(
                    "apply_setup",
                    {"curve": "DT-670"},
                    on_result=results.append,
                )
                self.application.processEvents()
                self.assertEqual(dispatched[-1], ("cryo_main", "apply_setup", {"curve": "DT-670"}, False))
                self.assertEqual(results[-1]["ok"], True)
                self.assertEqual(setup_api.config.extras["pid_file"], "updated.toml")
            finally:
                dialog.allow_application_close()
                dialog.close()
                owner.close()


if __name__ == "__main__":
    unittest.main()
