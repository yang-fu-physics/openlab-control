from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QMimeData, QPoint, QPointF, QThreadPool, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QDropEvent, QImage, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPlainTextEdit  # noqa: E402

from labcontrol.ui.data_browser import (  # noqa: E402
    DatBrowserWidget,
    PointDetailsDialog,
)
from labcontrol.plot_format import LOG_SCALE, load_plot_format  # noqa: E402
from labcontrol.data_filter import DataFilter, DataFilterRow  # noqa: E402
from labcontrol.dat_reader import read_dat  # noqa: E402
from labcontrol.time_filter import format_time_bound, parse_time_bound, time_filter_context  # noqa: E402
from labcontrol.ui.dat_plot import (  # noqa: E402
    PlotHit,
    YSeriesSelectionDialog,
)


class DataBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def _finish_reads(self) -> None:
        self.assertTrue(QThreadPool.globalInstance().waitForDone(5000))
        self.application.processEvents()

    def test_background_refresh_keeps_complete_snapshot_and_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "live.dat"
            complete = "[Data]\nTime(s),Value\n0,1\n"
            path.write_text(complete, encoding="utf-8")
            browser = DatBrowserWidget(Path(temp))
            browser.monitor_timer.stop()
            browser.queue_load(path)
            self._finish_reads()
            prior = browser.document
            for partial in ("1,", '1,"2\n'):
                path.write_text(complete + partial, encoding="utf-8")
                browser._check_for_updates()
                self._finish_reads()
                self.assertIs(browser.document, prior)
            path.write_text(complete + "1,2\n", encoding="utf-8")
            browser._check_for_updates()
            self._finish_reads()
            self.assertEqual(len(browser.document.rows), 2)
            menu = browser.canvas.build_context_menu()
            next(action for action in menu.actions() if action.text() == "Browse Raw File").trigger()
            self._finish_reads()
            raw = browser.findChild(QPlainTextEdit)
            self.assertTrue(raw.isReadOnly())
            self.assertEqual(raw.toPlainText(), complete + "1,2\n")
            browser.close()

    def test_new_file_request_wins_over_slow_previous_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slow, quick = root / "slow.dat", root / "quick.dat"
            for path in (slow, quick):
                path.write_text("[Data]\nTime(s),Value\n0,1\n", encoding="utf-8")
            browser = DatBrowserWidget(root)
            browser.monitor_timer.stop()
            entered, release = Event(), Event()

            def delayed(path, **kwargs):
                if path == slow.resolve():
                    entered.set()
                    release.wait(5)
                return read_dat(path, **kwargs)

            with patch("labcontrol.ui.data_browser.read_dat", side_effect=delayed):
                try:
                    browser.queue_load(slow, show_errors=False)
                    self.assertTrue(entered.wait(2))
                    browser.queue_load(quick, show_errors=False)
                    for _ in range(200):
                        if browser.current_path == quick.resolve():
                            break
                        QTest.qWait(10)
                    self.assertEqual(browser.current_path, quick.resolve())
                finally:
                    release.set()
                    self._finish_reads()
            self.assertEqual(browser.current_path, quick.resolve())
            self.assertFalse(slow.with_suffix(".plt").exists())
            browser.close()

    def test_filter_keeps_source_row_identity_navigation_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "filter.dat"
            source = "[Data]\nTime(s),R1,R2,Gate\n0,0,10,2\n1,1,11,2\n2,2,12,text\n3,3,13,2\n"
            path.write_text(source, encoding="utf-8")
            browser = DatBrowserWidget(path.parent)
            browser.monitor_timer.stop()
            self.assertTrue(browser.load_path(path))
            browser.canvas.set_axes("Time(s)", ("R1", "R2"))
            filters = DataFilter((
                DataFilterRow(column="Time(s)", minimum=1, maximum=3),
                DataFilterRow(column="Gate", minimum=2, maximum=2),
                DataFilterRow(), DataFilterRow(), DataFilterRow(),
            ))
            browser._apply_data_filter(filters)
            for points in browser.canvas.points_by_series.values():
                self.assertEqual([point.row_index for point in points], [1, 3])
            self.assertIn("Filtered: 2/4", browser.status_label.text())
            self.assertEqual(load_plot_format(path.with_suffix(".plt")).filters, filters)
            points = browser.canvas.points_by_series["R1"]
            dialog = PointDetailsDialog(
                browser.document, PlotHit("R1", points[0]), "Time(s)",
                points=points, select=browser.canvas._select_point,
            )
            dialog.next_button.click()
            self.assertIn("Data row: 4", dialog.summary_label.text())
            self.assertEqual(browser.canvas.selected_hit.row_index, 3)
            dialog.close()
            path.write_text("[Data]\nTime(s),R1,R2\n0,0,10\n1,1,11\n", encoding="utf-8")
            browser._check_for_updates()
            self._finish_reads()
            self.assertIn("Filters cleared for unavailable columns: Gate", browser.status_label.text())
            self.assertIsNone(browser.canvas.selected_hit)
            browser.close()

    def test_large_nonmonotonic_path_is_complete_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large.dat"
            path.write_text(
                "[Data]\nTime(s),Value\n" + "".join(
                    f"{index % 101},{100000 if index == 12503 else index + 1}\n"
                    for index in range(25_000)
                ),
                encoding="utf-8",
            )
            browser = DatBrowserWidget(path.parent)
            browser.monitor_timer.stop()
            self.assertTrue(browser.load_path(path))
            canvas = browser.canvas
            plot, ranges = canvas._plot_rect(), canvas._ranges()
            image = QImage(900, 700, QImage.Format.Format_ARGB32)
            painter = QPainter(image)
            try:
                canvas._draw_series(painter, canvas.points, QColor("black"), plot, ranges)
                cached = canvas._path_cache[id(canvas.points)][1]
                self.assertEqual(cached.elementCount(), 25_000)
                canvas._draw_series(painter, canvas.points, QColor("black"), plot, ranges)
                self.assertIs(canvas._path_cache[id(canvas.points)][1], cached)
                canvas.set_y_scale(LOG_SCALE)
                canvas._draw_series(painter, canvas.points, QColor("black"), plot, canvas._ranges())
                self.assertEqual(canvas._path_cache[id(canvas.points)][1].elementCount(), 25_000)
                self.assertIsNot(canvas._path_cache[id(canvas.points)][1], cached)
            finally:
                painter.end()
            browser.close()

    def test_time_bounds_use_first_record_and_preserve_wall_time_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "times.dat"
            path.write_text("[Data]\nTime(s),Y\n90061,1\n1,2\n", encoding="utf-8")
            context = time_filter_context(read_dat(path))
            self.assertEqual(context.initial_minimum, 90061)
            self.assertEqual(parse_time_bound("25:01:01", context), 90061)
            self.assertIsNone(parse_time_bound("", context))
            path.write_text(
                "[Header]\nFILEOPENTIME,3589995867,04/26/2025,11:59 pm\n"
                "[Data]\nTimestamp(s),Y\n3589995867,1\n3589995987,2\n",
                encoding="utf-8",
            )
            context = time_filter_context(read_dat(path))
            self.assertTrue(context.is_wall_time)
            text = format_time_bound(3589995987, context)
            self.assertTrue(text.startswith("2025-04-27 00:01"))
            self.assertEqual(parse_time_bound(text, context), 3589995987)

    def test_load_axes_nearest_point_and_live_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "live.dat"
            path.write_text(
                "[Header]\nINFO, live test\n[Data]\nTime(s),Value,Note\n0,1,first\n1,2,second\n",
                encoding="utf-8",
            )
            browser = DatBrowserWidget(Path(temp))
            browser.resize(900, 600)
            browser.canvas.resize(850, 500)
            browser.show()
            self.application.processEvents()
            self.assertTrue(browser.load_path(path))
            self.assertEqual(browser.canvas.x_column, "Time(s)")
            self.assertEqual(browser.canvas.y_column, "Value")
            self.assertEqual(len(browser.canvas.points), 2)

            ranges = browser.canvas._ranges()
            self.assertIsNotNone(ranges)
            point = browser.canvas.points[1]
            screen = browser.canvas._screen_point(
                point.x,
                point.y,
                browser.canvas._plot_rect(),
                ranges,
            )
            selected = browser.canvas._nearest_point(screen)
            self.assertIsNotNone(selected)
            self.assertEqual(selected.row[2], "second")

            activated = []
            browser.canvas.pointActivated.disconnect(browser._show_point_details)
            browser.canvas.pointActivated.connect(activated.append)
            QTest.mouseDClick(
                browser.canvas,
                Qt.MouseButton.LeftButton,
                pos=screen.toPoint(),
            )
            self.application.processEvents()
            self.assertEqual(activated[0].row[2], "second")
            self.assertEqual(browser.canvas.selected_hit.row[2], "second")

            browser.canvas.set_axes(None, "Value")
            self.assertIsNone(browser.canvas.x_column)
            plot = browser.canvas._plot_rect()
            start = QPoint(int(plot.left() + plot.width() * 0.2), int(plot.top() + plot.height() * 0.2))
            stop = QPoint(int(plot.left() + plot.width() * 0.8), int(plot.top() + plot.height() * 0.8))
            QTest.mousePress(browser.canvas, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(browser.canvas, stop)
            QTest.mouseRelease(browser.canvas, Qt.MouseButton.LeftButton, pos=stop)
            self.assertTrue(browser.canvas._manual_view)
            zoomed_range = browser.canvas._view_range
            path.write_text(
                "[Header]\nINFO, live test\n[Data]\nTime(s),Value,Note\n"
                "0,1,first\n1,2,second\n2,3,third\n",
                encoding="utf-8",
            )
            browser.monitor_timer.timeout.emit()
            self._finish_reads()
            self.assertEqual(len(browser.document.rows), 3)
            self.assertEqual(len(browser.canvas.points), 3)
            self.assertIsNone(browser.canvas.x_column)
            self.assertEqual(browser.canvas._view_range, zoomed_range)

            dropped_path = Path(temp) / "dropped.dat"
            dropped_path.write_text(
                "[Data]\nX,Y\n10,20\n",
                encoding="utf-8",
            )
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(dropped_path))])
            drop = QDropEvent(
                QPointF(20, 20),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            browser.dropEvent(drop)
            self.assertTrue(drop.isAccepted())
            self._finish_reads()
            self.assertEqual(browser.current_path, dropped_path.resolve())
            browser.close()

    def test_quantum_design_timestamp_displays_as_instrument_time(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "quantum.dat"
            path.write_text(
                "[Header]\n"
                "FILEOPENTIME,3589995867.25658,"
                "04/26/2025,6:56 pm\n"
                "[Data]\n"
                "Time Stamp (sec),Temperature (K),"
                "Magnetic Field (Oe)\n"
                "3589998029.84072,1.8007,49.52\n"
                "3590001629.84072,10.0,20000\n"
                "3590005229.84072,20.0,40000\n",
                encoding="utf-8",
            )
            browser = DatBrowserWidget(Path(temp))
            browser.resize(980, 620)
            browser.show()
            self.application.processEvents()
            self.assertTrue(browser.load_path(path))
            self.assertEqual(
                browser.canvas.x_column,
                "Time Stamp (sec)",
            )
            self.assertIsNotNone(
                browser.canvas._timestamp_reference
            )
            point = browser.canvas.points[0]
            formatted = browser.canvas.format_x_value(
                point.x,
                full=True,
            )
            self.assertEqual(
                formatted,
                (
                    "2025-04-26 19:32:02.584 "
                    "(instrument time)"
                ),
            )

            details = PointDetailsDialog(
                browser.document,
                PlotHit("Temperature (K)", point),
                browser.canvas.x_label,
                formatted,
            )
            self.assertIn(
                "2025-04-26 19:32:02.584",
                details.summary_label.text(),
            )
            details.close()
            browser.canvas.repaint()
            self.application.processEvents()
            browser.close()

    def test_y_series_batch_selection_and_logarithmic_axes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "log.dat"
            path.write_text(
                "[Data]\nX,Y,Aux\n"
                "-1,-1,1\n0,0,10\n1,1,100\n10,10,1000\n100,100,10000\n",
                encoding="utf-8",
            )
            browser = DatBrowserWidget(Path(temp))
            browser.resize(900, 620)
            browser.canvas.resize(850, 520)
            browser.show()
            self.application.processEvents()
            self.assertTrue(browser.load_path(path))
            self.assertTrue(browser.canvas.set_axes("X", ("Y",)))

            selector = YSeriesSelectionDialog(
                browser.document.numeric_columns(),
                browser.canvas.y_columns,
            )
            self.assertTrue(
                selector.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            selector.series_list.item(2).setCheckState(Qt.CheckState.Checked)
            self.assertEqual(selector.selected_columns(), ("Y", "Aux"))
            self.assertTrue(selector.isVisible() is False)
            selector.series_list.item(1).setCheckState(Qt.CheckState.Unchecked)
            selector.series_list.item(2).setCheckState(Qt.CheckState.Unchecked)
            self.assertFalse(
                selector.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )

            menu = browser.canvas.build_context_menu()
            labels = [action.text() for action in menu.actions()]
            self.assertIn("Select Y Series...", labels)
            self.assertIn("X Scale", labels)
            self.assertIn("Y Scale", labels)

            self.assertTrue(browser.canvas.set_x_scale(LOG_SCALE))
            self.assertTrue(browser.canvas.set_y_scale(LOG_SCALE))
            ranges = browser.canvas._ranges("Y")
            self.assertIsNotNone(ranges)
            self.assertGreater(ranges[0], 0)
            self.assertGreater(ranges[2], 0)
            plot = browser.canvas._plot_rect()
            zero_screen = browser.canvas._screen_point(
                browser.canvas.points[1].x,
                browser.canvas.points[1].y,
                plot,
                ranges,
            )
            self.assertIsNone(zero_screen)
            screens = [
                browser.canvas._screen_point(point.x, point.y, plot, ranges)
                for point in browser.canvas.points[2:]
            ]
            self.assertTrue(all(screen is not None for screen in screens))
            self.assertAlmostEqual(
                screens[1].x() - screens[0].x(),
                screens[2].x() - screens[1].x(),
                places=6,
            )
            self.assertAlmostEqual(
                screens[0].y() - screens[1].y(),
                screens[1].y() - screens[2].y(),
                places=6,
            )
            self.assertIn("X: X [Log]", browser.status_label.text())
            self.assertIn("Y: Y [Log]", browser.status_label.text())

            settings = load_plot_format(Path(temp) / "log.plt")
            self.assertEqual(settings.x_scale, LOG_SCALE)
            self.assertEqual(settings.y_scale, LOG_SCALE)
            browser.close()

            restored = DatBrowserWidget(Path(temp))
            self.assertTrue(restored.load_path(path))
            self.assertEqual(restored.canvas.x_scale, LOG_SCALE)
            self.assertEqual(restored.canvas.y_scale, LOG_SCALE)
            restored.close()

    def test_multiple_y_overlay_stacked_shared_x_and_plt_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "multi.dat"
            path.write_text(
                "[Header]\nINFO, multi series test\n[Data]\n"
                "Time(s),Temperature(K),Resistance(Ohm),Note\n"
                "0,2.0,100,a\n1,2.2,300,b\n2,2.4,900,c\n3,2.6,1200,d\n",
                encoding="utf-8",
            )
            browser = DatBrowserWidget(Path(temp))
            browser.resize(980, 720)
            browser.show()
            self.application.processEvents()
            self.assertTrue(browser.load_path(path))
            self.assertTrue((Path(temp) / "multi.plt").exists())

            self.assertTrue(
                browser.canvas.set_axes(
                    "Time(s)",
                    ("Temperature(K)", "Resistance(Ohm)"),
                )
            )
            self.assertTrue(browser.canvas.set_layout("overlay"))
            overlay_temperature = browser.canvas._ranges("Temperature(K)")
            overlay_resistance = browser.canvas._ranges("Resistance(Ohm)")
            self.assertEqual(overlay_temperature, overlay_resistance)

            self.assertTrue(browser.canvas.set_layout("stacked"))
            panels = browser.canvas._plot_rects()
            self.assertEqual(len(panels), 2)
            temperature_range = browser.canvas._ranges("Temperature(K)")
            resistance_range = browser.canvas._ranges("Resistance(Ohm)")
            self.assertEqual(temperature_range[:2], resistance_range[:2])
            self.assertNotEqual(temperature_range[2:], resistance_range[2:])

            _, resistance_panel = panels[1]
            start = QPoint(
                int(resistance_panel.left() + resistance_panel.width() * 0.2),
                int(resistance_panel.top() + resistance_panel.height() * 0.2),
            )
            stop = QPoint(
                int(resistance_panel.left() + resistance_panel.width() * 0.8),
                int(resistance_panel.top() + resistance_panel.height() * 0.8),
            )
            QTest.mousePress(browser.canvas, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(browser.canvas, stop)
            QTest.mouseRelease(browser.canvas, Qt.MouseButton.LeftButton, pos=stop)
            self.application.processEvents()
            self.assertIsNotNone(browser.canvas._x_view)
            self.assertIn("Resistance(Ohm)", browser.canvas._stacked_y_views)
            self.assertNotIn("Temperature(K)", browser.canvas._stacked_y_views)

            settings = load_plot_format(Path(temp) / "multi.plt")
            self.assertEqual(settings.layout, "stacked")
            self.assertEqual(
                settings.y_columns,
                ("Temperature(K)", "Resistance(Ohm)"),
            )
            self.assertEqual(settings.x_range, browser.canvas._x_view)
            self.assertEqual(
                settings.stacked_y_ranges["Resistance(Ohm)"],
                browser.canvas._stacked_y_views["Resistance(Ohm)"],
            )
            browser.close()

            restored = DatBrowserWidget(Path(temp))
            restored.resize(980, 720)
            self.assertTrue(restored.load_path(path))
            self.assertEqual(restored.canvas.layout_mode, "stacked")
            self.assertEqual(
                restored.canvas.y_columns,
                ("Temperature(K)", "Resistance(Ohm)"),
            )
            self.assertEqual(restored.canvas._x_view, settings.x_range)
            self.assertEqual(
                restored.canvas._stacked_y_views,
                settings.stacked_y_ranges,
            )
            restored.close()


if __name__ == "__main__":
    unittest.main()
