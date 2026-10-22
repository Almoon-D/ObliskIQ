"""The tenor window and the volatility-scaled strike band of the option-chain collector.

Ported from lite's ``collect/strikes.py``. The band is

    lower = spot * exp(-k * sigma * sqrt(tau)),  upper = spot * exp(+k * sigma * sqrt(tau))

with ``k = collectors.chain.strike_bounds_sigma`` and sigma annualised: roughly k standard
deviations of the terminal distribution, never a fixed percentage of spot (25 % is far too narrow
for a 90 %-vol name and far too wide for a 27 %-vol one). tau is the year fraction of
*diffusion*: calendar days to expiry converted to trading days, then to years on the same basis
that annualised sigma (:func:`diffusion_tau_years`). Feeding calendar days straight into ``/252``
inflates the variance by 365/252 and the band by about 1.2 times: plausible-looking and wrong.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ...config.schema_instruments import TenorConfig

#: IBKR's option-month form is MMMYY (``JAN27``), its years 20YY.
MONTH_ABBREVIATIONS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)  # fmt: skip
_MONTH_FORM_LENGTH = len("MMMYY")
_ABBREVIATION_LENGTH = len("MMM")
_CENTURY = 2000


@dataclass(frozen=True, slots=True)
class StrikeBounds:
    """One expiry's band and everything needed to explain it later."""

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
    """The year fraction over which realised diffusion is measured, written the long way:
    calendar days to trading days, then trading days to years on sigma's own basis."""
    trading_days = dte_calendar * (trading_days_per_year / calendar_days_per_year)
    return max(trading_days / trading_days_per_year, 0.0)


def strike_bounds(*, spot: float, sigma: float, tau_years: float, k: float) -> StrikeBounds:
    """The band around ``spot``; it widens with sigma and with time."""
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


def month_span(month: str) -> tuple[date, date] | None:
    """The first and last day of an IBKR ``MMMYY`` month; ``None`` when it is not one."""
    text = month.strip().upper()
    if len(text) != _MONTH_FORM_LENGTH:
        return None
    abbreviation, year_text = text[:_ABBREVIATION_LENGTH], text[_ABBREVIATION_LENGTH:]
    if abbreviation not in MONTH_ABBREVIATIONS or not year_text.isdigit():
        return None
    year, index = _CENTURY + int(year_text), MONTH_ABBREVIATIONS.index(abbreviation) + 1
    following = (
        date(year + 1, 1, 1) if index == len(MONTH_ABBREVIATIONS) else date(year, index + 1, 1)
    )
    return date(year, index, 1), following - timedelta(days=1)


class TenorWindow:
    """The expiries a chain snapshot covers: ``tenor.entry_dte_min`` to ``entry_dte_max`` days
    out. The whole chain is roughly ten times the volume, for data the mandate cannot trade."""

    def __init__(self, tenor: TenorConfig) -> None:
        self.dte_min = tenor.entry_dte_min
        self.dte_max = tenor.entry_dte_max

    def contains(self, expiry: date, session: date) -> bool:
        return self.dte_min <= (expiry - session).days <= self.dte_max

    def months(self, months: Iterable[str], session: date) -> list[str]:
        """The option months any day of which could hold an in-window expiry, in the order
        given; a month IBKR spells some other way is skipped."""
        earliest = session + timedelta(days=self.dte_min)
        latest = session + timedelta(days=self.dte_max)
        keep: list[str] = []
        for month in months:
            span = month_span(month)
            if span is not None and span[1] >= earliest and span[0] <= latest:
                keep.append(month)
        return keep

    def expiries(self, expiries: Iterable[date], session: date) -> list[date]:
        """The in-window expiries, ascending and distinct."""
        return sorted({expiry for expiry in expiries if self.contains(expiry, session)})


def select_strikes(ladder: Iterable[float], bounds: StrikeBounds, *, cap: int) -> list[float]:
    """The strikes inside ``bounds``, at most ``cap`` of them, ascending. When the cap bites the
    strikes nearest spot survive: they carry the volume and the part of the smile worth having."""
    inside = sorted({strike for strike in ladder if bounds.contains(strike)})
    if len(inside) > cap:
        inside = sorted(sorted(inside, key=lambda strike: abs(strike - bounds.spot))[:cap])
    return inside


def nearest_strike(ladder: Sequence[float], spot: float) -> float | None:
    """The listed strike closest to spot: one contract-info call there names every expiry of
    the month, which is what makes per-expiry bands affordable."""
    return min(ladder, key=lambda strike: abs(strike - spot)) if ladder else None
