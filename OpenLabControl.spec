# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


sys.path.insert(0, str(Path.cwd() / "src"))
hiddenimports = ["labcontrol.devices.simulated"] + collect_submodules("labcontrol_plugins")

a = Analysis(
    ["run.py"],
    pathex=["src"],
    binaries=[],
    datas=[
        ("configs", "configs"),
        ("examples", "examples"),
        ("docs", "docs"),
        ("plugin_templates", "plugin_templates"),
        ("modules", "modules"),
        ("README.md", "."),
        ("CHANGELOG.md", "."),
        ("SECURITY.md", "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OpenLabControl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="OpenLabControl",
)
