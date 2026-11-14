"""The book's breakers: a falling managed book freezes its own new buys, then stands itself down
to shadow (MASTER_PLAN §5.11; DECISIONS D88 "Measurement", D147, D163).

**What is measured.** At each decision cycle the managed book's value is read and stored with
one unit of the equity reference read at the same moment (``store/breakers.py``); the reading of
the day's last cycle (``portfolio_circuit_breaker.close_cycle``) is the day's **close**. A peak is
taken from closes alone: a value read inside the day is measured against them and never raises
them. The owner's flows are neutralised as the time-weighted return neutralises them
(``capital/twr.py``), so a withdrawal is not a fall and a deposit hides none.

**The fall beyond the reference.** From each close in scope the book has fallen ``1 - now /
then`` (never below zero) and the equity reference has fallen its own fraction over the same
days; the fall **beyond** it is the difference. A market's fall the owner accepts (D147) is
excused, a fall of the book's own is not. The reference only ever excuses: where it has no
reading at either end, nothing is excused, so a breaker depends on nothing but the book's own
readings to trip.

* **Rolling month.** The closes of the last ``window_days``. Beyond ``monthly_drawdown_pct``:
  new buys are frozen (``action``) while it holds. Sells stay allowed.
* **Hard.** Every close since the owner's last reset. Beyond ``hard_drawdown_pct``: the mode is
  forced to shadow and the trip is latched in the store until ``obliskiq breakers reset``; a
  latched book buys nothing in any mode.

**Honest states.** Fewer than ``min_observations`` closes in scope: ``n/a``, never "fine" and
never a freeze (a book that has just started has no fall to measure). The managed value not
readable now, the flows not valuable, or a last close older than ``max_close_age_days``:
``unreadable``, and new buys are refused. No breaker creates a sell.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..capital.managed import CapitalReader, CapitalView, is_shadow_family
from ..capital.twr import ExternalFlow, FlowTiming, Reading, time_weighted_return
from ..config.schema import ObliskConfig
from ..config.schema_engines import PortfolioCircuitBreakerConfig
from ..domain.capital import Deposit, Withdrawal
from ..domain.orders import ExecutionMode
from ..errors import ObliskError
from ..execution.account import CashRead
from ..store import breakers as repo
from ..store import capital as capital_repo
from ..store import fx as fx_repo
from ..store.breakers import BOOKS, RESET, TRIPPED, Book, BreakerEvent, ManagedReading
from ..store.db import Store
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

ZERO = Decimal(0)
ONE = Decimal(1)

#: The breakers' names, as the digest, the status and the run log show them.
ROLLING = "rolling-month"
HARD = "hard"
#: A breaker's states.
OK = "ok"
TRIPPED_STATE = "tripped"
NOT_MEASURED = "n/a"
UNREADABLE = "unreadable"
#: Who latches a trip.
BREAKER_ACTOR = "breaker"


@dataclass(frozen=True, slots=True)
class Fall:
    """The book's fall since the close of ``since`` and how much of it the equity reference's
    own fall excuses (``None``: the reference has no reading at one end)."""

    since: date
    fall: Decimal
    excused: Decimal | None

    @property
    def beyond(self) -> Decimal:
        return self.fall - (self.excused or ZERO)


@dataclass(frozen=True, slots=True)
class BreakerState:
    """One breaker now: its state, the worst fall in its scope and why."""

    name: str
    status: str
    threshold: Decimal
    closes: int
    worst: Fall | None
    reason: str

    def line(self) -> str:
        status = self.status.upper() if self.tripped else self.status
        return f"{self.name} breaker: {status} ({self.reason})"

    @property
    def tripped(self) -> bool:
        return self.status == TRIPPED_STATE


@dataclass(frozen=True, slots=True)
class BreakerReport:
    """Both breakers of one book. ``live``: measured on a value read now; otherwise on the last
    stored reading (``measured_at``)."""

    book: Book
    at: datetime
    rolling: BreakerState
    hard: BreakerState
    latched: BreakerEvent | None
    action: str
    live: bool = True
    measured_at: datetime | None = None
    #: The trips latched on the other books: the mode stood down from one of them.
    elsewhere: tuple[BreakerEvent, ...] = ()

    @property
    def buys_refused(self) -> str | None:
        """Why the book may not buy now; ``None`` when it may. An unmeasured breaker (``n/a``)
        refuses nothing and is reported as untested."""
        for state in (self.hard, self.rolling):
            if state.tripped:
                what = "" if state.name == HARD else f"; action {self.action}"
                return f"the {state.name} breaker is tripped ({state.reason}){what}"
            if state.status == UNREADABLE:
                return f"the {state.name} breaker cannot be read ({state.reason})"
        return None

    @property
    def newly_tripped(self) -> bool:
        """The hard breaker is tripped and no latch holds it yet."""
        return self.hard.tripped and self.latched is None

    def lines(self) -> list[str]:
        measured = (
            []
            if self.live or self.measured_at is None
            else [f"measured on the reading of {self.measured_at.isoformat()}"]
        )
        other = [
            f"{HARD} breaker of the {event.book} book: TRIPPED at {event.at.isoformat()} "
            f"({event.detail}); it holds until `obliskiq breakers reset`"
            for event in self.elsewhere
        ]
        return [self.rolling.line(), self.hard.line(), *other, *measured]


def book_of(mode: ExecutionMode) -> Book:
    """The book a mode runs: shadow's simulated one, or the broker's."""
    return "shadow" if is_shadow_family(mode) else "broker"


def _percent(value: Decimal) -> str:
    return f"{value:.1%}"


def _reference_fall(then: ManagedReading, now: ManagedReading) -> Decimal | None:
    """The equity reference's fall between two readings, never below zero; ``None`` when either
    holds no reading of the same instrument."""
    if (
        then.reference_unit is None
        or now.reference_unit is None
        or then.reference_conid != now.reference_conid
    ):
        return None
    return max(ZERO, ONE - now.reference_unit / then.reference_unit)


def _state(
    name: str,
    threshold: Decimal,
    scope: Sequence[tuple[ManagedReading, Decimal]],
    current: ManagedReading,
    index: Decimal,
    *,
    minimum: int,
    span: str,
) -> BreakerState:
    """A breaker over the closes of ``scope`` (each with its flow-neutral index)."""
    if len(scope) < minimum:
        return BreakerState(
            name,
            NOT_MEASURED,
            threshold,
            len(scope),
            None,
            f"{len(scope)} daily close(s) {span}, {minimum} needed to measure a fall",
        )
    falls = [
        Fall(close.as_of, max(ZERO, ONE - index / then), _reference_fall(close, current))
        for close, then in scope
    ]
    worst = max(falls, key=lambda fall: (fall.beyond, fall.since))
    excused = (
        "the equity reference has no reading over those days, so none of it is excused"
        if worst.excused is None
        else f"the equity reference fell {_percent(worst.excused)}"
    )
    reason = (
        f"the book fell {_percent(worst.fall)} since {worst.since.isoformat()} and {excused}: "
        f"{_percent(worst.beyond)} beyond it, limit {_percent(threshold)}"
    )
    status = TRIPPED_STATE if worst.beyond > threshold else OK
    return BreakerState(name, status, threshold, len(scope), worst, reason)


def judge(
    config: PortfolioCircuitBreakerConfig,
    *,
    book: Book,
    at: datetime,
    closes: Sequence[ManagedReading],
    current: ManagedReading | str,
    flows: Sequence[ExternalFlow] | str,
    timing: FlowTiming,
    latched: BreakerEvent | None = None,
    reset: BreakerEvent | None = None,
    live: bool = True,
) -> BreakerReport:
    """Both breakers of ``book`` at ``at``: ``closes`` are its daily closes, oldest first,
    ``current`` the value measured against them (or why there is none) and ``flows`` the owner's
    flows of the book in the base currency (or why they cannot be valued)."""
    today = at.date()
    closes = [close for close in closes if close.is_close and close.as_of <= today]

    def both(status: str, reason: str) -> BreakerReport:
        return _both(config, book, at, status, reason, len(closes), latched, live, current)

    if isinstance(current, str):
        return both(UNREADABLE, current)
    if not closes:
        return both(NOT_MEASURED, f"no daily close is recorded, {config.min_observations} needed")
    age = (today - closes[-1].as_of).days
    if age > config.max_close_age_days:
        return both(
            UNREADABLE,
            f"the last daily close is of {closes[-1].as_of.isoformat()}, {age} days old; more "
            f"than {config.max_close_age_days} is stale",
        )
    if isinstance(flows, str):
        return both(UNREADABLE, f"the owner's flows cannot be valued: {flows}")
    chain = time_weighted_return(
        [
            *(Reading(close.as_of, close.managed) for close in closes),
            Reading(today, current.managed),
        ],
        flows,
        timing=timing,
    )
    if chain.value is None:
        return both(UNREADABLE, f"the book's return cannot be chained: {chain.reason}")
    indices = [ONE]
    for period in chain.periods:
        indices.append(indices[-1] * (ONE + period.ret))
    if min(indices) <= ZERO:
        return both(UNREADABLE, "a period of the book's return leaves no positive value")
    indexed = list(zip(closes, indices, strict=False))
    index = indices[-1]
    since = today - timedelta(days=config.window_days)
    rolling = _state(
        ROLLING,
        config.monthly_drawdown_pct,
        [(close, then) for close, then in indexed if close.as_of >= since],
        current,
        index,
        minimum=config.min_observations,
        span=f"in the last {config.window_days} days",
    )
    hard = _state(
        HARD,
        config.hard_drawdown_pct,
        [(close, then) for close, then in indexed if reset is None or close.recorded_at > reset.at],
        current,
        index,
        minimum=config.min_observations,
        span="since the last reset" if reset is not None else "so far",
    )
    return _report(config, book, at, rolling, hard, latched, live, current)


def _both(
    config: PortfolioCircuitBreakerConfig,
    book: Book,
    at: datetime,
    status: str,
    reason: str,
    closes: int,
    latched: BreakerEvent | None,
    live: bool,
    current: ManagedReading | str,
) -> BreakerReport:
    """Both breakers in one state, for one reason."""
    return _report(
        config,
        book,
        at,
        BreakerState(ROLLING, status, config.monthly_drawdown_pct, closes, None, reason),
        BreakerState(HARD, status, config.hard_drawdown_pct, closes, None, reason),
        latched,
        live,
        current,
    )


def _report(
    config: PortfolioCircuitBreakerConfig,
    book: Book,
    at: datetime,
    rolling: BreakerState,
    hard: BreakerState,
    latched: BreakerEvent | None,
    live: bool,
    current: ManagedReading | str,
) -> BreakerReport:
    """The report; a latched trip is the hard breaker's state whatever is measured now."""
    if latched is not None:
        hard = BreakerState(
            HARD,
            TRIPPED_STATE,
            hard.threshold,
            hard.closes,
            hard.worst,
            f"tripped at {latched.at.isoformat()}: {latched.detail}; it holds until "
            "`obliskiq breakers reset`",
        )
    return BreakerReport(
        book=book,
        at=at,
        rolling=rolling,
        hard=hard,
        latched=latched,
        action=config.action,
        live=live,
        measured_at=None if isinstance(current, str) else current.recorded_at,
    )


def book_flows(store: Store, settings: ObliskConfig, *, as_of: date) -> list[ExternalFlow] | str:
    """The owner's deposits and withdrawals that move the managed book, in the base currency at
    the rate of their day; why, when one has no rate. In mode ``cap`` they are the recorded
    flows that count toward the book (``capital/managed.py``); in mode ``whole_account`` the
    ones that reached the account."""
    base = settings.account.base_currency
    cap = settings.capital.mode == "cap"
    found: list[ExternalFlow] = []
    for flow in capital_repo.cash_flows(store, as_of=as_of):
        if not isinstance(flow, Deposit | Withdrawal) or not flow.live:
            continue
        if not (flow.in_book if cap else flow.matched_at is not None):
            continue
        rate = ONE
        if flow.currency != base:
            recorded = fx_repo.rate_on(store, flow.currency, flow.on)
            if recorded is None:
                return f"no {flow.currency} rate was recorded on {flow.on.isoformat()}"
            rate = recorded.rate_to_base
        found.append(ExternalFlow(flow.on, flow.signed * rate))
    return found


def flow_timing(settings: ObliskConfig) -> FlowTiming:
    """Where a flow dated on a reading's day belongs: a capped book holds it from its day; the
    whole account as ``capital.twr.flow_timing`` says."""
    return "in_reading" if settings.capital.mode == "cap" else settings.capital.twr.flow_timing


def stored_report(
    store: Store,
    settings: ObliskConfig,
    book: Book,
    *,
    at: datetime,
    live: ManagedReading | str | None = None,
) -> BreakerReport:
    """The book's breakers from the store: against ``live`` (a value read now, or why there is
    none), else against its last stored reading."""
    today = at.date()
    every = repo.readings(store, book, as_of=today)
    latched = repo.tripped(store, book)
    elsewhere = tuple(event for event in repo.latches(store) if event.book != book)
    if live is None and not every:
        # Nothing was ever read: below the minimum observations, not a failed reading.
        nothing = _both(
            settings.portfolio_circuit_breaker,
            book,
            at,
            NOT_MEASURED,
            "no managed NAV reading is recorded",
            0,
            latched,
            False,
            "no managed NAV reading is recorded",
        )
        return replace(nothing, elsewhere=elsewhere)
    report = judge(
        settings.portfolio_circuit_breaker,
        book=book,
        at=at,
        closes=[reading for reading in every if reading.is_close],
        current=live if live is not None else every[-1],
        flows=book_flows(store, settings, as_of=today),
        timing=flow_timing(settings),
        latched=latched,
        reset=repo.latest_reset(store, book),
        live=live is not None,
    )
    return replace(report, elsewhere=elsewhere)


def release(store: Store, *, at: datetime, actor: str, reason: str) -> list[Book]:
    """The owner's reset: every latched book is released, and its hard breaker measures from
    the closes recorded after ``at``."""
    released: list[Book] = []
    for book in BOOKS:
        if repo.tripped(store, book) is None:
            continue
        repo.record_event(store, BreakerEvent(at, book, RESET, actor, reason))
        released.append(book)
    return released


class Breakers:
    """Reads, records and judges the managed book's breakers over the one capital reader."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        capital: CapitalReader,
        worker: StoreWorker,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._capital = capital
        self._worker = worker
        self._now = now
        config = settings.portfolio_circuit_breaker
        self._reference = config.equity_reference_conid
        self._close_cycle = config.close_cycle

    def closes(self, cycle: str) -> bool:
        """Whether ``cycle`` is the one whose reading is the day's close."""
        return cycle == self._close_cycle

    async def _reading(
        self, mode: ExecutionMode, view: CapitalView | str, cash: CashRead, *, is_close: bool
    ) -> ManagedReading | str:
        """The book's value now with the equity reference beside it; why, when the managed
        capital cannot be read."""
        at = self._now()
        if isinstance(view, str):
            return f"the managed NAV cannot be read: {view}"
        if not view.managed.is_finite() or view.managed <= ZERO:
            return f"a managed NAV of {view.managed} is not a book's value"
        # VERIFY-LIVE(the equity reference's quote outside its venue's hours): without a quote
        # the reading holds no reference and a fall measured from or to it is not excused.
        unit = (await self._capital.valuer.unit(self._reference, cash)).unit
        valued = unit is not None and unit > ZERO
        return ManagedReading(
            book=book_of(mode),
            as_of=at.date(),
            managed=view.managed,
            reference_conid=self._reference if valued else None,
            reference_unit=unit if valued else None,
            is_close=is_close,
            recorded_at=at,
        )

    async def _judge(self, mode: ExecutionMode, live: ManagedReading | str) -> BreakerReport:
        at = self._now()
        try:
            return await self._worker.run(
                stored_report, self._settings, book_of(mode), at=at, live=live
            )
        except Exception as exc:  # the store unreadable: nothing is measured, so nothing is bought
            reason = f"the store cannot be read ({type(exc).__name__})"
            return judge(
                self._settings.portfolio_circuit_breaker,
                book=book_of(mode),
                at=at,
                closes=(),
                current=reason,
                flows=(),
                timing=flow_timing(self._settings),
            )

    async def read(
        self, mode: ExecutionMode, view: CapitalView | str, cash: CashRead
    ) -> BreakerReport:
        """The breakers on the capital just read; writes nothing."""
        return await self._judge(mode, await self._reading(mode, view, cash, is_close=False))

    async def observe(
        self, mode: ExecutionMode, *, close: bool, dry_run: bool = False
    ) -> BreakerReport:
        """A decision cycle's reading: the managed capital is read, stored (as the day's close
        when ``close``) and judged. ``dry_run`` stores nothing."""
        try:
            cash = await self._capital.cash()
        except ObliskError as exc:
            return await self._judge(mode, f"the managed NAV cannot be read: {exc}")
        view = await self._capital.read(mode, cash=cash)
        reading = await self._reading(mode, view, cash, is_close=close)
        if not dry_run and not isinstance(reading, str):
            await self._worker.run(repo.record_reading, reading)
        return await self._judge(mode, reading)

    async def latch(self, report: BreakerReport) -> BreakerEvent:
        """Latch the hard breaker's trip: it holds, across restarts, until the owner's reset."""
        event = BreakerEvent(
            at=self._now(),
            book=report.book,
            event=TRIPPED,
            actor=BREAKER_ACTOR,
            detail=report.hard.reason,
        )
        await self._worker.run(repo.record_event, event)
        return event
