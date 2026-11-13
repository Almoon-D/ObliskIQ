"""The thesis book in the store: the lifecycle, the text an approval freezes, the append-only log,
grades and pinned shares; and the domain model's own rules.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from thesis_rig import HORIZON, ISIN, NOW, TODAY, approved, fill, instrument, thesis

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.orders import ExecutionMode
from obliskiq.domain.thesis import (
    Conviction,
    LogKind,
    ThesisSource,
    ThesisState,
    parse_ref,
    thesis_ref,
)
from obliskiq.engines.thesis.grading import grade
from obliskiq.errors import StoreError
from obliskiq.store import Store
from obliskiq.store import theses as repo

D = Decimal
BOOK = "thesis book"


def stored(store: Store, **changes: Any) -> Any:
    return repo.insert_thesis(store, thesis(**changes), actor="owner")


def approve(store: Store, thesis_id: int) -> Any:
    return repo.approve(
        store,
        thesis_id,
        mode=ExecutionMode.SHADOW,
        size_base=D(125),
        at=NOW,
        actor=BOOK,
        detail="approved at 125.00",
    )


def move(store: Store, thesis_id: int, to: ThesisState, **kw: Any) -> Any:
    return repo.move(store, thesis_id, to, at=NOW, actor=BOOK, detail=f"now {to.value}", **kw)


# ── the domain model ──


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"target": D(40), "invalidation": D(40)}, "never shorts"),
        ({"entry_max": D(60)}, "entry_max lies between"),
        ({"enter_by": HORIZON + timedelta(days=1)}, "entry window"),
        ({"enter_by": TODAY - timedelta(days=1)}, "entry window"),
        ({"source": ThesisSource.ANALYST, "conviction": Conviction.HIGH}, "only the owner"),
        ({"size_base": D(100)}, "fixed together"),
        ({"invalidation": D(0)}, "positive price"),
        ({"target": D("NaN")}, "finite number"),
    ],
)
def test_a_thesis_that_does_not_hold_together_is_refused(
    changes: dict[str, Any], words: str
) -> None:
    with pytest.raises(ValidationError, match=words):
        thesis(**changes)


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"conid": None}, "names its conid"),
        ({"conid": None, "listing_exchange": None, "currency": None}, "pinned when the idea"),
        ({"candidate_key": "US:APPLE"}, "carries its ISIN"),
        ({"candidate_key": "US:APPLE", "isin": ISIN}, "is a share"),
    ],
)
def test_an_instrument_is_pinned_whole_and_a_candidate_is_a_share(
    changes: dict[str, Any], words: str
) -> None:
    with pytest.raises(ValidationError, match=words):
        instrument(**changes)


def test_a_candidate_waits_for_its_pin() -> None:
    waiting = instrument(
        candidate_key="US:APPLE",
        isin=ISIN,
        asset_class=AssetClass.STOCK,
        conid=None,
        listing_exchange=None,
        currency=None,
    )
    assert not waiting.pinned
    assert instrument().pinned


def test_a_reference_names_its_thesis() -> None:
    assert thesis_ref(3) == "T3"
    assert approved(thesis_id=3).ref == "T3"
    assert [parse_ref(text) for text in ("T3", " t3 ", "3")] == [3, 3, 3]
    assert [parse_ref(text) for text in ("T0", "three", "", "T-1")] == [None] * 4


# ── the lifecycle ──


def test_a_thesis_is_stored_as_a_candidate_with_its_intake_entry(store: Store) -> None:
    first = stored(store)
    second = stored(store, entry_max=D(50))
    assert (first.thesis_id, second.thesis_id) == (1, 2)
    assert repo.get_thesis(store, 1) == first
    assert repo.get_thesis(store, 2) == second
    assert repo.get_thesis(store, 9) is None
    (entry,) = repo.log_entries(store, 1)
    assert (entry.kind, entry.seq, entry.actor) == (LogKind.INTAKE, 1, "owner")
    assert "handed in by owner, conviction normal" in entry.detail
    with pytest.raises(StoreError, match="enters the store as a candidate"):
        repo.insert_thesis(store, approved(), actor="owner")


def test_theses_are_listed_in_order_of_arrival_as_of_a_date(store: Store) -> None:
    stored(store)
    stored(store)
    move(store, 2, ThesisState.WITHDRAWN)
    assert [t.thesis_id for t in repo.theses(store, as_of=TODAY)] == [1, 2]
    assert [
        t.thesis_id for t in repo.theses(store, as_of=TODAY, states=[ThesisState.CANDIDATE])
    ] == [1]
    assert repo.theses(store, as_of=TODAY - timedelta(days=1)) == []


def test_the_lifecycle_runs_from_candidate_to_closed(store: Store) -> None:
    stored(store)
    held = approve(store, 1)
    assert (held.state, held.mode, held.size_base) == (
        ThesisState.APPROVED,
        ExecutionMode.SHADOW,
        D(125),
    )
    active = move(store, 1, ThesisState.ACTIVE)
    assert (active.state, active.opened_at, active.exit_reason) == (ThesisState.ACTIVE, NOW, None)
    closed = move(store, 1, ThesisState.CLOSED, exit_reason="target")
    assert (closed.state, closed.closed_at, closed.exit_reason) == (
        ThesisState.CLOSED,
        NOW,
        "target",
    )
    assert [e.kind for e in repo.log_entries(store, 1)] == [
        LogKind.INTAKE,
        LogKind.STATE,
        LogKind.STATE,
        LogKind.STATE,
    ]
    latest = repo.latest_log(store, 1)
    assert latest is not None
    assert latest.detail == "now closed"


@pytest.mark.parametrize(
    ("path", "to", "words"),
    [
        ([], ThesisState.ACTIVE, "T1 is candidate: it cannot become active"),
        ([], ThesisState.CLOSED, "cannot become closed"),
        (["approve"], ThesisState.REFUSED, "T1 is approved: it cannot become refused"),
        (["approve", ThesisState.ACTIVE], ThesisState.WITHDRAWN, "T1 is active"),
        ([ThesisState.REFUSED], ThesisState.EXPIRED, "T1 is refused"),
        ([], ThesisState.APPROVED, "approved with its mode and size"),
    ],
)
def test_a_thesis_moves_only_along_its_lifecycle(
    store: Store, path: list[Any], to: ThesisState, words: str
) -> None:
    stored(store)
    for step in path:
        if step == "approve":
            approve(store, 1)
        else:
            move(store, 1, step)
    with pytest.raises(StoreError, match=words):
        move(store, 1, to)


def test_an_unknown_thesis_cannot_be_moved_or_approved_twice(store: Store) -> None:
    with pytest.raises(StoreError, match="no thesis 7"):
        move(store, 7, ThesisState.REFUSED)
    stored(store)
    approve(store, 1)
    with pytest.raises(StoreError, match="cannot become approved"):
        approve(store, 1)


# ── what an approval freezes ──


def test_a_candidate_s_instrument_is_pinned_and_logged(store: Store) -> None:
    waiting = instrument(
        symbol="AAPL",
        candidate_key="US:APPLE",
        isin=ISIN,
        asset_class=AssetClass.STOCK,
        conid=None,
        listing_exchange=None,
        currency=None,
        description="APPLE INC",
    )
    stored(store, instrument=waiting)
    assert repo.pinned_shares(store) == []
    pinned = repo.pin_instrument(
        store,
        1,
        conid=265598,
        symbol="AAPL",
        listing_exchange="NASDAQ",
        currency="USD",
        at=NOW,
        actor=BOOK,
    )
    assert pinned.instrument.pinned
    assert (pinned.instrument.conid, pinned.instrument.currency) == (265598, "USD")
    assert repo.log_entries(store, 1)[-1].kind is LogKind.RESOLVED
    stored(store, instrument=pinned.instrument)  # a second idea on the same share
    stored(store)  # a universe instrument is not a pinned share
    (share,) = repo.pinned_shares(store)
    assert (share.conid, share.symbol, share.listing_exchange, share.currency) == (
        265598,
        "AAPL",
        "NASDAQ",
        "USD",
    )
    assert (share.description, share.isin) == ("APPLE INC", ISIN)


@pytest.mark.parametrize(
    "column",
    ["expectation", "target", "invalidation", "horizon", "entry_max", "enter_by", "conid", "view"],
)
def test_an_approved_thesis_s_text_and_triggers_cannot_change(store: Store, column: str) -> None:
    stored(store)
    store.execute("UPDATE theses SET expectation = 'reworded' WHERE thesis_id = 1")  # a candidate
    approve(store, 1)
    with pytest.raises(sqlite3.IntegrityError, match="text and triggers never change"):
        store.execute(f"UPDATE theses SET {column} = {column} WHERE thesis_id = 1")
    with pytest.raises(StoreError, match="text and triggers never change"):
        repo.pin_instrument(
            store,
            1,
            conid=1,
            symbol="X",
            listing_exchange="AEB",
            currency="EUR",
            at=NOW,
            actor=BOOK,
        )
    assert repo.log_entries(store, 1)[-1].kind is LogKind.STATE  # the failed pin logged nothing


def test_the_store_keeps_identity_approval_endings_and_rows(store: Store) -> None:
    stored(store)
    with pytest.raises(sqlite3.IntegrityError, match="identity never changes"):
        store.execute("UPDATE theses SET source = 'analyst' WHERE thesis_id = 1")
    approve(store, 1)
    with pytest.raises(sqlite3.IntegrityError, match="approved once"):
        store.execute("UPDATE theses SET size_base = '999' WHERE thesis_id = 1")
    move(store, 1, ThesisState.EXPIRED)
    with pytest.raises(sqlite3.IntegrityError, match="an ended thesis never changes"):
        store.execute("UPDATE theses SET state = 'active' WHERE thesis_id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.execute("DELETE FROM theses")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute("UPDATE thesis_log SET detail = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute("DELETE FROM thesis_log")


def test_a_log_entry_is_appended_in_order(store: Store) -> None:
    stored(store)
    assert repo.latest_log(store, 9) is None
    entry = repo.append_log(
        store, 1, at=NOW, actor="owner", kind=LogKind.REUNDERWRITE, detail="a new catalyst"
    )
    assert entry.seq == 2
    assert repo.latest_log(store, 1) == entry


# ── grades ──


def test_a_grade_is_written_once_and_read_as_of_a_date(store: Store) -> None:
    stored(store)
    approve(store, 1)
    active = move(store, 1, ThesisState.ACTIVE)
    graded = grade(
        active, [fill("BUY", "2", "45"), fill("SELL", "2", "60")], exit_reason="target", at=NOW
    )
    repo.record_grade(store, graded)
    assert repo.grades(store, as_of=TODAY) == [graded]
    assert repo.grades(store, as_of=TODAY - timedelta(days=1)) == []
    with pytest.raises(StoreError, match="UNIQUE"):
        repo.record_grade(store, graded)
    with pytest.raises(sqlite3.IntegrityError, match="written once"):
        store.execute("UPDATE thesis_grades SET reached = 0")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.execute("DELETE FROM thesis_grades")


def test_a_row_the_store_cannot_read_says_which(store: Store) -> None:
    stored(store)
    store.execute("PRAGMA ignore_check_constraints = ON")
    store.execute("UPDATE theses SET entry_max = 'abc' WHERE thesis_id = 1")
    with pytest.raises(StoreError, match="entry_max holds 'abc'"):
        repo.get_thesis(store, 1)
    store.execute("UPDATE theses SET entry_max = NULL, view = 'sideways' WHERE thesis_id = 1")
    with pytest.raises(StoreError, match="a theses row cannot be read"):
        repo.get_thesis(store, 1)
