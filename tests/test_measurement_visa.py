from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from labcontrol.measurement.visa import VisaSession


class VisaSessionTests(unittest.TestCase):
    def test_closing_a_probe_keeps_the_other_handle_open(self) -> None:
        manager = Mock()
        active, probe = Mock(), Mock()
        manager.open_resource.side_effect = [active, probe]
        with patch("pyvisa.ResourceManager", return_value=manager):
            session = VisaSession("GPIB0::10::INSTR", 3.0)
            VisaSession("ASRL1::INSTR", 1.0, read_termination="\r", baud_rate=19200).close()

        probe.close.assert_called_once_with()
        active.close.assert_not_called()
        manager.close.assert_not_called()
        self.assertEqual(probe.read_termination, "\r")
        self.assertEqual(probe.baud_rate, 19200)
        self.assertEqual(session.handle.timeout, 3000)

    def test_long_query_restores_io_timeout_after_failure(self) -> None:
        manager = Mock()
        handle = manager.open_resource.return_value
        with patch("pyvisa.ResourceManager", return_value=manager):
            session = VisaSession("GPIB0::10::INSTR", 3.0)

        def fail(command: str) -> str:
            self.assertEqual(command, "*OPC?")
            self.assertEqual(handle.timeout, 12000)
            raise OSError("query timed out")

        handle.query.side_effect = fail
        with self.assertRaises(OSError):
            session.query("*OPC?", 12.0)
        self.assertEqual(handle.timeout, 3000)
