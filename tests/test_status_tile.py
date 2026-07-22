from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.models import DeviceActivity, DeviceKind, DeviceSnapshot  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
