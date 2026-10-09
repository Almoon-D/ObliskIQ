"""Every structural key in universe.yaml has a consumer, or it is not in the file.

`tests/test_no_unread_config_keys.py` enforces this for `config.yaml` and has
since Phase 3. `universe.yaml` had no equivalent: that file reads it only as a
CONSUMER of config series ids — `SERIES_CONSUMER_FILES` — and never asks the
inverse question about `universe.yaml`'s own keys. This asks it.

CLAUDE.md states the standard both tests meet: **a key nothing reads is a
decision that looks made and is not.** In a reference file the cost is higher
than clutter, because the loader reads keys by NAME and rejects nothing it does
not recognise — so a key that never reaches `src/` is not a warning anywhere, it
is silence.

**What this scan does and does not walk.** `universe.yaml` is not shaped like
`config.yaml`, and treating it as though it were would produce a test that fails
on every market anybody adds:

* `markets:` keys are **data** — `us`, `japan`, `eurozone` — and their presence
  in `src/` would mean nothing either way. Their CHILDREN are structural:
  `cpi_series`, `slope_label`, `native_index` are the fields the loader reads.
* `instruments`, `unmapped_cells`, `futures_roots` and `commodity_proxies` are
  LISTS, so each entry's field names are the structural keys and each entry's
  values are data.
* The top-level keys are structural.

So the scan collects field names, never identifiers, and asks of each the same
question the config guard asks of a dotted key: does `src/obliskiq/` name it, does
a test pin it, or does SKILL.md cite it.

**It was written expecting to pass and did not.** A pre-deployment audit checked
all 54 structural keys by hand and reported every one consumed. Ten of the sixteen
`futures_roots` rows are YAML *flow* mappings — `{root: HO, ..., untradeable_reason:
futures contract prohibited; no accessible retail wrapper, macro signal only}` —
and an unquoted scalar inside `{}` ends at the comma. So the reason was truncated
at the comma, the tail became a key of its own (`macro signal only: null`), and
`untradeable_reason` reached `compute/gates.py` missing its second half. Nothing
could see it: the loader takes the keys it knows and ignores the rest, and a
truncated sentence still reads like a sentence. The quoting is fixed; this is
what found it.

Run standalone (no pytest needed):
    python3 tests/test_no_unread_universe_keys.py

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = ROOT / "universe.yaml"
SOURCE_DIR = ROOT / "src" / "obliskiq"
TESTS_DIR = ROOT / "tests"
SKILL_PATH = ROOT / ".claude" / "skills" / "obliskiq-lite" / "SKILL.md"

#: Top-level blocks that are LISTS of records. Each entry's field names are
#: structural; each entry's values are data.
RECORD_LISTS = ("instruments", "unmapped_cells", "futures_roots", "commodity_proxies")

#: Top-level blocks that are MAPPINGS keyed by an identifier. The identifiers are
#: data — a market name is not something `src/` has to spell — and the fields
#: inside each one are structural.
KEYED_BLOCKS = ("markets",)

#: Top-level blocks that are plain lists of scalars: values, with no field names
#: of their own. `sectors` is the sector axis, and every entry of it is data.
SCALAR_LISTS = ("sectors",)


def _load() -> dict[str, Any]:
    with UNIVERSE_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def structural_keys(universe: dict[str, Any] | None = None) -> dict[str, str]:
    """Every structural key, mapped to where in the file it comes from.

    The location travels with the key because "``bucket`` is unread" is a
    different investigation depending on whether it came off an instrument row or
    a futures root.
    """
    universe = universe if universe is not None else _load()
    found: dict[str, str] = dict.fromkeys(universe, "top level")

    for block in KEYED_BLOCKS:
        for identifier, spec in (universe.get(block) or {}).items():
            if not isinstance(spec, dict):
                continue
            for key in spec:
                found.setdefault(str(key), f"{block}.<{identifier}>")

    for block in RECORD_LISTS:
        for entry in universe.get(block) or []:
            if not isinstance(entry, dict):
                continue
            for key in entry:
                found.setdefault(str(key), f"{block}[]")

    for block in SCALAR_LISTS:
        # Named so the block is accounted for rather than silently unwalked: a
        # list of scalars has no field names, and an entry of it is data.
        assert isinstance(universe.get(block), list), f"{block} is no longer a list of scalars"

    return found


def _consumer_text() -> str:
    """Everything that counts as reading a key, minus this file.

    This file is excluded from its own scan for the reason the config guard
    excludes itself: it names keys to demonstrate the scanner — the orphan in the
    positive control, for one — and a scanner whose own examples satisfied it
    could never fail.
    """
    parts = [path.read_text(encoding="utf-8") for path in sorted(SOURCE_DIR.rglob("*.py"))]
    parts.append(SKILL_PATH.read_text(encoding="utf-8"))
    parts += [
        path.read_text(encoding="utf-8")
        for path in sorted(TESTS_DIR.rglob("*.py"))
        if path.resolve() != Path(__file__).resolve()
    ]
    return "\n".join(parts)


def unread_keys(universe: dict[str, Any] | None = None) -> dict[str, str]:
    """Structural keys nothing in the repository names, with where each came from.

    Matched as a plain substring rather than as a dotted path, which is the one
    place this differs from the config guard and is deliberate. A config key is
    addressed as ``section.key`` and the dotted form is what makes a citation
    checkable. A universe key is addressed as ``row.get("history_exchange")`` or
    as a column of the same name, so the bare name IS the citation, and requiring
    a dotted one would fail every key in the file.
    """
    haystack = _consumer_text()
    return {key: where for key, where in structural_keys(universe).items() if key not in haystack}


# ── tests ──


def test_there_is_a_universe_to_scan():
    """A scan over nothing passes vacuously."""
    universe = _load()
    keys = structural_keys(universe)
    assert len(keys) >= 50, f"only {len(keys)} structural keys found; the shape has changed"
    assert {"markets", "instruments", "futures_roots"} <= set(universe)


def test_every_universe_key_has_a_consumer():
    orphans = unread_keys()
    listed = "\n  ".join(f"{key}   ({where})" for key, where in sorted(orphans.items()))
    assert not orphans, (
        f"universe.yaml declares {len(orphans)} structural key(s) nothing reads:\n  {listed}\n\n"
        "Each one is a decision that looks made and is not. Either give it a consumer — "
        "read it in src/obliskiq/, pin it in a test, or cite it in SKILL.md — or delete "
        "it. Check the YAML first: a flow mapping `{a: 1, b: two words, three}` splits an "
        "unquoted scalar at the comma, which truncates the value AND invents a key, and "
        "the loader takes the fields it knows and ignores the rest."
    )


def test_a_market_identifier_is_data_and_is_never_required_to_appear_in_src():
    """The shape difference that would otherwise make this test useless.

    `markets:` is keyed by market name. Requiring `us` or `eurozone` to appear in
    `src/obliskiq/` would fail the day somebody adds a market — which is the
    ordinary case — while their FIELDS are what the loader actually reads.
    """
    universe = _load()
    keys = structural_keys(universe)
    for identifier in universe["markets"]:
        assert identifier not in keys or keys[identifier] != f"markets.<{identifier}>"
    assert "cpi_series" in keys and keys["cpi_series"].startswith("markets.<")


def test_the_scan_catches_an_orphan_key():
    """Positive control, end to end.

    Run against the whole consumer rule and not just one half of it: the rule is
    a text search, and a text search is easy to widen until it matches everything.
    """
    universe = _load()
    universe["futures_roots"].append({"root": "ZZ", "a_field_nobody_reads": True})
    orphans = unread_keys(universe)
    assert list(orphans) == ["a_field_nobody_reads"]
    assert orphans["a_field_nobody_reads"] == "futures_roots[]"


def test_a_truncating_flow_scalar_is_caught_rather_than_absorbed():
    """The defect this found on arrival, pinned as a shape rather than a instance.

    `{..., untradeable_reason: futures contract prohibited, macro signal only}`
    parses as TWO keys, and the second is the tail of the first. The value that
    reaches the code is silently short and the orphan is the only visible trace.
    """
    entry = yaml.safe_load("[{root: ZZ, reason: prohibited; no wrapper, macro signal only}]")[0]
    assert entry["reason"] == "prohibited; no wrapper"
    assert "macro signal only" in entry, "this is the shape the scan has to catch"

    universe = _load()
    universe["futures_roots"].append(entry)
    assert "macro signal only" in unread_keys(universe)


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
