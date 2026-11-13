import ctypes
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from labcontrol import labview_visa  # noqa: E402


class LabVIEWVisaTests(unittest.TestCase):
    def setUp(self):
        labview_visa._converter.cache_clear()
        self.addCleanup(labview_visa._converter.cache_clear)
        self.enterContext(patch.object(labview_visa.sys, "platform", "win32"))
        self.enterContext(patch.object(labview_visa.ctypes, "sizeof", return_value=8))
        self.load = self.enterContext(patch.object(labview_visa.ctypes, "CDLL"))

    def test_load_once_and_preserve_full_reference(self):
        value = 0x1EE36DC05C0

        def convert(address, out):
            self.assertEqual(address.value, b"GPIB0::14::INSTR")
            ctypes.cast(out, ctypes.POINTER(ctypes.c_size_t))[0] = value
            return 0

        function = self.load.return_value.VisaStrSource = Mock(side_effect=convert)
        self.load.assert_not_called()
        for _ in range(2):
            self.assertEqual(labview_visa.visa_resource_reference("GPIB0::14::INSTR").value, value)
        path = Path(self.load.call_args.args[0])
        binary = path.read_bytes()
        pe = struct.unpack_from("<I", binary, 0x3C)[0]
        self.assertEqual(binary[pe:pe + 6], b"PE\0\0\x64\x86")
        self.load.assert_called_once()
        self.assertEqual(function.argtypes, (ctypes.POINTER(ctypes.c_char), ctypes.POINTER(ctypes.c_size_t)))
        self.assertIs(function.restype, ctypes.c_int32)

    def test_invalid_input_does_not_load_dll(self):
        for address in (" ", "GPIB\0", "测试"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                labview_visa.visa_resource_reference(address)
        self.load.assert_not_called()

    def test_conversion_error_and_missing_reference_are_reported(self):
        function = self.load.return_value.VisaStrSource
        for code in (-1073807346, 0):
            function.return_value = code
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, f"return={code}, visaOut=0x0"):
                labview_visa.visa_resource_reference("GPIB0::14::INSTR")

    def test_load_failure_names_runtime(self):
        self.load.side_effect = OSError("missing dependency")
        with self.assertRaisesRegex(RuntimeError, "LabVIEW Runtime: missing dependency"):
            labview_visa.visa_resource_reference("GPIB0::14::INSTR")
