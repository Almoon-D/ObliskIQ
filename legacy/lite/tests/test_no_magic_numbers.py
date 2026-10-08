"""No threshold, window, floor or cap is a literal in Python.

CLAUDE.md: "All thresholds and windows from config.yaml. No magic numbers."
This is the test that makes that enforceable rather than aspirational. It is the
same idea as tests/test_skill_config_consistency.py, applied to the code: changing
a number must mean changing config.yaml, never a source file.

The rule enforced: a numeric literal in src/obliskiq/ must be either

  * structural arithmetic (0, 1, 2, -1, 0.5 — indices, halves, squares), or
  * the value of a module-level UPPER_CASE constant, which is a named protocol
    or unit fact, not a tunable.

Anything else has to come from config.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import ast

import pytest

#: Literals that carry no policy: array indices, halves, squares, sign flips.
STRUCTURAL = frozenset({0, 1, 2, -1, 0.5})


def _module_constant_values(tree: ast.Module) -> set[float]:
    """Numbers bound to a module-level UPPER_CASE name.

    A named constant states what it is — HTTP_UNAUTHORIZED, PERCENT_TO_DECIMAL,
    _BITS_PER_BYTE — and is a protocol or unit fact rather than a threshold
    somebody might want to tune.
    """
    values: set[float] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names or not all(n.lstrip("_").isupper() for n in names):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, (int, float))
                and not isinstance(child.value, bool)
            ):
                values.add(child.value)
    return values


def _offending_literals(source: str) -> list[tuple[int, float]]:
    tree = ast.parse(source)
    allowed = STRUCTURAL | _module_constant_values(tree)

    # Slices of a module constant (text[:ERROR_EXCERPT_CHARS]) are already named;
    # what matters is the bare literals.
    found: list[tuple[int, float]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value in allowed:
            continue
        found.append((node.lineno, value))
    return found


def _source_files(repo_root):
    return sorted((repo_root / "src" / "obliskiq").rglob("*.py"))


def test_there_are_source_files_to_scan(repo_root):
    """A scan over nothing passes vacuously; make sure it is scanning something."""
    assert len(_source_files(repo_root)) >= 10


@pytest.mark.parametrize(
    "relative",
    [
        "config.py",
        "env.py",
        "errors.py",
        "logging_setup.py",
        "util/numeric.py",
        "util/http.py",
        "store/db.py",
        "quant/bsm.py",
        "ibkr/oauth.py",
        "ibkr/cp_client.py",
        "ibkr/session.py",
        "macro/rates.py",
        "macro/series.py",
        "macro/fetchers.py",
        "util/timeutil.py",
        "collect/source.py",
        "collect/runlog.py",
        "collect/retry.py",
        "collect/state.py",
        "collect/vol.py",
        "collect/strikes.py",
        "collect/universe.py",
        "collect/chain_collector.py",
        "collect/curve_collector.py",
        "collect/backfill.py",
        "collect/macro_pull.py",
        "collect/cli.py",
        "instruments.py",
        "store/ledger.py",
        "compute/stats.py",
        "compute/frames.py",
        "compute/calendar.py",
        "compute/gates.py",
        "compute/smile.py",
        "compute/penalty.py",
        "compute/risk_engine.py",
        "compute/fx_engine.py",
        "compute/commodity_engine.py",
        "compute/indicators.py",
        "compute/build_grid.py",
        "compute/rankings.py",
        "compute/universe_census.py",
        "compute/cli.py",
    ],
)
def test_no_magic_numbers_in_module(repo_root, relative):
    path = repo_root / "src" / "obliskiq" / relative
    offenders = _offending_literals(path.read_text(encoding="utf-8"))
    assert not offenders, (
        f"{relative} contains numeric literals that are neither structural nor named "
        f"module constants: {offenders}. Move them to config.yaml."
    )


def test_the_whole_package_is_clean(repo_root):
    offenders = {}
    for path in _source_files(repo_root):
        found = _offending_literals(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(repo_root))] = found
    assert not offenders, offenders


def test_the_scan_actually_catches_a_threshold():
    """A guard against the detector silently passing everything."""
    offending = "def f(x):\n    return x > 0.15\n"
    assert _offending_literals(offending) == [(2, 0.15)]


def test_a_named_module_constant_is_accepted():
    allowed = "MAX_SPREAD_PCT = 0.15\n\n\ndef f(x):\n    return x > MAX_SPREAD_PCT\n"
    assert _offending_literals(allowed) == []


def test_a_lowercase_module_variable_is_not_a_constant():
    """Only UPPER_CASE names count; a lowercase module global is just a variable."""
    sneaky = "max_spread = 0.15\n"
    assert _offending_literals(sneaky) == [(1, 0.15)]


def test_config_keys_referenced_in_code_all_exist(repo_root, config):
    """Every literal config path passed to require_*/get/section must resolve.

    A typo in a dotted key would otherwise only surface at runtime, on the VM,
    with nobody watching."""
    import ast as _ast

    # require_* and section() are Config-specific. Plain .get() is not — dicts,
    # os.environ and httpx headers all have one — so it is only counted when the
    # receiver is named like a config object.
    config_only = {
        "section",
        "require_float",
        "require_int",
        "require_bool",
        "require_str",
        "require_list",
        "require_choice",
    }
    config_receivers = {
        "config",
        "cfg",
        "sources",
        "_config",
        "self._config",
        "cp_cfg",
        "md_cfg",
        "secdef_cfg",
        "sqlite_cfg",
        "oauth_cfg",
        "tenor",
        "data",
        "chain",
        "curve",
        "backfill",
        "collection",
        "history",
    }
    missing: list[tuple[str, str]] = []

    def _receiver_name(func: _ast.Attribute) -> str:
        value = func.value
        if isinstance(value, _ast.Name):
            return value.id
        if isinstance(value, _ast.Attribute):
            return value.attr
        return ""

    for path in _source_files(repo_root):
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call) or not isinstance(node.func, _ast.Attribute):
                continue
            attr = node.func.attr
            if not node.args:
                continue
            if attr == "get":
                if _receiver_name(node.func) not in config_receivers:
                    continue
            elif attr not in config_only:
                continue
            first = node.args[0]
            if not (isinstance(first, _ast.Constant) and isinstance(first.value, str)):
                continue
            key = first.value
            # A default argument means absence is meaningful; skip those.
            if len(node.args) > 1:
                continue
            if config.get(key, None) is None and not _resolves_under_a_section(config, key):
                missing.append((str(path.relative_to(repo_root)), key))

    assert not missing, f"config keys referenced in code but absent from config.yaml: {missing}"


def _resolves_under_a_section(config, key: str) -> bool:
    """Keys read from an already-bound section are relative, not absolute.

    Try each configured section prefix before declaring a key missing."""
    prefixes = [
        "http",
        "runtime.sqlite",
        "runtime.logging",
        "numerics.bsm",
        "numerics.implied_vol",
        "numerics.implied_vol.solve",
        "macro.risk_free_rate",
        "macro.collection",
        "macro.commodity_history",
        "macro.sources",
        "macro.sources.fred",
        "macro.sources.ecb",
        "macro.sources.cftc",
        "macro.sources.eia",
        "ibkr.cp_api",
        "ibkr.cp_api.oauth",
        "ibkr.cp_api.marketdata",
        "ibkr.cp_api.secdef",
        "ibkr.cp_api.session",
        "ibkr.cp_api.portfolio",
        "ibkr.cp_api.history",
        "ibkr.connector",
        "server",
        "server.collectors",
        "collectors",
        "collectors.universe",
        "collectors.chain",
        "collectors.curve",
        "collectors.backfill",
        "collectors.macro",
        "option_vendor",
        "commodities",
        "commodities.history",
        "commodities.cot",
        "commodities.proxies",
        "commodities.inventory",
        "commodities.term_structure",
        "data",
        "tenor",
        "positions",
        "account",
        "discipline",
        "instruments",
        "liquidity",
        "screen",
        "smile",
        "option_sleeve",
        "zombie_test",
        "equity_risk",
        "portfolio_circuit_breaker",
        "fx_inflation",
        "grid",
        "grid.min_observations",
        "grid.freshness_days",
        "census",
        "calendars",
    ]
    return any(config.get(f"{prefix}.{key}", None) is not None for prefix in prefixes)
