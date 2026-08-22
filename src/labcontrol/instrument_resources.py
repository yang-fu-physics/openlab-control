"""读取和写入现场物理仪表资源表。

资源表记录物理地址和用途；System 资源还记录扫描时确认的 System Instrument 与需要显示的
辅助读数。主读数和读数元数据来自 ``instrument.toml``，控制权限与安全上下限来自现场主配置。
资源表不保存输出状态、目标值或安全限制。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RESOURCE_FILE_VERSION = 2
_RESOURCE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_READING_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_PURPOSES = frozenset({"system", "measurement"})


class InstrumentResourceError(ValueError):
    """仪表资源表缺字段、类型错误或包含相互冲突的地址。"""


@dataclass(frozen=True, slots=True)
class InstrumentResource:
    """一台物理仪表的稳定名称、通讯地址及人工确认结果。

    ``system_instrument`` 只在 ``purpose="system"`` 时使用，值为 System Instrument 的目录/清单
    ID。主读数由 System Instrument 清单定义；``auxiliary_readings`` 只记录操作者选择显示的
    附加读数。System Instrument 仍须在连接时验证这些通道真实存在，不能只相信扫描结果。
    """

    id: str
    address: str
    identity: str = ""
    purpose: str = "measurement"
    system_instrument: str = ""
    auxiliary_readings: tuple[str, ...] = ()

    def public_payload(self) -> dict[str, Any]:
        """返回可安全传给 Measurement Module 前后端的只读 JSON 视图。"""

        return {
            "id": self.id,
            "address": self.address,
            "identity": self.identity,
            "purpose": self.purpose,
            "system_instrument": self.system_instrument,
            "auxiliary_readings": list(self.auxiliary_readings),
        }


def _plain_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise InstrumentResourceError(f"{label} must be text")
    result = value.strip()
    if len(result) > maximum or any(not character.isprintable() for character in result):
        raise InstrumentResourceError(
            f"{label} must be printable text with at most {maximum} characters"
        )
    return result


def _reading_id(value: object, label: str, *, allow_empty: bool = False) -> str:
    result = _plain_text(value, label, maximum=64)
    if not result and allow_empty:
        return ""
    if not _READING_ID.fullmatch(result):
        raise InstrumentResourceError(
            f"{label} must match [a-z][a-z0-9_]*"
        )
    return result


def validate_resources(
    resources: tuple[InstrumentResource, ...] | list[InstrumentResource],
) -> tuple[InstrumentResource, ...]:
    """严格验证并返回不可变资源序列。"""

    result: list[InstrumentResource] = []
    ids: set[str] = set()
    addresses: dict[str, str] = {}
    for index, item in enumerate(resources, start=1):
        if not isinstance(item, InstrumentResource):
            raise InstrumentResourceError(
                f"resources[{index}] must be an InstrumentResource"
            )
        resource_id = _plain_text(item.id, f"resources[{index}].id", maximum=64)
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

        identity = _plain_text(
            item.identity,
            f"resources[{index}].identity",
            maximum=1024,
        )
        purpose = _plain_text(
            item.purpose,
            f"resources[{index}].purpose",
            maximum=32,
        ).casefold()
        if purpose not in _PURPOSES:
            raise InstrumentResourceError(
                f"resources[{index}].purpose must be system or measurement"
            )
        system_instrument = _reading_id(
            item.system_instrument,
            f"resources[{index}].system_instrument",
            allow_empty=True,
        )
        auxiliary = tuple(
            _reading_id(
                value,
                f"resources[{index}].auxiliary_readings",
            )
            for value in item.auxiliary_readings
        )
        if len(auxiliary) != len(set(auxiliary)):
            raise InstrumentResourceError(
                f"resources[{index}].auxiliary_readings contains duplicates"
            )
        if purpose == "system" and not system_instrument:
            raise InstrumentResourceError(
                f"resources[{index}].system_instrument is required for a system resource"
            )
        if purpose == "measurement" and auxiliary:
            raise InstrumentResourceError(
                f"resources[{index}] measurement resources cannot declare system readings"
            )
        if purpose == "measurement" and system_instrument:
            raise InstrumentResourceError(
                f"resources[{index}] measurement resources cannot select a System Instrument"
            )
        result.append(
            InstrumentResource(
                id=resource_id,
                address=address,
                identity=identity,
                purpose=purpose,
                system_instrument=system_instrument,
                auxiliary_readings=auxiliary,
            )
        )
    return tuple(result)


def load_instrument_resources(path: str | Path) -> tuple[InstrumentResource, ...]:
    """读取资源表；文件尚未创建时返回空表。"""

    source = Path(path)
    if not source.exists():
        return ()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InstrumentResourceError(
            f"Cannot read instrument resource file {source}: {exc}"
        ) from exc
    if set(raw) - {"schema_version", "resources"}:
        unknown = ", ".join(sorted(set(raw) - {"schema_version", "resources"}))
        raise InstrumentResourceError(
            f"Unknown instrument resource fields: {unknown}"
        )
    version = raw.get("schema_version")
    if isinstance(version, bool) or version != RESOURCE_FILE_VERSION:
        raise InstrumentResourceError(
            f"schema_version must be {RESOURCE_FILE_VERSION}"
        )
    entries = raw.get("resources", [])
    if not isinstance(entries, list):
        raise InstrumentResourceError("resources must be an array of tables")
    resources: list[InstrumentResource] = []
    allowed = {
        "id",
        "address",
        "identity",
        "purpose",
        "system_instrument",
        "auxiliary_readings",
    }
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise InstrumentResourceError(
                f"resources[{index}] must be a TOML table"
            )
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise InstrumentResourceError(
                f"resources[{index}] has unknown fields: {', '.join(unknown)}"
            )
        if "id" not in entry or "address" not in entry:
            raise InstrumentResourceError(
                f"resources[{index}] requires id and address"
            )
        raw_auxiliary = entry.get("auxiliary_readings", [])
        if not isinstance(raw_auxiliary, list) or any(
            not isinstance(value, str) for value in raw_auxiliary
        ):
            raise InstrumentResourceError(
                f"resources[{index}].auxiliary_readings must be an array of strings"
            )
        resources.append(
            InstrumentResource(
                id=entry["id"],
                address=entry["address"],
                identity=entry.get("identity", ""),
                purpose=entry.get("purpose", "measurement"),
                system_instrument=entry.get("system_instrument", ""),
                auxiliary_readings=tuple(raw_auxiliary),
            )
        )
    return validate_resources(resources)


def _toml_string(value: str) -> str:
    """JSON 字符串也是合法 TOML basic string，且能可靠转义控制字符。"""

    return json.dumps(value, ensure_ascii=False)


def render_instrument_resources(
    resources: tuple[InstrumentResource, ...] | list[InstrumentResource],
) -> str:
    """生成稳定、便于人工检查的 TOML 文本。"""

    checked = validate_resources(resources)
    lines = [
        "# Generated by tools/instrument_scanner.py after manual confirmation.",
        "# This file contains addresses; keep it local and do not commit it.",
        f"schema_version = {RESOURCE_FILE_VERSION}",
    ]
    for item in checked:
        lines.extend(
            [
                "",
                "[[resources]]",
                f"id = {_toml_string(item.id)}",
                f"address = {_toml_string(item.address)}",
                f"identity = {_toml_string(item.identity)}",
                f"purpose = {_toml_string(item.purpose)}",
                f"system_instrument = {_toml_string(item.system_instrument)}",
                "auxiliary_readings = ["
                + ", ".join(_toml_string(value) for value in item.auxiliary_readings)
                + "]",
            ]
        )
    return "\n".join(lines) + "\n"


def write_instrument_resources(
    path: str | Path,
    resources: tuple[InstrumentResource, ...] | list[InstrumentResource],
) -> None:
    """同目录临时写入后原子替换，避免中途退出留下半个配置文件。"""

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
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "InstrumentResource",
    "InstrumentResourceError",
    "RESOURCE_FILE_VERSION",
    "load_instrument_resources",
    "render_instrument_resources",
    "validate_resources",
    "write_instrument_resources",
]
