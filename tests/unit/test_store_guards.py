"""Structural guards on the store: every read is bounded, and only the store holds SQL.

Adapted from the point-in-time pass over the legacy reads, which found unbounded reads one at
a time (a ``COUNT(*)`` among them). The guards below find the next one by construction.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
import inspect
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from obliskiq.store import repos

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "src" / "obliskiq"

#: Names that write; every other public function in ``repos`` reads.
WRITE_PREFIXES = ("upsert_", "supersede_")

_SQL = re.compile(
    r"\s*(SELECT\b.+\bFROM\b|INSERT\s+(OR\s+\w+\s+)?INTO\b|UPDATE\s+\w+\s+SET\b|DELETE\s+FROM\b"
    r"|PRAGMA\s+\w+|CREATE\s+(TABLE|INDEX|UNIQUE|TRIGGER|VIEW)\b|DROP\s+\w+|ALTER\s+TABLE\b"
    r"|BEGIN(\s+IMMEDIATE)?\s*$)",
    re.IGNORECASE | re.DOTALL,
)


def public_functions(module: ModuleType) -> dict[str, Callable[..., object]]:
    return {
        name: function
        for name, function in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_") and function.__module__ == module.__name__
    }


def unbounded_reads(functions: dict[str, Callable[..., object]]) -> list[str]:
    """Reads that do not take a required, keyword-only ``as_of``."""
    problems: list[str] = []
    for name, function in sorted(functions.items()):
        if name.startswith(WRITE_PREFIXES):
            continue
        parameter = inspect.signature(function).parameters.get("as_of")
        if (
            parameter is None
            or parameter.kind is not inspect.Parameter.KEYWORD_ONLY
            or parameter.default is not inspect.Parameter.empty
        ):
            problems.append(name)
    return problems


def sql_strings(source: str) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.value.strip()[:40])
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _SQL.match(node.value)
    ]


def test_there_are_reads_to_check() -> None:
    names = set(public_functions(repos))
    reads = {n for n in names if not n.startswith(WRITE_PREFIXES)}
    assert {"bar_series", "macro_series", "chain_rows", "curve", "snapshot_count"} <= reads
    assert {"upsert_bars", "upsert_macro", "supersede_instrument"} <= names - reads


def test_every_repository_read_takes_a_required_keyword_only_as_of() -> None:
    assert unbounded_reads(public_functions(repos)) == []


def test_the_scan_catches_a_read_without_a_bound() -> None:
    """Positive controls: a guard that passed everything would pass here too."""

    def no_bound(store: object, conid: int) -> None: ...

    def positional(store: object, as_of: object) -> None: ...

    def optional(store: object, *, as_of: object = None) -> None: ...

    def bounded(store: object, *, as_of: object) -> None: ...

    def write(store: object, rows: object) -> None: ...

    functions = {
        "no_bound": no_bound,
        "positional": positional,
        "optional": optional,
        "bounded": bounded,
        "upsert_things": write,
    }
    assert unbounded_reads(functions) == ["no_bound", "optional", "positional"]


def test_no_package_outside_the_store_holds_sql() -> None:
    problems: list[str] = []
    for path in sorted(SOURCE_DIR.rglob("*.py")):
        relative = path.relative_to(SOURCE_DIR)
        if relative.parts[0] == "store":
            continue
        for line, text in sql_strings(path.read_text(encoding="utf-8")):
            problems.append(f"{relative}:{line}: {text}")
    assert not problems, "SQL belongs in obliskiq/store/:\n  " + "\n  ".join(problems)


def test_the_scan_for_sql_catches_each_statement_kind() -> None:
    for statement in (
        'q = "SELECT * FROM bars"',
        'q = "  insert into bars values (1)"',
        'q = "UPDATE bars SET close = 1"',
        'q = "DELETE FROM bars"',
        'q = "PRAGMA foreign_keys = ON"',
        'q = "CREATE TABLE t (i INTEGER)"',
    ):
        assert sql_strings(statement), statement
    for harmless in ('q = "select one"', 'q = "Update"', 'q = "the SELECT keyword"'):
        assert sql_strings(harmless) == [], harmless


# ── the order path's state reads (DECISIONS D46) ──

#: Reads in ``store/orders.py`` that read the system's own state whole, by design: a time bound
#: could only hide a newer row (a kill switch, a downgrade, a spent clearance) and fail open.
STATE_READS = {
    "get_intent",
    "intent_by_coid",
    "intents_in_states",
    "intent_history",
    "get_clearance",
    "spent_effect",
    "intent_for_order",
    "broker_orders_for_intent",
    "fills_for_intent",
    "latest_mode_change",
    "latest_kill_switch",
    "holding_assignment",
    # Session 4a: what could still reach the broker, the bot's own fills, the owner's decisions
    # and the stops cut for an exit.
    "open_clearances",
    "fills_by_coid",
    "approval_for",
    "stop_resizes_in_states",
    "stop_resizes_for_exit",
    # Session 5: an instrument's intents (its protective stops and the entries they follow) and
    # the reconciliation in force.
    "intents_for_conid",
    "latest_reconciliation",
    "reconciliation_anchor",
}

#: Names that write in ``store/orders.py``.
ORDER_WRITE_PREFIXES = ("insert_", "transition_", "record_", "upsert_", "spend_", "assign_")


def test_every_order_path_read_is_bounded_or_a_listed_state_read() -> None:
    from obliskiq.store import orders

    functions = public_functions(orders)
    reads = {
        name: function
        for name, function in functions.items()
        if not name.startswith(ORDER_WRITE_PREFIXES) and name not in STATE_READS
    }
    assert unbounded_reads(reads) == []
    assert set(functions) >= STATE_READS, "a listed state read no longer exists"
    assert {"positions_as_of", "cash_as_of"} <= set(reads), "the account snapshots keep as_of"


# ── research history, the trial registry and tax lots (DECISIONS D27, D46) ──

#: The harness's own records and the lot engine's state, read whole: series metadata describes a
#: download, trials count whatever their date, lots are the engine's current state.
RESEARCH_STATE_READS = {"research_series_info", "research_series_in", "trials_for", "tax_lots"}

#: Names that write in ``store/research.py`` and ``store/lots.py``.
RESEARCH_WRITE_PREFIXES = ("replace_", "record_", "save_")


def test_every_research_read_is_bounded_or_a_listed_state_read() -> None:
    from obliskiq.store import lots, research

    functions = {**public_functions(research), **public_functions(lots)}
    reads = {
        name: function
        for name, function in functions.items()
        if not name.startswith(RESEARCH_WRITE_PREFIXES) and name not in RESEARCH_STATE_READS
    }
    assert set(reads) == {"research_observations"}, "a research read must bound itself by as_of"
    assert unbounded_reads(reads) == []
    assert set(functions) >= RESEARCH_STATE_READS, "a listed state read no longer exists"
