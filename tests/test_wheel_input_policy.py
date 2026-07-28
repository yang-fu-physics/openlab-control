from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from labcontrol.app import configure_qt_appearance  # noqa: E402
from labcontrol.ui.input_policy import (  # noqa: E402
    install_wheel_input_policy,
)


def send_wheel(widget: QWidget, vertical_delta: int) -> None:
    """向指定控件发送一个与真实鼠标滚轮等价的单步事件。"""

    center = widget.rect().center()
    event = QWheelEvent(
        QPointF(center),
        QPointF(widget.mapToGlobal(center)),
        QPoint(),
        QPoint(0, vertical_delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(widget, event)
    QApplication.processEvents()


class WheelInputPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])
        configure_qt_appearance(cls.application, 1.0)

    def test_policy_is_installed_only_once(self) -> None:
        first = install_wheel_input_policy(self.application)
        configure_qt_appearance(self.application, 1.0)
        second = install_wheel_input_policy(self.application)
        self.assertIs(first, second)

    def test_wheel_does_not_change_integer_or_decimal_input(self) -> None:
        integer = QSpinBox()
        integer.setRange(0, 20)
        integer.setValue(10)
        decimal = QDoubleSpinBox()
        decimal.setRange(0.0, 20.0)
        decimal.setValue(10.0)
        integer.show()
        decimal.show()
        self.application.processEvents()

        send_wheel(integer, 120)
        send_wheel(decimal, -120)

        self.assertEqual(integer.value(), 10)
        self.assertEqual(decimal.value(), 10.0)

        # 禁用的只有滚轮；键盘和数值框按钮仍属于明确的编辑动作。
        integer.setFocus()
        QTest.keyClick(integer, Qt.Key.Key_Up)
        self.assertEqual(integer.value(), 11)
        integer.close()
        decimal.close()

    def test_closed_combo_does_not_change_and_page_keeps_scrolling(self) -> None:
        scroll = QScrollArea()
        scroll.resize(320, 180)
        content = QWidget()
        content.setMinimumSize(280, 900)
        layout = QVBoxLayout(content)
        layout.addSpacing(350)
        combo = QComboBox()
        combo.addItems(["100 pA", "316 pA", "1 nA"])
        combo.setCurrentIndex(1)
        layout.addWidget(combo)
        spin = QSpinBox()
        spin.setRange(0, 99)
        spin.setValue(20)
        layout.addWidget(spin)
        layout.addStretch(1)
        scroll.setWidget(content)
        scroll.setWidgetResizable(False)
        scroll.show()
        self.application.processEvents()

        bar = scroll.verticalScrollBar()
        bar.setValue(120)
        before_combo_scroll = bar.value()
        send_wheel(combo, -120)
        self.assertEqual(combo.currentIndex(), 1)
        self.assertGreater(bar.value(), before_combo_scroll)

        before_spin_scroll = bar.value()
        send_wheel(spin, -120)
        self.assertEqual(spin.value(), 20)
        self.assertGreater(bar.value(), before_spin_scroll)
        scroll.close()

    def test_open_combo_allows_wheel_to_browse_multiple_pages(self) -> None:
        combo = QComboBox()
        combo.addItems([f"Range {index}" for index in range(40)])
        combo.setMaxVisibleItems(6)
        combo.resize(180, combo.sizeHint().height())
        combo.show()
        combo.showPopup()
        self.application.processEvents()

        view = combo.view()
        self.assertTrue(view.isVisible())
        scrollbar = view.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        before = scrollbar.value()

        send_wheel(view.viewport(), -120)

        self.assertGreater(scrollbar.value(), before)
        combo.hidePopup()
        combo.close()


if __name__ == "__main__":
    unittest.main()
