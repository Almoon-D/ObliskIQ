"""The release a commit on ``main`` publishes: version, tag, pre-release flag and notes.

The version is ``pyproject.toml``'s ``project.version`` (plain semantic versioning); the
notes are the body of that version's ``## [x.y.z] - YYYY-MM-DD`` section of
``CHANGELOG.md``; versions below 1.0.0 are pre-releases. A version without a non-empty
section is an error, never an empty release. Standard library only, so the release
workflow runs it with the runner's own Python; ``tests/unit/test_changelog.py`` runs the
same functions against the repository on every change.

Usage::

    python3 .github/scripts/release_notes.py --notes-file NOTES.md [--github-output FILE]

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

SEMVER_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SECTION_RE = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$")
FIRST_STABLE_MAJOR = 1


class ReleaseError(Exception):
    """The repository does not describe a publishable release."""


def project_version(pyproject_text: str) -> str:
    try:
        version = tomllib.loads(pyproject_text)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleaseError(f"pyproject.toml has no readable project.version: {exc}") from None
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        raise ReleaseError(f"project.version {version!r} is not MAJOR.MINOR.PATCH")
    return version


def section_heading(changelog_text: str, version: str) -> re.Match[str]:
    """The heading line of ``version``'s section."""
    matches = [
        match
        for line in changelog_text.splitlines()
        if (match := SECTION_RE.match(line)) and match.group("version") == version
    ]
    if not matches:
        raise ReleaseError(f"CHANGELOG.md has no section for {version}")
    if len(matches) > 1:
        raise ReleaseError(f"CHANGELOG.md has {len(matches)} sections for {version}")
    return matches[0]


def changelog_section(changelog_text: str, version: str) -> str:
    """The body of ``version``'s section, without its heading."""
    heading = section_heading(changelog_text, version).group(0)
    lines = changelog_text.splitlines()
    start = lines.index(heading) + 1
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    notes = "\n".join(body).strip()
    if not notes:
        raise ReleaseError(f"CHANGELOG.md's section for {version} is empty")
    return notes + "\n"


def is_prerelease(version: str) -> bool:
    return int(version.split(".")[0]) < FIRST_STABLE_MAJOR


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", type=Path, default=Path())
    parser.add_argument("--notes-file", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        version = project_version((args.root / "pyproject.toml").read_text(encoding="utf-8"))
        notes = changelog_section((args.root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    except (ReleaseError, OSError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    args.notes_file.write_text(notes, encoding="utf-8")
    outputs = {
        "version": version,
        "tag": f"v{version}",
        "prerelease": "true" if is_prerelease(version) else "false",
    }
    lines = "".join(f"{key}={value}\n" for key, value in outputs.items())
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(lines)
    sys.stdout.write(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
