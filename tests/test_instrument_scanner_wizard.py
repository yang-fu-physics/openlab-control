from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QStackedWidget,
    QTabWidget,
)
from instrument_scanner import (  # noqa: E402
    InstrumentScannerWindow,
    SIMULATIONS,
    VisaScanResult,
    execute_save_plan,
    merge_resource_drafts,
    render_generated_instrument,
    render_simulation,
)
from labcontrol.instrument_resources import (  # noqa: E402
    InstrumentResource,
    write_instrument_resources,
)
from labcontrol.instruments.manifest import (  # noqa: E402
    load_instrument_manifest,
)


MANIFEST = """
id = "test_controller"
name = "Test Controller"
version = "1.0.0"
api_version = "4"
backend = "backend:Driver"
kinds = ["temperature"]

[discovery]
identity_pattern = "^TEST,CTRL,"

[[config_fields]]
id = "pid_file"
label = "PID File"
type = "pid_file"
default = "pid.example.toml"

[[controls]]
id = "loop1"
label = "Loop 1"

[[panels]]
id = "main"
label = "Main Control"
template = "controller"
control = "loop1"
reading_options = ["temperature"]
default_reading = "temperature"
min_value = 1.0
max_value = 400.0
default_rate_per_minute = 1.0
max_rate_per_minute = 10.0
stability_tolerance = 0.05
stability_max_slope_per_minute = 0.03
stability_dwell_seconds = 1.5
stability_timeout_seconds = 120.0
stability_window_seconds = 1.0

[readings.temperature]
label = "Temperature"
unit = "K"
decimals = 3
"""

TCP_MANIFEST = """
id = "tcp_switch"
name = "TCP Switch"
version = "1.0.0"
api_version = "4"
backend = "backend:Driver"
kinds = ["monitor"]

[[config_fields]]
id = "host"
label = "Host"
type = "string"
default = ""

[[panels]]
id = "state"
label = "State"
template = "readout"
readings = ["value"]

[readings.value]
label = "State"
"""


def _write_instrument(
    root: Path,
    instrument_id: str = "test_controller",
    name: str = "Test Controller",
) -> Path:
    directory = root / instrument_id
    directory.mkdir(parents=True)
    (directory / "backend.py").write_text(
        "class Driver: pass\n", encoding="utf-8"
    )
    manifest = MANIFEST.replace(
        'id = "test_controller"', f'id = "{instrument_id}"', 1
    ).replace('name = "Test Controller"', f'name = "{name}"', 1)
    (directory / "instrument.toml").write_text(manifest, encoding="utf-8")
    (directory / "pid.example.toml").write_text(
        "# Requires validated zones.\nzones = []\n", encoding="utf-8"
    )
    return directory


def _write_tcp_instrument(root: Path) -> Path:
    directory = root / "tcp_switch"
    directory.mkdir(parents=True)
    (directory / "backend.py").write_text(
        "class Driver: pass\n", encoding="utf-8"
    )
    (directory / "instrument.toml").write_text(
        TCP_MANIFEST, encoding="utf-8"
    )
    return directory


class InstrumentScannerModelTests(unittest.TestCase):
    def test_merge_marks_only_absent_resources_missing(self) -> None:
        drafts = merge_resource_drafts(
            (
                InstrumentResource("present", "GPIB0::1::INSTR", "OLD"),
                InstrumentResource("missing", "GPIB0::2::INSTR", "SAVED"),
            ),
            (),
            (
                VisaScanResult(
                    "GPIB0::1::INSTR",
                    error="VisaIOError: timeout",
                ),
            ),
        )

        self.assertTrue(drafts[0].present)
        self.assertEqual(drafts[0].identity, "OLD")
        self.assertTrue(drafts[0].error)
        self.assertFalse(drafts[1].present)
        self.assertTrue(drafts[1].keep_for_measurement)

    def test_generated_document_references_but_does_not_copy_panels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            descriptor = load_instrument_manifest(
                _write_instrument(Path(temporary))
            )
            self.assertTrue(descriptor.valid, descriptor.error)
            rendered = render_generated_instrument(
                descriptor,
                [
                    {
                        "id": "main_controller",
                        "resource": "GPIB0::1::INSTR",
                        "identity": "TEST,CTRL,1",
                        "pid_file": "configs/pid/main_controller.toml",
                        "panels": [
                            {
                                "id": "main",
                                "enabled": False,
                            }
                        ],
                    }
                ],
            )
            raw = tomllib.loads(rendered)

            self.assertNotIn("panels", raw)
            self.assertEqual(raw["controls"][0]["id"], "loop1")
            self.assertEqual(raw["instances"][0]["panels"][0]["id"], "main")

    def test_simulation_is_a_self_contained_v4_document(self) -> None:
        raw = tomllib.loads(render_simulation(SIMULATIONS[0], 3))

        self.assertEqual(raw["api_version"], "4")
        self.assertEqual(raw["panels"][0]["control"], "main")
        self.assertEqual(raw["instances"][0]["panels"][0]["order"], 3)
        self.assertEqual(
            raw["instances"][0]["panels"][0]["role"], "sample_temp"
        )


class InstrumentScannerWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_unique_review_page_complete_plan_and_pid_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = root / "configs"
            instruments = root / "system_instruments"
            configs.mkdir()
            _write_instrument(instruments)
            write_instrument_resources(
                configs / "visa.resources.toml",
                (
                    InstrumentResource(
                        "controller", "GPIB0::1::INSTR", "TEST,CTRL,1"
                    ),
                    InstrumentResource(
                        "offline_meter", "GPIB0::2::INSTR", "METER,OLD,1"
                    ),
                ),
            )
            generated = configs / "instruments"
            generated.mkdir()
            stale = generated / "stale.toml"
            stale.write_text("id = \"stale\"\n", encoding="utf-8")

            with patch("instrument_scanner.QTimer.singleShot"):
                window = InstrumentScannerWindow(configs, instruments)
            self.addCleanup(window.close)
            self.assertIsInstance(window.pages, QStackedWidget)
            self.assertEqual(window.findChildren(QTabWidget), [])
            self.assertEqual(window.pages.count(), 3)
            self.assertEqual(window.step_list.count(), window.pages.count())
            self.assertEqual(
                sum(
                    window.step_list.item(index).toolTip() == "Review & Save"
                    for index in range(window.step_list.count())
                ),
                1,
            )
            self.assertEqual(
                [
                    int(window.step_list.item(index).text().split(maxsplit=1)[0])
                    for index in range(window.step_list.count())
                ],
                [1, 2, 3],
            )
            self.assertEqual(window.pages.currentIndex(), 0)
            self.assertEqual(window.step_list.currentRow(), 0)
            window.next_button.click()
            self.assertEqual(window.pages.currentIndex(), 1)
            self.assertEqual(window.step_list.currentRow(), 1)
            window.back_button.click()
            self.assertEqual(window.pages.currentIndex(), 0)
            window.step_list.setCurrentRow(window.review_page_index)
            self.assertEqual(
                window.pages.currentIndex(), window.review_page_index
            )
            window.pages.setCurrentIndex(1)
            self.assertEqual(window.step_list.currentRow(), 1)

            window._scan_completed(
                (
                    VisaScanResult(
                        "GPIB0::1::INSTR", "TEST,CTRL,1"
                    ),
                )
            )
            descriptor = window.descriptors[0]
            self.assertFalse(
                any(
                    checkbox.isChecked()
                    for checkbox in window._simulation_checks.values()
                )
            )
            self.assertEqual(
                window._resource_rows[1]["card"].property("missing"), "true"
            )
            window._add_instance(descriptor)
            instance = window._instrument_pages[descriptor.id]["instances"][0]
            resource = instance["resource"]
            resource.setCurrentIndex(resource.findData("GPIB0::1::INSTR"))
            self.assertFalse(window._resource_rows[0]["keep"].isEnabled())
            window._add_instance(descriptor)
            second = window._instrument_pages[descriptor.id]["instances"][1]
            second_resource = second["resource"]
            assigned_item = second_resource.model().item(
                second_resource.findData("GPIB0::1::INSTR")
            )
            self.assertFalse(assigned_item.isEnabled())
            window._remove_instance(
                window._instrument_pages[descriptor.id], second
            )
            window.pages.setCurrentIndex(window.review_page_index)
            plan = window._build_save_plan()

            self.assertEqual(
                [item.id for item in plan.resources], ["offline_meter"]
            )
            self.assertEqual(len(plan.writes), 1)
            self.assertEqual(plan.deletions, (stale.resolve(),))
            self.assertEqual(len(plan.pid_creations), 1)
            self.assertTrue(plan.pid_creations[0].requires_validated_zones)

            execute_save_plan(window.visa_path, plan)
            pid_path = configs / "pid" / "test_controller.toml"
            self.assertTrue(pid_path.is_file())
            pid_path.write_text("zones = [{temperature = 400.0}]\n", encoding="utf-8")
            second_plan = window._build_save_plan()
            self.assertEqual(second_plan.pid_creations, ())
            execute_save_plan(window.visa_path, second_plan)
            self.assertEqual(
                pid_path.read_text(encoding="utf-8"),
                "zones = [{temperature = 400.0}]\n",
            )
            self.assertFalse(stale.exists())

            window._simulation_checks[
                "simulated_second_stage"
            ].setChecked(True)
            window._refresh_review()
            self.assertEqual(window.order_list.count(), 2)
            window.order_list.setCurrentRow(1)
            window._move_order(-1)
            ordered_plan = window._build_save_plan()
            orders = {
                tomllib.loads(item.text)["instances"][0]["panels"][0][
                    "order"
                ]
                for item in ordered_plan.writes
            }
            self.assertEqual(orders, {1, 2})

    def test_six_instrument_steps_need_no_horizontal_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = root / "configs"
            instruments = root / "system_instruments"
            configs.mkdir()
            names = [
                f"System Instrument Category {index} With A Long Operator Name"
                for index in range(1, 7)
            ]
            for index, name in enumerate(names, start=1):
                _write_instrument(instruments, f"controller_{index}", name)

            with patch("instrument_scanner.QTimer.singleShot"):
                window = InstrumentScannerWindow(configs, instruments)
            self.addCleanup(window.close)
            window.resize(1180, 820)
            window.show()
            self.application.processEvents()

            self.assertEqual(window.pages.count(), 8)
            self.assertEqual(window.step_list.count(), 8)
            self.assertEqual(
                [
                    int(window.step_list.item(index).text().split(maxsplit=1)[0])
                    for index in range(window.step_list.count())
                ],
                list(range(1, 9)),
            )
            self.assertEqual(
                window.step_list.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            self.assertFalse(window.step_list.horizontalScrollBar().isVisible())
            self.assertEqual(window.navigation_panel.width(), 248)
            self.assertGreater(window.pages.width(), window.navigation_panel.width())
            for index, name in enumerate(names, start=1):
                self.assertEqual(window.step_list.item(index).toolTip(), name)

            for expected in range(1, window.pages.count()):
                window.next_button.click()
                self.assertEqual(window.pages.currentIndex(), expected)
                self.assertEqual(window.step_list.currentRow(), expected)
            for expected in reversed(range(window.pages.count() - 1)):
                window.back_button.click()
                self.assertEqual(window.pages.currentIndex(), expected)
                self.assertEqual(window.step_list.currentRow(), expected)

    def test_required_string_field_keeps_tcp_instance_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = root / "configs"
            instruments = root / "system_instruments"
            configs.mkdir()
            _write_tcp_instrument(instruments)

            with patch("instrument_scanner.QTimer.singleShot"):
                window = InstrumentScannerWindow(configs, instruments)
            self.addCleanup(window.close)
            descriptor = window.descriptors[0]
            window._add_instance(descriptor)
            instance = window._instrument_pages[descriptor.id]["instances"][0]

            self.assertFalse(window._instance_is_complete(descriptor, instance))
            instance["fields"]["host"][1].setText("192.0.2.10")
            self.assertTrue(window._instance_is_complete(descriptor, instance))


if __name__ == "__main__":
    unittest.main()
