"""Realised volatility, and the sigma that sizes the chain collector's strike band.

Ported from lite's ``collect/vol.py``. **Realised volatility** is the standard deviation of daily
log returns over a tenor of sessions, annualised on ``collectors.vol.trading_days_per_year``:
annualised because every other volatility here is (a quoted implied vol, the default sigma), and
a ladder whose rungs are quoted in different units silently rescales the band by whichever
fired. n returns need n + 1 closes; a sample without dispersion has no volatility.

**The sigma ladder** (``collectors.chain.sigma_ladder``) walks its rungs in order: realised vol
over the primary tenor, over the fallback tenor, the underlying's quoted implied vol, the
configured default. Every rung after the first is a fallback and marks the target degraded, and
a realised rung measured on bars older than ``bars_freshness_days`` is stale (enough closes say
nothing of when they were). Each row records the sigma and the rung that produced it, labelled
by the tenor that actually ran. The default is a fixed *sigma*, still scaled by each expiry's
own time, never a fixed percentage band.

The range-based estimators (Parkinson, Garman-Klass) in the same lite module feed the signal
indicators, and are ported with them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from ...config.schema_collectors import CollectorsConfig, SigmaRung
from ...quant.stats import annualise, log_returns, stdev

VENDOR_IV = "vendor_iv"
DEFAULT = "default"
#: A realised rung labels itself ``realised_<tenor>d`` from the tenor that ran.
REALISED_PREFIX = "realised_"


@dataclass(frozen=True, slots=True)
class Sigma:
    """A volatility and the rung that produced it."""

    value: float
    source: str
    #: Every rung below the first: the target is degraded.
    is_fallback: bool
    #: A realised rung measured on bars older than ``bars_freshness_days``.
    is_stale: bool

    def __post_init__(self) -> None:
        if not math.isfinite(self.value) or self.value <= 0:
            raise ValueError(f"sigma must be finite and positive, got {self.value!r}")


def realised_vol(
    closes: Sequence[float], *, tenor_days: int, trading_days_per_year: float
) -> float | None:
    """Annualised realised volatility over the last ``tenor_days`` returns, or ``None``.

    ``None`` without ``tenor_days + 1`` positive closes, or on a window with no dispersion: a
    caller that gets ``None`` moves down the ladder and never receives a number nobody measured.
    """
    usable = [close for close in closes if close > 0]
    if len(usable) < tenor_days + 1:
        return None
    daily = stdev(log_returns(usable[-(tenor_days + 1) :]))
    if daily is None or daily <= 0:
        return None
    return annualise(daily, trading_days_per_year=trading_days_per_year)


class SigmaLadder:
    """Resolves the strike-band sigma by walking the configured rungs in order."""

    def __init__(self, collectors: CollectorsConfig) -> None:
        self._vol = collectors.vol
        self._rungs: tuple[SigmaRung, ...] = tuple(collectors.chain.sigma_ladder)
        self._freshness_days = collectors.chain.bars_freshness_days

    @property
    def closes_needed(self) -> int:
        """The daily closes the most demanding realised rung reads."""
        return max(self._vol.primary_tenor_days, self._vol.fallback_tenor_days) + 1

    def resolve(
        self,
        closes: Sequence[float],
        *,
        vendor_iv: float | None,
        newest_close: date | None,
        session: date,
    ) -> Sigma:
        """The first rung that yields a sigma. The ladder ends with ``default`` (the schema
        refuses one that does not), so a snapshot is never lost for want of history."""
        stale = newest_close is not None and (session - newest_close).days > self._freshness_days
        for index, rung in enumerate(self._rungs):
            found = self._try(rung, closes, vendor_iv)
            if found is None:
                continue
            value, label = found
            return Sigma(
                value=value,
                source=label,
                is_fallback=index > 0,
                is_stale=stale and label.startswith(REALISED_PREFIX),
            )
        raise AssertionError("the sigma ladder ends with its default rung")  # pragma: no cover

    def _try(
        self, rung: SigmaRung, closes: Sequence[float], vendor_iv: float | None
    ) -> tuple[float, str] | None:
        if rung == "realised_primary":
            return self._realised(closes, self._vol.primary_tenor_days)
        if rung == "realised_fallback":
            return self._realised(closes, self._vol.fallback_tenor_days)
        if rung == "vendor_iv":
            if vendor_iv is None or not math.isfinite(vendor_iv) or vendor_iv <= 0:
                return None
            return vendor_iv, VENDOR_IV
        return self._vol.sigma_default, DEFAULT

    def _realised(self, closes: Sequence[float], tenor: int) -> tuple[float, str] | None:
        value = realised_vol(
            closes, tenor_days=tenor, trading_days_per_year=self._vol.trading_days_per_year
        )
        return None if value is None else (value, f"{REALISED_PREFIX}{tenor}d")
