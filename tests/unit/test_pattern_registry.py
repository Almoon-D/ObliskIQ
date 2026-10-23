"""research/pattern_registry.yaml is well formed until Session 15 adds its loader: every entry
has a lifecycle status, the field that status needs (a grading rule in shadow, a result when
retired, the data it needs as an idea), and files that exist; a tested entry's lock matches its
pre-registration (MASTER_PLAN §5.9; DECISIONS D62).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from pathlib import Path

import yaml

from obliskiq.research.registry import file_sha256

LIFECYCLE = {"idea", "pre-registered", "tested", "shadow", "active", "retired"}
NEEDS = {"shadow": "grading", "retired": "result", "idea": "data_needed"}
FILES = ("preregistration", "lock", "report", "audit")


def entries(repo_root: Path) -> list[dict]:
    data = yaml.safe_load((repo_root / "research" / "pattern_registry.yaml").read_text())
    return data["patterns"]


def test_every_entry_has_a_status_and_what_it_needs(repo_root):
    patterns = entries(repo_root)
    ids = [p["id"] for p in patterns]
    assert len(ids) == len(set(ids))
    for pattern in patterns:
        assert pattern["status"] in LIFECYCLE, pattern["id"]
        needed = NEEDS.get(pattern["status"])
        if needed is not None:
            assert pattern.get(needed), (pattern["id"], needed)


def test_every_named_file_exists_and_every_lock_matches(repo_root):
    for pattern in entries(repo_root):
        for field in FILES:
            if field in pattern:
                assert (repo_root / pattern[field]).is_file(), (pattern["id"], field)
        if "lock" in pattern:
            lock = json.loads((repo_root / pattern["lock"]).read_text())
            prereg = repo_root / pattern["preregistration"]
            assert lock["file"] == prereg.name
            assert lock["sha256"] == file_sha256(prereg)


def test_a_tested_entry_states_its_outcome(repo_root):
    for pattern in entries(repo_root):
        if pattern["status"] in {"tested", "shadow", "active", "retired"}:
            outcome = pattern["outcome"]
            assert outcome["decision"] in {"sufficient", "insufficient", "inconclusive"}
            low, high = (float(v) for v in outcome["interval"])
            assert low <= float(outcome["estimate_a_year"]) <= high


def test_every_hypothesis_a_source_serves_is_registered(repo_root, shipped_settings):
    """A source built for a hypothesis (DECISIONS D66) names one the registry holds."""
    registered = {p["hypothesis"] for p in entries(repo_root) if "hypothesis" in p}
    named = {
        reference.split(":", 1)[1]
        for reference, _ in shipped_settings.sources.references()
        if reference.startswith("hypothesis:")
    }
    assert named
    assert named <= registered, sorted(named - registered)
