"""1080p、4K 与不同 Windows DPI 环境共用的保守界面缩放。

Qt 仍负责设备像素比和字体栅格化；这里仅对代码中固定像素尺寸施加全局系数。自动缩放以
可用屏幕的物理像素估算，并限制在 1.0–1.4，避免 4K 小屏被放大过度或多屏切换后窗口失控。
"""

from __future__ import annotations

import math

from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication


BASE_WIDTH = 1920.0
BASE_HEIGHT = 1080.0
MIN_AUTO_SCALE = 1.0
MAX_AUTO_SCALE = 1.4


def automatic_ui_scale(pixel_width: float, pixel_height: float) -> float:
    """根据屏幕原生像素返回分档到 0.05 的保守缩放系数。"""
    if pixel_width <= 0 or pixel_height <= 0:
        return MIN_AUTO_SCALE
    resolution_ratio = min(pixel_width / BASE_WIDTH, pixel_height / BASE_HEIGHT)
    scale = math.sqrt(max(1.0, resolution_ratio))
    scale = min(MAX_AUTO_SCALE, max(MIN_AUTO_SCALE, scale))
    return round(scale * 20.0) / 20.0


def screen_ui_scale(screen: QScreen | None) -> float:
    """结合可用区域和 devicePixelRatio 计算一块实际屏幕的缩放。"""

    if screen is None:
        return MIN_AUTO_SCALE
    geometry = screen.availableGeometry()
    pixel_ratio = max(1.0, float(screen.devicePixelRatio()))
    return automatic_ui_scale(
        geometry.width() * pixel_ratio,
        geometry.height() * pixel_ratio,
    )


def current_ui_scale() -> float:
    """读取 QApplication 上由启动入口保存的统一缩放属性。"""

    application = QApplication.instance()
    if application is None:
        return 1.0
    value = application.property("openlabUiScale")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


def current_font_scale() -> float:
    """读取独立文字倍率；它不改变控件、间距或窗口几何。"""

    application = QApplication.instance()
    if application is None:
        return 1.0
    value = application.property("openlabFontScale")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


def scaled(value: float, scale: float | None = None) -> int:
    """缩放固定像素并返回至少为 1 的整数，供 Qt 几何尺寸使用。"""

    return max(1, round(value * (current_ui_scale() if scale is None else scale)))


def scaled_text(
    value: float,
    scale: float | None = None,
    font_scale: float | None = None,
) -> int:
    """同时应用整体与文字倍率，供样式表中的显式字号使用。"""

    return max(
        1,
        round(
            value
            * (current_ui_scale() if scale is None else scale)
            * (
                current_font_scale()
                if font_scale is None
                else font_scale
            )
        ),
    )


def scaled_float(value: float, scale: float | None = None) -> float:
    """缩放绘图线宽、坐标等需要保留小数的值。"""

    return value * (current_ui_scale() if scale is None else scale)
