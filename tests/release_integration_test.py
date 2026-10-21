#!/usr/bin/env python3
"""End-to-end release smoke tests with local Git repos and a fake gh binary.

These exercises genuinely commit, push, tag, and rerun the release shell script.
No GitHub access or token is used, and the real project files are never changed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str, cwd: Path, env: dict | None = None, check=True) -> str:
    result = subprocess.run(
        args, cwd=cwd, env=env, text=True, capture_output=True, check=False,
        timeout=40,
    )
    if check and result.returncode:
        raise AssertionError(
            f"{' '.join(args)} failed ({result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


class ReleasePublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="release-git-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        command("git", "init", "--bare", "--initial-branch=main", str(self.remote),
                cwd=self.root)
        self.work = self.root / "work"
        command("git", "clone", str(self.remote), str(self.work), cwd=self.root)
        command("git", "config", "user.name", "Release Tester", cwd=self.work)
        command("git", "config", "user.email", "release-tester@example.invalid",
                cwd=self.work)
        (self.work / "scripts").mkdir()
        (self.work / ".codex-plugin").mkdir()
        shutil.copy2(ROOT / "scripts/release_version.py",
                     self.work / "scripts/release_version.py")
        shutil.copy2(ROOT / "scripts/publish_release.sh",
                     self.work / "scripts/publish_release.sh")
        (self.work / "codex_auto_startstop.py").write_text('VERSION = "1.0.0"\n')
        (self.work / ".codex-plugin/plugin.json").write_text(
            '{\n  "version": "1.0.0",\n  "name": "test"\n}\n'
        )
        (self.work / "README.md").write_text("initial\n")
        command("git", "add", ".", cwd=self.work)
        command("git", "commit", "-m", "Initial release", cwd=self.work)
        command("git", "tag", "-a", "v1.0.0", "-m", "Release v1.0.0", cwd=self.work)
        # Simulate a maintainer bumping versions in an ordinary PR.
        (self.work / "codex_auto_startstop.py").write_text('VERSION = "1.0.1"\n')
        (self.work / ".codex-plugin/plugin.json").write_text(
            '{\n  "version": "1.0.1",\n  "name": "test"\n}\n'
        )
        command("git", "add", ".", cwd=self.work)
        command("git", "commit", "-m", "fix: safer hooks", cwd=self.work)
        command("git", "push", "origin", "main", "--tags", cwd=self.work)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        mock = self.bin / "gh"
        mock.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'from pathlib import Path\n'
            'p = Path(os.environ["FAKE_RELEASES"])\n'
            'args = sys.argv[1:]\n'
            'released = json.loads(p.read_text()) if p.exists() else []\n'
            'if args[:2] == ["release", "view"]:\n'
            '    sys.exit(0 if args[2] in released else 1)\n'
            'if args[:2] == ["release", "create"]:\n'
            '    if args[2] in released:\n'
            '        sys.exit("duplicate release")\n'
            '    if os.environ.get("FAKE_FAIL_CREATE") == "1":\n'
            '        sys.exit("simulated GitHub API outage")\n'
            '    released.append(args[2])\n'
            '    p.write_text(json.dumps(released))\n'
            '    sys.exit(0)\n'
            'sys.exit("unsupported mock-gh request: " + repr(args))\n'
        )
        mock.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.env["GH_TOKEN"] = "offline-test-token"
        self.env["FAKE_RELEASES"] = str(self.root / "releases.json")

    def sha(self) -> str:
        return command("git", "rev-parse", "HEAD", cwd=self.work)

    def publish(self, sha: str, fail: bool = False):
        env = dict(self.env)
        env["GITHUB_SHA"] = sha
        if fail:
            env["FAKE_FAIL_CREATE"] = "1"
        return subprocess.run(
            ["bash", "scripts/publish_release.sh"],
            cwd=self.work, env=env, capture_output=True, text=True,
            timeout=40,
        )

    def releases(self) -> list[str]:
        path = self.root / "releases.json"
        return json.loads(path.read_text()) if path.exists() else []

    def test_initial_release_and_next_auto_patch_are_idempotent(self):
        sha = self.sha()
        first = self.publish(sha)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.releases(), ["v1.0.1"])
        self.assertEqual(command("git", "rev-list", "-n", "1", "v1.0.1",
                                 cwd=self.work), sha)
        again = self.publish(sha)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.releases(), ["v1.0.1"])

        # A later main push with no explicit version bump gets an auto patch.
        (self.work / "README.md").write_text("more docs\n")
        command("git", "add", "README.md", cwd=self.work)
        command("git", "commit", "-m", "docs: clarify hooks", cwd=self.work)
        command("git", "push", "origin", "main", cwd=self.work)
        user_sha = self.sha()
        second = self.publish(user_sha)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.releases(), ["v1.0.1", "v1.0.2"])
        new_head = self.sha()
        self.assertNotEqual(new_head, user_sha)
        self.assertEqual(
            command("git", "rev-parse", "origin/main", cwd=self.work),
            new_head,
        )
        self.assertEqual(
            command("git", "rev-list", "-n", "1", "v1.0.2", cwd=self.work),
            new_head,
        )
        self.assertEqual(
            command("python3", "scripts/release_version.py", "current",
                    cwd=self.work), "1.0.2"
        )
        rerun = self.publish(user_sha)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(self.releases(), ["v1.0.1", "v1.0.2"])

    def test_recovers_after_release_api_failure_without_new_tag(self):
        sha = self.sha()
        first = self.publish(sha, fail=True)
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self.releases(), [])
        self.assertEqual(
            command("git", "rev-list", "-n", "1", "v1.0.1",
                    cwd=self.work), sha
        )
        retry = self.publish(sha)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.releases(), ["v1.0.1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
