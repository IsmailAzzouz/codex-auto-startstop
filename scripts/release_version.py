#!/usr/bin/env python3
"""Keep Codex script/plugin versions in sync when preparing GitHub releases.

The first release uses the committed version when it is newer than all existing
SemVer tags. Subsequent releases increment the latest tag's patch number unless
a maintainer has already committed a higher version (for example 1.2.0).
Only the version fields in the two project files are modified; no network,
third-party package or GitHub credential is needed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
PYTHON_FILE = ROOT / "codex_auto_startstop.py"
PLUGIN_FILE = ROOT / ".codex-plugin" / "plugin.json"
VERSION_PATTERN = r"\d+\.\d+\.\d+"
PYTHON_VERSION = re.compile(rf'(?m)^VERSION = "({VERSION_PATTERN})"$')
PLUGIN_VERSION = re.compile(rf'(?m)^  "version": "({VERSION_PATTERN})",$')
TAG_VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse_version(version: str) -> tuple[int, int, int]:
    if not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError(f"Invalid version: {version!r} (expected X.Y.Z)")
    return tuple(map(int, version.split(".")))  # type: ignore[return-value]


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def extract_version(text: str, pattern: re.Pattern, filename: str) -> str:
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ValueError(f"{filename} must contain exactly one X.Y.Z version")
    return matches[0]


def read_versions() -> tuple[str, str]:
    python = extract_version(
        PYTHON_FILE.read_text(encoding="utf-8"), PYTHON_VERSION, PYTHON_FILE.name
    )
    plugin = extract_version(
        PLUGIN_FILE.read_text(encoding="utf-8"), PLUGIN_VERSION, PLUGIN_FILE.name
    )
    return python, plugin


def ensure_versions_match() -> str:
    python, plugin = read_versions()
    if python != plugin:
        raise ValueError(
            f"Version mismatch: codex_auto_startstop.py={python}, "
            f".codex-plugin/plugin.json={plugin}"
        )
    return python


def choose_version(current: str, tags: Iterable[str]) -> str:
    """Use a manually increased version, otherwise bump latest tagged patch."""
    available = []
    for tag in tags:
        match = TAG_VERSION.fullmatch(tag.strip())
        if match:
            available.append(tuple(map(int, match.groups())))
    declared = parse_version(current)
    if not available or declared > max(available):
        return current
    latest = max(available)
    return format_version((latest[0], latest[1], latest[2] + 1))


def get_tags() -> list[str]:
    output = subprocess.check_output(
        ["git", "tag", "--list", "v*"], cwd=ROOT, text=True
    )
    return output.splitlines()


def replace_version(path: Path, pattern: re.Pattern, version: str) -> None:
    old = path.read_text(encoding="utf-8")
    matches = list(pattern.finditer(old))
    if len(matches) != 1:
        raise ValueError(f"Missing or duplicate version in {path}")
    match = matches[0]
    new = old[: match.start(1)] + version + old[match.end(1) :]
    if new != old:
        path.write_text(new, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "current", "next", "prepare"])
    args = parser.parse_args()

    current = ensure_versions_match()
    if args.command in ("check", "current"):
        print(current)
        return 0

    upcoming = choose_version(current, get_tags())
    if args.command == "prepare":
        replace_version(PYTHON_FILE, PYTHON_VERSION, upcoming)
        replace_version(PLUGIN_FILE, PLUGIN_VERSION, upcoming)
        assert ensure_versions_match() == upcoming
    print(upcoming)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"release-version: {exc}") from exc
