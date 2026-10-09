"""Tenor filtering and volatility-scaled strike bounds.

The band
--------

::

    lower = spot * exp(-k * sigma * sqrt(tau))
    upper = spot * exp(+k * sigma * sqrt(tau))

with ``k = data.strike_bounds_sigma`` and ``sigma`` annualised. A fixed
percentage band is never produced: ±25% is far too narrow for a 90%-IV name and
far too wide for a 27%-IV one.

The unit that matters
---------------------

docs/COLLECTORS.md writes the exponent as ``k*sigma*sqrt(T/252)``, where ``T`` is in
**trading** days and 252 is ``data.trading_days_per_year`` — so ``T/252`` is a
year fraction. Expiries arrive as CALENDAR days to expiry, and converting them at
``trading_days_per_year / day_count_basis`` makes ``T/252`` identical to
``dte_calendar / day_count_basis``. :func:`diffusion_tau_years` does that
conversion explicitly rather than relying on the two cancelling, because feeding
calendar days straight into ``/252`` inflates the variance by 365/252 and the
band width by about 1.20x — a band that looks plausible and is wrong.

This is deliberately a separate function from :func:`obliskiq.quant.bsm.tau_years`,
which measures contractual time to expiry for pricing. They agree numerically;
they answer different questions, and only one of them should move if the trading
calendar is ever counted properly.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import Config
from ..logging_setup import get_logger
from ..util.timeutil import parse_date

log = get_logger("collect.strikes")

#: IBKR's option-month form is MMMYY, e.g. JAN26.
_MONTH_ABBREVIATIONS = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)

#: Years in IBKR's two-digit month form are 20xx.
_CENTURY = 2000

#: Months in a year, for rolling a month index over into the next year.
_MONTHS_PER_YEAR = 12

#: Length of the MMM part of IBKR's MMMYY month form.
_MONTH_ABBREVIATION_LENGTH = 3


@dataclass(frozen=True)
class StrikeBounds:
    """The band for one expiry, and everything needed to explain it later."""

    lower: float
    upper: float
    spot: float
    sigma: float
    tau_years: float
    k: float

    def contains(self, strike: float) -> bool:
        return self.lower <= strike <= self.upper


def diffusion_tau_years(
    dte_calendar: float, *, trading_days_per_year: float, calendar_days_per_year: float
) -> float:
    """Year fraction over which realised diffusion is measured.

    Written the long way on purpose — calendar days to trading days, then trading
    days to years — so the 252 in the brief's formula is visibly the same 252 that
    annualises the realised vol feeding it.
    """
    trading_days = dte_calendar * (trading_days_per_year / calendar_days_per_year)
    return max(trading_days / trading_days_per_year, 0.0)


def strike_bounds(*, spot: float, sigma: float, tau_years: float, k: float) -> StrikeBounds:
    """Volatility-scaled bounds around ``spot``.

    Captures roughly ``k`` standard deviations of the terminal distribution. The
    band widens with sigma and with time, which is the entire point: it is not a
    percentage of spot.
    """
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot!r}")
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma!r}")
    move = k * sigma * math.sqrt(max(tau_years, 0.0))
    return StrikeBounds(
        lower=spot * math.exp(-move),
        upper=spot * math.exp(move),
        spot=spot,
        sigma=sigma,
        tau_years=tau_years,
        k=k,
    )


class TenorWindow:
    """The expiry window a chain snapshot is allowed to cover.

    Bounded by ``tenor.entry_dte_min`` and ``tenor.entry_dte_max``. Collecting the
    whole chain instead of the 2-4 expiries inside the window is roughly a tenfold
    volume increase for data the mandate cannot trade.
    """

    def __init__(self, config: Config) -> None:
        tenor = config.section("tenor")
        self.dte_min = tenor.require_int("entry_dte_min")
        self.dte_max = tenor.require_int("entry_dte_max")

    def contains_dte(self, dte: int) -> bool:
        return self.dte_min <= dte <= self.dte_max

    def earliest(self, today: date) -> date:
        return today + timedelta(days=self.dte_min)

    def latest(self, today: date) -> date:
        return today + timedelta(days=self.dte_max)

    def months(self, months: tuple[str, ...], today: date) -> list[str]:
        """The IBKR option months whose span overlaps the window.

        A month is kept when any day in it could carry an in-window expiry. The
        exact expiry dates are resolved afterwards from ``/iserver/secdef/info``;
        this only narrows which months are worth asking about.
        """
        earliest, latest = self.earliest(today), self.latest(today)
        keep: list[str] = []
        for month in months:
            span = month_span(month)
            if span is None:
                log.warning("unparseable option month, skipping", extra={"month": month})
                continue
            first, last = span
            if last >= earliest and first <= latest:
                keep.append(month)
        return keep

    def expiries(self, expiries: list[str], today: date) -> list[str]:
        """The expiry dates inside the window, ascending and deduplicated."""
        keep: set[str] = set()
        for raw in expiries:
            parsed = parse_date(raw)
            if parsed is None:
                continue
            if self.contains_dte((parsed - today).days):
                keep.add(parsed.isoformat())
        return sorted(keep)


def month_span(month: str) -> tuple[date, date] | None:
    """First and last calendar day of an IBKR ``MMMYY`` month, or ``None``."""
    text = str(month).strip().upper()
    if len(text) < len("MMMYY"):
        return None
    abbreviation = text[:_MONTH_ABBREVIATION_LENGTH]
    year_text = text[_MONTH_ABBREVIATION_LENGTH:]
    if abbreviation not in _MONTH_ABBREVIATIONS:
        return None
    try:
        year = _CENTURY + int(year_text)
    except ValueError:
        return None
    index = _MONTH_ABBREVIATIONS.index(abbreviation) + 1
    first = date(year, index, 1)
    # The day before the first of the next month, so month lengths and leap years
    # take care of themselves.
    next_first = date(year + 1, 1, 1) if index == _MONTHS_PER_YEAR else date(year, index + 1, 1)
    return first, next_first - timedelta(days=1)


def select_strikes(ladder: list[float], bounds: StrikeBounds, *, cap: int) -> list[float]:
    """Strikes inside ``bounds``, at most ``cap`` of them, ascending.

    When the cap bites, the strikes nearest spot survive. Those carry the volume,
    the open interest and the part of the smile worth having; the far wings are
    what a truncated chain can afford to lose.
    """
    inside = sorted({s for s in ladder if s is not None and bounds.contains(s)})
    if cap > 0 and len(inside) > cap:
        inside = sorted(sorted(inside, key=lambda s: abs(s - bounds.spot))[:cap])
    return inside


def nearest_strike(ladder: list[float], spot: float) -> float | None:
    """The listed strike closest to spot, used to resolve a month's expiry dates.

    One ``/iserver/secdef/info`` call at this strike returns every expiry in the
    month, which is what makes per-expiry bounds affordable: the alternative is a
    call per strike before knowing which strikes are wanted.
    """
    usable = [s for s in ladder if s is not None]
    if not usable:
        return None
    return min(usable, key=lambda s: abs(s - spot))
