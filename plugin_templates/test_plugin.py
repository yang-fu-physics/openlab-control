"""Starter test to copy into tests/test_my_plugin.py."""

from __future__ import annotations

import asyncio
import unittest


class MyPluginTests(unittest.TestCase):
    def test_connect_poll_disconnect(self) -> None:
        async def scenario() -> None:
            # Build a DeviceConfig with a fake transport, then verify:
            # 1. connect does not change unsafe device state;
            # 2. poll returns monotonic timestamp and configured units;
            # 3. disconnect is idempotent.
            pass

        asyncio.run(scenario())

    def test_protocol_error_mapping(self) -> None:
        # Verify timeout -> DeviceWarning or DeviceError according to the
        # laboratory policy, with stable code and context values.
        pass

    def test_hold(self) -> None:
        # Verify hold uses a fresh current readback and never commands zero
        # unless zero is already the current value.
        pass


if __name__ == "__main__":
    unittest.main()
