from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from labcontrol.config import ConfigurationError, load_config  # noqa: E402
from labcontrol.datafile import DatRunLogger  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instrument_resources import (  # noqa: E402
    InstrumentResource,
    InstrumentResourceError,
    load_instrument_resources,
    render_instrument_resources,
    write_instrument_resources,
)
from labcontrol.measurement.frontend_api import ModuleUIAPI  # noqa: E402
from labcontrol.module_api import ModuleAPI, ModuleError  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from instrument_scanner import (  # noqa: E402
    InstrumentScannerWindow,
    NI_VISA_DOWNLOAD_URL,
    VisaScanResult,
    application_root,
    discover_scan_descriptors,
    match_descriptor,
    scan_visa_resources,
    suggest_resource_id,
)


def _write_system_instrument(
    root: Path,
    instrument_id: str,
    kind: str,
    main_reading: str,
    auxiliary_readings: tuple[str, ...] = (),
) -> None:
    directory = root / "system_instruments" / instrument_id
    directory.mkdir(parents=True)
    (directory / "backend.py").write_text("class Driver: pass\n", encoding="utf-8")
    readings = (main_reading, *auxiliary_readings)
    metadata = "".join(
        f"\n[readings.{key}]\nlabel = \"{key}\"\nunit = \"K\"\n"
        for key in readings
    )
    (directory / "instrument.toml").write_text(
        (
            f'id = "{instrument_id}"\n'
            f'name = "{instrument_id}"\n'
            'version = "1.0.0"\n'
            'api_version = "2"\n'
            'backend = "backend:Driver"\n'
            f'kinds = ["{kind}"]\n'
            f'main_reading = "{main_reading}"\n'
            + metadata
        ),
        encoding="utf-8",
    )


class InstrumentResourceTests(unittest.TestCase):
    def test_round_trip_preserves_system_and_measurement_resources(
        self,
    ) -> None:
        resources = (
            InstrumentResource(
                id="cryocon_main",
                address="USB0::1::INSTR",
                identity="Cryo-con,24C,SERIAL,1.0",
                purpose="system",
                system_instrument="cryocon_22c_24c",
                auxiliary_readings=("temp_a",),
            ),
            InstrumentResource(
                "keithley_2400",
                "GPIB0::24::INSTR",
                "KEITHLEY INSTRUMENTS INC.,MODEL 2400,123,1.0",
                "measurement",
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "instruments.local.toml"
            write_instrument_resources(path, resources)
            self.assertEqual(
                load_instrument_resources(path),
                resources,
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("schema_version = 2", text)
            self.assertIn('purpose = "system"', text)

    def test_registry_rejects_duplicate_addresses_and_measurement_readings(
        self,
    ) -> None:
        duplicate = (
            InstrumentResource(
                "first",
                "GPIB0::1::INSTR",
            ),
            InstrumentResource(
                "second",
                "gpib0::1::instr",
            ),
        )
        with self.assertRaisesRegex(
            InstrumentResourceError,
            "assigned to both",
        ):
            render_instrument_resources(duplicate)
        with self.assertRaisesRegex(
            InstrumentResourceError,
            "cannot declare system readings",
        ):
            render_instrument_resources(
                (
                    InstrumentResource(
                        "meter",
                        "GPIB0::2::INSTR",
                        purpose="measurement",
                        auxiliary_readings=("voltage",),
                    ),
                )
            )
        with self.assertRaisesRegex(
            InstrumentResourceError,
            "cannot select a System Instrument",
        ):
            render_instrument_resources(
                (
                    InstrumentResource(
                        "meter",
                        "GPIB0::3::INSTR",
                        purpose="measurement",
                        system_instrument="cryocon_22c_24c",
                    ),
                )
            )

    def test_main_config_resolves_resource_and_reading_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            config_path = root / "configs" / "site.local.toml"
            shutil.copy2(
                ROOT / "configs" / "default.toml",
                config_path,
            )
            _write_system_instrument(
                root,
                "cryocon_22c_24c",
                "temperature",
                "temp_b",
                ("temp_a",),
            )
            write_instrument_resources(
                root / "configs" / "instruments.local.toml",
                (
                    InstrumentResource(
                        "cryocon_main",
                        "USB0::1::INSTR",
                        purpose="system",
                        system_instrument="cryocon_22c_24c",
                        auxiliary_readings=("temp_a",),
                    ),
                ),
            )
            source = config_path.read_text(encoding="utf-8")
            source = source.replace(
                'backend = "labcontrol.instruments.simulated:SimulatedTemperatureController"',
                (
                    'resource = "cryocon_main"'
                ),
                1,
            )
            source = source.replace('unit = "K"\n', "", 1)
            config_path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(
                ConfigurationError,
                "initial_value is only used by built-in simulators",
            ):
                load_config(config_path)
            source = source.replace('initial_value = 300.0\n', "", 1)
            config_path.write_text(
                source.replace(
                    'resource = "cryocon_main"',
                    'resource = "cryocon_main"\nmain_reading = "temp_b"',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "reading metadata comes from",
            ):
                load_config(config_path)
            config_path.write_text(source, encoding="utf-8")
            config = load_config(config_path)
            temperature = config.instrument("temperature")
            self.assertEqual(
                temperature.address,
                "USB0::1::INSTR",
            )
            self.assertEqual(
                temperature.main_reading,
                "temp_b",
            )
            self.assertEqual(
                temperature.auxiliary_readings,
                ("temp_a",),
            )
            logger = DatRunLogger(config, EventManager())
            paths = logger.open_run(
                "resource.seq",
                "T End Sequence\n",
            )
            self.assertEqual(
                load_instrument_resources(
                    paths.instrument_resources_snapshot
                ),
                config.instrument_resources,
            )
            logger.close()

    def test_external_system_instruments_require_distinct_resources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            _write_system_instrument(
                root,
                "cryocon_22c_24c",
                "temperature",
                "temp_b",
                ("temp_a",),
            )
            _write_system_instrument(
                root,
                "magnet_supply_x",
                "field",
                "field",
            )
            config_path = root / "configs" / "site.local.toml"
            write_instrument_resources(
                root / "configs" / "instruments.local.toml",
                (
                    InstrumentResource(
                        "temperature_controller",
                        "USB0::1::INSTR",
                        purpose="system",
                        system_instrument="cryocon_22c_24c",
                        auxiliary_readings=("temp_a",),
                    ),
                    InstrumentResource(
                        "magnet_controller",
                        "GPIB0::7::INSTR",
                        purpose="system",
                        system_instrument="magnet_supply_x",
                    ),
                ),
            )
            source = (ROOT / "configs" / "default.toml").read_text(
                encoding="utf-8"
            )
            source = source.replace(
                'backend = "labcontrol.instruments.simulated:SimulatedTemperatureController"',
                'resource = "temperature_controller"',
                1,
            ).replace(
                'backend = "labcontrol.instruments.simulated:SimulatedFieldController"',
                'resource = "magnet_controller"',
                1,
            )
            source = source.replace('unit = "K"\n', "", 1)
            source = source.replace('unit = "Oe"\n', "", 1)
            source = source.replace('initial_value = 300.0\n', "", 1)
            source = source.replace('initial_value = 0.0\n', "", 1)
            config_path.write_text(source, encoding="utf-8")

            config = load_config(config_path)

            self.assertEqual(
                config.instrument("temperature").address,
                "USB0::1::INSTR",
            )
            self.assertEqual(
                config.instrument("field").address,
                "GPIB0::7::INSTR",
            )
            self.assertEqual(
                set(config.resource_payload("system")),
                {"temperature_controller", "magnet_controller"},
            )

    def test_external_system_instrument_rejects_inline_or_missing_address_resource(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            config_path = root / "configs" / "site.local.toml"
            source = (ROOT / "configs" / "default.toml").read_text(
                encoding="utf-8"
            ).replace(
                'backend = "labcontrol.instruments.simulated:SimulatedTemperatureController"',
                'backend = "cryocon_22c_24c"',
                1,
            )
            config_path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "selected through resource"):
                load_config(config_path)

            config_path.write_text(
                source.replace(
                    'backend = "cryocon_22c_24c"',
                    'backend = "cryocon_22c_24c"\naddress = "USB0::1::INSTR"',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "resource file"):
                load_config(config_path)

    def test_unknown_resource_fails_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            config_path = root / "configs" / "site.local.toml"
            source = (
                ROOT / "configs" / "default.toml"
            ).read_text(encoding="utf-8")
            source = source.replace(
                'backend = "labcontrol.instruments.simulated:SimulatedTemperatureController"',
                (
                    'resource = "missing"'
                ),
                1,
            )
            source = source.replace('unit = "K"\n', "", 1)
            source = source.replace('initial_value = 300.0\n', "", 1)
            config_path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(
                ConfigurationError,
                "unknown resource",
            ):
                load_config(config_path)

    def test_module_api_returns_filtered_deep_copy(self) -> None:
        source = {
            "meter": {
                "id": "meter",
                "address": "GPIB0::1::INSTR",
                "purpose": "measurement",
            },
        }
        api = ModuleAPI(
            {},
            lambda _kind, _payload: None,
            _instrument_resources=source,
        )
        resources = api.resources()
        self.assertEqual(set(resources), {"meter"})
        resources["meter"]["address"] = "changed"  # type: ignore[index]
        self.assertEqual(
            api.resources()["meter"]["address"],
            "GPIB0::1::INSTR",
        )
        self.assertEqual(
            api.resource_address("meter"),
            "GPIB0::1::INSTR",
        )
        with self.assertRaisesRegex(ModuleError, "unavailable"):
            api.resource_address("missing")
        unsafe = ModuleAPI(
            {},
            lambda _kind, _payload: None,
            _instrument_resources={
                "controller": {
                    "purpose": "system",
                    "address": "USB0::1::INSTR",
                }
            },
        )
        with self.assertRaisesRegex(
            ModuleError,
            "non-measurement",
        ):
            unsafe.resources()

    def test_module_ui_api_rejects_system_instrument_resources(self) -> None:
        safe = ModuleUIAPI(
            resources={
                "meter": {
                    "purpose": "measurement",
                    "address": "GPIB0::1::INSTR",
                }
            }
        )
        copied = safe.resources()
        copied["meter"]["address"] = "changed"  # type: ignore[index]
        self.assertEqual(
            safe.resources()["meter"]["address"],
            "GPIB0::1::INSTR",
        )
        self.assertEqual(
            safe.resource("meter")["address"],
            "GPIB0::1::INSTR",
        )
        with self.assertRaisesRegex(
            ValueError,
            "cannot expose System Instrument",
        ):
            ModuleUIAPI(
                resources={
                    "controller": {
                        "purpose": "system",
                        "address": "USB0::1::INSTR",
                    }
                }
            )


class InstrumentScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_scanner_loads_existing_file_and_marks_incomplete_system_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "instruments.local.toml"
            instrument = root / "system_instruments" / "controller"
            instrument.mkdir(parents=True)
            (instrument / "backend.py").write_text(
                "class Driver: pass\n",
                encoding="utf-8",
            )
            (instrument / "instrument.toml").write_text(
                (
                    'id = "controller"\n'
                    'name = "Example Controller"\n'
                    'version = "1.0.0"\n'
                    'api_version = "2"\n'
                    'backend = "backend:Driver"\n'
                    'kinds = ["temperature"]\n'
                    'main_reading = "temp_b"\n'
                    '[discovery]\n'
                    'identity_pattern = "Maker,Controller"\n'
                    '[readings.temp_b]\nlabel = "Sample Temperature (Temp B)"\nunit = "K"\n'
                    '[readings.temp_a]\nlabel = "Cold Head Temperature (Temp A)"\nunit = "K"\n'
                    '[readings.heater_output]\nlabel = "Heater Output"\nunit = "%FS"\n'
                    '[readings.heater_range]\nlabel = "Heater Range"\n'
                ),
                encoding="utf-8",
            )
            write_instrument_resources(
                output,
                (
                    InstrumentResource(
                        "meter",
                        "GPIB0::1::INSTR",
                        "Maker,Model,Serial,Version with a very long tail",
                    ),
                ),
            )
            with patch(
                "instrument_scanner.QTimer.singleShot"
            ) as schedule_scan:
                window = InstrumentScannerWindow(
                    output,
                    root / "system_instruments",
                )
            try:
                schedule_scan.assert_called_once()
                self.assertEqual(
                    schedule_scan.call_args.args[0],
                    0,
                )
                self.assertEqual(
                    schedule_scan.call_args.args[1],
                    window.start_scan,
                )
                self.assertIn(
                    "System Instruments (1): Example Controller",
                    window.discovery_label.text(),
                )
                self.assertIn(
                    "Loaded 1 existing resource",
                    window.existing_label.text(),
                )
                self.assertEqual(len(window._rows), 1)
                self.assertIn(
                    "Maker,Model,Serial,Version with a very long tail",
                    window._rows[0]["details_text"].text(),
                )
                self.assertEqual(
                    window.scroll_area.horizontalScrollBarPolicy(),
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
                )

                window._show_results(
                    (
                        VisaScanResult(
                            "GPIB0::1::INSTR",
                            "Maker,Model,Serial,NewVersion",
                        ),
                        VisaScanResult(
                            "USB0::2::INSTR",
                            "Maker,Controller,2",
                        ),
                    )
                )
                window.resize(960, 600)
                window.show()
                self.application.processEvents()
                self.assertLessEqual(
                    window.minimumSizeHint().width(),
                    window.minimumWidth(),
                )
                self.assertFalse(
                    window.scroll_area.horizontalScrollBar().isVisible()
                )
                controls = window._rows[1]
                self.assertEqual(
                    controls["purpose"].currentText(),
                    "System",
                )
                self.assertEqual(
                    controls["system_instrument"].currentData(),
                    "controller",
                )
                self.assertEqual(
                    controls["main_label"].text(),
                    "Sample Temperature (Temp B)",
                )
                self.assertEqual(
                    {
                        key: checkbox.text()
                        for key, checkbox in controls[
                            "auxiliary_checks"
                        ].items()
                    },
                    {
                        "temp_a": "Cold Head Temperature (Temp A)",
                        "heater_output": "Heater Output",
                        "heater_range": "Heater Range",
                    },
                )
                self.assertTrue(
                    all(
                        checkbox.isChecked()
                        for checkbox in controls[
                            "auxiliary_checks"
                        ].values()
                    )
                )
                controls["auxiliary_checks"][
                    "heater_range"
                ].setChecked(False)
                configured = next(
                    resource
                    for resource in window._resources()
                    if resource.purpose == "system"
                )
                self.assertEqual(
                    configured.auxiliary_readings,
                    ("temp_a", "heater_output"),
                )
                controls["auxiliary_checks"][
                    "heater_range"
                ].setChecked(True)
                controls["id"].clear()
                with patch("instrument_scanner.QMessageBox.warning") as warning:
                    window.preview_and_save()
                warning.assert_called_once()
                self.assertIn("Complete these selected rows", warning.call_args.args[2])

                controls["purpose"].setCurrentText("Ignore")
                with patch(
                    "instrument_scanner.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Cancel,
                ) as question:
                    window.preview_and_save()
                self.assertIn(
                    "Existing entries replaced: meter",
                    question.call_args.args[2],
                )
            finally:
                window.close()

    def test_frozen_scanner_uses_shared_release_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "InstrumentScanner.exe"
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
            ):
                self.assertEqual(
                    application_root(),
                    Path(temp).resolve(),
                )

    def test_scan_explains_that_pyvisa_still_needs_a_visa_implementation(
        self,
    ) -> None:
        def unavailable_manager():
            raise ValueError("Could not locate a VISA implementation")

        with patch.dict(
            sys.modules,
            {
                "pyvisa": SimpleNamespace(
                    ResourceManager=unavailable_manager
                )
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "includes PyVISA.*NI-VISA",
            ):
                scan_visa_resources(0.25)

    def test_scan_failure_dialog_links_official_ni_visa_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dialogs: list[QMessageBox] = []

            def capture_dialog(dialog: QMessageBox) -> int:
                dialogs.append(dialog)
                return 0

            with (
                patch("instrument_scanner.QTimer.singleShot"),
                patch.object(
                    QMessageBox,
                    "exec",
                    new=capture_dialog,
                ),
            ):
                window = InstrumentScannerWindow(
                    root / "instruments.local.toml",
                    root / "system_instruments",
                )
                try:
                    window._scan_failed("<driver>\nmissing")
                    self.assertEqual(len(dialogs), 1)
                    dialog = dialogs[0]
                    self.assertEqual(
                        dialog.textFormat(),
                        Qt.TextFormat.RichText,
                    )
                    self.assertEqual(
                        dialog.textInteractionFlags(),
                        Qt.TextInteractionFlag.TextBrowserInteraction,
                    )
                    self.assertIn(
                        NI_VISA_DOWNLOAD_URL,
                        dialog.text(),
                    )
                    self.assertIn(
                        "&lt;driver&gt;<br>missing",
                        dialog.text(),
                    )
                    self.assertEqual(
                        window.summary_label.text(),
                        "VISA scan failed",
                    )
                finally:
                    window.close()

    def test_scan_uses_only_idn_query_and_closes_every_session(
        self,
    ) -> None:
        class Handle:
            def __init__(self, identity: str) -> None:
                self.identity = identity
                self.timeout = 0
                self.commands: list[str] = []
                self.closed = False

            def query(self, command: str) -> str:
                self.commands.append(command)
                return self.identity

            def close(self) -> None:
                self.closed = True

        first = Handle("Maker,Model,Serial,1")
        second = Handle("")

        class Manager:
            def __init__(self) -> None:
                self.closed = False

            @staticmethod
            def list_resources():
                return (
                    "GPIB0::1::INSTR",
                    "USB0::2::INSTR",
                )

            @staticmethod
            def open_resource(address, **_kwargs):
                return (
                    first
                    if address.startswith("GPIB")
                    else second
                )

            def close(self) -> None:
                self.closed = True

        manager = Manager()
        fake_pyvisa = SimpleNamespace(
            ResourceManager=lambda: manager
        )
        with patch.dict(
            sys.modules,
            {"pyvisa": fake_pyvisa},
        ):
            results = scan_visa_resources(0.25)
        self.assertEqual(first.commands, ["*IDN?"])
        self.assertEqual(second.commands, ["*IDN?"])
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertTrue(manager.closed)
        self.assertEqual(results[0].identity, "Maker,Model,Serial,1")
        self.assertIn("empty", results[1].error)

    def test_scan_rejects_unbounded_or_control_character_identity(
        self,
    ) -> None:
        class Handle:
            def __init__(self, identity: str) -> None:
                self.identity = identity
                self.closed = False
                self.timeout = 0

            def query(self, command: str) -> str:
                self.assert_command = command
                return self.identity

            def close(self) -> None:
                self.closed = True

        handles = [Handle("x" * 1025), Handle("Maker\x00Model")]

        class Manager:
            @staticmethod
            def list_resources():
                return ("GPIB0::1::INSTR", "GPIB0::2::INSTR")

            @staticmethod
            def open_resource(address, **_kwargs):
                return handles[0 if "::1::" in address else 1]

            @staticmethod
            def close() -> None:
                return None

        with patch.dict(
            sys.modules,
            {"pyvisa": SimpleNamespace(ResourceManager=Manager)},
        ):
            results = scan_visa_resources(0.25)

        self.assertTrue(all(handle.closed for handle in handles))
        self.assertTrue(all(not result.identity for result in results))
        self.assertTrue(all("printable" in result.error for result in results))

    def test_discovery_profile_suggests_instrument_and_reading_roles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instrument = root / "cryocon_22c_24c"
            instrument.mkdir()
            (instrument / "backend.py").write_text(
                "class Driver: pass\n",
                encoding="utf-8",
            )
            (instrument / "instrument.toml").write_text(
                (
                    'id = "cryocon_22c_24c"\n'
                    'name = "Cryo-con"\n'
                    'version = "1.0.0"\n'
                    'api_version = "2"\n'
                    'backend = "backend:Driver"\n'
                    'kinds = ["temperature"]\n'
                    'main_reading = "temp_b"\n'
                    '[discovery]\n'
                    'identity_pattern = "(?i)cryo-?con.*24c"\n'
                    '[readings.temp_b]\nlabel = "Sample Temperature (Temp B)"\nunit = "K"\n'
                    '[readings.temp_a]\nlabel = "Cold Head Temperature (Temp A)"\nunit = "K"\n'
                    '[readings.heater_output]\nlabel = "Heater Output"\nunit = "%FS"\n'
                    '[readings.heater_range]\nlabel = "Heater Range"\n'
                ),
                encoding="utf-8",
            )
            descriptors = discover_scan_descriptors(root)
            self.assertEqual(len(descriptors), 1)
            matched = match_descriptor(
                "Cryo-con,24C,123,1.0",
                descriptors,
            )
            self.assertIsNotNone(matched)
            assert matched is not None
            self.assertEqual(matched.main_reading, "temp_b")
            self.assertEqual(
                matched.auxiliary_readings,
                ("temp_a", "heater_output", "heater_range"),
            )
            self.assertEqual(
                {reading.key: reading.label for reading in matched.readings},
                {
                    "temp_b": "Sample Temperature (Temp B)",
                    "temp_a": "Cold Head Temperature (Temp A)",
                    "heater_output": "Heater Output",
                    "heater_range": "Heater Range",
                },
            )
            self.assertEqual(
                suggest_resource_id(
                    "Cryo-con,24C,123,1.0",
                    "USB0::1::INSTR",
                ),
                "cryo_con_24c_123",
            )

if __name__ == "__main__":
    unittest.main()
