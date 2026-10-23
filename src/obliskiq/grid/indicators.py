"""The fifteen indicator families, one function each, over one market x sector cell.

Ported from lite's ``compute/indicators.py``. Two rules run through every function:

* **A leaf is computed or it says why not.** Never a silent proxy, never an estimate presented as
  data. The reason names the missing input: which series, how many observations, how many were
  needed.
* **A short sample is a missing leaf, not a weak one.** A fixed horizon needs its full window
  (``return_63d`` from three bars is correctly labelled and completely wrong; year to date is the
  one exception), a percentile its floor, and a percentile of rolling readings counts the
  independent readings, not the overlapping ones.

Every family emits the same leaves in every branch, the ones that measure nothing included: a
grid whose leaf set changes with its status is a grid whose rows cannot be joined across days.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass, MarketDataStatus
from ..domain.sources import PositioningStatus
from ..quant.smile import Smile, SmileFilters, SmileQuote
from ..quant.stats import (
    correlation,
    drawdown_from_peak,
    max_drawdown,
    mean,
    norm_deviation,
    paired_log_returns,
    percentile_of,
    rolling_correlations,
    simple_return,
)
from ..quant.stats import (
    distance_from_low as distance_above_low,
)
from ..store import repos
from ..store.db import Store
from . import estimators
from .cells import Cell
from .frames import (
    BarFrame,
    ChainSmile,
    MacroReader,
    MarketSpec,
    bar_frame,
    cpi_yoy,
    fx_ratio,
    positioning,
    worst_status,
)
from .vocabulary import Family, Reason, Units

#: Return horizons in trading sessions: definitions from SKILL.md's indicator list, not tunable
#: windows (changing them changes what the grid means).
RETURN_HORIZONS: Final = (("5d", 5), ("21d", 21), ("63d", 63), ("12m", 252))
#: The 52-week range is a market convention, not a configured window.
WEEKS_PER_YEAR: Final = 52
#: Two endpoints, the arithmetic minimum for a return or a slope.
MIN_ENDPOINTS: Final = 2
#: A correlation window must be at least half populated before it is computed.
HALF: Final = 2
#: The family-7 leaves, in every branch.
IMPLIED_VOL_LEAVES: Final = (
    "implied_vol_atm",
    "implied_vol_skew",
    "implied_vol_atm_front",
    "implied_vol_atm_back",
    "implied_vol_term_slope",
)
TERM_LEAVES: Final = IMPLIED_VOL_LEAVES[2:]
POSITIONING_LEVEL: Final = "positioning_percentile"
#: The method label of a solved implied vol (``quant.iv_solve``): a European model on what may be
#: an American option, and the reason every provenance says so.
SOLVED_PROVENANCE: Final = (
    "Black-Scholes-Merton prices a European option and this underlying may be American-style, so "
    "early exercise is unmodelled"
)


def positioning_change_leaf(weeks: int) -> str:
    """The positioning change leaf: one name for the family and the views that carry it."""
    return f"positioning_change_{weeks}w"


def window_label(intended: str, count: int, noun: str = "bars") -> str:
    """The window asked for and the sample actually behind it."""
    return f"{intended} ({count} {noun})"


@dataclass(frozen=True, slots=True)
class GridParams:
    """The grid's windows and floors, bound from configuration once per build."""

    daily_window_years: int
    weekly_window_years: int
    rv_tenors: tuple[int, ...]
    correlation_windows: tuple[int, ...]
    technical_window_days: int
    trading_days_per_year: float
    calendar_days_per_year: float
    reference_tenor: int
    atm_iv_prefer: str
    iv_self_min_sessions: int
    iv_percentile_window_weeks: int
    garman_klass_max_restated_open_share: float
    correlation_norm_window_years: int
    correlation_norm_min_independent: int
    rv_percentile_min_independent: int
    min_returns: int
    min_correlation: int
    min_percentile: int
    min_turnover: int
    macro_freshness_days: int
    smile_filters: SmileFilters
    skew_delta: float
    term_front_target: int
    term_back_target: int
    term_max_deviation: int
    term_min_separation: int
    base_currency: str
    positioning_velocity_weeks: int

    @classmethod
    def from_settings(cls, settings: ObliskConfig) -> GridParams:
        grid = settings.grid
        floors = grid.min_observations
        terms = settings.smile.term_structure
        return cls(
            daily_window_years=settings.collectors.backfill.window_years,
            weekly_window_years=grid.weekly_window_years,
            rv_tenors=tuple(grid.rv_tenors_days),
            correlation_windows=tuple(grid.correlation_windows_days),
            technical_window_days=grid.technical_window_days,
            trading_days_per_year=settings.collectors.vol.trading_days_per_year,
            calendar_days_per_year=settings.numerics.bsm.day_count_basis,
            reference_tenor=settings.collectors.vol.primary_tenor_days,
            atm_iv_prefer=grid.atm_iv_prefer,
            iv_self_min_sessions=grid.iv_self_source_min_sessions,
            iv_percentile_window_weeks=grid.iv_percentile_window_weeks,
            garman_klass_max_restated_open_share=grid.garman_klass_max_restated_open_share,
            correlation_norm_window_years=grid.correlation_norm_window_years,
            correlation_norm_min_independent=grid.correlation_norm_min_independent,
            rv_percentile_min_independent=grid.realised_vol_percentile_min_independent,
            min_returns=floors.returns,
            min_correlation=floors.correlation,
            min_percentile=floors.percentile,
            min_turnover=floors.turnover,
            macro_freshness_days=grid.freshness_days.macro,
            smile_filters=SmileFilters.from_config(settings),
            skew_delta=settings.smile.skew_delta,
            term_front_target=terms.front_target_dte,
            term_back_target=terms.back_target_dte,
            term_max_deviation=terms.max_target_deviation_days,
            term_min_separation=terms.min_separation_days,
            base_currency=settings.account.base_currency,
            positioning_velocity_weeks=settings.sources.positioning.velocity_weeks,
        )

    def years(self, count: int) -> int:
        """``count`` years in calendar days."""
        return round(count * self.calendar_days_per_year)

    def sessions_to_days(self, sessions: int) -> int:
        """Trading sessions as calendar days."""
        return round(sessions * self.calendar_days_per_year / self.trading_days_per_year)


@dataclass(frozen=True, slots=True)
class CellInstrument:
    """The instrument measuring a cell, as the universe declares it and the store resolves it."""

    symbol: str
    conid: int
    asset_class: AssetClass
    #: Why no option chain is reachable, as declared; ``None`` when one is (a missing chain is
    #: then something a collector still owes).
    options_unavailable_reason: str | None


@dataclass(slots=True)
class CellInputs:
    """Everything the fifteen families read for one cell, each series loaded once."""

    store: Store
    settings: ObliskConfig
    params: GridParams
    reader: MacroReader
    as_of: date
    market: str
    sector: str
    spec: MarketSpec
    instrument: CellInstrument | None
    benchmark: int | None
    benchmark_market: str
    _daily: BarFrame | None = None
    _benchmark_daily: BarFrame | None = None
    _chain: ChainSmile | None = None
    _fx_cache: dict[int, tuple[float | None, str | None]] = field(default_factory=dict)

    @property
    def conid(self) -> int | None:
        return None if self.instrument is None else self.instrument.conid

    @property
    def name(self) -> str:
        return self.instrument.symbol if self.instrument is not None else self.market

    @property
    def daily(self) -> BarFrame:
        """The instrument's bars over the weekly window: the daily families slice the daily
        window off it, the range families derive weekly closes from all of it."""
        if self._daily is None:
            since = self.as_of - timedelta(days=self.params.years(self.params.weekly_window_years))
            self._daily = bar_frame(self.store, self.conid, as_of=self.as_of, since=since)
        return self._daily

    @property
    def window_daily(self) -> BarFrame:
        """The bars inside the daily window (what the collectors' backfill holds)."""
        since = self.as_of - timedelta(days=self.params.years(self.params.daily_window_years))
        return self.daily.since(since)

    @property
    def benchmark_daily(self) -> BarFrame:
        if self._benchmark_daily is None:
            since = self.as_of - timedelta(days=self.params.years(self.params.weekly_window_years))
            self._benchmark_daily = bar_frame(
                self.store, self.benchmark, as_of=self.as_of, since=since
            )
        return self._benchmark_daily

    @property
    def chain(self) -> ChainSmile:
        if self._chain is None:
            self._chain = ChainSmile(
                store=self.store,
                conid=self.conid,
                as_of=self.as_of,
                spot=self.daily.last_close,
                filters=self.params.smile_filters,
            )
        return self._chain

    def fx(self, horizon_days: int) -> tuple[float | None, str | None]:
        if horizon_days not in self._fx_cache:
            result = fx_ratio(
                self.reader,
                self.spec,
                horizon_days=horizon_days,
                base_currency=self.params.base_currency,
            )
            self._fx_cache[horizon_days] = (result.ratio, result.problem)
        return self._fx_cache[horizon_days]

    def options_unavailable(self) -> str | None:
        return None if self.instrument is None else self.instrument.options_unavailable_reason


# ─────────────────────────  1. returns  ─────────────────────────


def returns(inputs: CellInputs) -> list[Cell]:
    """5d, 21d, 63d, YTD and 12m, in local AND base currency: local is what the market did, base
    what the account earned, and a market up sixty percent locally can be down in euros."""
    closes = list(inputs.window_daily.close)
    cells: list[Cell] = []
    for label, sessions in RETURN_HORIZONS:
        window = closes[-(sessions + 1) :] if len(closes) > sessions else closes
        cells += _return_pair(inputs, label, window, sessions, required=sessions + 1)
    ytd = _year_to_date(inputs.window_daily, inputs.as_of)
    cells += _return_pair(
        inputs, "ytd", ytd, max(len(ytd) - 1, 0), required=max(inputs.params.min_returns, 2)
    )
    return cells


def _return_pair(
    inputs: CellInputs, label: str, window: list[float], sessions: int, *, required: int
) -> list[Cell]:
    local_leaf, base_leaf = f"return_{label}_local", f"return_{label}_base"
    text = f"{label} ({len(window)} bars)"
    needed = max(required, inputs.params.min_returns, MIN_ENDPOINTS)
    if len(window) < needed:
        reason = (
            f"only {len(window)} bar(s) inside the {label} window; a {label} return needs "
            f"{needed}. Backfill the history rather than reading a shorter move under a longer "
            "label"
        )
        return [
            Cell.absent(local_leaf, Family.RETURNS, reason, code=Reason.SHORT_SAMPLE, window=text),
            Cell.absent(base_leaf, Family.RETURNS, reason, code=Reason.SHORT_SAMPLE, window=text),
        ]
    local = simple_return(window[0], window[-1])
    if local is None:
        reason = f"the {label} window opens on a close that is not positive"
        return [
            Cell.absent(local_leaf, Family.RETURNS, reason, code=Reason.SHORT_SAMPLE, window=text),
            Cell.absent(base_leaf, Family.RETURNS, reason, code=Reason.SHORT_SAMPLE, window=text),
        ]
    cells = [
        Cell.measured(
            local_leaf, Family.RETURNS, local, units=Units.DECIMAL, window=text, n_obs=sessions
        )
    ]
    ratio, problem = inputs.fx(inputs.params.sessions_to_days(sessions))
    if ratio is None:
        cells.append(
            Cell.absent(
                base_leaf,
                Family.RETURNS,
                f"{problem}, so the base-currency leg of the return cannot be computed; a local "
                "figure is not a substitute, it embeds no currency at all",
                code=Reason.NO_FX,
                window=text,
            )
        )
    else:
        cells.append(
            Cell.measured(
                base_leaf,
                Family.RETURNS,
                (1.0 + local) * ratio - 1.0,
                units=Units.DECIMAL,
                window=text,
                n_obs=sessions,
            )
        )
    return cells


def _year_to_date(bars: BarFrame, as_of: date) -> list[float]:
    """Closes from the last session of the previous year on: year to date measures from the close
    the year started at."""
    boundary = date(as_of.year, 1, 1)
    for index, day in enumerate(bars.dates):
        if day >= boundary:
            return list(bars.close[max(index - 1, 0) :])
    return []


# ─────────────────────────  2-4. range position  ─────────────────────────


def _weekly(inputs: CellInputs) -> list[float]:
    return inputs.daily.weekly_closes()


def _range_cell(
    inputs: CellInputs,
    leaf: str,
    family: Family,
    values: list[float],
    window: str,
    compute: Callable[[list[float]], float | None],
) -> Cell:
    floor = inputs.params.min_percentile
    if len(values) < floor:
        return Cell.absent(
            leaf,
            family,
            f"only {len(values)} weekly close(s) in the {window} window, below the {floor} "
            "needed for a range position",
            code=Reason.SHORT_SAMPLE,
            window=window,
        )
    value = compute(values)
    if value is None:
        return Cell.absent(
            leaf,
            family,
            "the window holds no usable closes",
            code=Reason.SHORT_SAMPLE,
            window=window,
        )
    return Cell.measured(leaf, family, value, units=Units.DECIMAL, window=window, n_obs=len(values))


def distance_from_high(inputs: CellInputs) -> list[Cell]:
    """Distance below the 52-week high, and below the long-run high."""
    weekly = _weekly(inputs)
    year = weekly[-WEEKS_PER_YEAR:]
    long_run = f"{inputs.params.weekly_window_years}y weekly"
    return [
        _range_cell(
            inputs,
            "distance_from_52w_high",
            Family.DISTANCE_FROM_HIGH,
            year,
            window_label(f"{WEEKS_PER_YEAR}w", len(year)),
            drawdown_from_peak,
        ),
        _range_cell(
            inputs,
            "distance_from_long_run_high",
            Family.DISTANCE_FROM_HIGH,
            weekly,
            window_label(long_run, len(weekly)),
            drawdown_from_peak,
        ),
    ]


def distance_from_low(inputs: CellInputs) -> list[Cell]:
    """Distance above the 52-week low, part of the coiled definition."""
    year = _weekly(inputs)[-WEEKS_PER_YEAR:]
    return [
        _range_cell(
            inputs,
            "distance_from_52w_low",
            Family.DISTANCE_FROM_LOW,
            year,
            window_label(f"{WEEKS_PER_YEAR}w", len(year)),
            distance_above_low,
        )
    ]


def drawdown(inputs: CellInputs) -> list[Cell]:
    """The worst peak-to-trough fall inside the long-run window, wherever it happened: a market
    fully recovered from a 40 % collapse reads zero in family 2 and -0.40 here."""
    weekly = _weekly(inputs)
    return [
        _range_cell(
            inputs,
            "max_drawdown_long_run",
            Family.DRAWDOWN,
            weekly,
            window_label(f"{inputs.params.weekly_window_years}y weekly", len(weekly)),
            max_drawdown,
        )
    ]


# ─────────────────────────  5-6. realised volatility  ─────────────────────────


def _too_few_independent(found: int, floor: int, tenor: int) -> str:
    return (
        f"only {found} non-overlapping {tenor}-day reading(s) fit in the daily window, below the "
        f"{floor} floor in grid.realised_vol_percentile_min_independent. Readings sampled every "
        "session share all but one of their bars, so counting them would report roughly "
        f"{tenor} times the evidence behind the number; the percentile is absent rather than "
        "ranked against them"
    )


def _absent_pair(
    inputs: CellInputs, leaf: str, tenor: int, reason: str, code: Reason
) -> list[Cell]:
    """A level and its percentile, absent together: a percentile of an unmeasured level is the
    same absence twice."""
    return [
        Cell.absent(leaf, Family.REALISED_VOL, reason, code=code, window=f"{tenor}d"),
        Cell.absent(
            f"{leaf}_percentile",
            Family.REALISED_VOL,
            reason,
            code=code,
            window=f"{inputs.params.daily_window_years}y window of {tenor}d rolling vol",
        ),
    ]


def _percentile_leaf(
    inputs: CellInputs, leaf: str, tenor: int, level: float, history: list[float]
) -> Cell:
    floor = inputs.params.rv_percentile_min_independent
    window = f"{inputs.params.daily_window_years}y window of {tenor}d rolling vol"
    if len(history) < floor:
        return Cell.absent(
            leaf,
            Family.REALISED_VOL,
            _too_few_independent(len(history), floor, tenor),
            code=Reason.SHORT_SAMPLE,
            window=window,
        )
    value = percentile_of(history, level)
    if value is None:
        return Cell.absent(
            leaf, Family.REALISED_VOL, "no usable reading to rank against", code=Reason.SHORT_SAMPLE
        )
    return Cell.measured(
        leaf,
        Family.REALISED_VOL,
        value,
        units=Units.PERCENTILE,
        window=window_label(window, len(history), "readings"),
        n_obs=len(history),
    )


def realised_vol(inputs: CellInputs) -> list[Cell]:
    """Realised vol at the reference tenor and its percentile, on three estimators: close-to-
    close, Parkinson and Garman-Klass, each its own named leaf with its own percentile. The range
    estimators read the intraday range and see no overnight gap, so they sit below close-to-
    close on any market that closes at night; each ranks only against its own history."""
    params = inputs.params
    tenor = params.reference_tenor
    bars = inputs.window_daily
    basis = params.trading_days_per_year
    leaf = f"realised_vol_{tenor}d"
    level = estimators.close_to_close(bars.close, tenor, trading_days_per_year=basis)
    if level is None:
        cells = _absent_pair(
            inputs,
            leaf,
            tenor,
            f"fewer than {tenor + 1} usable daily closes, so a {tenor}-day realised vol cannot be "
            "measured",
            Reason.SHORT_SAMPLE,
        )
    else:
        history = estimators.rolling_close_to_close(bars.close, tenor, trading_days_per_year=basis)
        cells = [
            Cell.measured(
                leaf,
                Family.REALISED_VOL,
                level,
                units=Units.DECIMAL,
                window=f"{tenor}d",
                n_obs=tenor,
            ),
            _percentile_leaf(inputs, f"{leaf}_percentile", tenor, level, history),
        ]
    return cells + _range_leaves(inputs, bars, tenor)


def _range_leaves(inputs: CellInputs, bars: BarFrame, tenor: int) -> list[Cell]:
    basis = inputs.params.trading_days_per_year
    ranges = estimators.range_bars(bars.high, bars.low)
    cells = _estimator_leaves(
        inputs,
        f"parkinson_vol_{tenor}d",
        tenor,
        ranges,
        lambda window: estimators.parkinson(window, trading_days_per_year=basis),
        "bar(s) carrying a usable high and low",
    )
    ohlc = estimators.ohlc_bars(bars.open, bars.high, bars.low, bars.close)
    share = estimators.restated_open_share(ohlc[-tenor:]) if len(ohlc) >= tenor else None
    leaf = f"garman_klass_vol_{tenor}d"
    if share is not None and share > inputs.params.garman_klass_max_restated_open_share:
        return cells + _absent_pair(
            inputs,
            leaf,
            tenor,
            f"{share:.0%} of the last {tenor} daily bars open at exactly the previous close, "
            "above grid.garman_klass_max_restated_open_share: this listing restates the close "
            "instead of publishing an opening print, so Garman-Klass would compute a different "
            "formula, not a worse estimate. Parkinson needs no open and is computed beside it",
            Reason.OPEN_RESTATED,
        )
    return cells + _estimator_leaves(
        inputs,
        leaf,
        tenor,
        ohlc,
        lambda window: estimators.garman_klass(window, trading_days_per_year=basis),
        "bar(s) carrying a usable open, high, low and close",
    )


def _estimator_leaves[T](
    inputs: CellInputs,
    leaf: str,
    tenor: int,
    bars: Sequence[T],
    measure: Callable[[Sequence[T]], float | None],
    noun: str,
) -> list[Cell]:
    """One range estimator's level and its percentile, ranked against its own history."""
    if len(bars) < tenor:
        return _absent_pair(
            inputs,
            leaf,
            tenor,
            f"only {len(bars)} daily {noun}, below the {tenor} a {tenor}-session range estimate "
            "needs",
            Reason.NO_BAR_RANGE,
        )
    level = measure(bars[-tenor:])
    if level is None:
        return _absent_pair(
            inputs,
            leaf,
            tenor,
            f"the last {tenor} daily {noun} carry no usable range",
            Reason.NO_DISPERSION,
        )
    history = estimators.rolling(bars, tenor, measure, step=tenor)
    return [
        Cell.measured(
            leaf, Family.REALISED_VOL, level, units=Units.DECIMAL, window=f"{tenor}d", n_obs=tenor
        ),
        _percentile_leaf(inputs, f"{leaf}_percentile", tenor, level, history),
    ]


def rv_term_structure(inputs: CellInputs) -> list[Cell]:
    """Realised vol at every configured tenor, and the slope between the ends (short over long
    minus one; positive is an inverted term structure)."""
    basis = inputs.params.trading_days_per_year
    closes = inputs.window_daily.close
    cells: list[Cell] = []
    levels: dict[int, float] = {}
    for tenor in inputs.params.rv_tenors:
        value = estimators.close_to_close(closes, tenor, trading_days_per_year=basis)
        if value is None:
            cells.append(
                Cell.absent(
                    f"rv_{tenor}d",
                    Family.RV_TERM_STRUCTURE,
                    f"fewer than {tenor + 1} usable daily closes",
                    code=Reason.SHORT_SAMPLE,
                    window=f"{tenor}d",
                )
            )
            continue
        levels[tenor] = value
        cells.append(
            Cell.measured(
                f"rv_{tenor}d",
                Family.RV_TERM_STRUCTURE,
                value,
                units=Units.DECIMAL,
                window=f"{tenor}d",
                n_obs=tenor,
            )
        )
    tenors = sorted(levels)
    window = f"{tenors[0]}d vs {tenors[-1]}d" if len(tenors) >= MIN_ENDPOINTS else "unavailable"
    if len(tenors) < MIN_ENDPOINTS:
        cells.append(
            Cell.absent(
                "rv_term_slope",
                Family.RV_TERM_STRUCTURE,
                "at least two tenors are needed before a term structure has a slope",
                code=Reason.SHORT_SAMPLE,
                window=window,
            )
        )
    else:
        cells.append(
            Cell.measured(
                "rv_term_slope",
                Family.RV_TERM_STRUCTURE,
                levels[tenors[0]] / levels[tenors[-1]] - 1.0,
                units=Units.RATIO,
                window=window,
            )
        )
    return cells


# ─────────────────────────  7-9. implied volatility  ─────────────────────────


def _no_options_reason(inputs: CellInputs, family: str) -> str:
    declared = (inputs.options_unavailable() or "").rstrip(". ")
    return (
        f"{family} needs an option chain, and the universe declares none reachable for "
        f"{inputs.name}: {declared}. A structural gap, not a collection one"
    )


@dataclass(frozen=True, slots=True)
class AtmReading:
    value: float | None
    provenance: str
    status: MarketDataStatus | None


def _chain_atm(inputs: CellInputs) -> tuple[AtmReading | None, str]:
    """The ATM quote off the exchange-quoted smile at the expiry matched to the reference tenor,
    and why there is none if there is not."""
    chain = inputs.chain
    expiry = chain.matching(inputs.params.reference_tenor)
    if expiry is None or not chain.rows():
        return None, ""
    smile = chain.at_expiry(expiry)
    if smile is None:
        return None, (
            f" The {chain.session} chain has rows but none survived smile hygiene (spread, book "
            "depth, delta band, convexity)."
        )
    quote = smile.atm()
    return (
        AtmReading(
            quote.iv,
            f"ATM on the {chain.session} chain at the {expiry} expiry "
            f"({(expiry - inputs.as_of).days}d, matched to the {inputs.params.reference_tenor}d "
            f"realised tenor), {quote.source}; {smile.hygiene.summary()}",
            worst_status(smile.market_data_statuses),
        ),
        "",
    )


def atm_implied_vol(inputs: CellInputs) -> AtmReading:
    """At-the-money implied vol from whichever source ``grid.atm_iv_prefer`` ranks first: the
    chain smile, the vendor's figure on the underlying snapshot, or the session's solve. They are
    not equivalent (an inversion of a mid price is not a quote), so the order is declared."""
    if inputs.conid is None:
        return AtmReading(None, "no instrument is mapped to this cell", None)
    snapshots = repos.snapshots(inputs.store, inputs.conid, as_of=inputs.as_of, limit=1)
    latest = snapshots[-1] if snapshots else None
    note = ""

    def chain() -> AtmReading | None:
        nonlocal note
        found, note = _chain_atm(inputs)
        return found

    def vendor() -> AtmReading | None:
        if latest is None or latest.implied_vol is None:
            return None
        return AtmReading(
            latest.implied_vol,
            f"vendor implied vol from the {latest.snapshot_date} snapshot",
            latest.market_data_status,
        )

    def solved() -> AtmReading | None:
        if latest is None or latest.implied_vol_solved is None:
            return None
        return AtmReading(
            latest.implied_vol_solved,
            f"implied vol SOLVED from mid prices on the {latest.snapshot_date} chain "
            f"({latest.implied_vol_solved_source}); {SOLVED_PROVENANCE}",
            latest.market_data_status,
        )

    readers: dict[str, tuple[Callable[[], AtmReading | None], ...]] = {
        "solved": (solved, chain, vendor),
        "chain_first": (chain, solved, vendor),
        "vendor": (chain, vendor, solved),
    }
    for read in readers[inputs.params.atm_iv_prefer]:
        found = read()
        if found is not None:
            return found
    collected = len(inputs.chain.rows())
    if collected:
        return AtmReading(
            None,
            f"{inputs.name} has a collected chain ({collected} contracts on the newest session) "
            "but no usable implied vol: no contract carried one, no vendor figure was served and "
            f"none was solved. The chain is reachable; what is missing is a priced session.{note}",
            None,
        )
    return AtmReading(
        None,
        f"no option chain, no vendor implied vol and no solved implied vol for {inputs.name}; "
        f"family 7 exists only where a chain does.{note}",
        None,
    )


def implied_vol(inputs: CellInputs) -> list[Cell]:
    """The level of implied vol (ATM), its direction across strikes (the risk reversal) and its
    shape across time (front and back ATM and the slope between them)."""
    if inputs.options_unavailable():
        reason = _no_options_reason(inputs, "Family 7")
        return [
            Cell.inapplicable(leaf, Family.IMPLIED_VOL, reason, code=Reason.NO_OPTIONS)
            for leaf in IMPLIED_VOL_LEAVES
        ]
    atm = atm_implied_vol(inputs)
    cells = [
        Cell.absent("implied_vol_atm", Family.IMPLIED_VOL, atm.provenance, code=Reason.NO_CHAIN)
        if atm.value is None
        else Cell.measured(
            "implied_vol_atm",
            Family.IMPLIED_VOL,
            atm.value,
            units=Units.DECIMAL,
            window=atm.provenance,
            market_data_status=atm.status,
        )
    ]
    cells.append(_skew(inputs))
    return cells + _term_structure(inputs)


def _skew(inputs: CellInputs) -> Cell:
    """The risk reversal at ``smile.skew_delta``, off the hygiene-filtered smile, refusing
    (with the deltas each side reached) where the band does not bracket the target: the
    nearest listed strike would publish a 45-delta reading under a 25-delta name."""
    target = inputs.params.skew_delta
    window = f"{target:g}-delta risk reversal"
    chain = inputs.chain
    expiry = chain.matching(inputs.params.reference_tenor)
    smile = None if expiry is None else chain.at_expiry(expiry)
    if smile is None:
        return Cell.absent(
            "implied_vol_skew",
            Family.IMPLIED_VOL,
            f"no usable option chain for {inputs.name}, so there is no smile to read a skew off; "
            "unlike the ATM level this leaf has no vendor or solved fallback, a risk reversal is "
            "a statement about two strikes and only the chain carries them",
            code=Reason.NO_CHAIN,
            window=window,
        )
    quote = smile.risk_reversal(target)
    if quote is None:
        return Cell.absent(
            "implied_vol_skew",
            Family.IMPLIED_VOL,
            f"the collected band does not reach |delta| {target:g} on both sides of the "
            f"{chain.session} chain at {expiry}, so no risk reversal can be read without "
            f"substituting a nearer strike: {smile.delta_reach().describe()}; "
            f"{smile.hygiene.summary()}",
            code=Reason.NO_CHAIN,
            window=window,
        )
    return Cell.measured(
        "implied_vol_skew",
        Family.IMPLIED_VOL,
        quote.value,
        units=Units.DECIMAL,
        window=f"{quote.provenance()} on the {chain.session} chain at {expiry}; "
        f"{smile.hygiene.summary()}",
        n_obs=quote.n_points,
        market_data_status=worst_status(smile.market_data_statuses),
    )


def _term_absent(reason: str, window: str) -> list[Cell]:
    return [
        Cell.absent(leaf, Family.IMPLIED_VOL, reason, code=Reason.NO_CHAIN, window=window)
        for leaf in TERM_LEAVES
    ]


def _leg(expiries: list[date], as_of: date, target: int, tolerance: int) -> tuple[date | None, int]:
    nearest = min(expiries, key=lambda expiry: abs((expiry - as_of).days - target))
    dte = (nearest - as_of).days
    return (None if abs(dte - target) > tolerance else nearest), dte


def _term_structure(inputs: CellInputs) -> list[Cell]:
    """Front versus back ATM implied vol, both off the exchange-quoted chain or no leaf at all
    (a slope with a solved end and a quoted end has a methodology break inside one number).
    Each leg is the nearest listed expiry to its target; the window is the two actual DTEs and
    nothing else, so a leaf's history compares only like with like."""
    params = inputs.params
    chain = inputs.chain
    asked = f"{params.term_front_target}d vs {params.term_back_target}d"
    expiries = chain.expiries()
    if not expiries:
        return _term_absent(
            f"no option chain with a listed expiry ahead of {inputs.as_of} for {inputs.name}, so "
            "there is no term structure to measure",
            "unavailable",
        )
    listed = ", ".join(f"{(expiry - inputs.as_of).days}d" for expiry in expiries)
    if len(expiries) < MIN_ENDPOINTS:
        return _term_absent(
            f"only one listed expiry ({listed}) is collected for {inputs.name}, so there is no "
            "second point to measure a slope to; a slope against itself is zero by construction",
            asked,
        )
    front, front_dte = _leg(
        expiries, inputs.as_of, params.term_front_target, params.term_max_deviation
    )
    back, back_dte = _leg(
        expiries, inputs.as_of, params.term_back_target, params.term_max_deviation
    )
    if front is None or back is None:
        missing, reached, target = (
            ("front", front_dte, params.term_front_target)
            if front is None
            else ("back", back_dte, params.term_back_target)
        )
        return _term_absent(
            f"the {missing} leg wants an expiry within {params.term_max_deviation}d of {target}d "
            f"and the nearest listed one is {reached}d out (collected: {listed}); taking it would "
            "publish a different tenor pair under this leaf's name",
            asked,
        )
    if front == back:
        return _term_absent(
            f"both legs resolve to the same listed expiry ({front_dte}d) (collected: {listed}); a "
            "slope against itself says nothing about the curve",
            asked,
        )
    window = f"{front_dte}d vs {back_dte}d"
    if back_dte - front_dte < params.term_min_separation:
        return _term_absent(
            f"the two legs are {back_dte - front_dte}d apart, below the "
            f"{params.term_min_separation}d floor in smile.term_structure.min_separation_days",
            window,
        )
    smiles: list[tuple[Smile, SmileQuote]] = []
    for which, expiry in (("front", front), ("back", back)):
        smile = chain.at_expiry(expiry)
        if smile is None:
            return _term_absent(
                f"the {which} leg at {expiry} yielded no ATM implied vol: no strike survived "
                "smile hygiene or none carried one. Both legs come off the chain; there is no "
                "vendor or solved fallback here",
                window,
            )
        smiles.append((smile, smile.atm()))
    (front_smile, front_quote), (back_smile, back_quote) = smiles
    provenance = (
        f"front ATM {front_quote.iv:.4f} at {front} ({front_dte}d, {front_quote.source}); back "
        f"ATM {back_quote.iv:.4f} at {back} ({back_dte}d, {back_quote.source}); both "
        f"exchange-quoted off the {chain.session} chain. front hygiene: "
        f"{front_smile.hygiene.summary()}; back hygiene: {back_smile.hygiene.summary()}"
    )
    # Summed, not merged: these are status -> count maps.
    status = worst_status(
        Counter(front_smile.market_data_statuses) + Counter(back_smile.market_data_statuses)
    )
    cells = [
        Cell.measured(
            "implied_vol_atm_front",
            Family.IMPLIED_VOL,
            front_quote.iv,
            units=Units.DECIMAL,
            window=window,
            reason=provenance,
            n_obs=len(front_smile),
            market_data_status=status,
        ),
        Cell.measured(
            "implied_vol_atm_back",
            Family.IMPLIED_VOL,
            back_quote.iv,
            units=Units.DECIMAL,
            window=window,
            reason=provenance,
            n_obs=len(back_smile),
            market_data_status=status,
        ),
    ]
    if back_quote.iv <= 0:
        cells.append(
            Cell.absent(
                "implied_vol_term_slope",
                Family.IMPLIED_VOL,
                f"the back leg's ATM implied vol is {back_quote.iv:.4f}, which cannot be a "
                "denominator; the two levels beside this leaf are still measured",
                code=Reason.NO_CHAIN,
                window=window,
            )
        )
    else:
        cells.append(
            Cell.measured(
                "implied_vol_term_slope",
                Family.IMPLIED_VOL,
                front_quote.iv / back_quote.iv - 1.0,
                units=Units.RATIO,
                window=window,
                reason=provenance,
                n_obs=len(front_smile) + len(back_smile),
                market_data_status=status,
            )
        )
    return cells


def iv_percentile(inputs: CellInputs) -> list[Cell]:
    """The snapshot implied vol ranked over its own trailing window, self-computed from
    accumulated snapshots once enough have accrued (DECISIONS D134: no route serves a vendor
    percentile)."""
    window = f"{inputs.params.iv_percentile_window_weeks}w"
    if inputs.options_unavailable():
        return [
            Cell.inapplicable(
                "iv_percentile",
                Family.IV_PERCENTILE,
                _no_options_reason(inputs, "Family 8"),
                code=Reason.NO_OPTIONS,
                window=window,
            )
        ]
    if inputs.conid is None:
        return [
            Cell.absent(
                "iv_percentile",
                Family.IV_PERCENTILE,
                "no instrument is mapped to this cell",
                code=Reason.NO_IV_PERCENTILE,
                window=window,
            )
        ]
    params = inputs.params
    accumulated = repos.implied_vol_snapshot_count(inputs.store, inputs.conid, as_of=inputs.as_of)
    if accumulated < params.iv_self_min_sessions:
        return [
            Cell.absent(
                "iv_percentile",
                Family.IV_PERCENTILE,
                f"{accumulated} of the {params.iv_self_min_sessions} snapshots carrying an "
                f"implied vol (grid.iv_self_source_min_sessions) had accumulated by "
                f"{inputs.as_of}; a self-computed percentile compounds from day one and is worth "
                "nothing before then",
                code=Reason.NO_IV_PERCENTILE,
                window=window,
            )
        ]
    sessions = round(
        params.iv_percentile_window_weeks * params.trading_days_per_year / WEEKS_PER_YEAR
    )
    rows = repos.snapshots(inputs.store, inputs.conid, as_of=inputs.as_of, limit=sessions)
    values = [row.implied_vol for row in rows if row.implied_vol is not None]
    if not values:
        return [
            Cell.absent(
                "iv_percentile",
                Family.IV_PERCENTILE,
                f"{accumulated} snapshots had accumulated by {inputs.as_of}, but none inside the "
                f"most recent {window} carries an implied vol",
                code=Reason.NO_IV_PERCENTILE,
                window=window,
            )
        ]
    rank = percentile_of(values, values[-1])
    if rank is None:
        return [
            Cell.absent(
                "iv_percentile",
                Family.IV_PERCENTILE,
                "no finite implied vol to rank",
                code=Reason.NO_IV_PERCENTILE,
                window=window,
            )
        ]
    return [
        Cell.measured(
            "iv_percentile",
            Family.IV_PERCENTILE,
            rank,
            units=Units.PERCENTILE,
            window=window_label(window, len(values), "snapshots"),
            n_obs=len(values),
            reason=f"self-computed over {len(values)} of {accumulated} accumulated snapshots",
            market_data_status=rows[-1].market_data_status,
        )
    ]


def iv_hv_spread(inputs: CellInputs) -> list[Cell]:
    """Implied minus close-to-close realised at the reference tenor. Close-to-close, because an
    implied vol prices the total variance to expiry, overnight gaps included."""
    tenor = inputs.params.reference_tenor
    window = f"IV vs {tenor}d RV"
    if inputs.options_unavailable():
        return [
            Cell.inapplicable(
                "iv_minus_hv",
                Family.IV_HV_SPREAD,
                _no_options_reason(inputs, "Family 9"),
                code=Reason.NO_OPTIONS,
                window=window,
            )
        ]
    atm = atm_implied_vol(inputs)
    realised = estimators.close_to_close(
        inputs.window_daily.close, tenor, trading_days_per_year=inputs.params.trading_days_per_year
    )
    if atm.value is None:
        return [
            Cell.absent(
                "iv_minus_hv",
                Family.IV_HV_SPREAD,
                atm.provenance,
                code=Reason.NO_CHAIN,
                window=window,
            )
        ]
    if realised is None:
        return [
            Cell.absent(
                "iv_minus_hv",
                Family.IV_HV_SPREAD,
                f"fewer than {tenor + 1} usable daily closes, so there is no realised vol to "
                "compare against",
                code=Reason.SHORT_SAMPLE,
                window=window,
            )
        ]
    return [
        Cell.measured(
            "iv_minus_hv",
            Family.IV_HV_SPREAD,
            atm.value - realised,
            units=Units.DECIMAL,
            window=window,
            market_data_status=atm.status,
        )
    ]


# ─────────────────────────  10. correlation  ─────────────────────────


def aligned_returns(left: BarFrame, right: BarFrame) -> tuple[list[float], list[float]]:
    """Log returns on the sessions both series traded: by calendar date, never by position (a
    Tokyo Monday must not pair with a New York Friday)."""
    other = dict(zip(right.dates, right.close, strict=True))
    own: list[float] = []
    theirs: list[float] = []
    for day, close in zip(left.dates, left.close, strict=True):
        match = other.get(day)
        if match is not None:
            own.append(close)
            theirs.append(match)
    return paired_log_returns(own, theirs)


def correlation_to_benchmark(inputs: CellInputs) -> list[Cell]:
    """Correlation to the global risk benchmark, and its shift from the pair's own norm on
    non-overlapping readings. Correlation change matters more than level."""
    params = inputs.params
    cells: list[Cell] = []
    is_benchmark = inputs.conid is not None and inputs.conid == inputs.benchmark
    if inputs.benchmark is None or is_benchmark:
        reason, code = (
            (
                "this cell is the benchmark, so a correlation to it carries no information",
                Reason.IS_BENCHMARK,
            )
            if is_benchmark
            else (
                f"the benchmark market {inputs.benchmark_market} has no instrument in the store",
                Reason.NO_BENCHMARK,
            )
        )
        for window in params.correlation_windows:
            label = f"{window}d"
            cells += [
                Cell.refused(
                    f"correlation_{label}", Family.CORRELATION, reason, code=code, window=label
                ),
                Cell.refused(
                    f"correlation_shift_{label}",
                    Family.CORRELATION,
                    reason,
                    code=code,
                    window=label,
                ),
            ]
        return cells
    own, other = aligned_returns(inputs.daily, inputs.benchmark_daily)
    for window in params.correlation_windows:
        label = f"{window}d"
        recent_own, recent_other = own[-window:], other[-window:]
        if len(recent_own) < max(params.min_correlation, window // HALF):
            reason = (
                f"only {len(recent_own)} overlapping return(s) against the benchmark in the "
                f"{label} window, below the {max(params.min_correlation, window // HALF)} needed"
            )
            cells += [
                Cell.absent(
                    f"correlation_{label}",
                    Family.CORRELATION,
                    reason,
                    code=Reason.SHORT_SAMPLE,
                    window=label,
                ),
                Cell.absent(
                    f"correlation_shift_{label}",
                    Family.CORRELATION,
                    reason,
                    code=Reason.SHORT_SAMPLE,
                    window=label,
                ),
            ]
            continue
        value = correlation(recent_own, recent_other)
        if value is None:
            reason = "one of the two series has no dispersion in this window"
            cells += [
                Cell.absent(
                    f"correlation_{label}",
                    Family.CORRELATION,
                    reason,
                    code=Reason.NO_DISPERSION,
                    window=label,
                ),
                Cell.absent(
                    f"correlation_shift_{label}",
                    Family.CORRELATION,
                    reason,
                    code=Reason.NO_DISPERSION,
                    window=label,
                ),
            ]
            continue
        cells.append(
            Cell.measured(
                f"correlation_{label}",
                Family.CORRELATION,
                value,
                units=Units.RATIO,
                window=window_label(label, len(recent_own), "overlapping returns"),
                n_obs=len(recent_own),
            )
        )
        norm_window = f"{label} vs its {params.correlation_norm_window_years}y norm"
        shift, independent, problem = correlation_shift(own, other, window, params)
        if shift is None:
            cells.append(
                Cell.absent(
                    f"correlation_shift_{label}",
                    Family.CORRELATION,
                    problem,
                    code=Reason.NO_DISPERSION,
                    window=norm_window,
                )
            )
        else:
            cells.append(
                Cell.measured(
                    f"correlation_shift_{label}",
                    Family.CORRELATION,
                    shift,
                    units=Units.SIGMA,
                    window=window_label(norm_window, independent, "independent readings"),
                    n_obs=independent,
                )
            )
    return cells


def correlation_shift(
    own: list[float], other: list[float], window: int, params: GridParams
) -> tuple[float | None, int, str]:
    """The current correlation against the pair's own norm, in standard deviations, from
    non-overlapping readings (overlapping ones give a standard deviation of their own
    smoothness and inflate every z-score). Returns the shift, the readings behind it, and why
    there is none."""
    sessions = round(params.correlation_norm_window_years * params.trading_days_per_year)
    history = rolling_correlations(own, other, window=window, sample=sessions, step=window)
    floor = params.correlation_norm_min_independent
    if len(history) < floor:
        return (
            None,
            len(history),
            (
                f"only {len(history)} independent {window}-day correlation(s) fit in the "
                f"{params.correlation_norm_window_years}y norm window, below the {floor} floor in "
                "grid.correlation_norm_min_independent; overlapping readings would give a standard "
                "deviation of their own smoothness, so the shift is absent rather than inflated"
            ),
        )
    deviation = norm_deviation(history)
    if deviation is None:
        return None, len(history), "the pair's own correlation history has no dispersion"
    return deviation.z_score, len(history), ""


# ─────────────────────────  11. currency  ─────────────────────────


def currency_return(inputs: CellInputs) -> list[Cell]:
    """The local currency against the base over twelve months (positive is the local currency
    appreciating): the leg that turns a local return into a base one."""
    label = "12m"
    spec = inputs.spec
    if spec.currency == inputs.params.base_currency or spec.fx is None:
        return [
            Cell.inapplicable(
                "currency_return_12m",
                Family.CURRENCY_RETURN,
                spec.fx_gap or f"{spec.market} is in the base currency",
                code=Reason.NO_FX_DECLARED,
                window=label,
            )
        ]
    ratio, problem = inputs.fx(round(inputs.params.calendar_days_per_year))
    if ratio is None:
        return [
            Cell.absent(
                "currency_return_12m",
                Family.CURRENCY_RETURN,
                problem or f"{spec.fx} is absent",
                code=Reason.NO_FX,
                window=label,
            )
        ]
    return [
        Cell.measured(
            "currency_return_12m",
            Family.CURRENCY_RETURN,
            ratio - 1.0,
            units=Units.DECIMAL,
            window=label,
        )
    ]


# ─────────────────────────  12-13. rates  ─────────────────────────


def policy_rate(inputs: CellInputs) -> list[Cell]:
    """The local policy rate, and the same rate net of local inflation; a real rate whose CPI
    print is older than the macro freshness window is stale, because the nominal side updates
    daily and would otherwise make it read as current."""
    spec, reader = inputs.spec, inputs.reader
    if spec.policy is None:
        reason = f"{spec.policy_gap}; a neighbouring country's rate is not a substitute"
        return [
            Cell.inapplicable(
                "policy_rate", Family.POLICY_RATE, reason, code=Reason.SERIES_UNDECLARED
            ),
            Cell.inapplicable(
                "policy_rate_real", Family.POLICY_RATE, reason, code=Reason.SERIES_UNDECLARED
            ),
        ]
    nominal = reader.value_at(spec.policy, inputs.as_of)
    # A real rate the board declares no CPI for is structural whatever the nominal side holds:
    # tested first, so a missing policy rate does not make it read as merely uncollected.
    undeclared = (
        None
        if spec.cpi is not None
        else Cell.inapplicable(
            "policy_rate_real",
            Family.POLICY_RATE,
            f"{spec.cpi_gap}; a neighbour's inflation rate is not this market's",
            code=Reason.SERIES_UNDECLARED,
        )
    )
    if nominal is None:
        reason = reader.absence(spec.policy)
        return [
            Cell.absent("policy_rate", Family.POLICY_RATE, reason, code=Reason.SERIES_ABSENT),
            undeclared
            or Cell.absent(
                "policy_rate_real", Family.POLICY_RATE, reason, code=Reason.SERIES_ABSENT
            ),
        ]
    cells = [
        Cell.measured(
            "policy_rate", Family.POLICY_RATE, nominal, units=Units.DECIMAL, window=spec.policy
        )
    ]
    if undeclared is not None:
        return [*cells, undeclared]
    cpi = cpi_yoy(reader, spec, days_per_year=inputs.params.calendar_days_per_year)
    if cpi.value is None or cpi.released is None:
        cells.append(
            Cell.absent(
                "policy_rate_real",
                Family.POLICY_RATE,
                cpi.problem or f"{spec.cpi} is absent",
                code=Reason.MIXED_UNITS if cpi.rebased else Reason.SERIES_ABSENT,
            )
        )
        return cells
    real = Cell.measured(
        "policy_rate_real",
        Family.POLICY_RATE,
        nominal - cpi.value,
        units=Units.DECIMAL,
        window=f"{spec.policy} less {spec.cpi} (CPI printed {cpi.released})",
    )
    age = (inputs.as_of - cpi.released).days
    if age > inputs.params.macro_freshness_days:
        real = real.stale(
            f"the real rate is today's {spec.policy} less a {spec.cpi} print released "
            f"{cpi.released}, {age} days before {inputs.as_of} and beyond the "
            f"{inputs.params.macro_freshness_days}-day grid.freshness_days.macro window; the "
            "nominal side is current, so the leaf would otherwise read as current too"
        )
    cells.append(real)
    return cells


def yield_curve(inputs: CellInputs) -> list[Cell]:
    """The local ten-year yield, and the slope, saying which slope: a 10y-minus-2y and a
    10y-minus-policy slope are different measurements and never ranked together unlabelled."""
    spec, reader = inputs.spec, inputs.reader
    if spec.long_rate is None:
        reason = f"{spec.long_gap}, so there is no ten-year yield and no second point for a slope"
        return [
            Cell.inapplicable(
                "long_rate_10y", Family.YIELD_CURVE, reason, code=Reason.SERIES_UNDECLARED
            ),
            Cell.inapplicable(
                "curve_slope", Family.YIELD_CURVE, reason, code=Reason.SERIES_UNDECLARED
            ),
        ]
    long_rate = reader.value_at(spec.long_rate, inputs.as_of)
    if long_rate is None:
        reason = reader.absence(spec.long_rate)
        return [
            Cell.absent("long_rate_10y", Family.YIELD_CURVE, reason, code=Reason.SERIES_ABSENT),
            Cell.absent("curve_slope", Family.YIELD_CURVE, reason, code=Reason.SERIES_ABSENT),
        ]
    cells = [
        Cell.measured(
            "long_rate_10y",
            Family.YIELD_CURVE,
            long_rate,
            units=Units.DECIMAL,
            window=spec.long_rate,
        )
    ]
    if spec.slope_short is None or spec.slope_label is None:
        cells.append(
            Cell.inapplicable(
                "curve_slope",
                Family.YIELD_CURVE,
                f"{spec.slope_gap}: the slope has no second point, and which slope it would be is "
                "itself undeclared",
                code=Reason.SERIES_UNDECLARED,
            )
        )
        return cells
    short_rate = reader.value_at(spec.slope_short, inputs.as_of)
    if short_rate is None:
        cells.append(
            Cell.absent(
                "curve_slope",
                Family.YIELD_CURVE,
                f"the short leg: {reader.absence(spec.slope_short)}",
                code=Reason.SERIES_ABSENT,
            )
        )
    else:
        cells.append(
            Cell.measured(
                "curve_slope",
                Family.YIELD_CURVE,
                long_rate - short_rate,
                units=Units.DECIMAL,
                window=spec.slope_label,
            )
        )
    return cells


# ─────────────────────────  14. turnover  ─────────────────────────


def turnover(inputs: CellInputs) -> list[Cell]:
    """Recent turnover against its longer-run average: volume leaving its own average separates
    a drift from a move somebody is taking part in."""
    params = inputs.params
    window = f"{params.technical_window_days}d vs the full daily window"
    volumes = [v for v in inputs.window_daily.volume if v is not None and v > 0]
    if (
        inputs.instrument is not None
        and inputs.instrument.asset_class is AssetClass.INDEX
        and not volumes
    ):
        return [
            Cell.inapplicable(
                "turnover_vs_average",
                Family.TURNOVER,
                f"{inputs.name} is an index level, which carries no volume",
                code=Reason.INDEX_HAS_NO_VOLUME,
                window=window,
            )
        ]
    needed = max(params.min_turnover, params.technical_window_days)
    if len(volumes) < needed:
        return [
            Cell.absent(
                "turnover_vs_average",
                Family.TURNOVER,
                f"only {len(volumes)} session(s) carry volume, below the {needed} needed to "
                "compare against an average",
                code=Reason.NO_VOLUME,
                window=window,
            )
        ]
    recent = mean(volumes[-params.technical_window_days :])
    baseline = mean(volumes)
    if not recent or not baseline:
        return [
            Cell.absent(
                "turnover_vs_average",
                Family.TURNOVER,
                "the volume history holds no usable average",
                code=Reason.NO_VOLUME,
                window=window,
            )
        ]
    return [
        Cell.measured(
            "turnover_vs_average",
            Family.TURNOVER,
            recent / baseline - 1.0,
            units=Units.RATIO,
            window=window,
            n_obs=len(volumes),
        )
    ]


# ─────────────────────────  15. positioning  ─────────────────────────


def positioning_cells(inputs: CellInputs) -> list[Cell]:
    """Speculative positioning where a futures contract exists: its level (the net non-commercial
    share of open interest ranked over its trailing window) and its change (that share
    differenced over the velocity window, in share points). Different units, both needed:
    crowding does not time a move, and a book being dumped is a different trade from a crowd
    still forming. Released reports only."""
    spec = inputs.spec
    weeks = inputs.params.positioning_velocity_weeks
    change_leaf = positioning_change_leaf(weeks)
    window = f"{inputs.settings.sources.positioning.percentile_window_weeks}w"
    if spec.cot is None:
        reason = f"{spec.cot_gap}; absent by design, not uncollected"
        return [
            Cell.inapplicable(
                POSITIONING_LEVEL,
                Family.POSITIONING,
                reason,
                code=Reason.NO_POSITIONING,
                window=window,
            ),
            Cell.inapplicable(
                change_leaf,
                Family.POSITIONING,
                reason,
                code=Reason.NO_POSITIONING,
                window=f"{weeks}w",
            ),
        ]
    reading = positioning(inputs.store, inputs.settings, spec.cot, as_of=inputs.as_of)
    if reading.status is PositioningStatus.NO_DATA or reading.share is None:
        return [
            Cell.absent(
                POSITIONING_LEVEL,
                Family.POSITIONING,
                reading.reason,
                code=Reason.SERIES_ABSENT,
                window=window,
            ),
            Cell.absent(
                change_leaf,
                Family.POSITIONING,
                reading.reason,
                code=Reason.SERIES_ABSENT,
                window=f"{weeks}w",
            ),
        ]
    released = f"reports released by {inputs.as_of}, newest {reading.report_date}"
    level = (
        Cell.absent(
            POSITIONING_LEVEL,
            Family.POSITIONING,
            reading.reason,
            code=Reason.SHORT_SAMPLE,
            window=window,
        )
        if reading.percentile is None
        else Cell.measured(
            POSITIONING_LEVEL,
            Family.POSITIONING,
            reading.percentile,
            units=Units.PERCENTILE,
            window=window_label(f"{window} of {spec.cot}", reading.window, "weekly reports"),
            n_obs=reading.window,
            reason=released,
        )
    )
    change = (
        Cell.absent(
            change_leaf,
            Family.POSITIONING,
            f"{spec.cot} holds fewer than {weeks + 1} released reports, so there is no {weeks}-"
            "report change",
            code=Reason.SHORT_SAMPLE,
            window=f"{weeks}w",
        )
        if reading.velocity is None
        else Cell.measured(
            change_leaf,
            Family.POSITIONING,
            reading.velocity,
            units=Units.DECIMAL,
            window=f"{weeks} weekly reports of {spec.cot}",
            n_obs=weeks + 1,
            reason=released,
        )
    )
    return [level, change]


#: Every family, in SKILL.md's order; the builder walks this and nothing else.
INDICATOR_FUNCTIONS: Final[tuple[tuple[Family, Callable[[CellInputs], list[Cell]]], ...]] = (
    (Family.RETURNS, returns),
    (Family.DISTANCE_FROM_HIGH, distance_from_high),
    (Family.DISTANCE_FROM_LOW, distance_from_low),
    (Family.DRAWDOWN, drawdown),
    (Family.REALISED_VOL, realised_vol),
    (Family.RV_TERM_STRUCTURE, rv_term_structure),
    (Family.IMPLIED_VOL, implied_vol),
    (Family.IV_PERCENTILE, iv_percentile),
    (Family.IV_HV_SPREAD, iv_hv_spread),
    (Family.CORRELATION, correlation_to_benchmark),
    (Family.CURRENCY_RETURN, currency_return),
    (Family.POLICY_RATE, policy_rate),
    (Family.YIELD_CURVE, yield_curve),
    (Family.TURNOVER, turnover),
    (Family.POSITIONING, positioning_cells),
)
