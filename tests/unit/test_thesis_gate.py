"""The thesis book's engine: the draft screen, the gate's reason codes, sizes and slots, the
thesis's own exits and the grade of a closed thesis.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from thesis_rig import HORIZON, ISIN, NOW, TODAY, approved, draft, facts, fill, instrument, thesis

from obliskiq.config import ObliskConfig
from obliskiq.domain.thesis import Conviction, ThesisSource, ThesisState, View
from obliskiq.engines.thesis.book import Slot, base_size, position_size
from obliskiq.engines.thesis.exits import exit_due
from obliskiq.engines.thesis.gate import (
    Reason,
    ThesisGate,
    describe,
    gain_to_loss,
    screen,
    view_findings,
)
from obliskiq.engines.thesis.grading import grade, record

D = Decimal


@pytest.fixture
def gate(shipped_settings: ObliskConfig) -> ThesisGate:
    return ThesisGate(shipped_settings.thesis)


def codes(found: Any) -> list[str]:
    return [finding.code.value for finding in found]


# ── the draft on its own words ──


def test_a_complete_draft_passes_the_screen() -> None:
    assert screen(draft(), today=TODAY) == []
    assert screen(draft(entry_max=D(50), enter_by=TODAY), today=TODAY) == []


@pytest.mark.parametrize(
    ("missing", "words"),
    [
        ({"target": None}, "it has no target"),
        ({"invalidation": None}, "it has no invalidation"),
        ({"horizon": None}, "it has no horizon"),
        ({"expectation": None}, "what is expected"),
        ({"expectation": "  "}, "what is expected"),
        ({"symbol": None}, "name one instrument"),
        ({"candidate": "US:APPLE", "isin": ISIN}, "name one instrument"),
        ({"symbol": None, "candidate": "US:APPLE"}, "needs its ISIN"),
    ],
)
def test_an_incomplete_idea_is_refused_with_its_reason(missing: dict[str, Any], words: str) -> None:
    found = screen(draft(**missing), today=TODAY)
    assert codes(found) == [Reason.INCOMPLETE.value]
    assert all(finding.permanent for finding in found)
    assert words in describe(found)


def test_every_missing_field_is_named_at_once() -> None:
    found = screen(draft(target=None, invalidation=None, horizon=None), today=TODAY)
    assert codes(found) == ["incomplete", "incomplete", "incomplete"]


def test_levels_that_describe_a_fall_are_a_short_and_are_refused() -> None:
    found = screen(draft(target=D(30), invalidation=D(40)), today=TODAY)
    assert codes(found) == [Reason.SHORT.value]
    assert "the book never shorts" in found[0].reason


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"target": D(0)}, "levels"),
        ({"invalidation": D("-1")}, "levels"),
        ({"entry_max": D(70)}, "levels"),
        ({"entry_max": D(40)}, "levels"),
        ({"horizon": TODAY}, "dates"),
        ({"enter_by": TODAY - timedelta(days=1)}, "dates"),
        ({"enter_by": HORIZON + timedelta(days=1)}, "dates"),
        ({"source": ThesisSource.ANALYST, "conviction": Conviction.MAX}, "conviction"),
    ],
)
def test_levels_dates_and_a_borrowed_conviction_are_refused(
    changes: dict[str, Any], code: str
) -> None:
    assert codes(screen(draft(**changes), today=TODAY)) == [code]


def test_a_bearish_idea_is_held_only_as_a_defensive_instrument() -> None:
    risky, gold = instrument(), instrument(symbol="SGLD", defensive=True)
    found = view_findings(View.BEARISH, risky)
    assert codes(found) == [Reason.BEARISH_NOT_DEFENSIVE.value]
    assert found[0].permanent
    assert "never shorts" in found[0].reason
    assert view_findings(View.BEARISH, gold) == []
    assert view_findings(View.BULLISH, risky) == []


# ── sizes and slots ──


def test_the_base_size_is_the_capital_over_the_positions(shipped_settings: ObliskConfig) -> None:
    config = shipped_settings.thesis
    assert base_size(D(1000), config) == D(125)
    assert position_size(D(1000), Conviction.NORMAL, config) == D(125)
    assert position_size(D(1000), Conviction.HIGH, config) == D("187.5")
    assert position_size(D(1000), Conviction.MAX, config) == D(250)


def test_max_conviction_never_passes_the_position_cap(shipped_settings: ObliskConfig) -> None:
    config = shipped_settings.thesis
    eager = config.model_copy(
        update={"conviction": config.conviction.model_copy(update={"max": D(5)})}
    )
    cap = config.caps.position * D(1000)
    assert position_size(D(1000), Conviction.MAX, eager) == cap == D(250)
    judged = ThesisGate(eager).evaluate(thesis(conviction=Conviction.MAX), facts())
    assert judged.passed
    assert judged.size_base == cap
    assert judged.quantity is not None
    assert judged.quantity * D(45) <= cap


# ── the gate ──


def test_an_idea_that_passes_is_sized_in_whole_units(gate: ThesisGate) -> None:
    judged = gate.evaluate(thesis(), facts())
    assert judged.passed
    assert judged.permanent == ()
    assert (judged.size_base, judged.quantity) == (D(125), D(2))  # two units at 45


def test_a_braked_entry_is_sized_at_the_brake_s_share_and_an_approved_one_keeps_its_size(
    gate: ThesisGate,
) -> None:
    braked = gate.evaluate(thesis(conviction=Conviction.MAX), facts(entry_size=D("0.5")))
    assert (braked.size_base, braked.quantity) == (D(125), D(2))  # half of 250, two units at 45
    small = gate.evaluate(thesis(), facts(entry_size=D("0.5"), minimum=D(100)))
    assert [(f.code.value, f.permanent) for f in small.findings] == [("too_small", False)]
    assert gate.evaluate(approved(), facts(entry_size=D("0.5"))).size_base == D(125)


@pytest.mark.parametrize(
    ("changes", "code", "permanent"),
    [
        ({"prohibited": ("X reads as a leveraged product",)}, "prohibited_product", True),
        ({"ineligible": "no KID"}, "ineligible", True),
        ({"held_elsewhere": "the core book holds EMIM"}, "held_elsewhere", True),
        ({"reference": D(60)}, "target_passed", True),
        ({"reference": D(40)}, "invalidated", True),
        ({"reference": None}, "no_quote", False),
        ({"rate": None}, "no_quote", False),
        ({"reference": D(55)}, "gain_to_loss", False),
        ({"capital": D(0)}, "no_capital", False),
        ({"daily_value": None}, "liquidity_unmeasured", False),
        ({"sessions": 19}, "liquidity_unmeasured", False),
        ({"daily_value": D(1000)}, "illiquid", True),
        ({"minimum": D(500)}, "too_small", False),
        ({"reference": D("41"), "capital": D(300)}, "too_small", False),
    ],
)
def test_each_finding_has_its_code(
    gate: ThesisGate, changes: dict[str, Any], code: str, permanent: bool
) -> None:
    judged = gate.evaluate(thesis(), facts(**changes))
    assert not judged.passed
    assert judged.size_base is None
    assert code in codes(judged.findings)
    found = next(f for f in judged.findings if f.code.value == code)
    assert found.permanent is permanent
    assert bool(judged.permanent) is any(f.permanent for f in judged.findings)


def test_an_entry_limit_withholds_above_it(gate: ThesisGate) -> None:
    judged = gate.evaluate(thesis(entry_max=D(44)), facts())
    assert codes(judged.findings) == [Reason.ABOVE_ENTRY_MAX.value]
    assert gate.evaluate(thesis(entry_max=D(45)), facts()).passed


def test_a_full_book_withholds_the_next_idea(gate: ThesisGate) -> None:
    slots = tuple(Slot(n, D(125), None, None, "EUR") for n in range(1, 9))
    judged = gate.evaluate(thesis(), facts(slots=slots))
    assert codes(judged.findings) == [Reason.BOOK_FULL.value]
    assert not judged.findings[0].permanent


def test_the_caps_keep_the_book_from_being_one_bet(gate: ThesisGate) -> None:
    tech = instrument(sector="technology", country="US", currency="USD")
    held = (
        Slot(1, D(250), "technology", "US", "USD"),
        Slot(2, D(250), "energy", "US", "USD"),
        Slot(3, D(250), None, "US", "USD"),
    )
    judged = gate.evaluate(thesis(instrument=tech), facts(slots=held))
    # 375 of 1,000 in technology is under the 40 % cap; 875 in the US and in dollars is over 80 %.
    assert codes(judged.findings) == ["country_cap", "currency_cap"]
    more = (*held[:1], Slot(4, D(250), "technology", None, None))
    assert codes(gate.evaluate(thesis(instrument=tech), facts(slots=more)).findings) == [
        "sector_cap"
    ]
    # The base currency is no currency risk, and an unstated sector or country counts nowhere.
    plain = thesis()
    assert gate.evaluate(plain, facts(slots=held)).passed


def test_an_approved_thesis_keeps_its_slot_and_its_size(gate: ThesisGate) -> None:
    full = tuple(Slot(n, D(125), None, None, "EUR") for n in range(2, 10))
    held = approved()
    judged = gate.evaluate(held, facts(slots=full, capital=D(0)))
    assert judged.passed
    assert judged.size_base == D(125)
    # What moves with the market is asked again: thin liquidity now withholds, it does not refuse.
    thin = gate.evaluate(held, facts(daily_value=D(1000)))
    assert codes(thin.findings) == ["illiquid"]
    assert thin.permanent == ()
    assert gate.evaluate(held, facts(reference=D(40))).permanent


def test_the_gain_to_loss_ratio_is_measured_at_the_price() -> None:
    idea = thesis()
    assert gain_to_loss(idea, D(45)) == D(3)
    assert gain_to_loss(idea, D(40)) is None
    assert gain_to_loss(idea, D(60)) is None


# ── the thesis's own exits ──


def test_a_thesis_leaves_by_its_target_its_invalidation_or_its_horizon() -> None:
    idea = approved(state=ThesisState.ACTIVE, opened_at=NOW)
    assert exit_due(idea, D(50), TODAY) is None
    assert exit_due(idea, D(60), TODAY) == "target"
    assert exit_due(idea, D(40), TODAY) == "invalidation"
    assert exit_due(idea, D(50), HORIZON) is None
    assert exit_due(idea, D(50), HORIZON + timedelta(days=1)) == "time_window"
    # No price: a level is never judged, the horizon still ends it.
    assert exit_due(idea, None, TODAY) is None
    assert exit_due(idea, None, HORIZON + timedelta(days=1)) == "time_window"


# ── grading ──


def test_a_closed_thesis_is_graded_against_what_it_wrote() -> None:
    idea = approved(state=ThesisState.ACTIVE, opened_at=NOW)
    later = NOW + timedelta(days=30)
    fills = [fill("BUY", "2", "44"), fill("BUY", "2", "46", n=2), fill("SELL", "4", "39.6", later)]
    graded = grade(idea, fills, exit_reason="invalidation", at=later)
    assert (graded.quantity, graded.entry_price, graded.exit_price) == (D(4), D(45), D("39.6"))
    assert not graded.reached
    assert graded.payoff == D("39.6") / D(45) - 1
    assert graded.stated_payoff == D(60) / D(45) - 1
    assert (graded.days_held, graded.horizon_days) == (30, (HORIZON - NOW.date()).days)
    won = grade(idea, [fills[0], fill("SELL", "2", "59", later)], exit_reason="target", at=later)
    assert won.reached  # its target exit, filled a touch below the level
    high = grade(idea, [fills[0], fill("SELL", "2", "61", later)], exit_reason="stop", at=later)
    assert high.reached


def test_a_thesis_without_a_closed_position_is_not_graded() -> None:
    idea = approved(state=ThesisState.ACTIVE, opened_at=NOW)
    with pytest.raises(ValueError, match="no closed position"):
        grade(idea, [fill("BUY", "2", "44")], exit_reason="target", at=NOW)
    with pytest.raises(ValueError, match="no closed position"):
        grade(
            approved(), [fill("BUY", "2", "44"), fill("SELL", "2", "50")], exit_reason="x", at=NOW
        )


def test_the_record_is_kept_per_source_and_conviction() -> None:
    assert record([]) == []
    idea = approved(state=ThesisState.ACTIVE, opened_at=NOW, conviction=Conviction.HIGH)
    other = approved(thesis_id=2, state=ThesisState.ACTIVE, opened_at=NOW)
    won = grade(
        idea, [fill("BUY", "2", "50"), fill("SELL", "2", "60")], exit_reason="target", at=NOW
    )
    lost = grade(
        other, [fill("BUY", "2", "50"), fill("SELL", "2", "40")], exit_reason="invalidation", at=NOW
    )
    groups = {group.group: group for group in record([won, lost])}
    assert list(groups) == ["book", "source owner", "conviction high", "conviction normal"]
    assert (groups["book"].closed, groups["book"].reached) == (2, 1)
    assert groups["book"].mean_payoff == D(0)
    assert groups["book"].mean_stated_payoff == D("0.2")
    assert groups["conviction high"].mean_payoff == D("0.2")
    assert groups["conviction normal"].reached == 0


def test_a_date_in_the_horizon_past_gives_no_negative_days() -> None:
    idea = approved(state=ThesisState.ACTIVE, opened_at=NOW)
    late = NOW.replace(year=2027, month=6)
    graded = grade(
        idea,
        [fill("BUY", "1", "50"), fill("SELL", "1", "55", late)],
        exit_reason="time_window",
        at=late,
    )
    assert graded.days_held > graded.horizon_days
    assert date(2027, 6, 26) == late.date()
