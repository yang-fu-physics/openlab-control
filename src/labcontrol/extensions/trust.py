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
    pass


class TrustSubject(Protocol):
    id: str
    version: str
    fingerprint: str


def _is_reparse_point(path: Path) -> bool:
    try:
        stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ExtensionTrustError(f"Cannot inspect extension path {path}: {exc}") from exc
    attributes = int(getattr(stat, "st_file_attributes", 0))
    reparse_flag = int(getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def extension_files(root: Path) -> tuple[Path, ...]:
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
    extension_type: str
    extension_id: str
    version: str
    fingerprint: str
    trusted_at: str


class PluginTrustStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._records = self._load()

    @staticmethod
    def _key(extension_type: str, extension_id: str) -> str:
        return f"{extension_type.strip().casefold()}:{extension_id.strip()}"

    def _load(self) -> dict[str, TrustRecord]:
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
        record = self._records.get(self._key(extension_type, subject.id))
        return (
            record is not None
            and record.version == subject.version
            and record.fingerprint == subject.fingerprint
        )

    def trust(self, extension_type: str, subject: TrustSubject) -> None:
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
        self._records.pop(self._key(extension_type, extension_id), None)
        self._save()

    def _save(self) -> None:
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
