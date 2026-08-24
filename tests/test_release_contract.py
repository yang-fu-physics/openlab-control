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
from labcontrol.package_support.dependencies import (  # noqa: E402
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
        self.assertEqual(source_launcher.count("run.py %*"), 2)
        source_ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/configs/*.local.toml", source_ignore)

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
        self.assertEqual(specification.count("Analysis("), 2)
        self.assertEqual(specification.count("COLLECT("), 1)
        self.assertIn('["tools/instrument_scanner.py"]', specification)
        self.assertIn('name="InstrumentScanner"', specification)
        self.assertEqual(specification.count("exclude_binaries=True"), 2)
        self.assertIn("scanner_exe,\n    a.binaries", specification)
        self.assertIn("scanner_analysis.binaries", specification)
        self.assertIn("scanner_analysis.datas", specification)
        build_script = (ROOT / "build.bat").read_text(encoding="utf-8")
        self.assertIn(
            r"dist\OpenLabControl\InstrumentScanner.exe",
            build_script,
        )
        self.assertNotIn(r"tools\InstrumentScanner.exe", build_script)
        self.assertNotIn('xcopy /E /I /Y "tools"', build_script)
        self.assertIn("stage_windows_release.ps1", build_script)
        self.assertIn("if defined CI exit /b 0", build_script)

        staging_script = (
            ROOT / "tools" / "stage_windows_release.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('"InstrumentScanner.exe"', staging_script)
        self.assertIn('"OpenLabControl.exe"', staging_script)
        self.assertIn("$releaseToolsPath", staging_script)
        for name in (
            "configs",
            "examples",
            "docs",
            "templates",
            "integrations",
            "modules",
        ):
            self.assertIn(f'"{name}"', staging_script)
        self.assertNotIn("module_runtime", staging_script)
        for name in (
            "system_instruments",
            "runtime_packages",
            "trust_state",
        ):
            self.assertIn(f'"{name}"', staging_script)
        for generated_name in (
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".pyc",
            ".pyo",
        ):
            with self.subTest(generated_name=generated_name):
                self.assertIn(generated_name, staging_script)

    def test_github_release_workflow_builds_and_verifies_windows_assets(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        for required in (
            "windows-latest",
            "requirements-lock.txt",
            "is_prerelease",
            "unittest discover -s tests",
            "mkdocs build --strict",
            "OpenLabControl.spec",
            "stage_windows_release.ps1",
            "InstrumentScanner.exe",
            "OpenLabControl.exe",
            "Start-Process",
            "WaitForExit",
            "$process.ExitCode",
            "$process.Kill($true)",
            "Get-FileHash",
            "headless_demo.log",
            "forbiddenNames",
            "gh release create",
        ):
            with self.subTest(required=required):
                self.assertIn(required, workflow)
        self.assertIn(
            '-Arguments @("--headless-demo", "--timeout", "120")',
            workflow,
        )
        self.assertIn("-TimeoutMilliseconds 180000", workflow)
        self.assertNotIn("tools/InstrumentScanner.exe", workflow)

    def test_current_docs_describe_shared_framework_and_isolated_extras(self) -> None:
        current_documents = "\n".join(
            (ROOT / relative_path).read_text(encoding="utf-8")
            for relative_path in (
                "README.md",
                "docs/ARCHITECTURE.md",
                "docs/CONFIGURATION.md",
                "docs/OPERATIONS.md",
                "docs/DEVELOPMENT_REFERENCE.md",
            )
        )
        for obsolete in (
            "site_packages_directory",
            "Online Install",
            "在线 pip",
            "module_runtime/site-packages",
            "device_plugins",
            "plugin_runtime",
            "plugin_state",
            "[plugins]",
            "labcontrol.devices",
            "api.devices()",
            "device.toml",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, current_documents)
        for required in (
            "runtime_packages",
            "--no-index",
            "requirements.lock",
            "fingerprint",
            "PyVISA",
            "框架共享",
            "额外依赖",
            "measurement-modules-repository",
            "system-instruments-repository",
            "instrument.toml",
            "[[instruments]]",
            "backend",
        ):
            with self.subTest(required=required):
                self.assertIn(required, current_documents)


if __name__ == "__main__":
    unittest.main()
