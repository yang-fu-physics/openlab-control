from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QFrame  # noqa: E402

from labcontrol.config import InstrumentReadingConfig, load_config  # noqa: E402
from labcontrol.models import (  # noqa: E402
    InstrumentActivity,
    InstrumentConnectionState,
    InstrumentKind,
    InstrumentMetric,
    InstrumentSnapshot,
    LabEvent,
    Severity,
)
from labcontrol.sequence.model import (  # noqa: E402
    SPECS_BY_TYPE,
    CommandType,
    SystemInstrumentCommandSpec,
)
from labcontrol.ui.dialogs import (  # noqa: E402
    AlertDialog,
    CommandDialog,
    ManualControlDialog,
)
from labcontrol.ui.trend import TrendCanvas  # noqa: E402
from labcontrol.ui.instrument_panels import (  # noqa: E402
    ControllerPanel,
    InstrumentPanelHost,
    ReadoutGridPanel,
    ReadoutPanel,
    SwitchPanel,
)


class InstrumentPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_monitor_readout_panel_is_display_only(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        host = InstrumentPanelHost((config.instrument("second_stage"),))
        panel = host.main_panels["second_stage"]
        self.assertIsInstance(panel, ReadoutPanel)
        emitted: list[str] = []
        host.controlRequested.connect(emitted.append)
        snapshot = InstrumentSnapshot(
            instrument_id="second_stage",
            display_name="2nd Stage",
            kind=InstrumentKind.MONITOR,
            timestamp=time.monotonic(),
            unit="K",
            current=4.2345,
            activity=InstrumentActivity.IDLE,
        )
        host.update_snapshot(snapshot)
        panel.show()
        QTest.mouseDClick(panel, Qt.MouseButton.LeftButton)
        self.assertEqual(emitted, [])
        self.assertEqual(panel.value_label.text(), "4.234 K")
        self.assertEqual(panel.name_label.text(), "2nd Stage")
        self.assertEqual(panel.minimumWidth(), 205)
        self.assertEqual(panel.maximumHeight(), 105)
        self.assertEqual(
            panel.value_label.objectName(),
            "panelValue",
        )
        self.assertFalse(hasattr(panel, "title_label"))
        self.assertFalse(hasattr(panel, "state_label"))
        self.assertFalse(hasattr(panel, "detail_label"))
        self.assertEqual(
            panel.findChildren(QFrame, "readoutCell"),
            [],
        )
        self.assertEqual(panel.cursor().shape(), Qt.CursorShape.ArrowCursor)
        trend = TrendCanvas()
        trend.add_snapshots({"second_stage": snapshot})
        self.assertEqual(len(trend.history["2nd Stage"]), 1)
        self.assertEqual(
            trend.history["2nd Stage"][0][0],
            snapshot.timestamp,
        )
        self.assertFalse(trend._redraw_timer.isActive())
        trend.close()
        host.close()

    def test_controller_auxiliary_readings_share_four_value_panel(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        base = config.instrument("temperature")
        instrument = replace(
            base,
            auxiliary_readings=(
                "second_stage",
                "heater_output",
                "heater_range",
            ),
            readings=(
                base.reading(base.main_reading),
                InstrumentReadingConfig("second_stage", "2nd Stage", "K", 3),
                InstrumentReadingConfig("heater_output", "Heater", "%", 2),
                InstrumentReadingConfig("heater_range", "Range"),
            ),
        )
        host = InstrumentPanelHost((instrument,))
        panel = host.main_panels["temperature"]
        snapshot = InstrumentSnapshot(
            instrument_id="temperature",
            display_name="Temperature",
            kind=InstrumentKind.TEMPERATURE,
            timestamp=time.monotonic(),
            unit="K",
            current=4.2,
            target=4.0,
            rate_per_minute=1.0,
            metrics={
                "second_stage": InstrumentMetric(
                    "2nd Stage", 20.1254, "K", 3
                ),
                "heater_output": InstrumentMetric(
                    "Heater", 12.345, "%", 2
                ),
                "heater_range": InstrumentMetric("Range", "LOW"),
            },
        )
        host.update_snapshot(snapshot)

        self.assertEqual(
            list(host.readout_panels),
            [("temperature", 0)],
        )
        readout = host.readout_panels[("temperature", 0)]
        self.assertIsInstance(readout, ReadoutGridPanel)
        self.assertEqual(
            readout.value_labels["second_stage"].text(),
            "20.125 K",
        )
        self.assertEqual(
            readout.value_labels["heater_output"].text(),
            "12.35 %",
        )
        self.assertEqual(
            readout.value_labels["heater_range"].text(),
            "LOW",
        )
        self.assertEqual(panel.maximumHeight(), 105)

        host.update_snapshot(
            InstrumentSnapshot(
                instrument_id="temperature",
                display_name="Temperature",
                kind=InstrumentKind.TEMPERATURE,
                timestamp=time.monotonic(),
                unit="K",
                connection_state=InstrumentConnectionState.DISCONNECTED,
            )
        )
        self.assertEqual(
            readout.value_labels["second_stage"].text(),
            "—",
        )
        self.assertEqual(len(host.readout_panels), 1)
        host.close()

    def test_readout_fifth_value_starts_a_panel_to_the_right(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        base = config.instrument("second_stage")
        instrument = replace(
            base,
            panel_template="readout_grid",
            auxiliary_readings=("a", "b", "c", "d"),
            readings=(
                base.reading(base.main_reading),
                InstrumentReadingConfig("a", "A", "K", 1),
                InstrumentReadingConfig("b", "B", "K", 1),
                InstrumentReadingConfig("c", "C", "K", 1),
                InstrumentReadingConfig("d", "D", "K", 1),
            ),
        )
        host = InstrumentPanelHost((instrument,))
        first = host.readout_panels[("second_stage", 0)]
        second = host.readout_panels[("second_stage", 1)]

        self.assertIs(host.main_panels["second_stage"], first)
        self.assertEqual(
            list(first.value_labels),
            ["value", "a", "b", "c"],
        )
        self.assertEqual(list(second.value_labels), ["d"])
        self.assertLess(host._row.indexOf(first), host._row.indexOf(second))
        host.close()

    def test_single_readout_does_not_display_auxiliary_readings(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        base = config.instrument("second_stage")
        instrument = replace(
            base,
            auxiliary_readings=("extra",),
            readings=(
                base.reading(base.main_reading),
                InstrumentReadingConfig("extra", "Extra", "K", 2),
            ),
        )
        host = InstrumentPanelHost((instrument,))

        panel = host.main_panels["second_stage"]
        self.assertIsInstance(panel, ReadoutPanel)
        self.assertEqual(panel.name_label.text(), "2nd Stage")
        self.assertEqual(host.readout_panels, {})
        host.close()

    def test_switch_panel_displays_state_and_emits_declared_action(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        instrument = replace(
            config.instrument("second_stage"),
            id="compressor",
            display_name="Compressor",
            panel_template="switch",
        )
        commands = (
            SystemInstrumentCommandSpec(
                "compressor",
                "compressor_on",
                "Compressor On",
            ),
            SystemInstrumentCommandSpec(
                "compressor",
                "compressor_off",
                "Compressor Off",
            ),
        )
        host = InstrumentPanelHost((instrument,), commands)
        panel = host.main_panels["compressor"]
        self.assertIsInstance(panel, SwitchPanel)
        self.assertFalse(panel.buttons["compressor_on"].isEnabled())
        requested: list[tuple[str, str]] = []
        host.actionRequested.connect(
            lambda instrument_id, command_id: requested.append(
                (instrument_id, command_id)
            )
        )

        host.update_snapshot(
            InstrumentSnapshot(
                instrument_id="compressor",
                display_name="Compressor",
                kind=InstrumentKind.MONITOR,
                timestamp=time.monotonic(),
                current=1.0,
            )
        )
        self.assertEqual(panel.value_label.text(), "On")
        self.assertTrue(panel.buttons["compressor_on"].isEnabled())
        QTest.mouseClick(
            panel.buttons["compressor_off"],
            Qt.MouseButton.LeftButton,
        )
        self.assertEqual(
            requested,
            [("compressor", "compressor_off")],
        )

        host.set_actions_enabled(False)
        self.assertFalse(panel.buttons["compressor_on"].isEnabled())
        host.close()

    def test_status_cards_keep_fixed_light_colors(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        panel = ControllerPanel(config.instrument("temperature"))
        self.assertEqual(panel.title_label.objectName(), "panelTitle")
        self.assertEqual(panel.value_label.objectName(), "panelValue")
        self.assertEqual(panel.detail_label.objectName(), "panelDetail")
        style = panel.styleSheet()
        self.assertIn("background: #ffffff", style)
        self.assertIn("color: #202124", style)
        self.assertIn("color: #6f6f6f", style)
        panel.close()

    def test_live_trend_coalesces_visible_redraws_and_stops_them_when_hidden(
        self,
    ) -> None:
        trend = TrendCanvas()
        snapshot = InstrumentSnapshot(
            instrument_id="temperature",
            display_name="Temperature",
            kind=InstrumentKind.TEMPERATURE,
            timestamp=123.0,
            unit="K",
            current=4.2,
        )
        trend.add_snapshots({"temperature": snapshot})
        self.assertFalse(trend._redraw_timer.isActive())

        trend.show()
        self.app.processEvents()
        self.assertTrue(trend._redraw_timer.isActive())
        QTest.qWait(trend.REDRAW_INTERVAL_MS + 50)
        self.assertFalse(trend._redraw_timer.isActive())

        for index in range(50):
            snapshot.timestamp += 0.2
            snapshot.current = 4.2 + index / 1000
            trend.add_snapshots({"temperature": snapshot})
        self.assertEqual(len(trend.history["Temperature"]), 51)
        self.assertTrue(trend._redraw_timer.isActive())

        trend.hide()
        self.app.processEvents()
        self.assertFalse(trend._redraw_timer.isActive())
        trend.close()

    def test_tile_distinguishes_reconnecting_and_faulted_states(
        self,
    ) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        panel = ControllerPanel(config.instrument("temperature"))
        snapshot = InstrumentSnapshot(
            "temperature",
            "Temperature",
            InstrumentKind.TEMPERATURE,
            time.monotonic(),
            "K",
            message="Retrying for up to 60 seconds",
            connection_state=InstrumentConnectionState.RECONNECTING,
        )
        panel.update_snapshot(snapshot)
        self.assertEqual(panel.state_label.text(), "Reconnecting")
        self.assertIn("Retrying", panel.detail_label.text())
        snapshot.connection_state = InstrumentConnectionState.FAULTED
        snapshot.message = "Reconnect deadline exceeded"
        panel.update_snapshot(snapshot)
        self.assertEqual(panel.state_label.text(), "Faulted")
        self.assertIn("deadline", panel.detail_label.text())
        panel.close()

    def test_secondary_temperature_tile_and_dialog_are_display_only(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        secondary = replace(
            config.instrument("temperature"),
            id="temperature_backup",
            control_enabled=False,
        )
        panel = ControllerPanel(secondary)
        emitted: list[str] = []
        panel.controlRequested.connect(emitted.append)
        panel.update_snapshot(
            InstrumentSnapshot(
                secondary.id,
                secondary.display_name,
                secondary.kind,
                time.monotonic(),
                secondary.unit,
                10.0,
                10.0,
                1.0,
            )
        )
        panel.show()
        QTest.mouseDClick(panel, Qt.MouseButton.LeftButton)
        self.assertEqual(emitted, [])
        self.assertIn("Display only", panel.detail_label.text())
        self.assertEqual(panel.cursor().shape(), Qt.CursorShape.ArrowCursor)
        with self.assertRaises(ValueError):
            ManualControlDialog(secondary)
        panel.close()

    def test_alert_dialog_is_deleted_after_close(self) -> None:
        dialog = AlertDialog(
            LabEvent(
                key="instrument|FAULT|",
                severity=Severity.ERROR,
                source="instrument",
                code="FAULT",
                message="fault",
            )
        )
        self.assertTrue(
            dialog.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        )
        dialog.close()

    def test_temperature_and_oe_field_use_requested_precision(self) -> None:
        now = time.monotonic()
        temperature = InstrumentSnapshot(
            "temperature", "Temperature", InstrumentKind.TEMPERATURE, now, "K",
            300.1236, 299.9, 10.0, InstrumentActivity.MOVING,
        )
        field = InstrumentSnapshot(
            "field", "Magnetic Field", InstrumentKind.FIELD, now, "Oe",
            123.456, 200.0, 5000.0, InstrumentActivity.MOVING,
        )
        config = load_config(ROOT / "configs" / "default.toml")
        temperature_panel = ControllerPanel(config.instrument("temperature"))
        field_panel = ControllerPanel(config.instrument("field"))
        temperature_panel.update_snapshot(temperature)
        field_panel.update_snapshot(field)
        self.assertEqual(temperature_panel.value_label.text(), "300.124 K")
        self.assertIn("Target 299.900 K", temperature_panel.detail_label.text())
        self.assertEqual(field_panel.value_label.text(), "123.46 Oe")
        self.assertIn("Target 200.00 Oe", field_panel.detail_label.text())
        self.assertIn("5000.00 Oe/min", field_panel.detail_label.text())
        temperature_panel.close()
        field_panel.close()

    def test_control_dialogs_match_unit_precision_and_convert_field_unit(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        temperature_config = next(instrument for instrument in config.instruments if instrument.id == "temperature")
        field_config = next(instrument for instrument in config.instruments if instrument.id == "field")
        temperature_dialog = ManualControlDialog(temperature_config)
        field_dialog = ManualControlDialog(field_config)
        self.assertEqual(temperature_dialog.target_input.decimals(), 3)
        self.assertEqual(field_dialog.target_input.decimals(), 2)

        spec = SPECS_BY_TYPE[CommandType.SET_FIELD]
        command = spec.create()
        command.params["target"] = 10000.0
        command_dialog = CommandDialog(command, spec)
        self.assertEqual(command_dialog.inputs["target"].decimals(), 2)
        command_dialog.inputs["unit"].setCurrentText("T")
        self.assertEqual(command_dialog.inputs["target"].decimals(), 6)
        self.assertAlmostEqual(command_dialog.inputs["target"].value(), 1.0)

        scan_spec = SPECS_BY_TYPE[CommandType.SCAN_FIELD]
        scan_dialog = CommandDialog(scan_spec.create(), scan_spec)
        scan_dialog.inputs["unit"].setCurrentText("T")
        self.assertAlmostEqual(scan_dialog.inputs["start"].value(), 0.0)
        self.assertAlmostEqual(scan_dialog.inputs["stop"].value(), 1.0)
        self.assertAlmostEqual(scan_dialog.inputs["rate"].value(), 0.5)
        polarity = scan_dialog.inputs["nearest_polarity"]
        self.assertIsInstance(polarity, QCheckBox)
        self.assertFalse(polarity.isChecked())
        polarity.setChecked(True)
        self.assertTrue(scan_dialog.values()["nearest_polarity"])
        temperature_dialog.close()
        field_dialog.close()
        command_dialog.close()
        scan_dialog.close()

    def test_temperature_scan_dialog_switches_between_linear_and_list_points(self) -> None:
        spec = SPECS_BY_TYPE[CommandType.SCAN_TEMPERATURE]
        dialog = CommandDialog(spec.create(), spec)
        self.assertFalse(dialog.inputs["start"].isHidden())
        self.assertFalse(dialog.inputs["stop"].isHidden())
        self.assertFalse(dialog.inputs["steps"].isHidden())
        self.assertTrue(dialog.inputs["points"].isHidden())

        dialog.inputs["point_mode"].setCurrentText("List")
        self.assertTrue(dialog.inputs["start"].isHidden())
        self.assertTrue(dialog.inputs["stop"].isHidden())
        self.assertTrue(dialog.inputs["steps"].isHidden())
        self.assertFalse(dialog.inputs["points"].isHidden())
        dialog.inputs["points"].setText("300, 299.9, 300")
        dialog.accept()
        values = dialog.values()
        self.assertEqual(values["point_mode"], "List")
        self.assertEqual(values["points"], "[300, 299.9, 300]")
        dialog.close()

    def test_sequence_dialog_uses_configured_target_and_rate_limits(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")

        temperature_spec = SPECS_BY_TYPE[CommandType.SET_TEMPERATURE]
        temperature_dialog = CommandDialog(
            temperature_spec.create(),
            temperature_spec,
            instrument_configs=config.instruments,
        )
        self.assertAlmostEqual(temperature_dialog.inputs["target"].minimum(), 1.8)
        self.assertAlmostEqual(temperature_dialog.inputs["target"].maximum(), 400.0)
        self.assertAlmostEqual(temperature_dialog.inputs["rate"].maximum(), 30.0)
        self.assertIn("Configured limits (temperature)", temperature_dialog.limit_label.text())

        temperature_scan_spec = SPECS_BY_TYPE[CommandType.SCAN_TEMPERATURE]
        temperature_scan_dialog = CommandDialog(
            temperature_scan_spec.create(),
            temperature_scan_spec,
            instrument_configs=config.instruments,
        )
        for name in ("start", "stop"):
            self.assertAlmostEqual(temperature_scan_dialog.inputs[name].minimum(), 1.8)
            self.assertAlmostEqual(temperature_scan_dialog.inputs[name].maximum(), 400.0)
        self.assertAlmostEqual(temperature_scan_dialog.inputs["rate"].maximum(), 30.0)

        field_spec = SPECS_BY_TYPE[CommandType.SET_FIELD]
        field_dialog = CommandDialog(
            field_spec.create(),
            field_spec,
            instrument_configs=config.instruments,
        )
        self.assertAlmostEqual(field_dialog.inputs["target"].minimum(), -90000.0)
        self.assertAlmostEqual(field_dialog.inputs["target"].maximum(), 90000.0)
        self.assertAlmostEqual(field_dialog.inputs["rate"].maximum(), 10000.0)
        field_dialog.inputs["unit"].setCurrentText("T")
        self.assertAlmostEqual(field_dialog.inputs["target"].minimum(), -9.0)
        self.assertAlmostEqual(field_dialog.inputs["target"].maximum(), 9.0)
        self.assertAlmostEqual(field_dialog.inputs["rate"].maximum(), 1.0)
        self.assertIn("-9.000000 to 9.000000 T", field_dialog.limit_label.text())

        field_scan_spec = SPECS_BY_TYPE[CommandType.SCAN_FIELD]
        field_scan_dialog = CommandDialog(
            field_scan_spec.create(),
            field_scan_spec,
            instrument_configs=config.instruments,
        )
        for name in ("start", "stop"):
            self.assertAlmostEqual(field_scan_dialog.inputs[name].minimum(), -90000.0)
            self.assertAlmostEqual(field_scan_dialog.inputs[name].maximum(), 90000.0)
        self.assertAlmostEqual(field_scan_dialog.inputs["rate"].maximum(), 10000.0)

        temperature_dialog.close()
        temperature_scan_dialog.close()
        field_dialog.close()
        field_scan_dialog.close()

    def test_sequence_dialog_selects_custom_instrument_ids_and_their_limits(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        temperature = next(
            instrument for instrument in config.instruments
            if instrument.kind is InstrumentKind.TEMPERATURE
        )
        custom_instruments = (
            replace(temperature, id="cryostat_primary"),
            replace(
                temperature,
                id="cryostat_backup",
                control_enabled=False,
                min_value=2.0,
                max_value=350.0,
                max_rate_per_minute=12.0,
            ),
        )
        spec = SPECS_BY_TYPE[CommandType.SET_TEMPERATURE]
        dialog = CommandDialog(
            spec.create(),
            spec,
            instrument_configs=custom_instruments,
        )
        instrument_input = dialog.inputs["instrument_id"]
        self.assertEqual(instrument_input.currentText(), "cryostat_primary")
        self.assertEqual(instrument_input.count(), 1)
        self.assertEqual(dialog.inputs["target"].maximum(), 400.0)
        self.assertEqual(dialog.values()["instrument_id"], "cryostat_primary")
        dialog.close()

    def test_set_datafile_dialog_uses_native_file_chooser_for_save_and_open(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            spec = SPECS_BY_TYPE[CommandType.SET_DATAFILE]
            dialog = CommandDialog(
                spec.create(),
                spec,
                data_directory=directory,
            )
            self.assertIsNotNone(dialog.datafile_browse_button)

            chosen_without_suffix = directory / "new measurement"
            with patch(
                "labcontrol.ui.dialogs.QFileDialog.getSaveFileName",
                return_value=(
                    str(chosen_without_suffix),
                    "Data (*.dat)",
                ),
            ) as save_dialog:
                dialog._browse_datafile()
            self.assertEqual(
                dialog.inputs["path"].text(),
                str(chosen_without_suffix.with_suffix(".dat").resolve()),
            )
            self.assertEqual(
                dialog.inputs["path_scope"].currentText(),
                "Custom folder",
            )
            save_dialog.assert_called_once()

            existing = directory / "existing.dat"
            existing.write_text("[Data]\n", encoding="utf-8")
            dialog.inputs["mode"].setCurrentText("open")
            with patch(
                "labcontrol.ui.dialogs.QFileDialog.getOpenFileName",
                return_value=(str(existing), "Data (*.dat)"),
            ) as open_dialog:
                dialog.datafile_browse_button.click()
            self.assertEqual(
                dialog.inputs["path"].text(),
                str(existing.resolve()),
            )
            open_dialog.assert_called_once()
            dialog.close()

    def test_temperature_list_dialog_rejects_points_outside_configured_limits(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        spec = SPECS_BY_TYPE[CommandType.SCAN_TEMPERATURE]
        dialog = CommandDialog(spec.create(), spec, instrument_configs=config.instruments)
        dialog.inputs["point_mode"].setCurrentText("List")
        dialog.inputs["points"].setText("300, 500")

        with patch("labcontrol.ui.dialogs.QMessageBox.warning") as warning:
            dialog.accept()

        self.assertEqual(dialog.result(), dialog.DialogCode.Rejected)
        warning.assert_called_once()
        self.assertIn("outside the configured range", warning.call_args.args[2])
        dialog.inputs["points"].setText("300, 1.8")
        dialog.accept()
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
