"""给直接导出 LabVIEW VISA 参数的 DLL 提供地址转换。"""

import ctypes
import sys
from functools import cache
from pathlib import Path


@cache
def _converter() -> ctypes.CDLL:
    path = Path(__file__).with_name("labview") / "visa-str-to-source.dll"
    try:
        library = ctypes.CDLL(str(path))
    except OSError as exc:
        raise RuntimeError(
            f"Cannot load {path}. Check the matching 64-bit LabVIEW Runtime: {exc}"
        ) from exc
    library.VisaStrSource.argtypes = (
        ctypes.POINTER(ctypes.c_char), ctypes.POINTER(ctypes.c_size_t),
    )
    library.VisaStrSource.restype = ctypes.c_int32
    return library


def visa_resource_reference(address: str) -> ctypes.c_size_t:
    """转换 VISA 地址；只在加载目标 DLL 的同一 worker 内使用返回值。"""
    address = address.strip()
    if not address or not address.isascii() or "\0" in address:
        raise ValueError("VISA address must be a nonempty ASCII string.")
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("LabVIEW VISA conversion requires 64-bit Windows.")

    reference = ctypes.c_size_t()
    code = _converter().VisaStrSource(
        ctypes.create_string_buffer(address.encode("ascii")), ctypes.byref(reference),
    )
    if code != 0 or not reference.value:
        raise RuntimeError(
            f"VisaStrSource failed for {address!r}: return={code}, "
            f"visaOut=0x{reference.value:X}"
        )
    return reference
