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
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "device_status_interval_seconds = 1.0",
                    "device_status_interval_seconds = 0.05",
                ),
                encoding="utf-8",
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
            time.sleep(0.35)
            devices = runtime.devices
            self.assertIsNotNone(devices)
            clients = tuple(devices.devices.values())
            self.assertIsNotNone(runtime.logger)
            self.assertIsNotNone(runtime.logger.paths)
            run_paths = runtime.logger.paths
            runtime.shutdown(timeout=6.0)

            self.assertIsNone(runtime._thread)
            self.assertIsNone(runtime._loop)
            self.assertTrue(
                all(
                    getattr(client, "pid", None) is None
                    for client in clients
                )
            )
            device_status = run_paths.device_status_file.read_text(
                encoding="utf-8"
            )
            status_rows = (
                device_status.split("[Data]\n", 1)[1]
                .strip()
                .splitlines()
            )
            self.assertGreaterEqual(len(status_rows), 3)


if __name__ == "__main__":
    unittest.main()
