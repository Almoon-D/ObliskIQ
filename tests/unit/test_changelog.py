"""CHANGELOG.md has a section for the version in pyproject.toml, and the release script
reads it the way the release workflow will.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "release_notes.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release_notes = _load_script()

SAMPLE = """# Changelog

Intro text.

## [0.2.0] - 2026-11-01

### Added

- Second thing.

## [0.1.0] - 2026-10-08

### Added

- First thing.
"""


def _version() -> str:
    return release_notes.project_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_changelog_has_a_dated_non_empty_section_for_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = _version()
    heading = release_notes.section_heading(text, version)
    assert heading.group("date"), f"the {version} section heading carries no date"
    date.fromisoformat(heading.group("date"))
    assert release_notes.changelog_section(text, version).strip()


def test_the_changelog_follows_keep_a_changelog():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.startswith("# Changelog\n")
    assert "keepachangelog.com" in text
    assert "semver.org" in text


def test_a_section_ends_at_the_next_heading():
    assert release_notes.changelog_section(SAMPLE, "0.2.0") == "### Added\n\n- Second thing.\n"
    assert release_notes.changelog_section(SAMPLE, "0.1.0") == "### Added\n\n- First thing.\n"


def test_a_missing_section_fails_loudly():
    with pytest.raises(release_notes.ReleaseError, match=r"no section for 0\.3\.0"):
        release_notes.changelog_section(SAMPLE, "0.3.0")


def test_an_empty_section_fails_loudly():
    with pytest.raises(release_notes.ReleaseError, match=r"section for 0\.1\.0 is empty"):
        release_notes.changelog_section("## [0.1.0] - 2026-10-08\n\n## [0.0.1]\n- x\n", "0.1.0")


def test_a_duplicated_section_fails_loudly():
    with pytest.raises(release_notes.ReleaseError, match=r"2 sections for 0\.1\.0"):
        release_notes.changelog_section(SAMPLE + "\n## [0.1.0] - 2026-10-09\n- again\n", "0.1.0")


@pytest.mark.parametrize(
    "pyproject", ['[project]\nversion = "1.0"\n', '[project]\nname = "x"\n', "not toml ["]
)
def test_an_unusable_version_fails_loudly(pyproject):
    with pytest.raises(release_notes.ReleaseError):
        release_notes.project_version(pyproject)


@pytest.mark.parametrize(
    ("version", "expected"), [("0.1.0", True), ("0.99.3", True), ("1.0.0", False), ("2.1.0", False)]
)
def test_versions_below_one_are_prereleases(version, expected):
    assert release_notes.is_prerelease(version) is expected


def _repo(tmp_path: Path, version: str, changelog: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return tmp_path


def test_the_script_writes_notes_and_outputs(tmp_path, capsys):
    root = _repo(tmp_path, "0.2.0", SAMPLE)
    output = tmp_path / "github_output"
    status = release_notes.main(
        [
            "--root",
            str(root),
            "--notes-file",
            str(tmp_path / "n.md"),
            "--github-output",
            str(output),
        ]
    )
    assert status == 0
    assert (tmp_path / "n.md").read_text(encoding="utf-8") == "### Added\n\n- Second thing.\n"
    expected = "version=0.2.0\ntag=v0.2.0\nprerelease=true\n"
    assert output.read_text(encoding="utf-8") == expected
    assert capsys.readouterr().out == expected


def test_a_stable_release_is_not_a_prerelease(tmp_path, capsys):
    root = _repo(tmp_path, "1.0.0", "## [1.0.0] - 2027-01-01\n\n- Done.\n")
    assert release_notes.main(["--root", str(root), "--notes-file", str(tmp_path / "n.md")]) == 0
    assert "prerelease=false" in capsys.readouterr().out


def test_the_script_fails_without_a_section(tmp_path, capsys):
    root = _repo(tmp_path, "0.3.0", SAMPLE)
    notes = tmp_path / "n.md"
    assert release_notes.main(["--root", str(root), "--notes-file", str(notes)]) == 1
    assert "::error::CHANGELOG.md has no section for 0.3.0" in capsys.readouterr().err
    assert not notes.exists()


def test_the_script_fails_without_a_changelog(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    assert release_notes.main(["--root", str(tmp_path), "--notes-file", str(tmp_path / "n")]) == 1
    assert "::error::" in capsys.readouterr().err


def test_the_script_runs_against_this_repository(tmp_path):
    """Exactly what the release workflow does on main."""
    notes = tmp_path / "notes.md"
    assert release_notes.main(["--root", str(ROOT), "--notes-file", str(notes)]) == 0
    assert notes.read_text(encoding="utf-8").strip()
