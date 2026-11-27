from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDockWidget,
    QGroupBox,
    QMessageBox,
    QSizePolicy,
)

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.config import load_config  # noqa: E402
from labcontrol.models import (  # noqa: E402
    EventNotice,
    InstrumentConnectionState,
    InstrumentControlState,
    InstrumentKind,
    InstrumentSnapshot,
    LabEvent,
    RunProgress,
    RunState,
    Severity,
    StabilityState,
)
from labcontrol.sequence.model import (  # noqa: E402
    Command,
    CommandType,
    SequenceDocument,
)
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
from labcontrol.ui.preferences import (  # noqa: E402
    UiPreferences,
    UiPreferenceStore,
)
from tests.configuration_fixtures import (  # noqa: E402
    load_simulated_config,
    write_simulated_configuration,
)


class MainWindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])
        cls.config = load_simulated_config()
        configure_qt_appearance(cls.application, cls.config.ui_scale)

    def test_manual_set_closes_only_its_dialog_after_success(self):
        window = MainWindow(self.config)
        try:
            window._open_manual_control("temperature", "main")
            window._open_manual_control("field", "main")
            panel = self.config.instrument("temperature").panel("main")
            dialog = window.manual_dialogs[panel.key]
            other = window.manual_dialogs[self.config.instrument("field").panel("main").key]
            dialog.update_snapshot(InstrumentSnapshot(
                instrument_id="temperature", display_name="Temperature",
                kind=self.config.instrument("temperature").kind, timestamp=time.monotonic(),
                connection_state=InstrumentConnectionState.CONNECTED,
                controls={"main": InstrumentControlState(current=300.0, target=300.0)},
            ))
            future = Future()
            with patch.object(window.runtime, "set_target", return_value=future):
                dialog.apply_button.click()
            window._check_pending_manual_operations()
            self.assertTrue(dialog.isVisible())
            future.set_result(True)
            window._check_pending_manual_operations()
            self.assertFalse(dialog.isVisible())
            self.assertTrue(other.isVisible())
            self.assertEqual(window._pending_manual_operations, [])
        finally:
            window.close()

    def test_failed_or_unconfirmed_manual_set_keeps_dialog_open(self):
        for outcome in (False, RuntimeError("simulated write failure")):
            with self.subTest(outcome=outcome):
                window = MainWindow(self.config)
                try:
                    window._open_manual_control("temperature", "main")
                    dialog = window.manual_dialogs[self.config.instrument("temperature").panel("main").key]
                    dialog.update_snapshot(InstrumentSnapshot(
                        instrument_id="temperature", display_name="Temperature",
                        kind=self.config.instrument("temperature").kind, timestamp=time.monotonic(),
                        connection_state=InstrumentConnectionState.CONNECTED,
                        controls={"main": InstrumentControlState(current=300.0, target=300.0)},
                    ))
                    future = Future()
                    with patch.object(window.runtime, "set_target", return_value=future):
                        dialog.apply_button.click()
                    if isinstance(outcome, Exception):
                        future.set_exception(outcome)
                    else:
                        future.set_result(outcome)
                    window._check_pending_manual_operations()
                    self.assertTrue(dialog.isVisible())
                    self.assertIn("not confirmed" if outcome is False else "simulated write failure",
                                  window.statusBar().currentMessage())
                finally:
                    window.close()

    def test_data_browser_starts_in_mdi_and_can_detach_outside_the_main_viewport(self):
        from PySide6.QtCore import QPoint

        window = MainWindow(self.config)
        try:
            window.show()
            docked = window._show_data_browser()
            self.application.processEvents()
            self.assertIn(docked, window.mdi.subWindowList())
            browser = window.data_browser_windows[docked]
            self.assertEqual(browser.detach_action.text(), "Pop Out")

            # 点击 Pop Out 后弹出为无 owner 独立顶层窗，保持同一浏览器实例且可自由拖出主窗口。
            browser.detach_action.trigger()
            self.application.processEvents()
            viewer = window._window_for_data_browser(browser)
            self.assertIsNotNone(viewer)
            self.assertIsNot(viewer, docked)
            self.assertTrue(viewer.isWindow())
            self.assertIsNone(viewer.parent())
            self.assertNotIn(viewer, window.mdi.subWindowList())
            self.assertEqual(browser.detach_action.text(), "Dock")
            self.assertLess(viewer.minimumSizeHint().width(), 640)
            viewer.resize(480, 340)
            viewer.move(60, window.y() + window.height() + 20)
            position = viewer.pos()
            window._fit_mdi_windows()
            self.application.processEvents()
            self.assertEqual(viewer.pos(), position)
            self.assertLessEqual(viewer.width(), 480)

            # 点击 Dock 可无损放回主窗口中央 MDI 区域。
            browser.detach_action.trigger()
            self.application.processEvents()
            redocked = window._window_for_data_browser(browser)
            self.assertIsNotNone(redocked)
            self.assertIn(redocked, window.mdi.subWindowList())
            self.assertEqual(browser.detach_action.text(), "Pop Out")

            # 拖出中央 MDI 边界同样弹出为无 owner 独立顶层窗。
            viewport = window.mdi.viewport()
            inside_point = viewport.mapToGlobal(viewport.rect().center())
            outside_point = viewport.mapToGlobal(
                QPoint(viewport.width() + 80, viewport.height() + 80)
            )
            self.assertFalse(redocked._should_detach_at(inside_point))
            self.assertTrue(redocked._should_detach_at(outside_point))
            dragged_out = window._detach_data_browser(redocked, drop_pos=outside_point)
            self.application.processEvents()
            self.assertIsNotNone(dragged_out)
            self.assertTrue(dragged_out.isWindow())
            self.assertIsNone(dragged_out.parent())
            self.assertIs(window.data_browser_windows[dragged_out], browser)
        finally:
            window.close()

    def test_dragging_top_resize_border_at_mdi_top_does_not_detach_data_browser(
        self,
    ) -> None:
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QMouseEvent

        window = MainWindow(self.config)
        try:
            window.show()
            docked = window._show_data_browser()
            self.application.processEvents()
            docked.move(docked.x(), 0)
            self.application.processEvents()

            viewport = window.mdi.viewport()
            browser = window.data_browser_windows[docked]
            child_rect = browser.geometry()
            mid_x = float(docked.width() // 2)
            outside_top = QPointF(
                viewport.mapToGlobal(QPoint(docked.x() + docked.width() // 2, -60))
            )

            # 1) 贴住 MDI 顶部拖动上边框（y=1）向上缩放：即使 Qt 钳位尺寸不变，也绝不误弹出。
            top_border_local = QPointF(mid_x, 1.0)
            top_border_global = QPointF(docked.mapToGlobal(top_border_local.toPoint()))
            press = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                top_border_local,
                top_border_global,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                top_border_local,
                outside_top,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            docked.mousePressEvent(press)
            docked.mouseReleaseEvent(release)
            QTest.qWait(20)
            self.application.processEvents()
            self.assertIn(docked, window.mdi.subWindowList())
            self.assertIs(window._window_for_data_browser(browser), docked)

            # 2) 鼠标移回真正标题栏中央（清除上一步残留的缩放光标），按住拖出顶部：正常弹出无 owner 独立窗。
            resize_border = docked._resize_border_width(child_rect)
            title_y = float((resize_border + child_rect.top()) // 2)
            title_local = QPointF(mid_x, title_y)
            title_global = QPointF(docked.mapToGlobal(title_local.toPoint()))
            move_title = QMouseEvent(
                QEvent.Type.MouseMove,
                title_local,
                title_global,
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            press_title = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                title_local,
                title_global,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            release_title = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                title_local,
                outside_top,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            docked.mouseMoveEvent(move_title)
            docked.mousePressEvent(press_title)
            docked.mouseReleaseEvent(release_title)
            QTest.qWait(20)
            self.application.processEvents()
            detached = window._window_for_data_browser(browser)
            self.assertIsNotNone(detached)
            self.assertIsNot(detached, docked)
            self.assertTrue(detached.isWindow())
            self.assertIsNone(detached.parent())
            self.assertNotIn(detached, window.mdi.subWindowList())
        finally:
            window.close()

    def test_loaded_sequence_restores_dat_and_next_start_restores_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sequence = root / "remembered.seq"
            sequence.write_text('T Set Datafile open|create external saved.dat\nT Measure\nT End Sequence\n', encoding="utf-8")
            store = UiPreferenceStore(root / "ui.ini")
            first = MainWindow(self.config, ui_preference_store=store)
            try:
                with patch("labcontrol.ui.main_window.QFileDialog.getOpenFileName", return_value=(str(sequence), "")):
                    first._open_sequence()
                self.assertEqual(first.data_file_label.fullText(), "saved.dat")
                self.assertTrue(first._datafile_selected)
                self.assertEqual(store.last_sequence(), sequence.resolve())
            finally:
                first.close()
            second = MainWindow(self.config, ui_preference_store=store)
            try:
                self.assertEqual(second.sequence_path, sequence.resolve())
                self.assertEqual(second.data_file_label.fullText(), "saved.dat")
                second._new_sequence()
                self.assertEqual(second.data_file_label.fullText(), "")
                self.assertIsNone(store.last_sequence())
            finally:
                second.close()

    def test_running_sequence_keeps_editor_enabled_and_live_controls_locked(self):
        window = MainWindow(self.config)
        try:
            window._set_document(SequenceDocument([Command(CommandType.REMARK, {"text": "original"})]))
            with patch.object(window.runtime, "run_sequence"):
                window._start_sequence({})
            self.assertTrue(window.command_tree.isEnabled())
            self.assertTrue(window.change_sequence_button.isEnabled())
            self.assertFalse(window.modules_action.isEnabled())
            self.assertFalse(window.run_button.isEnabled())
            window.editor.list.setCurrentRow(0)
            added = Command(CommandType.REMARK, {"text": "next run"})
            window.editor.insert_command(added)
            self.assertEqual(window.document.commands[0], added)
            self.assertTrue(window._dirty)
            self.assertIn("next Run", window.editor.toolTip())
            window._handle_progress(RunProgress(state=RunState.COMPLETED, message="done"))
            self.assertTrue(window.modules_action.isEnabled())
        finally:
            window.current_run_state = RunState.IDLE
            window.close()

    def test_loading_sequence_during_run_defers_module_settings_until_run_ends(self):
        from labcontrol.sequence.module_settings import SequenceModuleSettings
        window = MainWindow(self.config)
        try:
            class FakeModuleWindow:
                def __init__(self) -> None:
                    self.current = {"current_a": 0.003}
                    self.unapplied = False
                    self.calls: list[tuple[dict[str, object], bool]] = []

                def settings(self) -> dict[str, object]:
                    return deepcopy(self.current)

                def load_settings(self, values: dict[str, object], *, mark_unapplied: bool = False) -> None:
                    self.calls.append((deepcopy(values), mark_unapplied))
                    self.current = deepcopy(values)
                    self.unapplied = mark_unapplied

                def has_unapplied_edits(self) -> bool:
                    return self.unapplied

                def isHidden(self) -> bool:
                    return False

                def set_sequence_running(self, running: bool) -> None:
                    pass

                def allow_application_close(self) -> None:
                    pass

                def close(self) -> bool:
                    return True

            fake_module_window = FakeModuleWindow()
            descriptor = window.module_descriptors[0]
            module_id = descriptor.id
            window.enabled_modules.add(module_id)
            window.module_windows[module_id] = fake_module_window
            window.current_run_state = RunState.RUNNING
            imported = SequenceModuleSettings(
                {module_id: {"current_a": 0.0001}},
                {module_id: descriptor.version},
            )
            window._set_document(
                SequenceDocument([Command(CommandType.REMARK, {"text": "next"})]),
                imported,
            )
            # During the active Run, the locked module window stays at old 3 mA.
            self.assertEqual(fake_module_window.calls, [])
            self.assertEqual(fake_module_window.settings()["current_a"], 0.003)
            self.assertTrue(window._sequence_module_window_issues)
            # Saving SEQ while Run is still active keeps imported 0.1 mA instead of old 3 mA.
            collected_during_run, _ = window._collect_sequence_module_settings()
            self.assertEqual(collected_during_run[module_id]["current_a"], 0.0001)

            # Once the active Run finishes, deferred 0.1 mA loads into the unlocked window as unapplied.
            window._handle_progress(RunProgress(state=RunState.COMPLETED, message="done"))
            self.assertEqual(len(fake_module_window.calls), 1)
            self.assertEqual(fake_module_window.settings()["current_a"], 0.0001)
            self.assertTrue(fake_module_window.has_unapplied_edits())
            self.assertEqual(window._deferred_sequence_module_settings, {})

            # Next Run collects the imported 0.1 mA (not overwritten by old 3 mA).
            with patch("labcontrol.ui.main_window.save_settings"):
                next_run_settings = window._save_and_collect_enabled_module_settings()
            self.assertEqual(next_run_settings[module_id]["current_a"], 0.0001)
        finally:
            window.current_run_state = RunState.IDLE
            window._deferred_sequence_module_settings.clear()
            for key, mod_win in list(window.module_windows.items()):
                if mod_win is fake_module_window:
                    window.module_windows.pop(key, None)
                    window.enabled_modules.discard(key)
            window.close()

    def test_pending_run_snapshots_validated_document_before_module_apply_finishes(self):
        window = MainWindow(self.config)
        try:
            original = SequenceDocument([Command(CommandType.REMARK, {"text": "validated"})])
            window._set_document(original)
            future = Future()
            window._pending_run = (deepcopy(original), {"demo": {"v": 1}}, [future])
            window.document.commands[0].update_params({"text": "edited while applying"})
            future.set_result(None)
            started = []
            with patch.object(
                window.runtime,
                "run_sequence",
                side_effect=lambda doc, settings: started.append((doc, settings)),
            ):
                window._check_pending_run()
            self.assertEqual(len(started), 1)
            self.assertEqual(started[0][0].commands[0].params["text"], "validated")
        finally:
            window.current_run_state = RunState.IDLE
            window.close()

    def test_floating_windows_stay_inside_minimum_viewport(self) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1180, 720)
            window.show()
            data_window = window._show_data_browser()
            self.application.processEvents()
            window._fit_mdi_windows()
            self.application.processEvents()

            viewport = window.mdi.viewport().rect()
            for subwindow in (window.sequence_window, data_window):
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

    def test_sequence_edit_popup_receives_instrument_limits_from_main_config(self) -> None:
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
            self.assertIn(
                "Configured limits (temperature.main)",
                observed["summary"],
            )
            QCoreApplication.sendPostedEvents(
                None,
                QEvent.Type.DeferredDelete,
            )
            self.assertEqual(window.findChildren(CommandDialog), [])
        finally:
            window.close()

    def test_bundled_modules_are_discovered_but_start_disabled(self) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1180, 720)
            window.show()
            self.application.processEvents()
            manager = window.module_manager
            self.assertEqual(manager.table.columnCount(), 3)
            self.assertEqual(
                [manager.table.horizontalHeaderItem(index).text() for index in range(3)],
                ["Enabled", "Name", "Version"],
            )
            self.assertEqual(
                set(window.status_panel.panels),
                {
                    "temperature.main",
                    "field.main",
                    "second_stage.main",
                },
            )
            self.assertEqual(
                window.status_scroll.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAsNeeded,
            )
            self.assertEqual(
                [descriptor.id for descriptor in window.module_descriptors],
                ["simulated_transport", "tutorial_resistance"],
            )
            self.assertEqual(window.windowTitle(), self.config.title)
            self.assertEqual(
                window.appearance_action.text(),
                "Appearance…",
            )
            self.assertEqual(
                window.command_hint.text(),
                "Double-click a command to configure and insert it",
            )
            self.assertTrue(window.command_hint.wordWrap())
            self.assertEqual(
                window.command_hint.sizePolicy().horizontalPolicy(),
                QSizePolicy.Policy.Ignored,
            )
            self.assertEqual(
                window.left_dock.features(),
                QDockWidget.DockWidgetFeature.NoDockWidgetFeatures,
            )
            self.assertEqual(
                window.command_dock.features(),
                QDockWidget.DockWidgetFeature.NoDockWidgetFeatures,
            )
            self.assertTrue(window.command_dock.isHidden())
            window._focus_sequence()
            self.application.processEvents()
            self.assertFalse(window.command_dock.isHidden())
            self.assertIsNotNone(window.status_dock.titleBarWidget())
            self.assertEqual(window.status_dock.titleBarWidget().height(), 0)
            compact_width = window.command_dock.minimumWidth()
            window.resizeDocks(
                [window.command_dock],
                [compact_width + 180],
                Qt.Orientation.Horizontal,
            )
            self.application.processEvents()
            expanded_width = window.command_dock.width()
            window.resizeDocks(
                [window.command_dock],
                [compact_width],
                Qt.Orientation.Horizontal,
            )
            self.application.processEvents()
            self.assertLess(
                window.command_dock.width(),
                expanded_width,
            )
            status_group = window.findChild(
                QGroupBox,
                "statusGroup",
            )
            self.assertIsNotNone(status_group)
            self.assertGreaterEqual(
                window.module_monitor_group.geometry().top(),
                status_group.geometry().bottom(),
            )
            self.assertGreater(
                window.run_button.geometry().top(),
                window.module_monitor_group.geometry().bottom(),
            )
            self.assertFalse(
                window.module_monitor_scroll
                .horizontalScrollBar()
                .isVisible()
            )
        finally:
            window.close()

    def test_multiple_data_browsers_keep_independent_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.dat"
            second_path = Path(directory) / "second.dat"
            first_path.write_text(
                "[Data]\nX,Y\n0,1\n",
                encoding="utf-8",
            )
            second_path.write_text(
                "[Data]\nX,Y\n0,2\n",
                encoding="utf-8",
            )
            window = MainWindow(self.config)
            try:
                window.show()
                first_window = window._show_data_browser(
                    first_path
                )
                second_window = window._show_data_browser(
                    second_path
                )
                self.application.processEvents()
                first_browser = window.data_browser_windows[
                    first_window
                ]
                second_browser = window.data_browser_windows[
                    second_window
                ]

                self.assertIsNot(first_window, second_window)
                self.assertIsNot(first_browser, second_browser)
                self.assertEqual(
                    first_browser.current_path,
                    first_path.resolve(),
                )
                self.assertEqual(
                    second_browser.current_path,
                    second_path.resolve(),
                )
                self.assertEqual(
                    first_window.windowTitle(),
                    "first.dat - Data Browser",
                )
                self.assertEqual(
                    second_window.windowTitle(),
                    "second.dat - Data Browser",
                )
                first_window.close()
                QCoreApplication.sendPostedEvents(
                    None,
                    QEvent.Type.DeferredDelete,
                )
                self.application.processEvents()

                self.assertNotIn(
                    first_window,
                    window.data_browser_windows,
                )
                self.assertIn(
                    second_window,
                    window.data_browser_windows,
                )
                self.assertTrue(second_window.isVisible())
                self.assertEqual(
                    second_browser.current_path,
                    second_path.resolve(),
                )
            finally:
                window.close()

    def test_view_data_loads_active_file_after_close_and_reopen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active.dat"
            path.write_text(
                "[Data]\nX,Y\n0,1\n",
                encoding="utf-8",
            )
            window = MainWindow(self.config)
            try:
                window.show()
                window._handle_event(
                    EventNotice(
                        LabEvent(
                            key="logging:DATAFILE_SELECTED",
                            severity=Severity.INFO,
                            source="logging",
                            code="DATAFILE_SELECTED",
                            message=str(path),
                        ),
                        show_popup=False,
                    )
                )

                window._view_data()
                self.application.processEvents()
                first_window = next(
                    reversed(window.data_browser_windows)
                )
                first_browser = window.data_browser_windows[
                    first_window
                ]
                self.assertEqual(
                    first_browser.current_path,
                    path.resolve(),
                )

                first_window.close()
                QCoreApplication.sendPostedEvents(
                    None,
                    QEvent.Type.DeferredDelete,
                )
                self.application.processEvents()
                self.assertNotIn(
                    first_window,
                    window.data_browser_windows,
                )

                window._view_data()
                self.application.processEvents()
                second_window = next(
                    reversed(window.data_browser_windows)
                )
                self.assertIsNot(second_window, first_window)
                self.assertEqual(
                    window.data_browser_windows[
                        second_window
                    ].current_path,
                    path.resolve(),
                )
            finally:
                window.close()

    def test_runtime_status_moves_from_starting_to_ready(self) -> None:
        window = MainWindow(self.config)
        try:
            self.assertIn(
                "Starting instrument runtime",
                window.statusBar().currentMessage(),
            )
            snapshots = {
                instrument.id: InstrumentSnapshot(
                    instrument_id=instrument.id,
                    display_name=instrument.display_name,
                    kind=instrument.kind,
                    timestamp=time.monotonic(),
                    current=0.0,
                    controls={
                        panel.id: InstrumentControlState(
                            current=0.0,
                            target=0.0,
                        )
                        for panel in instrument.panels
                        if panel.template == "controller"
                    },
                )
                for instrument in self.config.instrument_instances
            }

            window._handle_snapshots(snapshots)

            self.assertTrue(
                window.statusBar().currentMessage().startswith(
                    f"Ready · {len(snapshots)}/{len(snapshots)} "
                    "instruments connected"
                )
            )
        finally:
            window.close()

    def test_runtime_status_reports_connection_warnings(self) -> None:
        window = MainWindow(self.config)
        try:
            snapshots = {
                instrument.id: InstrumentSnapshot(
                    instrument_id=instrument.id,
                    display_name=instrument.display_name,
                    kind=instrument.kind,
                    timestamp=time.monotonic(),
                    current=0.0,
                    controls={
                        panel.id: InstrumentControlState(
                            current=0.0,
                            target=0.0,
                        )
                        for panel in instrument.panels
                        if panel.template == "controller"
                    },
                )
                for instrument in self.config.instrument_instances
            }
            window._handle_snapshots(snapshots)
            disconnected_id = self.config.instrument_instances[0].id
            snapshots[disconnected_id].connection_state = (
                InstrumentConnectionState.DISCONNECTED
            )

            window._handle_snapshots(snapshots)

            self.assertTrue(
                window.statusBar().currentMessage().startswith(
                    f"Ready with instrument warnings · {len(snapshots) - 1}/"
                    f"{len(snapshots)} instruments connected"
                )
            )
        finally:
            window.close()

    def test_clean_configuration_constructs_and_closes_without_panels(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            configs = root / "configs"
            configs.mkdir()
            general = configs / "general.toml"
            shutil.copy2(ROOT / "configs" / "general.default.toml", general)
            (configs / "instruments").mkdir()

            window = MainWindow(load_config(general))
            try:
                self.assertEqual(window.status_panel.panels, {})
            finally:
                window.close()

    def test_close_running_sequence_explains_system_instruments_continue(self) -> None:
        window = MainWindow(self.config)
        try:
            window.current_run_state = RunState.RUNNING
            with patch(
                "labcontrol.ui.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as question:
                self.assertFalse(window.close())

            message = question.call_args.args[2]
            self.assertIn("will not send Set or Hold", message)
            self.assertIn("current actions will continue", message)
        finally:
            window.current_run_state = RunState.IDLE
            window.close()

    def test_remembered_window_layout_round_trips(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = UiPreferenceStore(
                Path(directory) / "ui.ini"
            )
            preferences = UiPreferences(
                1.0,
                0.80,
                "remember",
            )
            first = MainWindow(
                self.config,
                ui_preferences=preferences,
                ui_preference_store=store,
            )
            first.show()
            self.application.processEvents()
            first.resize(1260, 760)
            first.trend_dialog.resize(710, 480)
            first.module_manager.resize(620, 430)
            first.sequence_window.setGeometry(
                17,
                23,
                650,
                420,
            )
            expected_main_size = first.size()
            expected_trend_size = first.trend_dialog.size()
            expected_manager_size = first.module_manager.size()
            expected_sequence = first.sequence_window.geometry()
            first.close()
            self.application.processEvents()

            restored = MainWindow(
                self.config,
                ui_preferences=preferences,
                ui_preference_store=store,
            )
            try:
                # offscreen 测试屏只有约 800 px，而主窗口最低宽度为 1180；Qt 的
                # restoreGeometry 会主动夹到可见屏幕/最低尺寸。高度仍能证明保存
                # 几何确实被读取，真实大屏则会保留完整宽度。
                self.assertEqual(
                    restored.height(),
                    expected_main_size.height(),
                )
                self.assertGreaterEqual(
                    restored.width(),
                    restored.minimumWidth(),
                )
                self.assertEqual(
                    restored.trend_dialog.size(),
                    expected_trend_size,
                )
                self.assertEqual(
                    restored.module_manager.size(),
                    expected_manager_size,
                )
                self.assertEqual(
                    restored.sequence_window.geometry(),
                    expected_sequence,
                )
                self.assertFalse(
                    restored.should_start_maximized()
                )
            finally:
                restored.close()

    def test_maximized_startup_mode_needs_no_saved_geometry(
        self,
    ) -> None:
        window = MainWindow(
            self.config,
            ui_preferences=UiPreferences(
                None,
                1.0,
                "maximized",
            ),
        )
        try:
            self.assertTrue(window.should_start_maximized())
        finally:
            window.close()

    def test_live_trend_stays_connected_under_continuous_simulated_polling(
        self,
    ) -> None:
        window = MainWindow(self.config)
        try:
            window.show()
            window._show_graph()
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(20)

            self.assertTrue(window.trend_dialog.isVisible())
            self.assertEqual(
                set(window.current_snapshots),
                {
                    instrument.id
                    for instrument in self.config.instrument_instances
                },
            )
            self.assertTrue(
                all(
                    snapshot.connected
                    and snapshot.connection_state.value
                    == "connected"
                    and snapshot.stability.value
                    not in {"timed_out", "stale"}
                    for snapshot in window.current_snapshots.values()
                )
            )
            self.assertTrue(
                all(
                    len(points) >= 2
                    for points
                    in window.trend_dialog.canvas.history.values()
                )
            )
            active_events = (
                ()
                if window.runtime.events is None
                else window.runtime.events.active_events()
            )
            self.assertEqual(
                [
                    event.code
                    for event in active_events
                    if (
                        "TIMEOUT" in event.code
                        or event.code
                        in {
                            "POLL_LOOP_FAILED",
                            "STALE_READING",
                        }
                    )
                ],
                [],
            )
        finally:
            window.close()

    def test_module_enables_without_application_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(
                ROOT / "modules",
                root / "modules",
            )
            window = MainWindow(
                load_config(
                    general
                )
            )
            try:
                window._set_module_enabled(
                    "simulated_transport",
                    True,
                )

                deadline = time.monotonic() + 5.0
                while (
                    "simulated_transport"
                    not in window.enabled_modules
                    and time.monotonic() < deadline
                ):
                    QTest.qWait(20)
                    window._drain_runtime_messages()

                self.assertIn(
                    "simulated_transport",
                    window.enabled_modules,
                    (
                        f"states={window.module_manager._states!r}; "
                        f"status={window.statusBar().currentMessage()!r}; "
                        f"pending={window._pending_module_operations!r}"
                    ),
                )
                self.assertEqual(
                    window.module_manager.runtime_state(
                        "simulated_transport"
                    ),
                    "enabled",
                )
                card = window.module_monitor_cards.get(
                    "simulated_transport"
                )
                self.assertIsNotNone(card)
                self.assertEqual(
                    card._display_columns,
                    ("R1", "R2", "R3", "R4"),
                )
                self.assertEqual(
                    card.results_label.text(),
                    "No results yet",
                )
            finally:
                window.close()

    def test_failed_module_enable_future_restores_disabled_controls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(
                ROOT / "modules",
                root / "modules",
            )
            window = MainWindow(
                load_config(
                    general
                )
            )
            try:
                failed: Future[object] = Future()
                failed.set_exception(
                    RuntimeError("simulated enable failure")
                )
                checkbox = (
                    window.module_manager._checkboxes[
                        "simulated_transport"
                    ]
                )
                with patch.object(
                    window.runtime,
                    "enable_module",
                    return_value=failed,
                ):
                    checkbox.click()

                self.assertFalse(checkbox.isEnabled())
                self.assertIn(
                    "simulated_transport",
                    window._pending_module_operations,
                )

                window._check_pending_module_operations()

                self.assertNotIn(
                    "simulated_transport",
                    window._pending_module_operations,
                )
                self.assertFalse(checkbox.isChecked())
                self.assertTrue(checkbox.isEnabled())
                self.assertEqual(
                    window.module_manager.runtime_state(
                        "simulated_transport"
                    ),
                    "disabled",
                )
                self.assertIn(
                    "simulated enable failure",
                    window.statusBar().currentMessage(),
                )
            finally:
                window.close()

    def test_open_sequence_prompts_and_keeps_current_state_when_declined(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(
                ROOT / "modules",
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
                {"simulated_transport": "2.0.1"},
            )
            window = MainWindow(
                load_config(
                    general
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
                        window,
                        "_confirm_sequence_module_sync",
                        return_value=False,
                    ) as confirm_sync,
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
                confirm_sync.assert_called_once()
                plan = confirm_sync.call_args.args[1]
                self.assertEqual(plan.to_enable, ("simulated_transport",))
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

    def test_open_sequence_syncs_modules_and_fills_pending_settings_when_confirmed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(
                ROOT / "modules",
                root / "modules",
            )
            sequence = root / "sync.seq"
            sequence.write_text(
                "T Remark sync test\n"
                "T End Sequence\n",
                encoding="utf-8",
            )
            window = MainWindow(load_config(general))
            try:
                # Start with simulated_transport enabled (extra module that sync.seq does not use).
                window._set_module_enabled("simulated_transport", True)
                deadline = time.monotonic() + 5.0
                while "simulated_transport" not in window.enabled_modules and time.monotonic() < deadline:
                    QTest.qWait(20)
                    window._drain_runtime_messages()
                self.assertIn("simulated_transport", window.enabled_modules)

                # Prepare sync.seq with only tutorial_resistance and custom pending parameters.
                window._set_module_enabled("tutorial_resistance", True)
                deadline = time.monotonic() + 5.0
                while "tutorial_resistance" not in window.enabled_modules and time.monotonic() < deadline:
                    QTest.qWait(20)
                    window._drain_runtime_messages()
                tut_window = window.module_windows["tutorial_resistance"]
                expected_settings = tut_window.settings()
                expected_settings["delay_seconds"] = 0.375
                expected_settings["base_resistance_ohm"] = 250.0
                save_sequence_module_settings(
                    sequence,
                    {"tutorial_resistance": expected_settings},
                    {"tutorial_resistance": tut_window.descriptor.version},
                )

                # Disable tutorial_resistance so only simulated_transport is enabled before Open SEQ.
                window._set_module_enabled("tutorial_resistance", False)
                deadline = time.monotonic() + 5.0
                while "tutorial_resistance" in window.enabled_modules and time.monotonic() < deadline:
                    QTest.qWait(20)
                    window._drain_runtime_messages()
                self.assertEqual(window.enabled_modules, {"simulated_transport"})

                # 1) Open sync.seq and confirm -> disables simulated_transport, enables tutorial_resistance,
                #    fills delay_seconds=0.375 / base_resistance_ohm=250.0 as unapplied, and NEVER applies.
                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(sequence), "Sequence (*.seq)"),
                    ),
                    patch.object(
                        window,
                        "_confirm_sequence_module_sync",
                        return_value=True,
                    ) as confirm_sync,
                    patch.object(
                        window.runtime,
                        "apply_module_settings",
                    ) as apply_settings,
                    patch("labcontrol.ui.main_window.QMessageBox.warning") as warning,
                ):
                    window._open_sequence()
                    deadline = time.monotonic() + 5.0
                    while (
                        window.enabled_modules != {"tutorial_resistance"}
                        and time.monotonic() < deadline
                    ):
                        QTest.qWait(20)
                        window._drain_runtime_messages()

                confirm_sync.assert_called_once()
                plan = confirm_sync.call_args.args[1]
                self.assertEqual(plan.to_enable, ("tutorial_resistance",))
                self.assertEqual(plan.to_disable, ("simulated_transport",))
                self.assertEqual(window.enabled_modules, {"tutorial_resistance"})
                self.assertAlmostEqual(
                    window.module_windows["tutorial_resistance"].settings()["delay_seconds"],
                    0.375,
                )
                self.assertAlmostEqual(
                    window.module_windows["tutorial_resistance"].settings()["base_resistance_ohm"],
                    250.0,
                )
                self.assertTrue(window.module_windows["tutorial_resistance"].has_unapplied_edits())
                apply_settings.assert_not_called()
                warning.assert_not_called()

                # 2) Re-opening the same sync.seq when module set & pending settings already match -> NO popup.
                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(sequence), "Sequence (*.seq)"),
                    ),
                    patch.object(
                        window,
                        "_confirm_sequence_module_sync",
                        side_effect=AssertionError("Should not prompt when state already matches"),
                    ),
                ):
                    window._open_sequence()

                # 3) Change pending setting in the Settings tab, then re-open sync.seq -> prompts again & restores 0.375.
                modified = window.module_windows["tutorial_resistance"].settings()
                modified["delay_seconds"] = 0.9
                window.module_windows["tutorial_resistance"].load_settings(modified, mark_unapplied=False)
                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(sequence), "Sequence (*.seq)"),
                    ),
                    patch.object(
                        window,
                        "_confirm_sequence_module_sync",
                        return_value=True,
                    ) as confirm_again,
                    patch.object(
                        window.runtime,
                        "apply_module_settings",
                    ) as apply_again,
                ):
                    window._open_sequence()

                confirm_again.assert_called_once()
                update_plan = confirm_again.call_args.args[1]
                self.assertEqual(update_plan.to_enable, ())
                self.assertEqual(update_plan.to_disable, ())
                self.assertEqual(update_plan.to_update_settings, ("tutorial_resistance",))
                self.assertAlmostEqual(
                    window.module_windows["tutorial_resistance"].settings()["delay_seconds"],
                    0.375,
                )
                self.assertTrue(window.module_windows["tutorial_resistance"].has_unapplied_edits())
                apply_again.assert_not_called()
            finally:
                window.close()

    def test_save_sequence_clears_disabled_module_and_records_zero_module_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(ROOT / "modules", root / "modules")
            sequence = root / "state.seq"
            sequence.write_text("T End Sequence\n", encoding="utf-8")
            window = MainWindow(load_config(general))
            try:
                window._set_document(load_sequence(sequence).document)
                window._set_module_enabled("simulated_transport", True)
                deadline = time.monotonic() + 5.0
                while "simulated_transport" not in window.enabled_modules and time.monotonic() < deadline:
                    QTest.qWait(20)
                    window._drain_runtime_messages()
                self.assertTrue(window._save_sequence())
                self.assertIn(
                    "simulated_transport",
                    load_sequence_module_settings(sequence).settings,
                )

                window._set_module_enabled("simulated_transport", False)
                deadline = time.monotonic() + 5.0
                while "simulated_transport" in window.enabled_modules and time.monotonic() < deadline:
                    QTest.qWait(20)
                    window._drain_runtime_messages()
                self.assertTrue(window._save_sequence())
                loaded = load_sequence_module_settings(sequence)
                self.assertEqual(loaded.settings, {})
                self.assertTrue(loaded.has_saved_state)
            finally:
                window.close()

    def test_startup_restores_last_enabled_modules_and_pending_settings_without_applying(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(ROOT / "modules", root / "modules")
            store = UiPreferenceStore(root / "ui.ini")
            first = MainWindow(load_config(general), ui_preference_store=store)
            try:
                first._set_module_enabled("tutorial_resistance", True)
                deadline = time.monotonic() + 5.0
                while "tutorial_resistance" not in first.enabled_modules and time.monotonic() < deadline:
                    QTest.qWait(20)
                    first._drain_runtime_messages()
                mod_win = first.module_windows["tutorial_resistance"]
                edited = mod_win.settings()
                edited["delay_seconds"] = 0.625
                edited["base_resistance_ohm"] = 180.0
                mod_win.load_settings(edited, mark_unapplied=True)
            finally:
                first.close()

            self.assertEqual(store.last_enabled_modules(), ("tutorial_resistance",))

            second = MainWindow(load_config(general), ui_preference_store=store)
            try:
                with patch.object(
                    second.runtime,
                    "apply_module_settings",
                ) as apply_settings:
                    deadline = time.monotonic() + 5.0
                    while "tutorial_resistance" not in second.enabled_modules and time.monotonic() < deadline:
                        QTest.qWait(20)
                        second._drain_runtime_messages()

                self.assertEqual(second.enabled_modules, {"tutorial_resistance"})
                restored_win = second.module_windows["tutorial_resistance"]
                self.assertAlmostEqual(restored_win.settings()["delay_seconds"], 0.625)
                self.assertAlmostEqual(restored_win.settings()["base_resistance_ohm"], 180.0)
                self.assertTrue(restored_win.has_unapplied_edits())
                self.assertIn("not applied", restored_win.message_label.text().casefold())
                apply_settings.assert_not_called()
            finally:
                second.close()

    def test_sync_disables_initializing_module_and_reenables_disabling_module(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(ROOT / "modules", root / "modules")
            empty_seq = root / "empty.seq"
            empty_seq.write_text("T End Sequence\n", encoding="utf-8")
            save_sequence_module_settings(empty_seq, {}, {})

            tut_seq = root / "tut.seq"
            tut_seq.write_text("T End Sequence\n", encoding="utf-8")

            window = MainWindow(load_config(general))
            tut_version = window._module_descriptor("tutorial_resistance").version
            save_sequence_module_settings(
                tut_seq,
                {
                    "tutorial_resistance": {
                        "base_resistance_ohm": 220.0,
                        "channel_step_ohm": 10.0,
                        "delay_seconds": 0.375,
                        "noise_ohm": 0.001,
                        "over_range_ohm": 1e6,
                    }
                },
                {"tutorial_resistance": tut_version},
            )
            try:
                # Part A: simulated_transport is mid-initializing -> open empty.seq and confirm ->
                # disable is queued and executes immediately once enable finishes, never flashing a window.
                in_flight_enable: Future[object] = Future()
                in_flight_disable: Future[object] = Future()
                with patch.object(
                    window.runtime,
                    "enable_module",
                    return_value=in_flight_enable,
                ):
                    window._set_module_enabled("simulated_transport", True)
                self.assertIn("simulated_transport", window._pending_module_operations)

                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(empty_seq), "Sequence (*.seq)"),
                    ),
                    patch.object(
                        window,
                        "_confirm_sequence_module_sync",
                        return_value=True,
                    ) as confirm_disable,
                    patch.object(
                        window.runtime,
                        "disable_module",
                        return_value=in_flight_disable,
                    ) as disable_mock,
                ):
                    window._open_sequence()
                    confirm_disable.assert_called_once()
                    self.assertEqual(confirm_disable.call_args.args[1].to_disable, ("simulated_transport",))
                    self.assertIs(window._queued_module_enabled.get("simulated_transport"), False)
                    disable_mock.assert_not_called()

                    # Enable completes: handler skips flashing a window, then pending-op reconciler disables.
                    window._handle_module_state(
                        {
                            "module_id": "simulated_transport",
                            "enabled": True,
                            "state": "enabled",
                            "status": {},
                            "message": "Enabled",
                        }
                    )
                    self.assertNotIn("simulated_transport", window.module_windows)
                    in_flight_enable.set_result(None)
                    window._check_pending_module_operations()
                    disable_mock.assert_called_once_with("simulated_transport")

                # Disable completes -> module settles cleanly as disabled.
                window._handle_module_state(
                    {
                        "module_id": "simulated_transport",
                        "enabled": False,
                        "state": "disabled",
                        "status": {},
                        "message": "Disabled",
                    }
                )
                in_flight_disable.set_result(None)
                window._check_pending_module_operations()
                self.assertNotIn("simulated_transport", window.enabled_modules)
                self.assertEqual(
                    window.module_manager.runtime_state("simulated_transport"),
                    "disabled",
                )

                # Part B: tutorial_resistance is mid-disabling -> open tut.seq and confirm ->
                # re-enable is queued and runs once disable completes, preserving SEQ 0.375 settings.
                window.enabled_modules.add("tutorial_resistance")
                disabling_future: Future[object] = Future()
                reenable_future: Future[object] = Future()
                with patch.object(
                    window.runtime,
                    "disable_module",
                    return_value=disabling_future,
                ):
                    window._set_module_enabled("tutorial_resistance", False)

                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(tut_seq), "Sequence (*.seq)"),
                    ),
                    patch.object(
                        window,
                        "_confirm_sequence_module_sync",
                        return_value=True,
                    ),
                    patch.object(
                        window.runtime,
                        "enable_module",
                        return_value=reenable_future,
                    ) as reenable_mock,
                    patch.object(
                        window.runtime,
                        "apply_module_settings",
                    ) as apply_mock,
                ):
                    window._open_sequence()
                    self.assertIs(window._queued_module_enabled.get("tutorial_resistance"), True)
                    reenable_mock.assert_not_called()

                    window._handle_module_state(
                        {
                            "module_id": "tutorial_resistance",
                            "enabled": False,
                            "state": "disabled",
                            "status": {},
                            "message": "Disabled",
                        }
                    )
                    disabling_future.set_result(None)
                    window._check_pending_module_operations()
                    reenable_mock.assert_called_once_with("tutorial_resistance")

                    window._handle_module_state(
                        {
                            "module_id": "tutorial_resistance",
                            "enabled": True,
                            "state": "enabled",
                            "status": {},
                            "message": "Enabled",
                        }
                    )
                    reenable_future.set_result(None)
                    window._check_pending_module_operations()

                self.assertIn("tutorial_resistance", window.enabled_modules)
                self.assertAlmostEqual(
                    window.module_windows["tutorial_resistance"].settings()["delay_seconds"],
                    0.375,
                )
                self.assertAlmostEqual(
                    window.module_windows["tutorial_resistance"].settings()["base_resistance_ohm"],
                    220.0,
                )
                self.assertTrue(window.module_windows["tutorial_resistance"].has_unapplied_edits())
                apply_mock.assert_not_called()
            finally:
                window.enabled_modules.clear()
                window.close()

    def test_reopening_same_or_declined_seq_while_initializing_preserves_pending_settings(
        self,
    ) -> None:
        from labcontrol.measurement.settings import save_settings

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(ROOT / "modules", root / "modules")
            # Local persisted module settings have old 0.1 s delay.
            save_settings(
                root / "module_data" / "tutorial_resistance" / "settings.toml",
                {
                    "base_resistance_ohm": 100.0,
                    "channel_step_ohm": 10.0,
                    "delay_seconds": 0.1,
                    "noise_ohm": 0.001,
                    "over_range_ohm": 1e6,
                },
            )
            seq_a = root / "seq_a.seq"
            seq_a.write_text("T End Sequence\n", encoding="utf-8")
            seq_b = root / "seq_b.seq"
            seq_b.write_text("T End Sequence\n", encoding="utf-8")

            window = MainWindow(load_config(general))
            tut_version = window._module_descriptor("tutorial_resistance").version
            save_sequence_module_settings(
                seq_a,
                {
                    "tutorial_resistance": {
                        "base_resistance_ohm": 250.0,
                        "channel_step_ohm": 10.0,
                        "delay_seconds": 0.375,
                        "noise_ohm": 0.001,
                        "over_range_ohm": 1e6,
                    }
                },
                {"tutorial_resistance": tut_version},
            )
            save_sequence_module_settings(
                seq_b,
                {
                    "tutorial_resistance": {
                        "base_resistance_ohm": 500.0,
                        "channel_step_ohm": 10.0,
                        "delay_seconds": 0.9,
                        "noise_ohm": 0.001,
                        "over_range_ohm": 1e6,
                    }
                },
                {"tutorial_resistance": tut_version},
            )
            try:
                # 1) First open of seq_a -> confirms and starts enabling tutorial_resistance (still in flight).
                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(seq_a), "Sequence (*.seq)"),
                    ),
                    patch.object(
                        window,
                        "_confirm_sequence_module_sync",
                        return_value=True,
                    ),
                    patch.object(
                        window.runtime,
                        "apply_module_settings",
                    ) as apply_mock,
                    patch("labcontrol.ui.main_window.QMessageBox.warning") as warning_mock,
                ):
                    window._open_sequence()
                    self.assertIn("tutorial_resistance", window._pending_module_operations)
                    self.assertNotIn("tutorial_resistance", window.enabled_modules)

                    # 2) Immediately re-open seq_a BEFORE initialization finishes -> no popup, 0.375 preserved!
                    with patch.object(
                        window,
                        "_confirm_sequence_module_sync",
                        side_effect=AssertionError("Re-opening matching SEQ while initializing should not prompt"),
                    ):
                        window._open_sequence()

                    # 3) Open seq_b BEFORE initialization finishes and decline -> 0.375 STILL preserved!
                    with (
                        patch(
                            "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                            return_value=(str(seq_b), "Sequence (*.seq)"),
                        ),
                        patch.object(
                            window,
                            "_confirm_sequence_module_sync",
                            return_value=False,
                        ) as decline_b,
                    ):
                        window._open_sequence()
                        decline_b.assert_called_once()

                    # 4) Let real tutorial_resistance worker finish initializing -> must load 0.375, NOT 0.1!
                    deadline = time.monotonic() + 5.0
                    while "tutorial_resistance" not in window.enabled_modules and time.monotonic() < deadline:
                        QTest.qWait(20)
                        window._drain_runtime_messages()

                warning_mock.assert_not_called()

                self.assertIn("tutorial_resistance", window.enabled_modules)
                mod_win = window.module_windows["tutorial_resistance"]
                self.assertAlmostEqual(mod_win.settings()["delay_seconds"], 0.375)
                self.assertAlmostEqual(mod_win.settings()["base_resistance_ohm"], 250.0)
                self.assertTrue(mod_win.has_unapplied_edits())
                apply_mock.assert_not_called()
            finally:
                window.close()

    def test_reopening_same_seq_during_run_preserves_deferred_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            shutil.copytree(ROOT / "modules", root / "modules")
            seq_new = root / "next_run.seq"
            seq_new.write_text("T End Sequence\n", encoding="utf-8")
            seq_orig = root / "orig_run.seq"
            seq_orig.write_text("T End Sequence\n", encoding="utf-8")

            window = MainWindow(load_config(general))
            try:
                window._set_module_enabled("tutorial_resistance", True)
                deadline = time.monotonic() + 5.0
                while "tutorial_resistance" not in window.enabled_modules and time.monotonic() < deadline:
                    QTest.qWait(20)
                    window._drain_runtime_messages()
                mod_win = window.module_windows["tutorial_resistance"]
                tut_version = mod_win.descriptor.version
                orig_settings = mod_win.settings()
                orig_settings["delay_seconds"] = 0.1
                mod_win.load_settings(orig_settings, mark_unapplied=False)
                new_settings = deepcopy(orig_settings)
                new_settings["delay_seconds"] = 0.375
                save_sequence_module_settings(
                    seq_new,
                    {"tutorial_resistance": new_settings},
                    {"tutorial_resistance": tut_version},
                )
                save_sequence_module_settings(
                    seq_orig,
                    {"tutorial_resistance": orig_settings},
                    {"tutorial_resistance": tut_version},
                )

                window.current_run_state = RunState.RUNNING
                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(seq_new), "Sequence (*.seq)"),
                    ),
                    patch("labcontrol.ui.main_window.QMessageBox.warning") as warning_mock,
                ):
                    # 1) First open during active Run queues deferred 0.375.
                    window._open_sequence()
                    self.assertAlmostEqual(
                        window._deferred_sequence_module_settings["tutorial_resistance"]["delay_seconds"],
                        0.375,
                    )
                    self.assertAlmostEqual(mod_win.settings()["delay_seconds"], 0.1)

                    # 2) Re-opening the same SEQ during active Run must NOT clear deferred 0.375.
                    window._open_sequence()
                    self.assertAlmostEqual(
                        window._deferred_sequence_module_settings["tutorial_resistance"]["delay_seconds"],
                        0.375,
                    )

                warning_mock.assert_called_once()

                # 3) Once Run finishes, deferred 0.375 loads into the unlocked window as unapplied.
                window._handle_progress(RunProgress(state=RunState.COMPLETED, message="done"))
                self.assertAlmostEqual(mod_win.settings()["delay_seconds"], 0.375)
                self.assertTrue(mod_win.has_unapplied_edits())
                self.assertEqual(window._deferred_sequence_module_settings, {})

                # 4) Switching back to a SEQ matching the locked window cancels stale deferred settings.
                mod_win.load_settings(orig_settings, mark_unapplied=False)
                window.current_run_state = RunState.RUNNING
                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(seq_new), "Sequence (*.seq)"),
                    ),
                    patch("labcontrol.ui.main_window.QMessageBox.warning"),
                ):
                    window._open_sequence()
                    self.assertIn("tutorial_resistance", window._deferred_sequence_module_settings)
                with (
                    patch(
                        "labcontrol.ui.main_window.QFileDialog.getOpenFileName",
                        return_value=(str(seq_orig), "Sequence (*.seq)"),
                    ),
                    patch("labcontrol.ui.main_window.QMessageBox.warning") as orig_warn,
                ):
                    window._open_sequence()
                    self.assertNotIn("tutorial_resistance", window._deferred_sequence_module_settings)
                    orig_warn.assert_not_called()
                window._handle_progress(RunProgress(state=RunState.COMPLETED, message="done"))
                self.assertAlmostEqual(mod_win.settings()["delay_seconds"], 0.1)
                self.assertFalse(mod_win.has_unapplied_edits())
            finally:
                window.current_run_state = RunState.IDLE
                window._deferred_sequence_module_settings.clear()
                window.close()

    def test_save_sequence_writes_its_module_settings_companion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            general = write_simulated_configuration(root)
            sequence = root / "saved.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )
            window = MainWindow(
                load_config(
                    general
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
            general = write_simulated_configuration(root)
            shutil.copytree(
                ROOT / "modules",
                root / "modules",
            )
            sequence = root / "enabled.seq"
            sequence.write_text(
                "T End Sequence\n",
                encoding="utf-8",
            )
            window = MainWindow(
                load_config(
                    general
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
                    "2.0.1",
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
                        {module_id: "1.1.0"},
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
                "T Measure instruments=transport\nT End Sequence\n", "legacy.seq"
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

    def test_close_while_running_finishes_immediately_if_sequence_completes_during_prompt(self) -> None:
        window = MainWindow(self.config)
        try:
            window.current_run_state = RunState.RUNNING

            def complete_then_accept(*_args, **_kwargs):
                window._handle_progress(RunProgress(state=RunState.COMPLETED, message="completed"))
                return QMessageBox.StandardButton.Yes

            with (
                patch("labcontrol.ui.main_window.QMessageBox.question", side_effect=complete_then_accept),
                patch.object(window.runtime, "stop_sequence") as stop_sequence,
                patch.object(window.runtime, "shutdown") as shutdown,
            ):
                self.assertTrue(window.close())

            stop_sequence.assert_not_called()
            shutdown.assert_called_once()
            self.assertFalse(window._close_after_stop)
        finally:
            window.current_run_state = RunState.IDLE
            window.close()

    def test_close_while_running_stays_open_without_leaking_flag_if_faulted_during_prompt(self) -> None:
        window = MainWindow(self.config)
        try:
            window.current_run_state = RunState.RUNNING

            def fault_then_accept(*_args, **_kwargs):
                window._handle_progress(RunProgress(state=RunState.FAULTED, message="cleanup failed"))
                return QMessageBox.StandardButton.Yes

            with (
                patch("labcontrol.ui.main_window.QMessageBox.question", side_effect=fault_then_accept),
                patch.object(window.runtime, "stop_sequence") as stop_sequence,
                patch.object(window.runtime, "shutdown") as shutdown,
            ):
                self.assertFalse(window.close())

            stop_sequence.assert_not_called()
            shutdown.assert_not_called()
            self.assertFalse(window._close_after_stop)
            self.assertEqual(window.run_detail_label.text(), "cleanup failed")
        finally:
            window.current_run_state = RunState.IDLE
            window.close()

    def test_loading_nested_set_datafile_selects_path_for_run(self) -> None:
        window = MainWindow(self.config)
        try:
            nested_datafile = Command(
                CommandType.SET_DATAFILE,
                {
                    "mode": "open|create",
                    "path_scope": "Run folder",
                    "path": "nested.dat",
                },
            )
            scan = Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 0.0, "steps": 1},
                [nested_datafile],
            )
            window._set_document(SequenceDocument([scan], "nested-dat.seq"))
            self.assertEqual(window.data_file_label.fullText(), "nested.dat")
            self.assertTrue(window._datafile_selected)

            window._remember_datafile_directory(nested_datafile)
            window._mark_dirty()

            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), "nested.dat")
        finally:
            window.close()

    def test_set_datafile_after_measure_blocks_run_and_change_inserts_before_measure(self) -> None:
        window = MainWindow(self.config)
        try:
            nested_datafile = Command(
                CommandType.SET_DATAFILE,
                {
                    "mode": "open|create",
                    "path_scope": "Run folder",
                    "path": "chosen.dat",
                },
            )
            scan = Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 0.0, "steps": 1},
                [Command(CommandType.MEASURE), nested_datafile],
            )
            window._set_document(SequenceDocument([scan], "measure-first.seq"))
            window._remember_datafile_directory(nested_datafile)
            window._mark_dirty()

            self.assertFalse(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), "")
            with (
                patch.object(window.runtime, "run_sequence") as run_sequence,
                patch("labcontrol.ui.main_window.QMessageBox.warning") as warning,
            ):
                window._run_sequence()
            run_sequence.assert_not_called()
            warning.assert_called_once()
            self.assertIn("before the first Measure", warning.call_args.args[2])

            # Top-level Measure followed by Set Datafile: Change must insert a new
            # Set Datafile at the start instead of updating the later one in place.
            late_datafile = Command(
                CommandType.SET_DATAFILE,
                {"mode": "open|create", "path_scope": "Run folder", "path": "late.dat"},
            )
            window._set_document(
                SequenceDocument([Command(CommandType.MEASURE), late_datafile], "late-dat.seq")
            )
            chosen_path = ROOT / "chosen_first.dat"
            with patch(
                "labcontrol.ui.main_window.QFileDialog.getSaveFileName",
                return_value=(str(chosen_path), "Data (*.dat)"),
            ):
                window._change_datafile()
            self.assertEqual(window.document.commands[0].type, CommandType.SET_DATAFILE)
            self.assertEqual(window.document.commands[0].params["path"], str(chosen_path.resolve()))
            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), str(chosen_path.resolve()))
        finally:
            window.close()

    def test_command_dock_defaults_hidden_and_follows_sequence_selection(self) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1380, 820)
            window.show()
            self.application.processEvents()

            # Collapsed by default on startup so workspace width stays maximized.
            self.assertTrue(window.command_dock.isHidden())
            self.assertFalse(window.commands_toolbar_action.isChecked())

            # Clicking inside the SEQ list auto-expands the command bar.
            QTest.mousePress(
                window.editor.list.viewport(),
                Qt.MouseButton.LeftButton,
                pos=window.editor.list.viewport().rect().center(),
            )
            self.application.processEvents()
            self.assertFalse(window.command_dock.isHidden())
            self.assertTrue(window.commands_toolbar_action.isChecked())

            # Opening/activating a Data Browser auto-collapses the command bar.
            browser_window = window._show_data_browser()
            self.application.processEvents()
            self.assertIs(window.mdi.activeSubWindow(), browser_window)
            self.assertTrue(window.command_dock.isHidden())
            self.assertFalse(window.commands_toolbar_action.isChecked())

            # Switching back to SEQ auto-expands the command bar again.
            window.mdi.setActiveSubWindow(window.sequence_window)
            self.application.processEvents()
            self.assertFalse(window.command_dock.isHidden())

            # Manually collapsing while editing SEQ keeps it hidden on clicks inside SEQ.
            window.commands_toolbar_action.trigger()
            self.application.processEvents()
            self.assertTrue(window.command_dock.isHidden())
            QTest.mousePress(
                window.editor.list.viewport(),
                Qt.MouseButton.LeftButton,
                pos=window.editor.list.viewport().rect().center(),
            )
            self.application.processEvents()
            self.assertTrue(window.command_dock.isHidden())

            # Switching to Data Browser and back to SEQ re-enables auto-expand.
            window.mdi.setActiveSubWindow(browser_window)
            self.application.processEvents()
            window.mdi.setActiveSubWindow(window.sequence_window)
            self.application.processEvents()
            self.assertFalse(window.command_dock.isHidden())
        finally:
            window.close()

    def test_status_bar_run_log_toggle_tracks_unread_severity_and_clears_on_open(self) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1380, 820)
            window.show()
            self.application.processEvents()

            self.assertTrue(window.log_dock.isHidden())
            self.assertFalse(window.log_toggle_button.isChecked())
            self.assertEqual(window.log_toggle_button.text(), "Run Log")

            window._append_log("INFO", "sequence", "STEP", "Started step 1")
            self.assertEqual(window.log_toggle_button.text(), "Run Log (1)")
            self.assertEqual(window.log_toggle_button.property("logSeverity"), "")

            window._append_log("WARNING", "instrument", "WARN", "High heater output")
            self.assertIn("warn", window.log_toggle_button.text())
            self.assertEqual(window.log_toggle_button.property("logSeverity"), "warning")

            window._append_log("ERROR", "instrument", "ERR", "Timeout")
            self.assertIn("err", window.log_toggle_button.text())
            self.assertEqual(window.log_toggle_button.property("logSeverity"), "error")

            window.log_toggle_button.click()
            self.application.processEvents()
            self.assertFalse(window.log_dock.isHidden())
            self.assertTrue(window.log_toggle_button.isChecked())
            self.assertEqual(window.log_toggle_button.text(), "Run Log")
            self.assertEqual(window.log_toggle_button.property("logSeverity"), "")

            window.log_toggle_button.click()
            self.application.processEvents()
            self.assertTrue(window.log_dock.isHidden())
            self.assertFalse(window.log_toggle_button.isChecked())
        finally:
            window.close()

    def test_toolbar_high_field_indicator_tracks_field_and_opens_manual_control(
        self,
    ) -> None:
        window = MainWindow(self.config)
        try:
            window.resize(1380, 820)
            window.show()
            self.application.processEvents()

            self.assertFalse(window.high_field_action.isVisible())

            now = time.monotonic()
            # At 100 Oe (not strictly above 100), pill stays hidden.
            window._handle_snapshots(
                {
                    "field": InstrumentSnapshot(
                        "field",
                        "Simulated Magnetic Field",
                        InstrumentKind.FIELD,
                        now,
                        "Oe",
                        100.0,
                        100.0,
                        controls={
                            "main": InstrumentControlState(
                                current=100.0,
                                target=100.0,
                                stability=StabilityState.STABLE,
                            )
                        },
                    )
                }
            )
            self.assertFalse(window.high_field_action.isVisible())

            # Negative high field activates toolbar pill and card warning.
            window._handle_snapshots(
                {
                    "field": InstrumentSnapshot(
                        "field",
                        "Simulated Magnetic Field",
                        InstrumentKind.FIELD,
                        now,
                        "Oe",
                        -12500.0,
                        -12500.0,
                        controls={
                            "main": InstrumentControlState(
                                current=-12500.0,
                                target=-12500.0,
                                stability=StabilityState.STABLE,
                            )
                        },
                    )
                }
            )
            self.assertTrue(window.high_field_action.isVisible())
            self.assertIn("-12500.000 Oe", window.high_field_action.text())
            self.assertTrue(window.status_panel.panels["field.main"].high_field_active)

            # Clicking the pill opens manual control for the active field panel.
            with patch.object(window, "_open_manual_control") as open_manual:
                window.high_field_action.trigger()
            open_manual.assert_called_once_with("field", "main")

            # Returning below 100 Oe hides the pill again.
            window._handle_snapshots(
                {
                    "field": InstrumentSnapshot(
                        "field",
                        "Simulated Magnetic Field",
                        InstrumentKind.FIELD,
                        now,
                        "Oe",
                        25.0,
                        0.0,
                        controls={
                            "main": InstrumentControlState(
                                current=25.0,
                                target=0.0,
                                stability=StabilityState.MOVING,
                            )
                        },
                    )
                }
            )
            self.assertFalse(window.high_field_action.isVisible())
            self.assertFalse(window.status_panel.panels["field.main"].high_field_active)
        finally:
            window.close()

    def test_main_window_sequence_cut_undo_shortcuts_and_focus_isolation(
        self,
    ) -> None:
        from PySide6.QtCore import QItemSelectionModel
        from PySide6.QtGui import QKeySequence

        window = MainWindow(self.config)
        try:
            window.show()
            window.activateWindow()
            first = Command(CommandType.WAIT, {"seconds": 1.0})
            second = Command(CommandType.REMARK, {"text": "second"})
            window._set_document(SequenceDocument([first, second], "shortcuts.seq"))
            window._focus_sequence()
            self.application.processEvents()

            # File shortcuts are configured cleanly.
            self.assertIn(QKeySequence("Ctrl+N"), window.new_action.shortcuts())
            self.assertIn(QKeySequence("Ctrl+O"), window.open_action.shortcuts())
            self.assertIn(QKeySequence("Ctrl+S"), window.save_action.shortcuts())
            self.assertIn(QKeySequence("Ctrl+Shift+S"), window.save_as_action.shortcuts())

            # Edit menu exposes Undo/Redo, Cut/Copy/Paste/Delete, and Move Up/Down.
            edit_labels = [
                action.text()
                for action in window.edit_menu.actions()
                if not action.isSeparator()
            ]
            self.assertEqual(
                edit_labels,
                [
                    "Undo",
                    "Redo",
                    "Disable",
                    "Enable",
                    "Cut",
                    "Copy",
                    "Paste",
                    "Delete",
                    "Move Up",
                    "Move Down",
                ],
            )

            # Dialog edit via _edit_command is undoable with Ctrl+Z and redoable with Ctrl+Y.
            with patch("labcontrol.ui.main_window.CommandDialog") as dialog_cls:
                dialog = dialog_cls.return_value
                dialog.exec.return_value = dialog_cls.DialogCode.Accepted
                dialog.values.return_value = {"seconds": 5.0}
                window._edit_command(first)
            self.application.processEvents()
            self.assertEqual(window.document.commands[0].params["seconds"], 5.0)

            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual(window.document.commands[0].params["seconds"], 1.0)

            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Y,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual(window.document.commands[0].params["seconds"], 5.0)

            # Cut row 0 with Ctrl+X and undo with Ctrl+Z inside MainWindow.
            window.editor.list.clearSelection()
            window.editor.list.item(0).setSelected(True)
            window.editor.list.setCurrentItem(
                window.editor.list.item(0),
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_X,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual(len(window.document.commands), 1)
            self.assertEqual(window.document.commands[0].type, CommandType.REMARK)

            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual(len(window.document.commands), 2)
            self.assertEqual(window.document.commands[0].type, CommandType.WAIT)

            # Ctrl+S triggers save directly from the focused SEQ list.
            with patch.object(window, "_save_sequence", return_value=True) as save_mock:
                window.save_action.triggered.disconnect()
                window.save_action.triggered.connect(save_mock)
                QTest.keyClick(
                    window.editor.list,
                    Qt.Key.Key_S,
                    Qt.KeyboardModifier.ControlModifier,
                )
                self.application.processEvents()
                save_mock.assert_called_once()

            # Change Data File is also undoable via Ctrl+Z and syncs the data file label.
            chosen_path = ROOT / "undo_datafile.dat"
            with patch(
                "labcontrol.ui.main_window.QFileDialog.getSaveFileName",
                return_value=(str(chosen_path), "Data (*.dat)"),
            ):
                window._change_datafile()
            self.assertEqual(window.document.commands[0].type, CommandType.SET_DATAFILE)
            self.assertEqual(window.data_file_label.fullText(), str(chosen_path.resolve()))
            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual(window.document.commands[0].type, CommandType.WAIT)
            self.assertEqual(window.data_file_label.fullText(), "")

            # Focusing docked Data Browser must NOT leak Ctrl+X / Delete / Ctrl+Z into SEQ.
            docked_browser_win = window._show_data_browser()
            docked_browser = window.data_browser_windows[docked_browser_win]
            docked_browser.setFocus()
            self.application.processEvents()
            QTest.keyClick(
                docked_browser,
                Qt.Key.Key_X,
                Qt.KeyboardModifier.ControlModifier,
            )
            QTest.keyClick(docked_browser, Qt.Key.Key_Delete)
            QTest.keyClick(
                docked_browser,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual(len(window.document.commands), 2)
            self.assertEqual(window.document.commands[0].params["seconds"], 5.0)
        finally:
            window.close()

    def test_datafile_selection_syncs_across_undo_redo_cut_disable_and_move(
        self,
    ) -> None:
        from PySide6.QtCore import QItemSelectionModel

        window = MainWindow(self.config)
        try:
            window.resize(1280, 760)
            window.show()
            window._set_document(
                SequenceDocument(
                    [Command(CommandType.MEASURE)],
                    "datafile-redo.seq",
                )
            )
            window.activateWindow()
            window.editor.list.setFocus()
            self.application.processEvents()

            self.assertFalse(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), "")

            # 1) Select DAT -> Undo -> Redo must restore sidebar label, flag, and Run readiness.
            chosen_path = ROOT / "redo_datafile.dat"
            expected_path = str(chosen_path.resolve())
            with patch(
                "labcontrol.ui.main_window.QFileDialog.getSaveFileName",
                return_value=(str(chosen_path), "Data (*.dat)"),
            ):
                window._change_datafile()
            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), expected_path)

            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertFalse(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), "")
            with (
                patch.object(window.runtime, "run_sequence") as run_seq,
                patch("labcontrol.ui.main_window.QMessageBox.warning") as warn,
            ):
                window._run_sequence()
            run_seq.assert_not_called()
            warn.assert_called_once()

            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Y,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertEqual(window.document.commands[0].type, CommandType.SET_DATAFILE)
            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), expected_path)
            with (
                patch.object(window, "_start_sequence") as start_seq,
                patch("labcontrol.ui.main_window.QMessageBox.warning") as warn,
            ):
                window._run_sequence()
            warn.assert_not_called()
            start_seq.assert_called_once()

            # 2) Cut Set Datafile -> Undo restores selection state.
            window.editor.list.clearSelection()
            window.editor.list.item(0).setSelected(True)
            window.editor.list.setCurrentItem(
                window.editor.list.item(0),
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_X,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertFalse(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), "")

            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), expected_path)

            # 3) Disable -> Enable restores selection state.
            window.editor.list.clearSelection()
            window.editor.list.item(0).setSelected(True)
            window.editor.list.setCurrentItem(
                window.editor.list.item(0),
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_D,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertFalse(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), "")

            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_E,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.application.processEvents()
            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), expected_path)

            # 4) Move Down below Measure clears selection; Move Up above Measure restores it.
            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Down,
                Qt.KeyboardModifier.AltModifier,
            )
            self.application.processEvents()
            self.assertFalse(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), "")

            QTest.keyClick(
                window.editor.list,
                Qt.Key.Key_Up,
                Qt.KeyboardModifier.AltModifier,
            )
            self.application.processEvents()
            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), expected_path)
        finally:
            window.close()

    def test_seq_drag_drop_syncs_datafile_and_accepts_command_palette_drops(
        self,
    ) -> None:
        import json

        from PySide6.QtCore import QByteArray, QMimeData, QPointF
        from PySide6.QtGui import QDropEvent

        from labcontrol.ui.dialogs import CommandDialog
        from labcontrol.ui.sequence_editor import PALETTE_COMMAND_MIME, SEQUENCE_COMMAND_MIME

        window = MainWindow(self.config)
        try:
            window.resize(1380, 820)
            window.show()
            chosen_path = ROOT / "drag_datafile.dat"
            expected_path = str(chosen_path.resolve())
            datafile_cmd = Command(
                CommandType.SET_DATAFILE,
                {
                    "mode": "open|create",
                    "path_scope": "Custom folder",
                    "path": expected_path,
                },
            )
            scan_cmd = Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 10.0, "steps": 2},
                [Command(CommandType.MEASURE)],
            )
            window._set_document(
                SequenceDocument([datafile_cmd, scan_cmd], "drag-main.seq")
            )
            self.application.processEvents()
            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), expected_path)

            # 1) Drag Set Datafile below Scan's End Scan -> after Measure, so datafile selection clears.
            end_scan_rect = window.editor.list.visualItemRect(window.editor.list.item(3))
            below_end_scan = QPointF(end_scan_rect.center().x(), end_scan_rect.bottom() - 2)
            mime = QMimeData()
            mime.setData(
                SEQUENCE_COMMAND_MIME,
                QByteArray(json.dumps([datafile_cmd.id]).encode("utf-8")),
            )
            window.editor.list.dropEvent(
                QDropEvent(
                    below_end_scan,
                    Qt.DropAction.MoveAction,
                    mime,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            self.application.processEvents()
            self.assertEqual(
                [c.type for c in window.document.commands],
                [CommandType.SCAN_TIME, CommandType.SET_DATAFILE],
            )
            self.assertFalse(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), "")

            # Undo restores Set Datafile before Scan and restores sidebar state.
            window.editor.undo()
            self.application.processEvents()
            self.assertTrue(window._datafile_selected)
            self.assertEqual(window.data_file_label.fullText(), expected_path)

            # Palette tree allows dragging leaf commands while blocking category headers.
            first_group = window.command_tree.topLevelItem(0)
            self.assertIsNotNone(first_group)
            self.assertFalse(bool(first_group.flags() & Qt.ItemFlag.ItemIsDragEnabled))
            self.assertTrue(bool(first_group.child(0).flags() & Qt.ItemFlag.ItemIsDragEnabled))

            # 2) Drop a palette command ("core", "wait") directly inside Scan (below Scan header).
            scan_rect = window.editor.list.visualItemRect(window.editor.list.item(1))
            below_scan = QPointF(scan_rect.center().x(), scan_rect.bottom() - 2)
            palette_mime = QMimeData()
            palette_mime.setData(
                PALETTE_COMMAND_MIME,
                QByteArray(json.dumps(["core", CommandType.WAIT.value]).encode("utf-8")),
            )
            with patch.object(
                CommandDialog,
                "exec",
                return_value=CommandDialog.DialogCode.Accepted,
            ):
                window.editor.list.dropEvent(
                    QDropEvent(
                        below_scan,
                        Qt.DropAction.CopyAction,
                        palette_mime,
                        Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier,
                    )
                )
                self.application.processEvents()
            live_scan = window.document.commands[1]
            self.assertEqual(
                [c.type for c in live_scan.children],
                [CommandType.WAIT, CommandType.MEASURE],
            )
        finally:
            window.close()

    def test_measure_inserts_and_drops_directly_without_dialog(self) -> None:
        import json

        from PySide6.QtCore import QByteArray, QMimeData, QPointF
        from PySide6.QtGui import QDropEvent

        from labcontrol.ui.sequence_editor import PALETTE_COMMAND_MIME

        window = MainWindow(self.config)
        try:
            window.resize(1380, 820)
            window.show()
            scan_cmd = Command(
                CommandType.SCAN_TIME,
                {"duration_seconds": 10.0, "steps": 2},
                [],
            )
            window._set_document(SequenceDocument([scan_cmd], "measure-direct.seq"))
            self.application.processEvents()

            measurement_group = window.command_tree.findItems(
                "Measurement Commands",
                Qt.MatchFlag.MatchExactly,
            )[0]
            measure_item = next(
                measurement_group.child(index)
                for index in range(measurement_group.childCount())
                if measurement_group.child(index).text(0) == "Measure"
            )

            with patch(
                "labcontrol.ui.main_window.CommandDialog",
                side_effect=AssertionError("Measure should not open CommandDialog"),
            ):
                # 1) Double-clicking Measure in the command palette inserts directly.
                window.editor.list.setCurrentRow(window.editor.list.count() - 1)
                window._insert_palette_command(measure_item, 0)
                self.assertEqual(
                    [c.type for c in window.document.commands],
                    [CommandType.SCAN_TIME, CommandType.MEASURE],
                )

                # 2) Drag-dropping Measure below Scan header inserts inside Scan without dialog.
                scan_rect = window.editor.list.visualItemRect(window.editor.list.item(0))
                below_scan = QPointF(scan_rect.center().x(), scan_rect.bottom() - 2)
                palette_mime = QMimeData()
                palette_mime.setData(
                    PALETTE_COMMAND_MIME,
                    QByteArray(json.dumps(["core", CommandType.MEASURE.value]).encode("utf-8")),
                )
                window.editor.list.dropEvent(
                    QDropEvent(
                        below_scan,
                        Qt.DropAction.CopyAction,
                        palette_mime,
                        Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier,
                    )
                )
                self.application.processEvents()
                self.assertEqual(
                    [c.type for c in scan_cmd.children],
                    [CommandType.MEASURE],
                )

                # 3) Double-clicking an existing Measure row does not open an empty dialog.
                window._edit_command(scan_cmd.children[0])
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
