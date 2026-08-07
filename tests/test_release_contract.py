from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol import __version__  # noqa: E402
from labcontrol.extensions.dependencies import (  # noqa: E402
    FRAMEWORK_DEPENDENCY_VERSIONS,
)


class ReleaseContractTests(unittest.TestCase):
    def test_source_and_project_versions_match(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)
        self.assertEqual(project["project"]["version"], __version__)

        for relative_path in (
            "README.md",
            "docs/ARCHITECTURE.md",
            "docs/DAT_FORMAT.md",
        ):
            with self.subTest(path=relative_path):
                self.assertIn(
                    __version__,
                    (ROOT / relative_path).read_text(encoding="utf-8"),
                )

        specification = (ROOT / "OpenLabControl.spec").read_text(encoding="utf-8")
        self.assertIn("from labcontrol import __version__", specification)
        self.assertIn("Version(__version__)", specification)
        self.assertIn("parsed_version.pre", specification)
        self.assertIn("version=version_info", specification)

    def test_release_dependencies_are_exactly_locked(self) -> None:
        entries = [
            line.strip()
            for line in (ROOT / "requirements-lock.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(entries)
        self.assertTrue(all("==" in entry for entry in entries))

        names = {entry.split("==", 1)[0].casefold().replace("_", "-") for entry in entries}
        self.assertTrue(
            {
                "packaging",
                "pip",
                "pyinstaller",
                "pyvisa",
                "pyside6",
                "qtawesome",
                "setuptools",
                "typing-extensions",
            }.issubset(names)
        )

        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)
        declared = {
            canonicalize_name(requirement.name): next(
                iter(requirement.specifier)
            ).version
            for requirement in (
                Requirement(item)
                for item in project["project"][
                    "dependencies"
                ]
            )
        }
        self.assertEqual(
            declared,
            {
                name: str(version)
                for name, version
                in FRAMEWORK_DEPENDENCY_VERSIONS.items()
            },
        )

        setup_script = (ROOT / "setup.bat").read_text(encoding="utf-8")
        self.assertIn("-r requirements-lock.txt", setup_script)
        self.assertNotIn("pip install --upgrade pip", setup_script)

    def test_source_launcher_and_external_release_resources_do_not_overlap(self) -> None:
        source_launcher = (ROOT / "run.bat").read_text(encoding="utf-8")
        self.assertNotIn(r"dist\OpenLabControl", source_launcher)
        self.assertIn(r".venv\Scripts\pythonw.exe", source_launcher)

        specification = (ROOT / "OpenLabControl.spec").read_text(encoding="utf-8")
        self.assertIn(
            "datas=framework_metadata",
            specification,
        )
        self.assertIn("copy_metadata", specification)
        for distribution in (
            "PySide6",
            "QtAwesome",
            "packaging",
            "PyVISA",
            "typing_extensions",
        ):
            with self.subTest(
                framework_metadata=distribution
            ):
                self.assertIn(
                    f'"{distribution}"',
                    specification,
                )
        self.assertIn("collect_submodules(", specification)
        self.assertIn('"pyvisa"', specification)
        self.assertIn('"pyvisa.testsuite"', specification)
        build_script = (ROOT / "build.bat").read_text(encoding="utf-8")
        for name in (
            "configs",
            "examples",
            "docs",
            "plugin_templates",
            "integrations",
            "modules",
        ):
            self.assertIn(f'"{name}" "dist\\OpenLabControl\\{name}"', build_script)
        self.assertNotIn("module_runtime", build_script)
        for name in (
            "device_plugins",
            "plugin_runtime",
            "plugin_state",
        ):
            self.assertIn(
                f'dist\\OpenLabControl\\{name}',
                build_script,
            )
        self.assertIn(
            'call :remove_python_caches "dist\\OpenLabControl\\%%R"',
            build_script,
        )
        for generated_name in (
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "*.pyc",
            "*.pyo",
        ):
            with self.subTest(generated_name=generated_name):
                self.assertIn(generated_name, build_script)

    def test_current_docs_describe_shared_framework_and_isolated_extras(self) -> None:
        current_documents = "\n".join(
            (ROOT / relative_path).read_text(encoding="utf-8")
            for relative_path in (
                "README.md",
                "docs/ARCHITECTURE.md",
                "docs/CONFIGURATION.md",
                "docs/OPERATIONS.md",
                "docs/PLUGIN_DEVELOPMENT.md",
            )
        )
        for obsolete in (
            "site_packages_directory",
            "Online Install",
            "在线 pip",
            "module_runtime/site-packages",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, current_documents)
        for required in (
            "plugin_runtime",
            "--no-index",
            "requirements.lock",
            "fingerprint",
            "PyVISA",
            "框架共享",
            "额外依赖",
            "Device Plugin 示例",
            "measurement-modules-repository",
        ):
            with self.subTest(required=required):
                self.assertIn(required, current_documents)


if __name__ == "__main__":
    unittest.main()
