"""No threshold, window, floor or cap is a literal in a decision path.

Adapted from ObliskIQ-lite-MCP. Changing a number must mean changing ``config/``, never a
source file. A numeric literal in a decision-path package must be either

* structural arithmetic (0, 1, 2, -1, 0.5 — indices, halves, squares), or
* the value of a module-level UPPER_CASE constant, which names a protocol or unit fact
  rather than a tunable.

Anything else comes from configuration. The decision paths are the packages whose numbers
size, gate, place, fund, tax, budget, rank or grade money (:data:`DECISION_PATHS`);
calendars, collectors, the store, the domain models, reporting, the cockpit, the command line,
configuration loading and the shared helpers carry protocol and format constants and are
left out.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "src" / "obliskiq"

#: Packages whose numbers are money decisions.
DECISION_PATHS = (
    "decision",
    "execution",
    "engines",
    "capital",
    "tax",
    "jurisdiction",
    "ibkr",
    "orchestration",
    "scoreboard",
    "quant",
    "grid",
    "llm",
    "research",
)

#: Literals that carry no policy: indices, halves, squares, sign flips.
STRUCTURAL = frozenset({0, 1, 2, -1, 0.5})


def _module_constant_values(tree: ast.Module) -> set[float]:
    """Numbers bound to a module-level UPPER_CASE name."""
    values: set[float] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not names or not all(name.lstrip("_").isupper() for name in names):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, int | float)
                and not isinstance(child.value, bool)
            ):
                values.add(child.value)
    return values


def offending_literals(source: str) -> list[tuple[int, float]]:
    tree = ast.parse(source)
    allowed = STRUCTURAL | _module_constant_values(tree)
    found: list[tuple[int, float]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        if value not in allowed:
            found.append((node.lineno, value))
    return found


def decision_path_files() -> list[Path]:
    return sorted(
        path for package in DECISION_PATHS for path in (SOURCE_DIR / package).rglob("*.py")
    )


def test_every_decision_path_exists():
    """A scope naming a package that is gone would scan nothing for it."""
    for package in DECISION_PATHS:
        assert (SOURCE_DIR / package / "__init__.py").is_file(), package


def test_there_are_decision_path_files_to_scan():
    assert len(decision_path_files()) >= len(DECISION_PATHS)


def test_no_magic_numbers_in_decision_paths():
    offenders = {
        str(path.relative_to(ROOT)): found
        for path in decision_path_files()
        if (found := offending_literals(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        f"numeric literals that are neither structural nor named module constants: {offenders}. "
        "Move them to config/."
    )


def test_the_scan_actually_catches_a_threshold():
    """A guard against the detector silently passing everything."""
    assert offending_literals("def f(x):\n    return x > 0.15\n") == [(2, 0.15)]


def test_a_named_module_constant_is_accepted():
    source = "MAX_SPREAD_PCT = 0.15\n\n\ndef f(x):\n    return x > MAX_SPREAD_PCT\n"
    assert offending_literals(source) == []


def test_a_lowercase_module_variable_is_not_a_constant():
    assert offending_literals("max_spread = 0.15\n") == [(1, 0.15)]


def test_structural_literals_and_booleans_are_accepted():
    assert offending_literals("def f(x):\n    return x[0] * 2 - 1 + 0.5 if True else -1\n") == []


# ── the money decisions take only listed protocol constants (DECISIONS D50) ──

#: Packages where a module-level constant does not pass on its name alone: each numeric one must
#: be listed in :data:`PROTOCOL_CONSTANTS` with the protocol or unit fact it names.
ALLOWLISTED_PACKAGES = ("execution", "decision")

#: The protocol and unit constants those packages hold, by module, with the fact each names.
PROTOCOL_CONSTANTS: dict[str, dict[str, str]] = {
    "execution/quotes.py": {"SECONDS_PER_HOUR": "seconds in an hour (a unit)"},
    "decision/rebalance_basic.py": {"ZERO": "the zero amount"},
    "decision/gates.py": {"ZERO": "the zero amount"},
    "decision/vehicle_selector.py": {
        "ZERO": "the zero amount",
        "ONE": "a whole score, and one unit of the base currency",
        "CONVERSIONS_PER_ROUND_TRIP": "a round trip converts in to buy and back to sell (two)",
        "MONTHS_PER_YEAR": "months in a year (a unit)",
    },
    "execution/pretrade.py": {
        "ZERO": "the zero amount",
        "CAP_WINDOW": "one day, the period 'per day' names (a unit)",
    },
    "execution/exposure.py": {"ZERO": "the zero amount"},
    "execution/exits.py": {"ZERO": "the zero amount"},
    "execution/currency.py": {"ZERO": "the zero amount"},
    "execution/protective.py": {"ZERO": "the zero amount"},
    "execution/executor.py": {"ZERO": "the zero amount"},
    "execution/reconcile.py": {"ZERO": "the zero amount"},
    "execution/fill_cash.py": {"ZERO": "the zero amount"},
    "execution/instruments.py": {
        "UNIT_MULTIPLIER": "one unit of a share, fund or bond costs its price (a unit)"
    },
}


def numeric_constant_names(source: str) -> set[str]:
    """Module-level UPPER_CASE names whose value holds a number."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        targets = (
            list(node.targets)
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        upper = [t.id for t in targets if isinstance(t, ast.Name) and t.id.lstrip("_").isupper()]
        numeric = any(
            isinstance(child, ast.Constant)
            and isinstance(child.value, int | float)
            and not isinstance(child.value, bool)
            for child in ast.walk(node)
        )
        if upper and numeric:
            names.update(upper)
    return names


def test_money_decision_constants_are_listed_protocol_facts():
    unlisted: dict[str, set[str]] = {}
    for package in ALLOWLISTED_PACKAGES:
        for path in sorted((SOURCE_DIR / package).rglob("*.py")):
            module = path.relative_to(SOURCE_DIR).as_posix()
            names = numeric_constant_names(path.read_text(encoding="utf-8"))
            extra = names - set(PROTOCOL_CONSTANTS.get(module, {}))
            if extra:
                unlisted[module] = extra
    assert not unlisted, (
        f"numeric module constants in a money-decision package that are not listed protocol "
        f"facts: {unlisted}. A tunable belongs in config/; a protocol or unit fact is listed in "
        "PROTOCOL_CONSTANTS with what it names."
    )


def test_every_listed_protocol_constant_still_exists():
    for module, names in PROTOCOL_CONSTANTS.items():
        found = numeric_constant_names((SOURCE_DIR / module).read_text(encoding="utf-8"))
        assert set(names) <= found, module


def test_the_constant_scan_finds_numeric_constants_only():
    source = "LIMIT = 0.15\nNAME = 'x'\nPAIR: tuple[int, int] = (1, 2)\nflag = 3\nOK = True\n"
    assert numeric_constant_names(source) == {"LIMIT", "PAIR"}
