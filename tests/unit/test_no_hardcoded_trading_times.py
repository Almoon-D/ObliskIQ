"""No trading time, time zone or venue code is a literal in ``src/obliskiq/``.

When a venue opens, where it sits and what it is called live in ``config/`` (the venue table)
or in the library the calendars read; code derives everything from those. A clock time such as
``"09:30"``, ``time(9, 30)`` or ``replace(hour=9)``, an IANA zone name such as
``"America/New_York"`` and a configured venue code such as ``"XNYS"`` are therefore refused in
source files. The tests are free to name them: they are the expected values.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "src" / "obliskiq"
VENUES_FILE = ROOT / "config" / "defaults" / "venues.yaml"

_CLOCK = re.compile(r"\d{1,2}:\d{2}(:\d{2})?")
_ZONE = re.compile(r"[A-Z][A-Za-z_]+(/[A-Z][A-Za-z_]+)+")
_CLOCK_KEYWORDS = frozenset({"hour", "minute", "second"})


def configured_venue_codes() -> frozenset[str]:
    """Every venue code the configuration names, from the files rather than from this test."""
    data = yaml.safe_load(VENUES_FILE.read_text(encoding="utf-8"))
    codes = set(data["venues"])
    for section in ("synthetic_calendars", "lunch_breaks", "seasonal_closes"):
        codes |= set(data[section])
    codes |= {entry["venue"] for entry in data["ibkr_exchanges"].values()}
    return frozenset(codes)


def _is_time_constructor(node: ast.Call) -> bool:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name == "time"


def offending_literals(source: str, venue_codes: frozenset[str]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if _CLOCK.fullmatch(text):
                found.append((node.lineno, f"clock time {text!r}"))
            elif _ZONE.fullmatch(text):
                found.append((node.lineno, f"time zone {text!r}"))
            elif text in venue_codes:
                found.append((node.lineno, f"venue code {text!r}"))
        elif isinstance(node, ast.Call):
            if _is_time_constructor(node) and any(
                isinstance(arg, ast.Constant) and isinstance(arg.value, int) for arg in node.args
            ):
                found.append((node.lineno, "time(...) built from literal numbers"))
            found.extend(
                (keyword.value.lineno, f"{keyword.arg}={keyword.value.value}")
                for keyword in node.keywords
                if keyword.arg in _CLOCK_KEYWORDS
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, int)
            )
    return found


def test_the_venue_table_is_there_to_check_against():
    """A scan against an empty table passes vacuously."""
    codes = configured_venue_codes()
    assert {"XNYS", "XETR", "XTKS", "XEUR"} <= codes
    assert len(codes) >= 27


def test_no_source_file_hardcodes_a_trading_time():
    codes = configured_venue_codes()
    problems: list[str] = []
    for path in sorted(SOURCE_DIR.rglob("*.py")):
        for line, what in offending_literals(path.read_text(encoding="utf-8"), codes):
            problems.append(f"{path.relative_to(ROOT)}:{line}: {what}")
    assert not problems, (
        "trading times, time zones and venue codes belong in config/defaults/venues.yaml:\n  "
        + "\n  ".join(problems)
    )


CODES = frozenset({"XNYS"})


@pytest.mark.parametrize(
    ("source", "what"),
    [
        ('OPEN = "09:30"', "clock time"),
        ('close = "16:00:00"', "clock time"),
        ("t = time(9, 30)", "time(...)"),
        ("t = datetime.time(16)", "time(...)"),
        ("d = d.replace(hour=9, minute=30)", "hour=9"),
        ("d = d.replace(minute=0)", "minute=0"),
        ('zone = ZoneInfo("America/New_York")', "time zone"),
        ('zone = "Europe/Madrid"', "time zone"),
        ('zone = "America/Argentina/Buenos_Aires"', "time zone"),
        ('venue = "XNYS"', "venue code"),
        ('if mic == "XNYS":\n    pass', "venue code"),
    ],
)
def test_the_scan_catches_each_kind_of_hardcoded_time(source: str, what: str):
    """Positive controls: without them, a scan that matched nothing would pass."""
    found = offending_literals(source, CODES)
    assert found, source
    assert any(what in description for _, description in found), found


@pytest.mark.parametrize(
    "source",
    [
        "t = time.fromisoformat(config.open)",
        "t = time()",
        "d = d + timedelta(hours=1, minutes=30)",
        "d = d.replace(hour=hour, minute=minute)",
        'name = "not a venue"',
        'ratio = "3:4 is not a clock"[0]',
        'text = "Region/"',
    ],
)
def test_the_scan_leaves_ordinary_code_alone(source: str):
    assert offending_literals(source, CODES) == []
