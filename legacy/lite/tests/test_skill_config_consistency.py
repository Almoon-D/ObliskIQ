# -*- coding: utf-8 -*-
"""config.yaml owns thresholds. The prose names the key instead of the value.

This test fails when a guarded document states a numeric threshold rather than
naming the config key that holds it. Three documents are guarded: SKILL.md,
which owns judgment; BUILD-BRIEF.md, which owns architecture and schema; and
README.md, the front door.

The contradiction it exists to prevent is not hypothetical. SKILL.md once said
"Sector cap 60%, country cap 80%, single foreign currency cap 65%" while
config.yaml said 0.40 / 0.80 / 0.80. Two of the three were wrong.

Note the design choice. A test that only flagged values *also present* in
config would have caught the 80% and waved through the 60% and the 65% —
exactly the two that were wrong. So the rule enforced here is stricter:
threshold-shaped numbers do not belong in a guarded document at all. Evidence
and worked illustrations are exempt by explicit allowlist, with a stated reason.

BUILD-BRIEF.md and README.md carry no exemptions, because after Phase 3 they
need none: architecture and orientation can be written without a single
threshold in them, and every number BUILD-BRIEF used to restate lived in a
section that duplicated something else.

Two documents are deliberately NOT guarded. docs/DECISIONS.md is a record of
what was measured and when — its numbers are evidence of a past state, not
claims about the current one — and docs/archive/ is frozen for the same reason.

The invariant: changing a number in config.yaml must never require editing a
guarded document.

Run standalone (no pytest needed):
    python3 tests/test_skill_config_consistency.py
"""
import io
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
SKILL_PATH = os.path.join(ROOT, ".claude", "skills", "obliskiq-lite", "SKILL.md")
BRIEF_PATH = os.path.join(ROOT, "BUILD-BRIEF.md")
README_PATH = os.path.join(ROOT, "README.md")

# A number counts as threshold-shaped when it carries a unit or magnitude
# marker. Bare integers in prose ("three buckets", "two sessions") do not.
UNIT_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|×|:1|-day|\sday|-year|\syear|-week|\sweek|\sDTE|\sbars|"
    r"\strading|d\b|\sof\smid|\sconcurrent)"
)

# Prose exempt from the scan, and why. Each entry is a distinctive fragment of
# SKILL.md; the digits inside it are ignored. A fragment that no longer appears
# is skipped, so ordinary rewording will not break the build — but a threshold
# re-introduced outside these fragments will.
#
# Evidence and worked illustrations are deliberately exempt. They record what
# happened, or demonstrate why a rule exists. They do not move when config does.
ALLOWED_FRAGMENTS = [
    (
        "so a 200 → 90 DTE hold burns about 33% of time value",
        "Worked illustration of sqrt-of-time decay, not a tenor gate.",
    ),
    (
        "A 20% stop is noise on a 90%-realised-vol name and far too loose on a 15%-vol one",
        "Worked illustration of why fixed-percentage stops fail.",
    ),
    (
        "A market up 60% in local terms can be down in base currency, and a 35% nominal bond "
        "yield against 35% inflation and 20% annual depreciation is a negative real return in euros",
        "Worked illustration of the nominal-return mirage.",
    ),
    (
        "Cycles here are 10–20 years — oil has multi-decade supercycles and uranium spent "
        "fifteen years in a bear market",
        "Evidence about commodity cycle length, not a configured window.",
    ),
    (
        "the edge had decayed by the time it was actionable, costing roughly 7% of the position",
        "Known failure mode. The cost of a missed mechanical window, not a gate.",
    ),
    (
        "A prior version required forward P/E percentiles against 5-year history",
        "Known failure mode. Historical record.",
    ),
    (
        "1. Returns: 5d, 21d, 63d, YTD, 12m",
        "Grid indicator definitions. These return horizons have no config key.",
    ),
]

# Conventional market idioms with no config key. Exempt wherever they appear.
#
# THE 52-WEEK EXEMPTION IS NARROW, AND THE REASON IT USED TO BE WIDE WAS WRONG.
# It read "the 52-week lookback is a market convention, not a tunable window",
# which is true of the RANGE-POSITION leaves and only those: the distance from
# the 52-week high and low is computed off indicators.WEEKS_PER_YEAR, a module
# constant with exactly that comment on it. It is false of the IV percentile,
# whose window IS data.iv_percentile_window_weeks — a real key, read by
# indicators.py and settable to anything. A blanket pattern therefore exempted
# two SKILL.md lines that restated a config value, which is the precise failure
# this whole file exists to prevent, waved through by the file itself.
#
# So the exemption is keyed to the sense: 52 weeks THE RANGE IS MEASURED OVER.
# Any other 52-week phrase — a range, a window, a lookback — is scanned like any
# other number, and the author has to say which 52 they mean.
ALLOWED_IDIOMS = [
    (
        r"52[-\s]weeks?(?=\s+(?:high|low|extremes?|distances?|and\s+long-run))",
        "Distance from the 52-week high/low is indicators.WEEKS_PER_YEAR, a market "
        "convention with no key. The IV percentile's window is NOT: it is "
        "data.iv_percentile_window_weeks and must be named, not restated.",
    ),
    (r"\b1:1\b", "Structural ratio in the penalty formula: gains offset losses one for one."),
    (r"10-year yield", "The tenor of a benchmark government bond, not a threshold."),
]

# The documents guarded, each with the fragments exempt inside it. An empty list
# is the strong position: nothing in that document may state a threshold at all.
GUARDED_DOCUMENTS = [
    ("SKILL.md", SKILL_PATH, ALLOWED_FRAGMENTS),
    ("BUILD-BRIEF.md", BRIEF_PATH, []),
    ("README.md", README_PATH, []),
]


def _numeric_leaves(node, path=""):
    """Every numeric scalar in config, with its dotted key. Booleans excluded."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = "%s.%s" % (path, key) if path else key
            for item in _numeric_leaves(value, child):
                yield item
    elif isinstance(node, list):
        for value in node:
            for item in _numeric_leaves(value, path):
                yield item
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)):
        yield path, float(node)


def config_values():
    with io.open(CONFIG_PATH, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    values = {}
    for key, value in _numeric_leaves(config):
        values.setdefault(value, set()).add(key)
    return values


def _mask(text, fragments=None):
    """Blank the digits in exempt prose, preserving offsets so lines stay true."""
    masked = text
    for fragment, _reason in (ALLOWED_FRAGMENTS if fragments is None else fragments):
        if fragment in masked:
            masked = masked.replace(fragment, re.sub(r"\d", "•", fragment))
    for pattern, _reason in ALLOWED_IDIOMS:
        masked = re.sub(
            pattern,
            lambda m: re.sub(r"\d", "•", m.group(0)),
            masked,
        )
    return masked


def find_restated_thresholds(path=SKILL_PATH, fragments=None):
    """Return [(line_no, literal, [colliding config keys], line text)]."""
    with io.open(path, encoding="utf-8") as handle:
        text = handle.read()
    lines = text.splitlines()
    scan = _mask(text, ALLOWED_FRAGMENTS if fragments is None else fragments)
    values = config_values()

    violations = []
    for match in UNIT_RE.finditer(scan):
        literal = float(match.group("num"))
        candidates = [literal]
        if match.group("unit") == "%":
            candidates.append(literal / 100.0)
        keys = sorted(set().union(*[values.get(c, set()) for c in candidates]))
        line_no = scan[: match.start()].count("\n") + 1
        violations.append((line_no, match.group(0).strip(), keys, lines[line_no - 1].strip()))
    return violations


def _assert_document_states_no_thresholds(name, path, fragments):
    violations = find_restated_thresholds(path, fragments)
    if not violations:
        return
    report = [
        "%s states %d threshold-shaped number(s). config.yaml owns thresholds."
        % (name, len(violations)),
        "",
    ]
    for line_no, literal, keys, line in violations:
        where = ("collides with config: %s" % ", ".join(keys)) if keys else "no config key holds this value"
        report.append("  %s:%d  '%s'  (%s)" % (name, line_no, literal, where))
        report.append("      %s" % line[:160])
    report += [
        "",
        "Fix: replace the value with the config key that holds it, e.g.",
        '  "caps are defined in config.yaml under positions; read them at runtime".',
        "",
        "If the number has no config key, add one rather than hardcoding it here.",
        "If it is evidence or a worked illustration, exempt it in this file's",
        "allowlist for this document, together with the reason. Do not widen the",
        "allowlist to make a restated threshold pass.",
    ]
    raise AssertionError("\n".join(report))


def test_skill_md_states_no_thresholds():
    _assert_document_states_no_thresholds(*GUARDED_DOCUMENTS[0])


def test_build_brief_states_no_thresholds():
    """The brief owns architecture and schema. Numbers in it belong to config.

    It used to restate a couple of dozen, in sections that duplicated SKILL.md,
    the playbooks and config itself. Those sections are gone; this keeps them
    from coming back one line at a time.
    """
    _assert_document_states_no_thresholds(*GUARDED_DOCUMENTS[1])


def test_readme_states_no_thresholds():
    """The front door. Already clean when this guard was added, and staying that way."""
    _assert_document_states_no_thresholds(*GUARDED_DOCUMENTS[2])


def test_every_guarded_document_exists():
    """A guard pointed at a moved file passes vacuously."""
    for name, path, _fragments in GUARDED_DOCUMENTS:
        assert os.path.isfile(path), "%s is not at %s" % (name, path)


def test_guard_is_not_vacuous():
    """Positive control: the scan must still catch the contradiction it was built for.

    Without this, gutting UNIT_RE or over-broadening the allowlist would make
    the test above pass silently and let the drift return.
    """
    regression = (
        "Minimum position 5% of NAV at purchase date. Maximum initial position 25% of NAV. "
        "Sector cap 60%, country cap 80%, single foreign currency cap 65%."
    )
    found = {m.group(0).strip() for m in UNIT_RE.finditer(_mask(regression))}
    for expected in ("60%", "80%", "65%", "25%"):
        assert expected in found, (
            "the scan no longer detects %r; UNIT_RE or the allowlist has been broken"
            % expected
        )

    values = config_values()
    assert values, "config.yaml yielded no numeric values"
    assert "positions.max_sector_pct_nav" in values.get(0.40, set()), (
        "expected positions.max_sector_pct_nav at 0.40; update this control if config changed"
    )



def test_the_52_week_exemption_covers_the_range_position_sense_and_nothing_else():
    """Positive control for the narrowed idiom, both ways round.

    The exemption used to be the bare pattern `52[-\s]weeks?`, justified by "a
    market convention, not a tunable window". That holds for the distance from
    the 52-week high and low, which is computed off indicators.WEEKS_PER_YEAR. It
    does not hold for the IV percentile, whose window is
    data.iv_percentile_window_weeks — so two SKILL.md lines restated a config
    value and this file waved them through.

    A test that only checked the exempt phrases still pass would not have caught
    that, and would not catch it coming back. This checks the other direction as
    well: the phrasings that must NOT be exempt.
    """
    exempt = [
        "Distance from 52-week high; distance from the long-run high",
        "Distance from 52-week low",
        "the distance from both 52-week extremes",
        "The two 52-week distances place the level in its own range",
        "near the 52-week low",
        "distance from 52-week and long-run extremes",
    ]
    for line in exempt:
        assert not UNIT_RE.search(_mask(line)), (
            "%r is the range-position convention and must stay exempt" % line
        )

    caught = [
        "IV percentile over 52 weeks",
        "IV percentile at or below the cap of its 52-week range",
        "ranked against its 52-week window",
    ]
    for line in caught:
        assert UNIT_RE.search(_mask(line)), (
            "%r restates data.iv_percentile_window_weeks and must NOT be exempt; "
            "the idiom pattern has been widened back" % line
        )


def test_the_iv_percentile_window_is_named_by_key_in_the_skill():
    """The two lines the widened exemption hid, pinned by key rather than by the
    absence of a number — so deleting the digit without naming the key fails too."""
    with io.open(SKILL_PATH, encoding="utf-8") as handle:
        text = handle.read()

    assert "data.iv_percentile_window_weeks" in text, (
        "SKILL.md must name the IV percentile's window as a dotted key"
    )
    assert "52-week range" not in text, "that range is a config value, not a convention"
    assert "percentile over 52 weeks" not in text, "same, in the indicator list"


def test_skill_frontmatter_is_parseable():
    with io.open(SKILL_PATH, encoding="utf-8") as handle:
        text = handle.read()
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    _, raw, _ = text.split("---\n", 2)
    meta = yaml.safe_load(raw)
    assert isinstance(meta, dict), "frontmatter did not parse to a mapping"
    assert meta.get("name"), "frontmatter is missing 'name'"
    assert meta.get("description"), "frontmatter is missing 'description'"
    folder = os.path.basename(os.path.dirname(SKILL_PATH))
    assert meta["name"] == folder, (
        "frontmatter name %r must match the skill folder %r" % (meta["name"], folder)
    )


#: The '|' blocks, by dotted key. These are documentation-in-config: prose that
#: belongs beside the prohibition it qualifies rather than in a file somebody has
#: to go and find. Naming them in full is what makes this test their consumer —
#: tests/test_no_unread_config_keys.py counts a dotted key, never a bare name.
BLOCK_SCALAR_KEYS = [
    "instruments.futures_options_note",
    "instruments.permitted_clarification",
]


def test_config_is_valid_yaml_with_intact_block_scalars():
    """The '|' blocks are indentation-sensitive and the likeliest copy-paste casualty."""
    with io.open(CONFIG_PATH, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    for key in BLOCK_SCALAR_KEYS:
        node = config
        for part in key.split("."):
            assert part in node, "%s is no longer in config.yaml" % key
            node = node[part]
        assert isinstance(node, str), "%s did not parse as a block scalar" % key
        assert "\n" in node.strip(), "%s collapsed to a single line" % key
        assert not node.startswith(" "), "%s has leading indentation bleed" % key


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            sys.stderr.write("FAIL %s\n%s\n\n" % (name, exc))
        else:
            sys.stdout.write("ok   %s\n" % name)
    sys.exit(1 if failures else 0)
