from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.config import load_config  # noqa: E402
from labcontrol.models import DeviceActivity, DeviceKind, DeviceSnapshot  # noqa: E402
from labcontrol.sequence.model import SPECS_BY_TYPE, CommandType  # noqa: E402
from labcontrol.ui.dialogs import CommandDialog, ManualControlDialog  # noqa: E402
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
        trend.close()
        tile.close()

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
        self.assertEqual(values["points"], "300.000, 299.900, 300.000")
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
