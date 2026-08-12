# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from packaging.version import Version
from PyInstaller.utils.hooks import (
    collect_submodules,
    copy_metadata,
)
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


hiddenimports = (
    ["labcontrol.instruments.simulated"]
    # System Instrument 与 Measurement Module 会在独立 worker 中动态 import PyVISA，
    # PyInstaller 静态分析看不到。由核心统一收集后，两者使用同一个 1.16.2 版本。
    + collect_submodules(
        "pyvisa",
        filter=lambda name: not name.startswith(
            "pyvisa.testsuite"
        ),
    )
)
framework_metadata = []
for distribution in (
    "PySide6",
    "QtAwesome",
    "packaging",
    "PyVISA",
    "typing_extensions",
):
    # System Instrument 与 Measurement Module 可用 importlib.metadata 核对实际共享版本；否则 PyInstaller 中
    # PyVISA.__version__ 会退化为 unknown，无法证明锁定契约。
    framework_metadata += copy_metadata(distribution)
parsed_version = Version(__version__)
release_numbers = list(parsed_version.release[:3])
release_numbers.extend([0] * (3 - len(release_numbers)))
prerelease_number = (
    int(parsed_version.pre[1])
    if parsed_version.pre is not None
    else 0
)
version_numbers = tuple(release_numbers + [prerelease_number])
def make_version_info(description, internal_name, filename):
    return VSVersionInfo(
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
                            StringStruct("FileDescription", description),
                            StringStruct("FileVersion", __version__),
                            StringStruct("InternalName", internal_name),
                            StringStruct("OriginalFilename", filename),
                            StringStruct("ProductName", "OpenLab Control"),
                            StringStruct("ProductVersion", __version__),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


version_info = make_version_info(
    "OpenLab Control",
    "OpenLabControl",
    "OpenLabControl.exe",
)
scanner_version_info = make_version_info(
    "OpenLab Control Instrument Scanner",
    "InstrumentScanner",
    "InstrumentScanner.exe",
)

a = Analysis(
    ["run.py"],
    pathex=["src"],
    binaries=[],
    # Mutable configuration, modules, examples, documentation, and integration
    # references are staged beside the EXE by build.bat. Bundling them here
    # would create an unused second copy under _internal.
    datas=framework_metadata,
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

# 发布包把扫描器与主程序放在根目录。两个 onedir EXE 由同一个 COLLECT 收集，直接共享
# 唯一的 ``_internal``；源码版则直接使用主项目 .venv 中的同一组锁定依赖。
scanner_analysis = Analysis(
    ["tools/instrument_scanner.py"],
    pathex=["src"],
    binaries=[],
    datas=framework_metadata,
    hiddenimports=collect_submodules(
        "pyvisa",
        filter=lambda name: not name.startswith(
            "pyvisa.testsuite"
        ),
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
scanner_pyz = PYZ(scanner_analysis.pure)
scanner_exe = EXE(
    scanner_pyz,
    scanner_analysis.scripts,
    [],
    exclude_binaries=True,
    name="InstrumentScanner",
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
    version=scanner_version_info,
)

coll = COLLECT(
    exe,
    scanner_exe,
    a.binaries,
    a.datas,
    scanner_analysis.binaries,
    scanner_analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="OpenLabControl",
)
