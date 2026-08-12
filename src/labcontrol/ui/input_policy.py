"""仪表控制界面的全局鼠标滚轮输入策略。

普通桌面软件常允许滚轮直接修改数值框或未展开的下拉框，但在仪表控制界面中，
用户往往只是想滚动较长的 Settings 页面。若鼠标恰好停在量程、通道、电流或目标值
控件上，Qt 的默认行为会悄悄改变参数。因此这里在 QApplication 层统一保护所有核心
窗口与 Measurement Module 前端，包括以后按需加载的第三方模块。
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QWidget,
)


_POLICY_ATTRIBUTE = "_openlab_wheel_input_policy"


class WheelInputPolicy(QObject):
    """阻止滚轮误改输入值，同时把滚轮动作转交给外层滚动页面。

    下拉列表已经展开时不进行拦截。此时滚轮事件的目标通常是下拉列表的 viewport，
    即使某个平台把事件先交给 QComboBox，也会通过 ``view().isVisible()`` 放行，
    因而较长的选项列表仍可正常翻页和浏览。
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel or not isinstance(event, QWheelEvent):
            return False
        if not isinstance(watched, QWidget):
            return False

        editor = self._protected_editor(watched)
        if editor is None:
            return False
        if isinstance(editor, QComboBox) and editor.view().isVisible():
            # 展开的列表可能包含多页选项；保持 Qt 原生滚轮浏览行为。
            return False

        self._forward_to_scroll_page(watched, editor, event)
        event.accept()
        return True

    @staticmethod
    def _protected_editor(widget: QWidget) -> QAbstractSpinBox | QComboBox | None:
        """找到事件所属的编辑控件，包括数值框内部的 QLineEdit。

        部分 Qt 平台会先把滚轮事件发送给数值框内部的文本编辑器，再向父控件传播。
        沿父级查找可确保源码版、Windows 打包版及不同 Qt 后端行为一致。
        """

        current: QWidget | None = widget
        while current is not None:
            if isinstance(current, (QAbstractSpinBox, QComboBox)):
                return current
            current = current.parentWidget()
        return None

    @classmethod
    def _forward_to_scroll_page(
        cls,
        source: QWidget,
        editor: QWidget,
        event: QWheelEvent,
    ) -> None:
        """把被保护控件上的滚轮动作发送给最近的可滚动外层页面。

        直接吞掉事件会让鼠标经过输入框时形成“滚动死区”。这里复制原事件并转换局部
        坐标，再交给 QScrollArea 的 viewport；原控件完全收不到事件，页面却仍能滚动。
        """

        scroll_area = cls._nearest_scroll_area(editor, event)
        if scroll_area is None:
            return

        viewport = scroll_area.viewport()
        local_position = QPointF(
            source.mapTo(viewport, event.position().toPoint())
        )
        forwarded = QWheelEvent(
            local_position,
            event.globalPosition(),
            event.pixelDelta(),
            event.angleDelta(),
            event.buttons(),
            event.modifiers(),
            event.phase(),
            event.inverted(),
            event.source(),
            # ``pointingDevice`` 是 Qt 对鼠标、触控板等输入硬件的称呼，
            # 不是 OpenLab Control 的 System Instrument。
            event.pointingDevice(),
        )
        QCoreApplication.sendEvent(viewport, forwarded)

    @staticmethod
    def _nearest_scroll_area(
        editor: QWidget,
        event: QWheelEvent,
    ) -> QAbstractScrollArea | None:
        """选择最近且在当前滚轮方向上确实具有滚动范围的父页面。"""

        delta = event.pixelDelta()
        if delta.isNull():
            delta = event.angleDelta()
        wants_vertical = delta.y() != 0 or delta.x() == 0
        wants_horizontal = delta.x() != 0

        current = editor.parentWidget()
        while current is not None:
            if isinstance(current, QAbstractScrollArea):
                vertical = current.verticalScrollBar()
                horizontal = current.horizontalScrollBar()
                if (
                    wants_vertical
                    and vertical.maximum() > vertical.minimum()
                ) or (
                    wants_horizontal
                    and horizontal.maximum() > horizontal.minimum()
                ):
                    return current
            current = current.parentWidget()
        return None


def install_wheel_input_policy(application: QApplication) -> WheelInputPolicy:
    """为一个 QApplication 安装一次全局策略，并保留强引用防止被 Qt 回收。"""

    existing = getattr(application, _POLICY_ATTRIBUTE, None)
    if isinstance(existing, WheelInputPolicy):
        return existing

    policy = WheelInputPolicy(application)
    application.installEventFilter(policy)
    setattr(application, _POLICY_ATTRIBUTE, policy)
    return policy
