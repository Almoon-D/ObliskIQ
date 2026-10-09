"""Packaging invariants.

requirements.txt duplicates the runtime dependency set from pyproject.toml. Two
lists of the same thing drift, and the one nobody runs is the one that rots — so
the drift is a test rather than a convention.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import re
import tomllib


def _parse(spec: str) -> tuple[str, str]:
    """Split a requirement into (normalised name, version constraint)."""
    match = re.match(r"^\s*([A-Za-z0-9._-]+)\s*(.*)$", spec)
    assert match, f"unparseable requirement: {spec!r}"
    name, constraint = match.groups()
    return name.lower().replace("_", "-"), constraint.replace(" ", "")


def _pyproject(repo_root) -> dict:
    return tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))


def _requirements(repo_root) -> list[str]:
    lines = (repo_root / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [ln for ln in (raw.strip() for raw in lines) if ln and not ln.startswith("#")]


def test_requirements_txt_matches_pyproject(repo_root):
    declared = dict(_parse(d) for d in _pyproject(repo_root)["project"]["dependencies"])
    mirrored = dict(_parse(d) for d in _requirements(repo_root))
    assert mirrored == declared, (
        "requirements.txt has drifted from pyproject.toml's [project.dependencies]. "
        "pyproject is authoritative; regenerate the mirror."
    )


def test_the_package_is_named_for_the_server(repo_root):
    """BUILD-BRIEF names the server obliskiq-lite-mcp."""
    assert _pyproject(repo_root)["project"]["name"] == "obliskiq-lite-mcp"


def test_the_skill_folder_and_frontmatter_names_agree(repo_root):
    """CLAUDE.md: the skill's frontmatter name and its folder name must always
    match each other. Enforced here as well as in the skill-consistency test,
    because renaming the package is exactly when this gets broken."""
    skill_dir = repo_root / ".claude" / "skills" / "obliskiq-lite"
    assert skill_dir.is_dir()
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"^name:\s*(\S+)\s*$", text, re.MULTILINE)
    assert match, "SKILL.md frontmatter has no name field"
    assert match.group(1) == skill_dir.name


def test_py_typed_marker_is_present_and_packaged(repo_root):
    """The package is fully annotated; without the PEP 561 marker no consumer
    sees any of it."""
    assert (repo_root / "src" / "obliskiq" / "py.typed").is_file()
    package_data = _pyproject(repo_root)["tool"]["setuptools"]["package-data"]["obliskiq"]
    assert "py.typed" in package_data


def test_migrations_are_packaged(repo_root):
    """A wheel without the .sql files installs a store that cannot migrate."""
    package_data = _pyproject(repo_root)["tool"]["setuptools"]["package-data"]["obliskiq"]
    assert any("migrations" in entry for entry in package_data)


def _workflow(repo_root):
    """The CI workflow, parsed.

    ``on:`` comes back under the key ``True`` and that is not a bug here: YAML
    1.1 resolves a bare ``on`` to a boolean, and ``yaml.safe_load`` implements
    1.1. GitHub reads the file as YAML 1.2, where it stays a string. Anything
    asserting on the triggers has to know which of the two it is holding.
    """
    import yaml

    return yaml.safe_load((repo_root / ".github" / "workflows" / "ci.yml").read_text())


def test_a_commit_is_not_tested_twice(repo_root):
    """`on: [push, pull_request]` unfiltered ran the whole matrix twice per commit.

    Once for the push and once for the pull_request event, on the identical SHA:
    eight jobs where four would do. The runner minutes are the smaller half. Two
    runs of one workflow on one commit report independently, so CI can show one
    green and one queued, and deciding whether the commit passed means knowing
    which run to trust.
    """
    triggers = _workflow(repo_root)[True]

    assert triggers["push"]["branches"] == ["main"], (
        "push must be limited to main, where no pull request covers the commit; "
        "anywhere else it duplicates the pull_request run"
    )
    assert "main" in triggers["pull_request"]["branches"]


def test_ci_never_cancels_a_run_on_main(repo_root):
    """Superseding a run is right on a branch and wrong on the default one.

    Two merges in quick succession would otherwise leave the first commit on main
    with no completed result at all — which is indistinguishable from a commit
    nobody ran, and main is what every deployment is cut from. On a pull request
    the superseded SHA is not going anywhere, so cancelling it costs nothing.
    """
    concurrency = _workflow(repo_root)["concurrency"]

    assert concurrency["group"], "a concurrency group is what makes runs supersede at all"
    cancel = str(concurrency["cancel-in-progress"])
    assert cancel != "True", "cancel-in-progress: true would cancel runs on main too"
    assert "refs/heads/main" in cancel, (
        "the cancel condition must exempt main by name, or a merge can land a commit "
        "whose CI never finished"
    )


def test_requires_python_covers_the_ci_matrix(repo_root):
    """A matrix entry below the declared floor would test what we do not support."""
    import yaml

    floor = _pyproject(repo_root)["project"]["requires-python"]
    match = re.search(r">=\s*(\d+)\.(\d+)", floor)
    assert match, f"unparseable requires-python: {floor!r}"
    minimum = (int(match.group(1)), int(match.group(2)))

    workflow = yaml.safe_load((repo_root / ".github" / "workflows" / "ci.yml").read_text())
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    versions = [tuple(int(part) for part in str(v).split(".")) for v in matrix]

    assert min(versions) == minimum, (
        f"the CI matrix starts at {min(versions)} but requires-python declares {minimum}"
    )
