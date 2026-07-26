"""第三方 Device Plugin 与 Measurement Module 共用的内容信任存储。

用户信任的是“类型 + ID + 版本 + 整棵目录的 SHA-256 指纹”，而不是一个长期有效的目录名。
插件任意文件发生变化后，旧信任自动失效。扫描拒绝符号链接和 Windows junction/reparse
point，防止被信任目录在校验后跳转到外部内容；信任文件采用落盘刷新后的原子替换。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


TRUST_SCHEMA_VERSION = 1
_IGNORED_PARTS = frozenset(
    {".git", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"}
)


class ExtensionTrustError(RuntimeError):
    """扩展内容无法安全扫描，或信任状态文件无法可靠读写。"""


class TrustSubject(Protocol):
    """设备/模块清单参与信任判断所需的最小字段集合。"""

    id: str
    version: str
    fingerprint: str


def _is_reparse_point(path: Path) -> bool:
    """同时识别跨平台符号链接与 Windows reparse point/junction。"""

    try:
        stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ExtensionTrustError(f"Cannot inspect extension path {path}: {exc}") from exc
    attributes = int(getattr(stat, "st_file_attributes", 0))
    reparse_flag = int(getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def extension_files(root: Path) -> tuple[Path, ...]:
    """返回参与指纹计算的稳定文件列表，并拒绝目录中的任何链接。"""

    resolved = root.resolve()
    if not resolved.is_dir():
        raise ExtensionTrustError(f"Extension directory does not exist: {root}")
    if _is_reparse_point(root):
        raise ExtensionTrustError(f"Extension directory must not be a link or junction: {root}")
    files: list[Path] = []
    for path in sorted(resolved.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(resolved)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if _is_reparse_point(path):
            raise ExtensionTrustError(
                f"Extension content must not contain links or junctions: {relative}"
            )
        if path.is_file():
            files.append(path)
    return tuple(files)


def extension_tree_digest(root: Path) -> str:
    """计算包含相对路径、文件大小和全部字节内容的目录 SHA-256。"""

    resolved = root.resolve()
    digest = hashlib.sha256()
    for path in extension_files(resolved):
        relative = path.relative_to(resolved).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class TrustRecord:
    """一次用户确认的扩展版本与内容指纹记录。"""

    extension_type: str
    extension_id: str
    version: str
    fingerprint: str
    trusted_at: str


class PluginTrustStore:
    """读取、判断并原子更新本地插件信任记录。"""

    def __init__(self, path: Path) -> None:
        """加载指定信任文件；损坏文件会报错而不是默认为全部可信。"""

        self.path = path.resolve()
        self._records = self._load()

    @staticmethod
    def _key(extension_type: str, extension_id: str) -> str:
        """构造带扩展类型的键，避免同名 Device 与 Module 相互授权。"""

        return f"{extension_type.strip().casefold()}:{extension_id.strip()}"

    def _load(self) -> dict[str, TrustRecord]:
        """严格解析信任文件的 schema、键和值。"""

        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if int(raw.get("schema_version", 0)) != TRUST_SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            records: dict[str, TrustRecord] = {}
            for key, value in dict(raw.get("plugins", {})).items():
                record = TrustRecord(
                    extension_type=str(value["extension_type"]),
                    extension_id=str(value["extension_id"]),
                    version=str(value["version"]),
                    fingerprint=str(value["fingerprint"]),
                    trusted_at=str(value["trusted_at"]),
                )
                expected = self._key(record.extension_type, record.extension_id)
                if key != expected:
                    raise ValueError(f"invalid trust record key {key!r}")
                if len(record.fingerprint) != 64:
                    raise ValueError(f"invalid fingerprint for {key}")
                records[key] = record
            return records
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ExtensionTrustError(
                f"Cannot read plugin trust store {self.path}: {exc}"
            ) from exc

    def is_trusted(self, extension_type: str, subject: TrustSubject) -> bool:
        """仅当版本和当前目录指纹都完全一致时返回真。"""

        record = self._records.get(self._key(extension_type, subject.id))
        return (
            record is not None
            and record.version == subject.version
            and record.fingerprint == subject.fingerprint
        )

    def trust(self, extension_type: str, subject: TrustSubject) -> None:
        """记录用户刚确认的扩展内容，并立即持久化。"""

        record = TrustRecord(
            extension_type=extension_type.strip().casefold(),
            extension_id=subject.id,
            version=subject.version,
            fingerprint=subject.fingerprint,
            trusted_at=datetime.now(timezone.utc).isoformat(),
        )
        self._records[self._key(extension_type, subject.id)] = record
        self._save()

    def revoke(self, extension_type: str, extension_id: str) -> None:
        """撤销一个扩展的信任；不存在也保持幂等。"""

        self._records.pop(self._key(extension_type, extension_id), None)
        self._save()

    def _save(self) -> None:
        """写入临时文件、刷新到磁盘后原子替换正式信任文件。"""

        payload = {
            "schema_version": TRUST_SCHEMA_VERSION,
            "plugins": {
                key: {
                    "extension_type": record.extension_type,
                    "extension_id": record.extension_id,
                    "version": record.version,
                    "fingerprint": record.fingerprint,
                    "trusted_at": record.trusted_at,
                }
                for key, record in sorted(self._records.items())
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise ExtensionTrustError(
                f"Cannot write plugin trust store {self.path}: {exc}"
            ) from exc
