"""The idea ledger (H14, DECISIONS D187): a call's path at its own levels on daily closes, its
excess over the reference after the round trip's cost, what is not evaluable, one open call per
instrument and segment, point in time, the groups' records, a segment's calibration, the rank and
the store's records.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.support.core_rig import seed_closes, seed_vehicle
from thesis_rig import instrument, thesis

from obliskiq.config import ObliskConfig
from obliskiq.domain.market import Bar
from obliskiq.domain.thesis import ThesisSource
from obliskiq.engines.thesis.book import rank_score
from obliskiq.errors import PreregistrationError, StoreError
from obliskiq.reporting.ledger import ledger_lines
from obliskiq.research.prereg import IdeaLedgerPrereg, IdeaLedgerSpec, load_any
from obliskiq.scoreboard import ledger
from obliskiq.scoreboard.ledger import OPEN, call_path, grade_ledger, record_segments
from obliskiq.scoreboard.rules import LEDGER_PATH, load_locked_ledger
from obliskiq.store import ledger as repo
from obliskiq.store import theses as thesis_repo
from obliskiq.store.db import Store
from obliskiq.store.ledger import IdeaCall, SegmentState

D = Decimal
REFERENCE = 163606916
STOCK = 4001
#: A Monday after the seeded instruments' creation stamp.
START = date(2026, 10, 5)
WRITTEN = datetime(2026, 10, 5, 9, tzinfo=UTC)
HORIZON = date(2026, 12, 31)


@pytest.fixture(scope="module")
def spec(repo_root: Path) -> IdeaLedgerSpec:
    found = load_any(repo_root / LEDGER_PATH)
    assert isinstance(found, IdeaLedgerPrereg)
    return found.spec


def call(**changes: Any) -> IdeaCall:
    fields: dict[str, Any] = {
        "thesis_id": 1,
        "conid": STOCK,
        "symbol": "AAA",
        "source": ThesisSource.ANALYST,
        "segment": "model@abc",
        "event_tags": (),
        "rises": True,
        "target": D(60),
        "invalidation": D(40),
        "horizon": HORIZON,
        "probability": 0.5,
        "known_at": WRITTEN,
    }
    return IdeaCall(**{**fields, **changes})


def bars(closes: dict[date, float], conid: int = STOCK) -> list[Bar]:
    return [
        Bar(conid=conid, session_date=d, open=None, high=None, low=None, close=c, volume=None,
            source="t")
        for d, c in sorted(closes.items())
    ]  # fmt: skip


def days(n: int, start: date = START, step: int = 1) -> list[date]:
    return [start + timedelta(days=step * (i + 1)) for i in range(n)]


# ── a call's path ──


def test_the_target_first_the_invalidation_first_or_the_horizon() -> None:
    d = days(5)
    up = call_path(call(), bars(dict(zip(d, [50, 55, 61, 30, 50], strict=True))), as_of=d[-1],
                   max_age_days=7)  # fmt: skip
    assert (up.outcome, up.start, up.end, up.start_close, up.end_close) == (
        "target",
        d[0],
        d[2],
        D(50),
        D(61),
    )
    down = call_path(call(), bars(dict(zip(d, [50, 45, 40, 70, 50], strict=True))), as_of=d[-1],
                     max_age_days=7)  # fmt: skip
    assert (down.outcome, down.end) == ("invalidation", d[2])
    short = call(horizon=d[3])
    flat = call_path(short, bars(dict(zip(d, [50, 51, 52, 53, 70], strict=True))), as_of=d[-1],
                     max_age_days=7)  # fmt: skip
    assert (flat.outcome, flat.end, flat.end_close) == ("horizon", d[3], D(53))
    # A view that the instrument falls reads its levels the other way.
    falls = call(rises=False, target=D(40), invalidation=D(60))
    fell = call_path(falls, bars(dict(zip(d, [50, 41, 39, 50, 50], strict=True))), as_of=d[-1],
                     max_age_days=7)  # fmt: skip
    assert (fell.outcome, fell.end) == ("target", d[2])
    # The first close itself resolves a call already beyond a level.
    at_once = call_path(call(), bars({d[0]: 62.0}), as_of=d[0], max_age_days=7)
    assert (at_once.outcome, at_once.start, at_once.end) == ("target", d[0], d[0])


def test_a_call_starts_after_the_day_it_was_written_and_stays_open_until_resolved() -> None:
    same_day = {START: 70.0, START + timedelta(days=1): 50.0}
    found = call_path(call(), bars(same_day), as_of=START + timedelta(days=1), max_age_days=7)
    assert found.start == START + timedelta(
        days=1
    )  # the close of the day it was written is not read
    assert found.open
    assert found.reason == OPEN
    waiting = call_path(call(), [], as_of=START + timedelta(days=3), max_age_days=7)
    assert waiting.open


def test_what_is_not_evaluable_says_why() -> None:
    after = START + timedelta(days=20)
    late = call_path(call(), bars({START + timedelta(days=9): 50.0}), as_of=after, max_age_days=7)
    assert late.reason == "no close within 7 days after it was written"
    none = call_path(call(), [], as_of=START + timedelta(days=20), max_age_days=7)
    assert none.reason == late.reason
    gap = {START + timedelta(days=1): 50.0, START + timedelta(days=12): 61.0}
    unseen = call_path(call(), bars(gap), as_of=START + timedelta(days=20), max_age_days=7)
    assert unseen.reason is not None
    assert "a level may have been crossed unseen" in unseen.reason
    short = call(horizon=START + timedelta(days=20))
    later = START + timedelta(days=30)
    stale = call_path(short, bars({START + timedelta(days=1): 50.0}), as_of=later, max_age_days=7)
    assert stale.reason == "no close within 7 days before its horizon 2026-10-25"


# ── grading over the store ──


def seed(store: Store, closes: dict[date, float], reference: dict[date, float]) -> None:
    seed_vehicle(store, REFERENCE, "XDWD")
    seed_vehicle(store, STOCK, "AAA")
    seed_closes(store, STOCK, closes)
    seed_closes(store, REFERENCE, reference)


def stored(store: Store, **changes: Any) -> IdeaCall:
    made = thesis_repo.insert_thesis(
        store, thesis(instrument=instrument(conid=STOCK, symbol="AAA")), actor="t"
    )
    return repo.record_call(store, call(thesis_id=made.thesis_id, **changes))


def venue(_exchange: str) -> str:
    return "XETR"


def test_a_resolved_call_earns_its_excess_after_the_round_trip(
    store: Store, shipped_settings: ObliskConfig, spec: IdeaLedgerSpec
) -> None:
    d = days(4)
    seed(store, dict(zip(d, [50.0, 55.0, 61.0, 61.0], strict=True)),
         dict(zip(d, [100.0, 101.0, 102.0, 102.0], strict=True)))  # fmt: skip
    stored(store)
    book = grade_ledger(store, shipped_settings, spec, as_of=d[-1], venue_of=venue)
    (graded,) = book.graded
    assert graded.path.outcome == "target"
    assert graded.graded.hit is True
    gross = (61 / 50) / (102 / 100)
    assert graded.graded.excess is not None
    cost = gross_log = __import__("math").log(gross)
    assert 0 < gross_log - graded.graded.excess < 0.05  # the round trip costs something, not much
    assert cost > graded.graded.excess
    group = book.groups["segment:model@abc"]
    assert (group.calls, group.open, group.missing) == (1, 0, 0)
    assert group.outcomes == {"target": 1, "invalidation": 0, "horizon": 0}
    assert group.record.decision is None  # one week is below the minimum
    assert set(book.groups) == {"segment:model@abc", "source:analyst"}
    assert book.unwritten == 0
    lines = ledger_lines(book)
    assert any("too few to judge: 1 weeks" in line for line in lines)
    assert any("1 reached the target first" in line for line in lines)


def test_prices_known_after_as_of_change_nothing(
    store: Store, shipped_settings: ObliskConfig, spec: IdeaLedgerSpec
) -> None:
    d = days(6)
    seed(store, dict(zip(d[:3], [50.0, 52.0, 54.0], strict=True)),
         dict(zip(d, [100.0] * 6, strict=True)))  # fmt: skip
    stored(store)
    before = grade_ledger(store, shipped_settings, spec, as_of=d[2], venue_of=venue)
    seed_closes(store, STOCK, {d[3]: 70.0, d[4]: 30.0})  # later closes: a target, then a loss
    again = grade_ledger(store, shipped_settings, spec, as_of=d[2], venue_of=venue)
    assert [g.path for g in before.graded] == [g.path for g in again.graded]
    assert before.graded[0].path.open
    after = grade_ledger(store, shipped_settings, spec, as_of=d[4], venue_of=venue)
    assert after.graded[0].path.outcome == "target"
    # A call written later is not known before it.
    assert grade_ledger(store, shipped_settings, spec, as_of=START - timedelta(days=1),
                        venue_of=venue).groups == {}  # fmt: skip


def test_a_call_inside_an_open_one_of_its_segment_is_counted_beside(
    store: Store, shipped_settings: ObliskConfig, spec: IdeaLedgerSpec
) -> None:
    d = days(10)
    seed(store, dict.fromkeys(d, 50.0), dict.fromkeys(d, 100.0))
    stored(store)
    stored(store, known_at=WRITTEN + timedelta(days=3))  # the first is still open
    stored(store, known_at=WRITTEN + timedelta(days=3), segment="owner",
           source=ThesisSource.OWNER)  # fmt: skip
    book = grade_ledger(store, shipped_settings, spec, as_of=d[-1], venue_of=venue)
    assert book.overlapping == 1
    assert len(book.graded) == 2
    assert any("1 written while an earlier idea" in line for line in ledger_lines(book))


def test_a_reference_or_a_cost_that_cannot_be_read_leaves_the_call_unmarked(
    store: Store, shipped_settings: ObliskConfig, spec: IdeaLedgerSpec
) -> None:
    d = days(3)
    seed(store, dict(zip(d, [50.0, 61.0, 61.0], strict=True)), {})
    stored(store)
    book = grade_ledger(store, shipped_settings, spec, as_of=d[-1], venue_of=venue)
    assert book.not_evaluable == {"no mark of the equity reference within the age allowed": 1}
    seed_closes(store, REFERENCE, dict.fromkeys(d, 100.0))
    unpriced = grade_ledger(store, shipped_settings, spec, as_of=d[-1], venue_of=lambda _e: None)
    assert unpriced.not_evaluable == {"AAA has no a venue to price its round trip": 1}
    nowhere = grade_ledger(store, shipped_settings, spec, as_of=d[-1], venue_of=lambda _e: "XNYS")
    (why,) = nowhere.not_evaluable
    assert why.startswith("the cost model cannot price AAA's round trip")


def test_the_registered_bins_must_be_the_reliability_table_s(
    store: Store, shipped_settings: ObliskConfig, spec: IdeaLedgerSpec
) -> None:
    other = spec.model_copy(
        update={"calibration": spec.calibration.model_copy(update={"reliability_bins": 4})}
    )
    with pytest.raises(PreregistrationError, match="4 reliability bins"):
        grade_ledger(store, shipped_settings, other, as_of=START, venue_of=venue)


# ── records and calibration on many calls ──


def resolved(i: int, hit: bool, probability: float, segment: str = "model@abc") -> ledger.Graded:
    week = START + timedelta(weeks=i)
    made = call(thesis_id=i + 1, known_at=datetime(week.year, week.month, week.day, 9, tzinfo=UTC),
                probability=probability, segment=segment)  # fmt: skip
    end = week + timedelta(days=14)
    found = ledger.CallPath("target" if hit else "invalidation", week + timedelta(days=1), end,
                            None, D(50), D(60) if hit else D(40))  # fmt: skip
    excess = 0.15 if hit else -0.12
    return ledger.grade(
        made,
        found,
        price=lambda conid, day: (
            D(100)
            if conid == REFERENCE
            else D(50 if day <= week + timedelta(days=1) else (60 if hit else 40))
        ),
        reference=REFERENCE,
        cost=lambda _c, _p: excess and 0.002,
    )


def test_a_segment_past_the_minimum_with_skill_is_trusted(spec: IdeaLedgerSpec) -> None:
    # Two calls a week for 70 weeks (56 effective observations), the stated probability right
    # most of the time.
    items = []
    for i in range(70):
        items.append(resolved(i, hit=True, probability=0.8))
        items.append(resolved(i, hit=i % 3 == 0, probability=0.2))
    rules = ledger._rules(spec, items, 0.8)
    assert rules.horizon_weeks == 2
    found = ledger._calibration("model@abc", items, spec, rules)
    assert found.mature
    assert found.state.calibrated
    assert found.base_rate is not None
    state = found.stored(START)
    assert state.passed
    p_eff = state.p_eff(0.8)
    assert p_eff is not None
    assert found.base_rate < p_eff <= 0.8
    group = ledger._group("segment:model@abc", items, rules, tests=3)
    assert group.record.decision is not None  # past the minimum
    from obliskiq.reporting.ledger import calibration_lines, group_lines

    text = "\n".join(calibration_lines(found, minimum="26 weeks and 50"))
    assert "trusted: its ideas may be offered free slots first" in text
    assert "stated 80% to 100%" in text
    assert "Verdict:" in "\n".join(group_lines(group, minimum="26 weeks and 30"))


def test_a_segment_below_the_minimum_is_not_trusted(spec: IdeaLedgerSpec) -> None:
    items = [resolved(i, hit=True, probability=0.9) for i in range(5)]
    rules = ledger._rules(spec, items, 0.8)
    found = ledger._calibration("model@abc", items, spec, rules)
    assert not found.mature
    assert not found.stored(START).passed
    from obliskiq.reporting.ledger import calibration_lines

    assert "Too few to read its probabilities" in "\n".join(
        calibration_lines(found, minimum="26 weeks and 50")
    )
    nothing = ledger._calibration("model@abc", [], spec, rules)
    assert "no resolved idea states a probability" in calibration_lines(nothing, minimum="x")[0]


# ── the rank ──


def test_the_rank_is_the_expected_gain_at_the_price() -> None:
    assert rank_score(0.5, rises=True, target=D(60), invalidation=D(40), price=D(50)) == 0.0
    assert rank_score(0.6, rises=True, target=D(60), invalidation=D(45), price=D(50)) == (
        pytest.approx(0.6 * 0.2 - 0.4 * 0.1)
    )
    assert rank_score(0.6, rises=False, target=D(40), invalidation=D(55), price=D(50)) == (
        pytest.approx(0.6 * 0.2 - 0.4 * 0.1)
    )
    assert rank_score(0.6, rises=True, target=D(60), invalidation=D(40), price=D(0)) is None
    trusted = SegmentState("s", START, True, 0.5, 0.4, "ok")
    assert trusted.p_eff(0.8) == pytest.approx(0.6)
    assert SegmentState("s", START, False, 0.0, 0.4, "no").p_eff(0.8) is None


# ── the store ──


def test_the_ledger_s_records_are_append_only_and_read_as_of(store: Store) -> None:
    made = thesis_repo.insert_thesis(store, thesis(), actor="t")
    repo.record_probability(store, made.thesis_id, 0.35, at=WRITTEN)
    assert repo.probability(store, made.thesis_id) == 0.35
    assert repo.probability(store, 99) is None
    first = repo.record_call(store, call(thesis_id=made.thesis_id, event_tags=("earnings",)))
    assert first.call_id is not None
    assert repo.calls(store, as_of=START) == [first]
    assert repo.calls(store, as_of=START - timedelta(days=1)) == []
    assert repo.called_theses(store) == {made.thesis_id}
    state = SegmentState("model@abc", START, True, 0.4, 0.3, "skill")
    assert repo.record_segment(store, state, at=WRITTEN)
    assert not repo.record_segment(store, state, at=WRITTEN)  # one row a segment a day
    later = SegmentState("model@abc", START + timedelta(days=1), False, 0.0, 0.3, "lost it")
    repo.record_segment(store, later, at=WRITTEN)
    assert repo.segment_states(store, as_of=START) == {"model@abc": state}
    assert repo.segment_states(store, as_of=START + timedelta(days=5))["model@abc"] == later
    assert repo.segment_states(store, as_of=START - timedelta(days=1)) == {}
    for table in ("idea_calls", "thesis_probabilities", "ledger_segments"):
        with (
            pytest.raises((StoreError, sqlite3.DatabaseError), match="append-only"),
            store.transaction(),
        ):
            store.execute(f"DELETE FROM {table}")


def test_a_call_that_cannot_be_read_is_a_store_error(store: Store) -> None:
    made = thesis_repo.insert_thesis(store, thesis(), actor="t")
    with store.transaction():
        store.execute(
            "INSERT INTO idea_calls (thesis_id, conid, symbol, source, segment, event_tags, rises, "
            "target, invalidation, horizon, probability, known_at) VALUES "
            "(?, 1, 'X', 'owner', 'owner', '[]', 1, '5abc', '40', '2026-12-31', NULL, "
            "'2026-10-05T09:00:00Z')",
            (made.thesis_id,),
        )
    with pytest.raises(StoreError, match="cannot be read"):
        repo.calls(store, as_of=START)


def test_a_book_s_segments_are_stored_for_its_day(
    store: Store, shipped_settings: ObliskConfig, spec: IdeaLedgerSpec
) -> None:
    d = days(3)
    seed(store, dict(zip(d, [50.0, 61.0, 61.0], strict=True)), dict.fromkeys(d, 100.0))
    stored(store)
    book = grade_ledger(store, shipped_settings, spec, as_of=d[-1], venue_of=venue)
    assert record_segments(store, book, at=WRITTEN) == 1
    assert record_segments(store, book, at=WRITTEN) == 0
    (state,) = repo.segment_states(store, as_of=d[-1]).values()
    assert not state.passed
    assert book.summary() == "idea ledger: 1 calls, 1 resolved, 0 segments trusted"


def test_the_ledger_rules_are_read_only_when_locked(repo_root: Path, tmp_path: Path) -> None:
    assert isinstance(load_locked_ledger(repo_root / LEDGER_PATH), IdeaLedgerPrereg)
    copy = tmp_path / "idea-ledger.md"
    copy.write_text((repo_root / LEDGER_PATH).read_text() + "\nchanged\n", encoding="utf-8")
    (tmp_path / "idea-ledger.lock").write_text(
        (repo_root / LEDGER_PATH).with_suffix(".lock").read_text(), encoding="utf-8"
    )
    with pytest.raises(PreregistrationError, match="not the text its lock records"):
        load_locked_ledger(copy)
    other = tmp_path / "scoreboard.md"
    other.write_bytes((repo_root / "research" / "preregistrations" / "scoreboard.md").read_bytes())
    lock = json.loads((repo_root / "research" / "preregistrations" / "scoreboard.lock").read_text())
    lock["file"] = "scoreboard.md"
    (tmp_path / "scoreboard.lock").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(PreregistrationError, match="not the idea ledger's"):
        load_locked_ledger(other)


def test_a_book_that_could_not_be_graded_says_why() -> None:
    assert ledger_lines("no lock") == ["Not graded: no lock."]
