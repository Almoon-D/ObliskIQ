"""H13's looks over the store, at a month end (``research/preregistrations/thesis-book.md``;
DECISIONS D173). Runs inside the store worker.

For each record (shadow, paper, live) the theses approved in its modes are the book; a thesis's
fills are the satellite's fills of its instrument (or of the option it is held as) from its
approval to its close, as the thesis book itself counts them (``decision/rebalance_thesis.py``).
Its months run from the month of the record's first entry fill; at month ``6k`` look ``k`` is
taken for the book and for each source, analyst segment and conviction level that has an entry
fill, from that group's own first entry month. Every look is recorded once, evaluable or not, and
never repeated.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ..config.schema import ObliskConfig
from ..domain.orders import OrderSide, Sleeve
from ..domain.tax import SecurityIdentity, SecurityKind
from ..domain.thesis import Thesis, ThesisState
from ..jurisdiction.tax import TaxPolicy
from ..quant.costs import CostBook
from ..quant.costs import OrderTerms as CostTerms
from ..research.prereg import ForwardBookSpec
from ..store import analyst as analyst_repo
from ..store import orders as order_repo
from ..store import repos
from ..store import theses as thesis_repo
from ..store import thesis_options as link_repo
from ..store.db import Store
from ..store.h13 import (
    BOOK,
    CONVICTION,
    RECORD_MODES,
    SEGMENT,
    SOURCE,
    H13Look,
    Record,
    capital_before,
    h13_looks,
    record_h13_look,
    record_of,
)
from ..tax.book import TaxFacts, security_key, tax_kind, universe_tax_facts
from .h13 import (
    Account,
    LookOutcome,
    LookRules,
    MonthX,
    Side,
    Trade,
    TwinInputs,
    closed_theses,
    judge,
    month_end,
    month_of,
    month_start,
    months_from,
    series,
    thesis_profits,
    twin_trades,
)
from .run import StoreMarks

ZERO = Decimal(0)
#: The states of a thesis that was approved: only those can have filled.
_APPROVED = frozenset(
    {ThesisState.APPROVED, ThesisState.ACTIVE, ThesisState.CLOSED, ThesisState.EXPIRED}
)

#: The venue a listing exchange trades on, as the calendars name it (``None``: none).
type VenueOf = Callable[[str], str | None]


def look_rules(spec: ForwardBookSpec) -> LookRules:
    return LookRules(
        theta=float(spec.theta),
        looks=spec.looks.count,
        step_alpha=float(spec.step.alpha),
        back_alpha=float(spec.back_to_shadow.alpha),
        min_closed_first=spec.step.min_closed_first,
        min_closed_between=spec.step.min_closed_between,
        min_closed_back=spec.back_to_shadow.min_closed,
    )


def look_number(months: int, spec: ForwardBookSpec) -> int | None:
    """The look ``months`` of a record falls on; ``None`` between looks or after the last."""
    every = spec.looks.every_months
    if months < every or months % every:
        return None
    look = months // every
    return look if look <= spec.looks.count else None


@dataclass(frozen=True, slots=True)
class Book:
    """A record's theses, their trades, and what values them."""

    record: Record
    theses: dict[int, Thesis]
    trades: list[Trade]
    identities: dict[str, SecurityIdentity]
    #: Each thesis's first entry fill, valued or not.
    first_buy: dict[int, date]
    #: Why a fill could not be valued: every look of the record is then not evaluable.
    problem: str | None = None


class Valuer:
    """Marks, rates and tax identities from the store, at most the mark age H13 allows."""

    def __init__(
        self, store: Store, settings: ObliskConfig, spec: ForwardBookSpec, *, as_of: date
    ) -> None:
        self._store = store
        self._settings = settings
        self.marks = StoreMarks(
            store,
            base=settings.account.base_currency,
            max_age_days=spec.adequacy.max_mark_age_days,
            as_of=as_of,
        )
        self._facts = universe_tax_facts(settings.universe)
        for share in thesis_repo.pinned_shares(store):
            self._facts.setdefault(share.conid, TaxFacts(share.conid, share.isin, None, None))
        self.multipliers: dict[str, Decimal] = {}

    def unit(self, security: str, on: date) -> Decimal | None:
        price = self.marks.price(int(security), on)
        return None if price is None else price * self.multipliers.get(security, Decimal(1))

    def identity(self, conid: int, kind_of: Thesis | None) -> SecurityIdentity | None:
        """The tax identity of a share or fund the thesis (or the universe) names."""
        declared = self._settings.universe.pinned(conid)
        asset_class = (
            kind_of.instrument.asset_class
            if kind_of is not None
            else None
            if declared is None
            else declared.asset_class
        )
        kind = None if asset_class is None else tax_kind(asset_class)
        facts = self._facts.get(conid)
        if facts is None and kind_of is not None and kind_of.instrument.isin is not None:
            facts = TaxFacts(conid, kind_of.instrument.isin, None, None)
        if kind is None or facts is None:
            return None
        try:
            return SecurityIdentity(
                key=security_key(conid),
                kind=kind,
                isin=facts.isin,
                issuer=facts.issuer,
                tracked_index=facts.tracked_index,
                listed=True,
            )
        except ValueError:
            return None


def read_book(store: Store, record: Record, valuer: Valuer, *, until: date) -> Book:
    """The record's theses and their trades to the end of ``until``."""
    modes = RECORD_MODES[record]
    shadow = record == "shadow"
    theses = {
        t.thesis_id: t
        for t in thesis_repo.theses(store, as_of=until, states=_APPROVED)
        if t.mode in modes and t.approved_at is not None and t.approved_at.date() <= until
    }
    fills = [
        f
        for f in order_repo.intent_fills(store)
        if f.sleeve is Sleeve.SATELLITE
        and f.mode in modes
        and f.fill.simulated == shadow
        and f.fill.executed_at.date() <= until
    ]
    identities: dict[str, SecurityIdentity] = {}
    trades: list[Trade] = []
    first_buy: dict[int, date] = {}
    problem: str | None = None
    for thesis_id, thesis in theses.items():
        option = link_repo.get(store, thesis_id)
        conid = option.option_conid if option is not None else thesis.instrument.conid
        currency = option.currency if option is not None else thesis.instrument.currency
        if conid is None or currency is None:
            continue
        key = security_key(conid)
        multiplier = Decimal(repr(option.multiplier)) if option is not None else Decimal(1)
        valuer.multipliers[key] = multiplier
        if option is not None:
            identities[key] = SecurityIdentity(
                key=key,
                kind=SecurityKind.OPTION,
                isin=None,
                issuer=None,
                tracked_index=None,
                listed=True,
            )
        else:
            found = valuer.identity(conid, thesis)
            if found is not None:
                identities[key] = found
        approved = thesis.approved_at
        closed = thesis.closed_at
        for item in fills:
            fill = item.fill
            if fill.conid != conid or approved is None or fill.executed_at < approved:
                continue
            if closed is not None and fill.executed_at > closed:
                continue
            day = fill.executed_at.date()
            if fill.side is OrderSide.BUY:
                first_buy[thesis_id] = min(day, first_buy.get(thesis_id, day))
            rate = valuer.marks.rate(currency, day)
            if rate is None:
                problem = problem or (
                    f"no {currency} rate within its age limit for the fill of {day.isoformat()}"
                )
                continue
            trades.append(
                Trade(
                    thesis_id=thesis_id,
                    security=key,
                    side="buy" if fill.side is OrderSide.BUY else "sell",
                    on=day,
                    quantity=fill.quantity,
                    value=fill.quantity * fill.price * multiplier * rate,
                    costs=(fill.commission or ZERO) * rate,
                )
            )
    trades.sort(key=lambda t: (t.on, t.side != "buy", t.thesis_id))
    return Book(record, theses, trades, identities, first_buy, problem)


def twin_inputs(
    store: Store, settings: ObliskConfig, valuer: Valuer, *, venue_of: VenueOf
) -> tuple[TwinInputs, SecurityIdentity | None] | str:
    """How the twin buys and sells the equity reference, and its tax identity."""
    conid = settings.portfolio_circuit_breaker.equity_reference_conid
    declared = settings.universe.pinned(conid)
    if declared is None:
        return f"the equity reference {conid} is not in the universe file"
    venue = venue_of(declared.listing_exchange)
    costs = CostBook.from_config(settings)
    currency = declared.currency
    sessions = settings.thesis.liquidity.sessions

    def close(day: date) -> Decimal | None:
        return valuer.marks.price(conid, day)

    def cost(units: Decimal, close_base: Decimal, side: Side, day: date) -> Decimal | None:
        rate = valuer.marks.rate(currency, day)
        if venue is None or rate is None or units <= ZERO:
            return None
        bars = market_value(store, conid, day, sessions)
        leg = costs.leg_cost(
            CostTerms(
                venue=venue,
                asset_class=declared.asset_class,
                currency=currency,
                quantity=units,
                price=close_base / rate,
            ),
            side=side,
            on=day,
            average_daily_value=bars,
        )
        return None if leg.total is None else leg.total * rate

    key = security_key(conid)
    return TwinInputs(key, close, cost), valuer.identity(conid, None)


def market_value(store: Store, conid: int, day: date, sessions: int) -> Decimal | None:
    """The reference's average daily traded value over ``sessions`` stored bars to ``day``."""
    bars = repos.bar_series(store, conid, as_of=day, limit=sessions)
    traded = [
        Decimal(repr(bar.close)) * Decimal(repr(bar.volume))
        for bar in bars
        if bar.volume is not None
    ]
    return sum(traded, ZERO) / len(traded) if traded else None


# ── the looks ──


def _groups(book: Book, segments: dict[int, str]) -> dict[str, set[int]]:
    """The book and each source, analyst segment and conviction level, by their theses."""
    found: dict[str, set[int]] = {BOOK: set(book.theses)}
    for thesis_id, thesis in book.theses.items():
        found.setdefault(f"{SOURCE}{thesis.source.value}", set()).add(thesis_id)
        segment = segments.get(thesis_id)
        if segment is not None:
            found.setdefault(f"{SEGMENT}{segment}", set()).add(thesis_id)
        found.setdefault(f"{CONVICTION}{thesis.conviction.value}", set()).add(thesis_id)
    return found


@dataclass(frozen=True, slots=True)
class GroupRun:
    """What one group's look needs."""

    trades: list[Trade]
    twin: list[Trade] | str
    months: list[str]


def _series(
    run: GroupRun,
    book: Book,
    *,
    twin_identity: SecurityIdentity | None,
    reference: str,
    policy: TaxPolicy,
    capital: Callable[[str], Decimal | None],
    valuer: Valuer,
) -> list[MonthX]:
    if isinstance(run.twin, str):
        return [MonthX(m, None, capital(m), f"the twin: {run.twin}") for m in run.months]
    twin_ids = {} if twin_identity is None else {reference: twin_identity}
    return series(
        run.months,
        Account(tuple(run.trades), book.identities, policy),
        Account(tuple(run.twin), twin_ids, policy),
        capital=capital,
        value=valuer.unit,
        reference=reference,
    )


def _as_record(
    record: Record, group: str, month: str, outcome: LookOutcome, at: datetime
) -> H13Look:
    months = [{"month": m.month, "x": m.x, "why": m.why} for m in outcome.xs]
    return H13Look(
        record=record,
        group=group,
        month=month,
        look=outcome.look,
        months=outcome.months,
        closed=outcome.closed,
        mean=outcome.mean,
        sd=outcome.sd,
        t_up=outcome.t_up,
        t_down=outcome.t_down,
        c_up=outcome.c_up,
        c_down=outcome.c_down,
        without_best=outcome.without_best,
        step=outcome.step,
        back=outcome.back,
        evaluable=outcome.evaluable,
        series=json.dumps(months, sort_keys=True),
        detail=outcome.detail,
        at=at,
    )


def _not_evaluable(
    record: Record, group: str, month: str, look: int, months: int, why: str, at: datetime
) -> H13Look:
    return H13Look(
        record=record,
        group=group,
        month=month,
        look=look,
        months=months,
        closed=0,
        mean=None,
        sd=None,
        t_up=None,
        t_down=None,
        c_up=None,
        c_down=None,
        without_best=None,
        step=False,
        back=False,
        evaluable=False,
        series="[]",
        detail=f"not evaluable: {why}",
        at=at,
    )


def run_h13(
    store: Store,
    settings: ObliskConfig,
    spec: ForwardBookSpec,
    *,
    policy: TaxPolicy | None,
    venue_of: VenueOf,
    month: str,
    at: datetime,
) -> list[H13Look]:
    """Take every look of H13 that falls at ``month``'s end, once; the looks recorded now."""
    end = month_end(month)
    valuer = Valuer(store, settings, spec, as_of=end)
    rules = look_rules(spec)
    segments = analyst_repo.thesis_segments(store)
    twin = twin_inputs(store, settings, valuer, venue_of=venue_of)
    reference = security_key(settings.portfolio_circuit_breaker.equity_reference_conid)
    previous = h13_looks(store)
    written: list[H13Look] = []
    for record in spec.records:
        name = record_of(record)
        book = read_book(store, name, valuer, until=end)
        if not book.first_buy:
            continue
        months = months_from(month_of(min(book.first_buy.values())), month)
        look = look_number(len(months), spec)
        if look is None:
            continue

        def capital(m: str, name: Record = name) -> Decimal | None:
            return capital_before(store, name, month_start(m))

        for group, members in _groups(book, segments).items():
            found = _look(
                name,
                group,
                members,
                book,
                month=month,
                look=look,
                rules=rules,
                spec=spec,
                twin=twin,
                reference=reference,
                policy=policy,
                capital=capital,
                valuer=valuer,
                previous=previous,
                at=at,
            )
            if found is not None and record_h13_look(store, found):
                written.append(found)
    return written


def _look(
    record: Record,
    group: str,
    members: set[int],
    book: Book,
    *,
    month: str,
    look: int,
    rules: LookRules,
    spec: ForwardBookSpec,
    twin: tuple[TwinInputs, SecurityIdentity | None] | str,
    reference: str,
    policy: TaxPolicy | None,
    capital: Callable[[str], Decimal | None],
    valuer: Valuer,
    previous: Sequence[H13Look],
    at: datetime,
) -> H13Look | None:
    """One group's look at ``month``, or ``None`` when the group has not entered yet."""
    end = month_end(month)
    is_book = group == BOOK
    entered = [day for thesis, day in book.first_buy.items() if thesis in members]
    if not entered:
        return None
    months = months_from(month_of(min(entered)), month)
    if book.problem is not None:
        return _not_evaluable(record, group, month, look, len(months), book.problem, at)
    trades = [t for t in book.trades if t.thesis_id in members]
    if policy is None:
        why = "no tax policy: the accounts cannot be valued after tax"
        return _not_evaluable(record, group, month, look, len(months), why, at)
    if isinstance(twin, str):
        return _not_evaluable(record, group, month, look, len(months), twin, at)
    inputs, twin_identity = twin

    def run_of(selected: list[Trade]) -> GroupRun:
        return GroupRun(selected, twin_trades(selected, inputs), months)

    xs = _series(
        run_of(trades),
        book,
        twin_identity=twin_identity,
        reference=reference,
        policy=policy,
        capital=capital,
        valuer=valuer,
    )
    closed = len(closed_theses(trades, end))
    can_step = is_book and record == record_of(spec.step.record)
    can_back = any(record == record_of(r) for r in spec.back_to_shadow.records)
    without: list[MonthX] | None = None
    last_step: int | None = None
    if can_step:
        profits = thesis_profits(trades, end, valuer.unit)
        if profits:
            best = max(sorted(profits), key=lambda thesis: profits[thesis])
            without = _series(
                run_of([t for t in trades if t.thesis_id != best]),
                book,
                twin_identity=twin_identity,
                reference=reference,
                policy=policy,
                capital=capital,
                valuer=valuer,
            )
        steps = [p for p in previous if p.record == record and p.group == BOOK and p.step]
        last_step = steps[-1].closed if steps else None
    outcome = judge(
        look,
        xs,
        rules=rules,
        closed=closed,
        can_step=can_step,
        can_back=can_back,
        closed_at_last_step=last_step,
        without_best=without,
    )
    return _as_record(record, group, month, outcome, at)
