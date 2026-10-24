"""The four-way return decomposition, its flags and the carry-illusion test (lite's
``compute/fx_engine.py``), written as leaves of each grid market (entity kind ``market``).

Nominal returns from high-inflation, depreciating-currency markets are mirages: a market up 60 %
in local terms can be down in euros. Every market return decomposes four ways::

    local nominal -> local real -> base nominal -> base real

and **ranking reads base real only** (:func:`fx_ranking`). Local nominal is kept because it must
be displayed, never because it may be ranked on. The FX convention (local per base, so a local
asset is worth ``local / fx`` in the base) is written once, in ``grid/frames.fx_ratio``. A real
return deflates by CPI compounded to the horizon, never by a whole year's inflation.

The horizons are the macro board's (``grid.macro_board.change_horizons_months``): the native
index's return from its close on or before the window's start to its newest close, as known on
the day. The flags (``fx_inflation.high_inflation_cpi_yoy``, ``high_depreciation_3y_annualised``
over ``depreciation_window_years``) and the carry verdict are derived from the stored leaves when
read, so a changed threshold never leaves a stale flag behind.

**The carry-illusion test** (covered interest parity): hedging a high-yield currency costs about
the rate differential, so a hedged high-yield bond returns about the base rate. Its excess over
the base rate is ``yield - local_rate``: the base rate cancels, which is the theory, not a slip,
and a test pins the identity. The hedge cost is approximated with the declared policy rates,
overnight instruments set against a ten-year yield, and every carry leaf says so. Hedging is a
yardstick here, never a recommendation: the mandate prohibits FX forwards.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from ...config.schema import ObliskConfig
from ...data.macro.catalog import MacroCatalog
from ...domain.measurement import EntityKind, Measurement, MeasurementStatus
from ...grid.cells import Cell, inputs_hash, to_measurement
from ...grid.equity_grid import resolve_conid
from ...grid.frames import (
    CpiReading,
    MacroReader,
    MarketSpec,
    bar_frame,
    calendar_days,
    cpi_yoy,
    fx_ratio,
    market_spec,
)
from ...grid.vocabulary import Reason, Units
from ...quant.stats import annualised_from_period, simple_return
from ...store import measurements as measurements_repo
from ...store.db import Store

FX_REAL: Final = "fx_real"
CARRY: Final = "carry"
#: The carry verdicts, derived when read.
ILLUSION: Final = "illusion"
OPPORTUNITY: Final = "opportunity"
NOT_MEASURED: Final = "n/a"
#: The legs of the decomposition, in order; ``base_real`` is the only rankable one.
FOUR_WAY: Final = ("local_nominal", "local_real", "base_nominal", "base_real")
RANK_LEG: Final = "base_real"
FX_LEG: Final = "fx_return"
CARRY_LEAVES: Final = (
    "carry_rate_differential",
    "carry_hedged_equivalent",
    "carry_excess_over_base",
    "carry_base_real_yield",
)
CARRY_BASIS: Final = (
    "hedge cost approximated by the local policy rate less the base policy rate, both overnight; "
    "covered interest parity prices the forward off money-market rates at the hedge tenor, so on "
    "a steep curve this differential is not the one a hedge would cost"
)
#: The bars read reach back this many windows, so a holiday at the window's start still finds
#: the close before it.
LOOKBACK_WINDOWS: Final = 2


def leg_leaf(leg: str, months: int) -> str:
    return f"{leg}_{months}m"


def _digest(rows: object) -> str:
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def deflate(
    nominal: float | None, inflation_yoy: float | None, days: int, basis: float
) -> float | None:
    """A nominal return made real over the same horizon: annual inflation compounded down to it,
    never subtracted whole (a year of inflation off a five-day return is absurd)."""
    if nominal is None or inflation_yoy is None or basis <= 0:
        return None
    horizon_inflation = float((1.0 + inflation_yoy) ** (days / basis)) - 1.0
    if horizon_inflation <= -1.0:
        return None
    return float((1.0 + nominal) / (1.0 + horizon_inflation) - 1.0)


def _leaf(
    name: str, family: str, value: float | None, gap: str | None, code: Reason, **kw: str
) -> Cell:
    if value is None:
        return Cell.absent(
            name, family, gap or f"{name} could not be computed", code=code, window=kw.get("window")
        )
    return Cell.measured(
        name, family, value, units=Units.DECIMAL, window=kw.get("window"), reason=kw.get("reason")
    )


@dataclass(frozen=True, slots=True)
class Index:
    """A market's native index closes as known on the day, or why there are none."""

    closes: tuple[tuple[date, float], ...]
    gap: str | None
    source: str


class FxEngine:
    """Writes every grid market's four-way decomposition and carry test."""

    def __init__(self, store: Store, settings: ObliskConfig) -> None:
        self._store = store
        self._settings = settings
        self._base = settings.account.base_currency
        self._basis = settings.numerics.bsm.day_count_basis
        self._horizons = tuple(settings.grid.macro_board.change_horizons_months)
        self._fx = settings.fx_inflation
        self._catalog = MacroCatalog.from_settings(settings)
        universe = settings.universe
        regions = universe.macro_board.regions
        base_region = next(
            (
                name
                for name in sorted(regions)
                if regions[name].currency is not None
                and market_spec(universe, name).currency == self._base
            ),
            None,
        )
        self._base_spec = None if base_region is None else market_spec(universe, base_region)
        self._digest = inputs_hash(
            {"fx": repr(self._fx), "horizons": self._horizons, "base": self._base}
        )

    def build(self, *, as_of: date, computed_at: datetime) -> list[Measurement]:
        reader = MacroReader(self._store, self._catalog, as_of)
        rows: list[Measurement] = []
        for market in sorted(self._settings.universe.grid.markets):
            rows += self.for_market(market, reader=reader, as_of=as_of, computed_at=computed_at)
        return rows

    def write(self, rows: Sequence[Measurement]) -> int:
        return measurements_repo.upsert_measurements(self._store, rows)

    def for_market(
        self, market: str, *, reader: MacroReader, as_of: date, computed_at: datetime
    ) -> list[Measurement]:
        spec = market_spec(self._settings.universe, market)
        index = self._index(spec, as_of)
        cpi = cpi_yoy(reader, spec, days_per_year=self._basis)
        base_cpi = self._base_cpi(reader)
        cells: list[Cell] = []
        for months in self._horizons:
            cells += self._horizon(spec, months, index, cpi, base_cpi, reader, as_of)
        cells += self._levels(spec, cpi, base_cpi, reader)
        cells += self._carry(spec, base_cpi, reader, as_of)
        series = list(spec.series_ids) + (
            [] if self._base_spec is None else list(self._base_spec.series_ids)
        )
        fingerprint = {
            "settings": self._digest,
            "as_of": as_of,
            "closes": _digest(index.closes),
            "series": _digest([(sid, reader.latest(sid)) for sid in series]),
        }
        return [
            to_measurement(
                cell,
                entity_kind=EntityKind.MARKET,
                entity_id=market,
                as_of=as_of,
                source=index.source,
                fingerprint=fingerprint,
                computed_at=computed_at,
            )
            for cell in cells
        ]

    # ── inputs ──

    def _index(self, spec: MarketSpec, as_of: date) -> Index:
        if spec.native_index is None:
            return Index((), f"{spec.market} declares no native index to measure a return on", "-")
        declared = self._settings.universe.instrument(spec.native_index)
        conid = None if declared is None else resolve_conid(self._store, declared, as_of=as_of)
        if conid is None:
            return Index(
                (),
                f"{spec.native_index} is not seeded, so there is no level to measure (a US-listed "
                "wrapper is no substitute: it embeds a dollar trade in every reading)",
                f"bars:{spec.native_index}",
            )
        reach = calendar_days(max(self._horizons), days_per_year=self._basis) * LOOKBACK_WINDOWS
        frame = bar_frame(self._store, conid, as_of=as_of, since=as_of - timedelta(days=reach))
        return Index(tuple(zip(frame.dates, frame.close, strict=True)), None, f"bars:{conid}")

    def _base_cpi(self, reader: MacroReader) -> CpiReading:
        if self._base_spec is None:
            return CpiReading(
                None, None, f"no macro-board region in {self._base} declares the base CPI"
            )
        return cpi_yoy(reader, self._base_spec, days_per_year=self._basis)

    # ── the decomposition ──

    def _horizon(
        self,
        spec: MarketSpec,
        months: int,
        index: Index,
        cpi: CpiReading,
        base_cpi: CpiReading,
        reader: MacroReader,
        as_of: date,
    ) -> list[Cell]:
        days = calendar_days(months, days_per_year=self._basis)
        window = f"{months}m ({days}d)"
        start = as_of - timedelta(days=days)
        before = [close for day, close in index.closes if day <= start]
        local: float | None = None
        local_gap = index.gap
        if local_gap is None:
            if before:
                local = simple_return(before[-1], index.closes[-1][1])
            else:
                local_gap = (
                    f"{spec.native_index} holds no close on or before {start}, the start of the "
                    f"{months}-month window"
                )
        ratio = fx_ratio(reader, spec, horizon_days=days, base_currency=self._base)
        local_real = deflate(local, cpi.value, days, self._basis)
        base_nominal = (
            None if local is None or ratio.ratio is None else (1 + local) * ratio.ratio - 1
        )
        base_real = deflate(base_nominal, base_cpi.value, days, self._basis)
        upstream = local_gap if local is None else None
        return [
            _leaf(
                leg_leaf("local_nominal", months),
                FX_REAL,
                local,
                local_gap,
                Reason.NO_HISTORY,
                window=window,
            ),
            _leaf(
                leg_leaf("local_real", months),
                FX_REAL,
                local_real,
                upstream or cpi.problem,
                Reason.SERIES_ABSENT,
                window=window,
            ),
            _leaf(
                leg_leaf("base_nominal", months),
                FX_REAL,
                base_nominal,
                upstream or ratio.problem,
                Reason.NO_FX,
                window=window,
            ),
            _leaf(
                leg_leaf("base_real", months),
                FX_REAL,
                base_real,
                upstream or ratio.problem or base_cpi.problem,
                Reason.SERIES_ABSENT,
                window=window,
            ),
            _leaf(
                leg_leaf(FX_LEG, months),
                FX_REAL,
                None if ratio.ratio is None else ratio.ratio - 1,
                ratio.problem,
                Reason.NO_FX,
                window=window,
            ),
        ]

    def _levels(
        self, spec: MarketSpec, cpi: CpiReading, base_cpi: CpiReading, reader: MacroReader
    ) -> list[Cell]:
        years = self._fx.depreciation_window_years
        days = round(years * self._basis)
        ratio = fx_ratio(reader, spec, horizon_days=days, base_currency=self._base)
        depreciation = (
            None
            if ratio.ratio is None
            else annualised_from_period(ratio.ratio - 1, years=float(years))
        )
        return [
            _leaf("cpi_yoy", FX_REAL, cpi.value, cpi.problem, Reason.SERIES_ABSENT),
            _leaf("base_cpi_yoy", FX_REAL, base_cpi.value, base_cpi.problem, Reason.SERIES_ABSENT),
            _leaf(
                "depreciation_annualised",
                FX_REAL,
                depreciation,
                ratio.problem or "the move against the base cannot be annualised",
                Reason.NO_FX,
                window=f"{years}y",
            ),
        ]

    # ── the carry test ──

    def _carry(
        self, spec: MarketSpec, base_cpi: CpiReading, reader: MacroReader, as_of: date
    ) -> list[Cell]:
        base_policy = None if self._base_spec is None else self._base_spec.policy
        inputs = {
            "ten-year yield": spec.long_rate,
            "local policy rate": spec.policy,
            "base policy rate": base_policy,
        }
        values = {
            what: None if series is None else reader.value_at(series, as_of)
            for what, series in inputs.items()
        }
        nominal, local, base = values.values()
        if nominal is None or local is None or base is None:
            missing = ", ".join(what for what, value in values.items() if value is None)
            reason = (
                f"the carry test needs the {missing}; reporting the headline yield without it is "
                "the error the test exists to prevent"
            )
            return [
                Cell.absent(name, CARRY, reason, code=Reason.SERIES_ABSENT) for name in CARRY_LEAVES
            ]
        differential = local - base
        hedged = nominal - differential
        real = None if base_cpi.value is None else hedged - base_cpi.value
        found = (differential, hedged, hedged - base, real)
        return [
            _leaf(name, CARRY, value, base_cpi.problem, Reason.SERIES_ABSENT, reason=CARRY_BASIS)
            for name, value in zip(CARRY_LEAVES, found, strict=True)
        ]


# ── the views ──


def market_leaves(store: Store, *, as_of: date) -> dict[str, dict[str, Measurement]]:
    """The newest market build on or before ``as_of``, by market then leaf."""
    session = measurements_repo.measured_session(store, EntityKind.MARKET, as_of=as_of)
    if session is None:
        return {}
    out: dict[str, dict[str, Measurement]] = {}
    for row in measurements_repo.session_measurements(store, EntityKind.MARKET, as_of=session):
        out.setdefault(row.entity_id, {})[row.leaf] = row
    return out


def _value(leaves: dict[str, Measurement], leaf: str) -> float | None:
    row = leaves.get(leaf)
    return None if row is None or row.status is not MeasurementStatus.OK else row.value


@dataclass(frozen=True, slots=True)
class RankedMarket:
    market: str
    base_real: float
    local_nominal: float | None
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CarryVerdict:
    """The carry-illusion test as read from a market's stored leaves."""

    market: str
    verdict: str
    reason: str


def flags(settings: ObliskConfig, leaves: dict[str, Measurement]) -> tuple[str, ...]:
    """A market's flags from its stored leaves and today's thresholds: a flagged market is ranked
    on base real like every other, and its thesis must address the currency."""
    fx = settings.fx_inflation
    out: list[str] = []
    cpi = _value(leaves, "cpi_yoy")
    if cpi is not None and cpi >= fx.high_inflation_cpi_yoy:
        out.append(f"CPI {cpi:.1%} year on year")
    depreciation = _value(leaves, "depreciation_annualised")
    if depreciation is not None and depreciation <= -fx.high_depreciation_3y_annualised:
        out.append(
            f"{depreciation:.1%} a year against the base over {fx.depreciation_window_years}y"
        )
    return tuple(out)


def fx_ranking(
    store: Store, settings: ObliskConfig, *, as_of: date, months: int
) -> list[RankedMarket]:
    """Markets ranked on base real over ``months``, best first, and on nothing else. A market
    whose base real is not measured, or (with ``fx_inflation.require_four_way_decomposition``)
    whose decomposition misses a leg, is left out rather than ranked last: an unmeasured market
    is not a poor one."""
    ranked: list[RankedMarket] = []
    for market, leaves in sorted(market_leaves(store, as_of=as_of).items()):
        legs = {leg: _value(leaves, leg_leaf(leg, months)) for leg in FOUR_WAY}
        base_real = legs[RANK_LEG]
        if base_real is None:
            continue
        if settings.fx_inflation.require_four_way_decomposition and None in legs.values():
            continue
        ranked.append(
            RankedMarket(market, base_real, legs["local_nominal"], flags(settings, leaves))
        )
    return sorted(ranked, key=lambda row: row.base_real, reverse=True)


def carry_verdict(
    settings: ObliskConfig, market: str, leaves: dict[str, Measurement]
) -> CarryVerdict:
    """Illusion when the hedged equivalent clears the base rate by no more than
    ``fx_inflation.carry_illusion_tolerance``, or its base-real yield is not positive; otherwise
    an opportunity, which is term or credit premium, not carry."""
    tolerance = settings.fx_inflation.carry_illusion_tolerance
    excess = _value(leaves, "carry_excess_over_base")
    real = _value(leaves, "carry_base_real_yield")
    if excess is None:
        row = leaves.get("carry_excess_over_base")
        return CarryVerdict(market, NOT_MEASURED, "not built" if row is None else str(row.reason))
    if excess <= tolerance:
        return CarryVerdict(
            market,
            ILLUSION,
            f"the hedged equivalent clears the base rate by {excess:.1%}, inside the "
            f"{tolerance:.1%} tolerance: the headline yield is compensation for expected "
            "depreciation, not an opportunity",
        )
    if real is not None and real <= 0:
        return CarryVerdict(
            market, ILLUSION, f"the hedged equivalent is {real:.1%} in real base-currency terms"
        )
    return CarryVerdict(
        market,
        OPPORTUNITY,
        f"the hedged equivalent clears the base rate by {excess:.1%}, beyond the {tolerance:.1%} "
        "tolerance: a term or credit premium, carried unhedged and knowingly",
    )


def carry_verdicts(store: Store, settings: ObliskConfig, *, as_of: date) -> list[CarryVerdict]:
    return [
        carry_verdict(settings, market, leaves)
        for market, leaves in sorted(market_leaves(store, as_of=as_of).items())
    ]
