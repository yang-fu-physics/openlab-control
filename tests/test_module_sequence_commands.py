from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from labcontrol.events import EventManager  # noqa: E402
from labcontrol.measurement.manifest import ModuleDescriptor  # noqa: E402
from labcontrol.measurement.service import MeasurementModuleService  # noqa: E402
from labcontrol.measurement.worker import ModuleWorkerClient, WorkerRequestError  # noqa: E402
from labcontrol.models import RunState  # noqa: E402
from labcontrol.module_commands import (  # noqa: E402
    estimate_module_scan_points,
    generate_scan_points,
    linear_points_from_step,
    log_points_from_per_decade,
    normalize_module_commands,
    resolve_module_point_bounds,
    resolve_module_scan_points,
    validate_module_command_parameters,
)
from labcontrol.sequence.engine import SequenceEngine  # noqa: E402
from labcontrol.sequence.model import Command, CommandType, SequenceDocument  # noqa: E402
from labcontrol.sequence.parser import (  # noqa: E402
    format_command,
    parse_sequence,
    serialize_sequence,
)
from labcontrol.ui.dialogs import CommandDialog  # noqa: E402
from labcontrol.ui.main_window import MainWindow  # noqa: E402
from labcontrol.ui.measurement_modules import ModuleWindow  # noqa: E402
from labcontrol.ui.sequence_editor import SequenceEditorWidget  # noqa: E402
from tests.configuration_fixtures import load_simulated_config  # noqa: E402


DECLARATIONS = [
    {
        "id": "set_current",
        "label": "Set Current",
        "description": "Set the source current without writing a DAT row.",
        "kind": "command",
        "fields": [
            {
                "name": "current",
                "label": "Current",
                "type": "float",
                "default": 0.001,
                "minimum": -0.01,
                "maximum": 0.01,
                "unit": "A",
                "decimals": 9,
            },
            {
                "name": "output",
                "label": "Enable output",
                "type": "bool",
                "default": True,
            },
        ],
    },
    {
        "id": "scan_current",
        "label": "Scan Current",
        "kind": "scan",
        "points_field": "points",
        "point_parameter": "current",
        "fields": [
            {
                "name": "points",
                "label": "Current points",
                "type": "list",
                "default": ["1 mA", "2 mA"],
            },
            {
                "name": "settle_seconds",
                "label": "Settle time",
                "type": "float",
                "default": 0.0,
                "minimum": 0.0,
                "unit": "s",
            },
        ],
    },
]


class ModuleCommandDeclarationTests(unittest.TestCase):
    def test_normalizes_commands_and_copies_mutable_defaults(self) -> None:
        specs = normalize_module_commands("test_meter", DECLARATIONS)
        self.assertEqual([spec.command_id for spec in specs], ["set_current", "scan_current"])
        self.assertEqual(specs[1].command_type, CommandType.MODULE_SCAN)
        first = specs[1].create()
        second = specs[1].create()
        first.params["points"].append("3 mA")
        self.assertEqual(second.params["points"], ["1 mA", "2 mA"])

    def test_rejects_ambiguous_scan_and_invalid_values(self) -> None:
        broken = [{
            "id": "scan_current",
            "kind": "scan",
            "fields": [{
                "name": "points",
                "type": "text",
                "default": "1,2",
            }],
        }]
        with self.assertRaisesRegex(TypeError, "points_field must name a list field"):
            normalize_module_commands("test_meter", broken)

        spec = normalize_module_commands("test_meter", DECLARATIONS)[0]
        self.assertIn(
            "no more than 0.01",
            " ".join(
                validate_module_command_parameters(
                    spec,
                    {"current": 1.0, "output": True},
                )
            ),
        )
        self.assertIn(
            "Unknown parameters",
            " ".join(
                validate_module_command_parameters(
                    spec,
                    {"current": 0.0, "output": True, "raw_scpi": "*RST"},
                )
            ),
        )

    def test_scan_modes_payload_roundtrip_generation_step_sync_and_limits(self) -> None:
        raw = [
            {
                "id": "scan_current",
                "label": "Scan Current",
                "kind": "scan",
                "points_field": "points",
                "point_parameter": "current",
                "scan_modes": ["Linear", "Log", "List"],
                "point_unit": "A",
                "point_limits": {
                    "setting": "max_current",
                    "symmetric": True,
                    "minimum": -0.07071,
                    "maximum": 0.07071,
                },
                "fields": [
                    {"name": "points", "label": "RMS points (A)", "type": "list", "default": ["100u", "200u", "500u"]},
                    {"name": "settle_seconds", "label": "Settle time", "type": "float", "default": 3.0, "minimum": 0.0, "unit": "s"},
                ],
            },
            {
                "id": "scan_voltage",
                "label": "Scan Voltage",
                "kind": "scan",
                "points_field": "points",
                "point_parameter": "voltage",
                "scan_modes": ["Linear", "Log", "List"],
                "point_unit": "V",
                "point_decimals": 3,
                "point_limits": {
                    "setting": "max_voltage",
                    "symmetric": False,
                    "minimum": 0.0,
                    "maximum": 5.0,
                },
                "fields": [
                    {"name": "points", "label": "RMS points (V)", "type": "list", "default": ["10m", "20m", "50m"]},
                    {"name": "settle_seconds", "label": "Settle time", "type": "float", "default": 3.0, "minimum": 0.0, "unit": "s"},
                ],
            },
        ]
        specs = normalize_module_commands("src", raw)
        # Payload round-trip over worker IPC must preserve all scan metadata identically.
        reloaded = normalize_module_commands("src", [spec.to_payload() for spec in specs])
        self.assertEqual(specs, reloaded)
        current_spec, voltage_spec = specs

        # Legacy List SEQ without point_mode remains valid and executable.
        legacy = {"points": ["100u", "200u", "500u"], "settle_seconds": 3.0}
        self.assertEqual(validate_module_command_parameters(current_spec, legacy), ())
        self.assertEqual(resolve_module_scan_points(current_spec, legacy), ["100u", "200u", "500u"])

        # Linear & Log modes validate, resolve points, and estimate step counts.
        linear = {"point_mode": "Linear", "start": "-200u", "stop": "200u", "steps": 5, "settle_seconds": 1.0}
        self.assertEqual(validate_module_command_parameters(current_spec, linear), ())
        self.assertEqual(resolve_module_scan_points(current_spec, linear), ["-200u", "-100u", "0", "100u", "200u"])
        self.assertEqual(estimate_module_scan_points(current_spec, linear), 5)

        log_curr = {"point_mode": "Log", "start": "10u", "stop": "1m", "steps": 3, "settle_seconds": 1.0}
        self.assertEqual(validate_module_command_parameters(current_spec, log_curr), ())
        self.assertEqual(resolve_module_scan_points(current_spec, log_curr), ["10u", "100u", "1m"])
        bad_log = {"point_mode": "Log", "start": "-10u", "stop": "10u", "steps": 3, "settle_seconds": 1.0}
        self.assertIn("same sign", " ".join(validate_module_command_parameters(current_spec, bad_log)))

        # OE1022D point_decimals=3 quantizes generated Log/Linear points to 1 mV resolution.
        self.assertEqual(
            generate_scan_points("Log", "10m", "100m", 5, unit="V", decimals=3),
            ["10m", "18m", "32m", "56m", "100m"],
        )

        # Step <-> Points integer rounding & snapped step calculation.
        steps, actual_step = linear_points_from_step(0, 0.001, 0.0003)
        self.assertEqual(steps, 4)
        self.assertAlmostEqual(float(actual_step), 0.001 / 3)
        log_steps, actual_ppd = log_points_from_per_decade(1e-5, 1e-3, 5.2)
        self.assertEqual(log_steps, 11)
        self.assertAlmostEqual(actual_ppd, 5.0)

        # Settings limit sync resolves symmetric and unipolar bounds.
        low, high, summary = resolve_module_point_bounds(current_spec, {"max_current": "1m"})
        self.assertAlmostEqual(low or 0.0, -1e-3)
        self.assertAlmostEqual(high or 0.0, 1e-3)
        self.assertIn("max_current", summary)
        v_low, v_high, _ = resolve_module_point_bounds(voltage_spec, {"max_voltage": "100m"})
        self.assertEqual(v_low, 0.0)
        self.assertAlmostEqual(v_high or 0.0, 0.1)


class ModuleCommandParserTests(unittest.TestCase):
    def test_module_scan_round_trips_without_installed_module(self) -> None:
        source = (
            'T Module Scan "test_meter" "scan_current" '
            '{"points":["1 mA","2 mA"],"settle_seconds":0}\n'
            "T     Measure\n"
            "T End Scan\n"
            "T End Sequence\n"
        )
        result = parse_sequence(source, "module-scan.seq")
        self.assertEqual(result.issues, ())
        command = result.document.commands[0]
        self.assertEqual(command.type, CommandType.MODULE_SCAN)
        self.assertEqual(command.module_id, "test_meter")
        self.assertEqual(command.module_command_id, "scan_current")
        self.assertEqual(command.children[0].type, CommandType.MEASURE)
        self.assertEqual(serialize_sequence(result.document), source)

        command.update_params(command.params)
        self.assertEqual(
            format_command(command),
            'Module Scan "test_meter" "scan_current" '
            '{"points":["1 mA","2 mA"],"settle_seconds":0}',
        )

    def test_malformed_or_nonfinite_module_json_is_an_error(self) -> None:
        malformed = parse_sequence(
            'T Module Command "test_meter" "set_current" []\nT End Sequence\n'
        )
        self.assertTrue(malformed.has_errors)
        self.assertIn("JSON object", malformed.issues[0].message)

        nonfinite = parse_sequence(
            'T Module Command "test_meter" "set_current" {"current":NaN}\n'
            "T End Sequence\n"
        )
        self.assertTrue(nonfinite.has_errors)
        self.assertIn("invalid JSON constant", nonfinite.issues[0].message)


class ModuleCommandWorkerTests(unittest.TestCase):
    def test_worker_exposes_metadata_and_dispatches_on_same_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "backend.py").write_text(
                "class Module:\n"
                "    columns = {'Value': ''}\n"
                "    sequence_commands = [\n"
                "        {\n"
                "            'id': 'set_current',\n"
                "            'label': 'Set Current',\n"
                "            'fields': [\n"
                "                {'name': 'current', 'type': 'float', 'default': 0.0}\n"
                "            ],\n"
                "        },\n"
                "    ]\n"
                "    def open(self, api):\n"
                "        return {}\n"
                "    def measure(self, slot, api):\n"
                "        return {'Value': 0}\n"
                "    def execute_sequence_command(self, command_id, parameters, api):\n"
                "        api.checkpoint()\n"
                "        return {'LastCommand': command_id, 'Current': parameters['current']}\n"
                "    def close(self, api):\n"
                "        return {}\n",
                encoding="utf-8",
            )
            descriptor = ModuleDescriptor(
                id="worker_meter",
                name="Worker Meter",
                version="1.0.0",
                path=root,
            )
            client = ModuleWorkerClient(descriptor)
            try:
                columns = client.start(timeout_seconds=2.0)
                self.assertEqual(columns[0].name, "Value")
                self.assertEqual(client.sequence_commands[0].command_id, "set_current")
                result = client.request(
                    "sequence_command",
                    {
                        "command_id": "set_current",
                        "parameters": {"current": 0.002},
                    },
                    event_handler=(
                        lambda message: (
                            {"state": "running"}
                            if message.get("kind") == "operation_state"
                            else {"system": {}}
                        )
                    ),
                    timeout_seconds=2.0,
                )
                self.assertEqual(result["LastCommand"], "set_current")
                self.assertEqual(result["Current"], 0.002)
            finally:
                client.close(timeout_seconds=1.0)

    def test_declared_commands_require_a_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "backend.py").write_text(
                "class Module:\n"
                "    columns = {'Value': ''}\n"
                "    sequence_commands = [{'id': 'set_value', 'fields': []}]\n"
                "    def open(self, api): return {}\n"
                "    def measure(self, slot, api): return {'Value': 0}\n"
                "    def close(self, api): return {}\n",
                encoding="utf-8",
            )
            client = ModuleWorkerClient(ModuleDescriptor(
                id="missing_handler",
                name="Missing Handler",
                version="1.0.0",
                path=root,
            ))
            with self.assertRaises(WorkerRequestError) as captured:
                client.start(timeout_seconds=2.0)
            self.assertEqual(captured.exception.code, "MODULE_WORKER_START_FAILED")
            self.assertIn("execute_sequence_command", str(captured.exception))


class ModuleCommandEngineTests(unittest.TestCase):
    def test_runtime_preflight_requires_enabled_matching_declaration(self) -> None:
        class Instruments:
            def __init__(self) -> None:
                self.config = load_simulated_config()

        descriptor = ModuleDescriptor(
            id="test_meter",
            name="Test Meter",
            version="1.0.0",
            path=ROOT,
        )
        service = MeasurementModuleService(
            (descriptor,),
            EventManager(),
            Instruments(),  # type: ignore[arg-type]
        )
        spec = normalize_module_commands("test_meter", DECLARATIONS)[0]
        command = spec.create()
        self.assertIn("must be Enabled", service.sequence_command_issues(command)[0])

        record = service.records["test_meter"]
        record.enabled = True
        record.sequence_commands = (spec,)
        self.assertEqual(service.sequence_command_issues(command), ())

        wrong_kind = Command(
            CommandType.MODULE_SCAN,
            dict(command.params),
            module_id="test_meter",
            module_command_id="set_current",
        )
        self.assertIn("must use Module Command", service.sequence_command_issues(wrong_kind)[0])

    def test_module_scan_sets_each_point_before_running_children(self) -> None:
        scan_spec, marker_spec = normalize_module_commands(
            "test_meter",
            [
                DECLARATIONS[1],
                {
                    "id": "mark",
                    "label": "Mark",
                    "fields": [],
                },
            ],
        )

        class Modules:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def sequence_command_spec(self, module_id, command_id):
                if module_id != "test_meter":
                    return None
                return {
                    "scan_current": scan_spec,
                    "mark": marker_spec,
                }.get(command_id)

            async def execute_sequence_command(self, command, parameters=None):
                self.calls.append((
                    command.module_command_id,
                    dict(command.params if parameters is None else parameters),
                ))
                return True

        class Instruments:
            control_ready = True

        class Logger:
            pass

        async def scenario() -> list[tuple[str, dict[str, object]]]:
            modules = Modules()
            engine = SequenceEngine(
                load_simulated_config(),
                Instruments(),  # type: ignore[arg-type]
                EventManager(),
                Logger(),  # type: ignore[arg-type]
                modules,  # type: ignore[arg-type]
            )
            engine.state = RunState.RUNNING
            scan = scan_spec.create()
            scan.children.append(marker_spec.create())
            await engine._scan_module(scan, ["scan"])
            return modules.calls

        calls = asyncio.run(scenario())
        self.assertEqual(
            [name for name, _ in calls],
            ["scan_current", "mark", "scan_current", "mark"],
        )
        self.assertEqual(calls[0][1]["current"], "1 mA")
        self.assertEqual(calls[2][1]["current"], "2 mA")


class ModuleCommandUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_generic_dialog_supports_bool_list_units_and_ranges(self) -> None:
        spec = normalize_module_commands("test_meter", DECLARATIONS)[1]
        command = spec.create()
        dialog = CommandDialog(command, spec)
        try:
            self.assertEqual(dialog.inputs["settle_seconds"].suffix(), " s")
            self.assertEqual(dialog.values()["points"], ["1 mA", "2 mA"])
        finally:
            dialog.close()

        command_spec = normalize_module_commands("test_meter", DECLARATIONS)[0]
        command_dialog = CommandDialog(command_spec.create(), command_spec)
        try:
            self.assertTrue(command_dialog.inputs["output"].isChecked())
            self.assertEqual(command_dialog.inputs["current"].maximum(), 0.01)
        finally:
            command_dialog.close()

    def test_module_scan_dialog_syncs_step_points_and_module_limits(self) -> None:
        from unittest.mock import patch

        scan_spec = normalize_module_commands("test_source", [{
            "id": "scan_current",
            "label": "Scan Current",
            "kind": "scan",
            "points_field": "points",
            "point_parameter": "current",
            "scan_modes": ["Linear", "Log", "List"],
            "point_unit": "A",
            "point_limits": {
                "setting": "max_current",
                "symmetric": True,
                "minimum": -0.07071,
                "maximum": 0.07071,
            },
            "fields": [
                {"name": "points", "label": "RMS points (A)", "type": "list", "default": ["100u", "200u", "500u"]},
                {"name": "settle_seconds", "label": "Settle time", "type": "float", "default": 3.0, "minimum": 0.0, "unit": "s"},
            ],
        }])[0]
        dialog = CommandDialog(
            scan_spec.create(),
            scan_spec,
            module_settings={"max_current": "1m"},
        )
        try:
            self.assertEqual(dialog.inputs["point_mode"].currentText(), "Linear")
            self.assertIn("-1mA to 1mA", dialog.limit_label.text())
            dialog.inputs["start"].setText("0")
            dialog.inputs["stop"].setText("1m")
            dialog.step_input.setText("300u")
            dialog._on_step_editing_finished()
            self.assertEqual(dialog.inputs["steps"].value(), 4)
            self.assertEqual(dialog.step_input.text(), "333.333333u")
            dialog.accept()
            self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
            self.assertEqual(
                dialog.values(),
                {"point_mode": "Linear", "start": "0", "stop": "1m", "steps": 4, "settle_seconds": 3.0},
            )
            # Exceeding synced max_current=1m is rejected before accept.
            dialog.reject()
            dialog.inputs["stop"].setText("2m")
            with patch("labcontrol.ui.dialogs.QMessageBox.warning") as warn:
                dialog.accept()
            warn.assert_called_once()
            self.assertEqual(dialog.result(), dialog.DialogCode.Rejected)
            # Log mode switches Step to Points / decade and rounds to integer points.
            dialog.inputs["point_mode"].setCurrentText("Log")
            dialog.inputs["start"].setText("10u")
            dialog.inputs["stop"].setText("1m")
            dialog.step_input.setText("5")
            dialog._on_step_editing_finished()
            self.assertEqual(dialog.inputs["steps"].value(), 11)
            self.assertEqual(dialog.step_input.text(), "5")
            dialog.accept()
            self.assertEqual(
                dialog.values(),
                {"point_mode": "Log", "start": "10u", "stop": "1m", "steps": 11, "settle_seconds": 3.0},
            )
        finally:
            dialog.close()

    def test_unavailable_line_is_red_until_matching_command_is_registered(self) -> None:
        command = normalize_module_commands("test_meter", DECLARATIONS)[0].create()
        editor = SequenceEditorWidget(SequenceDocument([command]))
        try:
            self.assertEqual(editor.list.item(0).background().color().name(), "#ffd8d8")
            editor.set_available_module_commands({("test_meter", "set_current")})
            self.assertNotEqual(editor.list.item(0).background().color().name(), "#ffd8d8")
        finally:
            editor.close()

    def test_frontend_custom_editor_returns_only_parameter_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "frontend.py").write_text(
                "from PySide6.QtWidgets import QWidget\n"
                "class Frontend(QWidget):\n"
                "    def __init__(self, api):\n"
                "        super().__init__()\n"
                "    def load(self, settings):\n"
                "        pass\n"
                "    def dump(self):\n"
                "        return {}\n"
                "    def edit_sequence_command(self, command_id, parameters):\n"
                "        result = dict(parameters)\n"
                "        result['edited_by'] = command_id\n"
                "        return result\n",
                encoding="utf-8",
            )
            owner = QWidget()
            module_window = ModuleWindow(
                ModuleDescriptor(
                    id="custom_editor",
                    name="Custom Editor",
                    version="1.0.0",
                    path=root,
                ),
                owner,
            )
            try:
                self.assertEqual(
                    module_window.edit_sequence_command(
                        "set_current",
                        {"current": 0.001},
                    ),
                    {
                        "current": 0.001,
                        "edited_by": "set_current",
                    },
                )
            finally:
                module_window.allow_application_close()
                module_window.close()
                owner.close()

    def test_main_window_adds_and_removes_direct_module_group(self) -> None:
        window = MainWindow(load_simulated_config())
        try:
            descriptor = ModuleDescriptor(
                id="test_meter",
                name="Test Meter",
                version="1.0.0",
                path=ROOT,
            )
            window.module_descriptors = (descriptor,)
            class FakeWindow:
                def update_runtime(self, *args):
                    del args

                def load_settings(self, *args, **kwargs):
                    del args, kwargs

                def show_in_front(self):
                    return None

                def hide(self):
                    return None

                def isHidden(self):  # noqa: N802
                    return False

                def settings(self):
                    return {}

                def allow_application_close(self):
                    return None

                def close(self):
                    return None

            window.module_windows["test_meter"] = FakeWindow()  # type: ignore[assignment]
            baseline = window.command_tree.topLevelItemCount()
            # 即使错误消息携带了声明，Disabled 状态也绝不能提前注册菜单。
            window._handle_module_state({
                "module_id": "test_meter",
                "enabled": False,
                "state": "disabled",
                "status": {},
                "message": "",
                "sequence_commands": DECLARATIONS,
            })
            self.assertEqual(window.command_tree.topLevelItemCount(), baseline)
            window._handle_module_state({
                "module_id": "test_meter",
                "enabled": True,
                "state": "enabled",
                "status": {},
                "message": "enabled",
                "sequence_commands": DECLARATIONS,
            })
            self.assertEqual(window.command_tree.topLevelItemCount(), baseline + 1)
            group = window.command_tree.topLevelItem(baseline)
            self.assertEqual(group.text(0), "Test Meter")
            self.assertEqual([group.child(i).text(0) for i in range(group.childCount())], [
                "Set Current",
                "Scan Current",
            ])
            window._handle_module_state({
                "module_id": "test_meter",
                "enabled": False,
                "state": "disabled",
                "status": {},
                "message": "disabled",
                "sequence_commands": [],
            })
            self.assertEqual(window.command_tree.topLevelItemCount(), baseline)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
