from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from ..devices.manifest import DevicePluginDescriptor
from ..extensions.trust import PluginTrustStore


def confirm_device_plugin_trust(
    parent: QWidget | None,
    store: PluginTrustStore,
    descriptor: DevicePluginDescriptor,
) -> bool:
    if store.is_trusted("device", descriptor):
        return True
    answer = QMessageBox.question(
        parent,
        "Trust Device Plugin?",
        (
            f"{descriptor.name} {descriptor.version}\n"
            f"ID: {descriptor.id}\n"
            f"Location: {descriptor.path}\n"
            f"SHA-256: {descriptor.fingerprint}\n\n"
            "This plugin is executable code and can access instruments and files. "
            "Trust it only if you know its source. Any content change will require "
            "confirmation again."
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return False
    store.trust("device", descriptor)
    return True
