"""No tax constant in code: every rate, window, year count and date of a tax rule is profile data.

The tax engine (``obliskiq/tax``) and the tax policies (``obliskiq/jurisdiction/tax*.py``) may
hold only structural numbers (0, 1, -1) and the listed unit facts below; a ``Decimal`` built
from a string literal is a rate in disguise and is refused too. Changing a tax rule therefore
means changing ``config/jurisdictions/<id>.yaml``, never a source file.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "src" / "obliskiq"

#: Numbers that carry no rule.
STRUCTURAL = frozenset({0, 1, -1})

#: Module constants that name a unit, never a rule, with the fact each names.
UNIT_FACTS = {
    "tax/lots.py": {"MONTHS_PER_YEAR": "months in a calendar year"},
    "jurisdiction/tax.py": {"PERCENT": "a percent is a hundredth"},
}


def tax_files() -> list[Path]:
    return sorted(
        [*(SOURCE_DIR / "tax").rglob("*.py"), *(SOURCE_DIR / "jurisdiction").glob("tax*.py")]
    )


def _constant_names(tree: ast.Module) -> dict[int, str]:
    """The id of every literal bound at module level, to the UPPER_CASE name it is bound to."""
    owners: dict[int, str] = {}
    for node in tree.body:
        targets = (
            list(node.targets)
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if names:
            for child in ast.walk(node):
                owners[id(child)] = names[0]
    return owners


def tax_constants(source: str, allowed_names: set[str]) -> list[str]:
    """Every number or decimal string in ``source`` that could be a tax rule."""
    tree = ast.parse(source)
    owners = _constant_names(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
            and node.value not in STRUCTURAL
            and owners.get(id(node)) not in allowed_names
        ):
            found.append(f"line {node.lineno}: {node.value!r}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Decimal"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append(f"line {node.lineno}: Decimal({node.args[0].value!r})")
    return found


def test_there_are_tax_files_to_scan():
    names = {path.relative_to(SOURCE_DIR).as_posix() for path in tax_files()}
    assert {
        "tax/lots.py",
        "tax/ledger.py",
        "jurisdiction/tax.py",
        "jurisdiction/tax_es_irpf.py",
        "jurisdiction/tax_generic.py",
        "jurisdiction/tax_profile.py",
    } <= names


def test_no_tax_constant_in_code():
    offenders = {}
    for path in tax_files():
        module = path.relative_to(SOURCE_DIR).as_posix()
        found = tax_constants(path.read_text(encoding="utf-8"), set(UNIT_FACTS.get(module, {})))
        if found:
            offenders[module] = found
    assert not offenders, (
        f"tax rules written in code: {offenders}. A rate, window, year count or date belongs "
        "in config/jurisdictions/<id>.yaml; a unit fact is listed in UNIT_FACTS."
    )


def test_every_listed_unit_fact_exists():
    for module, names in UNIT_FACTS.items():
        tree = ast.parse((SOURCE_DIR / module).read_text(encoding="utf-8"))
        bound = set(_constant_names(tree).values())
        assert set(names) <= bound, module


def test_the_scan_catches_a_rate_a_window_and_a_decimal_string():
    """Positive controls: the guard must not pass everything."""
    assert tax_constants("def f(base):\n    return base * 0.19\n", set()) == ["line 2: 0.19"]
    assert tax_constants("WINDOW_MONTHS = 2\n", set()) == ["line 1: 2"]
    assert tax_constants("RATE = Decimal('0.19')\n", {"RATE"}) == ["line 1: Decimal('0.19')"]
    assert tax_constants("MONTHS = 12\n", {"MONTHS"}) == []
    assert tax_constants("def f(x):\n    return x[0] + 1 - 1\n", set()) == []
