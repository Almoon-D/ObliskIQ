"""Realised volatility, and the sigma that sets the strike bounds.

Three things live here.

**Realised volatility** from stored daily bars: the standard deviation of log
returns over a tenor, annualised on ``data.trading_days_per_year``. Annualised,
because every other volatility in this system is — vendor implied vol,
``data.sigma_default``, the sigma the pricer takes — and a ladder whose rungs are
quoted in different units silently rescales the strike band depending on which
one fired.

**The range-based estimators**, Parkinson and Garman-Klass, which read the high,
the low and the open that ``bars`` has carried since migration 001. They are
ADDITIONAL to the close-to-close figure and never a substitute for it: they
measure the intraday range and see nothing of the overnight gap, so they are a
different quantity rather than a sharper reading of the same one. The section
below says what follows from that; ``docs/DECISIONS.md`` says why it decides the
design.

**The sigma ladder.** ``chain_collector`` needs a sigma to size its strike band,
and the band is worthless without one. The rungs, in ``collectors.chain.sigma_ladder``:

1. realised vol over ``data.strike_bounds_rv_tenor_days`` of daily bars — what
   the design actually calls for;
2. realised vol over ``collectors.chain.realised_fallback_tenor_days``, for an
   instrument with some history but not enough;
3. the vendor implied vol on the snapshot;
4. ``data.sigma_default``, scaled by the expiry's own tau at the point of use.

Rungs 2 to 4 are fallbacks and every one of them marks the target DEGRADED, so a
run that leaned on them says so in ``collector_runs`` instead of looking like a
clean run over a quietly wider band. The row records the sigma AND the rung, so a
band can be audited later rather than reconstructed.

Nothing here ever returns a fixed percentage band. Rung 4 is a fixed *sigma*,
which is not the same thing: it is still scaled by the real time to expiry, so a
120-day expiry gets a wider band than a 95-day one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from statistics import stdev

from ..config import Config
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..store import Store
from ..util.timeutil import parse_date

log = get_logger("collect.vol")

#: Rung identifiers accepted in ``collectors.chain.sigma_ladder``. The digits in
#: the shipped names are documentation: the tenor actually used comes from
#: ``data.strike_bounds_rv_tenor_days`` and
#: ``collectors.chain.realised_fallback_tenor_days``, and the label written to
#: ``chain_snapshots.sigma_source`` is derived from the tenor that really ran, so
#: a retuned config can never leave a row claiming a window it did not use.
RUNG_REALISED_PRIMARY = frozenset({"realised_30d", "realised_primary"})
RUNG_REALISED_FALLBACK = frozenset({"realised_10d", "realised_fallback"})
RUNG_VENDOR_IV = "vendor_iv"
RUNG_DEFAULT = "default"

#: Bar frequency labels as stored in ``bars.freq``.
FREQ_DAILY = "1d"
FREQ_WEEKLY = "1w"
FREQ_MONTHLY = "1mo"


#: The rung that is not a fallback. Everything else marks the target degraded.
SOURCE_DEFAULT = "default"
SOURCE_VENDOR_IV = "vendor_iv"

#: Both realised rungs label themselves from the tenor that actually ran, so
#: they are recognised by prefix rather than by an enumerated constant.
_REALISED_LABEL_PREFIX = "realised_"

#: Two bars is the arithmetic minimum for an overnight gap to exist between them.
_MIN_BARS_FOR_A_GAP = 2


@dataclass(frozen=True)
class Sigma:
    """A volatility and where it came from."""

    value: float
    source: str
    #: True for every rung below the first. Drives the ``degraded`` run status.
    is_fallback: bool
    #: True when a REALISED rung was measured on bars older than
    #: ``collectors.chain.bars_freshness_days``. The number is arithmetically
    #: fine and describes the wrong month: after an outage the backfill can fail
    #: while the bars it wrote months ago still satisfy the tenor, so the primary
    #: rung succeeds, ``is_fallback`` stays False, and nothing says the band was
    #: sized from stale vol. This is what says it.
    is_stale: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.value) or self.value <= 0:
            raise ValueError(f"sigma must be finite and positive, got {self.value!r}")


def realised_vol(
    closes: list[float], *, tenor_days: int, trading_days_per_year: float
) -> float | None:
    """Annualised realised volatility from ``closes``, or ``None``.

    ``None`` when there are not ``tenor_days + 1`` usable closes — n returns need
    n+1 prices — or when the sample has no dispersion. A caller that gets ``None``
    moves down the ladder; it never receives a number that was not measured.
    """
    usable = [c for c in closes if c is not None and c > 0]
    if len(usable) < tenor_days + 1:
        return None
    window = usable[-(tenor_days + 1) :]
    returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    if len(returns) < 2:
        return None
    daily = stdev(returns)
    if daily <= 0:
        return None
    return daily * math.sqrt(trading_days_per_year)


# ─────────────────────────  range-based estimators  ─────────────────────────
#
# ``realised_vol`` above reads one number per session and throws the rest of the
# bar away. ``bars`` has carried open, high, low and close since migration 001,
# and until this section existed the only reader of the highs and lows was the
# risk engine's ATR.
#
# These are NOT lower-variance estimators of the same quantity, and treating them
# as interchangeable with the close-to-close figure is the mistake this whole
# section is written to prevent. Both assume the price diffuses continuously
# through the session, so both measure the INTRADAY range and see nothing of the
# overnight gap. On the Session 7 ASML capture the mean absolute overnight move
# is roughly four fifths of the mean absolute intraday move, and Parkinson reads
# about six percent below close-to-close at thirty sessions. That is a bias, not
# noise, and it does not average away on a market that closes at night — which is
# every market this system tracks.
#
# What follows from that is in docs/DECISIONS.md, and the short form is: these
# feed their OWN named leaves and their own percentiles, where a rank of
# Parkinson against Parkinson's own history is legitimate because the bias is
# common to the whole series. Nothing subtracts one of these from an implied vol.


#: Parkinson's constant. The expected squared log range of a driftless Brownian
#: motion over one period is ``4 * ln 2`` times its variance, so dividing by it
#: converts a mean squared range back into a variance.
PARKINSON_SCALE = 4.0 * math.log(2.0)

#: Garman-Klass's weight on the squared open-to-close return, ``2 ln 2 - 1``.
#: The estimator is ``0.5 * (ln H/L)^2 - (2 ln 2 - 1) * (ln C/O)^2`` per bar. The
#: second term is SUBTRACTED, but it cannot win: both the open and the close lie
#: inside the bar, so ``|ln C/O| <= ln H/L`` and the weight is below a half. The
#: per-bar term is therefore never negative on a well-formed bar — which is a
#: property of the ORDERING INVARIANT :func:`ohlc_bars` enforces, not of the
#: formula, and it is why that filter refuses a close outside its own range
#: rather than passing it through as merely unusual.
GARMAN_KLASS_CLOSE_WEIGHT = 2.0 * math.log(2.0) - 1.0


def range_bars(highs: list[float], lows: list[float]) -> list[tuple[float, float]]:
    """Usable ``(high, low)`` pairs, oldest first.

    A bar is usable when both prices are present, positive and correctly
    ordered. ``BarSeries.high`` and ``.low`` are annotated ``list[float]`` but
    carry ``None`` wherever the column is NULL, exactly as ``stats.true_ranges``
    already guards against.

    Unusable bars are DROPPED rather than truncating the series, and that is
    sound here in a way it would not be for a close-to-close estimator: every
    term of a range estimator is a property of one bar alone, so a hole leaves no
    spliced return behind it. The caller reports how many bars survived.
    """
    out: list[tuple[float, float]] = []
    # strict: these are lockstep columns of one BarSeries and a length
    # mismatch is a bug in the caller, not a short series to silently trim.
    for high, low in zip(highs, lows, strict=True):
        if high is None or low is None or low <= 0 or high < low:
            continue
        out.append((float(high), float(low)))
    return out


def ohlc_bars(
    opens: list[float], highs: list[float], lows: list[float], closes: list[float]
) -> list[tuple[float, float, float, float]]:
    """Usable ``(open, high, low, close)`` bars, oldest first.

    Stricter than :func:`range_bars` by the two extra prices, and by the ordering
    invariant: a close or an open outside its own bar's range is a corrupt row,
    not a wide one, and Garman-Klass would take the logarithm of it regardless.
    """
    out: list[tuple[float, float, float, float]] = []
    for open_, high, low, close in zip(opens, highs, lows, closes, strict=True):
        if open_ is None or high is None or low is None or close is None:
            continue
        if low <= 0 or high < low:
            continue
        if not (low <= open_ <= high and low <= close <= high):
            continue
        out.append((float(open_), float(high), float(low), float(close)))
    return out


def restated_open_share(bars: list[tuple[float, float, float, float]]) -> float | None:
    """The share of bars whose open is exactly the previous bar's close.

    Some venues do not publish an opening auction print and serve the previous
    close in its place. Garman-Klass reads ``ln C/O`` as the session's directional
    move; where the open is a restatement that term becomes the whole
    close-to-close return, which the estimator then subtracts — so the number it
    returns is not a worse volatility, it is a different formula.

    Compared with ``==`` and not with a tolerance. A restatement is a copied
    value and therefore bit-identical; a tolerance would additionally catch every
    instrument that genuinely opened unchanged, which is a real observation.

    ``None`` when there are fewer than two bars to compare.
    """
    if len(bars) < _MIN_BARS_FOR_A_GAP:
        return None
    # Unpacked rather than indexed: the close is the LAST field, and writing that
    # as a literal index is the kind of number tests/test_no_magic_numbers.py
    # refuses on sight.
    gaps = list(pairwise(bars))
    restated = sum(1 for previous, current in gaps if current[0] == previous[-1])
    return restated / len(gaps)


def parkinson_from_bars(
    bars: list[tuple[float, float, float]] | list[tuple[float, float]],
    *,
    trading_days_per_year: float,
) -> float | None:
    """Annualised Parkinson volatility over exactly the bars given, or ``None``.

    ``sqrt( sum (ln H/L)^2 / (4 ln2 * n) * basis )``. ``None`` on an empty window
    or one in which every bar has ``high == low`` — a series with no dispersion
    yields no volatility, which is the refusal :func:`realised_vol` makes on a
    flat close series. A single zero-range bar is kept: a halted or untraded
    session really did have no range, and dropping it would raise the estimate by
    removing its own quietest observations.
    """
    if not bars:
        return None
    total = sum(math.log(bar[0] / bar[1]) ** 2 for bar in bars)
    if total <= 0:
        return None
    return math.sqrt(total / (PARKINSON_SCALE * len(bars)) * trading_days_per_year)


def garman_klass_from_bars(
    bars: list[tuple[float, float, float, float]], *, trading_days_per_year: float
) -> float | None:
    """Annualised Garman-Klass volatility over exactly the bars given, or ``None``.

    ``sqrt( mean[ 0.5 (ln H/L)^2 - (2 ln2 - 1)(ln C/O)^2 ] * basis )``. ``None``
    on an empty window or one in which every bar has ``high == low``, which is
    the only way the sum reaches zero once :func:`ohlc_bars` has enforced that
    the open and the close lie inside the bar — see GARMAN_KLASS_CLOSE_WEIGHT.
    The ``<= 0`` guard is kept as a backstop for a caller that assembled its own
    bars: a negative variance must be refused, never square-rooted.

    Whether the open is trustworthy at all is a separate question, and NOT one
    this function can answer by looking at its own output. A restated open makes
    ``ln C/O`` carry the whole close-to-close move, so the subtracted term grows
    and the estimate falls — it stays positive and looks entirely ordinary. Only
    :func:`restated_open_share` can see it, and the caller asks that FIRST.
    """
    if not bars:
        return None
    total = 0.0
    for open_, high, low, close in bars:
        total += 0.5 * math.log(high / low) ** 2
        total -= GARMAN_KLASS_CLOSE_WEIGHT * math.log(close / open_) ** 2
    if total <= 0:
        return None
    return math.sqrt(total / len(bars) * trading_days_per_year)


def parkinson_vol(
    highs: list[float], lows: list[float], *, tenor_days: int, trading_days_per_year: float
) -> float | None:
    """Annualised Parkinson volatility over the last ``tenor_days`` usable bars.

    ``None`` when fewer than ``tenor_days`` bars carry a usable high and low.
    Note the asymmetry with :func:`realised_vol`, which needs ``tenor_days + 1``
    PRICES to make ``tenor_days`` returns: a range estimator reads one term per
    bar, so the same tenor is the same number of sessions and one fewer row.
    """
    bars = range_bars(highs, lows)
    if len(bars) < tenor_days:
        return None
    return parkinson_from_bars(bars[-tenor_days:], trading_days_per_year=trading_days_per_year)


def garman_klass_vol(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    tenor_days: int,
    trading_days_per_year: float,
) -> float | None:
    """Annualised Garman-Klass volatility over the last ``tenor_days`` usable bars.

    Says nothing about whether the OPEN deserves to be read — see
    :func:`restated_open_share`, which the caller applies against a configured
    ceiling before reaching here, because how much restatement is too much is
    policy and belongs in config.yaml rather than in an estimator.
    """
    bars = ohlc_bars(opens, highs, lows, closes)
    if len(bars) < tenor_days:
        return None
    return garman_klass_from_bars(bars[-tenor_days:], trading_days_per_year=trading_days_per_year)


def recent_daily_closes(
    store: Store, contract_id: int, *, limit: int, until: date | None = None
) -> list[float]:
    """The most recent ``limit`` daily closes for a contract, oldest first.

    ``until`` bounds the read to bars dated on or before that session. Without
    it a screen run for a past date reads closes from after the date it is
    pricing, and the realised vol it derives is not one anybody could have had.
    """
    sql = "SELECT close FROM bars WHERE contract_id = ? AND freq = ? AND close IS NOT NULL"
    params: list[object] = [contract_id, FREQ_DAILY]
    if until is not None:
        # See bar_series: ts carries a time, so bound by the following midnight.
        sql += " AND ts < ?"
        params.append((until + timedelta(days=1)).isoformat())
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = store.execute(sql, params).fetchall()
    return [float(row[0]) for row in reversed(rows)]


def newest_daily_bar_date(store: Store, contract_id: int) -> date | None:
    """The date of the newest daily bar held for a contract, or None."""
    row = store.execute(
        "SELECT MAX(ts) FROM bars WHERE contract_id = ? AND freq = ? AND close IS NOT NULL",
        (contract_id, FREQ_DAILY),
    ).fetchone()
    stamp = row[0] if row else None
    # parse_date already takes the leading date off a stamp carrying a time.
    return parse_date(stamp)


class SigmaLadder:
    """Resolves the strike-bound sigma by walking the configured rungs in order."""

    def __init__(self, config: Config) -> None:
        data = config.section("data")
        chain = config.section("collectors.chain")

        self._trading_days_per_year = data.require_float("trading_days_per_year")
        self._primary_tenor = data.require_int("strike_bounds_rv_tenor_days")
        self._fallback_tenor = chain.require_int("realised_fallback_tenor_days")
        self._default_sigma = data.require_float("sigma_default")
        self._ladder = [str(rung) for rung in chain.require_list("sigma_ladder")]
        self._freshness_days = chain.require_int("bars_freshness_days")

        if not self._ladder:
            raise ConfigError(
                "collectors.chain.sigma_ladder is empty. There is no strike band "
                "without a sigma, and no sigma without a rung to produce one."
            )
        known = RUNG_REALISED_PRIMARY | RUNG_REALISED_FALLBACK | {RUNG_VENDOR_IV, RUNG_DEFAULT}
        unknown = [rung for rung in self._ladder if rung not in known]
        if unknown:
            raise ConfigError(
                f"collectors.chain.sigma_ladder names unknown rungs {unknown!r}. "
                f"Known rungs: {sorted(known)}."
            )

    @property
    def rungs(self) -> list[str]:
        return list(self._ladder)

    @property
    def max_bars_needed(self) -> int:
        """How many daily closes the most demanding realised rung wants."""
        return max(self._primary_tenor, self._fallback_tenor) + 1

    def resolve(
        self,
        closes: list[float],
        *,
        vendor_iv: float | None,
        newest_close: date | None = None,
        session: date | None = None,
    ) -> Sigma:
        """Walk the ladder and return the first rung that yields a usable sigma.

        Never returns ``None``: the last rung is a configured default, so a
        snapshot is never lost for want of history. What it does instead is say —
        on the row and on the run — which rung produced the number.

        ``newest_close`` and ``session`` are how a realised rung declares itself
        stale. Enough closes to satisfy the tenor says nothing about WHEN they
        were: after a machine has been off for weeks the primary rung still
        resolves, from bars that describe a different month.
        """
        stale = self._bars_are_stale(newest_close, session)
        for index, rung in enumerate(self._ladder):
            candidate = self._try_rung(rung, closes, vendor_iv)
            if candidate is None:
                continue
            value, label = candidate
            return Sigma(
                value=value,
                source=label,
                is_fallback=index > 0,
                # Only the realised rungs read the bars; a vendor IV or the
                # configured default is not made stale by an old bar series.
                is_stale=stale and label.startswith(_REALISED_LABEL_PREFIX),
            )
        raise ConfigError(
            "no rung of collectors.chain.sigma_ladder produced a sigma. Include "
            f"'{RUNG_DEFAULT}' as the last rung so the ladder always terminates."
        )

    def _bars_are_stale(self, newest_close: date | None, session: date | None) -> bool:
        if newest_close is None or session is None:
            return False
        return (session - newest_close).days > self._freshness_days

    def _try_rung(
        self, rung: str, closes: list[float], vendor_iv: float | None
    ) -> tuple[float, str] | None:
        if rung in RUNG_REALISED_PRIMARY:
            return self._realised(closes, self._primary_tenor)
        if rung in RUNG_REALISED_FALLBACK:
            return self._realised(closes, self._fallback_tenor)
        if rung == RUNG_VENDOR_IV:
            if vendor_iv is None or vendor_iv <= 0:
                return None
            return vendor_iv, SOURCE_VENDOR_IV
        if rung == RUNG_DEFAULT:
            return self._default_sigma, SOURCE_DEFAULT
        return None

    def _realised(self, closes: list[float], tenor: int) -> tuple[float, str] | None:
        value = realised_vol(
            closes, tenor_days=tenor, trading_days_per_year=self._trading_days_per_year
        )
        if value is None:
            return None
        # Label from the tenor that actually ran, not from the config rung name.
        return value, f"{_REALISED_LABEL_PREFIX}{tenor}d"
