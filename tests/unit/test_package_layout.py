"""The package layout of MASTER_PLAN §8: every package exists and says what it owns.

A package with no modules yet holds only its docstring — no stubs, no placeholder logic.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "obliskiq"

#: MASTER_PLAN §8.
PLANNED = (
    "config", "domain", "store", "calendars", "ibkr", "data", "quant", "grid", "engines",
    "jurisdiction", "tax", "capital", "llm", "decision", "execution", "orchestration",
    "scoreboard", "reporting", "server", "cli",
)  # fmt: skip

SPDX = "SPDX-License-Identifier: AGPL-3.0-or-later"
PLACEHOLDER_WORDS = ("TODO", "FIXME", "placeholder", "stub")


@pytest.mark.parametrize("name", PLANNED)
def test_each_planned_package_exists_and_states_what_it_owns(name):
    init = PACKAGE / name / "__init__.py"
    assert init.is_file(), name
    docstring = ast.get_docstring(ast.parse(init.read_text(encoding="utf-8")))
    assert docstring, f"{name}/__init__.py has no docstring"
    first_paragraph = docstring.split("\n\n")[0]
    assert len(first_paragraph.split()) >= 5, f"{name}: say what the package owns"
    assert not any(word in docstring for word in PLACEHOLDER_WORDS), name


@pytest.mark.parametrize("name", PLANNED)
def test_a_package_without_modules_holds_only_its_docstring(name):
    directory = PACKAGE / name
    if [path for path in directory.rglob("*.py") if path.name != "__init__.py"]:
        return
    tree = ast.parse((directory / "__init__.py").read_text(encoding="utf-8"))
    assert len(tree.body) == 1, f"{name}/__init__.py holds code but the package has no modules"


def test_every_source_file_carries_the_licence_identifier():
    missing = [
        str(path.relative_to(ROOT))
        for path in sorted(PACKAGE.rglob("*.py"))
        if SPDX not in path.read_text(encoding="utf-8")
    ]
    assert not missing


def test_no_source_file_leaves_a_placeholder():
    found = [
        str(path.relative_to(ROOT))
        for path in sorted(PACKAGE.rglob("*.py"))
        if any(word in path.read_text(encoding="utf-8") for word in ("TODO", "FIXME", "XXX"))
    ]
    assert not found
