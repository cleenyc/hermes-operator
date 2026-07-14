from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import re
import sys
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import hermes_operator  # noqa: E402


class ReleaseConsistencyTests(unittest.TestCase):
    def test_versions_and_policy_pin_match_release_sources(self) -> None:
        core_project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        plugin_project = tomllib.loads(
            (ROOT / "integrations/hermes_operator_plugin/pyproject.toml").read_text(
                encoding="utf-8"
            )
        )["project"]
        plugin_source = (
            ROOT / "integrations/hermes_operator_plugin/__init__.py"
        ).read_text(encoding="utf-8")
        policy_path = ROOT / "integrations/hermes_operator_plugin/policy.py"
        policy_source = policy_path.read_text(encoding="utf-8")
        policy_digest = hashlib.sha256(policy_path.read_bytes()).hexdigest()

        self.assertEqual(core_project["version"], hermes_operator.__version__)
        plugin_version = re.search(
            r'^PLUGIN_VERSION = "([^"]+)"$', plugin_source, re.MULTILINE
        )
        policy_version = re.search(
            r'^POLICY_VERSION = "([^"]+)"$', policy_source, re.MULTILINE
        )
        assert plugin_version is not None and policy_version is not None
        self.assertEqual(plugin_project["version"], plugin_version.group(1))

        release_inputs = [
            ROOT / "src/hermes_operator/cli.py",
            ROOT / "config/operator.example.toml",
            ROOT / "README.md",
            ROOT / "docs/CONFIGURATION.md",
            ROOT / "docs/DEPLOYMENT.md",
        ]
        for path in release_inputs:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn(plugin_version.group(1), text)
                self.assertIn(policy_version.group(1), text)
                self.assertIn(policy_digest, text)

    def test_complete_release_builder_covers_operational_artifacts(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "operator_release_builder", ROOT / "scripts/build_release.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module.VERSION, hermes_operator.__version__)
        required = {
            "README.md",
            "LICENSE",
            "Makefile",
            "pyproject.toml",
            "Dockerfile",
            "compose.yaml",
            "config/operator.example.toml",
            "config/outbound.example.toml",
            "deploy/hermes-operator.service",
            "docs/API.md",
            "docs/CONFIGURATION.md",
            "docs/NATIVE_AUTOMATION.md",
            "docs/REMINDERS.md",
            "integrations/hermes_operator_plugin/plugin.yaml",
            "integrations/hermes_operator_plugin/skills/operator-workflow/SKILL.md",
            "scripts/build_release.py",
            "src/hermes_operator/supervisor.py",
            "src/hermes_operator/verifier.py",
            "tests/test_release.py",
        }
        bundled = {
            path.relative_to(ROOT).as_posix() for path in module._files()
        }
        self.assertTrue(required <= bundled, sorted(required - bundled))


if __name__ == "__main__":
    unittest.main()
