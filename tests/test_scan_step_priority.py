from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication
from labcontrol.module_commands import normalize_module_commands
from labcontrol.sequence.model import CommandType, SPECS_BY_TYPE
from labcontrol.ui.dialogs import CommandDialog
from tests.configuration_fixtures import load_simulated_config


class ScanStepPriorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_temperature_bounds_keep_step_and_round_points_without_moving_bounds(self):
        spec = SPECS_BY_TYPE[CommandType.SCAN_TEMPERATURE]
        command = spec.create()
        command.params.update(start=10.0, stop=20.0, steps=11)
        dialog = CommandDialog(command, spec, instrument_configs=load_simulated_config().instrument_instances)
        try:
            dialog.inputs["stop"].setValue(30.0)
            dialog.inputs["stop"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 21)
            self.assertEqual(dialog.step_input.text(), "1")
            dialog.inputs["start"].setValue(15.0)
            dialog.inputs["start"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 16)
            dialog.inputs["stop"].setValue(30.4)
            dialog.inputs["stop"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 16)
            self.assertEqual(dialog.inputs["start"].value(), 15.0)
            self.assertEqual(dialog.inputs["stop"].value(), 30.4)
            self.assertAlmostEqual(float(dialog.step_input.text()), 15.4 / 15, places=5)
            dialog.inputs["steps"].setValue(9)
            self.assertEqual(dialog.step_input.text(), "1.925")
        finally:
            dialog.close()

    def test_field_unit_conversion_keeps_point_count_and_physical_step(self):
        spec = SPECS_BY_TYPE[CommandType.SCAN_FIELD]
        dialog = CommandDialog(spec.create(), spec, instrument_configs=load_simulated_config().instrument_instances)
        try:
            dialog.inputs["stop"].setValue(20000.0)
            dialog.inputs["stop"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 21)
            dialog.inputs["unit"].setCurrentText("T")
            self.assertEqual(dialog.inputs["steps"].value(), 21)
            self.assertEqual(dialog.inputs["stop"].value(), 2.0)
            self.assertEqual(dialog.step_input.text(), "0.1")
            dialog.inputs["start"].setValue(1.0)
            dialog.inputs["start"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 11)
            dialog.inputs["stop"].setValue(0.0)
            dialog.inputs["stop"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 11)
            self.assertEqual(dialog.step_input.text(), "-0.1")
        finally:
            dialog.close()

    def test_log_bounds_keep_points_per_decade(self):
        spec = SPECS_BY_TYPE[CommandType.SCAN_TEMPERATURE]
        command = spec.create()
        command.params.update(point_mode="Log", start=10.0, stop=100.0, steps=11)
        dialog = CommandDialog(command, spec)
        try:
            dialog.inputs["stop"].setValue(1000.0)
            dialog.inputs["stop"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 21)
            self.assertEqual(dialog.step_input.text(), "10")
            dialog.inputs["start"].setValue(100.0)
            dialog.inputs["start"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 11)
            self.assertEqual(dialog.step_input.text(), "10")
        finally:
            dialog.close()

    def test_module_si_bounds_update_on_commit_and_keep_step_through_equal_bounds(self):
        spec = normalize_module_commands("source", [{
            "id": "scan_current", "label": "Scan Current", "kind": "scan",
            "points_field": "points", "point_parameter": "current",
            "point_unit": "A", "scan_modes": ["Linear", "Log", "List"],
            "fields": [{"name": "points", "label": "Points", "type": "list", "default": ["0", "1m"]}],
        }])[0]
        command = spec.create()
        command.params.update(point_mode="Linear", start="0", stop="1m", steps=11)
        dialog = CommandDialog(command, spec)
        try:
            dialog.inputs["stop"].setText("2")
            self.assertEqual(dialog.inputs["steps"].value(), 11)
            self.assertEqual(dialog.step_input.text(), "100u")
            dialog.inputs["stop"].setText("2m")
            dialog.inputs["stop"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 21)
            self.assertEqual(dialog.step_input.text(), "100u")
            dialog.inputs["start"].setText("2m")
            dialog.inputs["start"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 1)
            self.assertEqual(dialog.step_input.text(), "100u")
            dialog.inputs["stop"].setText("1m")
            dialog.inputs["stop"].editingFinished.emit()
            self.assertEqual(dialog.inputs["steps"].value(), 11)
            self.assertEqual(dialog.step_input.text(), "-100u")
            dialog.inputs["point_mode"].setCurrentText("List")
            dialog.inputs["stop"].editingFinished.emit()
            self.assertEqual(dialog.values()["point_mode"], "List")
        finally:
            dialog.close()
