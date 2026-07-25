# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


sys.path.insert(0, str(Path.cwd() / "src"))
from labcontrol import __version__


hiddenimports = ["labcontrol.devices.simulated"] + collect_submodules("labcontrol_plugins")
version_numbers = tuple(
    (list(map(int, __version__.split("."))) + [0, 0, 0, 0])[:4]
)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=version_numbers,
        prodvers=version_numbers,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "OpenLab"),
                        StringStruct("FileDescription", "OpenLab Control"),
                        StringStruct("FileVersion", __version__),
                        StringStruct("InternalName", "OpenLabControl"),
                        StringStruct("OriginalFilename", "OpenLabControl.exe"),
                        StringStruct("ProductName", "OpenLab Control"),
                        StringStruct("ProductVersion", __version__),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    ["run.py"],
    pathex=["src"],
    binaries=[],
    # Mutable configuration, modules, examples, and documentation are staged
    # beside the EXE by build.bat. Bundling them here would create an unused
    # second copy under _internal.
    datas=[],
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
    version=version_info,
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
