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

from labcontrol.ui.data_browser import DatBrowserWidget  # noqa: E402
from labcontrol.plot_format import LOG_SCALE, load_plot_format  # noqa: E402
from labcontrol.ui.dat_plot import YSeriesSelectionDialog  # noqa: E402


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
