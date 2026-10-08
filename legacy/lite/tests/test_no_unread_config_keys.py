"""Every key in config.yaml has a consumer, or it is not in config.yaml.

The inverse of tests/test_no_magic_numbers.py. That one walks the code and
refuses a threshold written as a literal; this one walks config.yaml and refuses
a key nothing reads. Together they close the loop: a number lives in config, and
a key in config is a number something uses.

The failure this exists to end has happened repeatedly. ``valuation_source_mode``
offered ``none | edgar_us_only | paid`` while no Python file read it, so setting
it would have done exactly nothing while looking like a decision. Phase 3 found
nineteen more of the same shape. The worst were not the inert ones but the ones
that read as GUARANTEES: ``discipline.freeze_thesis_at_entry`` implied the thesis
freeze was optional when it is an unconditional SQL trigger, and the
``execution.*`` trio implied the server gated order staging when the server
exposes no order-staging tool at all. A promise enforced nowhere is worse than a
promise never made.

**What counts as a consumer**, in order of strength:

1. ``src/obliskiq/`` reads it. This is the only one that enforces anything.
2. ``tests/`` names it, pinning a policy value the code does not branch on —
   ``instruments.futures_data_use`` is asserted ``== "allowed"`` in two suites.
3. ``SKILL.md`` names it **as a dotted ``section.key``**. The skill reads
   config.yaml at runtime, so a key it cites is genuinely consumed by judgment.
   Prose that merely gestures at a rule does not count: that is precisely how
   ``instruments.preference_order`` sat unread while SKILL.md restated its
   contents in a sentence. The dotted form is what makes the citation checkable,
   and it is the same discipline test_skill_config_consistency.py enforces from
   the other direction.

Run standalone (no pytest needed):
    python3 tests/test_no_unread_config_keys.py

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
SOURCE_DIR = ROOT / "src" / "obliskiq"
TESTS_DIR = ROOT / "tests"
SKILL_PATH = ROOT / ".claude" / "skills" / "obliskiq-lite" / "SKILL.md"

#: Methods that address config by dotted path. ``get`` is not Config-specific —
#: dicts, os.environ and httpx headers all have one — so it is counted only on a
#: receiver known to hold a Config. Without that restriction
#: ``raw.get("instruments")`` over a parsed universe.yaml marks the whole
#: ``instruments`` subtree read, which is how preference_order stayed hidden.
REQUIRE_METHODS = frozenset(
    {
        "require_float",
        "require_int",
        "require_bool",
        "require_str",
        "require_list",
        "require_choice",
    }
)

#: Names that hold the root Config object.
ROOT_RECEIVERS = frozenset({"config", "cfg", "_config", "self._config"})

#: Sections read wholesale as a dict and then keyed at RUNTIME, where the child
#: names are data rather than knobs. For any other wholesale read, a child counts
#: as consumed only when its own name appears as a string literal in the source —
#: which is what catches grid.min_observations.regression, a floor no call to
#: ``InputBundle.floor`` ever asks for.
RUNTIME_KEYED_SECTIONS = {
    "account.fx_series_by_currency": "currency -> ECB series, keyed by the contract's currency",
    "macro.risk_free_rate.series_by_currency": "currency -> policy-rate series, keyed the same way",
    "grid.freshness_days": "input name -> window, keyed by indicators.FAMILY_INPUTS",
    "ibkr.cp_api.marketdata.snapshot_fields": "field name -> IBKR field id, keyed by field name",
    "ibkr.cp_api.marketdata.underlying_snapshot_fields": "the same map for underlyings",
    "collectors.daily.step_deadline_seconds": (
        "step name -> wall-clock budget, keyed by collect.cli DAILY_SEQUENCE. Stronger "
        "than the usual wholesale read: _step_deadlines refuses a map that is not "
        "exactly DAILY_SEQUENCE, so an orphan here fails the collectors rather than "
        "this scan"
    ),
}


# ── reading config.yaml ──


def _load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _leaf_keys(node: Any, path: str = "") -> list[str]:
    """Every addressable leaf, dotted. An empty mapping is itself a leaf."""
    if isinstance(node, dict) and node:
        found: list[str] = []
        for key, value in node.items():
            found.extend(_leaf_keys(value, f"{path}.{key}" if path else key))
        return found
    return [path]


def _section_paths(node: Any, path: str = "") -> set[str]:
    """Every mapping node, dotted. These are what ``section()`` can bind."""
    if not isinstance(node, dict):
        return set()
    found = {path} if path else set()
    for key, value in node.items():
        found |= _section_paths(value, f"{path}.{key}" if path else key)
    return found


def _child_sections(config: dict[str, Any], prefix: str) -> list[str]:
    node: Any = config
    for part in prefix.split("."):
        node = node[part]
    if not isinstance(node, dict):
        return []
    return [f"{prefix}.{key}" for key, value in node.items() if isinstance(value, dict)]


# ── reading the code ──


def _receiver_name(attribute: ast.Attribute) -> str | None:
    """The name of the object a method was called on, ``self.x`` included."""
    value = attribute.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        if isinstance(value.value, ast.Name) and value.value.id == "self":
            return f"self.{value.attr}"
        return value.attr
    return None


class _ModuleReader:
    """Resolves one module's config reads to absolute dotted paths.

    Section binding is the whole point. ``cfg = config.section("collectors")``
    followed by ``cfg.require_float("request_spacing_seconds")`` reads
    ``collectors.request_spacing_seconds`` and nothing else. Guessing a prefix
    instead — trying every section until one matches — would also "resolve" it
    against ``macro.collection.request_spacing_seconds``, and that duplicate
    would stay invisible. It was.
    """

    def __init__(self, tree: ast.Module, config: dict[str, Any], sections: set[str]) -> None:
        self._config = config
        self._sections = sections
        self._constants = self._module_string_constants(tree)
        self._defaults = self._string_parameter_defaults(tree)
        self._bindings = self._section_bindings(tree)

    @staticmethod
    def _module_string_constants(tree: ast.Module) -> dict[str, str]:
        """Module-level ``NAME = "some.key"``, so a key held in a constant resolves.

        ``instruments.py`` addresses its list as ``PROHIBITED_KEY``; read
        literally, that call passes a Name and the key looks unread.
        """
        found: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                found[target.id] = node.value.value
        return found

    @staticmethod
    def _string_parameter_defaults(tree: ast.Module) -> dict[str, str]:
        """String defaults of parameters, for ``from_config(config, section="http")``."""
        found: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = node.args.posonlyargs + node.args.args
            pairs = list(
                zip(
                    positional[len(positional) - len(node.args.defaults) :],
                    node.args.defaults,
                    strict=True,
                )
            )
            pairs += [
                (arg, default)
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
                if default is not None
            ]
            for arg, default in pairs:
                if isinstance(default, ast.Constant) and isinstance(default.value, str):
                    found[arg.arg] = default.value
        return found

    def _literal(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self._constants.get(node.id) or self._defaults.get(node.id)
        return None

    def _section_bindings(self, tree: ast.Module) -> dict[str, list[str]]:
        """Local names bound to a config section, resolved to absolute prefixes.

        Repeated because a binding can be built from another binding; three
        passes is more nesting than this codebase has.
        """
        bindings: dict[str, list[str]] = {}
        for _ in range(3):
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                call = node.value
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "section"
                    and call.args
                ):
                    continue
                base = _receiver_name(call.func)
                bases = [""] if base in ROOT_RECEIVERS else bindings.get(base or "", [])
                key = self._literal(call.args[0])
                prefixes: list[str] = []
                for outer in bases:
                    if key is None:
                        # sources.section(name) in macro/series.py: the argument is
                        # a loop variable, so every child section is reachable.
                        prefixes.extend(_child_sections(self._config, outer) if outer else [])
                        continue
                    resolved = f"{outer}.{key}" if outer else key
                    if resolved in self._sections:
                        prefixes.append(resolved)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    name = _target_name(target)
                    if name and prefixes:
                        bindings[name] = sorted(set(bindings.get(name, []) + prefixes))
        return bindings

    def read_paths(self, tree: ast.Module) -> set[str]:
        """Absolute config paths this module reads, sections expanded by caller."""
        found: set[str] = set()
        config_receivers = ROOT_RECEIVERS | set(self._bindings)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if not node.args:
                continue
            attribute = node.func.attr
            receiver = _receiver_name(node.func)
            inline = self._inline_section(node.func)
            if attribute == "get":
                if receiver not in config_receivers and inline is None:
                    continue
            elif attribute not in REQUIRE_METHODS:
                continue
            key = self._literal(node.args[0])
            if key is None:
                continue
            if inline is not None:
                found.add(f"{inline}.{key}")
            elif receiver in self._bindings:
                found.update(f"{prefix}.{key}" for prefix in self._bindings[receiver])
            else:
                found.add(key)
        return found

    def _inline_section(self, attribute: ast.Attribute) -> str | None:
        """``config.section("x").require_int("y")``, bound and read in one expression."""
        value = attribute.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "section"
            and value.args
        ):
            return self._literal(value.args[0])
        return None


def _target_name(target: ast.expr) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        if isinstance(target.value, ast.Name) and target.value.id == "self":
            return f"self.{target.attr}"
        return target.attr
    return None


# ── the scan ──


def _source_files() -> list[Path]:
    return sorted(SOURCE_DIR.rglob("*.py"))


def _source_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _source_files())


def keys_read_by_code(config: dict[str, Any] | None = None) -> set[str]:
    """Every config leaf some module in src/obliskiq/ reads."""
    config = config if config is not None else _load_config()
    leaves = set(_leaf_keys(config))
    sections = _section_paths(config)
    literals = _string_literals(_source_text())

    read: set[str] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        reader = _ModuleReader(tree, config, sections)
        for resolved in reader.read_paths(tree):
            if resolved in leaves:
                read.add(resolved)
            elif resolved in sections:
                read |= _children_of_a_wholesale_read(resolved, leaves, literals)
    return read


def _children_of_a_wholesale_read(section: str, leaves: set[str], literals: set[str]) -> set[str]:
    """What a ``get()`` of a whole section actually consumes.

    A runtime-keyed lookup table consumes all of it. Anything else is a set of
    named floors or windows, and a child is only consumed if the code names it:
    ``InputBundle.floor`` is called with "returns", "percentile", "turnover"
    and "correlation", never "regression".
    """
    children = {leaf for leaf in leaves if leaf.startswith(f"{section}.")}
    if section in RUNTIME_KEYED_SECTIONS:
        return children
    return {leaf for leaf in children if leaf.rsplit(".", 1)[-1] in literals}


def _string_literals(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def _keys_named_in(text: str, leaves: set[str]) -> set[str]:
    """Leaves this document names as a dotted ``section.key``."""
    return {leaf for leaf in leaves if leaf in text}


def unread_keys(config: dict[str, Any] | None = None) -> list[str]:
    """Config leaves with no consumer in src/, tests/ or SKILL.md."""
    config = config if config is not None else _load_config()
    leaves = set(_leaf_keys(config))
    consumed = keys_read_by_code(config)
    # This file is excluded from its own scan. It names keys to demonstrate the
    # scanner — the orphan in the positive control below, for one — and a scanner
    # that treated its own examples as consumers could never fail.
    tests_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TESTS_DIR.rglob("*.py"))
        if path != Path(__file__).resolve()
    )
    consumed |= _keys_named_in(tests_text, leaves)
    consumed |= _keys_named_in(SKILL_PATH.read_text(encoding="utf-8"), leaves)
    return sorted(leaves - consumed)


# ── tests ──


def test_there_is_a_config_to_scan():
    """A scan over nothing passes vacuously."""
    leaves = _leaf_keys(_load_config())
    assert len(leaves) >= 200
    assert len(_source_files()) >= 10


def test_every_config_key_has_a_consumer():
    orphans = unread_keys()
    listed = "\n  ".join(orphans)
    assert not orphans, (
        f"config.yaml declares {len(orphans)} key(s) nothing reads:\n  {listed}\n\n"
        "Each one is a decision that looks made and is not. Either give it a "
        "consumer — enforce it in src/obliskiq/, pin it in a test, or name it as "
        "a dotted key in SKILL.md, which reads config at runtime — or delete it. "
        "A key set to a value no code path can observe is worse than no key: it "
        "reads as a guarantee while enforcing nothing."
    )


def test_the_scan_catches_an_orphan_key():
    """Positive control, end to end: without it, gutting the resolver passes silently.

    Run against the whole consumer rule, not just the code scan, because two of
    the three consumers are text searches and a text search is easy to widen
    until it matches everything.
    """
    config = _load_config()
    config.setdefault("option_sleeve", {})["a_key_nobody_reads"] = 0.42
    assert "option_sleeve.a_key_nobody_reads" not in keys_read_by_code(config)
    assert unread_keys(config) == ["option_sleeve.a_key_nobody_reads"]


def test_a_bound_section_resolves_to_its_own_prefix():
    """The duplicate-hiding case, asserted directly.

    ``retry.py`` binds the ``collectors`` section and reads
    ``request_spacing_seconds`` from it. A resolver that guessed prefixes would
    also credit ``macro.collection.request_spacing_seconds``, which was a real
    duplicate that nothing paced anything with.
    """
    read = keys_read_by_code()
    assert "collectors.request_spacing_seconds" in read
    assert "macro.collection.request_spacing_seconds" not in read


def test_a_key_held_in_a_module_constant_resolves():
    """``instruments.py`` addresses its prohibition list as PROHIBITED_KEY."""
    assert "instruments.prohibited" in keys_read_by_code()


def test_a_section_passed_as_a_parameter_default_resolves():
    """``RetryPolicy.from_config(config, section="http")`` binds through a default."""
    read = keys_read_by_code()
    assert {"http.retries", "http.timeout_seconds"} <= read


def test_a_dynamically_bound_section_resolves():
    """``macro/series.py`` does ``sources.section(name)`` over a loop variable."""
    read = keys_read_by_code()
    assert "macro.sources.fred.enabled" in read
    assert "macro.sources.cftc.release_lag_days" in read


def test_a_dict_read_wholesale_does_not_cover_a_child_nothing_names():
    """The limit of a wholesale read, and why `regression` was invisible.

    ``grid.min_observations`` is read as a dict and then keyed by name. Only the
    names the code actually asks for count.
    """
    leaves = {
        "grid.min_observations.returns",
        "grid.min_observations.correlation",
        "grid.min_observations.regression",
    }
    covered = _children_of_a_wholesale_read(
        "grid.min_observations", leaves, {"returns", "correlation"}
    )
    assert covered == {"grid.min_observations.returns", "grid.min_observations.correlation"}


# ── the same rule, applied to macro series ────────────────────────────────────
#
# `macro.series` is a LIST, so its entries are values rather than keys and the
# scan above cannot see them: COT_USD sat in it for the life of the project,
# pulled from CFTC on every macro run, and read by nothing at all — no market
# declared `cot_series: COT_USD`, fx_engine holds no positioning input, and no
# MCP tool serves an arbitrary macro series. It passed every check in this file.
#
# A series collected and read by nothing is the same defect as an orphan config
# key, and costs more: it spends a request on every run and puts rows in a table
# nothing queries.

#: What counts as reading a series id: a `*_series` declaration in universe.yaml,
#: a reference in src/, a test naming it, or SKILL.md citing it.
SERIES_CONSUMER_FILES = ("universe.yaml",)


def _declared_series_ids() -> list[str]:
    config = _load_config()
    return [str(entry["id"]) for entry in config["macro"]["series"]]


def _series_consumer_text() -> str:
    parts = [(ROOT / name).read_text(encoding="utf-8") for name in SERIES_CONSUMER_FILES]
    parts.append(SKILL_PATH.read_text(encoding="utf-8"))
    parts.append(_source_text())
    for path in sorted(TESTS_DIR.rglob("*.py")):
        # This file is the scanner. A series id appearing in its own diagnostics
        # or fixtures must not satisfy the rule it enforces — a test that pins a
        # series IS a consumer, but not the test whose whole job is to ask
        # whether one exists.
        if path.resolve() == Path(__file__).resolve():
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def series_with_no_reader() -> set[str]:
    """Declared series ids that nothing in the repository names."""
    haystack = _series_consumer_text()
    return {
        series_id
        for series_id in _declared_series_ids()
        if not re.search(rf"\b{re.escape(series_id)}\b", haystack)
    }


def test_every_macro_series_has_a_consumer():
    """No exemptions, and none are wanted.

    Twenty-seven series were once collected on every run and read by nothing:
    a dollar index that no market declared, then twenty-six more — breakevens,
    credit spreads, liquidity series, VIX. They were deleted rather than
    declared. Carrying dead config so a later session does
    not have to think is how a repository fills with decisions that look made and
    are not — and nothing is lost, because config.yaml is version-controlled and
    each is a one-line re-add the day something reads it.

    So this rule has no allowlist. A series that earns its place has a consumer
    first: a ``*_series`` declaration in universe.yaml, a read in ``src/``, a
    test pinning it, or a dotted citation in SKILL.md.
    """
    unread = sorted(series_with_no_reader())
    assert not unread, (
        "these macro series are collected on every run and read by nothing:\n  "
        + "\n  ".join(unread)
        + "\n\nGive each a consumer, or delete it. A series pulled into a table "
        "nothing queries costs a request on every run and a decision that looks made."
    )


def test_every_allowlisted_section_still_exists():
    """An allowlist entry for a section that has gone is an exemption nobody reads."""
    config = _load_config()
    for section, reason in sorted(RUNTIME_KEYED_SECTIONS.items()):
        assert reason, f"{section} is exempt with no reason given"
        node: Any = config
        for part in section.split("."):
            assert isinstance(node, dict) and part in node, f"{section} is no longer in config.yaml"
            node = node[part]
        assert isinstance(node, dict) and node, f"{section} is not a populated section"


if __name__ == "__main__":
    failures = 0
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        try:
            function()
        except AssertionError as exc:
            failures += 1
            sys.stderr.write(f"FAIL {name}\n{exc}\n\n")
        else:
            sys.stdout.write(f"ok   {name}\n")
    sys.exit(1 if failures else 0)
