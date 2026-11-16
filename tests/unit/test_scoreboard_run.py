"""The scoreboard over the store: marks, calls, grading, the pattern lifecycle, the month-end run,
performance, TCA executions and the promotion gate's evidence (scoreboard/run.py,
store/scoreboard.py).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid7

import pytest
from tests.support.core_rig import seed_closes, seed_vehicle

from obliskiq.capital.allocation import THESIS_BOOK, layer_key
from obliskiq.config import ObliskConfig
from obliskiq.domain.capital import Deposit
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import (
    ClearanceAction,
    ExecutionMode,
    Fill,
    IntentRole,
    IntentState,
    LimitPrice,
    ModeChange,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    QuoteEvidence,
    ReconcileRun,
    ReconcileStatus,
    RiskEffect,
    Sleeve,
    TimeInForce,
)
from obliskiq.research.prereg import (
    ForwardBookPrereg,
    ForwardBookSpec,
    ScoreboardPrereg,
    ScoreboardSpec,
    load_any,
)
from obliskiq.scoreboard.layers import RegistryLayer
from obliskiq.scoreboard.monitor import Backtest
from obliskiq.scoreboard.run import (
    PANEL_LAYER,
    GradeBook,
    StoreMarks,
    all_calls,
    end_of,
    executions,
    grade_book,
    performance,
    promotion_evidence,
    run_lifecycle,
    run_month,
)
from obliskiq.store import fx as fx_repo
from obliskiq.store import nav as nav_repo
from obliskiq.store import orders as order_repo
from obliskiq.store import runs as run_repo
from obliskiq.store import scoreboard as repo
from obliskiq.store import session_events as session_repo
from obliskiq.store.db import Store
from obliskiq.store.fx import FxRate
from obliskiq.store.nav import NavReading
from obliskiq.store.scoreboard import AllocationEvent, Call, Drill, LayerEvent, LayerMonth, Look
from obliskiq.store.session_events import SessionEvent

D = Decimal
REFERENCE = 163606916
STOCK = 4001
USD_STOCK = 4002
AT = datetime(2026, 6, 1, 22, tzinfo=UTC)
#: After the seeded instruments' creation stamp (tests/support/core_rig.py), a Monday.
START = date(2026, 10, 5)


@pytest.fixture(scope="module")
def spec(repo_root: Path) -> ScoreboardSpec:
    found = load_any(repo_root / "research" / "preregistrations" / "scoreboard.md")
    assert isinstance(found, ScoreboardPrereg)
    return found.spec


@pytest.fixture(scope="module")
def h13(repo_root: Path) -> ForwardBookSpec:
    found = load_any(repo_root / "research" / "preregistrations" / "thesis-book.md")
    assert isinstance(found, ForwardBookPrereg)
    return found.spec


def seed_prices(store: Store) -> None:
    seed_vehicle(store, REFERENCE, "XDWD")
    seed_vehicle(store, STOCK, "AAA")
    seed_vehicle(store, USD_STOCK, "BBB", currency="USD")
    days = [START + timedelta(days=i) for i in range(120)]
    seed_closes(store, REFERENCE, {d: 100 + 0.1 * i for i, d in enumerate(days)})
    seed_closes(store, STOCK, {d: 50 + 0.2 * i for i, d in enumerate(days)})
    seed_closes(store, USD_STOCK, dict.fromkeys(days, 20.0))
    fx_repo.record_rates(store, [FxRate("USD", START, D("0.9"), "test", AT)])


# ── the store's records ──


def test_the_records_are_append_only(store: Store) -> None:
    repo.record_call(store, Call("l", 1, "up", AT, "d"))
    repo.record_layer_event(store, LayerEvent(AT, "l", "active", "s", "d"))
    repo.record_allocation(store, AllocationEvent(AT, "l", D("0.5"), "a", "r"))
    repo.record_drill(store, Drill(AT, "kill", ExecutionMode.PAPER, True, "owner", "d"))
    assert [e.layer for e in repo.layer_events(store)] == ["l"]
    assert [e.layer for e in repo.allocation_events(store, "l")] == ["l"]
    for table in ("scoreboard_calls", "layer_events", "allocation_events", "drills"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute(
                f"UPDATE {table} SET at = at"
                if table != "scoreboard_calls"
                else "UPDATE scoreboard_calls SET detail = 'x'"
            )


def test_a_month_a_look_and_a_weight_set_are_written_once(store: Store) -> None:
    month = LayerMonth("l", "2026-05", ExecutionMode.LIVE, 2, 0.01, 0.0, 0.0, AT)
    assert repo.record_month(store, month)
    assert not repo.record_month(store, month)
    look = Look("l", "2026-05", "gap", 1.0, -3.0, 0.001, False, "d", AT)
    assert repo.record_look(store, look)
    assert not repo.record_look(store, look)
    blank = Look("l", "2026-05", "twin", None, None, 0.001, False, "d", AT)
    assert repo.record_look(store, blank)
    assert [x.evaluable for x in repo.looks(store, "l")] == [True, False]
    weights = repo.WeightSet("l", "a" * 64, '{"a":1}', AT)
    assert repo.record_weights(store, weights)
    assert not repo.record_weights(store, weights)
    assert repo.latest_weights(store, "l") == weights
    assert repo.latest_weights(store, "m") is None
    assert repo.layer_months(store, "l") == [month]
    for table in ("layer_months", "monitor_looks", "weight_sets"):
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE layer_months SET closed = 3")


def test_calls_by_layer_and_time(store: Store) -> None:
    repo.record_call(store, Call("b", 1, "up", AT, "d", probability=0.6))
    stored = repo.record_call(store, Call("a", 2, "down", AT - timedelta(days=1), "d"))
    assert stored.call_id is not None
    assert [c.layer for c in repo.calls(store, as_of=AT)] == ["a", "b"]
    assert [c.layer for c in repo.calls(store, as_of=AT, layer="b")] == ["b"]
    assert repo.calls(store, as_of=AT - timedelta(days=2)) == []
    assert repo.layers_called(store) == ["a", "b"]


# ── marks ──


def test_marks_in_the_base_currency_bounded_by_age_and_day(store: Store) -> None:
    seed_prices(store)
    marks = StoreMarks(store, base="EUR", max_age_days=7, as_of=date(2026, 12, 1))
    assert marks.price(STOCK, START) == D("50.0")
    assert marks.price(USD_STOCK, START + timedelta(days=3)) == D("20.0") * D("0.9")
    assert marks.price(USD_STOCK, START + timedelta(days=15)) is None  # its rate is too old
    assert marks.price(STOCK, date(2026, 12, 2)) is None  # after the day of the reading
    assert marks.price(9999, START) is None
    assert marks.price(STOCK, START - timedelta(days=2)) is None  # no close yet
    assert marks.mark(STOCK, START) == 50.0
    assert marks.mark(9999, START) is None
    assert marks.rate("EUR", START) == D(1)
    assert marks.rate("GBP", START) is None


# ── grading over the store ──


def test_grade_book_grades_each_layer_and_the_panel(
    store: Store, shipped_settings: ObliskConfig, spec: ScoreboardSpec
) -> None:
    seed_prices(store)
    for week in range(3):
        at = datetime(2026, 10, 5, 12, tzinfo=UTC) + timedelta(weeks=week)
        repo.record_call(store, Call("signal", STOCK, "up", at, "d", probability=0.7))
        repo.record_call(
            store, Call(PANEL_LAYER, STOCK, "up", at, "d", probability=0.6, expected_excess=1.0)
        )
    book = grade_book(store, shipped_settings, spec, as_of=date(2027, 1, 15))
    assert set(book.records) == {"signal"}
    assert book.panel is not None
    signal = book.records["signal"]
    assert signal.graded == 3
    assert signal.decision is None
    assert signal.estimate.mean is not None
    assert signal.estimate.mean > 0
    assert set(book.calibration) == {"signal", PANEL_LAYER}
    assert not book.calibration["signal"].calibrated
    assert all_calls(store, as_of=date(2026, 10, 4)) == {}
    assert end_of(date(2026, 10, 4)).hour == 23


def test_theses_are_calls_per_source_and_segment(
    store: Store,
    shipped_settings: ObliskConfig,
    spec: ScoreboardSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from obliskiq.domain.thesis import ThesisSource, View
    from obliskiq.scoreboard import run as run_module

    def thesis(n: int, source: ThesisSource, view: View, conid: int | None) -> object:
        return SimpleNamespace(
            thesis_id=n,
            source=source,
            view=view,
            approved_at=None if conid is None else AT,
            instrument=SimpleNamespace(conid=conid),
        )

    found = [
        thesis(1, ThesisSource.OWNER, View.BULLISH, STOCK),
        thesis(2, ThesisSource.ANALYST, View.BEARISH, STOCK),
        thesis(3, ThesisSource.OWNER, View.BULLISH, None),
    ]
    monkeypatch.setattr(run_module.thesis_repo, "theses", lambda _store, **_kw: found)
    monkeypatch.setattr(run_module.analyst_repo, "thesis_segments", lambda _store: {2: "seg"})
    calls = run_module.thesis_calls(store, as_of=AT.date())
    assert [(c.layer, c.direction) for c in calls] == [
        ("thesis:owner", "up"),
        ("thesis:analyst", "down"),
        ("analyst:seg", "down"),
    ]


# ── the lifecycle and the month-end run ──


def _layer(status: str = "shadow", **kw: object) -> RegistryLayer:
    values: dict[str, object] = {
        "id": "pat",
        "status": status,
        "sleeve": Sleeve.SATELLITE,
        "budget": "stock_book",
        "decision": "sufficient",
        "labels": (),
        "theta": 0.005,
        "estimate": None,
        "interval": None,
        "metric": None,
        "backtest": Backtest(0.01, 0.03, 0.001),
    }
    values.update(kw)
    return RegistryLayer(**values)  # type: ignore[arg-type]


def test_the_lifecycle_promotes_a_pattern_that_meets_its_rule(
    store: Store, spec: ScoreboardSpec
) -> None:
    from tests.unit.test_scoreboard_capital import _forward

    book = GradeBook(date(2026, 6, 1), {"pat": _forward(0.01, 0.003)}, {}, None)
    held = run_lifecycle(store, spec, [_layer()], book, activation=lambda _: False, at=AT)
    assert held.events == []
    assert "activation" in held.notes[0]
    done = run_lifecycle(store, spec, [_layer()], book, activation=lambda _: True, at=AT)
    assert [e.event for e in done.events] == ["active"]
    assert repo.layer_events(store, "pat")[0].actor == "scoreboard"
    again = run_lifecycle(store, spec, [_layer()], book, activation=lambda _: True, at=AT)
    assert again.events == []
    empty = GradeBook(date(2026, 6, 1), {}, {}, None)
    assert (
        "no call graded"
        in run_lifecycle(
            store, spec, [_layer("shadow", id="other")], empty, activation=lambda _: True, at=AT
        ).notes[0]
    )


def _activate(store: Store, layer: str, at: datetime) -> None:
    repo.record_layer_event(store, LayerEvent(at, layer, "active", "scoreboard", "promoted"))


def test_the_month_end_drill_records_looks_freezes_and_steps(
    store: Store, shipped_settings: ObliskConfig, spec: ScoreboardSpec, h13: ForwardBookSpec
) -> None:
    _activate(store, "pat", datetime(2026, 1, 2, tzinfo=UTC))
    for m in range(1, 6):
        repo.record_month(
            store,
            LayerMonth(
                "pat", f"2026-{m:02d}", ExecutionMode.LIVE, 3, -0.03 - 0.001 * m, 0.01, 0.0, AT
            ),
        )
    changes = run_month(
        store,
        shipped_settings,
        spec,
        [_layer("active"), _layer("shadow", id="idle", decision="insufficient")],
        month="2026-05",
        h13=h13,
        activation=lambda _: True,
        retest=lambda _layer, _months: True,
        at=AT,
    )
    assert {look.boundary for look in changes.looks} == {"gap", "twin"}
    assert [e.event for e in changes.events] == ["freeze"]
    key = layer_key(Sleeve.SATELLITE, "pat")
    shares = {e.layer: e.share for e in changes.shares}
    assert shares == {key: D("0.50"), THESIS_BOOK: D("0.10")}
    repeat = run_month(
        store,
        shipped_settings,
        spec,
        [_layer("active")],
        month="2026-05",
        h13=h13,
        activation=lambda _: True,
        retest=lambda _layer, _months: True,
        at=AT,
    )
    assert repeat.looks == []
    assert repeat.shares == []


def test_a_twin_breach_in_the_month_end_run_takes_the_share_back(
    store: Store, shipped_settings: ObliskConfig, spec: ScoreboardSpec, h13: ForwardBookSpec
) -> None:
    key = layer_key(Sleeve.OPTIONS, "pat")
    _activate(store, "pat", datetime(2026, 1, 2, tzinfo=UTC))
    repo.record_allocation(store, AllocationEvent(AT, key, D("0.0125"), "allocation rule", "first"))
    for m in range(1, 6):
        repo.record_month(
            store,
            LayerMonth(
                "pat", f"2026-{m:02d}", ExecutionMode.LIVE, 1, -0.09, -0.09 + 0.0001 * m, 0.0, AT
            ),
        )
    changes = run_month(
        store,
        shipped_settings,
        spec,
        [_layer("active", sleeve=Sleeve.OPTIONS, budget="layer")],
        month="2026-05",
        h13=h13,
        activation=lambda _: True,
        retest=lambda _layer, _months: True,
        at=AT,
    )
    assert "shadow" in [e.event for e in changes.events]
    assert {e.layer: e.share for e in changes.shares}[key] == D(0)


# ── intents, clearances and fills ──


def _intent(
    store: Store,
    *,
    mode: ExecutionMode,
    sleeve: Sleeve = Sleeve.CORE,
    at: datetime,
    state: IntentState = IntentState.FILLED,
    cleared: bool = True,
    filled: bool = True,
    simulated: bool = False,
    quote: QuoteEvidence | None = None,
    price: str = "101",
    asset_class: AssetClass = AssetClass.ETF,
    role: IntentRole = IntentRole.ENTRY,
) -> UUID:
    intent_id = uuid7()
    terms = OrderTerms(
        conid=STOCK,
        side=OrderSide.BUY,
        quantity=D(10),
        price=LimitPrice(limit=D(102)),
        tif=TimeInForce.DAY,
        venue="IBIS2",
    )
    intent = OrderIntent(
        intent_id=intent_id,
        sleeve=sleeve,
        role=role,
        terms=terms,
        reason="test",
        mode=mode,
        approval_required=False,
        created_at=at,
        expires_at=at + timedelta(hours=8),
        state=state,
    )
    coid = str(intent_id)
    order_repo.insert_intent(store, intent, coid=coid, at=at)
    if cleared:
        clearance = PretradeClearance(
            clearance_id=uuid7(),
            intent_id=intent_id,
            action=ClearanceAction.PLACE,
            terms=terms,
            effect=RiskEffect.ADDS,
            asset_class=asset_class,
            quote=quote,
            issued_at=at,
            expires_at=at + timedelta(minutes=5),
            currency="EUR",
            notional=D(1020),
            notional_base=D(1020),
        )
        order_repo.record_clearance(store, clearance)
        assert (
            order_repo.spend_clearance(
                store,
                clearance.clearance_id,
                intent_id=intent_id,
                action=ClearanceAction.PLACE,
                order_id=None,
                terms_digest=clearance.terms_digest,
                now=at + timedelta(seconds=1),
            )
            is None
        )
    if filled:
        order_repo.upsert_fills(
            store,
            [
                Fill(
                    execution_id=f"x-{intent_id}",
                    coid=coid,
                    conid=STOCK,
                    side=OrderSide.BUY,
                    quantity=D(10),
                    price=D(price),
                    commission=D(1),
                    executed_at=at + timedelta(minutes=1),
                    simulated=simulated,
                )
            ],
        )
    return intent_id


def _quote(at: datetime) -> QuoteEvidence:
    return QuoteEvidence(
        conid=STOCK,
        status=MarketDataStatus.REALTIME,
        delay_seconds=0,
        observed_at=at,
        bid=D(99),
        ask=D(101),
    )


def test_executions_measure_arrival_and_the_cost_model(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    start = datetime(2026, 3, 2, 10, tzinfo=UTC)
    _intent(store, mode=ExecutionMode.PAPER, at=start, quote=_quote(start))
    _intent(store, mode=ExecutionMode.PAPER, at=start + timedelta(days=1), cleared=False)
    _intent(store, mode=ExecutionMode.PAPER, at=start + timedelta(days=2), simulated=True)
    _intent(store, mode=ExecutionMode.LIVE, at=start + timedelta(days=3))
    _intent(
        store, mode=ExecutionMode.PAPER, at=start + timedelta(days=4), asset_class=AssetClass.BOND
    )
    found = executions(store, shipped_settings, mode=ExecutionMode.PAPER, since=None)
    assert len(found) == 3
    measured, unmeasured, bond = found
    assert bond.modelled is None
    assert measured.arrival == D(100)
    assert measured.quote_status == "realtime"
    assert measured.modelled is not None
    assert measured.modelled > 0
    assert measured.order_type == "limit"
    assert measured.venue == "IBIS2"
    assert unmeasured.arrival is None
    assert unmeasured.modelled is None
    assert unmeasured.quote_status == "none recorded"
    later = executions(
        store, shipped_settings, mode=ExecutionMode.PAPER, since=start + timedelta(hours=12)
    )
    assert len(later) == 2


def test_the_promotion_evidence_of_a_paper_period(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    entered = datetime(2026, 1, 5, 9, tzinfo=UTC)
    order_repo.record_mode_change(
        store,
        ModeChange(
            at=entered,
            from_mode=ExecutionMode.SHADOW,
            to_mode=ExecutionMode.PAPER,
            direction="upgrade",
            actor="owner",
            reason="r",
        ),
    )
    _intent(store, mode=ExecutionMode.PAPER, at=entered + timedelta(days=1), quote=_quote(entered))
    _intent(
        store,
        mode=ExecutionMode.PAPER,
        at=entered + timedelta(days=2),
        state=IntentState.EXPIRED,
        filled=False,
    )
    _intent(store, mode=ExecutionMode.PAPER, at=entered + timedelta(days=3), cleared=False)
    _intent(
        store,
        mode=ExecutionMode.PAPER,
        at=entered + timedelta(days=4),
        state=IntentState.WORKING,
        filled=False,
    )
    _intent(store, mode=ExecutionMode.PAPER, at=entered - timedelta(days=3))
    _intent(
        store,
        mode=ExecutionMode.PAPER,
        at=entered + timedelta(days=5),
        role=IntentRole.PROTECTIVE,
        filled=False,
    )
    order_repo.record_reconciliation(
        store,
        ReconcileRun(at=entered + timedelta(days=1), status=ReconcileStatus.GREEN, findings=()),
    )
    run_repo.begin_run(
        store,
        run_id="r1",
        command="cycle:eu",
        session_date=date(2026, 1, 6),
        started_at=entered + timedelta(days=1),
        steps=("a",),
    )
    repo.record_drill(
        store, Drill(entered + timedelta(days=5), "kill", ExecutionMode.PAPER, True, "o", "d")
    )
    session_repo.record_session_event(
        store, SessionEvent(entered - timedelta(days=1), "reauthenticated", "before the period")
    )
    session_repo.record_session_event(
        store, SessionEvent(entered + timedelta(days=2), "reauthenticated", "the session was lost")
    )
    now = entered + timedelta(days=60)
    evidence = promotion_evidence(
        store,
        shipped_settings,
        mode=ExecutionMode.PAPER,
        now=now,
        sessions=lambda first, last: [first, last],
    )
    assert evidence.placed == 2
    assert evidence.filled == 1
    assert evidence.fills_without_clearance == 1
    assert evidence.stuck_intents == 1
    assert evidence.tca is not None
    assert evidence.tca.fills == 1
    assert [s for _, s in evidence.reconciliations] == ["green"]
    assert len(evidence.runs) == 1
    assert evidence.sessions == (date(2026, 1, 5), now.date())
    assert evidence.capped_book
    assert len(evidence.session_events) == 1
    assert not evidence.hard_breaker_tripped
    assert len(evidence.drills) == 1


def test_the_evidence_of_a_mode_the_audit_does_not_date(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    evidence = promotion_evidence(
        store, shipped_settings, mode=ExecutionMode.SHADOW, now=AT, sessions=lambda _a, _b: []
    )
    assert evidence.changes == ()
    assert evidence.placed == 0


# ── performance ──


def test_performance_over_the_store(store: Store, shipped_settings: ObliskConfig) -> None:
    seed_prices(store)
    for conid, symbol in ((280737629, "XMME"), (53507356, "DBXQ")):
        seed_vehicle(store, conid, symbol)
        seed_closes(store, conid, {START + timedelta(days=i): 10.0 for i in range(120)})
    for day, value in ((START, "1000"), (date(2026, 10, 6), "1010"), (date(2026, 11, 2), "1030")):
        nav_repo.record_nav(store, NavReading(day, D(value), "EUR", "test", AT))
    _intent(store, mode=ExecutionMode.PAPER, at=datetime(2026, 10, 5, 9, tzinfo=UTC), price="50")
    view = performance(store, shipped_settings, since=date(2026, 10, 1), as_of=date(2026, 11, 2))
    assert view.account.growth == D("0.03")
    assert [r.name for r in view.references] == ["global equity", "global 60/40"]
    assert all(r.growth is not None and r.growth > 0 for r in view.references)
    assert not isinstance(view.sleeves, str)
    (core,) = view.sleeves
    assert core.sleeve is Sleeve.CORE
    assert core.realised == D(0)
    assert core.held_value == D(10) * D("55.6")
    assert core.unrealised == D(10) * D("55.6") - D(501)


def test_performance_says_why_when_a_flow_has_no_rate(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    from obliskiq.store import capital as capital_repo

    flow = Deposit(
        on=date(2026, 10, 6),
        amount=D(100),
        currency="GBP",
        recorded_at=AT,
        recorded_by="owner",
    )
    stored = capital_repo.record_flow(store, flow)
    assert stored.flow_id is not None
    capital_repo.match_flow(store, stored.flow_id, at=AT)
    view = performance(store, shipped_settings, since=date(2026, 10, 1), as_of=date(2026, 11, 2))
    assert view.account.reason is not None
    assert "GBP" in view.account.reason
