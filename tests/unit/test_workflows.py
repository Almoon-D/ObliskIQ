"""CI, release and Dependabot configuration say what the plan requires.

``on:`` parses as the key ``True``: PyYAML implements YAML 1.1, where a bare ``on`` is a
boolean. GitHub reads the files as YAML 1.2, where it stays a string.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
GITHUB = ROOT / ".github"


def _load(relative: str) -> dict[Any, Any]:
    data = yaml.safe_load((GITHUB / relative).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _steps(workflow: dict[Any, Any], job: str) -> list[dict[str, Any]]:
    steps = workflow["jobs"][job]["steps"]
    assert isinstance(steps, list)
    return steps


# ── CI ──


def test_ci_has_one_job_named_ci_that_runs_make_check():
    workflow = _load("workflows/ci.yml")
    assert list(workflow["jobs"]) == ["ci"]
    assert workflow["jobs"]["ci"]["name"] == "ci"
    commands = [step.get("run", "") for step in _steps(workflow, "ci")]
    assert "make check" in commands
    assert "uv sync --locked" in commands


def test_ci_runs_on_uv_managed_python_314():
    workflow = _load("workflows/ci.yml")
    job = workflow["jobs"]["ci"]
    assert job["env"]["UV_PYTHON_PREFERENCE"] == "only-managed"
    setup = next(s for s in _steps(workflow, "ci") if s.get("uses", "").startswith("astral-sh/"))
    assert setup["with"]["python-version"] == "3.14"
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.14"


def test_a_commit_is_not_tested_twice():
    triggers = _load("workflows/ci.yml")[True]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"]["branches"] == ["main"]


def test_superseded_ci_runs_are_cancelled_but_never_on_main():
    concurrency = _load("workflows/ci.yml")["concurrency"]
    assert "github.ref" in concurrency["group"]
    cancel = str(concurrency["cancel-in-progress"])
    assert cancel != "True", "cancelling unconditionally would cancel runs on main too"
    assert "refs/heads/main" in cancel


def test_ci_only_reads_the_repository():
    assert _load("workflows/ci.yml")["permissions"] == {"contents": "read"}


# ── release ──


def test_the_release_runs_on_pushes_to_main_with_write_access_alone():
    workflow = _load("workflows/release.yml")
    assert workflow[True] == {"push": {"branches": ["main"]}}
    assert workflow["permissions"] == {"contents": "write"}
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_only_the_release_workflow_may_write():
    for path in sorted((GITHUB / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        permissions = workflow.get("permissions")
        assert isinstance(permissions, dict), f"{path.name} must declare its permissions"
        writes = {scope for scope, level in permissions.items() if level == "write"}
        for job in workflow["jobs"].values():
            assert "permissions" not in job, f"{path.name}: permissions are workflow-level"
        if path.name == "release.yml":
            assert writes == {"contents"}
        else:
            assert not writes, f"{path.name} asks for write access: {writes}"


def test_the_release_reads_its_notes_from_the_changelog_script():
    script = _steps(_load("workflows/release.yml"), "release")[1]["run"]
    assert ".github/scripts/release_notes.py" in script
    assert "--github-output" in script


def test_the_release_is_idempotent_and_marks_prereleases():
    publish = _steps(_load("workflows/release.yml"), "release")[2]["run"]
    assert "set -euo pipefail" in publish
    assert "releases/tags/$TAG" in publish, "an existing release must be detected"
    assert "git/ref/tags/$TAG" in publish, "an existing tag must be detected"
    assert "--verify-tag" in publish, "an existing tag is never moved"
    assert '--target "$GITHUB_SHA"' in publish, "a new tag goes on the pushed commit"
    assert "--prerelease" in publish


# ── Dependabot ──


def test_dependabot_updates_uv_and_actions_weekly_grouped_and_labelled():
    config = _load("dependabot.yml")
    assert config["version"] == 2
    updates = {update["package-ecosystem"]: update for update in config["updates"]}
    assert set(updates) == {"uv", "github-actions"}
    for update in updates.values():
        assert update["directory"] == "/"
        assert update["schedule"]["interval"] == "weekly"
        assert update["labels"] == ["dependencies"]
        groups = list(update["groups"].values())
        assert [group["update-types"] for group in groups] == [["minor", "patch"]]


# ── pull request template ──


def test_the_pull_request_template_has_every_section_and_the_money_path_checklist():
    text = (GITHUB / "pull_request_template.md").read_text(encoding="utf-8")
    headings = [line[3:] for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "Plan",
        "Changes",
        "Tests",
        "Live findings",
        "Risks",
        "Money-path checklist",
        "Follow-ups",
        "VERIFY-LIVE",
    ]
    boxes = [line for line in text.splitlines() if line.startswith("- [ ] ")]
    assert len(boxes) == 8


def test_the_write_workflow_pins_its_actions_by_commit():
    for step in _steps(_load("workflows/release.yml"), "release"):
        if "uses" in step:
            ref = step["uses"].split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), step["uses"]
