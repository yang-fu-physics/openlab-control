"""浮动窗口首次显示时的横向内容适配。

Qt 的 ``sizeHint`` 表示“首选尺寸”，不等同于刚好容纳内容的最小尺寸。测量模块的
Settings 页又包含可缩放的 ``QScrollArea``，如果直接采用首选尺寸，窗口常常明显偏宽；
如果只采用 ``minimumSizeHint``，首次显示又可能出现横向滚动条。

这里采用两阶段计算：Show 事件到来时先缩到布局允许的最小宽度；布局稳定后读取所有
可见 ``QAbstractScrollArea`` 的横向溢出量，并只增加缺少的像素。若竖向滚动条随布局
变化而占用新宽度，会在下一轮继续补齐。适配最多执行八轮且只在窗口第一次显示时运行，
因此不会在用户后续手动缩放或重新打开窗口时夺回尺寸控制权。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QListView,
    QTableView,
    QWidget,
)
from shiboken6 import isValid


_MAX_EXPANSION_PASSES = 8


class _InitialWidthFitter(QObject):
    """监听一个窗口的首次 Show，并完成一次无横向滚动条的紧凑适配。"""

    def __init__(
        self,
        target: QWidget,
        preferred_height: int | None,
    ) -> None:
        super().__init__(target)
        self._target = target
        self._preferred_height = (
            None
            if preferred_height is None
            else max(1, int(preferred_height))
        )
        self._passes = 0
        self._scheduled = False
        self._finished = False
        target.installEventFilter(self)

    def eventFilter(  # noqa: N802
        self,
        watched: QObject,
        event: QEvent,
    ) -> bool:
        # QObject 销毁阶段仍可能向已安装的过滤器发送最后几个事件，而 Python
        # 包装对象的属性可能已经开始清理。此时直接放行，避免关闭主窗口时产生
        # “Error calling Python override”噪声。
        target = getattr(self, "_target", None)
        if target is None or not isValid(target):
            return False
        if (
            watched is target
            and event.type() == QEvent.Type.Show
            and not getattr(self, "_finished", True)
        ):
            self._set_layout_minimum_width()
            self._schedule_expansion()
        return False

    def _set_layout_minimum_width(self) -> None:
        """先缩到布局下限；真正的滚动区缺口在 Show 后才能可靠读取。"""

        target = self._target
        target.ensurePolished()
        layout = target.layout()
        if layout is not None:
            layout.activate()
        width = max(
            1,
            target.minimumWidth(),
            target.minimumSizeHint().width(),
        )
        height_hint = (
            target.sizeHint().height()
            if self._preferred_height is None
            else self._preferred_height
        )
        height = max(
            1,
            target.minimumHeight(),
            height_hint,
        )
        target.resize(width, height)

    def _schedule_expansion(self) -> None:
        if self._scheduled or self._finished:
            return
        self._scheduled = True
        QTimer.singleShot(0, self._expand_visible_overflow)

    def _expand_visible_overflow(self) -> None:
        """按最大可见横向缺口扩展；多个并列滚动区无需把缺口相加。"""

        self._scheduled = False
        target = self._target
        if not isValid(target):
            self._finished = True
            return
        if not target.isVisible():
            # 首次 Show 后立即 Hide 的窗口不应被标记完成；下次显示仍可正确适配。
            return
        layout = target.layout()
        if layout is not None:
            layout.activate()
        deficit = 0
        for area in target.findChildren(QAbstractScrollArea):
            if not area.isVisibleTo(target):
                continue
            scrollbar = area.horizontalScrollBar()
            if not scrollbar.isVisible():
                continue
            overflow = scrollbar.maximum() - scrollbar.minimum()
            if isinstance(area, QTableView):
                # QTableView 默认 ScrollPerItem，此时 maximum=1 表示“还能滚动一
                # 列”，并不是缺一个像素。表头总宽减去 viewport 才是实际像素缺口。
                overflow = max(
                    overflow,
                    area.horizontalHeader().length()
                    - area.viewport().width(),
                )
            elif isinstance(area, QListView):
                # QListView 也可能按条目滚动；单列内容的 sizeHint 能给出真实宽度。
                overflow = max(
                    overflow,
                    area.sizeHintForColumn(0)
                    - area.viewport().width(),
                )
            deficit = max(deficit, overflow)
        if deficit <= 0:
            self._finish()
            return

        previous_width = target.width()
        target.resize(previous_width + deficit, target.height())
        self._passes += 1
        if (
            self._passes >= _MAX_EXPANSION_PASSES
            or target.width() <= previous_width
        ):
            # maximumWidth 或屏幕窗口管理器可能阻止继续扩展；此时停止循环，保留
            # 可访问的滚动条比持续争抢窗口尺寸更安全。
            self._finish()
            return
        self._schedule_expansion()

    def _finish(self) -> None:
        self._finished = True
        if isValid(self._target):
            self._target.removeEventFilter(self)


def fit_initial_window_width(
    target: QWidget,
    *,
    preferred_height: int | None = None,
) -> None:
    """让 ``target`` 第一次显示为无横向滚动条的最小可用宽度。

    ``preferred_height`` 只保留各窗口已有的纵向设计；本函数不会设置永久最大/最小宽度，
    也不会在第一次适配完成后响应再次显示，因此用户仍可自由调整窗口。
    """

    existing = getattr(
        target,
        "_openlab_initial_width_fitter",
        None,
    )
    if isinstance(existing, _InitialWidthFitter):
        return
    fitter = _InitialWidthFitter(target, preferred_height)
    # QObject 的 C++ parent 已能保证生命周期；同时保留 Python 引用，避免绑定层在
    # Show 事件到来前回收包装对象。
    setattr(
        target,
        "_openlab_initial_width_fitter",
        fitter,
    )


def preserve_restored_window_size(target: QWidget) -> None:
    """窗口已恢复用户几何时，取消首次内容宽度适配。

    首次适配只负责提供默认尺寸；若它在 ``restoreGeometry`` 后继续运行，会在 Show
    事件中覆盖用户上次保存的宽度。
    """

    fitter = getattr(
        target,
        "_openlab_initial_width_fitter",
        None,
    )
    if isinstance(fitter, _InitialWidthFitter):
        fitter._finish()


__all__ = [
    "fit_initial_window_width",
    "preserve_restored_window_size",
]
