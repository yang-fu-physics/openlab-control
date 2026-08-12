"""首次执行 System Instrument 或 Measurement Module 前的用户信任确认。

对话框展示 ID、版本、绝对位置和完整目录 SHA-256。默认按钮始终为 No；内容发生任何变化后
指纹不匹配，会重新询问，不能沿用旧确认。
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from ..instruments.manifest import SystemInstrumentDescriptor
from ..package_support.trust import ContentTrustStore
from ..measurement.manifest import ModuleDescriptor


def confirm_system_instrument_trust(
    parent: QWidget | None,
    store: ContentTrustStore,
    descriptor: SystemInstrumentDescriptor,
) -> bool:
    """确认可接触仪表和文件的 System Instrument，并记录精确内容指纹。"""

    if store.is_trusted("instrument", descriptor):
        return True
    answer = QMessageBox.question(
        parent,
        "Trust System Instrument?",
        (
            f"{descriptor.name} {descriptor.version}\n"
            f"ID: {descriptor.id}\n"
            f"Location: {descriptor.path}\n"
            f"SHA-256: {descriptor.fingerprint}\n\n"
            "This System Instrument is executable code and can access instruments and files. "
            "Trust it only if you know its source. Any content change will require "
            "confirmation again."
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return False
    store.trust("instrument", descriptor)
    return True


def confirm_measurement_module_trust(
    parent: QWidget | None,
    store: ContentTrustStore,
    descriptor: ModuleDescriptor,
) -> bool:
    """确认包含 Qt 前端与 worker 后端代码的 Measurement Module。"""

    if store.is_trusted("module", descriptor):
        return True
    answer = QMessageBox.question(
        parent,
        "Trust Measurement Module?",
        (
            f"{descriptor.name} {descriptor.version}\n"
            f"ID: {descriptor.id}\n"
            f"Location: {descriptor.path}\n"
            f"SHA-256: {descriptor.fingerprint}\n\n"
            "A measurement module contains executable UI and worker code. "
            "It can access files and its configured instrument. Trust it "
            "only if you know its source. Any content change will require "
            "confirmation again."
        ),
        QMessageBox.StandardButton.Yes
        | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return False
    store.trust("module", descriptor)
    return True
