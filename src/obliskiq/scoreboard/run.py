"""The scoreboard over the store: the calls it grades, the marks it grades them with, the daily
pattern lifecycle, the monthly looks and allocation, and the evidence the promotion gate reads.

Everything here runs inside the store worker (``StoreWorker.run``), synchronously, and writes
only the scoreboard's own append-only records (``store/scoreboard.py``); the live decision path
reads the shares it records (``capital/allocation.py``). Every read is bounded by the day it is
made for.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from ..capital.allocation import (
    ALLOCATION_ACTOR,
    THESIS_BOOK,
    ShareDecision,
    sleeve_budget,
    step_share,
)
from ..capital.twr import Reading, account_flows
from ..config.schema import ObliskConfig
from ..domain.orders import ExecutionMode, IntentRole, IntentState, OrderSide, Sleeve
from ..domain.thesis import ThesisState, View
from ..quant.costs import CostBook
from ..quant.costs import OrderTerms as CostTerms
from ..research.prereg import ScoreboardSpec
from ..store import analyst as analyst_repo
from ..store import breakers as breaker_repo
from ..store import capital as capital_repo
from ..store import fx as fx_repo
from ..store import nav as nav_repo
from ..store import orders as order_repo
from ..store import repos
from ..store import runs as run_repo
from ..store import scoreboard as repo
from ..store import theses as thesis_repo
from ..store import thesis_options as link_repo
from ..store.db import Store
from ..store.scoreboard import AllocationEvent, Call, Direction, LayerEvent
from .calibration import CalibrationState, calibration_state
from .grading import GradedCall, PanelRecord, Record, dedupe, grade_call, panel_record, record
from .layers import (
    SCOREBOARD_ACTOR,
    Activation,
    RegistryLayer,
    Retest,
    ThesisBookCapital,
    allocation_layers,
    as_record,
    layer_rule,
    layer_state,
    month_looks,
    promotion_decision,
    step_evidence,
    stock_book_rule,
    thesis_book_share,
)
from .performance import Series, SleevePnl, nav_index, reference_index, sleeve_pnl
from .promotion import Evidence, RunOutcome
from .rules import GradingRules
from .tca import Execution, analyse

ZERO = Decimal(0)
MONTHS_PER_YEAR = 12
#: The layer of the weekly forecast panel's calls.
PANEL_LAYER = "panel"
#: Theses are graded per source (``thesis:owner``, ``thesis:analyst``) and the analyst's per
#: segment (``analyst:<segment>``): an LLM nomination is an analyst thesis.
THESIS_PREFIX = "thesis:"
ANALYST_PREFIX = "analyst:"
#: The states of an intent still at the broker.
_WORKING = frozenset(
    {IntentState.STAGED, IntentState.SUBMITTED, IntentState.WORKING, IntentState.PARTIALLY_FILLED}
)
#: Intent states that are an order placed and finished.
_FINISHED = frozenset(
    {IntentState.FILLED, IntentState.CANCELLED, IntentState.REJECTED, IntentState.EXPIRED}
)
_LIVE = frozenset({ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE})
_THESIS_STATES = frozenset({ThesisState.APPROVED, ThesisState.ACTIVE, ThesisState.CLOSED})


def end_of(day: date) -> datetime:
    return datetime.combine(day, time.max, tzinfo=UTC).replace(microsecond=0)


# ── marks ──


class StoreMarks:
    """Closes in the base currency from the collected bars and the ledger's rates: the last close
    on or before a day, and the rate on or before that close, each at most ``max_age_days`` old."""

    def __init__(self, store: Store, *, base: str, max_age_days: int, as_of: date) -> None:
        self._store = store
        self._base = base
        self._age = timedelta(days=max_age_days)
        self._as_of = as_of
        self._currency: dict[int, str | None] = {}

    def currency(self, conid: int) -> str | None:
        if conid not in self._currency:
            found = repos.get_instrument(self._store, conid, as_of=self._as_of)
            self._currency[conid] = None if found is None else found.currency
        return self._currency[conid]

    def rate(self, currency: str, on: date) -> Decimal | None:
        if currency == self._base:
            return Decimal(1)
        found = fx_repo.rate_on_or_before(self._store, currency, on)
        if found is None or on - found.on > self._age:
            return None
        return found.rate_to_base

    def price(self, conid: int, on: date) -> Decimal | None:
        if on > self._as_of:
            return None
        bars = repos.bar_series(self._store, conid, as_of=on, limit=1)
        currency = self.currency(conid)
        if not bars or currency is None or on - bars[-1].session_date > self._age:
            return None
        fx = self.rate(currency, bars[-1].session_date)
        return None if fx is None else Decimal(str(bars[-1].close)) * fx

    def mark(self, conid: int, on: date) -> float | None:
        found = self.price(conid, on)
        return None if found is None else float(found)


# ── calls ──


def thesis_calls(store: Store, *, as_of: date) -> list[Call]:
    """Each approved thesis as a call on the day it was approved, per source and, for the
    analyst's, per segment."""
    segments = analyst_repo.thesis_segments(store)
    found: list[Call] = []
    for thesis in thesis_repo.theses(store, as_of=as_of, states=_THESIS_STATES):
        conid = thesis.instrument.conid
        if conid is None or thesis.approved_at is None:
            continue
        direction: Direction = "down" if thesis.view is View.BEARISH else "up"
        detail = f"thesis {thesis.thesis_id}"
        found.append(
            Call(
                f"{THESIS_PREFIX}{thesis.source.value}",
                conid,
                direction,
                thesis.approved_at,
                detail,
            )
        )
        segment = segments.get(thesis.thesis_id)
        if segment is not None:
            found.append(
                Call(f"{ANALYST_PREFIX}{segment}", conid, direction, thesis.approved_at, detail)
            )
    return found


def all_calls(store: Store, *, as_of: date) -> dict[str, list[Call]]:
    """Every layer's calls made by the end of ``as_of``, one per layer, instrument, direction and
    ISO week."""
    calls = [*repo.calls(store, as_of=end_of(as_of)), *thesis_calls(store, as_of=as_of)]
    by_layer: dict[str, list[Call]] = {}
    for call in dedupe(calls):
        by_layer.setdefault(call.layer, []).append(call)
    return by_layer


@dataclass(frozen=True, slots=True)
class GradeBook:
    """Every layer's record and calibration on a day, and the panel's."""

    as_of: date
    records: dict[str, Record]
    calibration: dict[str, CalibrationState]
    panel: PanelRecord | None
    graded: dict[str, list[GradedCall]] = field(default_factory=dict)


def grade_book(
    store: Store, settings: ObliskConfig, spec: ScoreboardSpec, *, as_of: date
) -> GradeBook:
    """Grade every layer's calls on ``as_of``."""
    target = settings.research.decision.target_power
    rules = GradingRules.of(spec, target_power=float(target))
    panel_rules = GradingRules.panel(spec, target_power=float(target))
    marks = StoreMarks(
        store,
        base=settings.account.base_currency,
        max_age_days=spec.grading.max_mark_age_days,
        as_of=as_of,
    )
    reference = settings.portfolio_circuit_breaker.equity_reference_conid
    records: dict[str, Record] = {}
    calibration: dict[str, CalibrationState] = {}
    graded_by: dict[str, list[GradedCall]] = {}
    panel: PanelRecord | None = None
    for layer, calls in sorted(all_calls(store, as_of=as_of).items()):
        layer_rules = panel_rules if layer == PANEL_LAYER else rules
        graded = [
            grade_call(c, mark=marks.mark, reference=reference, rules=layer_rules, as_of=as_of)
            for c in calls
        ]
        graded_by[layer] = graded
        if layer == PANEL_LAYER:
            panel = panel_record(layer, graded, panel_rules, min_names=spec.panel.min_names)
        else:
            records[layer] = record(layer, graded, rules)
        if any(c.probability is not None for c in calls):
            calibration[layer] = calibration_state(layer, graded, layer_rules, spec.calibration)
    return GradeBook(as_of, records, calibration, panel, graded_by)


# ── the daily lifecycle and the monthly run ──


@dataclass
class Changes:
    """What a run recorded: events, looks and shares, for the report and the alerts."""

    events: list[LayerEvent] = field(default_factory=list)
    looks: list[repo.Look] = field(default_factory=list)
    shares: list[AllocationEvent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def run_lifecycle(
    store: Store,
    spec: ScoreboardSpec,
    layers: Sequence[RegistryLayer],
    book: GradeBook,
    *,
    activation: Activation,
    at: datetime,
) -> Changes:
    """Promote the shadow entries whose rule is met; every promotion is logged."""
    changes = Changes()
    alpha = float(spec.grading.alpha)
    for layer in layers:
        state = layer_state(layer.status, repo.layer_events(store, layer.id))
        if state.status != "shadow":
            continue
        forward = book.records.get(layer.id)
        if forward is None:
            changes.notes.append(f"{layer.id}: no call graded yet")
            continue
        decision = promotion_decision(
            layer, forward, activation=activation(layer), spec=spec.patterns, alpha=alpha
        )
        if not decision.promote:
            changes.notes.append(f"{layer.id} stays in shadow: {decision.reason}")
            continue
        event = LayerEvent(at, layer.id, "active", SCOREBOARD_ACTOR, decision.reason)
        repo.record_layer_event(store, event)
        changes.events.append(event)
    return changes


def thesis_book_months(store: Store, *, as_of: date) -> tuple[int, int]:
    """The months of the thesis book's paper and live-account records: from the month of each
    record's first entry fill, counted whole."""
    firsts: dict[str, date] = {}
    for item in order_repo.intent_fills(store):
        if item.sleeve is not Sleeve.SATELLITE or item.role is not IntentRole.ENTRY:
            continue
        record_name = (
            "paper" if item.mode is ExecutionMode.PAPER else "live" if item.mode in _LIVE else None
        )
        day = item.fill.executed_at.date()
        if record_name is not None and day <= as_of:
            firsts.setdefault(record_name, day)

    def months(first: date | None) -> int:
        if first is None:
            return 0
        return (as_of.year - first.year) * MONTHS_PER_YEAR + as_of.month - first.month + 1

    return months(firsts.get("paper")), months(firsts.get("live"))


def run_month(
    store: Store,
    settings: ObliskConfig,
    spec: ScoreboardSpec,
    layers: Sequence[RegistryLayer],
    *,
    month: str,
    h13_first_look: int,
    activation: Activation,
    retest: Retest,
    at: datetime,
) -> Changes:
    """The month-end run: each active layer's looks and their actions, then every layer's
    live-account share. A look or a share is written once per month."""
    changes = Changes()
    for layer in layers:
        state = layer_state(layer.status, repo.layer_events(store, layer.id))
        found = month_looks(
            layer.id,
            state=state,
            months=repo.layer_months(store, layer.id),
            backtest=layer.backtest,
            spec=spec,
            month=month,
            at=at,
        )
        if found is None:
            continue
        for result in found.looks:
            look = as_record(layer.id, month, result, at)
            if repo.record_look(store, look):
                changes.looks.append(look)
        for event in found.events:
            repo.record_layer_event(store, event)
            changes.events.append(event)
    current = repo.current_shares(store)
    history = repo.allocation_events(store)
    by_key = allocation_layers(layers)
    cash_floor = settings.capital.budgets.cash_floor
    for key, layer in by_key.items():
        state = layer_state(layer.status, repo.layer_events(store, layer.id))
        months = repo.layer_months(store, layer.id)
        shares = [(e.at, e.share) for e in history if e.layer == key]
        rule = (
            stock_book_rule(spec, cash_floor=cash_floor)
            if layer.sleeve is Sleeve.SATELLITE
            else layer_rule(spec, sleeve_budget=sleeve_budget(settings, layer.sleeve))
        )
        evidence = step_evidence(
            layer=layer,
            state=state,
            months=months,
            looks=repo.looks(store, layer.id),
            shares=shares,
            activation=activation(layer),
            retest=retest(layer, months),
        )
        _record_share(store, changes, key, step_share(current.get(key, ZERO), rule, evidence), at)
    paper, live = thesis_book_months(store, as_of=at.date())
    decision = thesis_book_share(
        ThesisBookCapital(
            exploration=settings.thesis.capital_share,
            paper_months=paper,
            live_months=live,
            first_look_month=h13_first_look,
        )
    )
    _record_share(store, changes, THESIS_BOOK, decision, at)
    return changes


def _record_share(
    store: Store, changes: Changes, key: str, decision: ShareDecision, at: datetime
) -> None:
    current = repo.current_shares(store)
    if key in current and current[key] == decision.share:
        return
    if key not in current and decision.share == ZERO:
        return
    event = AllocationEvent(at, key, decision.share, ALLOCATION_ACTOR, decision.reason)
    repo.record_allocation(store, event)
    changes.shares.append(event)


# ── the promotion gate's evidence ──


def executions(
    store: Store, settings: ObliskConfig, *, mode: ExecutionMode, since: datetime | None
) -> list[Execution]:
    """The bot's broker fills in ``mode`` (since ``since``) with what TCA measures them against:
    the arrival quote of their placement's clearance and the cost model's spread and slippage for
    the leg."""
    costs = CostBook.from_config(settings)
    found: list[Execution] = []
    for item in order_repo.intent_fills(store):
        fill = item.fill
        intent_id = item.intent_id
        if item.mode is not mode or fill.simulated or intent_id is None:
            continue
        if since is not None and fill.executed_at < since:
            continue
        stored = order_repo.get_intent(store, intent_id)
        if stored is None:
            continue
        intent = stored.intent
        clearance = order_repo.placement_clearance(store, intent_id)
        quote = None if clearance is None else clearance.quote
        modelled: Decimal | None = None
        if clearance is not None and clearance.currency is not None:
            leg = costs.leg_cost(
                CostTerms(
                    venue=intent.terms.venue,
                    asset_class=clearance.asset_class,
                    currency=clearance.currency,
                    quantity=fill.quantity,
                    price=fill.price,
                ),
                side="buy" if fill.side is OrderSide.BUY else "sell",
                on=fill.executed_at.date(),
            )
            if leg.spread_cost is not None and leg.slippage_cost is not None and leg.notional:
                modelled = (leg.spread_cost + leg.slippage_cost) / leg.notional
        found.append(
            Execution(
                executed_at=fill.executed_at,
                mode=intent.mode,
                venue=intent.terms.venue,
                order_type=intent.terms.price.kind,
                quote_status="none recorded"
                if quote is None or quote.status is None
                else quote.status.value,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                arrival=None if quote is None else quote.reference(),
                modelled=modelled,
            )
        )
    return found


def promotion_evidence(
    store: Store,
    settings: ObliskConfig,
    *,
    mode: ExecutionMode,
    now: datetime,
    sessions: Callable[[date, date], Sequence[date]],
) -> Evidence:
    """What the store says of the current mode's period, for the promotion gate."""
    changes = tuple(order_repo.mode_changes(store))
    entered = changes[-1].at if changes and changes[-1].to_mode is mode else None
    first = (entered or now).date()
    runs = tuple(
        RunOutcome(r.session_date, r.command, r.status)
        for r in run_repo.runs_since(store, since=first)
    )
    reconciled = tuple(order_repo.reconciliation_statuses(store, since=entered or now, as_of=now))
    cutoff = now - timedelta(minutes=settings.runtime.stuck_after_minutes)
    stuck = sum(
        1
        for stored in order_repo.intents_in_states(store, _WORKING)
        if stored.intent.expires_at < cutoff
    )
    placed = filled = 0
    for stored in order_repo.intents_in_states(store, _FINISHED):
        intent = stored.intent
        if intent.mode is not mode or (entered is not None and intent.created_at < entered):
            continue
        if intent.role not in (IntentRole.ENTRY, IntentRole.EXIT):
            continue
        if order_repo.spent_effect(store, intent.intent_id) is None:
            continue
        placed += 1
        filled += intent.state is IntentState.FILLED
    violations = order_repo.fills_without_clearance(store, mode, since=entered or now, as_of=now)
    measured = executions(store, settings, mode=mode, since=entered)
    return Evidence(
        now=now,
        mode=mode,
        changes=changes,
        sessions=tuple(sessions(first, now.date())),
        runs=runs,
        reconciliations=reconciled,
        stuck_intents=stuck,
        relogins_recorded=False,
        placed=placed,
        filled=filled,
        fills_without_clearance=violations,
        tca=analyse(measured),
        drills=tuple(repo.drills(store)),
        capped_book=settings.capital.mode == "cap",
        hard_breaker_tripped=breaker_repo.tripped(store, "broker") is not None,
    )


# ── performance ──


@dataclass(frozen=True, slots=True)
class PerformanceView:
    """The account's growth and the references' over a period, and each sleeve's profit at its
    end (``sleeves`` is why not, when it cannot be computed)."""

    since: date
    as_of: date
    account: Series
    references: tuple[Series, ...]
    sleeves: list[SleevePnl] | str


def performance(
    store: Store, settings: ObliskConfig, *, since: date, as_of: date
) -> PerformanceView:
    """The account's time-weighted growth from ``since`` to ``as_of``, the configured references
    over the same NAV days, and each sleeve's profit on ``as_of``."""
    readings = [
        Reading(r.as_of, r.nav_base) for r in nav_repo.nav_series(store, since=since, as_of=as_of)
    ]
    marks = StoreMarks(
        store,
        base=settings.account.base_currency,
        max_age_days=settings.scoreboard.max_mark_age_days,
        as_of=as_of,
    )
    flows = account_flows(
        capital_repo.cash_flows(store, as_of=as_of),
        base=settings.account.base_currency,
        rate=marks.rate,
    )
    account = (
        Series("account", (), flows)
        if isinstance(flows, str)
        else nav_index(readings, flows, timing=settings.capital.twr.flow_timing)
    )
    days = [r.on for r in readings]
    references = tuple(
        reference_index(
            reference.name,
            {member.conid: member.weight for member in reference.members},
            days,
            marks.price,
        )
        for reference in settings.scoreboard.references
    )
    sleeves = sleeve_pnl(
        order_repo.intent_fills(store),
        currency=marks.currency,
        multiplier=lambda conid: _multiplier(store, conid),
        rate=marks.rate,
        price=marks.price,
        on=as_of,
    )
    return PerformanceView(since, as_of, account, references, sleeves)


def _multiplier(store: Store, conid: int) -> Decimal:
    """An option's contract multiplier from the thesis options it was chosen for; one for
    anything else."""
    found = link_repo.option_multiplier(store, conid)
    return Decimal(1) if found is None else found
