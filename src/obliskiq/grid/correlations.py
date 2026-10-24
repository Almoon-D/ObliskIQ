"""The cross-asset correlation matrix: every admitted pair of legs, at every configured window,
against the pair's own norm, written to the measurement store.

Ported from lite's ``CorrelationMatrix`` (``compute/rankings.py``). A **leg** is ``(asset class,
series, market)``: a market has an equity index, an FX rate against the base, a policy rate and a
long rate, and those are different series. An equity leg's series is the market name (a market is
measured on exactly one index), an FX or rate leg's the macro series id. A series several markets
claim (six euro-area markets share ``ECB_DFR``) is one leg, owned by the first claimant in sorted
order.

* **Which pairs** is architecture, not a threshold: equity x equity, equity x FX, equity x rates.
  FX x FX is the dollar index measured eleven ways, and rate x rate would mostly correlate one
  shared policy rate with itself.
* **A rate is differenced, a price log-returned**: a log return on a rate near zero is not a
  return. The transform is a function of the asset class and has no column.
* **Filtered on release, aligned on reference**: an observation enters once its release date has
  passed; it pairs with the bar of the day it applied. Alignment is by calendar date.
* **Three refusals produce counts, not numbers**: mixed frequencies (never resampled; an
  undeclared cadence is refused, not assumed), a units change inside the window, and the norm
  floor on independent readings.

Per pair and window the leaves are the correlation (``n_obs`` the aligned changes the pair has),
the norm's mean and standard deviation, and the shift in standard deviations (``n_obs`` the
independent readings behind the norm). Whether a shift is flagged is read off it against
``grid.correlation_shift_sd`` by the view.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import combinations
from typing import Final

from ..config.schema import ObliskConfig
from ..data.macro.catalog import MacroCatalog
from ..domain.measurement import EntityKind, Measurement
from ..quant.stats import (
    TRANSFORM_LEVEL_CHANGE,
    TRANSFORM_LOG_RETURN,
    correlation,
    norm_deviation,
    paired_changes,
    rolling_correlations,
)
from ..store import measurements as measurements_repo
from ..store import repos
from ..store.db import Store
from .cells import Cell, inputs_hash, to_measurement
from .equity_grid import resolve_conid
from .frames import MacroReader, market_spec
from .indicators import GridParams
from .vocabulary import Reason, Units

ASSET_EQUITY: Final = "equity"
ASSET_FX: Final = "fx"
ASSET_RATE: Final = "rate"
#: Which pairs are computed: architecture, not a setting.
CORRELATED_CLASS_PAIRS: Final = (
    (ASSET_EQUITY, ASSET_EQUITY),
    (ASSET_EQUITY, ASSET_FX),
    (ASSET_EQUITY, ASSET_RATE),
)
#: Equity bars are daily by construction.
DAILY: Final = "daily"
#: The leaves' family.
FAMILY: Final = "cross_asset_correlation"


def _digest(rows: object) -> str:
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Leg:
    """One side of a pair: what it is, which series, and the market that claims it."""

    asset_class: str
    series: str
    market: str

    @property
    def transform(self) -> str:
        return TRANSFORM_LEVEL_CHANGE if self.asset_class == ASSET_RATE else TRANSFORM_LOG_RETURN

    @property
    def key(self) -> tuple[str, str]:
        return (self.asset_class, self.series)

    @property
    def text(self) -> str:
        return f"{self.asset_class}:{self.series}"

    def describe(self) -> str:
        if self.asset_class == ASSET_EQUITY:
            return f"{self.market} equities"
        return f"{self.market} {self.asset_class} ({self.series})"


def pair_id(left: Leg, right: Leg) -> str:
    return f"{left.text}|{right.text}"


@dataclass(frozen=True, slots=True)
class LegSeries:
    """One leg's values by date, with the frequencies and units its rows declare."""

    values: dict[date, float]
    frequencies: frozenset[str]
    units: frozenset[str]
    fingerprint: str


def leaves_for(window: int) -> tuple[str, str, str, str]:
    return (
        f"correlation_{window}d",
        f"norm_mean_{window}d",
        f"norm_sd_{window}d",
        f"shift_{window}d",
    )


class CorrelationMatrix:
    """Every admitted pair at every window, against its own norm."""

    def __init__(self, store: Store, settings: ObliskConfig) -> None:
        self._store = store
        self._settings = settings
        self._params = GridParams.from_settings(settings)
        self._catalog = MacroCatalog.from_settings(settings)
        self._universe = settings.universe
        self._digest = inputs_hash({"params": repr(self._params)})

    def legs(self) -> list[Leg]:
        """Every distinct leg the declarations offer; a market declaring a series absent offers
        no leg for it, and a series several markets claim is one leg."""
        legs: dict[tuple[str, str], Leg] = {}
        for market in sorted(self._universe.grid.markets):
            spec = market_spec(self._universe, market)
            offered = [Leg(ASSET_EQUITY, market, market)]
            if spec.fx is not None:
                offered.append(Leg(ASSET_FX, spec.fx, market))
            offered += [
                Leg(ASSET_RATE, series, market)
                for series in (spec.policy, spec.long_rate)
                if series is not None
            ]
            for leg in offered:
                legs.setdefault(leg.key, leg)
        return sorted(legs.values(), key=lambda leg: leg.key)

    @staticmethod
    def pairs(legs: Sequence[Leg]) -> Iterator[tuple[Leg, Leg]]:
        admitted = {frozenset(pair) for pair in CORRELATED_CLASS_PAIRS}
        for left, right in combinations(legs, 2):
            if frozenset((left.asset_class, right.asset_class)) in admitted:
                yield left, right

    def build(self, *, as_of: date, computed_at: datetime) -> list[Measurement]:
        reader = MacroReader(self._store, self._catalog, as_of)
        legs = self.legs()
        series = {leg.key: self._values(leg, reader, as_of=as_of) for leg in legs}
        rows: list[Measurement] = []
        for left, right in self.pairs(legs):
            for window in self._params.correlation_windows:
                left_series, right_series = series[left.key], series[right.key]
                cells = self._cells(left, right, window, left_series, right_series)
                fingerprint = {
                    "settings": self._digest,
                    "as_of": as_of,
                    "left": None if left_series is None else left_series.fingerprint,
                    "right": None if right_series is None else right_series.fingerprint,
                }
                rows += [
                    to_measurement(
                        cell,
                        entity_kind=EntityKind.CORRELATION_PAIR,
                        entity_id=pair_id(left, right),
                        as_of=as_of,
                        source=f"{left.describe()} vs {right.describe()}",
                        fingerprint=fingerprint,
                        computed_at=computed_at,
                    )
                    for cell in cells
                ]
        return rows

    def write(self, rows: Sequence[Measurement]) -> int:
        return measurements_repo.upsert_measurements(self._store, rows)

    # ── internals ──

    def _values(self, leg: Leg, reader: MacroReader, *, as_of: date) -> LegSeries | None:
        """The leg's series by date: an equity leg off its native index's bars, an FX or rate
        leg off the macro store, released by ``as_of`` and keyed by reference date."""
        since = as_of - timedelta(days=self._params.years(self._params.weekly_window_years))
        if leg.asset_class == ASSET_EQUITY:
            symbol = self._universe.grid.markets[leg.market].native_index
            declared = None if symbol is None else self._universe.instrument(symbol)
            conid = None if declared is None else resolve_conid(self._store, declared, as_of=as_of)
            if conid is None:
                return None
            bars = repos.bar_series(self._store, conid, as_of=as_of, since=since)
            if not bars:
                return None
            values = {bar.session_date: bar.close for bar in bars}
            return LegSeries(
                values, frozenset({DAILY}), frozenset(), _digest(sorted(values.items()))
            )
        series = reader.series(leg.series)
        observations = (
            [] if series is None else [o for o in series.observations if o.reference_date >= since]
        )
        if not observations:
            return None
        values = {o.reference_date: o.value for o in observations}
        frequencies = frozenset(o.frequency for o in observations if o.frequency is not None)
        return LegSeries(
            values,
            frequencies,
            frozenset(o.units for o in observations if o.units is not None),
            _digest([(o.reference_date, o.release_date, o.value) for o in observations]),
        )

    def _cells(
        self,
        left: Leg,
        right: Leg,
        window: int,
        left_series: LegSeries | None,
        right_series: LegSeries | None,
    ) -> list[Cell]:
        names = leaves_for(window)
        label = f"{window}d"

        def absent(reason: str, code: Reason) -> list[Cell]:
            return [Cell.absent(name, FAMILY, reason, code=code, window=label) for name in names]

        if left_series is None or right_series is None:
            missing = left if left_series is None else right
            detail = (
                "has no bars for its native index; a foreign-listed wrapper is not a substitute"
                if missing.asset_class == ASSET_EQUITY
                else f"has no observation of {missing.series} released by this session"
            )
            return absent(f"{missing.describe()} {detail}", Reason.SERIES_ABSENT)
        for leg, values in ((left, left_series), (right, right_series)):
            if not values.frequencies:
                return absent(
                    f"{leg.describe()} declares no frequency on any observation in the window; "
                    "an undeclared cadence is refused rather than assumed to match",
                    Reason.MIXED_FREQUENCY,
                )
            if len(values.frequencies) > 1:
                return absent(
                    f"{leg.describe()} carries more than one frequency "
                    f"({', '.join(sorted(values.frequencies))})",
                    Reason.MIXED_FREQUENCY,
                )
        if left_series.frequencies != right_series.frequencies:
            return absent(
                f"{left.describe()} is {'/'.join(sorted(left_series.frequencies))} and "
                f"{right.describe()} is {'/'.join(sorted(right_series.frequencies))}: a "
                "correlation pairs two series period by period, so mixing frequencies would mean "
                "resampling one; it is refused, never resampled",
                Reason.MIXED_FREQUENCY,
            )
        for leg, values in ((left, left_series), (right, right_series)):
            if len(values.units) > 1:
                return absent(
                    f"{leg.describe()} carries more than one unit in the window "
                    f"({', '.join(sorted(values.units))})",
                    Reason.MIXED_UNITS,
                )
        days = sorted(set(left_series.values) & set(right_series.values))
        own, other = paired_changes(
            [left_series.values[day] for day in days],
            [right_series.values[day] for day in days],
            left_transform=left.transform,
            right_transform=right.transform,
        )
        needed = max(self._params.min_correlation, window)
        if len(own) < needed:
            return absent(
                f"{len(own)} overlapping change(s), below the {needed} a {window}-day correlation "
                "needs",
                Reason.SHORT_SAMPLE,
            )
        current = correlation(own[-window:], other[-window:])
        if current is None:
            return absent(
                "one of the two series has no dispersion in this window", Reason.NO_DISPERSION
            )
        cells = [
            Cell.measured(
                names[0],
                FAMILY,
                current,
                units=Units.RATIO,
                window=f"{label} ({len(own)} aligned changes)",
                n_obs=len(own),
            )
        ]
        sessions = round(
            self._params.correlation_norm_window_years * self._params.trading_days_per_year
        )
        history = rolling_correlations(own, other, window=window, sample=sessions, step=window)
        floor = self._params.correlation_norm_min_independent
        deviation = norm_deviation(history) if len(history) >= floor else None
        norm = f"{label} vs its {self._params.correlation_norm_window_years}y norm"
        if deviation is None:
            reason = (
                f"the correlation is measured, but its "
                f"{self._params.correlation_norm_window_years}-year history holds "
                f"{len(history)} independent {window}-day reading(s) against a floor of {floor}, "
                "or no dispersion among them: there is no norm to deviate from"
            )
            return cells + [
                Cell.absent(name, FAMILY, reason, code=Reason.NO_DISPERSION, window=norm)
                for name in names[1:]
            ]
        window_text = f"{norm} ({len(history)} independent readings)"
        _, mean_leaf, sd_leaf, shift_leaf = names
        norm_leaves = (
            (mean_leaf, deviation.mean, Units.RATIO),
            (sd_leaf, deviation.sd, Units.RATIO),
            (shift_leaf, deviation.z_score, Units.SIGMA),
        )
        return [
            *cells,
            *(
                Cell.measured(
                    name, FAMILY, value, units=units, window=window_text, n_obs=len(history)
                )
                for name, value, units in norm_leaves
            ),
        ]
