from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from configuration_fixtures import load_simulated_config  # noqa: E402
from labcontrol.config import (  # noqa: E402
    InstrumentPanelConfig,
    InstrumentReadingConfig,
)
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.instruments.base import InstrumentError, SystemInstrument  # noqa: E402
from labcontrol.instruments.worker import (  # noqa: E402
    InProcessInstrumentClient,
    InstrumentWorkerClient,
    InstrumentWorkerSpec,
    IsolatedInstrumentClient,
)
from labcontrol.models import (  # noqa: E402
    InstrumentControlState,
    InstrumentKind,
    InstrumentMetric,
    InstrumentSnapshot,
)
from labcontrol.sequence.model import (  # noqa: E402
    SPECS_BY_TYPE,
    CommandType,
    SystemInstrumentCommandSpec,
)
from labcontrol.sequence.parser import parse_sequence  # noqa: E402
from labcontrol.ui.dialogs import CommandDialog, ManualControlDialog  # noqa: E402
from labcontrol.ui.instrument_panels import (  # noqa: E402
    ControllerPanel,
    InstrumentPanelHost,
    ReadoutPanel,
    SwitchPanel,
)


class _RecordingClient:
    enforces_timeouts = False
    pid = None

    def __init__(self) -> None:
        self.open_count = 0
        self.set_controls: list[str] = []
        self.hold_controls: list[str] = []

    async def event_responses(self):
        return ()

    async def open(self) -> None:
        self.open_count += 1

    async def close(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def force_stop(self, timeout_seconds: float = 0.25) -> None:
        del timeout_seconds

    async def read_status(self) -> dict[str, object]:
        return {
            "value": 300.0,
            "target": 300.0,
            "rate": 1.0,
            "moving": False,
            "auxiliary": {"heater": 12.5},
        }

    async def read_measurement(self) -> dict[str, object]:
        return {"value": 300.0}

    async def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        control: str,
    ) -> None:
        del value, rate_per_minute, mode
        self.set_controls.append(control)

    async def hold(self, *, control: str) -> None:
        self.hold_controls.append(control)


class _DualControlClient(_RecordingClient):
    async def read_status(self) -> dict[str, object]:
        return {
            "value": 300.0,
            "auxiliary": {"shield": 20.0},
            "controls": {
                "loop_1": {
                    "target": 300.0,
                    "rate": 1.0,
                    "moving": False,
                    "ready": True,
                },
                "loop_2": {
                    "target": 25.0,
                    "rate": 0.5,
                    "moving": True,
                    "ready": False,
                },
            },
        }


def _multi_panel_config():
    config = load_simulated_config()
    base = config.instrument("temperature")
    controller = replace(
        base.panels[0],
        control_id="loop_1",
        order=2,
    )
    readout = InstrumentPanelConfig(
        id="heater",
        instrument_id=base.id,
        display_name="Heater",
        template="readout",
        enabled=True,
        order=1,
        readings=("heater",),
    )
    instrument = replace(
        base,
        panels=(controller, readout),
        auxiliary_readings=("heater",),
        readings=(
            *base.readings,
            InstrumentReadingConfig("heater", "Heater", "%", 1),
        ),
    )
    return replace(config, instrument_instances=(instrument,))


def _dual_control_config():
    config = load_simulated_config()
    base = config.instrument("temperature")
    main = replace(base.panels[0], control_id="loop_1", order=1)
    shield = replace(
        main,
        id="shield",
        display_name="Shield Temperature",
        role="none",
        control_id="loop_2",
        reading="shield",
        readings=("shield",),
        order=2,
        default_rate_per_minute=0.5,
    )
    instrument = replace(
        base,
        panels=(main, shield),
        auxiliary_readings=("shield",),
        readings=(
            *base.readings,
            InstrumentReadingConfig(
                "shield",
                "Shield Temperature",
                "K",
                2,
            ),
        ),
    )
    return replace(config, instrument_instances=(instrument,))


class InstrumentRuntimeV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_one_physical_instance_opens_once_and_routes_control_by_role(self) -> None:
        async def scenario() -> None:
            config = _multi_panel_config()
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            client = _RecordingClient()
            manager.instruments["temperature"] = client

            await manager.connect_all()
            snapshots = await manager.poll_all()
            measurement = await manager.poll_measurement_all()
            self.assertEqual(
                manager.latest["temperature"].metrics["heater"].value,
                12.5,
            )
            self.assertEqual(manager.latest["temperature"].target, 300.0)
            self.assertEqual(client.open_count, 1)
            self.assertEqual(tuple(manager.instruments), ("temperature",))
            self.assertEqual(
                manager.resolve_control_panel(
                    InstrumentKind.TEMPERATURE
                ).control_id,
                "loop_1",
            )

            applied = await manager.set_target_by_kind(
                InstrumentKind.TEMPERATURE,
                250.0,
                2.0,
            )
            self.assertTrue(applied)
            await manager.hold_instrument(
                "temperature",
                control="loop_1",
            )
            self.assertEqual(client.set_controls, ["loop_1"])
            self.assertEqual(client.hold_controls, ["loop_1"])
            self.assertEqual(snapshots["temperature"].metrics["heater"].value, 12.5)
            self.assertIsNone(
                measurement["temperature"].metrics["heater"].value
            )
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_status_fans_out_in_global_panel_order(self) -> None:
        config = _multi_panel_config()
        instrument = config.instrument("temperature")
        host = InstrumentPanelHost(
            config.instrument_instances,
            config.panels,
        )
        self.assertEqual(
            tuple(host.panels),
            ("temperature.heater", "temperature.main"),
        )
        snapshot = asyncio.run(self._snapshot(config))
        host.update_snapshot(snapshot)
        readout = host.panels["temperature.heater"]
        controller = host.panels["temperature.main"]
        self.assertIsInstance(readout, ReadoutPanel)
        self.assertIsInstance(controller, ControllerPanel)
        self.assertEqual(readout.value_label.text(), "12.5 %")
        self.assertEqual(controller.value_label.text(), "300.000 K")
        self.assertEqual(instrument.id, snapshot.instrument_id)
        host.close()

    def test_two_controls_share_one_worker_and_keep_independent_state(self) -> None:
        async def scenario() -> None:
            config = _dual_control_config()
            instrument = config.instrument("temperature")
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            client = _DualControlClient()
            manager.instruments[instrument.id] = client
            await manager.connect_all()
            snapshot = (await manager.poll_all())[instrument.id]

            self.assertEqual(client.open_count, 1)
            self.assertEqual(snapshot.target, 300.0)
            self.assertEqual(snapshot.controls["main"].current, 300.0)
            self.assertEqual(snapshot.controls["main"].target, 300.0)
            self.assertEqual(snapshot.controls["shield"].current, 20.0)
            self.assertEqual(snapshot.controls["shield"].target, 25.0)

            host = InstrumentPanelHost(
                config.instrument_instances,
                config.panels,
            )
            host.update_snapshot(snapshot)
            main_panel = host.panels["temperature.main"]
            shield_panel = host.panels["temperature.shield"]
            self.assertEqual(main_panel.value_label.text(), "300.000 K")
            self.assertIn("Target 300.000 K", main_panel.detail_label.text())
            self.assertEqual(shield_panel.value_label.text(), "20.00 K")
            self.assertIn("Target 25.00 K", shield_panel.detail_label.text())

            dialog = ManualControlDialog(
                instrument,
                instrument.panel("shield"),
            )
            dialog.update_snapshot(snapshot)
            self.assertEqual(dialog.current_label.text(), "Current: 20.00 K")
            self.assertEqual(dialog.target_input.value(), 25.0)

            await manager.set_target(
                instrument.id,
                30.0,
                0.5,
                control="loop_2",
            )
            await manager.set_target_by_kind(
                InstrumentKind.TEMPERATURE,
                280.0,
                2.0,
            )
            self.assertEqual(client.set_controls, ["loop_2", "loop_1"])
            latest = manager.latest[instrument.id]
            self.assertEqual(latest.controls["shield"].target, 30.0)
            self.assertEqual(latest.controls["main"].target, 280.0)
            self.assertEqual(latest.target, 280.0)

            dialog.close()
            host.close()
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_multiple_controls_require_explicit_backend_control_states(self) -> None:
        config = _dual_control_config()
        manager = InstrumentManager(
            config,
            EventManager(),
            isolate_processes=False,
        )
        with self.assertRaises(InstrumentError) as raised:
            manager._snapshot_from_reading(
                "temperature",
                {
                    "value": 300.0,
                    "target": 300.0,
                    "rate": 1.0,
                    "auxiliary": {"shield": 20.0},
                },
                measurement=False,
            )
        self.assertEqual(raised.exception.code, "INVALID_INSTRUMENT_READING")

    def test_hold_all_calls_each_physical_control_once(self) -> None:
        async def scenario() -> None:
            config = _dual_control_config()
            instrument = config.instrument("temperature")
            duplicate = replace(
                instrument.panel("main"),
                id="main_copy",
                display_name="Sample Temperature Copy",
                role="none",
                order=3,
            )
            instrument = replace(
                instrument,
                panels=(*instrument.panels, duplicate),
            )
            config = replace(config, instrument_instances=(instrument,))
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            client = _DualControlClient()
            manager.instruments[instrument.id] = client
            await manager.connect_all()
            await manager.poll_all()
            self.assertTrue(await manager.hold_all())
            self.assertEqual(client.hold_controls, ["loop_1", "loop_2"])
            await manager.disconnect_all()

        asyncio.run(scenario())

    def test_switch_uses_its_configured_reading(self) -> None:
        config = _multi_panel_config()
        base = config.instrument("temperature")
        state = InstrumentReadingConfig("output", "Output", "", None)
        switch = InstrumentPanelConfig(
            id="output",
            instrument_id=base.id,
            display_name="Output",
            template="switch",
            enabled=True,
            order=1,
            reading="output",
            readings=("output",),
            commands=("output_on",),
        )
        instrument = replace(
            base,
            panels=(switch,),
            readings=(*base.readings, state),
        )
        host = InstrumentPanelHost(
            (instrument,),
            (switch,),
            (
                SystemInstrumentCommandSpec(
                    instrument.id,
                    "output_on",
                    "Output On",
                ),
            ),
        )
        snapshot = InstrumentSnapshot(
            instrument_id=instrument.id,
            display_name=instrument.display_name,
            kind=instrument.kind,
            timestamp=time.monotonic(),
            current=0.0,
            metrics={
                "output": InstrumentMetric("Output", True),
            },
        )
        host.update_snapshot(snapshot)
        panel = host.panels["temperature.output"]
        self.assertIsInstance(panel, SwitchPanel)
        self.assertEqual(panel.value_label.text(), "On")
        host.close()

    def test_manual_dialog_emits_the_selected_control(self) -> None:
        config = _multi_panel_config()
        instrument = config.instrument("temperature")
        panel = instrument.panel("main")
        dialog = ManualControlDialog(instrument, panel)
        dialog.update_snapshot(
            InstrumentSnapshot(
                instrument_id=instrument.id,
                display_name=instrument.display_name,
                kind=instrument.kind,
                timestamp=time.monotonic(),
                unit="K",
                current=300.0,
                controls={
                    panel.id: InstrumentControlState(
                        current=300.0,
                        target=300.0,
                        rate_per_minute=1.0,
                    )
                },
            )
        )
        sets: list[tuple[object, ...]] = []
        holds: list[tuple[str, str]] = []
        self.assertFalse(hasattr(dialog, "mode_input"))
        dialog.setRequested.connect(lambda *values: sets.append(values))
        dialog.holdRequested.connect(
            lambda instrument_id, control: holds.append(
                (instrument_id, control)
            )
        )
        dialog.apply_button.click()
        dialog.hold_button.click()
        self.assertEqual(sets[0][0:2], ("temperature", "loop_1"))
        self.assertEqual(
            sets[0][2:],
            (dialog.target_input.value(), dialog.rate_input.value()),
        )
        self.assertEqual(holds, [("temperature", "loop_1")])
        dialog.close()

    def test_standard_command_dialog_uses_role_limits_without_instrument_field(
        self,
    ) -> None:
        config = _multi_panel_config()
        spec = SPECS_BY_TYPE[CommandType.SET_TEMPERATURE]
        dialog = CommandDialog(
            spec.create(),
            spec,
            instrument_configs=config.instrument_instances,
        )
        panel = config.instrument("temperature").panel("main")
        self.assertNotIn("instrument_id", dialog.inputs)
        self.assertEqual(dialog.inputs["target"].minimum(), panel.min_value)
        self.assertEqual(dialog.inputs["target"].maximum(), panel.max_value)
        self.assertEqual(
            dialog.inputs["rate"].maximum(),
            panel.max_rate_per_minute,
        )
        self.assertIn(panel.key, dialog.limit_label.text())
        dialog.close()

    async def _snapshot(self, config):
        manager = InstrumentManager(
            config,
            EventManager(),
            isolate_processes=False,
        )
        manager.instruments["temperature"] = _RecordingClient()
        await manager.connect_all()
        snapshot = (await manager.poll_all())["temperature"]
        await manager.disconnect_all()
        return snapshot

    def test_clean_configuration_has_no_workers_or_panels(self) -> None:
        config = replace(load_simulated_config(), instrument_instances=())

        async def scenario() -> None:
            manager = InstrumentManager(
                config,
                EventManager(),
                isolate_processes=False,
            )
            await manager.connect_all()
            self.assertEqual(await manager.poll_all(), {})
            await manager.disconnect_all()

        asyncio.run(scenario())
        host = InstrumentPanelHost((), ())
        self.assertEqual(host.panels, {})
        host.close()

    def test_removed_sequence_instrument_suffix_is_an_error(self) -> None:
        result = parse_sequence(
            "T Set Temperature 10.000 K at 1.000 K/min in Settle mode "
            'using instrument "other"\nEnd Sequence\n'
        )
        self.assertEqual(result.document.commands[0].type, CommandType.UNKNOWN)
        self.assertEqual(result.issues[0].level, "error")
        self.assertIn("not supported", result.issues[0].message)

    def test_isolated_worker_passes_control_as_keyword(self) -> None:
        async def scenario() -> None:
            config = _multi_panel_config()
            instrument = config.instrument("temperature")
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                (directory / "backend.py").write_text(
                    "from labcontrol.instruments.base import SystemInstrument\n"
                    "class Driver(SystemInstrument):\n"
                    "    def open(self): pass\n"
                    "    def close(self): pass\n"
                    "    def read_status(self): return {'value': 300.0}\n"
                    "    def set_target(self, value, rate_per_minute, mode='Settle', *, control):\n"
                    "        assert control == 'loop_1'\n"
                    "    def hold(self, *, control):\n"
                    "        assert control == 'loop_1'\n",
                    encoding="utf-8",
                )
                spec = InstrumentWorkerSpec(
                    instrument_config=instrument,
                    simulation_speed=1.0,
                    instrument_id="keyword_test",
                    backend="backend:Driver",
                    instrument_directory=str(directory),
                )
                client = IsolatedInstrumentClient(
                    InstrumentWorkerClient(spec),
                    startup_timeout_seconds=2.0,
                    operation_timeout_seconds=2.0,
                    shutdown_timeout_seconds=2.0,
                )
                await client.open()
                await client.set_target(
                    250.0,
                    1.0,
                    control="loop_1",
                )
                await client.hold(control="loop_1")
                await client.shutdown()

        asyncio.run(scenario())

    def test_control_is_keyword_only_across_runtime_contract(self) -> None:
        for owner in (
            SystemInstrument,
            IsolatedInstrumentClient,
            InProcessInstrumentClient,
            InstrumentManager,
        ):
            with self.subTest(owner=owner.__name__, operation="set_target"):
                parameter = inspect.signature(owner.set_target).parameters[
                    "control"
                ]
                self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
            with self.subTest(owner=owner.__name__, operation="hold"):
                method = (
                    owner.hold_instrument
                    if owner is InstrumentManager
                    else owner.hold
                )
                parameter = inspect.signature(method).parameters["control"]
                self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)


if __name__ == "__main__":
    unittest.main()
