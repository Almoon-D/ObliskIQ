"""The machine is static: no rule is anchored to a position this account has held.

The mandate exists because of real losses, and the *pattern* those losses taught
belongs in SKILL.md. The **names and the amounts do not.** A gate whose comment
reads as a lesson about two tickers stops applying the moment the book holds
different ones; a test pinned to one day's NAV stops being a statement about the
engine the moment NAV moves; and a rule that recites the account's own trades
invites the next session to reason from them rather than from what it measured
today. The ledger is where positions live. Everything scanned here is the
machine, and the machine must read the same whoever runs it.

Two kinds of anchor are refused:

**Symbols the account has traded that are not in the declared universe.** The
universe is read from ``universe.yaml`` at scan time rather than hardcoded, so
adding an instrument to the universe legitimises its name everywhere — and
removing one immediately makes every leftover mention a finding.

**Account-scale money.** A figure with cents, in the range a retail book's NAV,
sleeve or single realised P&L occupies. Round numbers pass: worked illustrations
need arithmetic somebody can follow, and ``2 x 1.50 = EUR 3.00`` is a tariff
calculation, not a statement about this account.

Not scanned, deliberately:

* ``docs/`` — ``DECISIONS.md`` records what was measured and when, and
  ``docs/archive/`` is frozen evidence. Both are history on purpose.
* ``references/*-playbook.md`` — a log of observed API behaviour, where a dated
  reading against a named contract is the whole value.
* ``tests/`` — including this file, which has to name what it bans.
* ``src/obliskiq/store/migrations/`` — applied migrations are checksummed
  (``store/db.py``), so editing one breaks every store it has already run on.
  A migration is immutable, comment and all.

Runs standalone, with only PyYAML:

    python3 tests/test_no_account_history.py
"""

import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIVERSE_PATH = os.path.join(ROOT, "universe.yaml")

#: Symbols this account has held or traded, and which are NOT in universe.yaml.
#: A symbol here is banned outright; a symbol in the universe is legitimate
#: wherever it appears, because the collectors, the grid and the gates all have
#: to name the instruments they measure.
TRADED_NOT_IN_UNIVERSE = ["ONDS", "IREN", "LNG", "MU"]

#: Money with cents, big enough to be a position, a sleeve or a NAV rather than
#: a commission or a strike. Matched with or without a currency marker, and with
#: either separator convention: 11,168.40 / 11168.40 / EUR 384.96 / 226.101747.
ACCOUNT_MONEY_RE = re.compile(
    r"(?<![\w.])(?:EUR|USD|€|\$)?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+\.\d{2,}|\d{3,6}\.\d{2,})"
    r"(?![\d\w])"
)

#: Below this a figure with cents is a fee, a premium per share or a strike, not
#: a fact about the size of this book.
ACCOUNT_MONEY_FLOOR = 100.0

#: Files scanned. The machine: what it enforces, what it says while enforcing
#: it, and what it tells the model before any of that.
SCANNED_FILES = [
    os.path.join(".claude", "skills", "obliskiq-lite", "SKILL.md"),
    "config.yaml",
    "universe.yaml",
    "calendars.yaml",
    "README.md",
    "BUILD-BRIEF.md",
    "ROADMAP.md",
]

#: Directories scanned, by extension. Migrations are excluded: see the docstring.
SCANNED_TREES = [(os.path.join("src", "obliskiq"), (".py",))]
EXCLUDED_DIRS = {"__pycache__", "migrations"}

#: Amounts that are arithmetic rather than account history, with the reason.
#: An entry is a literal fragment; a fragment that no longer appears is skipped,
#: so rewording does not break the build.
ALLOWED_AMOUNTS = [
    (
        "5% of 10,000 is 500",
        "A round worked example in a test-facing comment, not a NAV.",
    ),
]


def universe_symbols():
    """Every symbol the universe declares, in upper case."""
    with open(UNIVERSE_PATH, encoding="utf-8") as handle:
        universe = yaml.safe_load(handle)
    symbols = set()
    for key in ("instruments", "futures_roots", "commodity_proxies"):
        for row in universe.get(key) or []:
            for field in ("symbol", "root"):
                value = row.get(field)
                if value:
                    symbols.add(str(value).upper())
    return symbols


def _scanned_paths():
    for relative in SCANNED_FILES:
        path = os.path.join(ROOT, relative)
        if os.path.isfile(path):
            yield relative, path
    for relative_root, extensions in SCANNED_TREES:
        base = os.path.join(ROOT, relative_root)
        for directory, subdirectories, files in os.walk(base):
            subdirectories[:] = [d for d in subdirectories if d not in EXCLUDED_DIRS]
            for name in sorted(files):
                if name.endswith(extensions):
                    path = os.path.join(directory, name)
                    yield os.path.relpath(path, ROOT), path


def _mask_allowed(text):
    """Blank the digits in exempt arithmetic, preserving length so lines stay true."""
    for fragment, _reason in ALLOWED_AMOUNTS:
        if fragment in text:
            text = text.replace(fragment, re.sub(r"\d", "•", fragment))
    return text


def find_traded_symbols():
    """[(relative_path, line_no, symbol, line)] for every banned symbol mention."""
    declared = universe_symbols()
    banned = [s for s in TRADED_NOT_IN_UNIVERSE if s not in declared]
    if not banned:
        return []
    pattern = re.compile(r"(?<![A-Za-z0-9_])({})(?![A-Za-z0-9_])".format("|".join(banned)))

    hits = []
    for relative, path in _scanned_paths():
        with open(path, encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                for match in pattern.finditer(line):
                    hits.append((relative, line_no, match.group(1), line.strip()))
    return hits


def find_account_amounts():
    """[(relative_path, line_no, amount, line)] for every account-scale figure."""
    hits = []
    for relative, path in _scanned_paths():
        with open(path, encoding="utf-8") as handle:
            text = _mask_allowed(handle.read())
        lines = text.splitlines()
        for match in ACCOUNT_MONEY_RE.finditer(text):
            amount = float(match.group("num").replace(",", ""))
            if amount < ACCOUNT_MONEY_FLOOR:
                continue
            line_no = text[: match.start()].count("\n") + 1
            hits.append((relative, line_no, match.group(0).strip(), lines[line_no - 1].strip()))
    return hits


def test_no_traded_symbol_outside_the_universe_is_named():
    hits = find_traded_symbols()
    if not hits:
        return
    report = ["A symbol this account traded is named outside the ledger:", ""]
    for relative, line_no, symbol, line in hits:
        report.append(f"  {relative}:{line_no}  {symbol}")
        report.append(f"      {line[:160]}")
    report += [
        "",
        "Fix: state the pattern, not the position. 'An entry high in the long-run",
        "range with no dated catalyst' is a rule; a ticker is a memory, and the",
        "machine has to read the same whoever runs it. If the instrument now",
        "belongs to the tracked universe, declare it in universe.yaml — this scan",
        "reads the universe rather than a hardcoded list.",
    ]
    raise AssertionError("\n".join(report))


def test_no_account_scale_amount_is_stated():
    hits = find_account_amounts()
    if not hits:
        return
    report = ["An account-scale amount is stated in the machine:", ""]
    for relative, line_no, amount, line in hits:
        report.append(f"  {relative}:{line_no}  '{amount}'")
        report.append(f"      {line[:160]}")
    report += [
        "",
        "Fix: a NAV, a sleeve balance or a realised P&L is a fact about one day of",
        "one account and belongs in the ledger or in docs/archive/. If the figure is",
        "arithmetic a reader has to follow, use round numbers and exempt the",
        "fragment in ALLOWED_AMOUNTS with its reason.",
    ]
    raise AssertionError("\n".join(report))


def test_the_universe_is_readable_and_not_empty():
    """The symbol allowlist is derived, so an unreadable universe must fail loudly."""
    symbols = universe_symbols()
    assert len(symbols) > 20, (
        f"universe.yaml yielded {len(symbols)} symbols; the scan would be vacuous"
    )
    assert "SPY" in symbols, "expected SPY in the declared universe; update this control"


def test_the_scan_is_not_vacuous():
    """Positive control: both patterns must still catch what they were built for."""
    declared = universe_symbols()
    for symbol in TRADED_NOT_IN_UNIVERSE:
        assert symbol not in declared, (
            f"{symbol} is now in universe.yaml, so it is no longer a past-trade anchor. "
            "Remove it from TRADED_NOT_IN_UNIVERSE."
        )

    regression = (
        "Evidence from this book: ONDS was entered near the high and IREN followed. "
        "The booked penalty was EUR 384.96 against a sleeve of 558.42, on a NAV of "
        "11,168.40."
    )
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])({})(?![A-Za-z0-9_])".format("|".join(TRADED_NOT_IN_UNIVERSE))
    )
    assert {m.group(1) for m in pattern.finditer(regression)} == {"ONDS", "IREN"}, (
        "the symbol scan no longer detects a past-trade anchor"
    )

    amounts = {m.group("num") for m in ACCOUNT_MONEY_RE.finditer(regression)}
    for expected in ("384.96", "558.42", "11,168.40"):
        assert expected in amounts, f"the amount scan no longer detects {expected}"

    # And it must NOT fire on the arithmetic the config legitimately shows.
    tariff = "2 x 1.50 = EUR 3.00 per round trip, and EUR 3.00 / 0.15 = EUR 20."
    assert not [
        m
        for m in ACCOUNT_MONEY_RE.finditer(tariff)
        if float(m.group("num").replace(",", "")) >= ACCOUNT_MONEY_FLOOR
    ], "the amount scan now fires on a published tariff calculation"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            sys.stderr.write(f"FAIL {name}\n{exc}\n\n")
        else:
            sys.stdout.write(f"ok   {name}\n")
    sys.exit(1 if failures else 0)
