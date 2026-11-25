"""The hunt in numbers, for the digest and ``obliskiq status`` (MASTER_PLAN §1, §5.5; DECISIONS
D185, D193, D195): the hunting share, the capital the thesis book holds and the capital that
waits in the reserve, and beside the owner's bar the book's growth.

**The bar** is 50 % a year, read as the compound growth of the last three years (D193). The
growth shown beside it is the managed book's own, from its daily closes (``managed_nav``, the
breakers' readings), the owner's flows neutralised as the time-weighted return neutralises them:
compounded a year over the last three years, or over the record so far while it is shorter, with
its length said; and the current year's, so far. **After tax**: each year's tax on the realised
gains (the tax lots' ledger, settled or provisional) is taken from the book at that year's last
close. The shadow book keeps no tax lots (its fills are simulated), so its growth is before tax
and says so. It is a bar, not a forecast: nothing in the machine raises risk because a year
fell short (D193).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from ..capital.allocation import THESIS_BOOK, recorded_shares
from ..capital.budgets import thesis_share
from ..capital.twr import Reading, time_weighted_return
from ..config.schema import ObliskConfig
from ..decision.breakers import book_flows, book_of, flow_timing
from ..domain.orders import ExecutionMode
from ..domain.thesis import SLOT_STATES
from ..errors import TaxError
from ..jurisdiction.tax import TaxPolicy
from ..store import breakers as breaker_repo
from ..store import lots as lots_repo
from ..store import theses as thesis_repo
from ..store.db import Store
from ..tax.ledger import TaxLedger

ZERO = Decimal(0)
ONE = Decimal(1)
#: The owner's bar: compound growth a year, read over the last ``BAR_YEARS`` (DECISIONS D153,
#: D185, D193). His goal, recorded as a ruling; a change of it is a decision, not a setting.
BAR_GROWTH: Final = Decimal("0.50")
BAR_YEARS: Final = 3
#: Days in a year, for compounding a span of days.
YEAR_DAYS: Final = Decimal("365.25")
#: The words every figure beside the bar carries.
NOT_A_FORECAST: Final = "bar, not forecast"

_LIVE = frozenset({ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE})


@dataclass(frozen=True, slots=True)
class HuntReading:
    """The hunt on one day, in the base currency: the hunting share, what the thesis book holds
    or has approved (at the sizes it was approved at), what waits in the reserve (the rest of
    the managed book), and the growth beside the bar (``None`` with ``note`` saying why)."""

    mode: ExecutionMode
    share: Decimal
    in_theses: Decimal
    in_reserve: Decimal | None
    growth: Decimal | None
    span_days: int
    year: Decimal | None
    after_tax: bool
    note: str

    def lines(self) -> list[str]:
        held = f"{self.in_theses:.2f} in theses"
        waiting = (
            "the reserve not measured yet"
            if self.in_reserve is None
            else f"{self.in_reserve:.2f} waiting in the reserve"
        )
        lines = [f"hunting share {self.share:.0%}: {held}, {waiting}"]
        taxed = "after tax" if self.after_tax else "before tax"
        if self.growth is None:
            lines.append(f"growth beside the {BAR_GROWTH:.0%} bar: not measured ({self.note})")
            return lines
        years = Decimal(self.span_days) / YEAR_DAYS
        span = (
            f"the last {BAR_YEARS} years"
            if years >= BAR_YEARS
            else f"the record so far, {self.span_days} days"
        )
        this_year = "n/a" if self.year is None else f"{self.year:+.1%}"
        lines.append(
            f"growth {self.growth:+.1%} a year over {span} ({taxed}), this year {this_year}; "
            f"the bar {BAR_GROWTH:.0%} a year over {BAR_YEARS} years: {NOT_A_FORECAST}"
            + (f" ({self.note})" if self.note else "")
        )
        return lines


def hunt_reading(
    store: Store,
    settings: ObliskConfig,
    mode: ExecutionMode,
    *,
    as_of: date,
    policy: TaxPolicy | None,
) -> HuntReading:
    """The hunt as of ``as_of`` in ``mode``, from the store alone (no broker session).
    ``policy`` prices the tax lots' years; without it the growth is before tax."""
    if mode in _LIVE:
        share = recorded_shares(store).get(THESIS_BOOK, ZERO)
    else:
        share = thesis_share(settings, mode)
    in_theses = sum(
        (
            thesis.size_base or ZERO
            for thesis in thesis_repo.theses(store, as_of=as_of, states=SLOT_STATES)
            if thesis.mode is mode
        ),
        ZERO,
    )
    book = book_of(mode)
    closes = [
        reading
        for reading in breaker_repo.readings(store, book, as_of=as_of, closes_only=True)
        if reading.as_of <= as_of
    ]
    in_reserve = None if not closes else max(ZERO, closes[-1].managed - in_theses)

    def measured(
        growth: Decimal | None, span: int, year: Decimal | None, taxed: bool, note: str
    ) -> HuntReading:
        return HuntReading(mode, share, in_theses, in_reserve, growth, span, year, taxed, note)

    start = as_of - timedelta(days=int(YEAR_DAYS * BAR_YEARS))
    window = [close for close in closes if close.as_of >= start]
    if len(window) < 2:
        return measured(None, 0, None, False, "fewer than two daily closes of the managed book")
    flows = book_flows(store, settings, as_of=as_of)
    if isinstance(flows, str):
        return measured(None, 0, None, False, f"the owner's flows cannot be valued: {flows}")
    timing = flow_timing(settings)
    chain = time_weighted_return(
        [Reading(close.as_of, close.managed) for close in window], flows, timing=timing
    )
    if chain.value is None:
        return measured(None, 0, None, False, chain.reason or "the return cannot be chained")
    index: dict[date, Decimal] = {window[0].as_of: ONE}
    level = ONE
    for period in chain.periods:
        level *= ONE + period.ret
        index[period.end] = level
    taxed, note = _after_tax(store, book, policy, window, index)
    span = (window[-1].as_of - window[0].as_of).days
    last = index[window[-1].as_of]
    growth = last ** (YEAR_DAYS / Decimal(span)) - ONE if span > 0 and last > ZERO else None
    year_start = max((day for day in index if day.year < as_of.year), default=window[0].as_of)
    year = last / index[year_start] - ONE
    return measured(growth, span, year, taxed, note)


def _after_tax(
    store: Store,
    book: str,
    policy: TaxPolicy | None,
    window: list[breaker_repo.ManagedReading],
    index: dict[date, Decimal],
) -> tuple[bool, str]:
    """Take each year's tax from ``index`` at its last close in the window, in place; whether it
    could be, and what the reader should know."""
    if book == "shadow":
        return False, "the shadow book keeps no tax lots"
    if policy is None:
        return False, "no tax policy to price the lots"
    ledger = TaxLedger(policy)
    try:
        ledger.restore(lots_repo.load_tax_state(store).ledger)
    except TaxError as exc:
        return False, f"the tax lots cannot be read: {exc}"
    settled = {summary.tax_year: summary for summary in ledger.settled()}
    values = {close.as_of: close.managed for close in window}
    days = sorted(index)
    for year in sorted({day.year for day in days}):
        last = max(day for day in days if day.year == year)
        try:
            summary = settled.get(year) or ledger.provisional(year)
        except TaxError as exc:
            return False, f"the tax of {year} cannot be computed: {exc}"
        if summary.tax_due <= ZERO or values[last] <= ZERO:
            continue
        cut = ONE - summary.tax_due / values[last]
        for day in days:
            if day >= last:
                index[day] *= cut
    return True, ""
