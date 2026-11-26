from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from labcontrol.ui.data_browser import (  # noqa: E402
    DatBrowserWidget,
    PointDetailsDialog,
)
from labcontrol.plot_format import LOG_SCALE, load_plot_format  # noqa: E402
from labcontrol.ui.dat_plot import (  # noqa: E402
    PlotHit,
    YSeriesSelectionDialog,
)


class DataBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

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
            QTest.qWait(900)
            self.application.processEvents()
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
            self.assertIn("Copy File Path", labels)
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

    def test_hover_updates_single_line_status_and_clears_on_leave(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hover.dat"
            path.write_text(
                "[Data]\nTemp,R1(Ohm),R2(Ohm),R3(Ohm),R4(Ohm)\n"
                "292,0.01322,0.00940,0.03060,0.01800\n"
                "296,0.01328,0.00946,0.03075,0.01806\n"
                "300,0.01334,0.00954,0.03100,0.01814\n",
                encoding="utf-8",
            )
            browser = DatBrowserWidget(Path(temp))
            browser.resize(900, 640)
            browser.show()
            self.application.processEvents()
            self.assertTrue(browser.load_path(path))
            self.assertTrue(browser.path_label.isHidden())
            self.assertFalse(browser.status_label.wordWrap())
            self.assertTrue(
                browser.canvas.set_axes(
                    "Temp",
                    ("R1(Ohm)", "R2(Ohm)", "R3(Ohm)", "R4(Ohm)"),
                )
            )
            self.assertTrue(browser.canvas.set_layout("stacked"))
            browser.canvas.repaint()
            self.application.processEvents()

            panels = browser.canvas._plot_rects()
            self.assertEqual(len(panels), 4)
            r1_rect = panels[0][1]
            ranges = browser.canvas._ranges("R1(Ohm)")
            self.assertIsNotNone(ranges)
            pt = browser.canvas.points_by_series["R1(Ohm)"][1]
            screen = browser.canvas._screen_point(pt.x, pt.y, r1_rect, ranges)
            self.assertIsNotNone(screen)
            QTest.mouseMove(browser.canvas, screen.toPoint())
            self.application.processEvents()
            self.assertIn("R1(Ohm) = 0.01328", browser.status_label.text())
            self.assertIn("Temp = 296", browser.status_label.text())

            browser.canvas.leaveEvent(None)
            self.application.processEvents()
            self.assertNotIn("R1(Ohm) = 0.01328", browser.status_label.text())
            browser.close()

    def test_large_dataset_100k_hover_reuses_cache_preserves_spikes_and_stays_fast(
        self,
    ) -> None:
        import time

        from PySide6.QtGui import QMouseEvent
        from labcontrol.dat_reader import DatDocument
        from labcontrol.ui.dat_plot import DatPlotCanvas

        n_rows = 100_000
        columns = ("Temp(K)", "R1(Ohm)", "R2(Ohm)", "R3(Ohm)", "R4(Ohm)")
        rows = tuple(
            (
                f"{2.0 + 298.0 * i / (n_rows - 1):.4f}",
                f"{0.013 + 0.001 * (i / n_rows) + (0.0005 if i == 50_000 else 0.0):.6f}",
                f"{0.009 + 0.002 * (i / n_rows):.6f}",
                f"{0.030 + 0.003 * (i / n_rows):.6f}",
                f"{0.018 + 0.0015 * (i / n_rows):.6f}",
            )
            for i in range(n_rows)
        )
        document = DatDocument(
            path=None,
            header_lines=(),
            columns=columns,
            rows=rows,
            modified_ns=0,
            size_bytes=0,
        )
        canvas = DatPlotCanvas()
        try:
            canvas.resize(900, 620)
            canvas.show()
            canvas.set_document(document)
            self.assertTrue(
                canvas.set_axes(
                    "Temp(K)",
                    ("R1(Ohm)", "R2(Ohm)", "R3(Ohm)", "R4(Ohm)"),
                )
            )
            for layout in ("overlay", "stacked"):
                with self.subTest(layout=layout):
                    self.assertTrue(canvas.set_layout(layout))
                    canvas.repaint()
                    self.application.processEvents()
                    cached_pixmap = canvas._base_pixmap
                    self.assertIsNotNone(cached_pixmap)

                    panels = canvas._plot_rects()
                    r1_rect = panels[0][1]
                    ranges = canvas._ranges("R1(Ohm)")
                    self.assertIsNotNone(ranges)

                    # Single-row spike at index 50,000 must survive decimation and be hittable.
                    spike = canvas.points_by_series["R1(Ohm)"][50_000]
                    spike_pos = canvas._screen_point(spike.x, spike.y, r1_rect, ranges)
                    self.assertIsNotNone(spike_pos)
                    spike_hit = canvas._nearest_point(spike_pos)
                    self.assertIsNotNone(spike_hit)
                    self.assertAlmostEqual(spike_hit.y, spike.y, places=6)

                    # 20 consecutive mouse moves + repaints must reuse base cache and stay fast.
                    elapsed_ms: list[float] = []
                    for step in range(20):
                        idx = int((step + 1) * (n_rows - 2) / 21)
                        pt = canvas.points_by_series["R1(Ohm)"][idx]
                        pos = canvas._screen_point(pt.x, pt.y, r1_rect, ranges)
                        self.assertIsNotNone(pos)
                        event = QMouseEvent(
                            QMouseEvent.Type.MouseMove,
                            pos,
                            pos,
                            Qt.MouseButton.NoButton,
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier,
                        )
                        t0 = time.perf_counter()
                        canvas.mouseMoveEvent(event)
                        canvas.repaint()
                        elapsed_ms.append((time.perf_counter() - t0) * 1000.0)
                        self.assertIsNotNone(canvas._hover_hit)

                    self.assertIs(canvas._base_pixmap, cached_pixmap)
                    self.assertLess(max(elapsed_ms), 50.0)
                    self.assertLess(sum(elapsed_ms) / len(elapsed_ms), 15.0)
        finally:
            canvas.close()

    def test_constant_x_4000_rows_middle_points_remain_hoverable_and_clickable(
        self,
    ) -> None:
        from PySide6.QtGui import QMouseEvent
        from labcontrol.dat_reader import DatDocument
        from labcontrol.ui.dat_plot import DatPlotCanvas

        n_rows = 4_000
        columns = ("Temp(K)", "R1(Ohm)")
        rows = tuple(
            ("300.000", f"{10.0 + 90.0 * i / (n_rows - 1):.4f}")
            for i in range(n_rows)
        )
        document = DatDocument(
            path=None,
            header_lines=(),
            columns=columns,
            rows=rows,
            modified_ns=0,
            size_bytes=0,
        )
        canvas = DatPlotCanvas()
        try:
            canvas.resize(900, 620)
            canvas.show()
            canvas.set_document(document)
            self.assertTrue(canvas.set_axes("Temp(K)", ("R1(Ohm)",)))

            # Verify both lazy hit-grid path (before repaint) and painted cache path.
            for painted in (False, True):
                if painted:
                    canvas.repaint()
                    self.application.processEvents()
                plot = canvas._plot_rect()
                ranges = canvas._ranges("R1(Ohm)")
                self.assertIsNotNone(ranges)

                activated: list[PlotHit] = []
                canvas.pointActivated.connect(activated.append)
                try:
                    for row_idx in (1_000, 2_000, 3_000):
                        pt = canvas.points_by_series["R1(Ohm)"][row_idx]
                        pos = canvas._screen_point(pt.x, pt.y, plot, ranges)
                        self.assertIsNotNone(pos)

                        hit = canvas._nearest_point(pos)
                        self.assertIsNotNone(hit, f"row {row_idx} (painted={painted}) missed")
                        self.assertAlmostEqual(hit.x, 300.0, places=6)
                        self.assertAlmostEqual(hit.y, pt.y, delta=1.0)

                        move_ev = QMouseEvent(
                            QMouseEvent.Type.MouseMove,
                            pos,
                            pos,
                            Qt.MouseButton.NoButton,
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier,
                        )
                        canvas.mouseMoveEvent(move_ev)
                        self.assertIsNotNone(canvas._hover_hit)
                        self.assertAlmostEqual(canvas._hover_hit.y, pt.y, delta=1.0)

                        dclick_ev = QMouseEvent(
                            QMouseEvent.Type.MouseButtonDblClick,
                            pos,
                            pos,
                            Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier,
                        )
                        canvas.mouseDoubleClickEvent(dclick_ev)
                    self.assertEqual(len(activated), 3)
                    self.assertAlmostEqual(activated[1].y, 55.0, delta=1.0)
                finally:
                    canvas.pointActivated.disconnect(activated.append)
        finally:
            canvas.close()


if __name__ == "__main__":
    unittest.main()
