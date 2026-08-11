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
from PySide6.QtWidgets import QApplication, QCheckBox  # noqa: E402

from labcontrol.config import load_config  # noqa: E402
from labcontrol.models import (  # noqa: E402
    DeviceActivity,
    DeviceConnectionState,
    DeviceKind,
    DeviceMetric,
    DeviceRole,
    DeviceSnapshot,
    LabEvent,
    Severity,
)
from labcontrol.sequence.model import SPECS_BY_TYPE, CommandType  # noqa: E402
from labcontrol.ui.dialogs import (  # noqa: E402
    AlertDialog,
    CommandDialog,
    ManualControlDialog,
)
from labcontrol.ui.trend import TrendCanvas  # noqa: E402
from labcontrol.ui.widgets import StatusTile  # noqa: E402


class StatusTileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_monitor_tile_is_display_only(self) -> None:
        tile = StatusTile("second_stage", "2nd Stage", DeviceKind.MONITOR)
        emitted: list[str] = []
        tile.doubleClicked.connect(emitted.append)
        snapshot = DeviceSnapshot(
            device_id="second_stage",
            display_name="2nd Stage",
            kind=DeviceKind.MONITOR,
            timestamp=time.monotonic(),
            connected=True,
            unit="K",
            current=4.2345,
            activity=DeviceActivity.IDLE,
        )
        tile.update_snapshot(snapshot)
        tile.show()
        QTest.mouseDClick(tile, Qt.MouseButton.LeftButton)
        self.assertEqual(emitted, [])
        self.assertEqual(tile.value_label.text(), "4.234 K")
        self.assertEqual(tile.state_label.text(), "Monitoring")
        self.assertIn("Display only", tile.detail_label.text())
        self.assertEqual(tile.cursor().shape(), Qt.CursorShape.ArrowCursor)
        trend = TrendCanvas()
        trend.add_snapshots({"second_stage": snapshot})
        self.assertEqual(len(trend.history["2nd Stage"]), 1)
        self.assertEqual(
            trend.history["2nd Stage"][0][0],
            snapshot.timestamp,
        )
        self.assertFalse(trend._redraw_timer.isActive())
        trend.close()
        tile.close()

    def test_temperature_tile_shows_same_connection_auxiliary_metrics(self) -> None:
        tile = StatusTile(
            "temperature",
            "Temperature",
            DeviceKind.TEMPERATURE,
        )
        tile.update_snapshot(
            DeviceSnapshot(
                device_id="temperature",
                display_name="Temperature",
                kind=DeviceKind.TEMPERATURE,
                timestamp=time.monotonic(),
                connected=True,
                unit="K",
                current=4.2,
                target=4.0,
                rate_per_minute=1.0,
                metrics=(
                    DeviceMetric("second_stage", "2nd Stage", 20.1254, "K", 3),
                    DeviceMetric("heater_output", "Heater", 12.345, "%", 2),
                    DeviceMetric("heater_range", "Range", "LOW"),
                ),
            )
        )
        self.assertFalse(tile.metrics_label.isHidden())
        self.assertEqual(
            tile.metrics_label.text(),
            "2nd Stage 20.125 K · Heater 12.35 %\nRange LOW",
        )
        tile.close()

    def test_live_trend_coalesces_visible_redraws_and_stops_them_when_hidden(
        self,
    ) -> None:
        trend = TrendCanvas()
        snapshot = DeviceSnapshot(
            device_id="temperature",
            display_name="Temperature",
            kind=DeviceKind.TEMPERATURE,
            timestamp=123.0,
            connected=True,
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
        tile = StatusTile(
            "temperature",
            "Temperature",
            DeviceKind.TEMPERATURE,
        )
        snapshot = DeviceSnapshot(
            "temperature",
            "Temperature",
            DeviceKind.TEMPERATURE,
            time.monotonic(),
            False,
            "K",
            message="Retrying for up to 60 seconds",
            connection_state=DeviceConnectionState.RECONNECTING,
        )
        tile.update_snapshot(snapshot)
        self.assertEqual(tile.state_label.text(), "Reconnecting")
        self.assertIn("Retrying", tile.detail_label.text())
        snapshot.connection_state = DeviceConnectionState.FAULTED
        snapshot.message = "Reconnect deadline exceeded"
        tile.update_snapshot(snapshot)
        self.assertEqual(tile.state_label.text(), "Faulted")
        self.assertIn("deadline", tile.detail_label.text())
        tile.close()

    def test_secondary_temperature_tile_and_dialog_are_display_only(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        secondary = replace(
            config.device("temperature"),
            id="temperature_backup",
            role=DeviceRole.SECONDARY,
            control_enabled=False,
        )
        tile = StatusTile(
            secondary.id,
            secondary.display_name,
            secondary.kind,
            secondary.control_enabled,
        )
        emitted: list[str] = []
        tile.doubleClicked.connect(emitted.append)
        tile.update_snapshot(
            DeviceSnapshot(
                secondary.id,
                secondary.display_name,
                secondary.kind,
                time.monotonic(),
                True,
                secondary.unit,
                10.0,
                10.0,
                1.0,
            )
        )
        tile.show()
        QTest.mouseDClick(tile, Qt.MouseButton.LeftButton)
        self.assertEqual(emitted, [])
        self.assertIn("Display only", tile.detail_label.text())
        self.assertEqual(tile.cursor().shape(), Qt.CursorShape.ArrowCursor)
        with self.assertRaises(ValueError):
            ManualControlDialog(secondary)
        tile.close()

    def test_alert_dialog_is_deleted_after_close(self) -> None:
        dialog = AlertDialog(
            LabEvent(
                key="device|FAULT|",
                severity=Severity.ERROR,
                source="device",
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
        temperature = DeviceSnapshot(
            "temperature", "Temperature", DeviceKind.TEMPERATURE, now, True, "K",
            300.1236, 299.9, 10.0, DeviceActivity.MOVING,
        )
        field = DeviceSnapshot(
            "field", "Magnetic Field", DeviceKind.FIELD, now, True, "Oe",
            123.456, 200.0, 5000.0, DeviceActivity.MOVING,
        )
        temperature_tile = StatusTile("temperature", "Temperature", DeviceKind.TEMPERATURE)
        field_tile = StatusTile("field", "Magnetic Field", DeviceKind.FIELD)
        temperature_tile.update_snapshot(temperature)
        field_tile.update_snapshot(field)
        self.assertEqual(temperature_tile.value_label.text(), "300.124 K")
        self.assertIn("Target 299.900 K", temperature_tile.detail_label.text())
        self.assertEqual(field_tile.value_label.text(), "123.46 Oe")
        self.assertIn("Target 200.00 Oe", field_tile.detail_label.text())
        self.assertIn("5000.00 Oe/min", field_tile.detail_label.text())
        temperature_tile.close()
        field_tile.close()

    def test_control_dialogs_match_unit_precision_and_convert_field_unit(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        temperature_config = next(device for device in config.devices if device.id == "temperature")
        field_config = next(device for device in config.devices if device.id == "field")
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
            device_configs=config.devices,
        )
        self.assertAlmostEqual(temperature_dialog.inputs["target"].minimum(), 1.8)
        self.assertAlmostEqual(temperature_dialog.inputs["target"].maximum(), 400.0)
        self.assertAlmostEqual(temperature_dialog.inputs["rate"].maximum(), 30.0)
        self.assertIn("Configured limits (temperature)", temperature_dialog.limit_label.text())

        temperature_scan_spec = SPECS_BY_TYPE[CommandType.SCAN_TEMPERATURE]
        temperature_scan_dialog = CommandDialog(
            temperature_scan_spec.create(),
            temperature_scan_spec,
            device_configs=config.devices,
        )
        for name in ("start", "stop"):
            self.assertAlmostEqual(temperature_scan_dialog.inputs[name].minimum(), 1.8)
            self.assertAlmostEqual(temperature_scan_dialog.inputs[name].maximum(), 400.0)
        self.assertAlmostEqual(temperature_scan_dialog.inputs["rate"].maximum(), 30.0)

        field_spec = SPECS_BY_TYPE[CommandType.SET_FIELD]
        field_dialog = CommandDialog(
            field_spec.create(),
            field_spec,
            device_configs=config.devices,
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
            device_configs=config.devices,
        )
        for name in ("start", "stop"):
            self.assertAlmostEqual(field_scan_dialog.inputs[name].minimum(), -90000.0)
            self.assertAlmostEqual(field_scan_dialog.inputs[name].maximum(), 90000.0)
        self.assertAlmostEqual(field_scan_dialog.inputs["rate"].maximum(), 10000.0)

        temperature_dialog.close()
        temperature_scan_dialog.close()
        field_dialog.close()
        field_scan_dialog.close()

    def test_sequence_dialog_selects_custom_device_ids_and_their_limits(self) -> None:
        config = load_config(ROOT / "configs" / "default.toml")
        temperature = next(
            device for device in config.devices
            if device.kind is DeviceKind.TEMPERATURE
        )
        custom_devices = (
            replace(temperature, id="cryostat_primary"),
            replace(
                temperature,
                id="cryostat_backup",
                role=DeviceRole.SECONDARY,
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
            device_configs=custom_devices,
        )
        device_input = dialog.inputs["device_id"]
        self.assertEqual(device_input.currentText(), "cryostat_primary")
        self.assertEqual(device_input.count(), 1)
        self.assertEqual(dialog.inputs["target"].maximum(), 400.0)
        self.assertEqual(dialog.values()["device_id"], "cryostat_primary")
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
        dialog = CommandDialog(spec.create(), spec, device_configs=config.devices)
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
