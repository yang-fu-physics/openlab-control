"""把 System Instrument 清单指令绑定到站点中的逻辑仪表实例。"""

from __future__ import annotations

from .config import AppConfig
from .instruments.manifest import SystemInstrumentDescriptor
from .sequence.model import COMMAND_SPECS, SystemInstrumentCommandSpec


def configured_system_instrument_commands(
    config: AppConfig,
    descriptors: tuple[SystemInstrumentDescriptor, ...],
) -> tuple[SystemInstrumentCommandSpec, ...]:
    """返回已配置仪表的指令，并拒绝直接列表中无法区分的同名项。"""

    descriptors_by_id = {descriptor.id: descriptor for descriptor in descriptors}
    labels = {
        spec.label.casefold(): f"core command {spec.label!r}"
        for spec in COMMAND_SPECS
    }
    commands: list[SystemInstrumentCommandSpec] = []
    for instrument in config.instruments:
        if ":" in instrument.backend:
            continue
        descriptor = descriptors_by_id.get(instrument.backend)
        if descriptor is None:
            raise ValueError(
                f"Instrument {instrument.id} selects unknown System Instrument "
                f"{instrument.backend!r}"
            )
        for declared in descriptor.sequence_commands:
            folded_label = declared.label.casefold()
            owner = labels.get(folded_label)
            if owner is not None:
                raise ValueError(
                    f"System Instrument sequence command label {declared.label!r} "
                    f"from {instrument.id}.{declared.id} conflicts with {owner}"
                )
            labels[folded_label] = f"{instrument.id}.{declared.id}"
            commands.append(
                SystemInstrumentCommandSpec(
                    instrument.id,
                    declared.id,
                    declared.label,
                )
            )
    return tuple(commands)
