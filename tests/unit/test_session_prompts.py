"""`make prompt S=<id>` prints the common header and one session's prompt, and every line
range a prompt pins in the frozen `legacy/` snapshot covers whole sections.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "docs" / "SESSION_PROMPTS.md"
LEGACY = ROOT / "legacy"

#: A session heading, e.g. "## Session 4 — …" or "## Session 4a — …".
SESSION_HEADING = re.compile(r"^## Session (?P<id>\d+[a-z]?) ", re.MULTILINE)
#: A pinned range list in a prompt, e.g. "legacy/lite/config.yaml:37-117, 134-144".
PINNED = re.compile(r"(?P<path>legacy/[\w./-]+\.(?:md|ya?ml)):(?P<ranges>\d+-\d+(?:,\s*\d+-\d+)*)")
#: Where a section starts: a Markdown heading or a top-level YAML key.
SECTION_START = re.compile(r"^(#{1,6} |[A-Za-z_][\w-]*:)")

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def _prompt(session: str | None) -> subprocess.CompletedProcess[str]:
    args = ["make", "-s", "--no-print-directory", "prompt"]
    if session is not None:
        args.append(f"S={session}")
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def _session_ids() -> list[str]:
    return SESSION_HEADING.findall(PROMPTS.read_text(encoding="utf-8"))


def test_a_session_prints_the_common_header_and_its_own_section_only():
    result = _prompt("4")
    assert result.returncode == 0, result.stderr
    headings = _headings(result.stdout)
    assert len(headings) == 2
    assert headings[0].startswith("## Common header")
    assert headings[1].startswith("## Session 4 ")
    assert "## Session 4a" not in result.stdout
    assert "\n---\n" not in result.stdout


def test_a_lettered_session_is_not_its_numbered_parent():
    result = _prompt("4a")
    assert result.returncode == 0, result.stderr
    assert [h.split(" —")[0] for h in _headings(result.stdout)] == [
        "## Common header (inside every prompt)",
        "## Session 4a",
    ]


def test_session_one_is_not_sessions_ten_to_nineteen():
    result = _prompt("1")
    assert result.returncode == 0, result.stderr
    assert not any(
        h.startswith("## Session 1") and not h.startswith("## Session 1 ")
        for h in _headings(result.stdout)
    )


@pytest.mark.parametrize("session", _session_ids())
def test_every_session_extracts_with_exactly_one_goal(session: str):
    result = _prompt(session)
    assert result.returncode == 0, result.stderr
    assert sum(line.startswith("GOAL:") for line in result.stdout.splitlines()) == 1


def test_an_unknown_session_fails():
    result = _prompt("99")
    assert result.returncode != 0
    assert "no Session 99" in result.stderr


def test_a_missing_session_id_fails_with_the_usage():
    result = _prompt(None)
    assert result.returncode != 0
    assert "usage: make prompt S=" in result.stderr


def _pinned_ranges() -> list[tuple[str, int, int]]:
    text = PROMPTS.read_text(encoding="utf-8")
    pins: list[tuple[str, int, int]] = []
    for match in PINNED.finditer(text):
        for span in re.split(r",\s*", match["ranges"]):
            start, end = (int(n) for n in span.split("-"))
            pins.append((match["path"], start, end))
    return pins


def test_the_prompts_pin_legacy_ranges():
    assert _pinned_ranges(), "no pinned legacy range found; the pattern or the prompts changed"


@pytest.mark.skipif(not LEGACY.is_dir(), reason="legacy/ is deleted in Session 20")
@pytest.mark.parametrize(("path", "start", "end"), _pinned_ranges())
def test_a_pinned_legacy_range_covers_whole_sections(path: str, start: int, end: int):
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    assert 1 <= start <= end <= len(lines), f"{path}:{start}-{end} is outside the file"
    assert SECTION_START.match(lines[start - 1]), f"{path}:{start} does not open a section"
    after = end  # index of the first line past the range
    assert after == len(lines) or SECTION_START.match(lines[after]), f"{path}:{end} cuts a section"
