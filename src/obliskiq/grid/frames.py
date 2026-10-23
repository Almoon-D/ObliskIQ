"""Typed reads for the measurement families, composed over the store's ``as_of``-bounded
repositories.

Ported from lite's ``compute/frames.py``. Two rules decide whether a derived number can be
trusted, and both live here so no family restates them:

* **Macro series align on release, never reference.** A series holds only what was public on
  the day asked (``repos.macro_series``); a value "as of" an earlier day is the newest by
  reference among those released by then (:func:`known_at`). An observation with no release
  date cannot be placed in time and is left out, counted, and named in the reason.
* **A short series is not a series.** Readers return what the store holds and say how much;
  they never pad, interpolate or carry a value forward.

The market's series are the ones its macro-board region declares (``config/universe.yaml``):
the FX rate against the base currency, the policy rate and CPI of the real rate, the ten-year
yield and the curve's short leg, the CFTC contract of the equity index. A cell the board
declares unmapped hands its declared reason to the leaf.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

from ..config.schema import ObliskConfig
from ..config.schema_universe import POLICY_FIRST_PREFIX, MacroCellConfig, UniverseConfig
from ..data.macro.catalog import MacroCatalog
from ..data.positioning.readings import positioning_reading
from ..domain.enums import MarketDataStatus
from ..domain.macro import MacroObservation, MacroSeries
from ..domain.market import Bar, ChainRow
from ..domain.sources import PositioningReading
from ..quant.smile import Smile, SmileFilters
from ..store import repos
from ..store import sources as sources_repo
from ..store.db import Store

#: Sources publish rates and inflation in percent; leaves are decimals.
PERCENT_TO_DECIMAL: Final = 100.0
#: How a percent series declares its units.
PERCENT_UNITS: Final = "%"
#: Months in a year, to turn a horizon in months into calendar days (a unit).
MONTHS_PER_YEAR: Final = 12
#: The real-rate label whose CPI is already a year-on-year rate (the other one is an index).
CPI_RATE_LABEL: Final = "policy_minus_cpi_rate"
#: The equity-index label whose series is a CFTC contract.
POSITIONING_LABEL: Final = "cot_positioning"
#: The currency of a board region that declares none (the world row): it matches no rate.
NO_CURRENCY: Final = "none"
#: A row whose quote state the route did not report, as ``Smile`` counts it.
UNREPORTED: Final = "unreported"
#: Worst first: a set of rows is labelled by its weakest member; unreported sits above live.
_STATUS_SEVERITY: Final = (
    MarketDataStatus.NONE.value,
    MarketDataStatus.FROZEN.value,
    MarketDataStatus.DELAYED.value,
    UNREPORTED,
    MarketDataStatus.REALTIME.value,
)


def worst_status(counts: Mapping[str, int]) -> MarketDataStatus | None:
    """One label for a mixed set of quote states: the weakest present; ``None`` when nothing was
    reported (or nothing counted), which is never read as real-time."""
    for name in _STATUS_SEVERITY:
        if counts.get(name):
            return None if name == UNREPORTED else MarketDataStatus(name)
    return None


def calendar_days(months: int, *, days_per_year: float) -> int:
    return round(months * days_per_year / MONTHS_PER_YEAR)


# ── bars ──


@dataclass(frozen=True, slots=True)
class BarFrame:
    """Daily bars of one instrument, oldest first, with each column aligned to ``dates``."""

    conid: int | None
    dates: tuple[date, ...] = ()
    open: tuple[float | None, ...] = ()
    high: tuple[float | None, ...] = ()
    low: tuple[float | None, ...] = ()
    close: tuple[float, ...] = ()
    volume: tuple[float | None, ...] = ()

    @classmethod
    def of(cls, conid: int | None, bars: Sequence[Bar]) -> BarFrame:
        return cls(
            conid=conid,
            dates=tuple(bar.session_date for bar in bars),
            open=tuple(bar.open for bar in bars),
            high=tuple(bar.high for bar in bars),
            low=tuple(bar.low for bar in bars),
            close=tuple(bar.close for bar in bars),
            volume=tuple(bar.volume for bar in bars),
        )

    def __len__(self) -> int:
        return len(self.close)

    @property
    def last_close(self) -> float | None:
        return self.close[-1] if self.close else None

    @property
    def last_date(self) -> date | None:
        return self.dates[-1] if self.dates else None

    def since(self, start: date) -> BarFrame:
        """The bars on or after ``start``."""
        first = next((i for i, day in enumerate(self.dates) if day >= start), len(self.dates))
        return BarFrame(
            conid=self.conid,
            dates=self.dates[first:],
            open=self.open[first:],
            high=self.high[first:],
            low=self.low[first:],
            close=self.close[first:],
            volume=self.volume[first:],
        )

    def weekly_closes(self) -> list[float]:
        """The last close of each ISO week, oldest first. The store holds daily bars only (a
        weekly bar stamped at its period start would leak the days after it), so weekly closes
        are derived; the newest week may still be running, as a weekly bar's would."""
        out: list[float] = []
        week: tuple[int, int] | None = None
        for day, close in zip(self.dates, self.close, strict=True):
            iso = day.isocalendar()
            key = (iso.year, iso.week)
            if key == week:
                out[-1] = close
            else:
                out.append(close)
                week = key
        return out


def bar_frame(store: Store, conid: int | None, *, as_of: date, since: date) -> BarFrame:
    """The bars of ``conid`` from ``since`` to ``as_of``; empty for no instrument."""
    if conid is None:
        return BarFrame(conid=None)
    return BarFrame.of(conid, repos.bar_series(store, conid, as_of=as_of, since=since))


# ── macro series ──


def known_at(series: MacroSeries, day: date) -> MacroObservation | None:
    """The newest observation (by reference period) released on or before ``day``."""
    released = [
        o for o in series.observations if o.release_date is not None and o.release_date <= day
    ]
    return released[-1] if released else None


def as_decimal(observation: MacroObservation, units: str) -> float:
    """A percent series as a decimal; any other unit as it stands."""
    return observation.value / PERCENT_TO_DECIMAL if units == PERCENT_UNITS else observation.value


@dataclass(slots=True)
class MacroReader:
    """Declared series as known on ``as_of``, each read once. A series nobody declared is not
    read: it has no source to ask for and no release rule to align on."""

    store: Store
    catalog: MacroCatalog
    as_of: date
    _cache: dict[str, MacroSeries | None] = field(default_factory=dict)

    def series(self, series_id: str) -> MacroSeries | None:
        if series_id not in self._cache:
            spec = self.catalog.spec(series_id)
            self._cache[series_id] = (
                None
                if spec is None
                else repos.macro_series(self.store, series_id, as_of=self.as_of, source=spec.source)
            )
        return self._cache[series_id]

    def units(self, series_id: str) -> str | None:
        spec = self.catalog.spec(series_id)
        return None if spec is None else spec.units

    def frequency(self, series_id: str) -> str | None:
        spec = self.catalog.spec(series_id)
        return None if spec is None else spec.frequency

    def latest(self, series_id: str) -> MacroObservation | None:
        series = self.series(series_id)
        return None if series is None else series.latest

    def value_at(self, series_id: str, day: date) -> float | None:
        """The decimal value known on ``day`` (on or before ``as_of``)."""
        series = self.series(series_id)
        units = self.units(series_id)
        if series is None or units is None:
            return None
        observation = known_at(series, min(day, self.as_of))
        return None if observation is None else as_decimal(observation, units)

    def absence(self, series_id: str) -> str:
        """Why a declared series yields nothing on ``as_of``: never collected, or collected and
        undatable (the remedy is a release rule, not a collection run)."""
        series = self.series(series_id)
        if series is None:
            return f"{series_id} is not declared in sources.macro.series"
        if series.unaligned and not series.observations:
            return (
                f"{series_id} holds {series.unaligned} observation(s) and none carries a release "
                "date, so none can be read point in time; what is missing is its release rule"
            )
        note = (
            f" ({series.unaligned} observation(s) carry no release date)"
            if series.unaligned
            else ""
        )
        return f"{series_id} has no observation released on or before {self.as_of}{note}"


# ── the market's series, from its macro-board region ──


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """The series one grid market's macro families read, each with the declared reason when the
    board says it has none."""

    market: str
    currency: str
    native_index: str | None
    fx: str | None
    fx_gap: str | None
    policy: str | None
    policy_gap: str | None
    cpi: str | None
    cpi_is_yoy: bool
    cpi_gap: str | None
    long_rate: str | None
    long_gap: str | None
    slope_short: str | None
    slope_label: str | None
    slope_gap: str | None
    cot: str | None
    cot_gap: str | None

    @property
    def series_ids(self) -> tuple[str, ...]:
        """Every macro series the market reads (the CFTC contract is not a macro series)."""
        found = (self.fx, self.policy, self.cpi, self.long_rate, self.slope_short)
        return tuple(dict.fromkeys(s for s in found if s is not None))


def _gap(cell: MacroCellConfig | None, what: str, ref: str) -> str:
    if cell is not None and cell.unmapped is not None:
        return f"{ref} declares no {what}: {cell.unmapped}"
    return f"{ref} declares no {what}"


def market_spec(universe: UniverseConfig, market: str) -> MarketSpec:
    """The series ``market`` reads, from the board region of the same name (a configuration
    check guarantees every grid market has one). A board region that is no grid market (the
    world row) is read in its own declared currency and has no native index."""
    grid_market = universe.grid.markets.get(market)
    board = universe.macro_board
    cells = board.regions[market].cells
    fx = cells.get("fx_vs_base")
    real = cells.get("real_rate")
    long_cell = cells.get("rate_10y")
    curve = cells.get("curve")
    equity = cells.get("equity_index")
    policy = board.policy_series(market)
    cpi = (
        real.series[1]
        if real is not None
        and real.label is not None
        and real.label.startswith(POLICY_FIRST_PREFIX)
        else None
    )
    slope_short = curve.series[1] if curve is not None and curve.label is not None else None
    cot = (
        equity.series[0]
        if equity is not None and equity.label == POSITIONING_LABEL and equity.series
        else None
    )
    return MarketSpec(
        market=market,
        currency=(
            grid_market.currency
            if grid_market is not None
            else board.regions[market].currency or NO_CURRENCY
        ),
        native_index=None if grid_market is None else grid_market.native_index,
        fx=fx.series[0] if fx is not None and fx.series else None,
        fx_gap=_gap(fx, "FX series against the base currency", f"{market}.fx_vs_base"),
        policy=policy,
        policy_gap=_gap(real, "policy rate (nor does its curve)", f"{market}.real_rate"),
        cpi=cpi,
        cpi_is_yoy=real is not None and real.label == CPI_RATE_LABEL,
        cpi_gap=_gap(real, "CPI", f"{market}.real_rate"),
        long_rate=long_cell.series[0] if long_cell is not None and long_cell.series else None,
        long_gap=_gap(long_cell, "ten-year yield", f"{market}.rate_10y"),
        slope_short=slope_short,
        slope_label=curve.label if curve is not None else None,
        slope_gap=_gap(curve, "curve", f"{market}.curve"),
        cot=cot,
        cot_gap=_gap(equity, "CFTC contract", f"{market}.equity_index"),
    )


# ── FX and inflation ──


@dataclass(frozen=True, slots=True)
class FxRatio:
    """``fx_start / fx_end``: the base-currency contribution of the local currency's move."""

    ratio: float | None
    problem: str | None


def fx_ratio(
    reader: MacroReader, spec: MarketSpec, *, horizon_days: int, base_currency: str
) -> FxRatio:
    """The FX leg of a base-currency return over ``horizon_days`` calendar days.

    The rates are quoted as units of local currency per unit of the base, so a rising series is
    the base strengthening and a local asset is worth ``local / fx`` in the base: the base return
    is ``(1 + local) * (fx_start / fx_end) - 1``. Written once, here, because the ratio upside
    down inverts every reading in the grid. 1.0 for the base currency itself; ``None``, never a
    silent 1.0, for a currency whose rate is missing, and refused when the series' declared units
    do not say "local per base".
    """
    if spec.currency == base_currency:
        return FxRatio(1.0, None)
    if spec.fx is None:
        return FxRatio(None, spec.fx_gap)
    expected = f"{spec.currency} per {base_currency}"
    units = reader.units(spec.fx)
    if units != expected:
        return FxRatio(
            None,
            f"{spec.fx} is declared in {units!r}, not {expected!r}; a ratio read the other way "
            "up would invert the base-currency return",
        )
    end = reader.value_at(spec.fx, reader.as_of)
    start = reader.value_at(spec.fx, reader.as_of - timedelta(days=horizon_days))
    if end is None or start is None or end <= 0 or start <= 0:
        return FxRatio(None, f"{reader.absence(spec.fx)} at one end of the window or both")
    return FxRatio(start / end, None)


@dataclass(frozen=True, slots=True)
class CpiReading:
    """Local inflation, year on year, as a decimal, with the release date of its print."""

    value: float | None
    released: date | None
    problem: str | None
    rebased: bool = False


def cpi_yoy(reader: MacroReader, spec: MarketSpec, *, days_per_year: float) -> CpiReading:
    """A year-on-year rate as published, or an index's ratio against the print a year earlier.

    An index ratio across a rebase divides one unit by another and reports the result as
    inflation; two readings carrying different declared units are refused.
    """
    if spec.cpi is None:
        return CpiReading(None, None, spec.cpi_gap)
    series = reader.series(spec.cpi)
    units = reader.units(spec.cpi) or ""
    latest = None if series is None else known_at(series, reader.as_of)
    if series is None or latest is None:
        return CpiReading(None, None, reader.absence(spec.cpi))
    if spec.cpi_is_yoy:
        return CpiReading(as_decimal(latest, units), latest.release_date, None)
    year_ago = known_at(series, reader.as_of - timedelta(days=round(days_per_year)))
    if year_ago is None or year_ago.value <= 0:
        return CpiReading(
            None, None, f"{spec.cpi} holds no index print released a year before {reader.as_of}"
        )
    if latest.units is not None and year_ago.units is not None and latest.units != year_ago.units:
        return CpiReading(
            None,
            None,
            f"{spec.cpi} measures {latest.units} at {latest.reference_date} and "
            f"{year_ago.units} a year earlier; the publisher rebased the index, so a ratio of "
            "the two would divide one unit by another. Re-collect it on one base",
            rebased=True,
        )
    return CpiReading(latest.value / year_ago.value - 1.0, latest.release_date, None)


# ── positioning ──


def positioning(
    store: Store, settings: ObliskConfig, contract_id: str, *, as_of: date
) -> PositioningReading:
    """A CFTC contract's crowding as known on ``as_of`` (Session 10's reading, released reports
    only)."""
    config = settings.sources.positioning
    code = config.contracts[contract_id].code
    reports = sources_repo.cot_reports(store, code, as_of=as_of)
    return positioning_reading(contract_id, reports, as_of=as_of, config=config)


# ── the option chain, read once per cell ──


@dataclass(slots=True)
class ChainSmile:
    """The hygiene-filtered smiles of one underlying's newest chain on or before ``as_of``,
    built at most once each. The same filters the risk engine reads (``quant.smile``), so the
    grid and the risk engine cannot disagree about one instrument on one day."""

    store: Store
    conid: int | None
    as_of: date
    spot: float | None
    filters: SmileFilters
    session: date | None = None
    _rows: tuple[ChainRow, ...] | None = None
    _smiles: dict[date, Smile | None] = field(default_factory=dict)

    def rows(self) -> tuple[ChainRow, ...]:
        if self._rows is None:
            self._rows = ()
            if self.conid is not None:
                self.session = repos.latest_chain_date(
                    self.store, as_of=self.as_of, underlying_conid=self.conid
                )
                if self.session is not None:
                    self._rows = repos.chain_rows(
                        self.store,
                        as_of=self.as_of,
                        underlying_conid=self.conid,
                        snapshot_date=self.session,
                    )
        return self._rows

    def expiries(self) -> list[date]:
        """Listed expiries still ahead of ``as_of``: a past expiry implies nothing."""
        return sorted({row.expiry for row in self.rows() if row.expiry > self.as_of})

    def at_expiry(self, expiry: date) -> Smile | None:
        if expiry not in self._smiles:
            rows = self.rows()
            self._smiles[expiry] = (
                None
                if not rows or not self.spot or self.conid is None
                else Smile.from_rows(
                    rows,
                    spot=self.spot,
                    underlying_conid=self.conid,
                    expiry=expiry,
                    filters=self.filters,
                )
            )
        return self._smiles[expiry]

    def matching(self, tenor_days: int) -> date | None:
        """The listed expiry closest to ``tenor_days`` out: an implied vol compared with a
        realised one over the same tenor, never the front of the curve."""
        ahead = self.expiries()
        if not ahead:
            return None
        return min(ahead, key=lambda expiry: abs((expiry - self.as_of).days - tenor_days))
