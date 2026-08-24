from __future__ import annotations

import asyncio
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.config import load_config  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instruments.manifest import (  # noqa: E402
    InstrumentSequenceCommandDescriptor,
    SystemInstrumentDescriptor,
)
from labcontrol.models import InstrumentKind, RunState  # noqa: E402
from labcontrol.sequence.engine import SequenceEngine  # noqa: E402
from labcontrol.sequence.model import (  # noqa: E402
    CommandType,
    SequenceDocument,
)
from labcontrol.sequence.parser import (  # noqa: E402
    parse_sequence,
    serialize_sequence,
)
from labcontrol.system_instrument_commands import (  # noqa: E402
    configured_system_instrument_commands,
)
from labcontrol.ui.main_window import MainWindow  # noqa: E402


def _descriptor(
    instrument_id: str,
    command_id: str,
    label: str,
) -> SystemInstrumentDescriptor:
    return SystemInstrumentDescriptor(
        id=instrument_id,
        name=instrument_id,
        version="1.0.0",
        path=ROOT,
        panel_template="controller",
        backend="backend:Instrument",
        kinds=(InstrumentKind.TEMPERATURE,),
        sequence_commands=(
            InstrumentSequenceCommandDescriptor(
                command_id,
                label,
            ),
        ),
    )


class SystemInstrumentSequenceCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])
        cls.config = load_config(ROOT / "configs" / "default.toml")

    def test_configured_command_round_trips_as_direct_seq_text(self) -> None:
        descriptor = _descriptor(
            "compressor",
            "compressor_on",
            "Compressor On",
        )
        instrument = replace(
            self.config.instruments[0],
            backend="compressor",
        )
        config = replace(self.config, instruments=(instrument,))
        specs = configured_system_instrument_commands(
            config,
            (descriptor,),
        )

        result = parse_sequence(
            "T Compressor On\nT End Sequence\n",
            instrument_commands=specs,
        )
        self.assertFalse(result.has_errors)
        command = result.document.commands[0]
        self.assertIs(command.type, CommandType.INSTRUMENT_COMMAND)
        self.assertEqual(command.instrument_id, instrument.id)
        self.assertEqual(command.instrument_command_id, "compressor_on")
        self.assertEqual(
            serialize_sequence(result.document),
            "T Compressor On\nT End Sequence\n",
        )

        unavailable = parse_sequence(
            "T Compressor On\nT End Sequence\n"
        )
        self.assertIs(
            unavailable.document.commands[0].type,
            CommandType.UNKNOWN,
        )
        self.assertEqual(unavailable.issues[0].level, "warning")

    def test_direct_labels_must_not_conflict(self) -> None:
        first = _descriptor("first", "on", "Compressor On")
        second = _descriptor("second", "start", "compressor on")
        instruments = (
            replace(self.config.instruments[0], backend="first"),
            replace(self.config.instruments[1], backend="second"),
        )
        with self.assertRaisesRegex(ValueError, "conflicts with"):
            configured_system_instrument_commands(
                replace(self.config, instruments=instruments),
                (first, second),
            )

        wait = _descriptor("first", "wait", "Wait")
        with self.assertRaisesRegex(ValueError, "core command"):
            configured_system_instrument_commands(
                replace(
                    self.config,
                    instruments=(
                        replace(
                            self.config.instruments[0],
                            backend="first",
                        ),
                    ),
                ),
                (wait,),
            )

    def test_command_is_a_direct_system_commands_child_and_inserts_without_dialog(
        self,
    ) -> None:
        descriptor = _descriptor(
            "compressor",
            "compressor_on",
            "Compressor On",
        )
        specs = configured_system_instrument_commands(
            replace(
                self.config,
                instruments=(
                    replace(
                        self.config.instruments[0],
                        backend="compressor",
                    ),
                ),
            ),
            (descriptor,),
        )
        runtime = Mock()
        runtime.instrument_sequence_commands = specs
        runtime.drain_messages.return_value = []
        with patch(
            "labcontrol.ui.main_window.RuntimeService",
            return_value=runtime,
        ):
            window = MainWindow(self.config)
        try:
            system_group = window.command_tree.findItems(
                "System Commands",
                Qt.MatchFlag.MatchExactly,
            )[0]
            matches = [
                system_group.child(index)
                for index in range(system_group.childCount())
                if system_group.child(index).text(0) == "Compressor On"
            ]
            self.assertEqual(len(matches), 1)
            window._insert_palette_command(matches[0], 0)
            inserted = window.document.commands[-1]
            self.assertIs(
                inserted.type,
                CommandType.INSTRUMENT_COMMAND,
            )
            self.assertEqual(
                inserted.instrument_command_id,
                "compressor_on",
            )
        finally:
            window.close()

    def test_engine_dispatches_action_without_writing_a_data_row(self) -> None:
        descriptor = _descriptor(
            "compressor",
            "compressor_on",
            "Compressor On",
        )
        specs = configured_system_instrument_commands(
            replace(
                self.config,
                instruments=(
                    replace(
                        self.config.instruments[0],
                        backend="compressor",
                    ),
                ),
            ),
            (descriptor,),
        )

        class Instruments:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def execute_sequence_command(
                self,
                instrument_id: str,
                command_id: str,
            ) -> bool:
                self.calls.append((instrument_id, command_id))
                return True

        instruments = Instruments()
        logger = Mock()
        engine = SequenceEngine(
            self.config,
            instruments,  # type: ignore[arg-type]
            EventManager(),
            logger,
            Mock(),  # type: ignore[arg-type]
            instrument_sequence_commands=specs,
        )
        engine.state = RunState.RUNNING
        engine._checkpoint = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(
            engine._execute_command(
                specs[0].create(),
                ["Compressor On"],
            )
        )
        self.assertEqual(
            instruments.calls,
            [("temperature", "compressor_on")],
        )
        self.assertEqual(logger.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
