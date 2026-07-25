from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol import __version__  # noqa: E402


class ReleaseContractTests(unittest.TestCase):
    def test_source_and_project_versions_match(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)
        self.assertEqual(project["project"]["version"], __version__)

        for relative_path in (
            "README.md",
            "docs/ARCHITECTURE.md",
            "docs/DAT_FORMAT.md",
            "docs/TECHNICAL_SPECIFICATION.md",
        ):
            with self.subTest(path=relative_path):
                self.assertIn(
                    __version__,
                    (ROOT / relative_path).read_text(encoding="utf-8"),
                )

        specification = (ROOT / "OpenLabControl.spec").read_text(encoding="utf-8")
        self.assertIn("from labcontrol import __version__", specification)
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
                "pyside6",
                "qtawesome",
                "setuptools",
            }.issubset(names)
        )

        setup_script = (ROOT / "setup.bat").read_text(encoding="utf-8")
        self.assertIn("-r requirements-lock.txt", setup_script)
        self.assertNotIn("pip install --upgrade pip", setup_script)

    def test_source_launcher_and_external_release_resources_do_not_overlap(self) -> None:
        source_launcher = (ROOT / "run.bat").read_text(encoding="utf-8")
        self.assertNotIn(r"dist\OpenLabControl", source_launcher)
        self.assertIn(r".venv\Scripts\pythonw.exe", source_launcher)

        specification = (ROOT / "OpenLabControl.spec").read_text(encoding="utf-8")
        self.assertIn("datas=[]", specification)
        build_script = (ROOT / "build.bat").read_text(encoding="utf-8")
        for name in ("configs", "examples", "docs", "plugin_templates", "modules"):
            self.assertIn(f'"{name}" "dist\\OpenLabControl\\{name}"', build_script)


if __name__ == "__main__":
    unittest.main()
