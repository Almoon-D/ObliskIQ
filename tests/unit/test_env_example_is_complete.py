"""``.env.example`` names exactly the environment variables the code reads.

Adapted from ObliskIQ-lite-MCP's tests/test_env_template_is_complete.py. A variable the
code reads that the template never mentions leaves the operator provisioning a machine from
a file that does not tell the whole truth; a name the template lists that nothing reads is
a decision that looks made and is not. ``test_no_committed_secrets`` separately requires
every value in it to be empty.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from pathlib import Path

from obliskiq.env import variable_names

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / ".env.example"

_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def variables_declared(text: str) -> set[str]:
    """Every variable a template assigns, commented-out lines included."""
    declared = set()
    for line in text.splitlines():
        candidate = line.lstrip()
        if candidate.startswith("#"):
            candidate = candidate.lstrip("#").strip()
        match = _ASSIGNMENT_RE.match(candidate)
        if match:
            declared.add(match.group(1))
    return declared


def test_the_template_names_every_variable_the_code_reads():
    missing = variable_names() - variables_declared(TEMPLATE.read_text(encoding="utf-8"))
    assert not missing, f".env.example does not name: {sorted(missing)}"


def test_the_template_names_nothing_the_code_does_not_read():
    extra = variables_declared(TEMPLATE.read_text(encoding="utf-8")) - variable_names()
    assert not extra, f".env.example names variables nothing reads: {sorted(extra)}"


def test_the_scan_catches_a_missing_variable():
    """Positive control."""
    declared = variables_declared("FRED_API_KEY=\n# EIA_API_KEY=\nnot an assignment\n")
    assert declared == {"FRED_API_KEY", "EIA_API_KEY"}
    assert "SEC_USER_AGENT" in variable_names() - declared
