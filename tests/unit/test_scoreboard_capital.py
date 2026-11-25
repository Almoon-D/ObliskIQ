"""The allocation rule, the pattern lifecycle and the monthly looks' actions (DECISIONS D65,
D149, D155, D156; research/preregistrations/scoreboard.md).

Drills: a gap breach freezes a layer's new entries, a twin breach returns it to shadow with its
share, and a budget step happens only at a look with every condition met.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from obliskiq.capital.allocation import (
    STOCK_BOOK,
    THESIS_BOOK,
    LiveShares,
    StepEvidence,
    StepRule,
    allocation_now,
    layer_key,
    live_allocation,
    live_shares,
    sleeve_budget,
    step_share,
)
from obliskiq.capital.budgets import book_allocation
from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import ExecutionMode, Sleeve
from obliskiq.quant.inference import MeanEstimate
from obliskiq.research.prereg import ScoreboardPrereg, ScoreboardSpec, load_any
from obliskiq.scoreboard.grading import Record
from obliskiq.scoreboard.layers import (
    LayerRegistryError,
    LayerState,
    RegistryLayer,
    ThesisBookCapital,
    as_record,
    layer_rule,
    layer_state,
    month_looks,
    no_activation,
    no_retest,
    pooled,
    promotion_decision,
    registry_layers,
    step_evidence,
    stock_book_rule,
    thesis_book_share,
)
from obliskiq.scoreboard.monitor import Backtest
from obliskiq.store.h13 import H13Look
from obliskiq.store.scoreboard import LayerEvent, LayerMonth, Look
from obliskiq.store.worker import StoreWorker

D = Decimal
AT = datetime(2027, 6, 30, 22, tzinfo=UTC)


@pytest.fixture(scope="module")
def spec(repo_root: Path) -> ScoreboardSpec:
    found = load_any(repo_root / "research" / "preregistrations" / "scoreboard.md")
    assert isinstance(found, ScoreboardPrereg)
    return found.spec


def rule() -> StepRule:
    return StepRule(D("0.50"), D("0.10"), D("0.95"), 12, 12, 12, 30, 15)


def evidence(**changes: object) -> StepEvidence:
    base = StepEvidence(
        sufficient=True,
        activation=True,
        twin_breached=False,
        gap_breach_outstanding=False,
        live_months=24,
        months_since_step=12,
        closed=40,
        closed_since_step=20,
        steps_taken=0,
        retest_sufficient=True,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# ── the step rule ──


def test_a_sufficient_layer_takes_its_first_share() -> None:
    decision = step_share(D(0), rule(), evidence())
    assert (decision.share, decision.changed) == (D("0.50"), True)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"sufficient": False}, "sufficient outcome"),
        ({"activation": False}, "activation condition"),
    ],
)
def test_no_share_without_a_sufficient_outcome_and_its_activation(
    changes: dict[str, object], reason: str
) -> None:
    decision = step_share(D("0.50"), rule(), evidence(**changes))
    assert decision.share == D(0)
    assert reason in decision.reason


def test_a_budget_step_at_a_look_with_every_condition() -> None:
    decision = step_share(D("0.50"), rule(), evidence())
    assert decision.share == D("0.60")
    assert decision.changed
    top = step_share(D("0.90"), rule(), evidence(steps_taken=4))
    assert top.share == D("0.95")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"live_months": 6, "months_since_step": 6}, "live-account months"),
        ({"closed": 10}, "closed positions"),
        ({"gap_breach_outstanding": True}, "gap breach"),
        ({"retest_sufficient": None}, "could not be run"),
        ({"retest_sufficient": False}, "no longer sufficient"),
        ({"steps_taken": 1, "months_since_step": 5}, "a look every 12"),
        ({"steps_taken": 1, "closed_since_step": 3}, "closed positions"),
    ],
)
def test_a_budget_step_is_impossible_before_its_minimums(
    changes: dict[str, object], reason: str
) -> None:
    decision = step_share(D("0.50"), rule(), evidence(**changes))
    assert decision.share == D("0.50")
    assert not decision.changed
    assert reason in decision.reason


def test_no_step_beyond_the_ceiling() -> None:
    decision = step_share(D("0.95"), rule(), evidence(steps_taken=5))
    assert decision.share == D("0.95")
    assert "ceiling" in decision.reason


def test_a_twin_breach_takes_the_share_back() -> None:
    decision = step_share(D("0.60"), rule(), evidence(twin_breached=True))
    assert (decision.share, decision.changed) == (D(0), True)
    assert not step_share(D(0), rule(), evidence(twin_breached=True)).changed


def test_the_rules_from_the_pre_registration(spec: ScoreboardSpec) -> None:
    stock = stock_book_rule(spec, cash_floor=D("0.05"))
    assert (stock.first, stock.step, stock.ceiling) == (D("0.50"), D("0.10"), D("0.95"))
    other = layer_rule(spec, sleeve_budget=D("0.05"))
    assert (other.first, other.step, other.ceiling) == (D("0.0125"), D("0.0125"), D("0.05"))
    assert (stock.min_closed_first, stock.min_closed_between) == (30, 15)


# ── the allocation the live path reads ──


def test_shares_by_sleeve() -> None:
    shares = {
        STOCK_BOOK: D("0.50"),
        THESIS_BOOK: D("0.10"),
        layer_key(Sleeve.OPTIONS, "options-h8"): D("0.0125"),
        "nonsense": D("0.30"),
        "bogus:entry": D("0.30"),
    }
    found = live_shares(shares)
    assert (found.satellite, found.options, found.thesis) == (D("0.60"), D("0.0125"), D("0.10"))
    assert LiveShares.none() == LiveShares(D(0), D(0), D(0))


def test_the_live_allocation_keeps_the_reserve_and_the_cash_floor(
    shipped_settings: ObliskConfig,
) -> None:
    capital = shipped_settings.capital.model_copy(update={"satellite_ceiling": D("0.95")})
    settings = shipped_settings.model_copy(update={"capital": capital})
    found = live_allocation(settings, D(1000), LiveShares(D("0.60"), D("0.0125"), D("0.10")))
    assert (found.satellite, found.options, found.cash_floor) == (D(600), D("12.5"), D(50))
    # The thesis book's share stays in the reserve's allocation: the reserve gives up what the
    # book holds or has approved when it is committed (allocation.reserve_budget, D196).
    assert found.core == D("437.5")
    assert found.thesis_book == D(100)
    capped = live_allocation(shipped_settings, D(1000), LiveShares(D("0.60"), D(0), D("0.10")))
    assert capped.satellite == D(200)
    assert capped.core == D(850)
    nothing = live_allocation(shipped_settings, D(1000), LiveShares.none())
    assert (nothing.core, nothing.satellite) == (D(950), D(0))
    assert sleeve_budget(shipped_settings, Sleeve.OPTIONS) == D("0.05")


async def test_the_live_account_reads_the_recorded_shares(
    store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    from obliskiq.store import scoreboard as repo
    from obliskiq.store.scoreboard import AllocationEvent

    paper = await allocation_now(store_worker, shipped_settings, D(1000), ExecutionMode.PAPER)
    assert paper == book_allocation(shipped_settings, D(1000), ExecutionMode.PAPER)
    assert paper.thesis_book == D(950)  # the owner's share of 1, less the cash floor
    live = await allocation_now(store_worker, shipped_settings, D(1000), ExecutionMode.LIVE)
    assert (live.core, live.satellite, live.thesis_book) == (D(950), D(0), D(0))
    await store_worker.run(
        repo.record_allocation, AllocationEvent(AT, THESIS_BOOK, D("0.10"), "allocation rule", "r")
    )
    live = await allocation_now(store_worker, shipped_settings, D(1000), ExecutionMode.LIVE_SMALL)
    assert (live.core, live.satellite, live.thesis_book) == (D(950), D(100), D(100))


# ── the registry's layers ──


REGISTRY = """
patterns:
  - id: retired-one
    status: retired
  - id: shadow-one
    status: shadow
    outcome:
      decision: inconclusive
      labels: [positive]
      estimate: "0.004"
      interval: ["0.001", "0.007"]
      theta: "0.005"
      metric: signed_excess_log_return
  - id: active-one
    status: active
    sleeve: options
    budget: layer
    outcome: {decision: sufficient, theta: "0.01"}
    monitoring: {mean: "0.01", sd: "0.04", se: "0.002"}
"""


def test_the_registry_s_running_layers(tmp_path: Path) -> None:
    path = tmp_path / "pattern_registry.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    found = registry_layers(path)
    assert [layer.id for layer in found] == ["shadow-one", "active-one"]
    shadow, active = found
    assert shadow.interval == (0.001, 0.007)
    assert shadow.labels == ("positive",)
    assert active.sleeve is Sleeve.OPTIONS
    assert active.backtest == Backtest(0.01, 0.04, 0.002)
    assert registry_layers(tmp_path / "missing.yaml") == []
    path.write_text("patterns:\n  - status: shadow\n", encoding="utf-8")
    with pytest.raises(LayerRegistryError):
        registry_layers(path)
    path.write_text("patterns:\n  - {id: x, status: shadow, budget: all}\n", encoding="utf-8")
    with pytest.raises(LayerRegistryError, match="budget"):
        registry_layers(path)


def test_the_shipped_registry_has_no_running_layer_yet(repo_root: Path) -> None:
    assert registry_layers(repo_root / "research" / "pattern_registry.yaml") == []


def test_a_layer_s_state_from_its_events() -> None:
    def event(kind: str, minutes: int) -> LayerEvent:
        return LayerEvent(AT + timedelta(minutes=minutes), "x", kind, "s", "d")  # type: ignore[arg-type]

    assert layer_state("shadow", []) == LayerState("shadow", None, False)
    state = layer_state("shadow", [event("active", 1), event("freeze", 2)])
    assert state.status == "active"
    assert state.frozen
    assert state.activated_at == AT + timedelta(minutes=1)
    assert not layer_state("active", [event("freeze", 1), event("unfreeze", 2)]).frozen
    back = layer_state("active", [event("freeze", 1), event("shadow", 2)])
    assert back.status == "shadow"
    assert not back.frozen


def _forward(mean: float, se: float, *, cohorts: int = 30, effective: float = 40.0) -> Record:
    return Record(
        layer="x",
        calls=1,
        pending=0,
        missing=0,
        graded=1,
        cohorts=cohorts,
        effective=effective,
        observed_rho=None,
        estimate=MeanEstimate(30, mean, se, None, None, (mean - 1.645 * se, mean + 1.645 * se)),
        theta=0.005,
        decision=None,
    )


def layer(decision: str | None = "sufficient", labels: tuple[str, ...] = ()) -> RegistryLayer:
    return RegistryLayer(
        id="p",
        status="shadow",
        sleeve=Sleeve.SATELLITE,
        budget="layer",
        decision=decision,
        labels=labels,
        theta=0.005,
        estimate=0.004,
        interval=(0.001, 0.007),
        metric="signed_excess_log_return",
        backtest=None,
    )


def test_pattern_promotion_is_impossible_before_the_minimum(spec: ScoreboardSpec) -> None:
    young = _forward(0.02, 0.005, cohorts=10)
    decision = promotion_decision(layer(), young, activation=True, spec=spec.patterns, alpha=0.05)
    assert not decision.promote
    assert "cohorts" in decision.reason
    thin = _forward(0.02, 0.005, effective=12.0)
    assert not promotion_decision(
        layer(), thin, activation=True, spec=spec.patterns, alpha=0.05
    ).promote


def test_pattern_promotion_by_its_rule(spec: ScoreboardSpec) -> None:
    good = _forward(0.01, 0.003)
    assert promotion_decision(
        layer(), good, activation=True, spec=spec.patterns, alpha=0.05
    ).promote
    no_condition = promotion_decision(
        layer(), good, activation=False, spec=spec.patterns, alpha=0.05
    )
    assert not no_condition.promote
    assert "activation" in no_condition.reason
    losing = promotion_decision(
        layer(), _forward(-0.02, 0.003), activation=True, spec=spec.patterns, alpha=0.05
    )
    assert not losing.promote
    assert "losing" in losing.reason
    insufficient = promotion_decision(
        layer("insufficient"), good, activation=True, spec=spec.patterns, alpha=0.05
    )
    assert not insufficient.promote
    assert "insufficient" in insufficient.reason


def test_an_inconclusive_positive_pattern_promotes_on_its_pooled_estimate(
    spec: ScoreboardSpec,
) -> None:
    entry = layer("inconclusive", ("positive",))
    strong = promotion_decision(
        entry, _forward(0.02, 0.002), activation=True, spec=spec.patterns, alpha=0.05
    )
    assert strong.promote
    weak = promotion_decision(
        entry, _forward(0.004, 0.003), activation=True, spec=spec.patterns, alpha=0.05
    )
    assert not weak.promote
    assert "pooled" in weak.reason
    other_metric = replace(entry, metric="log_growth_difference")
    assert not promotion_decision(
        other_metric, _forward(0.02, 0.002), activation=True, spec=spec.patterns, alpha=0.05
    ).promote


def test_a_pooled_estimate_by_hand() -> None:
    mean, (low, high) = pooled((0.01, 0.02), (0.03, 0.01), alpha=0.05)
    assert mean == pytest.approx((0.01 / 0.0004 + 0.03 / 0.0001) / (1 / 0.0004 + 1 / 0.0001))
    se = (1 / 0.0004 + 1 / 0.0001) ** -0.5
    assert high - low == pytest.approx(2 * 1.6448536 * se, rel=1e-6)
    with pytest.raises(ValueError, match="positive"):
        pooled((0.01, 0.0), (0.02, 0.01), alpha=0.05)


# ── the monthly looks and their actions ──


def months(gaps: list[float], *, twin: float = 0.0, first: int = 1) -> list[LayerMonth]:
    return [
        LayerMonth("p", f"2027-{first + i:02d}", ExecutionMode.LIVE, 1, twin + g, twin, 0.0, AT)
        for i, g in enumerate(gaps)
    ]


def active(at: datetime = datetime(2027, 1, 3, tzinfo=UTC), frozen: bool = False) -> LayerState:
    return LayerState("active", at, frozen)


def test_a_gap_breach_drill_freezes_new_entries(spec: ScoreboardSpec) -> None:
    rng = random.Random(1)
    record = months([rng.gauss(-0.04, 0.005) for _ in range(6)], twin=0.01)
    found = month_looks(
        "p",
        state=active(),
        months=record,
        backtest=Backtest(0.01, 0.04, 0.002),
        spec=spec,
        month="2027-06",
        at=AT,
    )
    assert found is not None
    gap, twin = found.looks
    assert gap.breached
    assert not twin.breached
    assert [e.event for e in found.events] == ["freeze"]
    again = month_looks(
        "p",
        state=active(frozen=True),
        months=record,
        backtest=Backtest(0.01, 0.04, 0.002),
        spec=spec,
        month="2027-06",
        at=AT,
    )
    assert again is not None
    assert again.events == ()
    look = as_record("p", "2027-06", gap, AT)
    assert look.breached
    assert look.evaluable
    assert look.boundary == "gap"


def test_a_twin_breach_drill_returns_the_layer_to_shadow(spec: ScoreboardSpec) -> None:
    record = months([0.0001 * (-1) ** i for i in range(6)], twin=-0.08)
    found = month_looks(
        "p",
        state=active(),
        months=record,
        backtest=Backtest(0.01, 0.03, 0.001),
        spec=spec,
        month="2027-06",
        at=AT,
    )
    assert found is not None
    assert found.looks[1].breached
    assert [e.event for e in found.events] == ["shadow"]


def test_no_look_outside_the_look_months_or_for_a_shadow_layer(spec: ScoreboardSpec) -> None:
    record = months([0.0, 0.0])
    early = month_looks(
        "p", state=active(), months=record, backtest=None, spec=spec, month="2027-02", at=AT
    )
    assert early is None
    assert (
        month_looks(
            "p",
            state=LayerState("shadow", None, False),
            months=record,
            backtest=None,
            spec=spec,
            month="2027-06",
            at=AT,
        )
        is None
    )
    before = month_looks(
        "p", state=active(), months=record, backtest=None, spec=spec, month="2026-12", at=AT
    )
    assert before is None


def test_a_month_without_a_record_or_a_backtest_is_not_evaluable(spec: ScoreboardSpec) -> None:
    gappy = months([0.01, 0.02, 0.0])[:2]
    found = month_looks(
        "p", state=active(), months=gappy, backtest=None, spec=spec, month="2027-03", at=AT
    )
    assert found is not None
    assert not any(look.evaluable for look in found.looks)
    assert "have no record" in found.looks[0].detail
    assert found.events == ()
    whole = months([0.01, -0.01, 0.02])
    no_backtest = month_looks(
        "p", state=active(), months=whole, backtest=None, spec=spec, month="2027-03", at=AT
    )
    assert no_backtest is not None
    assert "no backtest" in no_backtest.looks[1].detail


def test_step_evidence_counts_live_account_months_only() -> None:
    record = [
        LayerMonth("p", "2027-01", ExecutionMode.PAPER, 9, 0.0, 0.0, 0.0, AT),
        *[
            LayerMonth("p", f"2027-{m:02d}", ExecutionMode.LIVE, 3, 0.0, 0.0, 0.0, AT)
            for m in range(2, 8)
        ],
    ]
    looks = [
        Look("p", "2027-06", "gap", 1.0, -3.0, 0.001, False, "d", AT),
        Look("p", "2027-07", "twin", None, None, 0.001, False, "d", AT),
    ]
    found = step_evidence(
        layer=layer(),
        state=active(),
        months=record,
        looks=looks,
        shares=[(datetime(2027, 4, 30, tzinfo=UTC), D("0.50"))],
        activation=True,
        retest=None,
    )
    assert found.live_months == 6
    assert found.closed == 18
    assert found.months_since_step == 3
    assert found.closed_since_step == 9
    assert found.steps_taken == 0
    assert not found.gap_breach_outstanding
    breached = step_evidence(
        layer=layer(),
        state=LayerState("shadow", None, False),
        months=record,
        looks=[Look("p", "2027-07", "twin", -9.0, -3.0, 0.001, True, "d", AT)],
        shares=[],
        activation=True,
        retest=True,
    )
    assert breached.twin_breached
    assert not breached.activation
    assert not no_activation(layer())
    assert no_retest(layer(), record) is None


# ── the thesis book ──


def _h13_look(
    record: str, month: str, *, step: bool = False, back: bool = False, group: str = "book"
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
        t_up=4.5 if step else 0.1,
        t_down=-4.0 if back else 0.1,
        c_up=4.032,
        c_down=3.365,
        without_best=0.005,
        step=step,
        back=back,
        evaluable=True,
        series="[]",
        detail="d",
        at=datetime(2027, 7, 1, tzinfo=UTC),
    )


def _thesis_share(*looks: H13Look) -> Decimal:
    return thesis_book_share(ThesisBookCapital(D("0.10"), D("0.10"), D("0.50"), looks)).share


def test_the_thesis_book_s_live_share_follows_h13_s_looks() -> None:
    fresh = thesis_book_share(ThesisBookCapital(D("0.10"), D("0.10"), D("0.50"), ()))
    assert fresh.share == D("0.10")
    assert "exploration share" in fresh.reason
    # A look that neither steps nor goes back, or a group's look, changes nothing.
    assert _thesis_share(_h13_look("live", "2027-06")) == D("0.10")
    assert _thesis_share(_h13_look("live", "2027-06", back=True, group="source:owner")) == D("0.10")
    # A step at each live look, to the ceiling; shadow and paper never step.
    steps = [_h13_look("live", f"20{27 + i}-06", step=True) for i in range(6)]
    assert _thesis_share(steps[0]) == D("0.20")
    assert _thesis_share(*steps) == D("0.50")
    assert _thesis_share(_h13_look("paper", "2027-06", step=True)) == D("0.10")
    # The paper record below its boundary: no share on the live account.
    paper = thesis_book_share(
        ThesisBookCapital(
            D("0.10"), D("0.10"), D("0.50"), (_h13_look("paper", "2027-06", back=True),)
        )
    )
    assert paper.share == D(0)
    assert "paper record" in paper.reason
    # Back to shadow on the live record wins over every earlier step.
    assert _thesis_share(steps[0], _h13_look("live", "2028-12", back=True)) == D(0)
    # A shadow look decides nothing.
    assert _thesis_share(_h13_look("shadow", "2027-06", back=True)) == D("0.10")
