#!/usr/bin/env python3
"""Dependency-free regression tests for automatic release versioning."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_version.py"
spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
release_version = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(release_version)


class ReleaseVersionTests(unittest.TestCase):
    def test_manifest_and_script_versions_agree(self):
        version = release_version.ensure_versions_match()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        metadata = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(version, metadata["version"])

    def test_initial_version_is_used_when_newer_than_tags(self):
        self.assertEqual(
            release_version.choose_version("1.0.1", ["v1.0.0"]), "1.0.1"
        )

    def test_patch_increments_when_version_was_already_published(self):
        self.assertEqual(
            release_version.choose_version("1.0.1", ["v1.0.0", "v1.0.1"]),
            "1.0.2",
        )

    def test_finds_highest_numeric_semver_not_lexical(self):
        self.assertEqual(
            release_version.choose_version(
                "1.0.1", ["v1.0.9", "v1.0.10", "v1.0.3"]
            ),
            "1.0.11",
        )

    def test_honours_explicit_minor_or_major_bump(self):
        self.assertEqual(
            release_version.choose_version("1.2.0", ["v1.0.99"]), "1.2.0"
        )
        self.assertEqual(
            release_version.choose_version("2.0.0", ["v1.9.99"]), "2.0.0"
        )

    def test_nonsemver_tags_are_ignored(self):
        self.assertEqual(
            release_version.choose_version(
                "1.0.1", ["v1.0.0", "v1.1.0-rc1", "docs", "vnotaversion"]
            ),
            "1.0.1",
        )

    def test_no_prior_tags(self):
        self.assertEqual(release_version.choose_version("1.0.1", []), "1.0.1")

    def test_older_declared_version_does_not_reuse_existing_tag(self):
        self.assertEqual(
            release_version.choose_version("0.1.0", ["v1.2.3"]), "1.2.4"
        )

    def test_both_version_fields_are_updated_without_changing_other_data(self):
        with tempfile.TemporaryDirectory() as td:
            python_file = Path(td) / "plugin.py"
            plugin_file = Path(td) / "plugin.json"
            python_file.write_text('VERSION = "1.0.1"\nother = "unchanged"\n')
            plugin_file.write_text(
                '{\n  "name": "test",\n  "version": "1.0.1",\n  "hooks": "here"\n}\n'
            )
            release_version.replace_version(
                python_file, release_version.PYTHON_VERSION, "1.0.2"
            )
            release_version.replace_version(
                plugin_file, release_version.PLUGIN_VERSION, "1.0.2"
            )
            self.assertIn('VERSION = "1.0.2"', python_file.read_text())
            self.assertIn('other = "unchanged"', python_file.read_text())
            self.assertEqual(
                json.loads(plugin_file.read_text())["version"], "1.0.2"
            )
            self.assertIn('"hooks": "here"', plugin_file.read_text())

    def test_invalid_versions_fail(self):
        for version in ("1.2", "1.2.3-rc1", "not a version", ""):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    release_version.parse_version(version)

    def test_shell_release_script_has_valid_syntax(self):
        subprocess.run(
            ["bash", "-n", str(ROOT / "scripts" / "publish_release.sh")],
            check=True,
        )

    def test_version_check_command_passes(self):
        subprocess.run(
            [sys.executable, str(SCRIPT), "check"],
            check=True, capture_output=True, text=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
