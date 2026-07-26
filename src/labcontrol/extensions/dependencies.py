"""插件额外依赖的离线验证与隔离安装。

主框架锁定并提供 PySide6、PyVISA、packaging 等共同依赖。扩展的 ``requirements.lock`` 只
允许声明额外包，并从本地 wheel 目录安装到“扩展 ID + 内容指纹”对应的私有 site-packages。
安装过程不访问网络，完成后写入运行时标记并再次核对实际版本。
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .trust import ExtensionTrustError, extension_tree_digest


_HASH = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})(?:\s|$)")

# 这些是扩展可以直接使用、并由 OpenLab Control 统一锁定和随正式包携带的依赖。
# 模块/设备插件即使在 manifest 中声明兼容范围，也不会为它们再创建一份隔离副本。
# 版本必须与 pyproject.toml、requirements.txt 和 requirements-lock.txt 保持一致。
FRAMEWORK_DEPENDENCY_VERSIONS: Mapping[str, Version] = (
    MappingProxyType(
        {
            canonicalize_name("PySide6"): Version("6.11.1"),
            canonicalize_name("QtAwesome"): Version("1.4.2"),
            canonicalize_name("packaging"): Version("26.2"),
            canonicalize_name("PyVISA"): Version("1.16.2"),
            canonicalize_name("typing_extensions"): Version(
                "4.16.0"
            ),
        }
    )
)


class DependencyInstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OfflineInstallResult:
    target: Path
    stdout: str
    stderr: str


def partition_extension_dependencies(
    dependencies: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """把扩展依赖分成核心已提供和仍需隔离安装的两组。

    返回 ``(framework, extra, errors)``。核心依赖使用全局锁定版本，所有模块和设备
    插件看到的是同一份包；只有 ``extra`` 才要求扩展携带 requirements.lock/wheels，
    并在 Install Dependencies 后加入对应 worker 的 sys.path。

    manifest 可以继续声明 PyVISA 等核心包的兼容范围，便于表达最低要求。若其范围不
    接受核心锁定版本，扩展会在导入源码前被判为不兼容，不能用私有副本覆盖核心版本。
    """

    framework: list[str] = []
    extra: list[str] = []
    errors: list[str] = []
    for raw_requirement in dependencies:
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement:
            # 具体的语法错误仍由 manifest 校验输出；先归到 extra，保证不会误判成
            # 核心已提供并绕过 requirements.lock 检查。
            extra.append(raw_requirement)
            continue
        if (
            requirement.marker is not None
            and not requirement.marker.evaluate()
        ):
            framework.append(raw_requirement)
            continue
        provided = FRAMEWORK_DEPENDENCY_VERSIONS.get(
            canonicalize_name(requirement.name)
        )
        if provided is None or requirement.url is not None:
            extra.append(raw_requirement)
            continue
        if (
            requirement.specifier
            and provided not in requirement.specifier
        ):
            errors.append(
                f"{requirement.name} {requirement.specifier} is "
                "incompatible with framework-provided version "
                f"{provided}"
            )
            continue
        framework.append(raw_requirement)
    return (
        tuple(framework),
        tuple(extra),
        tuple(dict.fromkeys(errors)),
    )


def _logical_lock_lines(path: Path) -> tuple[str, ...]:
    try:
        physical = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DependencyInstallError(
            f"Cannot read offline dependency lock {path}: {exc}"
        ) from exc
    logical: list[str] = []
    pending = ""
    for raw_line in physical:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continuation = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continuation else stripped
        pending = f"{pending} {fragment}".strip()
        if not continuation:
            logical.append(pending)
            pending = ""
    if pending:
        raise DependencyInstallError(
            f"{path.name} ends with an unfinished line continuation"
        )
    return tuple(logical)


def validate_requirements_lock(
    extension_directory: Path,
    dependencies: Iterable[str],
) -> tuple[str, ...]:
    declared = tuple(dependencies)
    if not declared:
        return ()
    path = extension_directory / "requirements.lock"
    if not path.is_file():
        return (
            "dependencies require requirements.lock with exact versions "
            "and SHA-256 hashes",
        )
    try:
        lines = _logical_lock_lines(path)
    except DependencyInstallError as exc:
        return (str(exc),)
    errors: list[str] = []
    locked: dict[str, Version] = {}
    for line in lines:
        if line.startswith("-"):
            errors.append(
                "requirements.lock may contain only pinned requirements, "
                "not pip options"
            )
            continue
        requirement_text = line.split(" --hash=", 1)[0].strip()
        hashes = _HASH.findall(line)
        unsupported = _HASH.sub(
            "",
            line[len(requirement_text):],
        ).strip()
        if unsupported:
            errors.append(
                "requirements.lock contains unsupported tokens after "
                f"{requirement_text}: {unsupported}"
            )
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement:
            errors.append(
                f"invalid locked requirement: {requirement_text}"
            )
            continue
        if requirement.url is not None:
            errors.append(
                f"dependency URLs are not allowed: {requirement_text}"
            )
        specifiers = tuple(requirement.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            errors.append(
                f"locked dependency must use one exact == version: "
                f"{requirement_text}"
            )
            continue
        if not hashes:
            errors.append(
                f"locked dependency has no SHA-256 hash: "
                f"{requirement_text}"
            )
        try:
            version = Version(specifiers[0].version)
        except InvalidVersion:
            errors.append(
                f"locked dependency has an invalid version: "
                f"{requirement_text}"
            )
            continue
        name = canonicalize_name(requirement.name)
        if name in locked:
            errors.append(
                f"requirements.lock repeats dependency {requirement.name}"
            )
        else:
            locked[name] = version
    if not lines:
        errors.append("requirements.lock is empty")
    for raw_requirement in declared:
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement:
            errors.append(f"invalid declared dependency: {raw_requirement}")
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        version = locked.get(canonicalize_name(requirement.name))
        if version is None:
            errors.append(
                f"requirements.lock does not pin {requirement.name}"
            )
        elif requirement.specifier and version not in requirement.specifier:
            errors.append(
                f"locked {requirement.name} {version} does not satisfy "
                f"{requirement.specifier}"
            )
    return tuple(dict.fromkeys(errors))


def installed_dependency_versions(
    site_packages: Path,
) -> dict[str, str]:
    if not site_packages.is_dir():
        return {}
    installed: dict[str, str] = {}
    try:
        distributions = importlib.metadata.distributions(
            path=[str(site_packages)]
        )
        for distribution in distributions:
            name = distribution.metadata.get("Name")
            if name:
                installed[canonicalize_name(name)] = distribution.version
    except (OSError, ValueError):
        return {}
    return installed


def missing_dependencies(
    dependencies: Iterable[str],
    site_packages: Path,
) -> tuple[str, ...]:
    installed = installed_dependency_versions(site_packages)
    missing: list[str] = []
    for raw_requirement in dependencies:
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement:
            missing.append(raw_requirement)
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        version_text = installed.get(
            canonicalize_name(requirement.name)
        )
        if version_text is None:
            missing.append(raw_requirement)
            continue
        try:
            version = Version(version_text)
        except InvalidVersion:
            missing.append(raw_requirement)
            continue
        if requirement.specifier and version not in requirement.specifier:
            missing.append(raw_requirement)
    return tuple(missing)


def dependency_runtime_errors(
    dependencies: Iterable[str],
    site_packages: Path,
    fingerprint: str,
) -> tuple[str, ...]:
    declared = tuple(dependencies)
    if not declared:
        return ()
    errors: list[str] = []
    missing = missing_dependencies(declared, site_packages)
    if missing:
        errors.append(
            "missing dependencies: " + ", ".join(missing)
        )
    marker_path = site_packages.parent / "runtime.json"
    if not marker_path.is_file():
        errors.append(
            "extra dependency runtime is not installed"
        )
        return tuple(dict.fromkeys(errors))
    try:
        marker = json.loads(
            marker_path.read_text(encoding="utf-8")
        )
        if int(marker.get("schema_version", 0)) != 1:
            raise ValueError("unsupported schema version")
        if str(marker.get("fingerprint", "")) != fingerprint:
            raise ValueError(
                "extension fingerprint does not match"
            )
        recorded = str(marker["runtime_digest"])
        if not re.fullmatch(r"[0-9a-f]{64}", recorded):
            raise ValueError("invalid runtime digest")
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        errors.append(
            f"invalid isolated runtime marker: {exc}"
        )
        return tuple(dict.fromkeys(errors))
    try:
        current = extension_tree_digest(site_packages)
    except ExtensionTrustError as exc:
        errors.append(str(exc))
    else:
        if current != recorded:
            errors.append(
                "isolated dependency runtime content changed"
            )
    return tuple(dict.fromkeys(errors))


def install_offline_dependencies(
    *,
    python_executable: Path,
    extension_directory: Path,
    site_packages: Path,
    shared_wheels_directory: Path,
    dependencies: Iterable[str],
    fingerprint: str,
    timeout_seconds: float = 600.0,
) -> OfflineInstallResult:
    declared = tuple(dependencies)
    errors = validate_requirements_lock(
        extension_directory,
        declared,
    )
    if errors:
        raise DependencyInstallError("; ".join(errors))
    runtime_errors = dependency_runtime_errors(
        declared,
        site_packages,
        fingerprint,
    )
    if not runtime_errors:
        return OfflineInstallResult(site_packages, "", "")
    if not python_executable.is_file():
        raise DependencyInstallError(
            f"Python runtime does not exist: {python_executable}"
        )
    wheel_directories = tuple(
        path
        for path in (
            extension_directory / "wheels",
            shared_wheels_directory,
        )
        if path.is_dir()
    )
    if not wheel_directories:
        raise DependencyInstallError(
            "No offline wheel directory is available"
        )
    runtime_root = site_packages.parent
    runtime_parent = runtime_root.parent
    runtime_parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{runtime_root.name}.staging.",
            dir=runtime_parent,
        )
    )
    staging_site = staging_root / "site-packages"
    staging_site.mkdir()
    command = [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--only-binary=:all:",
        "--require-hashes",
        "--disable-pip-version-check",
        "--target",
        str(staging_site),
    ]
    for directory in wheel_directories:
        command.extend(["--find-links", str(directory)])
    command.extend(
        ["-r", str(extension_directory / "requirements.lock")]
    )
    creationflags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt"
        else 0
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            raise DependencyInstallError(
                "Offline pip install failed:\n"
                + (result.stderr or result.stdout)[-4000:]
            )
        unresolved = missing_dependencies(
            declared,
            staging_site,
        )
        if unresolved:
            raise DependencyInstallError(
                "Offline install completed but requirements remain "
                "unresolved: "
                + ", ".join(unresolved)
            )
        marker = {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "requirements": list(declared),
            "runtime_digest": extension_tree_digest(
                staging_site
            ),
        }
        (staging_root / "runtime.json").write_text(
            json.dumps(
                marker,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        backup: Path | None = None
        if runtime_root.exists():
            backup = Path(
                tempfile.mkdtemp(
                    prefix=f".{runtime_root.name}.old.",
                    dir=runtime_parent,
                )
            )
            backup.rmdir()
            runtime_root.replace(backup)
        try:
            staging_root.replace(runtime_root)
        except Exception:
            if backup is not None and not runtime_root.exists():
                backup.replace(runtime_root)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        return OfflineInstallResult(
            site_packages,
            result.stdout,
            result.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        raise DependencyInstallError(
            "Offline dependency installation timed out after "
            f"{timeout_seconds:g} seconds"
        ) from exc
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
