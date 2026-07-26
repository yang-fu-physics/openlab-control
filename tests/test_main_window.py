from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
MODULE_REPOSITORY = (
    ROOT
    / "plugin_templates"
    / "measurement-modules-repository"
)
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.config import load_config  # noqa: E402
from labcontrol.sequence.model import CommandType  # noqa: E402
from labcontrol.sequence.module_settings import (  # noqa: E402
    SequenceModuleSettings,
    load_sequence_module_settings,
    save_sequence_module_settings,
)
from labcontrol.sequence.parser import (  # noqa: E402
    load_sequence,
    parse_sequence,
)
from labcontrol.ui.dialogs import CommandDialog  # noqa: E402
from labcontrol.ui.main_window import MainWindow  # noqa: E402


class MainWindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])
        cls.config = load_config(ROOT / "configs" / "default.toml")
        configure_qt_appearance(cls.application, cls.config.ui_scale)

    def test_floating_windows_stay_inside_minimum_viewport(self) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1180, 720)
            window.show()
            window._show_data_browser()
            self.application.processEvents()
            window._fit_mdi_windows()
            self.application.processEvents()

            viewport = window.mdi.viewport().rect()
            for subwindow in (window.sequence_window, window.data_window):
                geometry = subwindow.geometry()
                self.assertGreaterEqual(geometry.left(), viewport.left())
                self.assertGreaterEqual(geometry.top(), viewport.top())
                self.assertLessEqual(geometry.right(), viewport.right())
                self.assertLessEqual(geometry.bottom(), viewport.bottom())
        finally:
            window.close()

    def test_new_sequence_reopens_a_closed_sequence_window(self) -> None:
        window = MainWindow(self.config)
        try:
            window.show()
            self.application.processEvents()
            window.sequence_window.close()
            self.application.processEvents()
            self.assertFalse(window.sequence_window.isVisible())

            window.new_action.trigger()
            self.application.processEvents()

            self.assertTrue(window.sequence_window.isVisible())
            self.assertTrue(window.editor.isVisible())
            self.assertIs(window.mdi.activeSubWindow(), window.sequence_window)
            self.assertEqual(window.document.name, "Untitled.seq")
            self.assertEqual(window.editor.list.count(), 1)
            self.assertEqual(window.editor.list.item(0).text(), "End Sequence")
        finally:
            window.close()

    def test_custom_file_paths_do_not_force_the_left_dock_wider(self) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1180, 720)
            window.show()
            self.application.processEvents()
            baseline_minimum = window.left_dock.minimumSizeHint().width()
            custom_path = ROOT / "a very long custom output directory" / (
                "a_very_long_measurement_file_name_that_must_not_expand_the_sidebar.dat"
            )
            with patch(
                "labcontrol.ui.main_window.QFileDialog.getSaveFileName",
                return_value=(str(custom_path), "Data (*.dat)"),
            ):
                window._change_datafile()
            window.sequence_label.setFullText("a_very_long_sequence_name_" * 8 + ".seq")
            self.application.processEvents()

            command = next(
                item for item in window.document.commands if item.type.value == "set_datafile"
            )
            expected_path = str(custom_path.resolve())
            self.assertEqual(command.type.value, "set_datafile")
            self.assertEqual(command.params["path_scope"], "Custom folder")
            self.assertEqual(command.params["path"], expected_path)
            self.assertEqual(window.data_file_label.fullText(), expected_path)
            self.assertEqual(window.data_file_label.toolTip(), expected_path)
            self.assertEqual(
                window.data_file_label.sizePolicy().horizontalPolicy(),
                QSizePolicy.Policy.Ignored,
            )
            self.assertIn("…", window.data_file_label.text())
            self.assertLessEqual(window.left_dock.minimumSizeHint().width(), baseline_minimum)
        finally:
            window.close()

    def test_sequence_edit_popup_receives_device_limits_from_main_config(self) -> None:
        window = MainWindow(self.config)
        try:
            command = next(
                item for item in window.document.commands
                if item.type is CommandType.SET_TEMPERATURE
            )
            observed: dict[str, float | str] = {}

            def inspect_and_reject(dialog) -> object:
                observed["minimum"] = dialog.inputs["target"].minimum()
                observed["maximum"] = dialog.inputs["target"].maximum()
                observed["max_rate"] = dialog.inputs["rate"].maximum()
                observed["summary"] = dialog.limit_label.text()
                return dialog.DialogCode.Rejected

            with patch("labcontrol.ui.main_window.CommandDialog.exec", new=inspect_and_reject):
                window._edit_command(command)

            self.assertEqual(observed["minimum"], 1.8)
            self.assertEqual(observed["maximum"], 400.0)
            self.assertEqual(observed["max_rate"], 30.0)
            self.assertIn("Configured limits (temperature)", observed["summary"])
            QCoreApplication.sendPostedEvents(
                None,
                QEvent.Type.DeferredDelete,
            )
            self.assertEqual(window.findChildren(CommandDialog), [])
        finally:
            window.close()

    def test_default_modules_directory_is_empty_and_has_no_measurement_tile(self) -> None:
        window = MainWindow(self.config)
        try:
            manager = window.module_manager
            self.assertEqual(manager.table.columnCount(), 3)
            self.assertEqual(
                [manager.table.horizontalHeaderItem(index).text() for index in range(3)],
                ["Enabled", "Name", "Version"],
            )
            self.assertEqual(set(window.status_tiles), {"temperature", "field", "second_stage"})
            self.assertEqual(window.module_descriptors, ())
            self.assertEqual(window.windowTitle(), self.config.title)
        finally:
            window.close()

    def test_dependency_install_requires_selected_module_to_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            shutil.copy2(
                ROOT / "configs" / "default.toml",
                root / "configs" / "default.toml",
            )
            shutil.copytree(
                MODULE_REPOSITORY / "modules",
                root / "modules",
            )
            window = MainWindow(
                load_config(root / "configs" / "default.toml")
            )
            try:
                window.enabled_modules.add("simulated_transport")
                with patch(
                    "labcontrol.ui.main_window.QMessageBox.warning"
                ) as warning:
                    window._install_module_dependencies(
                        "simulated_transport"
                    )
                warning.assert_called_once()
                self.assertIn(
                    "Disable this measurement module",
                    warning.call_args.args[2],
                )
            finally:
                window.close()

    def test_open_sequence_imports_module_settings_without_enabling_or_applying(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            shutil.copy2(
                ROOT / "configs" / "default.toml",
                root / "configs" / "default.toml",
            )
            shutil.copytree(
                MODULE_REPOSITORY / "modules",
                root / "modules",
            )
            sequence = root / "experiment.seq"
            sequence.write_text(
                "T Remark imported settings\n"
                "T End Sequence\n",
                encoding="utf-8",
            )
            save_sequence_module_settings(
                sequence,
                {
                    "simulated_transport": {
                        "delay_seconds": 0.375,
                    }
                },
                {"simulated_transport": "1.0.1"},
            )
            window = MainWindow(
                load_config(
                    root / "configs" / "default.toml"
                )
            )
            try:
                with (
                    patch(
                        "labcontrol.ui.main_window."
                        "QFileDialog.getOpenFileName",
                        return_value=(
                            str(sequence),
                            "Sequence (*.seq)",
                        ),
                    ),
                    patch.object(
                        window.runtime,
                        "enable_module",
                    ) as enable_module,
                    patch.object(
                        window.runtime,
                        "apply_module_settings",
                    ) as apply_settings,
                    patch(
                        "labcontrol.ui.main_window."
                        "QMessageBox.warning"
                    ) as warning,
                ):
                    window._open_sequence()

                self.assertEqual(
                    window.sequence_path,
                    sequence.resolve(),
                )
                self.assertEqual(
                    window._saved_module_settings(
                        "simulated_transport"
                    )["delay_seconds"],
                    0.375,
                )
                self.assertEqual(
                    window.enabled_modules,
                    set(),
                )
                self.assertFalse(
                    (
                        root
                        / "module_data"
                        / "simulated_transport"
                        / "settings.toml"
                    ).exists()
                )
                enable_module.assert_not_called()
                apply_settings.assert_not_called()
                warning.assert_not_called()
            finally:
                window.close()

    def test_save_sequence_writes_its_module_settings_companion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            shutil.copy2(
                ROOT / "configs" / "default.toml",
                root / "configs" / "default.toml",
            )
            sequence = root / "saved.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )
            window = MainWindow(
                load_config(
                    root / "configs" / "default.toml"
                )
            )
            try:
                window._set_document(
                    load_sequence(sequence).document,
                    SequenceModuleSettings(
                        {
                            "third_party_module": {
                                "gain": 4,
                            }
                        },
                        {
                            "third_party_module": (
                                "2.3.0"
                            )
                        },
                    ),
                )

                self.assertTrue(
                    window._save_sequence()
                )
                loaded = (
                    load_sequence_module_settings(
                        sequence
                    )
                )

                self.assertEqual(
                    loaded.settings,
                    {
                        "third_party_module": {
                            "gain": 4,
                        }
                    },
                )
                self.assertEqual(
                    loaded.versions,
                    {
                        "third_party_module": "2.3.0"
                    },
                )
            finally:
                window.close()

    def test_save_sequence_captures_enabled_module_window_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configs").mkdir()
            shutil.copy2(
                ROOT / "configs" / "default.toml",
                root / "configs" / "default.toml",
            )
            shutil.copytree(
                MODULE_REPOSITORY / "modules",
                root / "modules",
            )
            sequence = root / "enabled.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )
            window = MainWindow(
                load_config(
                    root / "configs" / "default.toml"
                )
            )
            fake_window = Mock()
            fake_window.settings.return_value = {
                "delay_seconds": 0.875,
            }
            try:
                window._set_document(
                    load_sequence(sequence).document
                )
                window.enabled_modules.add(
                    "simulated_transport"
                )
                window.module_windows[
                    "simulated_transport"
                ] = fake_window

                self.assertTrue(
                    window._save_sequence()
                )
                loaded = (
                    load_sequence_module_settings(
                        sequence
                    )
                )

                self.assertEqual(
                    loaded.settings[
                        "simulated_transport"
                    ]["delay_seconds"],
                    0.875,
                )
                self.assertEqual(
                    loaded.versions[
                        "simulated_transport"
                    ],
                    "1.0.1",
                )
            finally:
                window.enabled_modules.clear()
                window.module_windows.clear()
                window.close()

    def test_import_updates_enabled_window_but_never_applies_settings(
        self,
    ) -> None:
        window = MainWindow(self.config)
        fake_window = Mock()
        module_id = "simulated_transport"
        try:
            window.enabled_modules.add(module_id)
            window.module_windows[
                module_id
            ] = fake_window
            imported = {
                "delay_seconds": 0.625,
            }
            with patch.object(
                window.runtime,
                "apply_module_settings",
            ) as apply_settings:
                window._set_document(
                    parse_sequence(
                        "T End Sequence\n",
                        "loaded.seq",
                    ).document,
                    SequenceModuleSettings(
                        {module_id: imported},
                        {module_id: "1.0.1"},
                    ),
                )

            fake_window.load_settings.assert_called_once_with(
                imported,
                mark_unapplied=True,
            )
            apply_settings.assert_not_called()
            self.assertFalse(window._dirty)
        finally:
            window.enabled_modules.clear()
            window.module_windows.clear()
            window.close()

    def test_legacy_measure_parameters_block_run(self) -> None:
        window = MainWindow(self.config)
        try:
            document = parse_sequence(
                "T Measure devices=transport\nT End Sequence\n", "legacy.seq"
            ).document
            window._set_document(document)
            with (
                patch.object(window.runtime, "run_sequence") as run_sequence,
                patch("labcontrol.ui.main_window.QMessageBox.critical") as critical,
            ):
                window._run_sequence()
            run_sequence.assert_not_called()
            critical.assert_called_once()
            self.assertIn("Measure has no parameters", critical.call_args.args[2])
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
