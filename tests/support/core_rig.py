"""Helpers for the core engine's tests: month-end histories, books, choices and tax answers.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from obliskiq.domain.enums import AssetClass, InstrumentRole
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Bar
from obliskiq.engines.core.engine import BookState, RoleChoice, Sizing
from obliskiq.engines.core.history import HistoryView, MonthEnds, month_end
from obliskiq.errors import TaxError
from obliskiq.store import Store, repos

D = Decimal
ON = date(2026, 10, 26)
MONTH = date(2026, 9, 30)
STAMP = datetime(2026, 10, 1, tzinfo=UTC)


def month_ends(last: date, count: int) -> list[date]:
    """``count`` month ends through ``last``, oldest first."""
    out: list[date] = []
    year, month = last.year, last.month
    for _ in range(count):
        out.append(month_end(year, month))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out[::-1]


def path(start: float, monthly: float, count: int, last: date = MONTH) -> MonthEnds:
    """A key's month-end closes growing by ``monthly`` a month (negative falls)."""
    days = month_ends(last, count)
    return MonthEnds(
        "x", tuple((day, start * (1 + monthly) ** i) for i, day in enumerate(days)), None
    )


def history(paths: Mapping[str, MonthEnds], month: date = MONTH) -> HistoryView:
    return HistoryView({key: MonthEnds(key, p.closes, p.note) for key, p in paths.items()}, month)


def choice(
    role: str,
    primary: int | None,
    *,
    partner: int | None = None,
    held: Sequence[int] = (),
    closed: Sequence[int] = (),
) -> RoleChoice:
    return RoleChoice(
        role=role,
        primary=primary,
        partner=partner,
        held=tuple(held),
        closed=frozenset(closed),
        reason="chosen in a test",
    )


def book(
    units: Mapping[int, str] | None = None,
    prices: Mapping[int, str] | None = None,
    *,
    budget: str = "10000",
    cash: str = "10000",
) -> BookState:
    return BookState(
        units={conid: D(q) for conid, q in (units or {}).items()},
        prices={conid: D(p) for conid, p in (prices or {}).items()},
        budget=D(budget),
        cash=D(cash),
        on=ON,
    )


def sizing(
    *,
    minimum: str = "50",
    unpriced: Sequence[int] = (),
    sale_cost: str = "0",
    prices: Mapping[int, str] | None = None,
) -> Sizing:
    """Legs cost nothing beyond their notional (``sale_cost`` a sale), and ``unpriced`` conids
    cannot be costed."""
    known = {conid: D(p) for conid, p in (prices or {}).items()}

    def buy(conid: int, quantity: Decimal) -> Decimal | None:
        if conid in unpriced:
            return None
        return quantity * known.get(conid, D(100))

    def sell(conid: int, quantity: Decimal) -> Decimal | None:
        return None if conid in unpriced else D(sale_cost)

    return Sizing(no_trade_band=D("0.005"), minimum=D(minimum), buy_cost=buy, sell_cost=sell)


@dataclass
class Lots:
    """Stand-in tax answers: a tax per unit sold by conid, a basis by conid, the conids a buy
    would defer a loss on, the ones whose lots are incomplete, and a trial that fails."""

    tax_per_unit: dict[int, Decimal] = field(default_factory=dict)
    bases: dict[int, Decimal] = field(default_factory=dict)
    defers: set[int] = field(default_factory=set)
    incomplete: set[int] = field(default_factory=set)
    broken: bool = False

    def complete(self, conid: int, units: Decimal) -> bool:
        return conid not in self.incomplete

    def basis(self, conid: int) -> Decimal:
        return self.bases.get(conid, D(0))

    def trial_tax(self, sales: Sequence[tuple[int, Decimal, Decimal]], *, on: date) -> Decimal:
        if self.broken:
            raise TaxError("a lot is missing")
        return sum((self.tax_per_unit.get(c, D(0)) * q for c, q, _ in sales), D(0))

    def purchase_defers_loss(self, conid: int, on: date) -> bool:
        return conid in self.defers


# ── the store ──


def seed_vehicle(store: Store, conid: int, symbol: str, currency: str = "EUR") -> None:
    repos.upsert_instrument(
        store,
        Instrument(
            conid=conid,
            symbol=symbol,
            listing_exchange="IBIS2",
            currency=currency,
            asset_class=AssetClass.ETF,
            domicile=None,
            ucits=None,
            kid_status=None,
            share_class=None,
            lot_size=None,
            option_exchange=None,
            history_exchange=None,
            roles=frozenset({InstrumentRole.DATA}),
            superseded_by=None,
            created_at=STAMP,
            superseded_at=None,
        ),
    )


def seed_closes(store: Store, conid: int, closes: Mapping[date, float]) -> None:
    repos.upsert_bars(
        store,
        [
            Bar(
                conid=conid,
                session_date=day,
                open=None,
                high=None,
                low=None,
                close=value,
                volume=None,
                source="t",
            )
            for day, value in closes.items()
        ],
    )


def eurusd(day: date, value: float) -> MacroObservation:
    return MacroObservation(
        series_id="EURUSD",
        source="ecb",
        reference_date=day,
        release_date=day,
        value=value,
        frequency="daily",
        units="USD per EUR",
        collected_at=STAMP,
    )


def weekdays(start: date, end: date) -> list[date]:
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days
