"""主窗口 Measurement Module 监视卡。

卡片只消费核心已经校验过的运行状态和测量结果消息。它不持有 worker、VISA session
或模块前端，也不会主动请求仪表；点击行为仅通知主窗口恢复对应的独立模块窗口。
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..measurement.manifest import ModuleDescriptor
from .scaling import scaled


_MAX_VISIBLE_RESULT_LINES = 8


def format_compact_result(value: object, unit: str = "") -> str:
    """把一个结果压缩成适合窄侧栏的短字符串。"""

    if value is None:
        return "—"
    if isinstance(value, bool):
        return "On" if value else "Off"
    display_unit = {
        "Ohm": "Ω",
        "ohm": "Ω",
        "deg": "°",
    }.get(unit, unit)
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return "—"
        if not display_unit:
            return f"{number:.4g}"
        if number == 0:
            return f"0 {display_unit}"
        exponent = max(
            -12,
            min(
                12,
                int(
                    math.floor(
                        math.log10(abs(number)) / 3.0
                    )
                )
                * 3,
            ),
        )
        prefix = {
            -12: "p",
            -9: "n",
            -6: "µ",
            -3: "m",
            0: "",
            3: "k",
            6: "M",
            9: "G",
            12: "T",
        }[exponent]
        scaled_value = number / (10.0**exponent)
        text = f"{scaled_value:.4g}"
        if text == "-0":
            text = "0"
        return f"{text} {prefix}{display_unit}"
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > 32:
        text = text[:31] + "…"
    return text or "—"


class ModuleMonitorCard(QFrame):
    """一个 Enabled 模块的只读摘要；点击后请求恢复模块窗口。"""

    activated = Signal(str)

    def __init__(
        self,
        descriptor: ModuleDescriptor,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.descriptor = descriptor
        self._state = "enabled"
        self._message = ""
        self._minimized = False
        self._alerts: tuple[str, ...] = ()
        self._display_columns: tuple[str, ...] = ()
        self._results: dict[int, dict[str, object]] = {}
        self._multi_slot = False
        self.setObjectName("moduleMonitorCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(
            f"Open {descriptor.name} module window"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            scaled(6),
            scaled(4),
            scaled(6),
            scaled(4),
        )
        layout.setSpacing(0)
        self.name_label = QLabel(descriptor.name)
        self.name_label.setTextFormat(Qt.TextFormat.PlainText)
        self.name_label.setWordWrap(True)
        self.name_label.setMinimumWidth(0)
        self.name_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.name_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.name_label)
        self.state_label = QLabel("Enabled")
        self.state_label.setTextFormat(Qt.TextFormat.PlainText)
        self.state_label.setWordWrap(True)
        self.state_label.setMinimumWidth(0)
        self.state_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.state_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.state_label)
        self.results_label = QLabel("No results yet")
        self.results_label.setTextFormat(Qt.TextFormat.PlainText)
        self.results_label.setWordWrap(True)
        layout.addWidget(self.results_label)
        for label in (
            self.name_label,
            self.state_label,
            self.results_label,
        ):
            # 让整张卡片接收鼠标点击，而不是被内部 QLabel 截断。
            label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                True,
            )
        self._refresh_appearance()

    def set_display_columns(self, names: object) -> None:
        """设置模块选择的结果列；worker 已在握手时完成严格验证。"""

        if not isinstance(names, (list, tuple)):
            names = ()
        self._display_columns = tuple(
            str(name)
            for name in names
            if str(name).strip()
        )
        if not self._results:
            self.results_label.setText(
                "No results yet"
                if self._display_columns
                else "No compact results declared"
            )

    def update_runtime(
        self,
        state: str,
        message: str = "",
        *,
        minimized: bool | None = None,
    ) -> None:
        self._state = state
        self._message = message
        if minimized is not None:
            self._minimized = minimized
        self._refresh_appearance()

    def set_minimized(self, minimized: bool) -> None:
        if self._minimized == minimized:
            return
        self._minimized = minimized
        self._refresh_appearance()

    def set_alerts(self, severities: object) -> None:
        normalized = tuple(
            str(value).casefold()
            for value in (
                severities
                if isinstance(severities, (list, tuple, set))
                else ()
            )
        )
        if normalized == self._alerts:
            return
        self._alerts = normalized
        self._refresh_appearance()

    def reset_results(self) -> None:
        """在新的 Measure 前清空上一轮，避免把旧值误认为当前值。"""

        self._results.clear()
        self.results_label.setText(
            "Waiting for next Measure"
            if self._display_columns
            else "No compact results declared"
        )

    def update_result(
        self,
        payload: Mapping[str, object],
    ) -> None:
        """缓存一条核心结果消息；不执行模块代码或 I/O。"""

        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            return
        items: list[dict[str, object]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                continue
            name = str(raw_item.get("name", "")).strip()
            if not name:
                continue
            items.append({
                "name": name,
                "unit": str(raw_item.get("unit", "")),
                "value": raw_item.get("value"),
            })
        if not items:
            return
        slot = max(1, int(payload.get("slot", 1)))
        self._multi_slot = bool(
            payload.get("multi_slot", False)
        )
        # 没有 slots 的 follower 模块每个逻辑行都会被调用；只保留最新一行，避免同一
        # 台单次仪表在四通道 SEQ 中显示四份近似相同的结果。
        cache_key = slot if self._multi_slot else 0
        self._results[cache_key] = {
            "slot": slot,
            "items": items,
        }
        if not self._display_columns:
            self._display_columns = tuple(
                str(item["name"])
                for item in items
            )
        self._render_results()

    def _render_results(self) -> None:
        entries = sorted(
            self._results.values(),
            key=lambda item: int(item["slot"]),
        )
        hidden = max(
            0,
            len(entries) - _MAX_VISIBLE_RESULT_LINES,
        )
        if hidden:
            entries = entries[-_MAX_VISIBLE_RESULT_LINES:]
        lines: list[str] = []
        for entry in entries:
            slot = int(entry["slot"])
            items = list(entry["items"])
            present = [
                item
                for item in items
                if item.get("value") is not None
            ]
            if (
                self._multi_slot
                and len(self._display_columns) == 1
            ):
                item = items[0]
                lines.append(
                    f"CH{slot}  "
                    f"{format_compact_result(item.get('value'), str(item.get('unit', '')))}"
                )
                continue
            if not present:
                lines.append(
                    f"CH{slot}  —"
                    if self._multi_slot
                    else "—"
                )
                continue
            lines.append(
                "   ".join(
                    f"{item['name']} "
                    f"{format_compact_result(item.get('value'), str(item.get('unit', '')))}"
                    for item in present
                )
            )
        if hidden:
            lines.append(f"… {hidden} earlier channel(s)")
        self.results_label.setText(
            "\n".join(lines) or "No results yet"
        )

    def _refresh_appearance(self) -> None:
        alert = (
            "error"
            if "error" in self._alerts
            else "warning"
            if "warning" in self._alerts
            else ""
        )
        visual_state = (
            alert
            or (
                "error"
                if self._state == "faulted"
                else self._state
            )
        )
        colors = {
            "enabled": (
                "#2e7d32",
                "rgba(46, 125, 50, 0.08)",
            ),
            "measuring": (
                "#1565c0",
                "rgba(21, 101, 192, 0.10)",
            ),
            "warning": (
                "#a86100",
                "rgba(255, 167, 38, 0.13)",
            ),
            "error": (
                "#b3261e",
                "rgba(179, 38, 30, 0.12)",
            ),
            "initializing": (
                "#6d5d00",
                "rgba(109, 93, 0, 0.10)",
            ),
        }
        border, background = colors.get(
            visual_state,
            ("#7a7a7a", "rgba(120, 120, 120, 0.08)"),
        )
        self.setStyleSheet(
            "QFrame#moduleMonitorCard {"
            f"border: 1px solid {border};"
            f"border-left: {scaled(4)}px solid {border};"
            f"border-radius: {scaled(5)}px;"
            f"background: {background};"
            "}"
        )
        if alert:
            state_text = alert.title()
            count = sum(
                1
                for value in self._alerts
                if value == alert
            )
            if count > 1:
                state_text += f" ({count})"
        else:
            state_text = self._state.replace(
                "_",
                " ",
            ).title()
        if self._minimized:
            state_text += " · Minimized"
        self.state_label.setText(state_text)
        details = [self.descriptor.name, state_text]
        if self._message:
            details.append(self._message)
        self.setToolTip(escape("\n".join(details)))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(
                event.position().toPoint()
            )
        ):
            self.activated.emit(self.descriptor.id)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in {
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        }:
            self.activated.emit(self.descriptor.id)
            event.accept()
            return
        super().keyPressEvent(event)


class ModuleMonitorPanel(QGroupBox):
    """管理左侧所有模块卡片、结果缓存和活动报警颜色。"""

    activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Measurement Modules", parent)
        self.setObjectName("moduleMonitorGroup")
        self.cards: dict[str, ModuleMonitorCard] = {}
        self._alerts: dict[str, dict[str, str]] = {}

        group_layout = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("moduleMonitorScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(scaled(5))
        self.empty_label = QLabel(
            "No enabled measurement modules"
        )
        self.empty_label.setWordWrap(True)
        self.empty_label.setObjectName("mutedLabel")
        self.content_layout.addWidget(self.empty_label)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        group_layout.addWidget(self.scroll)

    def update_module(
        self,
        descriptor: ModuleDescriptor,
        *,
        enabled: bool,
        state: str,
        message: str,
        minimized: bool,
        display_columns: object,
    ) -> None:
        """根据一条 module_state 消息创建、更新或移除卡片。"""

        if not enabled:
            self.remove_module(descriptor.id)
            return
        card = self.cards.get(descriptor.id)
        if card is None:
            card = ModuleMonitorCard(
                descriptor,
                self.content,
            )
            card.activated.connect(self.activated.emit)
            self.content_layout.insertWidget(
                max(0, self.content_layout.count() - 1),
                card,
            )
            self.cards[descriptor.id] = card
            self.empty_label.setVisible(False)
            card.set_alerts(
                tuple(
                    self._alerts.get(
                        descriptor.id,
                        {},
                    ).values()
                )
            )
        card.set_display_columns(display_columns)
        card.update_runtime(
            state,
            message,
            minimized=minimized,
        )

    def remove_module(self, module_id: str) -> None:
        card = self.cards.pop(module_id, None)
        if card is not None:
            self.content_layout.removeWidget(card)
            card.deleteLater()
        self._alerts.pop(module_id, None)
        self.empty_label.setVisible(not self.cards)

    def clear(self) -> None:
        for module_id in tuple(self.cards):
            self.remove_module(module_id)

    def update_result(
        self,
        payload: Mapping[str, object],
    ) -> None:
        card = self.cards.get(
            str(payload.get("module_id", ""))
        )
        if card is not None:
            card.update_result(payload)

    def reset_results(self, module_id: str) -> None:
        card = self.cards.get(module_id)
        if card is not None:
            card.reset_results()

    def set_minimized(
        self,
        module_id: str,
        minimized: bool,
    ) -> None:
        card = self.cards.get(module_id)
        if card is not None:
            card.set_minimized(minimized)

    def update_alert(
        self,
        module_id: str,
        event_key: str,
        severity: str,
        *,
        resolved: bool,
    ) -> None:
        """更新去重事件状态；事件详情和弹窗仍由主窗口负责。"""

        active = self._alerts.setdefault(module_id, {})
        if resolved:
            active.pop(event_key, None)
        elif severity in {"warning", "error"}:
            active[event_key] = severity
        if not active:
            self._alerts.pop(module_id, None)
        card = self.cards.get(module_id)
        if card is not None:
            card.set_alerts(tuple(active.values()))
