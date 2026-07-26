from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import load_config  # noqa: E402
from labcontrol.runtime import RuntimeService  # noqa: E402
from labcontrol.sequence.model import (  # noqa: E402
    Command,
    CommandType,
    SequenceDocument,
)


class RuntimeShutdownTests(unittest.TestCase):
    def test_shutdown_stops_active_sequence_thread_and_device_workers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "configs").mkdir()
            config_path = root / "configs" / "default.toml"
            shutil.copy2(
                ROOT / "configs" / "default.toml",
                config_path,
            )
            runtime = RuntimeService(
                load_config(config_path),
                module_descriptors=(),
            )
            runtime.start(timeout=5.0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if any(
                    message.kind == "snapshots"
                    for message in runtime.drain_messages()
                ):
                    break
                time.sleep(0.02)
            else:
                self.fail("Runtime did not publish initial snapshots")

            runtime.run_sequence(
                SequenceDocument(
                    [
                        Command(
                            CommandType.WAIT,
                            {"seconds": 10.0},
                        )
                    ],
                    "shutdown.seq",
                )
            )
            time.sleep(0.05)
            devices = runtime.devices
            self.assertIsNotNone(devices)
            clients = tuple(devices.devices.values())
            runtime.shutdown(timeout=6.0)

            self.assertIsNone(runtime._thread)
            self.assertIsNone(runtime._loop)
            self.assertTrue(
                all(
                    getattr(client, "pid", None) is None
                    for client in clients
                )
            )


if __name__ == "__main__":
    unittest.main()
