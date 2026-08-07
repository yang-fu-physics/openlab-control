from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.extensions.dependencies import (  # noqa: E402
    FRAMEWORK_DEPENDENCY_VERSIONS,
    dependency_runtime_errors,
    install_offline_dependencies,
    missing_dependencies,
    partition_extension_dependencies,
    validate_requirements_lock,
)
from labcontrol.extensions.trust import extension_tree_digest  # noqa: E402
from labcontrol.measurement.manifest import ModuleDescriptor  # noqa: E402
from labcontrol.measurement.worker import ModuleWorkerClient  # noqa: E402


def _write_distribution(
    site_packages: Path,
    name: str,
    version: str,
) -> None:
    normalized = name.replace("-", "_")
    package = site_packages / normalized
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f"VERSION = {version!r}\n",
        encoding="utf-8",
    )
    metadata = (
        site_packages
        / f"{normalized}-{version}.dist-info"
    )
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: {version}\n",
        encoding="utf-8",
    )


def _write_wheel(
    wheels: Path,
    name: str,
    version: str,
) -> Path:
    normalized = name.replace("-", "_")
    wheel = (
        wheels
        / f"{normalized}-{version}-py3-none-any.whl"
    )
    dist_info = f"{normalized}-{version}.dist-info"
    with zipfile.ZipFile(
        wheel,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(
            f"{normalized}/__init__.py",
            f"VERSION = {version!r}\n",
        )
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\n"
            f"Name: {name}\n"
            f"Version: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: OpenLab Control test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            "",
        )
    return wheel


class ExtensionDependencyTests(unittest.TestCase):
    def test_framework_dependencies_are_shared_and_version_checked(
        self,
    ) -> None:
        framework, extra, errors = (
            partition_extension_dependencies(
                (
                    "PyVISA>=1.16,<1.17",
                    "typing_extensions>=4.16,<5",
                    "module-only-demo==2.0.0",
                )
            )
        )
        self.assertEqual(
            framework,
            (
                "PyVISA>=1.16,<1.17",
                "typing_extensions>=4.16,<5",
            ),
        )
        self.assertEqual(
            extra,
            ("module-only-demo==2.0.0",),
        )
        self.assertEqual(errors, ())
        self.assertEqual(
            str(FRAMEWORK_DEPENDENCY_VERSIONS["pyvisa"]),
            "1.16.2",
        )

        framework, extra, errors = (
            partition_extension_dependencies(
                ("PyVISA>=2",)
            )
        )
        self.assertEqual(framework, ())
        self.assertEqual(extra, ())
        self.assertTrue(
            any(
                "framework-provided version 1.16.2"
                in error
                for error in errors
            ),
            errors,
        )

    def test_missing_runtime_is_reported_as_not_installed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            errors = dependency_runtime_errors(
                ("module-only-demo==1.0.0",),
                Path(temporary)
                / "runtime"
                / "site-packages",
                "a" * 64,
            )
        self.assertIn(
            "extra dependency runtime is not installed",
            errors,
        )
        self.assertFalse(
            any(
                "invalid isolated runtime marker"
                in error
                for error in errors
            )
        )

    def test_lock_requires_exact_hashed_non_url_requirements(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dependencies = ("demo-package>=1,<2",)
            self.assertTrue(
                validate_requirements_lock(root, dependencies)
            )
            (root / "requirements.lock").write_text(
                "demo-package>=1 --hash=sha256:"
                + "0" * 64
                + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "exact == version" in error
                    for error in validate_requirements_lock(
                        root,
                        dependencies,
                    )
                )
            )
            (root / "requirements.lock").write_text(
                "demo-package==1.5.0 --hash=sha256:"
                + "0" * 64
                + " --index-url https://example.invalid\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "unsupported tokens" in error
                    for error in validate_requirements_lock(
                        root,
                        dependencies,
                    )
                )
            )
            (root / "requirements.lock").write_text(
                "demo-package==1.5.0 --hash=sha256:"
                + "0" * 64
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                validate_requirements_lock(
                    root,
                    dependencies,
                ),
                (),
            )

    def test_dependency_detection_reads_only_selected_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            _write_distribution(first, "isolated-demo", "1.0.0")
            _write_distribution(second, "isolated-demo", "2.0.0")
            self.assertEqual(
                missing_dependencies(
                    ("isolated-demo<2",),
                    first,
                ),
                (),
            )
            self.assertEqual(
                missing_dependencies(
                    ("isolated-demo>=2",),
                    second,
                ),
                (),
            )
            self.assertEqual(
                missing_dependencies(
                    ("isolated-demo>=2",),
                    first,
                ),
                ("isolated-demo>=2",),
            )

    def test_offline_installer_uses_hashed_local_wheel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extension = root / "extension"
            wheels = extension / "wheels"
            wheels.mkdir(parents=True)
            wheel = _write_wheel(
                wheels,
                "offline-demo",
                "1.0.0",
            )
            digest = hashlib.sha256(
                wheel.read_bytes()
            ).hexdigest()
            (extension / "requirements.lock").write_text(
                "offline-demo==1.0.0 --hash=sha256:"
                + digest
                + "\n",
                encoding="utf-8",
            )
            target = (
                root
                / "runtime"
                / "fingerprint"
                / "site-packages"
            )
            result = install_offline_dependencies(
                python_executable=Path(sys.executable),
                extension_directory=extension,
                site_packages=target,
                shared_wheels_directory=root / "shared-wheels",
                dependencies=("offline-demo==1.0.0",),
                fingerprint="a" * 64,
                timeout_seconds=30.0,
            )
            self.assertEqual(result.target, target)
            self.assertEqual(
                missing_dependencies(
                    ("offline-demo==1.0.0",),
                    target,
                ),
                (),
            )
            self.assertTrue(
                (target.parent / "runtime.json").is_file()
            )
            (
                target / "offline_demo" / "__init__.py"
            ).write_text(
                "VERSION = 'tampered'\n",
                encoding="utf-8",
            )
            self.assertIn(
                "isolated dependency runtime content changed",
                dependency_runtime_errors(
                    ("offline-demo==1.0.0",),
                    target,
                    "a" * 64,
                ),
            )

    def test_two_module_workers_import_different_dependency_versions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clients: list[ModuleWorkerClient] = []
            try:
                for module_id, version in (
                    ("first", "1.0.0"),
                    ("second", "2.0.0"),
                ):
                    module_root = root / module_id
                    module_root.mkdir()
                    (module_root / "backend.py").write_text(
                        "from isolated_demo import VERSION\n"
                        "class Module:\n"
                        "    columns = {'Value': ''}\n"
                        "    def open(self, api):\n"
                        "        return {'version': VERSION}\n"
                        "    def measure(self, slot, api):\n"
                        "        return {'Value': 0}\n"
                        "    def close(self, api):\n"
                        "        return {}\n",
                        encoding="utf-8",
                    )
                    dependency_root = (
                        root / "runtime" / module_id
                    )
                    _write_distribution(
                        dependency_root,
                        "isolated-demo",
                        version,
                    )
                    descriptor = ModuleDescriptor(
                        id=module_id,
                        name=module_id,
                        version="1.0.0",
                        path=module_root,
                        fingerprint=extension_tree_digest(
                            module_root
                        ),
                    )
                    client = ModuleWorkerClient(
                        descriptor,
                        dependency_root,
                    )
                    client.start(timeout_seconds=2.0)
                    clients.append(client)
                    self.assertEqual(
                        client.request(
                            "open",
                            timeout_seconds=2.0,
                        )["version"],
                        version,
                    )
                self.assertNotIn("isolated_demo", sys.modules)
            finally:
                for client in clients:
                    client.close(timeout_seconds=0.5)


if __name__ == "__main__":
    unittest.main()
