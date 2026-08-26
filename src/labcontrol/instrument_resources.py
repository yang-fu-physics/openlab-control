"""Read and write the Measurement Module VISA resource inventory."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_RESOURCE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class InstrumentResourceError(ValueError):
    """The VISA resource file is malformed or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class InstrumentResource:
    """One unassigned VISA resource available to Measurement Modules."""

    id: str
    address: str
    identity: str = ""

    def public_payload(self) -> dict[str, Any]:
        """Return the established Measurement Module payload shape."""

        return {
            "id": self.id,
            "address": self.address,
            "identity": self.identity,
            "purpose": "measurement",
        }


def _plain_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise InstrumentResourceError(f"{label} must be text")
    result = value.strip()
    if len(result) > maximum or any(
        not character.isprintable() for character in result
    ):
        raise InstrumentResourceError(
            f"{label} must be printable text with at most {maximum} characters"
        )
    return result


def validate_resources(
    resources: tuple[InstrumentResource, ...] | list[InstrumentResource],
) -> tuple[InstrumentResource, ...]:
    """Validate boundary fields and reject duplicate IDs or addresses."""

    result: list[InstrumentResource] = []
    ids: set[str] = set()
    addresses: dict[str, str] = {}
    for index, item in enumerate(resources, start=1):
        if not isinstance(item, InstrumentResource):
            raise InstrumentResourceError(
                f"resources[{index}] must be an InstrumentResource"
            )
        resource_id = _plain_text(
            item.id,
            f"resources[{index}].id",
            maximum=64,
        )
        if not _RESOURCE_ID.fullmatch(resource_id):
            raise InstrumentResourceError(
                f"resources[{index}].id must match [a-z][a-z0-9_]*"
            )
        if resource_id in ids:
            raise InstrumentResourceError(f"duplicate resource id: {resource_id}")
        ids.add(resource_id)
        address = _plain_text(
            item.address,
            f"resources[{index}].address",
            maximum=512,
        )
        if not address:
            raise InstrumentResourceError(
                f"resources[{index}].address must not be empty"
            )
        address_key = address.casefold()
        if address_key in addresses:
            raise InstrumentResourceError(
                f"address {address!r} is assigned to both "
                f"{addresses[address_key]} and {resource_id}"
            )
        addresses[address_key] = resource_id
        result.append(
            InstrumentResource(
                id=resource_id,
                address=address,
                identity=_plain_text(
                    item.identity,
                    f"resources[{index}].identity",
                    maximum=1024,
                ),
            )
        )
    return tuple(result)


def load_instrument_resources(path: str | Path) -> tuple[InstrumentResource, ...]:
    """Load ``configs/visa.resources.toml``; a missing file is an empty inventory."""

    source = Path(path)
    if not source.exists():
        return ()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InstrumentResourceError(
            f"Cannot read VISA resources {source}: {exc}"
        ) from exc
    unknown_top_level = sorted(set(raw) - {"resources"})
    if unknown_top_level:
        raise InstrumentResourceError(
            "Unknown VISA resource file fields: " + ", ".join(unknown_top_level)
        )
    entries = raw.get("resources", [])
    if not isinstance(entries, list):
        raise InstrumentResourceError("resources must be an array of tables")
    resources: list[InstrumentResource] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise InstrumentResourceError(
                f"resources[{index}] must be a TOML table"
            )
        unknown = sorted(set(entry) - {"id", "address", "identity"})
        if unknown:
            raise InstrumentResourceError(
                f"resources[{index}] has unknown fields: {', '.join(unknown)}"
            )
        if "id" not in entry or "address" not in entry:
            raise InstrumentResourceError(
                f"resources[{index}] requires id and address"
            )
        resources.append(
            InstrumentResource(
                id=entry["id"],
                address=entry["address"],
                identity=entry.get("identity", ""),
            )
        )
    return validate_resources(resources)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_instrument_resources(
    resources: tuple[InstrumentResource, ...] | list[InstrumentResource],
) -> str:
    """Render the complete standalone VISA resource file."""

    lines = [
        "# Managed by Instrument Scanner.",
        "# Unassigned VISA resources available to Measurement Modules.",
    ]
    for item in validate_resources(resources):
        lines.extend(
            [
                "",
                "[[resources]]",
                f"id = {_toml_string(item.id)}",
                f"address = {_toml_string(item.address)}",
                f"identity = {_toml_string(item.identity)}",
            ]
        )
    return "\n".join(lines) + "\n"


def write_instrument_resources(
    path: str | Path,
    resources: tuple[InstrumentResource, ...] | list[InstrumentResource],
) -> None:
    """Atomically replace the standalone VISA resource file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_instrument_resources(resources)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "InstrumentResource",
    "InstrumentResourceError",
    "load_instrument_resources",
    "render_instrument_resources",
    "validate_resources",
    "write_instrument_resources",
]
