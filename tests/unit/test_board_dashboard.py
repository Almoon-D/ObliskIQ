"""The board the service writes after its digest, and the dashboard rendered from it: every part
carried (or its reason), amounts exact, every value escaped, nothing loaded from anywhere.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from board_rig import AT, digest, performance

from obliskiq.config import ObliskConfig
from obliskiq.reporting.board import (
    BOARD_FORMAT,
    build_board,
    read_board,
    read_patterns,
    write_board,
)
from obliskiq.reporting.dashboard import render_dashboard

PATTERNS = [
    {
        "id": "x",
        "hypothesis": "H1",
        "title": "Momentum <b>",
        "status": "retired",
        "decision": "insufficient",
    },
    {"id": "y", "hypothesis": "H13", "title": "Thesis", "status": "shadow", "decision": None},
]


def _board(settings: ObliskConfig, **kw: object) -> dict[str, object]:
    return build_board(
        digest(settings, **kw),  # type: ignore[arg-type]
        settings,
        performance=performance(),
        shares={"satellite:thesis-book": Decimal("0.05")},
        patterns=PATTERNS,
    )


def test_the_board_carries_every_part_with_exact_amounts(shipped_settings: ObliskConfig) -> None:
    board = _board(shipped_settings)
    assert board["format"] == BOARD_FORMAT
    assert board["mode"] == "paper"
    assert board["nav"] == {"value": "1000", "currency": "EUR", "as_of": AT.date()}
    assert board["twr"]["value"] == "0.012"  # type: ignore[index]
    book = board["book"]
    assert book["capital"]["investable"] == "950"  # type: ignore[index]
    assert book["core"]["variant"] == "D50"  # type: ignore[index]
    assert book["positions"][0]["value"] == "612.50"  # type: ignore[index]
    assert board["budgets"]["live_shares"] == {"satellite:thesis-book": "0.05"}  # type: ignore[index]
    assert board["intents"][0]["size"] == "2"  # type: ignore[index]
    assert board["settings"]["thesis.caps.position"] == "0.25"  # type: ignore[index]
    assert board["performance"]["series"][0]["growth"] == "0.012"  # type: ignore[index]
    assert board["reconciliation"]["status"] == "green"  # type: ignore[index]


def test_a_part_that_was_not_read_says_so(shipped_settings: ObliskConfig) -> None:
    board = build_board(
        digest(shipped_settings, plan=None, nav=None, twr=None, reconcile=None, mode=None),
        shipped_settings,
        performance="the performance cannot be measured: StoreError",
        shares={},
        patterns="no research registry beside this installation",
    )
    assert board["book"] == {"surveyed": False, "capital": None, "core": None, "positions": []}
    assert board["performance"]["measured"] is False  # type: ignore[index]
    assert board["patterns"] == "no research registry beside this installation"
    assert (board["nav"], board["twr"], board["mode"]) == (None, None, None)


def test_a_plan_without_capital_or_portfolio_is_carried(shipped_settings: ObliskConfig) -> None:
    from board_rig import plan

    bare = plan()
    bare.capital = None
    bare.portfolio = None
    bare.blocked = None
    board = build_board(
        digest(shipped_settings, plan=bare), shipped_settings, performance=performance(),
        shares={}, patterns=[],
    )  # fmt: skip
    assert board["book"]["capital"] is None  # type: ignore[index]
    assert board["book"]["core"] is None  # type: ignore[index]
    page = render_dashboard(json.loads(json.dumps(board, default=str)))
    assert "core variant" not in page
    assert "managed " not in page


def test_an_unmeasured_return_has_its_reason(shipped_settings: ObliskConfig) -> None:
    from obliskiq.capital.twr import TwrResult

    board = _board(shipped_settings, twr=TwrResult(periods=(), value=None, reason="no NAV"))
    assert board["twr"] == {"value": None, "since": None, "until": None, "reason": "no NAV"}


def test_the_board_is_written_atomically_and_read_back(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    path = tmp_path / "data" / "board.json"
    write_board(path, _board(shipped_settings))
    read = read_board(path)
    assert read is not None
    assert read["book"]["positions"][0]["symbol"] == "XDWD"
    assert sorted(p.name for p in path.parent.iterdir()) == ["board.json"]


@pytest.mark.parametrize("content", [None, "{broken", '{"format": 99}', "[1]"])
def test_a_missing_or_foreign_board_is_none(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "board.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert read_board(path) is None


def test_the_registry_is_read_for_display(repo_root: Path, tmp_path: Path) -> None:
    patterns = read_patterns(repo_root / "research" / "pattern_registry.yaml")
    assert isinstance(patterns, list)
    h1 = next(p for p in patterns if p["hypothesis"] == "H1")
    assert (h1["status"], h1["decision"]) == ("retired", "insufficient")
    assert "cannot be read" in str(read_patterns(tmp_path / "absent.yaml"))
    odd = tmp_path / "odd.yaml"
    odd.write_text("patterns: {}\n", encoding="utf-8")
    assert "holds no list" in str(read_patterns(odd))
    mixed = tmp_path / "mixed.yaml"
    mixed.write_text("patterns:\n  - just text\n  - {id: a, hypothesis: H9}\n", encoding="utf-8")
    assert read_patterns(mixed) == [
        {"id": "a", "hypothesis": "H9", "title": "None", "status": "None", "decision": None}
    ]


def test_the_dashboard_shows_every_section_escaped_and_self_contained(
    shipped_settings: ObliskConfig,
) -> None:
    board = json.loads(json.dumps(_board(shipped_settings), default=str))
    page = render_dashboard(board)
    assert page.startswith("<!doctype html>")
    for section in (
        "Performance against the references",
        "Sleeves and their budgets",
        "Book: positions and targets",
        "Thesis book",
        "Patterns (research registry)",
        "Intents today",
        "Cash reservations and open questions",
        "Venues",
        "Health",
        "Settings in force",
    ):
        assert section in page
    assert "1000 EUR" in page
    assert "1.20%" in page
    assert "XDWD" in page
    assert "&lt;script&gt;" in page
    assert "<script" not in page
    assert "Momentum &lt;b&gt;" in page
    assert "untested" in page
    assert "http" not in page  # nothing is loaded from anywhere
    assert "prefers-color-scheme: dark" in page


def test_an_empty_board_renders_its_absences(shipped_settings: ObliskConfig) -> None:
    board = build_board(
        digest(
            shipped_settings, plan=None, nav=None, twr=None, reconcile=None, mode=None,
            intents=[], venues=[], health=[], alerts={}, problems=[], kill_on=True,
        ),
        shipped_settings,
        performance="not measured yet",
        shares={},
        patterns="no registry",
    )  # fmt: skip
    page = render_dashboard(json.loads(json.dumps(board, default=str)))
    assert "the book was not surveyed" in page
    assert "none recorded" in page
    assert "not measured: not measured yet" in page
    assert "no intent today" in page
    assert "no registry" in page
    assert "ON" in page
    board["twr"] = {"value": None, "reason": "no NAV"}
    board["patterns"] = []
    page = render_dashboard(json.loads(json.dumps(board, default=str)))
    assert "not measured (no NAV)" in page
    assert "no hypothesis registered" in page
