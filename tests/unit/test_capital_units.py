"""The capital module's arithmetic: managed and investable capital, the sleeve budgets, deposit
tranches, the time-weighted return against a hand calculation, external holdings going stale,
the default strategy value and settlement on a venue's calendar.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from obliskiq.calendars.service import Calendars
from obliskiq.capital.budgets import SleeveBudgets
from obliskiq.capital.deploy import counted_from, held_back
from obliskiq.capital.external import external_book
from obliskiq.capital.managed import CapitalView, book_fills, compose
from obliskiq.capital.strategy import (
    DefaultStrategyValue,
    ExpectedReturn,
    PositionFacts,
    Protection,
)
from obliskiq.capital.twr import (
    ExternalFlow,
    Reading,
    account_flows,
    time_weighted_return,
)
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_runtime import CapitalConfig
from obliskiq.domain.capital import Adjustment, AdjustmentKind, Deposit, ExternalHolding, Withdrawal
from obliskiq.domain.orders import ExecutionMode, Fill, IntentRole, OrderSide, Sleeve
from obliskiq.errors import CalendarCoverageError
from obliskiq.store.orders import IntentFill

D = Decimal
AT = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)


def capital(settings: ObliskConfig, **values: Any) -> ObliskConfig:
    return settings.model_copy(update={"capital": settings.capital.model_copy(update=values)})


# ── managed and investable capital ──


def test_a_capped_book_is_its_holdings_and_its_own_cash(shipped_settings: ObliskConfig) -> None:
    view = compose(
        shipped_settings,
        nav=D(100000),
        book_value=D(600),
        book_cash=D(450),
        unmanaged=D(5000),
        reserved=D(300),
    )
    assert (view.managed, view.book_cash) == (D(1050), D(450))  # gains above the start kept
    assert view.investable == D(750)  # managed less reserved
    assert view.unfunded == D(0)
    assert "a book of 1050.00" in view.detail


def test_a_capped_book_never_manages_more_than_the_account_outside_unmanaged(
    shipped_settings: ObliskConfig,
) -> None:
    view = compose(
        shipped_settings,
        nav=D(900),
        book_value=D(600),
        book_cash=D(450),
        unmanaged=D(200),
        reserved=D(0),
    )
    assert view.managed == D(700)  # 900 less the unmanaged 200
    assert view.book_cash == D(100)
    assert "outside unmanaged holdings" in view.detail


def test_the_whole_account_manages_the_nav_less_what_no_sleeve_owns(
    shipped_settings: ObliskConfig,
) -> None:
    whole = capital(shipped_settings, mode="whole_account", cap_base=None)
    view = compose(
        whole, nav=D(20000), book_value=D(8000), book_cash=None, unmanaged=D(5000), reserved=D(1000)
    )
    assert view.managed == D(20000)
    assert view.investable == D(14000)  # 20,000 less 5,000 unmanaged less 1,000 reserved
    assert view.book_cash == D(7000)
    assert view.unfunded == D(0)
    short = compose(
        whole, nav=D(10000), book_value=D(9900), book_cash=None, unmanaged=D(0), reserved=D(500)
    )
    assert short.unfunded == D(400)  # 100 of cash against 500 reserved
    assert compose(
        whole, nav=D(10), book_value=D(0), book_cash=None, unmanaged=D(0), reserved=D(50)
    ).investable == D(0)


def test_a_view_finds_its_holdings() -> None:
    view = CapitalView("cap", D(1), D(1), D(0), D(0), D(1), D(0), D(1), (), "d")
    assert view.holding(1) is None


def test_the_book_in_shadow_is_its_simulated_fills_and_on_paper_the_brokers() -> None:
    def entry(mode: ExecutionMode, simulated: bool) -> IntentFill:
        fill = Fill(
            execution_id=f"{mode}-{simulated}",
            coid="c",
            conid=1,
            side=OrderSide.BUY,
            quantity=D(1),
            price=D(1),
            commission=None,
            executed_at=AT,
            simulated=simulated,
        )
        return IntentFill(fill, Sleeve.CORE, mode, IntentRole.ENTRY, "rebalance")

    fills = [entry(ExecutionMode.SHADOW, True), entry(ExecutionMode.PAPER, False)]
    assert [f.mode for f in book_fills(fills, ExecutionMode.SHADOW)] == [ExecutionMode.SHADOW]
    assert [f.mode for f in book_fills(fills, ExecutionMode.PAPER)] == [ExecutionMode.PAPER]


# ── budgets ──


def test_an_inactive_sleeves_budget_stays_in_the_core(shipped_settings: ObliskConfig) -> None:
    budgets = SleeveBudgets(shipped_settings.capital)
    alone = budgets.allocate(D(1000), active={Sleeve.CORE})
    assert (alone.core, alone.satellite, alone.options, alone.cash_floor) == (
        D(950),
        D(0),
        D(0),
        D(50),
    )
    every = budgets.allocate(D(1000), active=set(Sleeve))
    assert (every.core, every.satellite, every.options) == (D(700), D(200), D(50))
    assert every.of(Sleeve.SATELLITE) == D(200)
    assert every.of(Sleeve.OPTIONS) == D(50)
    assert budgets.satellite_ceiling == D("0.20")
    with pytest.raises(ValueError, match="always active"):
        budgets.allocate(D(1), active={Sleeve.SATELLITE})


def test_budgets_and_the_ceiling_are_checked(shipped_settings: ObliskConfig) -> None:
    data = shipped_settings.capital.model_dump()
    over = data | {"budgets": data["budgets"] | {"core": "0.80"}}
    with pytest.raises(ValueError, match="more than the whole book"):
        CapitalConfig.model_validate(over)
    with pytest.raises(ValueError, match="below the equity satellite"):
        CapitalConfig.model_validate(data | {"satellite_ceiling": "0.10"})
    with pytest.raises(ValueError, match="leaves less than"):
        CapitalConfig.model_validate(data | {"satellite_ceiling": "0.30"})
    with pytest.raises(ValueError, match="rank overweight"):
        CapitalConfig.model_validate(
            data
            | {
                "planner": data["planner"]
                | {"strategy_cost": {"overweight": "0.1", "ordinary": "0", "protected": "0"}}
            }
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        CapitalConfig.model_validate(
            data | {"pacing": data["pacing"] | {"weak_below": "0.5", "strong_above": "0.1"}}
        )


# ── deposits deployed in tranches ──


def _deposit(amount: str, on: date, **kw: Any) -> Deposit:
    return Deposit(
        amount=D(amount), currency="EUR", on=on, recorded_at=AT, recorded_by="owner", **kw
    )


def test_a_deposit_is_released_in_equal_daily_tranches(shipped_settings: ObliskConfig) -> None:
    three = capital(
        shipped_settings,
        deployment=shipped_settings.capital.deployment.model_copy(update={"tranche_days": 3}),
    ).capital
    day = date(2026, 10, 21)
    flows = [_deposit("900", day)]
    assert held_back(flows, three, today=day) == {"EUR": D(600)}  # a third released
    assert held_back(flows, three, today=day + timedelta(days=1)) == {"EUR": D(300)}
    assert held_back(flows, three, today=day + timedelta(days=2)) == {}
    assert held_back(flows, three, today=day - timedelta(days=1)) == {}  # not yet counted
    at_once = shipped_settings.capital
    assert held_back(flows, at_once, today=day) == {}
    # A cancelled deposit, one outside the book and a withdrawal hold nothing back.
    cancelled = _deposit("100", day, cancelled_at=AT)
    outside = _deposit("100", day, in_book=False)
    out = Withdrawal(amount=D(5), currency="EUR", on=day, recorded_at=AT, recorded_by="owner")
    assert held_back([cancelled, outside, out], three, today=day) == {}


def test_the_whole_account_counts_a_deposit_once_the_broker_shows_it(
    shipped_settings: ObliskConfig,
) -> None:
    whole = capital(shipped_settings, mode="whole_account", cap_base=None).capital
    day = date(2026, 10, 21)
    assert counted_from(_deposit("10", day), whole) is None
    shown = _deposit("10", day, matched_at=AT + timedelta(days=2))
    assert counted_from(shown, whole) == day + timedelta(days=2)
    assert counted_from(_deposit("10", day), shipped_settings.capital) == day


# ── the time-weighted return ──


def test_twr_with_flows_equals_a_hand_calculation() -> None:
    days = [date(2026, 10, d) for d in (1, 2, 3, 4)]
    readings = [Reading(days[0], D(1000)), Reading(days[1], D(1100)), Reading(days[2], D(1155)),
                Reading(days[3], D(1039.5))]  # fmt: skip
    flows = [ExternalFlow(days[1], D(50)), ExternalFlow(days[3], D(-100))]
    result = time_weighted_return(readings, flows, timing="in_reading")
    # (1100 - 50) / 1000 = 1.05; 1155 / 1100 = 1.05; (1039.5 + 100) / 1155 = 0.98658...
    expected = D(1050) / D(1000) * (D(1155) / D(1100)) * (D("1139.5") / D(1155)) - 1
    assert result.value == expected
    assert [p.flow for p in result.periods] == [D(50), D(0), D(-100)]
    assert (result.since, result.until) == (days[0], days[3])
    # The same deposit, read as arriving after the day's reading, starts the next period.
    after = time_weighted_return(
        [Reading(days[0], D(1000)), Reading(days[1], D(1050)), Reading(days[2], D(1155))],
        [ExternalFlow(days[1], D(50))],
        timing="after_reading",
    )
    assert after.value == D(1050) / D(1000) * (D(1155) / D(1100)) - 1
    # A flow is never a return: a deposit alone leaves the return at zero.
    flat = time_weighted_return(
        [Reading(days[0], D(1000)), Reading(days[1], D(1500))],
        [ExternalFlow(days[1], D(500))],
        timing="in_reading",
    )
    assert flat.value == 0


def test_a_twr_that_cannot_be_measured_says_why() -> None:
    day = date(2026, 10, 1)
    assert time_weighted_return([Reading(day, D(1))], [], timing="in_reading").reason
    broken = time_weighted_return(
        [Reading(day, D(100)), Reading(day + timedelta(days=1), D(10))],
        [ExternalFlow(day, D(-100))],
        timing="after_reading",
    )
    assert broken.value is None
    assert "no positive base" in str(broken.reason)


def test_only_the_owners_flows_that_reached_the_account_are_external() -> None:
    day = date(2026, 10, 2)
    shown = _deposit("100", day, matched_at=AT)
    unseen = _deposit("50", day)
    out = Withdrawal(
        amount=D(20), currency="USD", on=day, recorded_at=AT, recorded_by="o", matched_at=AT
    )
    dividend = Adjustment(
        category=AdjustmentKind.DIVIDEND,
        amount=D(3),
        currency="EUR",
        on=day,
        recorded_at=AT,
        recorded_by="o",
        matched_at=AT,
    )
    flows = account_flows(
        [shown, unseen, out, dividend], base="EUR", rate=lambda _currency, _on: D("0.9")
    )
    assert flows == [ExternalFlow(day, D(100)), ExternalFlow(day, D(-18))]
    missing = account_flows([out], base="EUR", rate=lambda _currency, _on: None)
    assert missing == "no USD rate was recorded on 2026-10-02"


# ── external holdings (lite's external_positions) ──


def _external(symbol: str, last_updated: date, value: str = "1000") -> ExternalHolding:
    return ExternalHolding(
        broker="DEGIRO",
        symbol=symbol,
        quantity=D(3),
        currency="EUR",
        market_value_base=D(value),
        last_updated=last_updated,
    )


def test_a_stale_external_holding_makes_the_book_unreliable(shipped_settings: ObliskConfig) -> None:
    stale_days = shipped_settings.positions.external_data_stale_days
    today = date(2026, 10, 21)
    fresh = _external("ASML", today)
    old = _external("VWCE", today - timedelta(days=stale_days + 5), "5000")
    book = external_book([fresh, old], shipped_settings.positions, as_of=today)
    assert book.total_base == D(6000)
    assert not book.reliable
    assert book.stale == ("DEGIRO:VWCE",)
    assert str(stale_days) in str(book.reason())
    assert book.label(fresh) == "DEGIRO:ASML"
    clean = external_book([fresh], shipped_settings.positions, as_of=today)
    assert clean.reliable
    assert clean.reason() is None


# ── the default strategy value ──


def test_an_entry_window_protects_a_thesis_but_not_a_core_line(
    shipped_settings: ObliskConfig,
) -> None:
    value = DefaultStrategyValue(shipped_settings.capital)
    days = shipped_settings.capital.planner.entry_window_days
    today = date(2026, 10, 21)
    fresh = PositionFacts(1, Sleeve.SATELLITE, D(10), today - timedelta(days=days - 1))
    protected = value.protection(fresh, today=today)
    assert protected is not None
    assert protected.why is Protection.ENTRY_WINDOW
    assert protected.until == today + timedelta(days=1)
    old = PositionFacts(1, Sleeve.SATELLITE, D(10), today - timedelta(days=days))
    assert value.protection(old, today=today) is None
    core = PositionFacts(2, Sleeve.CORE, D(10), today)
    assert value.protection(core, today=today) is None
    unknown = PositionFacts(3, Sleeve.OPTIONS, D(1), None)
    assert value.protection(unknown, today=today) is None
    assert not value.would_exit(core, today=today)
    assert value.expected_return(core, today=today) is None
    assert ExpectedReturn(D("0.2"), "sufficient", "h1").usable
    assert not ExpectedReturn(D("0.2"), "inconclusive", "h1").usable


# ── settlement on the venue's calendar ──


def test_settlement_counts_the_venues_sessions_across_a_holiday(
    build_calendars: Callable[..., Calendars],
) -> None:
    calendars = build_calendars(("XNYS", "XAMS"))
    # New York settles T+1: a Wednesday trade before Thanksgiving settles on Friday.
    assert calendars.settlement_date("XNYS", date(2026, 11, 25)) == date(2026, 11, 27)
    # Amsterdam settles T+2: the 23rd of December settles after Christmas, on the 28th.
    assert calendars.settlement_date("XAMS", date(2026, 12, 23)) == date(2026, 12, 28)
    # The last session whose trades settle by the 27th of November: the 25th.
    assert calendars.last_safe_session(
        "XNYS", date(2026, 11, 27), from_day=date(2026, 11, 20)
    ) == date(2026, 11, 25)
    assert (
        calendars.last_safe_session("XNYS", date(2026, 11, 27), from_day=date(2026, 11, 26)) is None
    )


def test_a_settlement_beyond_the_calendar_is_never_assumed(
    build_calendars: Callable[..., Calendars],
) -> None:
    calendars = build_calendars(("XAMS",))
    end = calendars.coverage()["XAMS"]
    assert end is not None
    with pytest.raises(CalendarCoverageError):
        calendars.settlement_date("XAMS", end[1])
