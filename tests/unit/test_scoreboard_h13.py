"""H13's record (research/preregistrations/thesis-book.md; DECISIONS D156, D173): the book and
its twin after tax at liquidation, x_m by hand, the locked boundary table, a look not evaluable on
a stale mark, a step and a return to shadow, and the holdbacks the decision path reads.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid7

import pytest
from tests.support.core_rig import seed_closes, seed_vehicle
from tests.support.thesis_rig import EMIM_CONID, thesis

from obliskiq.capital.allocation import THESIS_BOOK
from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import (
    ExecutionMode,
    Fill,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderIntent,
    OrderSide,
    OrderTerms,
    Sleeve,
    TimeInForce,
)
from obliskiq.domain.tax import SecurityIdentity, SecurityKind
from obliskiq.domain.thesis import Conviction, ThesisSource, ThesisState
from obliskiq.errors import StoreError
from obliskiq.jurisdiction import load_profile
from obliskiq.jurisdiction.tax_es_irpf import EsIrpfTaxPolicy
from obliskiq.research.prereg import ForwardBookPrereg, ForwardBookSpec, load_any
from obliskiq.scoreboard.h13 import (
    Account,
    MonthX,
    Trade,
    TwinInputs,
    closed_theses,
    critical,
    judge,
    month_end,
    months_from,
    profit,
    series,
    thesis_profits,
    twin_trades,
)
from obliskiq.scoreboard.h13_run import look_number, look_rules, run_h13
from obliskiq.scoreboard.run import run_month
from obliskiq.store import orders as order_repo
from obliskiq.store import theses as thesis_repo
from obliskiq.store.db import Store
from obliskiq.store.h13 import (
    BookCapital,
    H13Look,
    book_capital,
    capital_before,
    h13_looks,
    holdbacks,
    record_book_capital,
    record_h13_look,
)
from obliskiq.store.scoreboard import current_shares

D = Decimal
REFERENCE = 163606916
AT = datetime(2027, 7, 2, 22, tzinfo=UTC)

AAA = SecurityIdentity(
    key="AAA", kind=SecurityKind.SHARE, isin="US0378331005", issuer=None, tracked_index=None,
    listed=True,
)  # fmt: skip
REF = SecurityIdentity(
    key="REF", kind=SecurityKind.ETF, isin="IE00BJ0KDQ92", issuer="Xtrackers",
    tracked_index="MSCI World", listed=True,
)  # fmt: skip


@pytest.fixture(scope="module")
def es(repo_root: Path) -> EsIrpfTaxPolicy:
    profile = load_profile(repo_root / "config", "es")
    assert profile.tax is not None
    return EsIrpfTaxPolicy(profile.tax)


@pytest.fixture(scope="module")
def h13(repo_root: Path) -> ForwardBookSpec:
    found = load_any(repo_root / "research" / "preregistrations" / "thesis-book.md")
    assert isinstance(found, ForwardBookPrereg)
    return found.spec


def _values(table: Mapping[tuple[str, date], str]):  # type: ignore[no-untyped-def]
    def value(security: str, on: date) -> Decimal | None:
        found = table.get((security, on))
        return None if found is None else D(found)

    return value


# ── the accounts and x_m by hand ──


def test_x_m_by_hand_after_tax_against_the_twin(es: EsIrpfTaxPolicy) -> None:
    """One thesis: 10 AAA bought for 1,000 plus 2 of commission on 10 January, marked at 110 on
    31 January, sold for 1,150 less 2 on 15 February. The twin spends the same 1,002 on the
    reference at 50 with a cost of 1 a leg: 20.02 units, marked at 51, sold at 52. K is 10,000.
    The es profile taxes a gain at 19 % up to 6,000."""
    book = (
        Trade(1, "AAA", "buy", date(2027, 1, 10), D(10), D(1000), D(2)),
        Trade(1, "AAA", "sell", date(2027, 2, 15), D(10), D(1150), D(2)),
    )
    closes = {date(2027, 1, 10): D(50), date(2027, 2, 15): D(52)}
    twin = twin_trades(
        book, TwinInputs("REF", closes.get, lambda _units, _close, _side, _day: D(1))
    )
    assert not isinstance(twin, str)
    assert [(t.side, t.quantity, t.value, t.costs) for t in twin] == [
        ("buy", D("20.02"), D("1001.00"), D(1)),
        ("sell", D("20.02"), D("1041.04"), D(1)),
    ]
    value = _values(
        {
            ("AAA", date(2027, 1, 31)): "110",
            ("REF", date(2027, 1, 31)): "51",
            ("REF", date(2027, 2, 28)): "52",
        }
    )
    mine = Account(book, {"AAA": AAA}, es)
    theirs = Account(tuple(twin), {"REF": REF}, es)
    # January: the book's gain at its mark is 1,100 - 1,002 = 98, taxed 18.62.
    assert profit(mine, date(2027, 1, 31), value) == D("79.38")
    # The twin's: 20.02 x 51 - 1,002 = 19.02, taxed 3.6138.
    assert profit(theirs, date(2027, 1, 31), value) == D("15.4062")
    # February: the book realised 1,148 - 1,002 = 146, taxed 27.74; the twin 38.04, taxed 7.2276.
    assert profit(mine, date(2027, 2, 28), value) == D("118.26")
    assert profit(theirs, date(2027, 2, 28), value) == D("30.8124")
    xs = series(
        ["2027-01", "2027-02"],
        mine,
        theirs,
        capital=lambda _m: D(10000),
        value=value,
        reference="REF",
    )
    assert xs[0].x == pytest.approx(math.log(1.007938) - math.log(1.00154062))
    assert xs[1].x == pytest.approx(math.log(1.003888) - math.log(1.00154062))
    assert closed_theses(book, date(2027, 1, 31)) == set()
    assert closed_theses(book, date(2027, 2, 28)) == {1}
    assert thesis_profits(book, date(2027, 1, 31), value) == {1: D(98)}


def test_a_month_that_cannot_be_measured_says_why(es: EsIrpfTaxPolicy) -> None:
    book = (Trade(1, "AAA", "buy", date(2027, 1, 10), D(10), D(1000), D(2)),)
    twin = (Trade(1, "REF", "buy", date(2027, 1, 10), D(20), D(1000), D(2)),)
    fresh = {("REF", date(2027, 1, 31)): "51", ("AAA", date(2027, 1, 31)): "100"}
    run = {
        "stale mark": (fresh | {("AAA", date(2027, 1, 31)): ""}, {"AAA": AAA}, D(10000)),
        "no reference": ({("AAA", date(2027, 1, 31)): "100"}, {"AAA": AAA}, D(10000)),
        "no tax facts": (fresh, {}, D(10000)),
        "no capital": (fresh, {"AAA": AAA}, None),
    }
    whys: dict[str, str | None] = {}
    for name, (table, ids, k) in run.items():
        cleaned = {key: v for key, v in table.items() if v}
        (found,) = series(
            ["2027-01"],
            Account(book, ids, es),
            Account(twin, {"REF": REF}, es),
            capital=lambda _m, k=k: k,
            value=_values(cleaned),
            reference="REF",
        )
        assert found.x is None
        whys[name] = found.why
    assert "AAA has no mark" in str(whys["stale mark"])
    assert "the reference has no mark" in str(whys["no reference"])
    assert "no tax facts for AAA" in str(whys["no tax facts"])
    assert "no capital" in str(whys["no capital"])


def test_a_month_without_a_thesis_is_zero_and_the_twin_needs_its_inputs(
    es: EsIrpfTaxPolicy,
) -> None:
    value = _values({("REF", date(2027, 1, 31)): "51"})
    (empty,) = series(
        ["2027-01"],
        Account((), {}, es),
        Account((), {}, es),
        capital=lambda _m: None,
        value=value,
        reference="REF",
    )
    assert empty.x == 0.0
    buy = Trade(1, "AAA", "buy", date(2027, 1, 10), D(10), D(1000), D(2))
    sell = Trade(1, "AAA", "sell", date(2027, 1, 12), D(11), D(1000), D(2))
    no_close = TwinInputs("REF", lambda _d: None, lambda *_: D(1))
    assert "no close" in str(twin_trades([buy], no_close))
    no_cost = TwinInputs("REF", lambda _d: D(50), lambda *_: None)
    assert "cannot price the twin's purchase" in str(twin_trades([buy], no_cost))
    costly = TwinInputs("REF", lambda _d: D(50), lambda *_: D(5000))
    assert "exceed" in str(twin_trades([buy], costly))
    priced = TwinInputs("REF", lambda _d: D(50), lambda *_: D(1))
    assert "sells more than it bought" in str(twin_trades([buy, sell], priced))
    sell_unpriced = TwinInputs(
        "REF", lambda _d: D(50), lambda _u, _c, side, _d: D(1) if side == "buy" else None
    )
    ok_sell = Trade(1, "AAA", "sell", date(2027, 1, 12), D(10), D(1000), D(2))
    assert "twin's sale" in str(twin_trades([buy, ok_sell], sell_unpriced))
    unheld = Account((sell,), {"AAA": AAA}, es)
    assert "cannot account" in str(profit(unheld, date(2027, 1, 31), value))


# ── the locked boundaries and a look ──

#: H13's table: look, months, step up at least, back to shadow at most.
TABLE = [
    (1, 6, 4.032, -3.365),
    (2, 12, 3.106, -2.718),
    (3, 18, 2.898, -2.567),
    (4, 24, 2.807, -2.500),
    (5, 30, 2.756, -2.462),
    (6, 36, 2.724, -2.438),
    (7, 42, 2.701, -2.421),
    (8, 48, 2.685, -2.408),
    (9, 54, 2.672, -2.399),
    (10, 60, 2.662, -2.391),
]


def test_the_locked_boundary_table_is_reproduced(h13: ForwardBookSpec) -> None:
    rules = look_rules(h13)
    for look, months, up, down in TABLE:
        assert look_number(months, h13) == look
        assert round(critical(rules.step_alpha, rules.looks, months), 3) == up
        assert round(-critical(rules.back_alpha, rules.looks, months), 3) == down
    assert look_number(5, h13) is None
    assert look_number(7, h13) is None
    assert look_number(66, h13) is None


def _xs(values: list[float | None]) -> list[MonthX]:
    months = months_from("2027-01", "2027-12")
    return [
        MonthX(m, v, D(1), None if v is not None else "stale")
        for m, v in zip(months, values, strict=False)
    ]


def test_a_look_not_evaluable_on_a_stale_mark_decides_nothing(h13: ForwardBookSpec) -> None:
    found = judge(
        1,
        _xs([0.05, 0.06, None, 0.05, 0.06, 0.05]),
        rules=look_rules(h13),
        closed=20,
        can_step=True,
        can_back=True,
        closed_at_last_step=None,
        without_best=_xs([0.05] * 6),
    )
    assert not found.evaluable
    assert not found.step
    assert not found.back
    assert found.mean is None
    assert "2027-03: stale" in found.detail


def test_a_step_needs_its_boundary_its_closed_theses_and_the_record_without_its_best(
    h13: ForwardBookSpec,
) -> None:
    rules = look_rules(h13)
    strong = _xs([0.050, 0.052, 0.049, 0.051, 0.050, 0.048])

    def look(**kw: object) -> object:
        args: dict[str, object] = {
            "rules": rules,
            "closed": 8,
            "can_step": True,
            "can_back": True,
            "closed_at_last_step": None,
            "without_best": _xs([0.01] * 6),
        }
        return judge(1, strong, **{**args, **kw})  # type: ignore[arg-type]

    stepped = look()
    assert stepped.step  # type: ignore[attr-defined]
    assert not stepped.back  # type: ignore[attr-defined]
    assert stepped.t_up >= 4.032  # type: ignore[attr-defined]
    assert not look(closed=7).step  # type: ignore[attr-defined]
    # Four more closed since the previous step at each later one.
    assert not look(closed=11, closed_at_last_step=8).step  # type: ignore[attr-defined]
    assert look(closed=12, closed_at_last_step=8).step  # type: ignore[attr-defined]
    assert "not above zero" in look(without_best=_xs([-0.01] * 6)).detail  # type: ignore[attr-defined]
    assert "cannot be measured" in look(without_best=None).detail  # type: ignore[attr-defined]
    assert not look(can_step=False).step  # type: ignore[attr-defined]


def test_a_return_to_shadow_needs_its_boundary_and_four_closed(h13: ForwardBookSpec) -> None:
    rules = look_rules(h13)
    losing = _xs([-0.050, -0.052, -0.049, -0.051, -0.050, -0.048])
    args: dict[str, object] = {
        "rules": rules,
        "can_step": True,
        "closed_at_last_step": None,
        "without_best": None,
    }
    back = judge(1, losing, closed=4, can_back=True, **args)  # type: ignore[arg-type]
    assert back.back
    assert not back.step
    assert back.t_down <= -3.365  # type: ignore[operator]
    assert not judge(1, losing, closed=3, can_back=True, **args).back  # type: ignore[arg-type]
    assert not judge(1, losing, closed=9, can_back=False, **args).back  # type: ignore[arg-type]
    flat = judge(1, _xs([0.0] * 6), closed=9, can_back=True, **args)  # type: ignore[arg-type]
    assert flat.evaluable
    assert not flat.back
    assert "does not vary" in flat.detail
    one = judge(1, _xs([0.01]), closed=9, can_back=True, **args)  # type: ignore[arg-type]
    assert not one.evaluable


# ── over the store ──


def _filled(
    store: Store,
    *,
    thesis_id: int,
    mode: ExecutionMode,
    side: OrderSide,
    at: datetime,
    price: str,
    quantity: str = "10",
) -> None:
    intent_id = uuid7()
    terms = OrderTerms(
        conid=EMIM_CONID,
        side=side,
        quantity=D(quantity),
        price=LimitPrice(limit=D(price)),
        tif=TimeInForce.DAY,
        venue="AEB",
    )
    intent = OrderIntent(
        intent_id=intent_id,
        sleeve=Sleeve.SATELLITE,
        thesis_id=f"T{thesis_id}",
        role=IntentRole.ENTRY if side is OrderSide.BUY else IntentRole.EXIT,
        terms=terms,
        reason="test",
        mode=mode,
        approval_required=False,
        created_at=at,
        expires_at=at + timedelta(hours=8),
        state=IntentState.FILLED,
    )
    order_repo.insert_intent(store, intent, coid=str(intent_id), at=at)
    order_repo.upsert_fills(
        store,
        [
            Fill(
                execution_id=f"x-{intent_id}",
                coid=str(intent_id),
                conid=EMIM_CONID,
                side=side,
                quantity=D(quantity),
                price=D(price),
                commission=D(1),
                executed_at=at,
                simulated=mode is ExecutionMode.SHADOW,
            )
        ],
    )


def _seed_book(store: Store, *, mode: ExecutionMode, stale_from: date | None = None) -> None:
    """One owner thesis on EMIM entered in January 2027, its marks and the reference's to
    July, and the book's capital before each month."""
    seed_vehicle(store, REFERENCE, "XDWD")
    seed_vehicle(store, EMIM_CONID, "EMIM")
    days = [date(2026, 12, 20) + timedelta(days=i) for i in range(210)]
    seed_closes(store, REFERENCE, {d: 100 + 0.05 * i for i, d in enumerate(days)})
    seed_closes(
        store,
        EMIM_CONID,
        {d: 45 + 0.04 * i for i, d in enumerate(days) if stale_from is None or d < stale_from},
    )
    stored = thesis_repo.insert_thesis(
        store, thesis(created_at=datetime(2027, 1, 4, tzinfo=UTC), enter_by=date(2027, 1, 20)),
        actor="owner",
    )  # fmt: skip
    thesis_repo.approve(
        store,
        stored.thesis_id,
        mode=mode,
        size_base=D(460),
        at=datetime(2027, 1, 5, 9, tzinfo=UTC),
        actor="t",
        detail="approved",
    )
    _filled(
        store,
        thesis_id=stored.thesis_id,
        mode=mode,
        side=OrderSide.BUY,
        at=datetime(2027, 1, 6, 10, tzinfo=UTC),
        price="45.7",
    )
    for month in months_from("2026-12", "2027-06"):
        record_book_capital(
            store, BookCapital(mode, month_end(month), D(4000), datetime(2027, 1, 1, tzinfo=UTC))
        )


def test_h13_looks_once_at_month_six_and_the_share_follows(
    store: Store, shipped_settings: ObliskConfig, h13: ForwardBookSpec, es: EsIrpfTaxPolicy
) -> None:
    _seed_book(store, mode=ExecutionMode.PAPER)
    early = run_h13(
        store, shipped_settings, h13, policy=es, venue_of=lambda _e: "XAMS",
        month="2027-05", at=AT,
    )  # fmt: skip
    assert early == []
    found = run_h13(
        store, shipped_settings, h13, policy=es, venue_of=lambda _e: "XAMS",
        month="2027-06", at=AT,
    )  # fmt: skip
    groups = {look.group for look in found}
    assert groups == {"book", "source:owner", "conviction:normal"}
    book = next(look for look in found if look.group == "book")
    assert book.record == "paper"
    assert book.look == 1
    assert book.months == 6
    assert book.evaluable, book.detail
    assert not book.step  # paper never steps
    months = json.loads(book.series)
    assert [m["month"] for m in months] == months_from("2027-01", "2027-06")
    assert all(m["x"] is not None for m in months)
    again = run_h13(
        store, shipped_settings, h13, policy=es, venue_of=lambda _e: "XAMS",
        month="2027-06", at=AT,
    )  # fmt: skip
    assert again == []  # recorded once
    assert len(h13_looks(store, "paper")) == 3


def test_a_stale_mark_makes_the_look_not_evaluable_and_changes_nothing(
    store: Store, shipped_settings: ObliskConfig, h13: ForwardBookSpec, es: EsIrpfTaxPolicy
) -> None:
    from obliskiq.research.prereg import ScoreboardPrereg

    _seed_book(store, mode=ExecutionMode.PAPER, stale_from=date(2027, 4, 15))
    found = run_h13(
        store, shipped_settings, h13, policy=es, venue_of=lambda _e: "XAMS",
        month="2027-06", at=AT,
    )  # fmt: skip
    book = next(look for look in found if look.group == "book")
    assert not book.evaluable
    assert "not evaluable" in book.detail
    assert "2027-04" in book.detail
    rules = load_any(Path("research") / "preregistrations" / "scoreboard.md")
    assert isinstance(rules, ScoreboardPrereg)
    changes = run_month(
        store,
        shipped_settings,
        rules.spec,
        [],
        month="2027-06",
        h13=h13,
        activation=lambda _: False,
        retest=lambda _l, _m: None,
        at=AT,
    )
    assert current_shares(store)[THESIS_BOOK] == shipped_settings.thesis.capital_share
    assert "exploration share" in changes.shares[0].reason


def test_without_a_tax_policy_or_a_rate_the_look_is_recorded_not_evaluable(
    store: Store, shipped_settings: ObliskConfig, h13: ForwardBookSpec
) -> None:
    _seed_book(store, mode=ExecutionMode.SHADOW)
    found = run_h13(
        store, shipped_settings, h13, policy=None, venue_of=lambda _e: "XAMS",
        month="2027-06", at=AT,
    )  # fmt: skip
    assert found
    assert all(not look.evaluable for look in found)
    assert "no tax policy" in found[0].detail


def test_the_twin_needs_a_venue_the_cost_model_prices(
    store: Store, shipped_settings: ObliskConfig, h13: ForwardBookSpec, es: EsIrpfTaxPolicy
) -> None:
    _seed_book(store, mode=ExecutionMode.SHADOW)
    found = run_h13(
        store, shipped_settings, h13, policy=es, venue_of=lambda _e: None,
        month="2027-06", at=AT,
    )  # fmt: skip
    book = next(look for look in found if look.group == "book")
    assert not book.evaluable
    assert "twin" in book.detail


def test_the_book_capital_keeps_the_day_s_first_reading(store: Store) -> None:
    first = BookCapital(ExecutionMode.LIVE, date(2027, 1, 29), D(500), AT)
    assert record_book_capital(store, first)
    assert not record_book_capital(
        store, BookCapital(ExecutionMode.LIVE, date(2027, 1, 29), D(9), AT)
    )
    record_book_capital(store, BookCapital(ExecutionMode.LIVE_SMALL, date(2027, 1, 30), D(600), AT))
    assert capital_before(store, "live", date(2027, 2, 1)) == D(600)
    assert capital_before(store, "live", date(2027, 1, 30)) == D(500)
    assert capital_before(store, "paper", date(2027, 2, 1)) is None
    assert [c.capital for c in book_capital(store)] == [D(500), D(600)]


# ── the drills: a step and a return to shadow, and the holdbacks ──


def _look(
    record: str, group: str, month: str, *, step: bool = False, back: bool = False
) -> H13Look:
    return H13Look(
        record=record,  # type: ignore[arg-type]
        group=group,
        month=month,
        look=1,
        months=6,
        closed=8,
        mean=0.01,
        sd=0.01,
        t_up=None,
        t_down=None,
        c_up=None,
        c_down=None,
        without_best=None,
        step=step,
        back=back,
        evaluable=True,
        series="[]",
        detail="d",
        at=AT,
    )


def test_a_step_and_a_return_to_shadow_drill_through_the_month_end_run(
    store: Store, shipped_settings: ObliskConfig, h13: ForwardBookSpec
) -> None:
    from obliskiq.research.prereg import ScoreboardPrereg

    rules = load_any(Path("research") / "preregistrations" / "scoreboard.md")
    assert isinstance(rules, ScoreboardPrereg)

    def month(name: str) -> Decimal:
        run_month(
            store,
            shipped_settings,
            rules.spec,
            [],
            month=name,
            h13=h13,
            activation=lambda _: False,
            retest=lambda _l, _m: None,
            at=AT,
        )
        return current_shares(store)[THESIS_BOOK]

    exploration = shipped_settings.thesis.capital_share
    assert month("2027-05") == exploration
    record_h13_look(store, _look("live", "book", "2027-06", step=True))
    assert month("2027-06") == exploration + h13.step.share_step
    record_h13_look(store, _look("live", "book", "2027-12", back=True))
    assert month("2027-12") == D(0)


def test_a_group_s_return_to_shadow_holds_its_source_segment_or_conviction_back(
    store: Store,
) -> None:
    assert holdbacks(store).sources == {}
    record_h13_look(store, _look("paper", "source:analyst", "2027-06", back=True))
    record_h13_look(store, _look("live", "analyst:m1-abc", "2027-06", back=True))
    record_h13_look(store, _look("live", "conviction:max", "2027-06", back=True))
    record_h13_look(store, _look("shadow", "source:owner", "2027-06", back=True))
    record_h13_look(store, _look("live", "book", "2027-06", back=True))
    record_h13_look(store, _look("live", "conviction:high", "2027-06"))
    found = holdbacks(store)
    assert set(found.sources) == {"analyst"}
    assert set(found.segments) == {"m1-abc"}
    assert set(found.convictions) == {"max"}


def test_an_h13_look_is_written_once(store: Store) -> None:
    import sqlite3

    look = _look("live", "book", "2027-06", step=True)
    assert record_h13_look(store, look)
    assert not record_h13_look(store, look)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE h13_looks SET step = 0")
    with pytest.raises(StoreError, match="CHECK constraint"):
        record_h13_look(store, _look("paper", "book", "2027-07", step=True))


def test_the_thesis_states_and_sources_used_are_the_domain_s() -> None:
    assert {s.value for s in ThesisSource} == {"owner", "analyst"}
    assert {c.value for c in Conviction} == {"normal", "high", "max"}
    assert ThesisState.CLOSED.value == "closed"
