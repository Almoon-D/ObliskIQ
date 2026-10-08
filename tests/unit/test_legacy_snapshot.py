"""``legacy/`` is complete per its MANIFEST, neutralised, excluded and imported by nothing.

The snapshot is read-only porting material: every file under ``legacy/`` has exactly one
MANIFEST row and every row a file; no legacy agent instruction keeps a name a tool would
load; and each tool that could pick it up is told not to. Session 20 deletes ``legacy/``
together with this test.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "legacy"
MANIFEST = LEGACY / "MANIFEST.md"

TREES = {
    "lite": ("Almoon-D/ObliskIQ-lite-MCP", "b1ebd49", "AGPL-3.0"),
    "old": ("Almoon-D/Inversor-Inteligente", "68280e1", "Apache-2.0"),
    "pulse": ("Almoon-D/market-pulse-bot", "900dbf4", "AGPL-3.0"),
}
ACTIONS = {"port", "reference", "knowledge", "registry"}

ROW_RE = re.compile(
    r"^\| `(?P<path>legacy/[^`]+)` \| (?P<repo>[^|]+) \| `(?P<sha>[0-9a-f]{7})` \| "
    r"`(?P<original>[^`]+)` \| (?P<licence>[^|]+) \| (?P<target>[^|]+) \| (?P<action>\w+) \|$"
)


def _rows() -> list[re.Match[str]]:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    return [match for line in lines if (match := ROW_RE.match(line))]


def _files() -> set[str]:
    return {
        str(path.relative_to(ROOT))
        for path in LEGACY.rglob("*")
        if path.is_file() and path != MANIFEST
    }


def test_every_file_has_exactly_one_manifest_row_and_every_row_a_file():
    paths = [row.group("path") for row in _rows()]
    assert len(paths) == len(set(paths)), "a path is listed twice"
    assert set(paths) == _files()


def test_each_row_names_its_tree_source_commit_licence_and_action():
    for row in _rows():
        tree = row.group("path").split("/")[1]
        repo, sha, licence = TREES[tree]
        assert row.group("repo").strip() == repo
        assert row.group("sha") == sha
        assert row.group("licence").strip() == licence
        assert row.group("action") in ACTIONS
        assert row.group("target").strip()


def test_the_full_commits_are_recorded():
    text = MANIFEST.read_text(encoding="utf-8")
    for repo, sha, _licence in TREES.values():
        assert re.search(rf"`{re.escape(repo)}` \| `{sha}[0-9a-f]{{33}}`", text), repo


def test_each_tree_keeps_its_licence():
    for tree in TREES:
        assert (LEGACY / tree / "LICENSE").is_file(), tree


def test_the_pattern_library_is_the_only_registry_row():
    registry = [row.group("path") for row in _rows() if row.group("action") == "registry"]
    assert registry == ["legacy/old/ObliskIQ/modules/analytics/pattern_library.py"]


def test_what_the_plan_marks_not_ported_was_not_copied():
    files = _files()
    for absent in (
        "legacy/old/ObliskIQ/modules/macro/analyzer.py",
        "legacy/old/ObliskIQ/modules/analytics/regime_transition_detector.py",
        "legacy/old/ObliskIQ/modules/execution/orchestrator.py",
        "legacy/old/ObliskIQ/modules/utils/prompt_composer.py",
        "legacy/old/ObliskIQ/modules/utils/hybrid_formatter.py",
        "legacy/lite/docs/NEXT-SESSIONS.md",
    ):
        assert absent not in files, absent
    assert not [
        name
        for name in files
        if name.startswith(("legacy/lite/docs/archive/", "legacy/pulse/deploy/"))
    ]


def test_no_legacy_agent_instruction_keeps_a_loadable_name():
    for path in LEGACY.rglob("*"):
        assert path.name not in {"CLAUDE.md", "AGENTS.md", ".claude"}, path
    assert (LEGACY / "lite" / "CLAUDE.legacy.md").is_file()
    assert (LEGACY / "lite" / "_claude" / "skills" / "obliskiq-lite" / "SKILL.md").is_file()
    assert (LEGACY / "lite" / "ROADMAP.legacy.md").is_file()


def test_every_tool_excludes_legacy():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tool = config["tool"]
    assert "legacy" in tool["ruff"]["extend-exclude"]
    assert tool["mypy"]["files"] == ["src"]
    assert "legacy" in tool["pytest"]["ini_options"]["norecursedirs"]
    assert tool["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert "legacy/*" in tool["coverage"]["run"]["omit"]
    assert tool["coverage"]["run"]["source"] == ["obliskiq"]
    assert "/legacy" in tool["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert tool["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/obliskiq"]
    contracts = tool["importlinter"]["contracts"]
    assert any(
        contract["forbidden_modules"] == ["legacy"] and contract["source_modules"] == ["obliskiq"]
        for contract in contracts
    )
    hooks = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "exclude: ^legacy/" in hooks


def test_nothing_in_src_or_tests_imports_legacy():
    offenders = []
    for directory in ("src", "tests"):
        for path in (ROOT / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                if "legacy" in roots:
                    offenders.append(str(path.relative_to(ROOT)))
    assert not offenders
