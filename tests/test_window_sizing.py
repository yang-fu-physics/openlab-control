from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QHeaderView,
    QScrollArea,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from labcontrol.ui.window_sizing import (  # noqa: E402
    fit_initial_window_width,
    preserve_restored_window_size,
)


class InitialWindowWidthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = (
            QApplication.instance() or QApplication([])
        )

    def _settle_layout(self) -> None:
        # 适配器按零延迟定时器逐轮处理可能新出现的竖向滚动条，连续处理事件可
        # 在 offscreen 平台上得到与真实首次绘制相同的稳定几何。
        for _ in range(12):
            self.application.processEvents()

    def test_first_show_uses_pixel_minimum_without_horizontal_scroll(
        self,
    ) -> None:
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        content.setMinimumWidth(500)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        fit_initial_window_width(
            dialog,
            preferred_height=260,
        )

        dialog.show()
        self._settle_layout()
        fitted_width = dialog.width()
        self.assertFalse(
            scroll.horizontalScrollBar().isVisible()
        )

        # 少一个像素就重新出现横向溢出，证明结果不是“足够宽”的任意值，而是
        # 当前样式、边框和布局下能完整容纳内容的最小宽度。
        dialog.resize(fitted_width - 1, dialog.height())
        self._settle_layout()
        self.assertTrue(
            scroll.horizontalScrollBar().isVisible()
        )
        dialog.close()

    def test_user_width_is_preserved_after_first_show(
        self,
    ) -> None:
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        content.setMinimumWidth(420)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        fit_initial_window_width(dialog)

        dialog.show()
        self._settle_layout()
        user_width = dialog.width() + 137
        dialog.resize(user_width, dialog.height())
        dialog.hide()
        dialog.show()
        self._settle_layout()

        self.assertEqual(dialog.width(), user_width)
        dialog.close()

    def test_item_scrolling_table_uses_pixel_header_deficit(
        self,
    ) -> None:
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        table = QTableWidget(0, 3, dialog)
        table.setHorizontalHeaderLabels(
            [
                "Enabled",
                "A deliberately long module name",
                "Version",
            ]
        )
        for section in range(3):
            table.horizontalHeader().setSectionResizeMode(
                section,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        layout.addWidget(table)
        fit_initial_window_width(
            dialog,
            preferred_height=240,
        )

        dialog.show()
        self._settle_layout()
        fitted_width = dialog.width()
        self.assertFalse(
            table.horizontalScrollBar().isVisible()
        )
        dialog.resize(fitted_width - 1, dialog.height())
        self._settle_layout()
        self.assertTrue(
            table.horizontalScrollBar().isVisible()
        )
        dialog.close()

    def test_restored_geometry_skips_default_width_fit(
        self,
    ) -> None:
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        content = QWidget(dialog)
        content.setMinimumWidth(220)
        layout.addWidget(content)
        fit_initial_window_width(dialog)
        # 用户保存的宽度必须大于布局最低值；恢复一个低于内容下限的宽度时，Qt
        # 自身仍会正确扩大窗口，这不属于首次适配器覆盖。
        dialog.resize(700, 277)
        preserve_restored_window_size(dialog)

        dialog.show()
        self._settle_layout()

        self.assertEqual(dialog.width(), 700)
        self.assertEqual(dialog.height(), 277)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
