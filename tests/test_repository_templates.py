from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.devices.manifest import load_device_manifest  # noqa: E402
from labcontrol.measurement.manifest import load_manifest  # noqa: E402


TEMPLATES = ROOT / "plugin_templates"
MODULE_REPOSITORY = TEMPLATES / "measurement-modules-repository"
DEVICE_REPOSITORY = TEMPLATES / "device-plugins-repository"


class RepositoryTemplateTests(unittest.TestCase):
    def test_core_starts_without_an_active_measurement_module(self) -> None:
        manifests = tuple((ROOT / "modules").glob("*/module.toml"))
        self.assertEqual(manifests, ())

    def test_measurement_repository_contains_a_valid_reference_module(self) -> None:
        module_path = (
            MODULE_REPOSITORY
            / "modules"
            / "simulated_transport"
        )
        descriptor = load_manifest(module_path)
        self.assertTrue(descriptor.valid, descriptor.error)
        self.assertEqual(descriptor.id, "simulated_transport")
        self.assertEqual(descriptor.columns, ())
        with (module_path / "module.toml").open("rb") as handle:
            self.assertEqual(set(tomllib.load(handle)), {"name", "version"})

    def test_device_repository_plugins_are_independently_installable(self) -> None:
        paths = sorted(
            path
            for path in (DEVICE_REPOSITORY / "plugins").iterdir()
            if path.is_dir()
        )
        self.assertEqual(
            [path.name for path in paths],
            ["example_controller", "example_monitor"],
        )
        descriptors = [
            load_device_manifest(path)
            for path in paths
        ]
        self.assertTrue(
            all(descriptor.valid for descriptor in descriptors),
            [descriptor.error for descriptor in descriptors],
        )
        self.assertEqual(
            {descriptor.id for descriptor in descriptors},
            {"example_controller", "example_monitor"},
        )

    def test_templates_contain_no_generated_runtime_or_secret_files(self) -> None:
        forbidden_names = {
            "runtime.json",
            "trusted_plugins.json",
            "settings.toml",
            ".env",
        }
        observed = {
            path.name
            for repository in (MODULE_REPOSITORY, DEVICE_REPOSITORY)
            for path in repository.rglob("*")
            if path.is_file()
        }
        self.assertTrue(forbidden_names.isdisjoint(observed))


if __name__ == "__main__":
    unittest.main()
