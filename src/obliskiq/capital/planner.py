"""The reservation planner: a dated withdrawal is planned for the moment it is announced
(MASTER_PLAN §5.15, DECISIONS D108).

Investable capital drops by every open reservation at once (``capital/managed.py``), so the
targets are recomputed on the reduced capital. The planner then raises the cash, reservation by
reservation, earliest due first:

1. **The cash line** first: the book's cash, and the proceeds of the sales already working.
2. **Sales**, chosen to minimise ``tax + trading + strategy`` cost per euro raised, each weighed by
   ``capital.planner.weights``: the tax the sale adds to its year (Session 4b's lot engine on the
   live lots, so losses come before gains and a loss the two-month rule would defer saves nothing),
   its trading cost (commission, spread and slippage), and its strategy cost from
   :class:`~obliskiq.capital.strategy.StrategyValue`: least for exits the strategy makes anyway and
   the part of a core role above its target (what rebalancing would trim anyway), more for an
   ordinary holding, most for a protected one. A position is sold first in, first out, in whole
   units; a sale whose tax or trading cost is unknown comes after every one whose cost is known.
3. **Protected positions** (inside an entry window, a probe, a pending catalyst) are cut only after
   the owner says so: when the rest cannot meet the date the planner asks
   (:class:`~obliskiq.domain.capital.OwnerQuestion`): postpone to a proposed date, reduce the
   amount, or proceed, and, where the date allows, sell after year-end. Without an answer by the
   deadline the withdrawal wins and the plan executes.

**Settlement.** A sale must settle by the due date less ``capital.reservations.buffer_days``; the
last safe session is the last session of the sale's venue whose T+n (``settlement_sessions``, on
the venue's calendar) still meets it, so a holiday moves it earlier. A sale that cannot settle in
time is not planned.

**Pacing** (``capital.pacing``). A sale the strategy would make anyway, an overweight, or a
position expected to do little is sold at once (in ``tranches``, one per session); a position
expected to do well, and a protected one, is held to the last ``hold_sessions`` safe sessions —
with a later date offered to the owner when its expected return is strong — and is never held past
the last safe session. An expected return counts only from a layer whose outcome is sufficient.

:meth:`ReservationPlanner.survey` reads and writes nothing; :meth:`ReservationPlanner.apply` asks
the questions the survey found and makes today's sales intents (exits with the reason
``withdrawal``). The plan is surveyed again at every cycle, so what was sold, what arrived and what
the owner answered are always read afresh.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..domain.capital import (
    OptionEffect,
    OwnerQuestion,
    QuestionKind,
    QuestionOption,
    QuestionState,
    WithdrawalReservation,
)
from ..domain.orders import (
    TERMINAL_STATES,
    ExecutionMode,
    IntentState,
    OrderIntent,
    OrderSide,
    ReconcileStatus,
    Sleeve,
)
from ..errors import CalendarError, ObliskError, TaxError
from ..execution.intent_maker import IntentMaker
from ..execution.killswitch import KillSwitch
from ..logging_setup import get_logger
from ..quant.costs import CostBook
from ..quant.costs import OrderTerms as CostTerms
from ..store import capital as capital_repo
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..tax.book import TaxBook, TaxHooks
from ..util.timeutil import utc_now
from .budgets import SleeveBudgets
from .managed import BookHolding, CapitalReader, CapitalView, book_fills
from .questions import OwnerQuestions
from .strategy import ExpectedReturn, PositionFacts, Protected, StrategyValue

log = get_logger("capital.planner")

ZERO = Decimal(0)

#: The exit reason a planned sale gives (``discipline.exit_reasons_allowed``).
REASON = "withdrawal"
#: Who the audit names for a question the planner asks.
PLANNER_ACTOR = "reservation planner"


class Category(StrEnum):
    """What selling a slice of a position costs the strategy, in the default ranking."""

    EXIT_ANYWAY = "exit_anyway"
    OVERWEIGHT = "overweight"
    ORDINARY = "ordinary"
    PROTECTED = "protected"


class Pace(StrEnum):
    """When a planned sale is made."""

    NOW = "now"  # at the next window, one order: the strategy sells it anyway or expects little
    TRANCHES = "tranches"  # from the next window, ``capital.pacing.tranches`` sessions
    HOLD = "hold"  # over the last ``capital.pacing.hold_sessions`` safe sessions


@dataclass(frozen=True, slots=True)
class PlannedSale:
    """One sale of a reservation's plan, and when it is made."""

    reservation_id: int
    conid: int
    symbol: str
    venue: str
    sleeve: Sleeve
    units: Decimal
    proceeds: Decimal
    category: Category
    tax: Decimal | None
    trading: Decimal | None
    strategy: Decimal
    pace: Pace
    start: date
    last_safe: date
    today_units: Decimal
    reason: str


@dataclass
class ReservationLine:
    """How one reservation is met."""

    reservation: WithdrawalReservation
    amount_base: Decimal
    from_cash: Decimal = ZERO
    sales: list[PlannedSale] = field(default_factory=list)
    shortfall: Decimal = ZERO
    status: str = "funded"
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"R{self.reservation.reservation_id}"


@dataclass
class ReservationPlan:
    """The plan for every open reservation, and the questions it needs asked."""

    mode: ExecutionMode
    at: datetime
    capital: CapitalView | None = None
    lines: list[ReservationLine] = field(default_factory=list)
    to_ask: list[OwnerQuestion] = field(default_factory=list)
    blocked: str | None = None

    @property
    def sales_today(self) -> list[PlannedSale]:
        return [sale for line in self.lines for sale in line.sales if sale.today_units > ZERO]

    @property
    def problems(self) -> list[str]:
        """Reservations the plan cannot meet by their date."""
        return [
            f"{line.label}: {line.status}"
            for line in self.lines
            if line.shortfall > ZERO and not line.status.startswith("waiting")
        ]

    def render(self) -> str:
        lines = [f"reservation plan ({self.mode.value}) at {self.at.isoformat()}"]
        if self.blocked is not None:
            lines.append(f"  nothing planned: {self.blocked}")
        if self.capital is not None:
            c = self.capital
            lines.append(
                f"  managed {c.managed:.2f}, reserved {c.reserved:.2f}, investable "
                f"{c.investable:.2f}, the book's cash {c.book_cash:.2f}"
            )
        for line in self.lines:
            r = line.reservation
            lines.append(
                f"  {line.label} {r.amount} {r.currency} due {r.due_date.isoformat()} "
                f"({r.reason}): {line.status}; {line.from_cash:.2f} from the cash line"
            )
            for sale in line.sales:
                tax = "unknown" if sale.tax is None else f"{sale.tax:.2f}"
                trading = "unknown" if sale.trading is None else f"{sale.trading:.2f}"
                lines.append(
                    f"    sell {sale.units} {sale.symbol} ({sale.category.value}, {sale.pace.value}"
                    f" from {sale.start.isoformat()}, last safe {sale.last_safe.isoformat()}) "
                    f"for about {sale.proceeds:.2f}: tax {tax}, trading {trading}, strategy "
                    f"{sale.strategy:.2f}; today {sale.today_units} — {sale.reason}"
                )
            lines += [f"    note: {note}" for note in line.notes]
        lines += [f"  asks: {q.context}" for q in self.to_ask]
        return "\n".join(lines)


@dataclass
class PlannerActions:
    """What :meth:`ReservationPlanner.apply` did: the questions asked, the intents made and the
    sales that wait for a later window."""

    asked: list[OwnerQuestion] = field(default_factory=list)
    intents: list[OrderIntent] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)


@dataclass
class _Slice:
    units: Decimal
    category: Category


@dataclass
class _Candidate:
    """A held position the plan may sell from: its slices, in the order they would be sold, hold
    the units no earlier reservation took."""

    holding: BookHolding
    unit: Decimal
    symbol: str
    venue: str
    last_safe: date
    facts: PositionFacts
    protection: Protected | None
    expected: ExpectedReturn | None
    slices: list[_Slice]

    @property
    def conid(self) -> int:
        return self.holding.conid

    @property
    def blocks(self) -> bool:
        """Whether its protection still holds on its last safe day for the reservation in hand:
        a protection that ends before it lets the position be sold after it ends."""
        protection = self.protection
        return protection is not None and (
            protection.until is None or protection.until > self.last_safe
        )


@dataclass
class _Chosen:
    candidate: _Candidate
    units: Decimal
    category: Category
    tax: Decimal | None
    trading: Decimal | None
    strategy: Decimal

    @property
    def proceeds(self) -> Decimal:
        return self.units * self.candidate.unit


def _whole(value: Decimal, rounding: str) -> Decimal:
    return value.to_integral_value(rounding=rounding)


class ReservationPlanner:
    """Plans and paces the sales that meet open reservations, and asks before cutting."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        capital: CapitalReader,
        tax: TaxBook,
        costs: CostBook,
        calendars: Calendars,
        strategy: StrategyValue,
        worker: StoreWorker,
        maker: IntentMaker,
        questions: OwnerQuestions,
        kill: KillSwitch,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._questions = questions
        self._capital = capital
        self._tax = tax
        self._costs = costs
        self._calendars = calendars
        self._strategy = strategy
        self._worker = worker
        self._maker = maker
        self._kill = kill
        self._now = now
        config = settings.capital
        self._planner = config.planner
        self._pacing = config.pacing
        self._buffer = timedelta(days=config.reservations.buffer_days)
        self._postpone = timedelta(days=config.questions.postpone_days)
        self._budgets = SleeveBudgets(config)
        self._weights = {t.conid: t.weight for t in settings.core.skeleton_targets}

    # ── the survey ──

    async def survey(self, mode: ExecutionMode) -> ReservationPlan:
        """The plan now: reads only, writes nothing."""
        at = self._now()
        plan = ReservationPlan(mode=mode, at=at)
        reservations = await self._capital.open_reservations()
        if not reservations:
            return plan
        if await self._kill.is_on():
            plan.blocked = "the kill switch is on: no new decision"
            return plan
        latest = await self._worker.run(repo.latest_reconciliation)
        if latest is not None and latest.status is not ReconcileStatus.GREEN:
            plan.blocked = f"reconciliation is {latest.status.value}: the plan waits"
            return plan
        try:
            cash = await self._capital.cash()
        except ObliskError as exc:
            plan.blocked = f"the account cannot be read: {exc}"
            return plan
        view = await self._capital.read(mode, cash=cash)
        if isinstance(view, str):
            plan.blocked = view
            return plan
        plan.capital = view
        try:
            hooks: TaxHooks | None = await self._tax.hooks()
        except TaxError as exc:
            log.warning("the tax lots cannot be read", extra={"error": str(exc)})
            hooks = None
        today = at.date()
        busy, pending = await self._working(mode, view)
        available = max(view.book_cash, ZERO) + pending
        candidates = await self._candidates(view, mode, busy, today)
        asked = await self._worker.run(capital_repo.questions)
        chosen_all: list[_Chosen] = []
        for reservation in reservations:
            rate = await self._capital.valuer.rate(reservation.currency, cash)
            if rate is None:
                plan.blocked = f"{reservation.currency} has no rate to the base currency"
                return plan
            line = ReservationLine(reservation, reservation.amount * rate)
            plan.lines.append(line)
            line.from_cash = min(available, line.amount_base)
            available -= line.from_cash
            need = line.amount_base - line.from_cash
            if need <= ZERO:
                line.status = "funded: met from the cash line and the sales already working"
                continue
            await self._plan_line(
                plan, line, need, candidates, chosen_all, hooks, asked, today, rate
            )
        return plan

    async def _working(self, mode: ExecutionMode, view: CapitalView) -> tuple[set[int], Decimal]:
        """Conids an open intent of this mode works on, and the proceeds the open withdrawal
        sales will still bring."""
        open_states = [s for s in IntentState if s not in TERMINAL_STATES]
        busy: set[int] = set()
        pending = ZERO
        for stored in await self._worker.run(repo.intents_in_states, open_states):
            intent = stored.intent
            if intent.mode is not mode or intent.terms.conversion:
                continue
            busy.add(intent.conid)
            if intent.side is OrderSide.SELL and intent.reason == REASON:
                holding = view.holding(intent.conid)
                unit = None if holding is None else holding.unit.unit
                quantity = intent.terms.quantity or ZERO
                fills = await self._worker.run(repo.fills_for_intent, intent.intent_id)
                left = max(ZERO, quantity - sum((f.quantity for f in fills), ZERO))
                if unit is not None:
                    pending += left * unit
        return busy, pending

    async def _opened(self, mode: ExecutionMode) -> dict[int, date]:
        """The day each position was last opened (its units rose from zero)."""
        units: dict[int, Decimal] = {}
        opened: dict[int, date] = {}
        for entry in book_fills(await self._worker.run(repo.intent_fills), mode):
            fill = entry.fill
            before = units.get(fill.conid, ZERO)
            after = before + (fill.quantity if fill.side is OrderSide.BUY else -fill.quantity)
            if before <= ZERO < after:
                opened[fill.conid] = fill.executed_at.date()
            units[fill.conid] = after
        return opened

    async def _candidates(
        self, view: CapitalView, mode: ExecutionMode, busy: set[int], today: date
    ) -> list[_Candidate]:
        opened = await self._opened(mode)
        # An overweight is what rebalancing would trim anyway: the part of a core role above its
        # target before the reservations, not the shrinking every role shows on the reduced
        # capital, which is the reservation itself.
        before = view.investable + view.reserved
        allocation = self._budgets.allocate(before, active={Sleeve.CORE})
        found: list[_Candidate] = []
        for holding in view.holdings:
            unit, instrument = holding.unit.unit, holding.unit.instrument
            if holding.conid in busy or unit is None or instrument is None or holding.units <= 0:
                continue
            try:
                venue = self._calendars.venue_for_ibkr(instrument.listing_exchange)
            except CalendarError:
                continue
            facts = PositionFacts(
                conid=holding.conid,
                sleeve=holding.sleeve,
                units=holding.units,
                opened_on=opened.get(holding.conid),
            )
            protection = self._strategy.protection(facts, today=today)
            expected = self._strategy.expected_return(facts, today=today)
            slices: list[_Slice] = []
            rest = holding.units
            if self._strategy.would_exit(facts, today=today):
                slices.append(_Slice(rest, Category.EXIT_ANYWAY))
                rest = ZERO
            elif holding.sleeve is Sleeve.CORE:
                target = self._weights.get(holding.conid, ZERO) * allocation.core
                over = _whole(max(ZERO, holding.units - target / unit), ROUND_FLOOR)
                if over > ZERO:
                    slices.append(_Slice(min(over, rest), Category.OVERWEIGHT))
                    rest -= min(over, rest)
            if rest > ZERO:
                kind = Category.PROTECTED if protection is not None else Category.ORDINARY
                slices.append(_Slice(rest, kind))
            found.append(
                _Candidate(
                    holding=holding,
                    unit=unit,
                    symbol=instrument.symbol,
                    venue=venue,
                    last_safe=today,  # set per reservation
                    facts=facts,
                    protection=protection,
                    expected=expected,
                    slices=slices,
                )
            )
        return found

    # ── one reservation ──

    async def _plan_line(
        self,
        plan: ReservationPlan,
        line: ReservationLine,
        need: Decimal,
        candidates: list[_Candidate],
        chosen_all: list[_Chosen],
        hooks: TaxHooks | None,
        asked: Sequence[OwnerQuestion],
        today: date,
        rate: Decimal,
    ) -> None:
        reservation = line.reservation
        settle_by = reservation.due_date - self._buffer
        usable: list[_Candidate] = []
        for candidate in candidates:
            try:
                last = self._calendars.last_safe_session(candidate.venue, settle_by, from_day=today)
            except CalendarError as exc:
                line.notes.append(f"{candidate.symbol}: {exc}")
                continue
            if last is None:
                line.notes.append(
                    f"{candidate.symbol}: no session of {candidate.venue} settles by "
                    f"{settle_by.isoformat()}"
                )
                continue
            candidate.last_safe = last
            usable.append(candidate)
        mine = [q for q in asked if q.reservation_id == reservation.reservation_id]
        decided = self._decided(mine, QuestionKind.PROTECTED_CUT)
        protected_ok = decided is not None and decided.effect in (
            OptionEffect.PROCEED,
            OptionEffect.AFTER_YEAR_END,
        )
        not_before = (
            decided.not_before
            if decided is not None and decided.effect is OptionEffect.AFTER_YEAR_END
            else None
        )
        tax_day = today if not_before is None else not_before
        picked, need = self._select(usable, need, chosen_all, hooks, tax_day, protected_ok=False)
        self._commit(picked)
        protected_needed: list[_Chosen] = []
        if need > ZERO:
            extra, rest = self._select(
                usable, need, chosen_all + picked, hooks, tax_day, protected_ok=True
            )
            if protected_ok:
                self._commit(extra)
                picked += extra
                need = rest
            else:
                protected_needed = [
                    c for c in extra if c.category is Category.PROTECTED and c.candidate.blocks
                ]
        chosen_all += picked
        for choice in picked:
            line.sales.append(self._schedule(line, choice, today, not_before))
        later = self._later_date_question(line, picked, mine)
        if later is not None:
            plan.to_ask.append(later)
        line.shortfall = max(ZERO, need)
        if line.shortfall <= ZERO:
            line.status = "planned"
            return
        if protected_needed:
            open_one = next(
                (
                    q
                    for q in mine
                    if q.kind is QuestionKind.PROTECTED_CUT and q.state is QuestionState.OPEN
                ),
                None,
            )
            if open_one is None:
                plan.to_ask.append(
                    self._protected_question(line, picked, protected_needed, today, rate)
                )
                line.status = "waiting for the owner's answer: meeting it cuts protected positions"
            else:
                line.status = f"waiting for the owner's answer (Q{open_one.question_id})"
            return
        line.status = (
            f"short by {line.shortfall:.2f}: nothing else can be sold in time for "
            f"{reservation.due_date.isoformat()}"
        )

    @staticmethod
    def _decided(questions: Sequence[OwnerQuestion], kind: QuestionKind) -> QuestionOption | None:
        """The option in force from the latest closed question of ``kind``."""
        closed = [q for q in questions if q.kind is kind and q.state is not QuestionState.OPEN]
        return closed[-1].chosen if closed else None

    def _select(
        self,
        candidates: Sequence[_Candidate],
        need: Decimal,
        before: Sequence[_Chosen],
        hooks: TaxHooks | None,
        on: date,
        *,
        protected_ok: bool,
    ) -> tuple[list[_Chosen], Decimal]:
        """Greedy: the cheapest next slice per unit of money raised until ``need`` is met, a
        slice whose tax or trading cost is unknown after every known one. Nothing is taken from
        the candidates (:meth:`_commit` does that)."""
        left = {c.conid: [piece.units for piece in c.slices] for c in candidates}
        picked: list[_Chosen] = []
        while need > ZERO:
            best: tuple[tuple[int, Decimal], _Chosen, int] | None = None
            for candidate in candidates:
                index = next(
                    (i for i, units in enumerate(left[candidate.conid]) if units > 0), None
                )
                if index is None:
                    continue
                category = candidate.slices[index].category
                if category is Category.PROTECTED and candidate.blocks and not protected_ok:
                    continue
                wanted = max(Decimal(1), _whole(need / candidate.unit, ROUND_CEILING))
                units = min(left[candidate.conid][index], wanted)
                choice = self._cost(candidate, units, category, [*before, *picked], hooks, on)
                unknown = int(choice.tax is None) + int(choice.trading is None)
                key = (unknown, self._score(choice) / choice.proceeds)
                if best is None or key < best[0]:
                    best = (key, choice, index)
            if best is None:
                break
            _, choice, index = best
            left[choice.candidate.conid][index] -= choice.units
            picked.append(choice)
            need -= choice.proceeds
        return picked, need

    @staticmethod
    def _commit(picked: Sequence[_Chosen]) -> None:
        """Take the chosen units from their candidates, first slice first."""
        for choice in picked:
            units = choice.units
            for piece in choice.candidate.slices:
                take = min(piece.units, units)
                piece.units -= take
                units -= take
                if units <= ZERO:
                    break

    def _score(self, choice: _Chosen) -> Decimal:
        weights = self._planner.weights
        return (
            weights.tax * (choice.tax or ZERO)
            + weights.trading * (choice.trading or ZERO)
            + weights.strategy * choice.strategy
        )

    def _cost(
        self,
        candidate: _Candidate,
        units: Decimal,
        category: Category,
        before: Sequence[_Chosen],
        hooks: TaxHooks | None,
        on: date,
    ) -> _Chosen:
        value = units * candidate.unit
        strategy = value * self._strategy_share(candidate, category)
        return _Chosen(
            candidate=candidate,
            units=units,
            category=category,
            tax=self._tax_of(candidate, units, before, hooks, on),
            trading=self._trading_of(candidate, units, on),
            strategy=strategy,
        )

    def _strategy_share(self, candidate: _Candidate, category: Category) -> Decimal:
        shares = self._planner.strategy_cost
        if category is Category.EXIT_ANYWAY:
            return ZERO
        if category is Category.OVERWEIGHT:
            return shares.overweight
        expected = candidate.expected
        if expected is not None and expected.usable:
            return max(expected.annual, shares.overweight)
        return shares.protected if category is Category.PROTECTED else shares.ordinary

    def _tax_of(
        self,
        candidate: _Candidate,
        units: Decimal,
        before: Sequence[_Chosen],
        hooks: TaxHooks | None,
        on: date,
    ) -> Decimal | None:
        if hooks is None or not hooks.complete(candidate.conid, candidate.holding.units):
            return None
        prior = [
            (c.candidate.conid, c.units, c.candidate.unit)
            for c in before
            if hooks.complete(c.candidate.conid, c.candidate.holding.units)
        ]
        try:
            with_it = hooks.trial_tax([*prior, (candidate.conid, units, candidate.unit)], on=on)
            without = hooks.trial_tax(prior, on=on)
        except TaxError:
            return None
        return with_it - without

    def _trading_of(self, candidate: _Candidate, units: Decimal, on: date) -> Decimal | None:
        instrument = candidate.holding.unit.instrument
        rate = candidate.holding.unit.rate
        if instrument is None or rate is None or rate <= ZERO:
            return None
        price = candidate.unit / (instrument.multiplier * rate)
        try:
            leg = self._costs.leg_cost(
                CostTerms(
                    venue=candidate.venue,
                    asset_class=instrument.asset_class,
                    currency=instrument.currency,
                    quantity=units,
                    price=price,
                ),
                side="sell",
                on=on,
                multiplier=instrument.multiplier,
            )
        except ValueError:
            return None
        total = leg.total
        return None if total is None else total * rate

    # ── pacing ──

    def _pace(self, candidate: _Candidate, category: Category) -> tuple[Pace, str]:
        """How a sale is paced: at once when the strategy sells it anyway or expects little from
        it, held to the last safe days when it expects a strong move (or, without a usable
        expected return, when the position is protected), in tranches otherwise."""
        if category in (Category.EXIT_ANYWAY, Category.OVERWEIGHT):
            return Pace.NOW, f"{category.value.replace('_', ' ')}: sold at once, proceeds set aside"
        expected = candidate.expected
        if expected is not None and expected.usable:
            said = f"{expected.layer} expects {expected.annual:.1%} a year"
            if expected.annual >= self._pacing.strong_above:
                return Pace.HOLD, f"{said}: held to the last safe days"
            if expected.annual < self._pacing.weak_below:
                return Pace.NOW, f"{said}: sold at once, proceeds set aside"
            return Pace.TRANCHES, f"{said}: sold in tranches, proceeds set aside"
        if category is Category.PROTECTED and candidate.protection is not None:
            why = candidate.protection.why.value.replace("_", " ")
            return Pace.HOLD, f"protected ({why}): held to the last safe days"
        return Pace.TRANCHES, "sold in tranches from the next window, proceeds set aside"

    def _sessions(self, venue: str, start: date, end: date) -> list[date]:
        days: list[date] = []
        day: date | None = start
        while day is not None and day <= end:
            if self._calendars.session_bounds(venue, day) is not None:
                days.append(day)
            day = self._calendars.next_session(venue, day)
        return days

    def _first_session(self, venue: str, on_or_after: date) -> date | None:
        if self._calendars.session_bounds(venue, on_or_after) is not None:
            return on_or_after
        return self._calendars.next_session(venue, on_or_after)

    def _schedule(
        self, line: ReservationLine, choice: _Chosen, today: date, not_before: date | None
    ) -> PlannedSale:
        candidate = choice.candidate
        venue, last = candidate.venue, candidate.last_safe
        pace, why = self._pace(candidate, choice.category)
        start = self._first_session(venue, today) or last
        if pace is Pace.HOLD:
            back = last
            for _ in range(self._pacing.hold_sessions - 1):
                earlier = self._calendars.previous_session(venue, back)
                if earlier is None or earlier < today:
                    break
                back = earlier
            start = max(start, back)
        protection = candidate.protection
        if choice.category is Category.PROTECTED and protection is not None and protection.until:
            freed = self._first_session(venue, protection.until)
            if freed is not None and freed <= last:
                start = max(start, freed)
        if not_before is not None:
            first = self._first_session(venue, not_before)
            if first is not None:
                start = max(start, first)
        if start > last:
            start = last
        window = self._sessions(venue, start, last)
        if pace is Pace.NOW:
            window = window[:1]
        elif pace is Pace.TRANCHES:
            window = window[: self._pacing.tranches]
        today_units = ZERO
        if window and today >= window[0]:
            remaining = [day for day in window if day >= today] or [window[-1]]
            count = len(remaining)
            today_units = min(choice.units, _whole(choice.units / count, ROUND_CEILING))
        if today > last:
            today_units = choice.units
            why += "; past its last safe day, sold now"
        assert line.reservation.reservation_id is not None
        return PlannedSale(
            reservation_id=line.reservation.reservation_id,
            conid=candidate.conid,
            symbol=candidate.symbol,
            venue=venue,
            sleeve=candidate.holding.sleeve,
            units=choice.units,
            proceeds=choice.proceeds,
            category=choice.category,
            tax=choice.tax,
            trading=choice.trading,
            strategy=choice.strategy,
            pace=pace,
            start=start,
            last_safe=last,
            today_units=today_units,
            reason=why,
        )

    # ── the question ──

    def _protected_question(
        self,
        line: ReservationLine,
        picked: Sequence[_Chosen],
        protected: Sequence[_Chosen],
        today: date,
        rate: Decimal,
    ) -> OwnerQuestion:
        reservation = line.reservation
        assert reservation.reservation_id is not None
        everything = [*picked, *protected]
        tax_all = self._sum_tax(everything)
        strategy_all = sum((c.strategy for c in everything), ZERO)
        ordinary = self._planner.strategy_cost.ordinary
        names = ", ".join(
            f"{c.candidate.symbol} ({self._protection_text(c.candidate)})" for c in protected
        )
        last = min(c.candidate.last_safe for c in protected)
        venue = protected[0].candidate.venue
        hold_start = last
        for _ in range(self._pacing.hold_sessions - 1):
            earlier = self._calendars.previous_session(venue, hold_start)
            if earlier is None or earlier < today:
                break
            hold_start = earlier
        bounds = self._calendars.session_bounds(venue, hold_start)
        deadline = self._now() if bounds is None else max(self._now(), bounds.open_utc)
        options = [
            QuestionOption(
                key="proceed",
                effect=OptionEffect.PROCEED,
                label=f"sell what the date needs, {names} included",
                tax_cost=tax_all,
                strategy_cost=strategy_all,
            )
        ]
        postpone_to = self._postpone_date(reservation, protected)
        options.append(
            QuestionOption(
                key="postpone",
                effect=OptionEffect.POSTPONE,
                label=f"take the money on {postpone_to.isoformat()} instead",
                tax_cost=tax_all,
                strategy_cost=sum((c.strategy for c in picked), ZERO)
                + sum((c.proceeds for c in protected), ZERO) * ordinary,
                due_date=postpone_to,
            )
        )
        covered = line.from_cash + sum((c.proceeds for c in picked), ZERO)
        reduced = _whole(covered / rate, ROUND_FLOOR)
        if ZERO < reduced < reservation.amount:
            options.append(
                QuestionOption(
                    key="reduce",
                    effect=OptionEffect.REDUCE,
                    label=f"take {reduced} {reservation.currency} by the date instead",
                    tax_cost=self._sum_tax(picked),
                    strategy_cost=sum((c.strategy for c in picked), ZERO),
                    amount=reduced,
                )
            )
        year_end = self._after_year_end(today, last, venue, tax_all)
        if year_end is not None:
            options.append(
                QuestionOption(
                    key="after_year_end",
                    effect=OptionEffect.AFTER_YEAR_END,
                    label=(
                        f"sell from {year_end.isoformat()}, in the next tax year: its tax is paid "
                        "a year later"
                    ),
                    tax_cost=tax_all,
                    strategy_cost=strategy_all,
                    not_before=year_end,
                )
            )
        context = (
            f"{line.label}: {reservation.amount} {reservation.currency} due "
            f"{reservation.due_date.isoformat()} ({reservation.reason}) needs "
            f"{line.shortfall:.2f} {self._settings.account.base_currency} more than the cash "
            f"line and unprotected sales raise; meeting the date cuts {names}"
        )
        return OwnerQuestion(
            kind=QuestionKind.PROTECTED_CUT,
            reservation_id=reservation.reservation_id,
            context=context,
            options=tuple(options),
            default="proceed",
            deadline=deadline,
            created_at=self._now(),
        )

    def _later_date_question(
        self,
        line: ReservationLine,
        picked: Sequence[_Chosen],
        asked: Sequence[OwnerQuestion],
    ) -> OwnerQuestion | None:
        """A later date offered when a sale is held to the last safe days because its layer
        expects a strong move; asked once per reservation, the default being the date as it is."""
        strong = [
            (choice, sale)
            for choice, sale in zip(picked, line.sales, strict=True)
            if sale.pace is Pace.HOLD
            and choice.candidate.expected is not None
            and choice.candidate.expected.usable
        ]
        if not strong or any(q.kind is QuestionKind.LATER_DATE for q in asked):
            return None
        reservation = line.reservation
        assert reservation.reservation_id is not None
        first = min(sale.start for _, sale in strong)
        bounds = self._calendars.session_bounds(strong[0][1].venue, first)
        deadline = self._now() if bounds is None else max(self._now(), bounds.open_utc)
        later = reservation.due_date + self._postpone
        held = sum((choice.strategy for choice, _ in strong), ZERO)
        names = ", ".join(
            f"{choice.candidate.symbol} ({sale.reason.split(':')[0]})" for choice, sale in strong
        )
        return OwnerQuestion(
            kind=QuestionKind.LATER_DATE,
            reservation_id=reservation.reservation_id,
            context=(
                f"{line.label}: {reservation.amount} {reservation.currency} due "
                f"{reservation.due_date.isoformat()} is met by selling {names} in the last safe "
                f"days, from {first.isoformat()}; a later date would let them run"
            ),
            options=(
                QuestionOption(
                    key="proceed",
                    effect=OptionEffect.PROCEED,
                    label=f"keep the date: sell them from {first.isoformat()}",
                    tax_cost=self._sum_tax([c for c, _ in strong]),
                    strategy_cost=held,
                ),
                QuestionOption(
                    key="postpone",
                    effect=OptionEffect.POSTPONE,
                    label=f"take the money on {later.isoformat()} instead",
                    tax_cost=self._sum_tax([c for c, _ in strong]),
                    strategy_cost=ZERO,
                    due_date=later,
                ),
            ),
            default="proceed",
            deadline=deadline,
            created_at=self._now(),
        )

    @staticmethod
    def _protection_text(candidate: _Candidate) -> str:
        protection = candidate.protection
        if protection is None:
            return candidate.holding.sleeve.value
        until = "" if protection.until is None else f" until {protection.until.isoformat()}"
        return f"{protection.why.value.replace('_', ' ')}{until}"

    @staticmethod
    def _sum_tax(chosen: Sequence[_Chosen]) -> Decimal | None:
        if any(c.tax is None for c in chosen):
            return None
        return sum((c.tax or ZERO for c in chosen), ZERO)

    def _postpone_date(
        self, reservation: WithdrawalReservation, protected: Sequence[_Chosen]
    ) -> date:
        """The first due date at which the protected positions are free: the end of their
        protection, settled, plus the buffer; the configured postponement when it is unknown."""
        latest: date | None = None
        for choice in protected:
            protection = choice.candidate.protection
            if protection is None or protection.until is None:
                return reservation.due_date + self._postpone
            first = self._first_session(choice.candidate.venue, protection.until)
            if first is None:
                return reservation.due_date + self._postpone
            settled = self._calendars.settlement_date(choice.candidate.venue, first)
            latest = settled if latest is None else max(latest, settled)
        assert latest is not None
        return max(latest + self._buffer, reservation.due_date + timedelta(days=1))

    def _after_year_end(
        self, today: date, last: date, venue: str, tax: Decimal | None
    ) -> date | None:
        """The first session of the next tax year, when the plan pays tax this year and that
        session still settles in time."""
        if tax is None or tax <= ZERO or last.year <= today.year:
            return None
        first = self._first_session(venue, date(today.year + 1, 1, 1))
        if first is None or first > last:
            return None
        return first

    # ── writing ──

    async def apply(self, plan: ReservationPlan) -> PlannerActions:
        """Ask the plan's questions and make today's sales intents, one per position, at a window
        its venue is tradable in; a sale that cannot be priced waits for the next cycle."""
        actions = PlannerActions()
        for question in plan.to_ask:
            asked = await self._questions.ask(question, actor=PLANNER_ACTOR)
            if asked is not None:
                actions.asked.append(asked)
        today: dict[int, tuple[PlannedSale, Decimal]] = {}
        for sale in plan.sales_today:
            first, units = today.get(sale.conid, (sale, ZERO))
            today[sale.conid] = (first, units + sale.today_units)
        for conid, (sale, units) in sorted(today.items()):
            why = self._closed(sale, plan.at)
            if why is not None:
                actions.waiting.append(f"{sale.symbol}: {why}")
                continue
            made = await self._maker.make(
                conid=conid,
                side=OrderSide.SELL,
                quantity=units,
                sleeve=sale.sleeve,
                reason=REASON,
                mode=plan.mode,
            )
            if isinstance(made, str):
                actions.waiting.append(f"{sale.symbol}: {made}")
                continue
            actions.intents.append(made)
        return actions

    def _closed(self, sale: PlannedSale, at: datetime) -> str | None:
        """Why the sale's venue is not tradable now, or ``None`` when it is."""
        status = self._calendars.tradability(sale.venue, at, conid=sale.conid)
        return None if status.ok else f"{sale.venue} is not tradable now ({status.reason})"
