"""The promotion ladder L0 to L3 (MASTER_PLAN §5.19), transaction-cost analysis and the
performance arithmetic: TWR, the references, each sleeve's profit and the attribution.

Promotion never passes on missing data: an unmeasured criterion fails its level.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from obliskiq.capital.twr import ExternalFlow, Reading
from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import ExecutionMode, Fill, IntentRole, ModeChange, OrderSide, Sleeve
from obliskiq.scoreboard.performance import (
    SleevePnl,
    attribution,
    nav_index,
    reference_index,
    sleeve_pnl,
)
from obliskiq.scoreboard.promotion import (
    MONTHLY_REPORT_COMMAND,
    RELOGIN,
    Criterion,
    Evidence,
    RunOutcome,
    evaluate,
    mode_since,
    relogins,
)
from obliskiq.scoreboard.tca import Execution, analyse, measure
from obliskiq.store.orders import IntentFill
from obliskiq.store.scoreboard import Drill
from obliskiq.store.session_events import SessionEvent

D = Decimal
NOW = datetime(2027, 3, 1, 12, tzinfo=UTC)


def change(to: ExecutionMode, at: datetime) -> ModeChange:
    lower = {
        ExecutionMode.PAPER: ExecutionMode.SHADOW,
        ExecutionMode.LIVE_SMALL: ExecutionMode.PAPER,
        ExecutionMode.LIVE: ExecutionMode.LIVE_SMALL,
    }[to]
    return ModeChange(
        at=at, from_mode=lower, to_mode=to, direction="upgrade", actor="o", reason="r"
    )


def execution(price: str, arrival: str | None = "100", modelled: str | None = "0.002") -> Execution:
    return Execution(
        executed_at=NOW,
        mode=ExecutionMode.PAPER,
        venue="IBIS2",
        order_type="limit",
        quote_status="realtime",
        side=OrderSide.BUY,
        quantity=D(10),
        price=D(price),
        commission=D(1),
        arrival=None if arrival is None else D(arrival),
        modelled=None if modelled is None else D(modelled),
    )


def evidence(mode: ExecutionMode, since: datetime, **changes: object) -> Evidence:
    days = [since.date() + timedelta(days=i) for i in range((NOW - since).days + 1)]
    sessions = tuple(d for d in days if d.weekday() < 5)
    base = Evidence(
        now=NOW,
        mode=mode,
        changes=(change(mode, since),)
        if mode is not ExecutionMode.SHADOW
        else (
            ModeChange(
                at=since,
                from_mode=ExecutionMode.PAPER,
                to_mode=ExecutionMode.SHADOW,
                direction="downgrade",
                actor="o",
                reason="r",
            ),
        ),
        sessions=sessions,
        runs=tuple(RunOutcome(d, "cycle:eu", "ok") for d in sessions),
        reconciliations=tuple(
            (datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=20), "green")
            for d in sessions
        ),
        stuck_intents=0,
        session_events=tuple(
            SessionEvent(
                datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=4, minutes=1),
                "reauthenticated",
                "the brokerage session was lost",
            )
            for d in days
        ),
        placed=30,
        filled=29,
        fills_without_clearance=0,
        tca=analyse([execution("100.1") for _ in range(25)]),
        drills=tuple(
            Drill(since + timedelta(days=3), kind, ExecutionMode.PAPER, True, "o", "done")
            for kind in ("breaker", "kill", "cash_flow")
        ),
        capped_book=True,
        hard_breaker_tripped=False,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# ── L1 paper ──


def test_l1_passes_with_ten_clean_trading_days_of_shadow(shipped_settings: ObliskConfig) -> None:
    found = evaluate(
        ExecutionMode.PAPER,
        evidence(ExecutionMode.SHADOW, NOW - timedelta(days=16)),
        shipped_settings.promotion,
    )
    assert found.passed, found.summary()
    assert "every criterion is met" in found.summary()


@pytest.mark.parametrize(
    ("changes", "failing"),
    [
        ({"session_events": ()}, "re-logged"),
        ({"stuck_intents": 1}, "stuck"),
        ({"runs": ()}, "unhandled error"),
        ({"reconciliations": ()}, "clean reconciliation"),
        ({"hard_breaker_tripped": True}, "hard breaker"),
    ],
)
def test_l1_cannot_pass_on_missing_data_or_a_fault(
    shipped_settings: ObliskConfig, changes: dict[str, object], failing: str
) -> None:
    found = evaluate(
        ExecutionMode.PAPER,
        evidence(ExecutionMode.SHADOW, NOW - timedelta(days=16), **changes),
        shipped_settings.promotion,
    )
    assert not found.passed
    assert failing in found.summary()


def _relogin(found_events: tuple[SessionEvent, ...], since: datetime) -> Criterion:
    found = relogins(evidence(ExecutionMode.SHADOW, since, session_events=found_events), since)
    assert found.name == RELOGIN
    return found


def test_l1_reads_the_recorded_re_logins() -> None:
    since = NOW - timedelta(days=16)
    every_day = evidence(ExecutionMode.SHADOW, since).session_events
    assert _relogin(every_day, since).passed is True
    # A reset that falls the evening before in UTC (Europe's midnight) still counts.
    evening = tuple(replace(e, at=e.at - timedelta(hours=6)) for e in every_day)
    assert _relogin(evening, since).passed is True
    # A trading day with no re-login on it or the day before fails the criterion.
    gap = tuple(e for e in every_day if e.at.date() not in {date(2027, 2, 24), date(2027, 2, 25)})
    missed = _relogin(gap, since)
    assert missed.passed is False
    assert "2027-02-25" in missed.detail
    # A failed re-login left without a successful one after it fails it.
    failed = (*every_day, SessionEvent(NOW - timedelta(hours=1), "reauthentication_failed", "x"))
    unrepaired = _relogin(failed, since)
    assert unrepaired.passed is False
    assert "not repaired" in unrepaired.detail
    repaired = (*failed, SessionEvent(NOW - timedelta(minutes=5), "reauthenticated", "x"))
    assert _relogin(repaired, since).passed is True
    # Nothing recorded, or no whole trading day yet, is unmeasured: never passed.
    assert _relogin((), since).passed is None
    assert _relogin(every_day, NOW - timedelta(hours=2)).passed is None
    assert relogins(evidence(ExecutionMode.SHADOW, since), None).passed is None


def test_l1_needs_a_dated_entry_and_ten_sessions(shipped_settings: ObliskConfig) -> None:
    short = evaluate(
        ExecutionMode.PAPER,
        evidence(ExecutionMode.SHADOW, NOW - timedelta(days=5)),
        shipped_settings.promotion,
    )
    assert not short.passed
    assert "10 trading days" in short.summary()
    undated = evaluate(
        ExecutionMode.PAPER,
        evidence(ExecutionMode.SHADOW, NOW - timedelta(days=16), changes=()),
        shipped_settings.promotion,
    )
    assert not undated.passed
    assert any(c.passed is None for c in undated.criteria)
    errors = evaluate(
        ExecutionMode.PAPER,
        evidence(
            ExecutionMode.SHADOW,
            NOW - timedelta(days=16),
            runs=(RunOutcome(NOW.date(), "cycle:eu", "error"),),
        ),
        shipped_settings.promotion,
    )
    assert "a run every trading day" in errors.summary()
    assert "unhandled error" in errors.summary()


# ── L2 live-small ──


def test_l2_passes_after_eight_clean_weeks_of_paper(shipped_settings: ObliskConfig) -> None:
    found = evaluate(
        ExecutionMode.LIVE_SMALL,
        evidence(ExecutionMode.PAPER, NOW - timedelta(weeks=9)),
        shipped_settings.promotion,
    )
    assert found.passed, found.summary()


@pytest.mark.parametrize(
    ("changes", "failing"),
    [
        ({"fills_without_clearance": 1}, "no rail violation"),
        ({"placed": 30, "filled": 20}, "order success"),
        ({"placed": 5, "filled": 5}, "order success"),
        ({"tca": None}, "slippage"),
        ({"tca": analyse([execution("100.1")] * 5)}, "slippage"),
        ({"tca": analyse([execution("101")] * 25)}, "slippage"),
        ({"drills": ()}, "breaker drill"),
        ({"capped_book": False}, "a capped book"),
    ],
)
def test_l2_cannot_pass_on_missing_data_or_a_fault(
    shipped_settings: ObliskConfig, changes: dict[str, object], failing: str
) -> None:
    found = evaluate(
        ExecutionMode.LIVE_SMALL,
        evidence(ExecutionMode.PAPER, NOW - timedelta(weeks=9), **changes),
        shipped_settings.promotion,
    )
    assert not found.passed
    assert failing in found.summary()


def test_l2_reads_only_the_latest_drill_of_each_kind_in_paper(
    shipped_settings: ObliskConfig,
) -> None:
    since = NOW - timedelta(weeks=9)
    drills = (
        Drill(since + timedelta(days=1), "kill", ExecutionMode.PAPER, True, "o", "ok"),
        Drill(
            since + timedelta(days=2), "kill", ExecutionMode.PAPER, False, "o", "the switch failed"
        ),
        Drill(since + timedelta(days=1), "breaker", ExecutionMode.SHADOW, True, "o", "ok"),
        Drill(since - timedelta(days=1), "cash_flow", ExecutionMode.PAPER, True, "o", "before"),
    )
    found = evaluate(
        ExecutionMode.LIVE_SMALL,
        evidence(ExecutionMode.PAPER, since, drills=drills),
        shipped_settings.promotion,
    )
    marks = {c.name: c.passed for c in found.criteria}
    assert marks["the kill drill"] is False
    assert marks["the breaker drill"] is None
    assert marks["the cash-flow drill"] is None


def test_l2_needs_eight_weeks(shipped_settings: ObliskConfig) -> None:
    found = evaluate(
        ExecutionMode.LIVE_SMALL,
        evidence(ExecutionMode.PAPER, NOW - timedelta(weeks=3)),
        shipped_settings.promotion,
    )
    assert not found.passed
    assert "8 weeks of paper" in found.summary()
    undated = evaluate(
        ExecutionMode.LIVE_SMALL,
        evidence(ExecutionMode.PAPER, NOW - timedelta(weeks=9), changes=()),
        shipped_settings.promotion,
    )
    assert not undated.passed


# ── L3 live ──


def test_l3_needs_three_months_of_live_small_and_a_monthly_report(
    shipped_settings: ObliskConfig,
) -> None:
    since = NOW - timedelta(days=100)
    report = RunOutcome(NOW.date() - timedelta(days=20), MONTHLY_REPORT_COMMAND, "ok")
    base = evidence(ExecutionMode.LIVE_SMALL, since)
    found = evaluate(
        ExecutionMode.LIVE, replace(base, runs=(*base.runs, report)), shipped_settings.promotion
    )
    assert found.passed, found.summary()
    no_report = evaluate(ExecutionMode.LIVE, base, shipped_settings.promotion)
    assert not no_report.passed
    assert "monthly scoreboard report" in no_report.summary()
    young = evaluate(
        ExecutionMode.LIVE,
        evidence(ExecutionMode.LIVE_SMALL, NOW - timedelta(days=40)),
        shipped_settings.promotion,
    )
    assert not young.passed
    assert "3 months" in young.summary()
    undated = evaluate(ExecutionMode.LIVE, replace(base, changes=()), shipped_settings.promotion)
    assert not undated.passed


def test_the_ladder_is_climbed_one_level_at_a_time(shipped_settings: ObliskConfig) -> None:
    skipped = evaluate(
        ExecutionMode.LIVE_SMALL,
        evidence(ExecutionMode.SHADOW, NOW - timedelta(weeks=20)),
        shipped_settings.promotion,
    )
    assert not skipped.passed
    assert "one level at a time" in skipped.criteria[0].detail
    bottom = evaluate(
        ExecutionMode.SHADOW,
        evidence(ExecutionMode.SHADOW, NOW - timedelta(weeks=20)),
        shipped_settings.promotion,
    )
    assert not bottom.passed
    assert mode_since([], ExecutionMode.PAPER) is None


# ── transaction-cost analysis ──


def test_tca_by_hand_and_by_group() -> None:
    found = measure(execution("100.5"))
    assert not isinstance(found, str)
    assert found.realised == pytest.approx(0.005 + 1 / 1005)
    assert found.modelled == pytest.approx(0.002 + 1 / 1005)
    assert found.excess == pytest.approx(0.003)
    sell = measure(replace(execution("99.5"), side=OrderSide.SELL, commission=None))
    assert not isinstance(sell, str)
    assert sell.realised == pytest.approx(0.005)
    assert measure(execution("100", arrival=None)) == "no arrival quote"
    assert "cost model" in str(measure(execution("100", modelled=None)))
    report = analyse(
        [
            execution("100.5"),
            execution("100.3"),
            replace(execution("100.2"), venue="ARCA"),
            execution("100", arrival=None),
        ]
    )
    assert report.fills == 3
    assert report.unmeasured == {"no arrival quote": 1}
    arca, ibis = report.groups
    assert arca.venue == "ARCA"
    assert arca.interval is None
    assert ibis.fills == 2
    assert ibis.excess == pytest.approx(0.002)
    assert ibis.interval is not None
    assert ibis.interval[0] < 0.002 < ibis.interval[1]
    assert report.excess == pytest.approx((0.003 + 0.001 + 0.0) / 3)
    empty = analyse([])
    assert empty.excess is None
    assert empty.interval is None


# ── performance arithmetic ──


def test_a_twr_index_neutralises_flows() -> None:
    readings = [
        Reading(date(2027, 1, 1), D(1000)),
        Reading(date(2027, 1, 2), D(1600)),
        Reading(date(2027, 1, 3), D(1650)),
    ]
    index = nav_index(readings, [ExternalFlow(date(2027, 1, 2), D(500))], timing="in_reading")
    assert [p.value for p in index.points] == [D(1), D("1.1"), D("1.1") * D(1650) / D(1600)]
    assert index.growth == D("1.1") * D(1650) / D(1600) - 1
    lonely = nav_index(readings[:1], [], timing="in_reading")
    assert lonely.growth is None
    assert lonely.reason is not None


def test_a_reference_mix_rebalances_at_each_month_end() -> None:
    days = [date(2027, 1, 30), date(2027, 1, 31), date(2027, 2, 1), date(2027, 2, 2)]
    prices = {
        1: {days[0]: D(100), days[1]: D(110), days[2]: D(110), days[3]: D(121)},
        2: {days[0]: D(100), days[1]: D(100), days[2]: D(100), days[3]: D(100)},
    }

    def price(conid: int, on: date) -> Decimal | None:
        return prices[conid].get(on)

    index = reference_index("mix", {1: D("0.5"), 2: D("0.5")}, days, price)
    values = [p.value for p in index.points]
    assert values[1] == D("1.05")
    # Rebalanced to half and half at 1.05 on 31 January: half of it rises 10 % on 2 February.
    assert values[3] == D("1.05") * D("1.05")
    missing = reference_index(
        "mix", {1: D("0.5"), 3: D("0.5")}, days, lambda c, d: price(c, d) if c != 3 else None
    )
    assert missing.points == ()
    assert "no price of 3" in (missing.reason or "")
    gap = dict(prices[2])
    del gap[days[2]]
    stops = reference_index(
        "mix", {1: D("0.5"), 2: D("0.5")}, days, lambda c, d: (prices[1] if c == 1 else gap).get(d)
    )
    assert len(stops.points) == 2
    assert stops.reason is not None
    assert reference_index("mix", {1: D(1)}, [], price).reason == "no day to measure"


def fill(side: OrderSide, quantity: str, price: str, day: int, conid: int = 1) -> IntentFill:
    return IntentFill(
        Fill(
            execution_id=f"{side.value}{day}{conid}",
            coid=None,
            conid=conid,
            side=side,
            quantity=D(quantity),
            price=D(price),
            commission=D(1),
            executed_at=datetime(2027, 1, day, 15, tzinfo=UTC),
        ),
        Sleeve.SATELLITE,
        ExecutionMode.PAPER,
        IntentRole.ENTRY,
        "r",
    )


def test_each_sleeve_s_profit_by_average_cost() -> None:
    fills = [
        fill(OrderSide.BUY, "10", "100", 1),
        fill(OrderSide.BUY, "10", "110", 2),
        fill(OrderSide.SELL, "5", "120", 3),
        fill(OrderSide.BUY, "1", "50", 4, conid=2),
        fill(OrderSide.BUY, "2", "10", 5, conid=3),
        fill(OrderSide.SELL, "2", "12", 6, conid=3),
        fill(OrderSide.BUY, "1", "50", 20),
    ]
    found = sleeve_pnl(
        fills,
        currency=lambda _conid: "USD",
        multiplier=lambda _conid: D(1),
        rate=lambda _code, _on: D("0.5"),
        price=lambda conid, _on: D(60) if conid == 1 else None,
        on=date(2027, 1, 10),
    )
    assert not isinstance(found, str)
    (satellite,) = found
    cost = (D(1000) * D("0.5") + D("0.5")) + (D(1100) * D("0.5") + D("0.5"))
    basis = cost * 5 / 20
    round_trip = (D(24) * D("0.5") - D("0.5")) - (D(20) * D("0.5") + D("0.5"))
    assert satellite.realised == D(600) * D("0.5") - D("0.5") - basis + round_trip
    assert satellite.held_value == D(15) * D(60)
    assert satellite.unrealised == satellite.held_value - (cost - basis)
    assert satellite.unmarked == (2,)
    assert satellite.commissions == D(3)
    assert (
        sleeve_pnl(
            fills,
            currency=lambda _c: None,
            multiplier=lambda _c: D(1),
            rate=lambda _a, _b: D(1),
            price=lambda _a, _b: None,
            on=date(2027, 1, 10),
        )
        == "instrument 1 has no currency"
    )
    norate = sleeve_pnl(
        fills,
        currency=lambda _c: "USD",
        multiplier=lambda _c: D(1),
        rate=lambda _a, _b: None,
        price=lambda _a, _b: None,
        on=date(2027, 1, 10),
    )
    assert isinstance(norate, str)
    assert "USD rate" in norate


def test_attribution_leaves_the_rest_to_cash() -> None:
    start = [SleevePnl(Sleeve.CORE, D(0), D(10), D(110), D(0), ())]
    end = [
        SleevePnl(Sleeve.CORE, D(5), D(20), D(120), D(0), ()),
        SleevePnl(Sleeve.SATELLITE, D(0), D(3), D(30), D(0), ()),
    ]
    found = attribution(D(1000), D(1150), D(100), start, end)
    assert found.sleeves == {Sleeve.CORE: D(15), Sleeve.SATELLITE: D(3)}
    assert found.cash == D(150) - D(100) - D(18)
