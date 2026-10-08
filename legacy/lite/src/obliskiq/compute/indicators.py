"""The fifteen indicator families, one function each.

SKILL.md fixes the indicator set so that cells are comparable across every
market and sector. A **family** is one of those fifteen; it emits one or more
**leaf cells**, because "returns: 5d, 21d, 63d, YTD, 12m, local and base" is one
indicator in the mandate and ten numbers in the store. ``grid.indicator`` holds
the leaf and ``grid.indicator_family`` holds the family, so the leaf stays
addressable and the fifteen stay countable.

Two rules run through every function here.

**A cell is computed or it is ``n/a`` with a reason.** Never a silent proxy,
never an estimate presented as data. The reason names the missing input — which
series, how many observations, how many were needed — because a coverage report
that says "n/a" and nothing else cannot be acted on.

**A short sample is a missing cell, not a weak one.** Every family compares its
sample against ``grid.min_observations`` and refuses below it. A correlation over
four days and a percentile over nine points are numbers nobody could defend.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..collect.vol import (
    garman_klass_from_bars,
    ohlc_bars,
    parkinson_from_bars,
    range_bars,
    restated_open_share,
)
from ..collect.vol import realised_vol as annualised_realised_vol
from ..config import Config
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..store.db import Store
from .frames import (
    PERCENT_TO_DECIMAL,
    BarSeries,
    MacroObservation,
    MacroSeries,
    bar_series,
    chain_rows,
    cpi_yoy,
    fx_ratio,
    latest_chain_date,
    macro_series,
    mixed_cpi_units,
    underlying_snapshot_count,
    underlying_snapshots,
)
from .smile import Smile, SmileFilters
from .stats import (
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
from .stats import (
    distance_from_low as _distance_above_low,
)

log = get_logger("compute.indicators")

# ─────────────────────────  the fifteen families  ─────────────────────────

FAMILY_RETURNS = "returns"
FAMILY_DISTANCE_FROM_HIGH = "distance_from_high"
FAMILY_DISTANCE_FROM_LOW = "distance_from_low"
FAMILY_DRAWDOWN = "drawdown"
FAMILY_REALISED_VOL = "realised_vol"
FAMILY_RV_TERM_STRUCTURE = "rv_term_structure"
FAMILY_IMPLIED_VOL = "implied_vol"
FAMILY_IV_PERCENTILE = "iv_percentile"

#: ``data.atm_iv_prefer``. Which implied vol indicator 7 reads when more than one
#: exists for a session. An exchange-quoted figure and one this repository solved
#: out of a mid price are not the same claim, so the order is DECLARED rather than
#: left to whichever branch happens to run first.
ATM_IV_PREFER_VENDOR = "vendor"
ATM_IV_PREFER_SOLVED = "solved"
ATM_IV_PREFER_CHAIN_FIRST = "chain_first"
FAMILY_IV_HV_SPREAD = "iv_hv_spread"
FAMILY_CORRELATION = "correlation"
FAMILY_CURRENCY_RETURN = "currency_return"
FAMILY_POLICY_RATE = "policy_rate"
FAMILY_YIELD_CURVE = "yield_curve"
FAMILY_TURNOVER = "turnover"
FAMILY_POSITIONING = "positioning"

#: The fixed set from SKILL.md, in its order. Exactly fifteen, and a test says so.
FAMILIES = (
    FAMILY_RETURNS,
    FAMILY_DISTANCE_FROM_HIGH,
    FAMILY_DISTANCE_FROM_LOW,
    FAMILY_DRAWDOWN,
    FAMILY_REALISED_VOL,
    FAMILY_RV_TERM_STRUCTURE,
    FAMILY_IMPLIED_VOL,
    FAMILY_IV_PERCENTILE,
    FAMILY_IV_HV_SPREAD,
    FAMILY_CORRELATION,
    FAMILY_CURRENCY_RETURN,
    FAMILY_POLICY_RATE,
    FAMILY_YIELD_CURVE,
    FAMILY_TURNOVER,
    FAMILY_POSITIONING,
)

# ─────────────────────────  what each family reads  ─────────────────────────
#
# Freshness belongs to the input that was read, not to the cell that displays
# it. Until this map existed, one reading of the newest daily BAR was stamped on
# every leaf of a market x sector: a policy-rate cell went stale
# because an equity bar was old, and a months-old vendor IV percentile read 'ok'
# because the bars were fresh. Both directions were wrong, and neither was
# visible in the output.
#
# INDICATOR_FUNCTIONS is a flat tuple with no metadata about what each function
# touches, so this is declared rather than derived. A family added without an
# entry here is caught by a test rather than silently exempted from freshness.

INPUT_DAILY_BARS = "daily_bars"
INPUT_OPTION_CHAIN = "option_chain"
INPUT_UNDERLYING_SNAPSHOT = "underlying_snapshot"
INPUT_MACRO = "macro"

#: Every input source a freshness verdict can be computed for.
INPUT_SOURCES = (
    INPUT_DAILY_BARS,
    INPUT_OPTION_CHAIN,
    INPUT_UNDERLYING_SNAPSHOT,
    INPUT_MACRO,
)

#: Family -> what it needs, as a tuple of REQUIREMENT GROUPS. Every group must
#: be satisfied, and within a group any one source will do. Three shapes, and
#: flattening them to a single list would get one of the three wrong:
#:
#:   returns        ((bars,),)                    one source, one group
#:   implied_vol    ((chain, snapshot),)          reads the chain, falls back to
#:                                                the vendor snapshot; either
#:                                                being current is enough
#:   iv_hv_spread   ((chain, snapshot), (bars,))  an implied vol from EITHER,
#:                                                minus a realised vol from bars;
#:                                                an old chain makes the spread
#:                                                indicative even against today's
#:                                                bars
#:
#: So a family is as fresh as its worst group, and a group as fresh as its best
#: member. A family added without an entry here fails a test rather than being
#: silently exempted from freshness.
FAMILY_INPUTS: dict[str, tuple[tuple[str, ...], ...]] = {
    FAMILY_RETURNS: ((INPUT_DAILY_BARS,),),
    FAMILY_DISTANCE_FROM_HIGH: ((INPUT_DAILY_BARS,),),
    FAMILY_DISTANCE_FROM_LOW: ((INPUT_DAILY_BARS,),),
    FAMILY_DRAWDOWN: ((INPUT_DAILY_BARS,),),
    FAMILY_REALISED_VOL: ((INPUT_DAILY_BARS,),),
    FAMILY_RV_TERM_STRUCTURE: ((INPUT_DAILY_BARS,),),
    FAMILY_IMPLIED_VOL: ((INPUT_OPTION_CHAIN, INPUT_UNDERLYING_SNAPSHOT),),
    FAMILY_IV_PERCENTILE: ((INPUT_UNDERLYING_SNAPSHOT,),),
    FAMILY_IV_HV_SPREAD: ((INPUT_OPTION_CHAIN, INPUT_UNDERLYING_SNAPSHOT), (INPUT_DAILY_BARS,)),
    # Own bars against the benchmark's bars: one source, read twice.
    FAMILY_CORRELATION: ((INPUT_DAILY_BARS,),),
    FAMILY_CURRENCY_RETURN: ((INPUT_MACRO,),),
    FAMILY_POLICY_RATE: ((INPUT_MACRO,),),
    FAMILY_YIELD_CURVE: ((INPUT_MACRO,),),
    FAMILY_TURNOVER: ((INPUT_DAILY_BARS,),),
    FAMILY_POSITIONING: ((INPUT_MACRO,),),
}

#: Families that read the market spec and the macro store and NOTHING else — no
#: bar, no chain, no snapshot — and which therefore compute for a market that has
#: no measuring instrument at all.
#:
#: DERIVED from FAMILY_INPUTS rather than listed by hand, because the property
#: that matters is "touches no instrument data", and a hand-kept list would go on
#: asserting it after a family gained a bar input. build_grid uses this to let a
#: market's macro through on its `broad` cell, and a family that stopped being
#: macro-only would otherwise be handed a contract id of None and read an empty
#: bar series as though it were a short one.
MACRO_ONLY_FAMILIES = frozenset(
    family
    for family, groups in FAMILY_INPUTS.items()
    if groups and all(group == (INPUT_MACRO,) for group in groups)
)


#: Families whose value comes off a market-data quote, and which therefore carry
#: a `market_data_status`. A macro cell reads a published series, which has no
#: quote state at all — labelling one FROZEN would invent a fact.
MARKET_DATA_FAMILIES = frozenset(
    family
    for family, groups in FAMILY_INPUTS.items()
    for group in groups
    if INPUT_OPTION_CHAIN in group or INPUT_UNDERLYING_SNAPSHOT in group
)


# ─────────────────────────  vocabulary  ─────────────────────────

STATUS_OK = "ok"
STATUS_NA = "n/a"
STATUS_STALE = "stale"
#: No collector could ever fill this cell — there is no instrument mapped to it,
#: the measuring instrument has no reachable option chain, or the market declares
#: no macro series for it. Distinct from ``n/a``, which means COLLECTIBLE and not
#: collected yet. The two used to share a status, which made the coverage floor
#: unreachable: 141 of 156 cells had no instrument, so a build could not exceed
#: about 9.6% against a 20% floor and reported DEGRADED every day, including the
#: days when collection really had failed.
STATUS_INAPPLICABLE = "inapplicable"

#: Every leaf family 7 emits, in every branch INCLUDING the ones that measure
#: nothing. A grid whose leaf set changes with its status is a grid whose rows
#: cannot be joined across days.
IMPLIED_VOL_LEAVES = (
    "implied_vol_atm",
    "implied_vol_skew",
    "implied_vol_atm_front",
    "implied_vol_atm_back",
    "implied_vol_term_slope",
)

UNIT_DECIMAL = "decimal"
UNIT_PERCENTILE = "percentile"
UNIT_RATIO = "ratio"
UNIT_INDEX = "index"

#: Return horizons, in TRADING sessions. These are definitions from SKILL.md's
#: indicator list rather than tunable windows, which is why they live here and
#: not in config: changing them changes what the grid means, not how it is tuned.
RETURN_HORIZONS = (("5d", 5), ("21d", 21), ("63d", 63), ("12m", 252))

#: The 52-week lookback is a market convention, not a configured window.
WEEKS_PER_YEAR = 52

#: Why a cell is absent, as a short code. The cell's own `reason` names the
#: specific market, series or count; the CODE is what a coverage report groups
#: on. Without it the histogram is one line per market x sector saying the same
#: thing 141 times, which is a report nobody reads — the exact failure mode the
#: rankings exist to prevent one level up.
REASON_NO_INSTRUMENT = "no_instrument_mapped"
REASON_SHORT_SAMPLE = "sample_too_short"
REASON_SERIES_ABSENT = "macro_series_absent"
REASON_NO_FX = "fx_series_absent"
REASON_NO_CHAIN = "no_option_chain"
REASON_NO_IV_PERCENTILE = "iv_percentile_unavailable"
REASON_NO_BENCHMARK = "no_benchmark_series"
REASON_NO_DISPERSION = "no_dispersion"
REASON_NO_VOLUME = "no_volume_history"
REASON_NO_POSITIONING = "no_futures_contract"
#: Two units inside one percentile window. Not a short sample and not an
#: absence: a ranking of two different quantities against each other, which
#: reads as an ordinary number all the way to the output. See
#: MacroSeries.units_known_at and migration 015.
REASON_MIXED_UNITS = "mixed_units_in_window"
REASON_STALE = "inputs_stale"
#: A computed value that is not a real number (`nan`, `inf`, `-inf`) reached the
#: grid's write site with a status claiming otherwise. This is the last resort
#: rather than a routine path — every leaf is expected to guard its own
#: arithmetic — and it exists so that a value nobody could defend never reaches
#: a caller as `status: "ok"` with a JSON `null` where `server/encode.py`
#: silently turned the non-finite float into one. See build_grid._write_cells.
REASON_NON_FINITE = "non_finite_value"
#: A range-based estimator needs a high and a low per bar, and this series does
#: not carry enough of them. `n/a` and deliberately NOT `inapplicable`: the
#: column exists on `bars` and a route that fills it would fill this cell, so the
#: gap stays in the collection-health denominator on the same reasoning
#: `instrument_not_seeded` was introduced under.
REASON_NO_BAR_RANGE = "no_high_low_range"

#: The opens on this series are the previous closes restated, so Garman-Klass —
#: which reads ln(C/O) as the session's directional move and SUBTRACTS its square
#: — would be computing a different formula rather than a worse number. Also
#: `n/a` rather than structural: another route may serve a real opening print.
REASON_OPEN_RESTATED = "open_restates_previous_close"

#: universe.yaml maps an instrument to this cell and the store holds no row for
#: it. COLLECTIBLE, not structural: `obliskiq-collect universe` owes it, and
#: reporting it as `no_instrument_mapped` would blame the declaration for a
#: seeding failure and hide the failure inside the ambition gap.
REASON_NOT_SEEDED = "instrument_not_seeded"

#: Structural codes. Each says "no collector could ever fill this", and each is
#: DECLARED somewhere a human wrote it rather than inferred from an absence:
#: ``no_instrument_mapped`` from the market x sector axes in universe.yaml,
#: ``options_not_available`` from a row's own options_unavailable_reason, and the
#: undeclared-series codes from a market spec that nulls the series on purpose.
REASON_NO_OPTIONS = "options_not_available"
REASON_SERIES_UNDECLARED = "macro_series_undeclared"
REASON_NO_FX_DECLARED = "fx_series_undeclared"

#: The benchmark cell's correlation TO ITSELF. Structural and permanent: no
#: collector will ever fill it, and it counted as a collection failure every day.
REASON_IS_BENCHMARK = "cell_is_the_benchmark"

#: An index LEVEL carries no volume — there is nothing to turn over. Distinct
#: from a young store with too few sessions, which is a collection gap.
REASON_INDEX_HAS_NO_VOLUME = "index_carries_no_volume"

#: Every reason code whose cells are structurally inapplicable rather than
#: merely uncollected. The coverage report excludes these from its denominator,
#: and `Cell.inapplicable` is the only constructor that produces them.
INAPPLICABLE_CODES = frozenset(
    {
        REASON_NO_INSTRUMENT,
        REASON_NO_OPTIONS,
        REASON_SERIES_UNDECLARED,
        REASON_NO_FX_DECLARED,
        REASON_NO_POSITIONING,
        REASON_IS_BENCHMARK,
        REASON_INDEX_HAS_NO_VOLUME,
    }
)

#: ``universe.asset_class`` for an index level. Named here because the turnover
#: family needs it, and importing build_grid would be a cycle: build_grid is
#: what builds these inputs.
ASSET_INDEX = "IND"

#: IV percentile provenance, reported in the cell's reason and the self-audit.
SOURCE_VENDOR = "vendor"
SOURCE_SELF = "self"
SOURCE_BOTH = "both"


@dataclass(frozen=True)
class Cell:
    """One leaf of the grid."""

    indicator: str
    family: str
    value: float | None = None
    status: str = STATUS_OK
    reason: str | None = None
    window: str | None = None
    units: str | None = None
    #: A short, groupable classification of why the cell is absent. Required
    #: whenever one is: a coverage report groups on this, and the reason carries
    #: the specifics.
    reason_code: str | None = None
    #: How many observations are ACTUALLY behind the value. ``window`` names the
    #: span the code asked for; where a floor admits a shorter sample the two
    #: diverge, and only this says by how much. None where the cell carries no
    #: sample — an absent cell, or a level read rather than a sample statistic.
    n_obs: int | None = None
    #: The market-data state of the quotes behind this value, worst first —
    #: FROZEN, DELAYED, REALTIME, whatever the route reported. NULL on a macro
    #: cell, which reads a published series and has no quote state, and on a
    #: cell whose route reported nothing. SKILL.md requires anything frozen or
    #: delayed to be labelled indicative in every table it appears in, and until
    #: this field existed the grid could not carry the label that far.
    market_data_status: str | None = None

    @classmethod
    def absent(
        cls,
        indicator: str,
        family: str,
        reason: str,
        *,
        code: str,
        window: str | None = None,
    ) -> Cell:
        return cls(
            indicator=indicator,
            family=family,
            value=None,
            status=STATUS_NA,
            reason=reason,
            window=window,
            reason_code=code,
        )

    @classmethod
    def inapplicable(
        cls,
        indicator: str,
        family: str,
        reason: str,
        *,
        code: str,
        window: str | None = None,
    ) -> Cell:
        """A cell no collector could ever fill.

        Separate from ``absent`` because the two answer different questions and
        only one of them is a collection failure. This is excluded from the
        coverage denominator and reported as the ambition gap instead; ``absent``
        stays in the denominator, because something is supposed to arrive.
        """
        if code not in INAPPLICABLE_CODES:
            raise ValueError(
                f"{code!r} is not a structural reason code. An inapplicable cell claims "
                "that no collector could ever fill it, which is a claim about the "
                "declaration and not about today's store — add the code to "
                "INAPPLICABLE_CODES if that is really what it means."
            )
        return cls(
            indicator=indicator,
            family=family,
            value=None,
            status=STATUS_INAPPLICABLE,
            reason=reason,
            window=window,
            reason_code=code,
        )

    def stale(self, reason: str) -> Cell:
        """Downgrade an otherwise-good cell whose newest input is old."""
        if self.status != STATUS_OK:
            return self
        return Cell(
            indicator=self.indicator,
            family=self.family,
            value=self.value,
            status=STATUS_STALE,
            reason=reason,
            window=self.window,
            units=self.units,
            reason_code=REASON_STALE,
            n_obs=self.n_obs,
            market_data_status=self.market_data_status,
        )

    def labelled(self, market_data_status: str | None) -> Cell:
        """Fill in the market-data state behind this value, if it has none.

        A separate step from construction because most of the fifteen family
        functions return a plain number and know nothing about the rows
        underneath it, while the builder that read those rows does. The two that
        DO know — implied vol and the IV/HV spread, which hold the actual smile
        they measured — set it themselves, and this never overwrites them: the
        builder's label is the freshest row in the store, theirs is the row the
        value actually came from, and where those differ the specific one is
        right."""
        if market_data_status is None or self.market_data_status is not None:
            return self
        return Cell(
            indicator=self.indicator,
            family=self.family,
            value=self.value,
            status=self.status,
            reason=self.reason,
            window=self.window,
            units=self.units,
            reason_code=self.reason_code,
            n_obs=self.n_obs,
            market_data_status=market_data_status,
        )


@dataclass
class IndicatorInputs:
    """Everything the fifteen families read, assembled once per cell."""

    store: Store
    config: Config
    as_of: date
    market: str
    sector: str
    contract_id: int | None
    symbol: str | None
    market_spec: dict[str, Any] = field(default_factory=dict)
    benchmark_contract_id: int | None = None
    benchmark_market: str | None = None
    #: Why no option chain is reachable for the measuring instrument, as
    #: universe.yaml declares it. None means one IS reachable, so a missing chain
    #: is something a collector still owes. Declared rather than inferred from a
    #: falsy collect_chain: absence is exactly the ambiguity being removed here.
    options_unavailable_reason: str | None = None
    #: ``universe.asset_class`` for the measuring instrument, upper-cased. An
    #: index LEVEL has no volume to turn over, which is a fact about what the
    #: instrument is rather than about how much of it has been collected.
    asset_class: str | None = None

    # windows and floors, bound from config once
    daily_window_years: int = 0
    weekly_window_years: int = 0
    rv_tenors: tuple[int, ...] = ()
    correlation_windows: tuple[int, ...] = ()
    technical_window_days: int = 0
    trading_days_per_year: float = 0.0
    calendar_days_per_year: float = 0.0
    iv_percentile_source: str = SOURCE_VENDOR
    #: ``data.atm_iv_prefer``: vendor | solved | chain_first.
    atm_iv_prefer: str = ATM_IV_PREFER_VENDOR
    iv_self_min_sessions: int = 0
    iv_percentile_window_weeks: int = WEEKS_PER_YEAR
    #: ``data.garman_klass_max_restated_open_share``. Above this share of bars
    #: whose open is exactly the previous close, the open is a restatement rather
    #: than an opening print and the Garman-Klass leaves refuse. How much is too
    #: much is policy, which is why the estimator itself does not hold it.
    garman_klass_max_restated_open_share: float = 0.0
    correlation_norm_window_years: int = 0
    #: Independent (non-overlapping) correlations the norm needs before its
    #: standard deviation is a dispersion. See grid.correlation_norm_min_independent.
    correlation_norm_min_independent: int = 0
    #: The same rule for the realised-vol percentiles, which are also built from
    #: rolling readings and were also counting overlapping ones as evidence. Its
    #: own key rather than the correlation norm's: that one is documented as what
    #: a standard deviation needs, and this one floors a rank. See
    #: grid.realised_vol_percentile_min_independent.
    realised_vol_percentile_min_independent: int = 0
    min_observations: dict[str, int] = field(default_factory=dict)
    #: ``grid.freshness_days``, one window per input source. Bound here so a
    #: family function can answer 'is this input current?' without a second
    #: config read, and so the builder and the families agree on one number.
    freshness: dict[str, int] = field(default_factory=dict)
    #: Smile hygiene, bound once. The grid reads a chain through exactly the
    #: same filters the zombie test does — two standards behind one implied
    #: vol is how the grid and the risk engine end up disagreeing about the
    #: same instrument on the same day.
    smile_filters: SmileFilters | None = None

    _daily: BarSeries | None = None
    _weekly: BarSeries | None = None
    _benchmark_daily: BarSeries | None = None

    @classmethod
    def bind(
        cls,
        store: Store,
        config: Config,
        *,
        as_of: date,
        market: str,
        sector: str,
        contract_id: int | None,
        symbol: str | None,
        market_spec: dict[str, Any],
        benchmark_contract_id: int | None,
        benchmark_market: str | None,
        options_unavailable_reason: str | None = None,
        asset_class: str | None = None,
    ) -> IndicatorInputs:
        data = config.section("data")
        return cls(
            store=store,
            config=config,
            as_of=as_of,
            market=market,
            sector=sector,
            contract_id=contract_id,
            symbol=symbol,
            market_spec=market_spec,
            benchmark_contract_id=benchmark_contract_id,
            benchmark_market=benchmark_market,
            options_unavailable_reason=options_unavailable_reason,
            asset_class=asset_class,
            daily_window_years=data.require_int("daily_window_years"),
            weekly_window_years=data.require_int("weekly_window_years"),
            rv_tenors=tuple(int(t) for t in data.require_list("rv_tenors_days")),
            correlation_windows=tuple(
                int(w) for w in data.require_list("correlation_windows_days")
            ),
            technical_window_days=data.require_int("technical_window_days"),
            trading_days_per_year=data.require_float("trading_days_per_year"),
            calendar_days_per_year=config.require_float("numerics.bsm.day_count_basis"),
            iv_percentile_source=data.require_str("iv_percentile_source"),
            atm_iv_prefer=data.require_str("atm_iv_prefer"),
            iv_self_min_sessions=data.require_int("iv_self_source_min_sessions"),
            iv_percentile_window_weeks=data.require_int("iv_percentile_window_weeks"),
            garman_klass_max_restated_open_share=data.require_float(
                "garman_klass_max_restated_open_share"
            ),
            correlation_norm_window_years=config.require_int("grid.correlation_norm_window_years"),
            correlation_norm_min_independent=config.require_int(
                "grid.correlation_norm_min_independent"
            ),
            realised_vol_percentile_min_independent=config.require_int(
                "grid.realised_vol_percentile_min_independent"
            ),
            min_observations=dict(config.get("grid.min_observations")),
            freshness=dict(config.get("grid.freshness_days")),
            smile_filters=SmileFilters.from_config(config),
        )

    # ── lazily loaded series ──

    @property
    def daily(self) -> BarSeries:
        if self._daily is None:
            # The DAILY window, not the weekly one. data.daily_window_years is
            # also what collect/backfill.py fetches, so asking for the weekly
            # span here requested five years of daily bars the collector never
            # collects and never could.
            since = self.as_of - timedelta(
                days=round(self.daily_window_years * self.calendar_days_per_year)
            )
            self._daily = (
                BarSeries(contract_id=self.contract_id or 0, freq="1d")
                if self.contract_id is None
                else bar_series(self.store, self.contract_id, since=since, until=self.as_of)
            )
        return self._daily

    @property
    def weekly(self) -> BarSeries:
        if self._weekly is None:
            since = self.as_of - timedelta(
                days=round(self.weekly_window_years * self.calendar_days_per_year)
            )
            self._weekly = (
                BarSeries(contract_id=self.contract_id or 0, freq="1w")
                if self.contract_id is None
                else bar_series(
                    self.store, self.contract_id, freq="1w", since=since, until=self.as_of
                )
            )
        return self._weekly

    @property
    def benchmark_daily(self) -> BarSeries:
        if self._benchmark_daily is None:
            since = self.as_of - timedelta(
                days=round(self.weekly_window_years * self.calendar_days_per_year)
            )
            self._benchmark_daily = (
                BarSeries(contract_id=0, freq="1d")
                if self.benchmark_contract_id is None
                else bar_series(
                    self.store, self.benchmark_contract_id, since=since, until=self.as_of
                )
            )
        return self._benchmark_daily

    def floor(self, key: str) -> int:
        return int(self.min_observations.get(key, 0))

    def freshness_days(self, source: str) -> int:
        """The freshness window for one input source.

        Raises rather than defaulting: a source with no configured window would
        silently never go stale, which is the failure this whole change exists
        to end."""
        if source not in self.freshness:
            raise ConfigError(f"grid.freshness_days has no window for input source {source!r}")
        return int(self.freshness[source])

    def series(self, series_id: Any) -> MacroSeries | None:
        if not series_id:
            return None
        series = macro_series(self.store, str(series_id))
        return series if len(series) else None

    def latest_value(self, series_id: Any, *, as_decimal: bool = True) -> float | None:
        """The newest value PUBLISHED on or before ``as_of``.

        Release-aligned, not reference-aligned: a policy rate or a CPI print
        feeds a decision, and a decision cannot use a number that was not out
        yet. A series carrying no release date yields None rather than a figure
        nobody could have read.
        """
        series = self.series(series_id)
        if series is None:
            return None
        latest = series.known_at(self.as_of)
        if latest is None:
            return None
        return latest.value / PERCENT_TO_DECIMAL if as_decimal else latest.value


# ─────────────────────────  1. returns  ─────────────────────────


def returns(inputs: IndicatorInputs) -> list[Cell]:
    """5d, 21d, 63d, YTD and 12m, in local AND base currency.

    Both are reported always. Local is what the market did; base is what the
    account earned, and a market up sixty percent locally can be down in euros.
    """
    cells: list[Cell] = []
    closes = inputs.daily.close
    stamps = inputs.daily.ts
    fx_ratio_cache: dict[str, float | None] = {}

    for label, sessions in RETURN_HORIZONS:
        # A fixed horizon needs its full window. Computing "63d" from three bars
        # produces a number correctly labelled and completely wrong: the label is
        # the claim, and a two-session move is not a quarter's return.
        cells.extend(
            _return_pair(
                inputs,
                label,
                _tail(closes, sessions),
                sessions,
                fx_ratio_cache,
                required=sessions + 1,
            )
        )

    # Year to date is the exception: in early January it is legitimately short,
    # because that is how much year there has been.
    ytd_closes = _year_to_date(stamps, closes, inputs.as_of)
    cells.extend(
        _return_pair(
            inputs,
            "ytd",
            ytd_closes,
            max(len(ytd_closes) - 1, 0),
            fx_ratio_cache,
            required=max(inputs.floor("returns"), _MIN_ENDPOINTS),
        )
    )
    return cells


def _return_pair(
    inputs: IndicatorInputs,
    label: str,
    window: list[float],
    sessions: int,
    fx_cache: dict[str, float | None],
    *,
    required: int,
) -> list[Cell]:
    local_name = f"return_{label}_local"
    base_name = f"return_{label}_base"
    window_text = f"{label} ({len(window)} bars)"
    needed = max(required, inputs.floor("returns"), _MIN_ENDPOINTS)

    if len(window) < needed:
        reason = (
            f"only {len(window)} bar(s) inside the {label} window; a {label} return needs "
            f"{needed}. Backfill the history rather than reading a shorter move under a "
            "longer label."
        )
        return [
            Cell.absent(
                local_name, FAMILY_RETURNS, reason, window=window_text, code=REASON_SHORT_SAMPLE
            ),
            Cell.absent(
                base_name, FAMILY_RETURNS, reason, window=window_text, code=REASON_SHORT_SAMPLE
            ),
        ]

    local = simple_return(window[0], window[-1])
    cells = [
        Cell(
            indicator=local_name,
            family=FAMILY_RETURNS,
            value=local,
            window=window_text,
            units=UNIT_DECIMAL,
        )
    ]

    calendar_days = round(sessions * inputs.calendar_days_per_year / inputs.trading_days_per_year)
    ratio = _fx_ratio(inputs, calendar_days, fx_cache)
    if ratio is None or local is None:
        cells.append(
            Cell.absent(
                base_name,
                FAMILY_RETURNS,
                f"no FX series {inputs.market_spec.get('fx_series') or '(undeclared)'} in "
                "the store, so the euro leg of the return cannot be computed. A local "
                "figure is not a substitute: it embeds no currency at all.",
                window=window_text,
                code=REASON_NO_FX,
            )
        )
    else:
        cells.append(
            Cell(
                indicator=base_name,
                family=FAMILY_RETURNS,
                value=(1.0 + local) * ratio - 1.0,
                window=window_text,
                units=UNIT_DECIMAL,
            )
        )
    return cells


# ─────────────────────────  2-4. range position  ─────────────────────────


def distance_from_high(inputs: IndicatorInputs) -> list[Cell]:
    """Distance below the 52-week high, and below the long-run high."""
    return [
        _range_cell(
            inputs,
            indicator="distance_from_52w_high",
            family=FAMILY_DISTANCE_FROM_HIGH,
            values=_weeks(inputs, WEEKS_PER_YEAR),
            window=_window_label(f"{WEEKS_PER_YEAR}w", _weeks(inputs, WEEKS_PER_YEAR)),
            compute=drawdown_from_peak,
        ),
        _range_cell(
            inputs,
            indicator="distance_from_long_run_high",
            family=FAMILY_DISTANCE_FROM_HIGH,
            values=inputs.weekly.close,
            window=_window_label(f"{inputs.weekly_window_years}y weekly", inputs.weekly.close),
            compute=drawdown_from_peak,
        ),
    ]


def distance_from_low(inputs: IndicatorInputs) -> list[Cell]:
    """Distance above the 52-week low. Part of the coiled definition."""
    return [
        _range_cell(
            inputs,
            indicator="distance_from_52w_low",
            family=FAMILY_DISTANCE_FROM_LOW,
            values=_weeks(inputs, WEEKS_PER_YEAR),
            window=_window_label(f"{WEEKS_PER_YEAR}w", _weeks(inputs, WEEKS_PER_YEAR)),
            compute=_distance_above_low,
        )
    ]


def drawdown(inputs: IndicatorInputs) -> list[Cell]:
    """The WORST peak-to-trough fall inside the long-run window.

    Family 2's ``distance_from_long_run_high`` says where the series sits today
    relative to its highest point. This says what it lived through, wherever in
    the window that happened — and the two were the same computation under two
    names, so one of the fifteen families measured nothing the others did not.
    A market fully recovered from a 40% collapse reads zero there and -0.40
    here, and only this one is a statement about the risk in holding it.
    """
    return [
        _range_cell(
            inputs,
            indicator="max_drawdown_long_run",
            family=FAMILY_DRAWDOWN,
            values=inputs.weekly.close,
            window=_window_label(f"{inputs.weekly_window_years}y weekly", inputs.weekly.close),
            compute=max_drawdown,
        )
    ]


def _range_cell(
    inputs: IndicatorInputs,
    *,
    indicator: str,
    family: str,
    values: list[float],
    window: str,
    compute: Callable[[list[float]], float | None],
) -> Cell:
    floor = inputs.floor("percentile")
    if len(values) < floor:
        return Cell.absent(
            indicator,
            family,
            f"only {len(values)} weekly bar(s) in the {window} window, below the {floor} "
            "needed for a range position",
            window=window,
            code=REASON_SHORT_SAMPLE,
        )
    value = compute(values)
    if value is None:
        return Cell.absent(
            indicator,
            family,
            "the window holds no usable closes",
            window=window,
            code=REASON_SHORT_SAMPLE,
        )
    return Cell(indicator=indicator, family=family, value=value, window=window, units=UNIT_DECIMAL)


# ─────────────────────────  5-6. realised volatility  ─────────────────────────


def realised_vol(inputs: IndicatorInputs) -> list[Cell]:
    """Realised vol at the reference tenor, and its long-run percentile.

    A level without its percentile says nothing about regime: thirty percent is
    quiet for one name and a multi-year extreme for another.

    THREE estimators, each with its own named leaves: close-to-close, Parkinson
    and Garman-Klass. They are not three readings of one number — the two
    range-based ones measure the intraday range and see nothing of the overnight
    gap, so on a market that closes at night they read materially below the
    close-to-close figure and always will. That is why they are added beside it
    rather than replacing it, why the estimator is the LEAF NAME rather than a
    provenance column that could go stale, and why nothing subtracts one of them
    from an implied vol. ``docs/DECISIONS.md`` carries the measurement.
    """
    tenor = _reference_tenor(inputs)
    window = f"{tenor}d"
    level = _realised_vol(inputs.daily.close, tenor, inputs.trading_days_per_year)
    floor = inputs.realised_vol_percentile_min_independent

    if level is None:
        # The range leaves still go out. They are a different estimator over a
        # different input, so gating them on the close series would leave a cell
        # with NO row for them at all — not an n/a, an absence from the table —
        # and a leaf missing from the grid is the one thing this build treats as
        # worse than a leaf that says why it is missing.
        reason = (
            f"fewer than {tenor + 1} usable daily closes, so a {tenor}-day realised vol "
            "cannot be measured"
        )
        return [
            Cell.absent(
                f"realised_vol_{tenor}d",
                FAMILY_REALISED_VOL,
                reason,
                window=window,
                code=REASON_SHORT_SAMPLE,
            ),
            Cell.absent(
                f"realised_vol_{tenor}d_percentile",
                FAMILY_REALISED_VOL,
                reason,
                window=window,
                code=REASON_SHORT_SAMPLE,
            ),
            *_range_vol_leaves(inputs, tenor),
        ]

    cells = [
        Cell(
            indicator=f"realised_vol_{tenor}d",
            family=FAMILY_REALISED_VOL,
            value=level,
            window=window,
            units=UNIT_DECIMAL,
            # tenor RETURNS, taken off tenor + 1 closes. The range leaves read
            # one term per BAR and so consume one row of history less for the
            # same tenor. Both are tenor sessions, which is what n_obs states;
            # their rolling percentiles differ by one reading for the same
            # reason, and test_window_honesty pins that rather than tidying it.
            n_obs=tenor,
        )
    ]

    history = _rolling_realised_vol(inputs.daily.close, tenor, inputs.trading_days_per_year)
    # The window the code ASKED for. The label below names the readings actually
    # behind it, because the floor admits far fewer than the span implies and a
    # percentile over a handful of independent readings is not a multi-year one.
    percentile_window = f"{inputs.daily_window_years}y window of {tenor}d rolling vol"
    if len(history) < floor:
        cells.append(
            Cell.absent(
                f"realised_vol_{tenor}d_percentile",
                FAMILY_REALISED_VOL,
                _too_few_independent_readings(len(history), floor, tenor),
                window=percentile_window,
                code=REASON_SHORT_SAMPLE,
            )
        )
    else:
        cells.append(
            Cell(
                indicator=f"realised_vol_{tenor}d_percentile",
                family=FAMILY_REALISED_VOL,
                value=percentile_of(history, level),
                window=_window_label(percentile_window, history, noun="readings"),
                units=UNIT_PERCENTILE,
                n_obs=len(history),
            )
        )
    cells.extend(_range_vol_leaves(inputs, tenor))
    return cells


def _range_vol_leaves(inputs: IndicatorInputs, tenor: int) -> list[Cell]:
    """The Parkinson and Garman-Klass leaves, with their own windows and counts.

    Parkinson asks only for a high and a low, which every bar route this build
    reaches serves. Garman-Klass additionally reads the open, and an open that is
    the previous close restated is not an opening print — so it is tested against
    ``data.garman_klass_max_restated_open_share`` FIRST, and refused with the
    measured share rather than computed anyway. A window that happens to sum to a
    positive variance on restated opens is still not a Garman-Klass reading.
    """
    bars = inputs.daily
    basis = inputs.trading_days_per_year

    cells = _estimator_leaves(
        inputs,
        indicator=f"parkinson_vol_{tenor}d",
        tenor=tenor,
        bars=range_bars(bars.high, bars.low),
        measure=lambda window: parkinson_from_bars(window, trading_days_per_year=basis),
        noun="bar(s) carrying a usable high and low",
    )

    ohlc = ohlc_bars(bars.open, bars.high, bars.low, bars.close)
    share = restated_open_share(ohlc[-tenor:]) if len(ohlc) >= tenor else None
    if share is not None and share > inputs.garman_klass_max_restated_open_share:
        reason = (
            f"{share:.0%} of the last {tenor} daily bars open at exactly the previous close, "
            f"above data.garman_klass_max_restated_open_share. This listing restates the close "
            "rather than publishing an opening print, and Garman-Klass reads the open-to-close "
            "move as the session's direction — so the figure would be a different formula, not "
            "a worse estimate. Parkinson needs no open and is computed beside this."
        )
        cells.extend(
            _absent_pair(
                f"garman_klass_vol_{tenor}d",
                tenor=tenor,
                daily_window_years=inputs.daily_window_years,
                reason=reason,
                code=REASON_OPEN_RESTATED,
            )
        )
        return cells

    cells.extend(
        _estimator_leaves(
            inputs,
            indicator=f"garman_klass_vol_{tenor}d",
            tenor=tenor,
            bars=ohlc,
            measure=lambda window: garman_klass_from_bars(window, trading_days_per_year=basis),
            noun="bar(s) carrying a usable open, high, low and close",
        )
    )
    return cells


def _estimator_leaves(
    inputs: IndicatorInputs,
    *,
    indicator: str,
    tenor: int,
    bars: list[Any],
    measure: Callable[[list[Any]], float | None],
    noun: str,
) -> list[Cell]:
    """One estimator's level leaf and its percentile, over the bars it can use.

    The same two refusals for both range estimators, because they fail the same
    two ways: too few usable bars, and a window with no range in it at all. The
    percentile is ranked against the SAME estimator's own history — a rank of
    Parkinson against close-to-close readings would be a ranking of two different
    quantities, which is the failure ``MacroSeries.units_known_at`` exists to stop
    one layer down.
    """
    window = f"{tenor}d"
    percentile_window = f"{inputs.daily_window_years}y window of {window} rolling vol"
    if len(bars) < tenor:
        return _absent_pair(
            indicator,
            tenor=tenor,
            daily_window_years=inputs.daily_window_years,
            reason=(
                f"only {len(bars)} daily {noun}, below the {tenor} a {tenor}-session "
                "range estimate needs"
            ),
            code=REASON_NO_BAR_RANGE,
        )

    level = measure(bars[-tenor:])
    if level is None:
        return _absent_pair(
            indicator,
            tenor=tenor,
            daily_window_years=inputs.daily_window_years,
            reason=(
                f"the last {tenor} daily {noun} carry no usable range, so no volatility "
                "can be measured from them"
            ),
            code=REASON_NO_DISPERSION,
        )

    cells = [
        Cell(
            indicator=indicator,
            family=FAMILY_REALISED_VOL,
            value=level,
            window=window,
            units=UNIT_DECIMAL,
            n_obs=tenor,
        )
    ]

    history = _rolling(bars, tenor, measure, step=tenor)
    floor = inputs.realised_vol_percentile_min_independent
    if len(history) < floor:
        cells.append(
            Cell.absent(
                f"{indicator}_percentile",
                FAMILY_REALISED_VOL,
                _too_few_independent_readings(len(history), floor, tenor),
                window=percentile_window,
                code=REASON_SHORT_SAMPLE,
            )
        )
    else:
        cells.append(
            Cell(
                indicator=f"{indicator}_percentile",
                family=FAMILY_REALISED_VOL,
                value=percentile_of(history, level),
                window=_window_label(percentile_window, history, noun="readings"),
                units=UNIT_PERCENTILE,
                n_obs=len(history),
            )
        )
    return cells


def _too_few_independent_readings(found: int, floor: int, tenor: int) -> str:
    """Why a percentile is refused, in the terms the sampling actually uses.

    It names the sampling as well as the count, because "only 2 readings" on a
    store holding sixty bars reads as a collection failure and is not one: the
    bars are there, and sixty of them hold two windows that share no data. The
    same sentence one family over refuses an overlapping correlation norm, and
    for the same reason.
    """
    return (
        f"only {found} NON-OVERLAPPING {tenor}-day reading(s) fit in the daily window, "
        f"below the {floor} floor in grid.realised_vol_percentile_min_independent. "
        "Readings sampled every session share all but one of their bars, so counting "
        f"them would report roughly {tenor} times the evidence actually behind the "
        "number; the percentile is reported absent rather than ranked against them"
    )


def _absent_pair(
    indicator: str, *, tenor: int, daily_window_years: int, reason: str, code: str
) -> list[Cell]:
    """A level leaf and its percentile, both absent for the same reason.

    Both, always: a percentile of a level that could not be measured is not a
    thinner claim than the level, it is the same absence twice, and emitting only
    one of them would leave the other missing from the grid entirely rather than
    present and honest.
    """
    return [
        Cell.absent(indicator, FAMILY_REALISED_VOL, reason, window=f"{tenor}d", code=code),
        Cell.absent(
            f"{indicator}_percentile",
            FAMILY_REALISED_VOL,
            reason,
            window=f"{daily_window_years}y window of {tenor}d rolling vol",
            code=code,
        ),
    ]


def rv_term_structure(inputs: IndicatorInputs) -> list[Cell]:
    """Realised vol at every configured tenor, and the slope between the ends.

    A single tenor cannot show whether volatility is expanding or collapsing,
    which is the distinction the whole screen turns on.
    """
    cells: list[Cell] = []
    levels: dict[int, float] = {}
    for tenor in inputs.rv_tenors:
        value = _realised_vol(inputs.daily.close, tenor, inputs.trading_days_per_year)
        window = f"{tenor}d"
        if value is None:
            cells.append(
                Cell.absent(
                    f"rv_{tenor}d",
                    FAMILY_RV_TERM_STRUCTURE,
                    f"fewer than {tenor + 1} usable daily closes",
                    window=window,
                    code=REASON_SHORT_SAMPLE,
                )
            )
            continue
        levels[tenor] = value
        cells.append(
            Cell(
                indicator=f"rv_{tenor}d",
                family=FAMILY_RV_TERM_STRUCTURE,
                value=value,
                window=window,
                units=UNIT_DECIMAL,
            )
        )

    tenors = sorted(levels)
    slope_window = (
        f"{tenors[0]}d vs {tenors[-1]}d" if len(tenors) >= _MIN_ENDPOINTS else "unavailable"
    )
    if len(tenors) < _MIN_ENDPOINTS or levels[tenors[-1]] <= 0:
        cells.append(
            Cell.absent(
                "rv_term_slope",
                FAMILY_RV_TERM_STRUCTURE,
                "at least two tenors are needed before a term structure has a slope",
                window=slope_window,
                code=REASON_SHORT_SAMPLE,
            )
        )
    else:
        cells.append(
            Cell(
                indicator="rv_term_slope",
                family=FAMILY_RV_TERM_STRUCTURE,
                value=levels[tenors[0]] / levels[tenors[-1]] - 1.0,
                window=slope_window,
                units=UNIT_RATIO,
            )
        )
    return cells


# ─────────────────────────  7-9. implied volatility  ─────────────────────────


def _no_options_reason(inputs: IndicatorInputs, indicator: str) -> str:
    """Why an option family does not apply to this cell, in the row's own words.

    Reads the declaration rather than the store. "There is no chain today" and
    "no chain is reachable for this instrument at all" are different claims, and
    only the first is something a collector still owes.
    """
    declared = (inputs.options_unavailable_reason or "").rstrip(". ")
    return (
        f"{indicator} needs an option chain, and universe.yaml declares none reachable for "
        f"{inputs.symbol or inputs.market}: {declared}. A structural gap, not a collection "
        "one — no collector run will change it."
    )


def _iv_percentile_leaves(source: str) -> tuple[str, ...]:
    """The leaves indicator 8 emits under a given ``data.iv_percentile_source``.

    Shared with the computing path so that an inapplicable cell writes exactly
    the same leaf names a populated one would. A grid whose leaf set changes with
    its status is a grid whose rows cannot be joined across days.
    """
    leaves: list[str] = []
    if source in (SOURCE_VENDOR, SOURCE_BOTH):
        leaves.append("iv_percentile_vendor")
    if source in (SOURCE_SELF, SOURCE_BOTH):
        leaves.append("iv_percentile_self")
    leaves.append("iv_percentile")
    if source == SOURCE_BOTH:
        leaves.append("iv_percentile_divergence")
    return tuple(leaves)


def implied_vol(inputs: IndicatorInputs) -> list[Cell]:
    """The LEVEL of implied vol and the DIRECTION of it, where a chain exists.

    Two leaves, and they answer different questions off the same collected
    surface. ``implied_vol_atm`` is the level, and it is symmetric: it says how
    much movement is priced and nothing about which way. ``implied_vol_skew`` is
    the risk reversal — the out-of-the-money call's implied vol minus the
    out-of-the-money put's, both at ``smile.skew_delta`` — and it says which tail
    the market is paying for.

    The collectors buy up to ``collectors.chain.max_expiries_per_underlying``
    expiries at ``max_strikes_per_expiry`` strikes on both rights and store every
    row with its own ``iv`` and ``delta``. For the life of the project the grid
    published one number off all of that.

    Both leaves are emitted in every branch, including the ones that measure
    nothing. A grid whose leaf set changes with its status is a grid whose rows
    cannot be joined across days — the same reason ``_iv_percentile_leaves``
    exists one indicator along.
    """
    if inputs.options_unavailable_reason:
        return [
            Cell.inapplicable(
                indicator,
                FAMILY_IMPLIED_VOL,
                _no_options_reason(inputs, "Indicator 7"),
                code=REASON_NO_OPTIONS,
            )
            for indicator in IMPLIED_VOL_LEAVES
        ]

    # One chain read for both leaves. _atm_implied_vol may answer from the vendor
    # snapshot without touching it, and the skew can only ever come off the chain.
    chain = _ChainSmile(inputs)
    value, reason, status = _atm_implied_vol(inputs, chain=chain)

    cells: list[Cell] = []
    if value is None:
        cells.append(
            Cell.absent("implied_vol_atm", FAMILY_IMPLIED_VOL, reason, code=REASON_NO_CHAIN)
        )
    else:
        cells.append(
            Cell(
                indicator="implied_vol_atm",
                family=FAMILY_IMPLIED_VOL,
                value=value,
                window=reason,
                units=UNIT_DECIMAL,
                market_data_status=status,
            )
        )
    cells.append(_skew_cell(inputs, chain))
    cells.extend(_term_structure_cells(inputs, chain))
    return cells


#: The three leaves the term structure emits, in the order they are written.
_TERM_LEAVES = ("implied_vol_atm_front", "implied_vol_atm_back", "implied_vol_term_slope")


@dataclass(frozen=True)
class _TermLeg:
    """One end of the implied term structure: an expiry, its DTE, its ATM vol."""

    expiry: date
    dte: int
    quote: Any
    smile: Smile


def _term_window(front_dte: int | None, back_dte: int | None) -> str:
    """The window label, and it carries the two ACTUAL DTEs or it carries nothing.

    A slope between 95 and 180 days and one between 95 and 125 are not the same
    measurement, and the grid ACCUMULATES history rather than re-measuring it —
    ``get_grid_cell(history=N)`` tells the caller to compare only where ``window``
    matches, so this label is what makes that check possible. Which is why the
    provenance goes in ``reason`` instead: a window carrying an expiry date or a
    hygiene count would differ on every session and nothing would ever compare.
    """
    if front_dte is None or back_dte is None:
        return "unavailable"
    return f"{front_dte}d vs {back_dte}d"


def _term_leg(chain: _ChainSmile, expiry: date, *, as_of: date) -> _TermLeg | None:
    """The ATM quote at one expiry, off the hygiene-filtered smile for it."""
    smile = chain.at_expiry(expiry)
    if smile is None:
        return None
    quote = smile.atm()
    if quote is None:
        return None
    return _TermLeg(expiry=expiry, dte=(expiry - as_of).days, quote=quote, smile=smile)


def _leg_expiry(expiries: list[date], *, as_of: date, target: int, tolerance: int):
    """The listed expiry closest to ``target`` days out, if one is close enough.

    Returns ``(expiry, dte)`` or ``(None, nearest_dte)`` — the second form is what
    lets the refusal name the DTE it actually reached instead of asserting that
    nothing was near.
    """
    if not expiries:
        return None, None
    nearest = min(expiries, key=lambda expiry: abs((expiry - as_of).days - target))
    dte = (nearest - as_of).days
    if abs(dte - target) > tolerance:
        return None, dte
    return nearest, dte


def _term_absent(reason: str, *, window: str) -> list[Cell]:
    return [
        Cell.absent(indicator, FAMILY_IMPLIED_VOL, reason, window=window, code=REASON_NO_CHAIN)
        for indicator in _TERM_LEAVES
    ]


def _term_structure_cells(inputs: IndicatorInputs, chain: _ChainSmile) -> list[Cell]:
    """Front-versus-back implied vol: the regime signal the level cannot carry.

    ``implied_vol_atm`` is symmetric in time as well as in direction — it says
    how much movement is priced at one tenor and nothing about whether the curve
    is upward-sloping or INVERTED. "Vol is high" and "vol is high and inverted"
    are opposite trades, and until this leaf existed the grid could not tell them
    apart. ``rv_term_slope`` is the realised twin, and the slope here is the same
    statistic — short over long, minus one — so the two can be read against each
    other rather than merely beside each other.

    **Both legs come off the chain or there is no cell.** Unlike the ATM level
    there is no vendor or solved fallback, which is deliberate and is the same
    rule the skew leaf follows for a stronger reason: ``implied_vol_solved`` sits
    in its own column precisely because an inversion of a mid price through a
    European model is not an exchange quote, and a slope with a solved front and
    a quoted back would have a methodology break inside a single number. Reading
    both ends off one hygiene-filtered chain makes that break impossible rather
    than discouraged.

    **Which pair, and why it is declared rather than taken.** A fixed DTE pair is
    not available: the collectors buy only expiries inside
    ``tenor.entry_dte_min``-``entry_dte_max``, so a front month was never
    collected on any name. Each leg is the nearest LISTED expiry to a target in
    ``smile.term_structure``, and a leg further than the configured deviation
    from its target is refused with the DTE it reached. A name with one listed
    expiry in band resolves both targets to the same expiry and gets ``n/a`` —
    never a slope against itself.
    """
    filters = inputs.smile_filters
    if filters is None or not filters.term_front_target_dte or not filters.term_back_target_dte:
        return _term_absent(
            "no smile hygiene was bound, so there are no term-structure targets to read "
            "a slope between",
            window="unavailable",
        )

    front_target = filters.term_front_target_dte
    back_target = filters.term_back_target_dte
    tolerance = filters.term_max_target_deviation_days
    separation = filters.term_min_separation_days
    asked = f"{front_target}d vs {back_target}d"

    expiries = chain.expiries()
    if not expiries:
        return _term_absent(
            f"no option chain with a listed expiry ahead of {inputs.as_of.isoformat()} for "
            f"{inputs.symbol or inputs.contract_id}, so there is no term structure to "
            f"measure.{chain.note}",
            window="unavailable",
        )

    listed = ", ".join(f"{(expiry - inputs.as_of).days}d" for expiry in expiries)
    # Tested BEFORE the per-leg tolerance, because it is a different finding with
    # a different remedy. A name listing one expiry fails the back leg's
    # tolerance too, and reporting that would send a reader looking for a
    # tolerance to widen when what is missing is a second expiry to widen it
    # onto. This is the case the leaf was specified to name.
    if len(expiries) < _MIN_ENDPOINTS:
        return _term_absent(
            f"only one listed expiry ({listed}) is collected for "
            f"{inputs.symbol or inputs.contract_id}, so there is no second point to measure a "
            "slope to. A slope against itself is zero by construction and says nothing about "
            "the curve.",
            window=asked,
        )

    front_expiry, front_reach = _leg_expiry(
        expiries, as_of=inputs.as_of, target=front_target, tolerance=tolerance
    )
    back_expiry, back_reach = _leg_expiry(
        expiries, as_of=inputs.as_of, target=back_target, tolerance=tolerance
    )
    if front_expiry is None or back_expiry is None:
        missing = "front" if front_expiry is None else "back"
        reached = front_reach if front_expiry is None else back_reach
        target = front_target if front_expiry is None else back_target
        return _term_absent(
            f"the {missing} leg wants an expiry within {tolerance}d of {target}d and the "
            f"nearest listed one is {reached}d out. Collected expiries: {listed}. Taking it "
            "anyway would publish a different tenor pair under this leaf's name, which reads "
            "as an ordinary number all the way to the output.",
            window=asked,
        )

    if front_expiry == back_expiry:
        # Reachable only where the two tolerance bands overlap, which the shipped
        # targets do not. Kept because the config decides that, and a rule that
        # holds by arithmetic somebody could change is a rule worth stating.
        return _term_absent(
            f"both legs resolve to the same listed expiry ({front_reach}d), the only one "
            f"within {tolerance}d of either target. Collected expiries: {listed}. A slope "
            "against itself is zero by construction and says nothing about the curve.",
            window=asked,
        )

    front_dte = (front_expiry - inputs.as_of).days
    back_dte = (back_expiry - inputs.as_of).days
    window = _term_window(front_dte, back_dte)
    if back_dte - front_dte < separation:
        return _term_absent(
            f"the two legs are {back_dte - front_dte}d apart, below the {separation}d floor in "
            "smile.term_structure.min_separation_days. A slope measured across too short a "
            "span is not comparable with one measured across a real term, and the grid "
            "accumulates these rows rather than re-measuring them.",
            window=window,
        )

    front = _term_leg(chain, front_expiry, as_of=inputs.as_of)
    back = _term_leg(chain, back_expiry, as_of=inputs.as_of)
    if front is None or back is None:
        which = "front" if front is None else "back"
        expiry = front_expiry if front is None else back_expiry
        return _term_absent(
            f"the {which} leg at {expiry.isoformat()} yielded no ATM implied vol: either no "
            "strike survived smile hygiene (spread, book depth, delta band, convexity) or "
            "none carried an implied vol. Both legs have to come off the chain — there is no "
            "vendor or solved fallback here, because a slope mixing a solved end with a "
            f"quoted one has a methodology break inside it.{chain.note}",
            window=window,
        )

    provenance = (
        f"front ATM {front.quote.iv:.4f} at {front.expiry.isoformat()} ({front.dte}d, "
        f"{front.quote.source}); back ATM {back.quote.iv:.4f} at {back.expiry.isoformat()} "
        f"({back.dte}d, {back.quote.source}); both exchange-quoted off the "
        f"{chain.session.isoformat() if chain.session else 'collected'} chain. "
        f"front hygiene: {front.smile.hygiene.summary()}; back hygiene: "
        f"{back.smile.hygiene.summary()}"
    )
    # SUMMED, not merged. These are status -> COUNT maps and `_worst_status`
    # breaks its ties on the count, so `{**front, **back}` would let the back
    # leg's tally overwrite the front's wholesale and report a mostly-frozen
    # pair as live.
    status = _worst_status(
        dict(Counter(front.smile.market_data_statuses) + Counter(back.smile.market_data_statuses))
    )
    points = len(front.smile) + len(back.smile)
    cells = [
        Cell(
            indicator="implied_vol_atm_front",
            family=FAMILY_IMPLIED_VOL,
            value=front.quote.iv,
            window=window,
            units=UNIT_DECIMAL,
            reason=provenance,
            n_obs=len(front.smile),
            market_data_status=status,
        ),
        Cell(
            indicator="implied_vol_atm_back",
            family=FAMILY_IMPLIED_VOL,
            value=back.quote.iv,
            window=window,
            units=UNIT_DECIMAL,
            reason=provenance,
            n_obs=len(back.smile),
            market_data_status=status,
        ),
    ]
    if back.quote.iv <= 0:
        cells.append(
            Cell.absent(
                "implied_vol_term_slope",
                FAMILY_IMPLIED_VOL,
                f"the back leg's ATM implied vol is {back.quote.iv:.4f}, which cannot be a "
                "denominator. The two levels beside this leaf are still measured.",
                window=window,
                code=REASON_NO_CHAIN,
            )
        )
    else:
        cells.append(
            Cell(
                indicator="implied_vol_term_slope",
                family=FAMILY_IMPLIED_VOL,
                # Short over long minus one, exactly as rv_term_slope computes it,
                # so the implied and realised term structures are the same
                # statistic and a reader can subtract one from the other.
                value=front.quote.iv / back.quote.iv - 1.0,
                window=window,
                units=UNIT_RATIO,
                reason=provenance,
                n_obs=points,
                market_data_status=status,
            )
        )
    return cells


def _skew_cell(inputs: IndicatorInputs, chain: _ChainSmile) -> Cell:
    """The risk reversal, or `n/a` naming the deltas the band could actually reach.

    **Off the hygiene-filtered smile, never the raw chain.** The same spread,
    book-depth, delta-band and convexity filters that decide whether a position is
    measurable decide this: a skew computed off a strike hygiene dropped is not a
    skew, and running two standards would make the grid and the risk engine
    disagree about the same instrument on the same day.

    **Never a silent substitution.** The target |delta| has to sit inside the band
    on BOTH sides, and the strike is interpolated between the two points either
    side of it. Where it does not, this refuses and says what each side reached —
    taking the nearest listed strike instead would publish a 45-delta reading
    under the name of a 25-delta one, which reads as an ordinary number all the
    way to the output.
    """
    target = inputs.smile_filters.skew_delta if inputs.smile_filters is not None else None
    if not target:
        return Cell.absent(
            "implied_vol_skew",
            FAMILY_IMPLIED_VOL,
            "no smile hygiene was bound, so there is no delta target to read a skew at",
            code=REASON_NO_CHAIN,
        )

    window = f"{target:g}-delta risk reversal"
    smile = chain.build()
    if smile is None:
        return Cell.absent(
            "implied_vol_skew",
            FAMILY_IMPLIED_VOL,
            (
                f"no usable option chain for {inputs.symbol or inputs.contract_id}, so there is "
                f"no smile to read a skew off. Unlike the ATM level this leaf has no vendor "
                f"or solved fallback: a risk reversal is a statement about TWO strikes and "
                f"only the chain carries them.{chain.note}"
            ),
            window=window,
            code=REASON_NO_CHAIN,
        )

    quote = smile.risk_reversal(target)
    if quote is None:
        return Cell.absent(
            "implied_vol_skew",
            FAMILY_IMPLIED_VOL,
            (
                f"the collected band does not reach |delta| {target:g} on both sides of "
                f"{chain.where()}, so no risk reversal can be read without substituting a "
                f"nearer strike for the one asked for. {smile.delta_reach().describe()}; "
                f"{smile.hygiene.summary()}. Widen "
                f"collectors.chain.max_strikes_per_expiry, or read the ATM level alone."
            ),
            window=window,
            code=REASON_NO_CHAIN,
        )

    return Cell(
        indicator="implied_vol_skew",
        family=FAMILY_IMPLIED_VOL,
        value=quote.value,
        window=f"{quote.provenance()} on {chain.where()}; {smile.hygiene.summary()}",
        units=UNIT_DECIMAL,
        n_obs=quote.n_points,
        market_data_status=_worst_status(smile.market_data_statuses),
    )


def iv_percentile(inputs: IndicatorInputs) -> list[Cell]:
    """IV percentile over 52 weeks, from the vendor or self-computed.

    Precedence follows ``data.iv_percentile_source``. The vendor figure has been
    proven unreliable for broad ETFs — it reported S&P 500 implied vol at the
    hundredth percentile while VIX sat near a three-month low — so ``both``
    reports each and the divergence between them.
    """
    window = f"{inputs.iv_percentile_window_weeks}w"
    if inputs.options_unavailable_reason:
        reason = _no_options_reason(inputs, "Indicator 8")
        return [
            Cell.inapplicable(
                indicator, FAMILY_IV_PERCENTILE, reason, window=window, code=REASON_NO_OPTIONS
            )
            for indicator in _iv_percentile_leaves(inputs.iv_percentile_source)
        ]
    vendor, vendor_reason = _vendor_iv_percentile(inputs)
    self_value, self_reason = _self_iv_percentile(inputs)
    source = inputs.iv_percentile_source

    cells: list[Cell] = []
    if source in (SOURCE_VENDOR, SOURCE_BOTH):
        cells.append(
            _percentile_cell(
                "iv_percentile_vendor",
                vendor,
                vendor_reason,
                window,
                code=REASON_NO_IV_PERCENTILE,
            )
        )
    if source in (SOURCE_SELF, SOURCE_BOTH):
        cells.append(
            _percentile_cell(
                "iv_percentile_self",
                self_value,
                self_reason,
                window,
                code=REASON_NO_IV_PERCENTILE,
            )
        )

    chosen = vendor if source == SOURCE_VENDOR else self_value
    chosen_reason = vendor_reason if source == SOURCE_VENDOR else self_reason
    if source == SOURCE_BOTH:
        chosen = vendor if vendor is not None else self_value
        chosen_reason = vendor_reason if vendor is not None else self_reason
    cells.append(
        _percentile_cell(
            "iv_percentile", chosen, chosen_reason, window, code=REASON_NO_IV_PERCENTILE
        )
    )

    if source == SOURCE_BOTH:
        if vendor is None or self_value is None:
            cells.append(
                Cell.absent(
                    "iv_percentile_divergence",
                    FAMILY_IV_PERCENTILE,
                    "both sources are required to measure a divergence and one is absent",
                    window=window,
                    code=REASON_NO_IV_PERCENTILE,
                )
            )
        else:
            cells.append(
                Cell(
                    indicator="iv_percentile_divergence",
                    family=FAMILY_IV_PERCENTILE,
                    value=vendor - self_value,
                    window=window,
                    units=UNIT_PERCENTILE,
                )
            )
    return cells


def _percentile_cell(
    indicator: str, value: float | None, reason: str, window: str, *, code: str
) -> Cell:
    if value is None:
        return Cell.absent(indicator, FAMILY_IV_PERCENTILE, reason, window=window, code=code)
    return Cell(
        indicator=indicator,
        family=FAMILY_IV_PERCENTILE,
        value=value,
        window=window,
        units=UNIT_PERCENTILE,
    )


def iv_hv_spread(inputs: IndicatorInputs) -> list[Cell]:
    """Implied minus realised at the reference tenor.

    Materially negative, with implied vol in the bottom quartile of its own
    range, is the definition of a cheap option. Both conditions, always.
    """
    tenor = _reference_tenor(inputs)
    window = f"IV vs {tenor}d RV"
    if inputs.options_unavailable_reason:
        return [
            Cell.inapplicable(
                "iv_minus_hv",
                FAMILY_IV_HV_SPREAD,
                _no_options_reason(inputs, "Indicator 9"),
                window=window,
                code=REASON_NO_OPTIONS,
            )
        ]
    implied, implied_reason, implied_status = _atm_implied_vol(inputs)
    realised = _realised_vol(inputs.daily.close, tenor, inputs.trading_days_per_year)

    if implied is None:
        return [
            Cell.absent(
                "iv_minus_hv",
                FAMILY_IV_HV_SPREAD,
                implied_reason,
                window=window,
                code=REASON_NO_CHAIN,
            )
        ]
    if realised is None:
        return [
            Cell.absent(
                "iv_minus_hv",
                FAMILY_IV_HV_SPREAD,
                f"fewer than {tenor + 1} usable daily closes, so there is no realised vol "
                "to compare against",
                window=window,
                code=REASON_SHORT_SAMPLE,
            )
        ]
    return [
        Cell(
            indicator="iv_minus_hv",
            family=FAMILY_IV_HV_SPREAD,
            value=implied - realised,
            window=window,
            units=UNIT_DECIMAL,
            market_data_status=implied_status,
        )
    ]


# ─────────────────────────  10. correlation  ─────────────────────────


def correlation_to_benchmark(inputs: IndicatorInputs) -> list[Cell]:
    """Correlation to the global risk benchmark, and its shift from its own norm.

    Correlation CHANGE matters more than level: a market decoupling from its
    usual driver is a regime signal, and a pair that always sits at 0.9 and one
    that never correlates both deviate meaningfully from their own history.
    """
    cells: list[Cell] = []
    is_the_benchmark = (
        inputs.contract_id is not None and inputs.contract_id == inputs.benchmark_contract_id
    )
    if inputs.benchmark_contract_id is None or is_the_benchmark:
        # Two different absences shared one code, and only one of them is a
        # collection failure. A cell that IS the benchmark can never correlate
        # to itself informatively — no collector will ever fill it — so it was
        # counted against coverage every single day.
        reason = (
            "this cell IS the benchmark, so a correlation to it carries no information"
            if is_the_benchmark
            else f"the benchmark market {inputs.benchmark_market!r} has no bars in the store"
        )
        code = REASON_IS_BENCHMARK if is_the_benchmark else REASON_NO_BENCHMARK
        for window in inputs.correlation_windows:
            cells.extend(_absent_correlation_pair(f"{window}d", reason, code=code))
        return cells

    own, other = _aligned_returns(inputs.daily, inputs.benchmark_daily)
    floor = inputs.floor("correlation")
    for window in inputs.correlation_windows:
        label = f"{window}d"
        recent_own, recent_other = own[-window:], other[-window:]
        if len(recent_own) < max(floor, window // _HALF):
            reason = (
                f"only {len(recent_own)} overlapping return(s) against the benchmark in "
                f"the {label} window, below the {floor} floor"
            )
            cells.extend(_absent_correlation_pair(label, reason, code=REASON_SHORT_SAMPLE))
            continue

        value = correlation(recent_own, recent_other)
        if value is None:
            reason = "one of the two series has no dispersion in this window"
            cells.extend(_absent_correlation_pair(label, reason, code=REASON_NO_DISPERSION))
            continue

        cells.append(
            Cell(
                indicator=f"correlation_{label}",
                family=FAMILY_CORRELATION,
                value=value,
                # Admitted at half the window, so the label has to say how many
                # overlapping returns are really behind it. rankings.py requires
                # the FULL window for the same pair, and without this the two
                # disagree under one name with nothing to show why.
                window=_window_label(label, recent_own, noun="overlapping returns"),
                units=UNIT_RATIO,
                n_obs=len(recent_own),
            )
        )
        shift, shift_reason = _correlation_shift(own, other, window, inputs)
        norm_window = f"{label} vs its {inputs.correlation_norm_window_years}y norm"
        if shift is None:
            cells.append(
                Cell.absent(
                    f"correlation_shift_{label}",
                    FAMILY_CORRELATION,
                    shift_reason,
                    window=norm_window,
                    code=REASON_NO_DISPERSION,
                )
            )
        else:
            cells.append(
                Cell(
                    indicator=f"correlation_shift_{label}",
                    family=FAMILY_CORRELATION,
                    value=shift,
                    window=norm_window,
                    units=UNIT_RATIO,
                )
            )
    return cells


# ─────────────────────────  11. currency  ─────────────────────────


def currency_return(inputs: IndicatorInputs) -> list[Cell]:
    """The local currency against the base, over twelve months.

    Positive is the local currency appreciating. This is the leg that turns a
    local nominal return into a base nominal one, reported on its own so the
    reader can see how much of a base return was the market and how much was FX.
    """
    label = "12m"
    days = round(inputs.calendar_days_per_year)
    fx_series = inputs.market_spec.get("fx_series")
    if not fx_series:
        # The base currency itself declares no FX series, and a market outside the
        # FX family declares none on purpose. Either way nothing is coming.
        return [
            Cell.inapplicable(
                "currency_return_12m",
                FAMILY_CURRENCY_RETURN,
                f"{inputs.market} declares no fx_series in universe.yaml. For the base "
                "currency there is no rate to read; elsewhere the gap is declared rather "
                "than filled with a neighbour's cross.",
                window=label,
                code=REASON_NO_FX_DECLARED,
            )
        ]
    ratio = _fx_ratio(inputs, days, {})
    if ratio is None:
        return [
            Cell.absent(
                "currency_return_12m",
                FAMILY_CURRENCY_RETURN,
                f"FX series {fx_series} is declared but absent from the store",
                window=label,
                code=REASON_NO_FX,
            )
        ]
    return [
        Cell(
            indicator="currency_return_12m",
            family=FAMILY_CURRENCY_RETURN,
            value=ratio - 1.0,
            window=label,
            units=UNIT_DECIMAL,
        )
    ]


# ─────────────────────────  12-13. rates  ─────────────────────────


def policy_rate(inputs: IndicatorInputs) -> list[Cell]:
    """The local policy rate, and the same rate net of local inflation."""
    series_id = inputs.market_spec.get("policy_rate_series")
    if not series_id:
        reason = (
            f"{inputs.market} declares no policy_rate_series in universe.yaml. A "
            "neighbouring country's rate is not a substitute, so the gap is declared "
            "rather than filled."
        )
        return [
            Cell.inapplicable(
                "policy_rate", FAMILY_POLICY_RATE, reason, code=REASON_SERIES_UNDECLARED
            ),
            Cell.inapplicable(
                "policy_rate_real", FAMILY_POLICY_RATE, reason, code=REASON_SERIES_UNDECLARED
            ),
        ]
    nominal = inputs.latest_value(series_id)
    if nominal is None:
        reason = (
            f"policy rate series {series_id} is declared but absent from the store. A "
            "neighbouring country's rate is not a substitute."
        )
        # "Absent" and "present but unalignable" are different gaps with different
        # remedies — collect it, versus declare its release rule — and saying the
        # first when the second is true sends the reader to the wrong place. Three
        # markets read "absent" for a series holding 3,652 current rows.
        unaligned = _unalignable_note(inputs, series_id)
        if unaligned:
            reason = unaligned
        return [
            Cell.absent("policy_rate", FAMILY_POLICY_RATE, reason, code=REASON_SERIES_ABSENT),
            Cell.absent("policy_rate_real", FAMILY_POLICY_RATE, reason, code=REASON_SERIES_ABSENT),
        ]

    cells = [
        Cell(
            indicator="policy_rate",
            family=FAMILY_POLICY_RATE,
            value=nominal,
            window=str(series_id),
            units=UNIT_DECIMAL,
        )
    ]
    cpi = _cpi_yoy(inputs)
    if cpi is None:
        cpi_id = inputs.market_spec.get("cpi_series")
        rebased = _rebased_cpi_note(inputs)
        cells.append(
            Cell.absent(
                "policy_rate_real",
                FAMILY_POLICY_RATE,
                rebased
                or _unalignable_note(inputs, cpi_id)
                or (
                    f"CPI series {cpi_id} is absent, so the real rate cannot be computed"
                    if cpi_id
                    # A DECLARED gap reads differently from a collection one: the
                    # market names no CPI because no source this build can read
                    # point-in-time publishes one, and universe.yaml carries the
                    # reason per market. "absent" would send a reader to the
                    # collector logs looking for a run that was never supposed to
                    # happen.
                    else (
                        f"{inputs.market} declares no cpi_series in universe.yaml, which "
                        "states why: no source this build reads publishes a CPI for it "
                        "with a per-observation release date. A neighbour's inflation "
                        "rate is not this market's."
                    )
                ),
                code=REASON_MIXED_UNITS if rebased else REASON_SERIES_ABSENT,
            )
        )
    else:
        cpi_id = inputs.market_spec.get("cpi_series")
        # The vintage travels, because a real rate is a DIFFERENCE and the reader
        # cannot otherwise see that one side of it is years older than the other.
        vintage = _cpi_vintage(inputs)
        window = f"{series_id} less {cpi_id}"
        if vintage is not None:
            window = f"{window} (CPI printed {vintage.isoformat()})"
        real = Cell(
            indicator="policy_rate_real",
            family=FAMILY_POLICY_RATE,
            value=nominal - cpi,
            window=window,
            units=UNIT_DECIMAL,
        )
        # A fresh policy rate minus a stale inflation print is not a real rate,
        # and it is the shape that hides: the nominal side updates daily, so the
        # cell looks current whatever the CPI is doing. The builder's own macro
        # freshness cannot catch it, because it takes the NEWEST release across a
        # market's series and the FX rate is daily.
        #
        # This fired on eleven markets for as long as the OECD CPALTT01 family
        # FRED served them from kept not printing. Those series are gone as of
        # Phase 9 and no market reads a dead id any more, so nothing trips it
        # today — which is the reason to keep it rather than to remove it. The
        # next family to stop will do so without announcing itself, and this is
        # what will say so.
        age_window = inputs.freshness_days(INPUT_MACRO)
        if vintage is not None and (inputs.as_of - vintage).days > age_window:
            real = real.stale(
                f"the real rate is today's {series_id} less a {cpi_id} print released "
                f"{vintage.isoformat()}, {(inputs.as_of - vintage).days} days before "
                f"{inputs.as_of} and beyond the {age_window}-day "
                f"grid.freshness_days.{INPUT_MACRO} window. The nominal side is current, "
                "so this cell would otherwise read as current too."
            )
        cells.append(real)
    return cells


def yield_curve(inputs: IndicatorInputs) -> list[Cell]:
    """The local ten-year yield, and the slope — saying WHICH slope.

    A ten-year-minus-two-year slope and a ten-year-minus-policy slope are
    different measurements. The cell carries the label so the two are never
    ranked against each other as if they were the same.
    """
    long_id = inputs.market_spec.get("long_rate_series")
    label = inputs.market_spec.get("slope_label")
    short_id = inputs.market_spec.get("slope_short_series")

    if not long_id:
        reason = (
            f"{inputs.market} declares no long_rate_series in universe.yaml, so there is "
            "no ten-year yield to read and no second point for a slope"
        )
        return [
            Cell.inapplicable(
                "long_rate_10y", FAMILY_YIELD_CURVE, reason, code=REASON_SERIES_UNDECLARED
            ),
            Cell.inapplicable(
                "curve_slope", FAMILY_YIELD_CURVE, reason, code=REASON_SERIES_UNDECLARED
            ),
        ]
    long_rate = inputs.latest_value(long_id)
    if long_rate is None:
        reason = f"long rate series {long_id} is declared but absent from the store"
        return [
            Cell.absent("long_rate_10y", FAMILY_YIELD_CURVE, reason, code=REASON_SERIES_ABSENT),
            Cell.absent("curve_slope", FAMILY_YIELD_CURVE, reason, code=REASON_SERIES_ABSENT),
        ]

    cells = [
        Cell(
            indicator="long_rate_10y",
            family=FAMILY_YIELD_CURVE,
            value=long_rate,
            window=str(long_id),
            units=UNIT_DECIMAL,
        )
    ]
    if not short_id or not label:
        cells.append(
            Cell.inapplicable(
                "curve_slope",
                FAMILY_YIELD_CURVE,
                f"{inputs.market} declares no slope_short_series or no slope_label, so the "
                "slope has no second point and which slope it would be is itself "
                "undeclared. A 10y-minus-2y and a 10y-minus-policy slope are different "
                "measurements and are never ranked together unlabelled.",
                code=REASON_SERIES_UNDECLARED,
            )
        )
        return cells
    short_rate = inputs.latest_value(short_id)
    if short_rate is None:
        cells.append(
            Cell.absent(
                "curve_slope",
                FAMILY_YIELD_CURVE,
                f"short leg {short_id} is declared but absent from the store",
                code=REASON_SERIES_ABSENT,
            )
        )
    else:
        cells.append(
            Cell(
                indicator="curve_slope",
                family=FAMILY_YIELD_CURVE,
                value=long_rate - short_rate,
                window=str(label),
                units=UNIT_DECIMAL,
            )
        )
    return cells


# ─────────────────────────  14. turnover  ─────────────────────────


def turnover(inputs: IndicatorInputs) -> list[Cell]:
    """Recent turnover against its longer-run average.

    Volume leaving its own average is what separates a drift from a move
    somebody is taking part in.
    """
    window = f"{inputs.technical_window_days}d vs the full daily window"
    volumes = [v for v in inputs.daily.volume if v is not None and v > 0]
    floor = inputs.floor("turnover")
    if inputs.asset_class == ASSET_INDEX and not volumes:
        # An index LEVEL is a computed number, not a traded instrument: there is
        # nothing to turn over and no collector will ever supply it. Counting it
        # as a collection gap made several of the measuring instruments look
        # permanently behind, every day, for a reason nothing could fix.
        return [
            Cell.inapplicable(
                "turnover_vs_average",
                FAMILY_TURNOVER,
                f"{inputs.symbol or 'this instrument'} is an index level, which carries no "
                "volume; turnover is not a property it has",
                window=window,
                code=REASON_INDEX_HAS_NO_VOLUME,
            )
        ]
    if len(volumes) < max(floor, inputs.technical_window_days):
        return [
            Cell.absent(
                "turnover_vs_average",
                FAMILY_TURNOVER,
                f"only {len(volumes)} session(s) carry volume, below the "
                f"{max(floor, inputs.technical_window_days)} needed to compare against an "
                "average",
                window=window,
                code=REASON_NO_VOLUME,
            )
        ]
    recent = mean(volumes[-inputs.technical_window_days :])
    baseline = mean(volumes)
    if not recent or not baseline or baseline <= 0:
        return [
            Cell.absent(
                "turnover_vs_average",
                FAMILY_TURNOVER,
                "the volume history holds no usable average",
                window=window,
                code=REASON_NO_VOLUME,
            )
        ]
    return [
        Cell(
            indicator="turnover_vs_average",
            family=FAMILY_TURNOVER,
            value=recent / baseline - 1.0,
            window=window,
            units=UNIT_RATIO,
        )
    ]


# ─────────────────────────  15. positioning  ─────────────────────────

#: The name of the positioning LEVEL leaf. Named rather than repeated, because
#: three sites in this function and one in ``rankings`` have to agree on it.
POSITIONING_LEVEL = "positioning_percentile"


def positioning_change_indicator(weeks: int) -> str:
    """The leaf name for a positioning change over ``weeks`` weeks.

    Built here rather than at each call site so the family that EMITS the leaf
    and the ranked views that CARRY it cannot drift apart when
    ``commodities.cot.change_windows_weeks`` changes. ``rankings`` names the
    windows it wants through this function for the same reason
    ``DIMENSION_INDICATOR_PREFIX`` was deleted: a second spelling of one name is
    a disagreement waiting to happen.
    """
    return f"positioning_change_{weeks}w"


def positioning(inputs: IndicatorInputs) -> list[Cell]:
    """Speculative positioning where a futures contract exists, and its CHANGE.

    The only flow measure in the whole grid; everything else is price-derived.
    Absent by design almost everywhere, and the reason says so rather than
    leaving a hole.

    **The level and the change are different measurements and are not in the
    same units.** The level is a percentile of the net non-commercial share of
    open interest against ``commodities.cot.percentile_window_years`` of its own
    history. The change is that same SHARE differenced over a window in weeks,
    so it is in share-of-open-interest points and reads as "the crowd took two
    more points of open interest this month" — not as a movement in the rank.
    Differencing the percentile instead would rank a past reading against
    today's distribution and call the result a flow.

    Both are needed and neither substitutes for the other: crowding does not time
    a move, and a book being dumped is a different trade from a crowd still
    forming, while a change with no level cannot say whether two points is the
    top of the range or the middle of it.
    """
    series_id = inputs.market_spec.get("cot_series")
    window_years = inputs.config.require_int("commodities.cot.percentile_window_years")
    change_weeks = _change_windows(inputs)
    window = f"{window_years}y"
    if not series_id:
        reason = (
            f"no futures contract with a COT series is declared for {inputs.market}, so "
            "there is no positioning to read. Absent by design, not uncollected."
        )
        return [
            Cell.inapplicable(
                POSITIONING_LEVEL,
                FAMILY_POSITIONING,
                reason,
                window=window,
                code=REASON_NO_POSITIONING,
            ),
            *(
                Cell.inapplicable(
                    positioning_change_indicator(weeks),
                    FAMILY_POSITIONING,
                    reason,
                    window=f"{weeks}w",
                    code=REASON_NO_POSITIONING,
                )
                for weeks in change_weeks
            ),
        ]

    since = inputs.as_of - timedelta(days=round(window_years * inputs.calendar_days_per_year))
    series = macro_series(inputs.store, str(series_id), since=since)
    # Release-aligned. CFTC publishes Tuesday positions on the following Friday
    # and the lag is stored on the row, so reading it on its reference Tuesday
    # would put a report in the percentile three days before the market saw it.
    observations = series.observations_known_at(inputs.as_of)
    values = [o.value for o in observations]
    units = series.units_known_at(inputs.as_of)
    if len(units) > 1:
        # The same refusal covers the change leaves, and for a stronger reason
        # than it covers the percentile: a difference across a units change
        # subtracts a share from a contract count and reports the contract count.
        reason = (
            f"{series_id} carries {len(units)} different units inside the {window} "
            f"window ({', '.join(sorted(units))}). A percentile ranks a value against "
            "others of the same quantity, and a change differences two of them, so this "
            "one would compare two measurements. Re-collect the series so one definition "
            "covers it."
        )
        return [
            Cell.absent(
                POSITIONING_LEVEL,
                FAMILY_POSITIONING,
                reason,
                window=window,
                code=REASON_MIXED_UNITS,
            ),
            *(
                Cell.absent(
                    positioning_change_indicator(weeks),
                    FAMILY_POSITIONING,
                    reason,
                    window=f"{weeks}w",
                    code=REASON_MIXED_UNITS,
                )
                for weeks in change_weeks
            ),
        ]

    floor = inputs.floor("percentile")
    if len(values) < floor:
        level = Cell.absent(
            POSITIONING_LEVEL,
            FAMILY_POSITIONING,
            f"{series_id} holds {len(values)} observation(s) published on or before "
            f"{inputs.as_of} in the {window} window, below the {floor} floor"
            + _unaligned_note(series),
            window=window,
            code=REASON_SHORT_SAMPLE,
        )
    else:
        level = Cell(
            indicator=POSITIONING_LEVEL,
            family=FAMILY_POSITIONING,
            value=percentile_of(values, values[-1]),
            window=f"{window} of {series_id}",
            units=UNIT_PERCENTILE,
            n_obs=len(values),
        )
    # A change needs far less history than a percentile does, so it is computed
    # against its own floor rather than being suppressed with the level: a store
    # holding one quarter of a weekly series can say which way the crowd moved
    # long before it can say where that sits in five years.
    return [
        level,
        *(
            _positioning_change(inputs, series, observations, str(series_id), weeks)
            for weeks in change_weeks
        ),
    ]


def _change_windows(inputs: IndicatorInputs) -> tuple[int, ...]:
    return tuple(int(w) for w in inputs.config.require_list("commodities.cot.change_windows_weeks"))


def _positioning_change(
    inputs: IndicatorInputs,
    series: MacroSeries,
    observations: list[MacroObservation],
    series_id: str,
    weeks: int,
) -> Cell:
    """The change in the net non-commercial share of open interest over ``weeks``.

    Both endpoints are readings PUBLISHED on or before ``as_of``, and the older
    one is the newest reading published on or before the window's start — so the
    span is at least the window it is labelled with and never less. It can be
    more, when the series has a publication gap, which is why the label carries
    the two release dates and ``n_obs`` carries the count: a change over a window
    that quietly stretched is exactly the overstatement ``Cell.n_obs`` exists to
    make visible.
    """
    indicator = positioning_change_indicator(weeks)
    floor = inputs.config.require_int("commodities.cot.change_min_observations")
    label = f"{weeks}w"

    def absent(reason: str) -> Cell:
        return Cell.absent(
            indicator, FAMILY_POSITIONING, reason, window=label, code=REASON_SHORT_SAMPLE
        )

    if not observations:
        return absent(
            f"{series_id} holds no reading published on or before {inputs.as_of}, so there "
            f"is no {label} change to measure" + _unaligned_note(series)
        )

    # By RELEASE, not by reference period. `observations_known_at` guarantees
    # every one of these carries a release date and orders them by the period
    # they describe, and for CFTC the two orders agree — but a revision or a
    # batched publication would break that, and the endpoints of a change are
    # defined by when the market could see them.
    published = sorted(observations, key=lambda o: (o.release_date or date.min, o.reference_date))
    start = inputs.as_of - timedelta(weeks=weeks)
    earlier = [o for o in published if o.release_date is not None and o.release_date <= start]
    if not earlier:
        return absent(
            f"{series_id} holds no reading published on or before {start}, so a {label} "
            f"change would be measured over less than {label}. The oldest reading available "
            f"was published {published[0].release_date}" + _unaligned_note(series)
        )

    opening = earlier[-1]
    latest = published[-1]
    sample = [
        o
        for o in published
        if o.release_date is not None and o.release_date >= opening.release_date
    ]
    if len(sample) < floor:
        return absent(
            f"{series_id} holds {len(sample)} weekly reading(s) between "
            f"{opening.release_date} and {latest.release_date}, below the {floor} a change "
            "needs. A delta over two readings is a difference, not a trend"
            + _unaligned_note(series)
        )
    if opening.release_date == latest.release_date:
        return absent(
            f"{series_id} has published nothing since {latest.release_date}, so both ends of "
            f"the {label} window are the same reading"
        )

    return Cell(
        indicator=indicator,
        family=FAMILY_POSITIONING,
        value=latest.value - opening.value,
        window=(
            f"{label} of {series_id} ({len(sample)} weekly readings, "
            f"{opening.release_date} to {latest.release_date}, by release)"
        ),
        units=UNIT_DECIMAL,
        n_obs=len(sample),
    )


#: Every family, in SKILL.md's order. build_grid walks this and nothing else, so
#: adding an indicator means adding it here and the count changes visibly.
INDICATOR_FUNCTIONS = (
    returns,
    distance_from_high,
    distance_from_low,
    drawdown,
    realised_vol,
    rv_term_structure,
    implied_vol,
    iv_percentile,
    iv_hv_spread,
    correlation_to_benchmark,
    currency_return,
    policy_rate,
    yield_curve,
    turnover,
    positioning,
)


def all_cells(inputs: IndicatorInputs) -> list[Cell]:
    """Every leaf cell for one market x sector."""
    cells: list[Cell] = []
    for function in INDICATOR_FUNCTIONS:
        cells.extend(function(inputs))
    return cells


# ─────────────────────────  helpers  ─────────────────────────

#: Two endpoints, the arithmetic minimum for a return.
_MIN_ENDPOINTS = 2

#: A correlation window must be at least half populated before it is computed;
#: the other half being missing is a different measurement wearing the same name.
_HALF = 2


def _tail(values: list[float], sessions: int) -> list[float]:
    """The last ``sessions`` returns worth of closes, so ``sessions + 1`` prices."""
    if len(values) <= sessions:
        return list(values)
    return values[-(sessions + 1) :]


def _unaligned_note(series: MacroSeries) -> str:
    """Name the observations dropped for want of a release date, or say nothing.

    A series that cannot be aligned point-in-time is a coverage gap, not a free
    pass. Without this the cell reads as an ordinary short sample and nobody
    learns that the sample is short BECAUSE the source states no publication
    date — which is a different problem with a different remedy.
    """
    unaligned = series.unaligned_count
    if not unaligned:
        return ""
    return (
        f" ({unaligned} observation(s) carry no release date and cannot be aligned point-in-time)"
    )


def _window_label(intended: str, values: list[float], *, noun: str = "bars") -> str:
    """Name the window AND the sample actually behind it.

    A range measured over thirty weekly bars is a real reading, but printing it
    as "52w" claims a high the sample never contained. The floor in
    ``grid.min_observations.percentile`` decides whether it is computed at all;
    this makes sure the label never overstates what it was computed on.

    ``noun`` names what is being counted, because the unit differs by family: a
    range counts bars, a volatility percentile counts rolling readings, and a
    correlation counts the returns on which both series traded.
    """
    return f"{intended} ({len(values)} {noun})"


def _weeks(inputs: IndicatorInputs, count: int) -> list[float]:
    weekly = inputs.weekly.close
    return weekly[-count:] if len(weekly) > count else list(weekly)


def _year_to_date(stamps: list[str], closes: list[float], as_of: date) -> list[float]:
    """Closes from the last session of the previous year onward.

    Starting at the prior year-end rather than the first session of January: a
    year-to-date return measures from the close the year started at.
    """
    boundary = date(as_of.year, 1, 1).isoformat()
    start_index = 0
    for index, stamp in enumerate(stamps):
        if stamp[: len(boundary)] >= boundary:
            start_index = max(index - 1, 0)
            break
    else:
        return []
    return closes[start_index:]


def _fx_ratio(
    inputs: IndicatorInputs, calendar_days: int, cache: dict[str, float | None]
) -> float | None:
    """Memoised :func:`obliskiq.compute.frames.fx_ratio` for one cell.

    The returns family asks for five horizons on one market, so the same series
    would otherwise be read five times per cell.
    """
    key = str(calendar_days)
    if key not in cache:
        cache[key] = fx_ratio(
            inputs.store,
            inputs.market_spec,
            as_of=inputs.as_of,
            horizon_days=calendar_days,
            base_currency=inputs.config.require_str("account.base_currency"),
        )
    return cache[key]


def _unalignable_note(inputs: IndicatorInputs, series_id: Any) -> str | None:
    """Say so when a series is in the store but carries no release date.

    ``known_at`` excludes an observation with no release date rather than
    assuming it was public on its reference day, so such a series reads as
    missing. The remedy is a declared ``release_date_policy``, not a collection
    run, and the reason has to point at the right one.
    """
    series = inputs.series(series_id)
    if series is None or not series.unaligned_count:
        return None
    if series.unaligned_count < len(series):
        return None
    return (
        f"{series_id} holds {len(series)} observations and NONE carries a release "
        "date, so none can be read point-in-time. The series is collected; what is "
        "missing is its macro.series release_date_policy. It is not absent."
    )


def _cpi_vintage(inputs: IndicatorInputs) -> date | None:
    """When the CPI print behind the real rate was RELEASED, not what it describes.

    ``known_at`` already refuses an observation with no release date, so this is
    the same reading the value came from rather than a second, looser one.
    """
    series = inputs.series(inputs.market_spec.get("cpi_series"))
    if series is None:
        return None
    observation = series.known_at(inputs.as_of)
    return None if observation is None else observation.release_date


def _rebased_cpi_note(inputs: IndicatorInputs) -> str | None:
    """Say so when the two ends of the year-on-year ratio measure on different bases.

    A rebase is the one way a CPI series can be fully collected, fully aligned and
    still unusable, and it is the one way that reads as an ordinary number: the
    ratio comes out near -22% and carries a fresh release date. Named separately
    from "absent" because the remedy is different — re-collect the series on one
    vintage, not wait for a print.
    """
    spec = inputs.market_spec
    series_id = spec.get("cpi_series")
    if not series_id or bool(spec.get("cpi_is_yoy", True)):
        return None
    series = inputs.series(series_id)
    if series is None:
        return None
    latest = series.known_at(inputs.as_of)
    year_ago = series.known_at(inputs.as_of - timedelta(days=round(inputs.calendar_days_per_year)))
    if latest is None or year_ago is None or not mixed_cpi_units(latest, year_ago):
        return None
    return (
        f"{series_id} measures {latest.units} at {latest.reference_date} and "
        f"{year_ago.units} a year earlier, so a year-on-year ratio of the two would "
        "divide one unit by another and report it as inflation. The publisher rebased "
        "the index; re-collect the series so one base covers the window."
    )


def _cpi_yoy(inputs: IndicatorInputs) -> float | None:
    return cpi_yoy(
        inputs.store,
        inputs.market_spec,
        as_of=inputs.as_of,
        calendar_days_per_year=inputs.calendar_days_per_year,
    )


def _reference_tenor(inputs: IndicatorInputs) -> int:
    """The tenor realised vol is reported at, from ``data.strike_bounds_rv_tenor_days``."""
    return inputs.config.require_int("data.strike_bounds_rv_tenor_days")


def _realised_vol(closes: list[float], tenor: int, trading_days_per_year: float) -> float | None:
    """Annualised realised vol, from the collectors' own implementation.

    Reused rather than reimplemented: the chain collector sizes its strike bands
    with this function, and a grid that measured volatility even slightly
    differently would put the band and the cell out of agreement with no way to
    tell which was right.
    """
    return annualised_realised_vol(
        closes, tenor_days=tenor, trading_days_per_year=trading_days_per_year
    )


def _rolling(
    values: list[Any], span: int, measure: Callable[[list[Any]], float | None], *, step: int = 1
) -> list[float]:
    """Every reading ``measure`` yields, stepping ``step`` items between windows.

    One implementation for all three estimators' percentiles. Each step slices
    only the window it needs; slicing the whole prefix instead would copy the
    entire history once per reading, which across every grid cell is most of the
    build. A window ``measure`` declines contributes nothing rather than a zero.

    ``step`` decides what the resulting count MEANS. Stepping one item at a time
    makes two consecutive windows share all but one observation, so a hundred
    readings are not a hundred pieces of evidence; passing ``step=span`` (or the
    tenor, where the span carries a boundary item the readings do not share)
    gives readings built on disjoint data. The walk runs BACKWARDS from the
    newest observation so the final window always ends on it — forwards, a
    stride that does not divide the history would leave the most recent sessions
    out of it, which is the one window a percentile of today's reading cannot do
    without. :func:`obliskiq.compute.stats.rolling_correlations` samples its own
    history the same way and for the same reason.
    """
    out: list[float] = []
    stride = max(1, step)
    for end in reversed(range(len(values), span - 1, -stride)):
        value = measure(values[end - span : end])
        if value is not None:
            out.append(value)
    return out


def _rolling_realised_vol(
    closes: list[float], tenor: int, trading_days_per_year: float
) -> list[float]:
    """The NON-OVERLAPPING ``tenor``-day close-to-close readings, for a percentile.

    ``span`` is ``tenor + 1`` because n returns need n + 1 prices. The range
    estimators read one term per bar and so slide over ``tenor`` items — the one
    place the three estimators genuinely differ in shape, and the reason this
    wrapper exists rather than every caller passing its own span.

    ``step`` is the tenor and not the span, which is the same distinction from
    the other side: consecutive windows share their boundary CLOSE and no RETURN
    at all, and it is the returns the volatility is computed from. Stepping by
    ``tenor + 1`` would skip a session between readings for no gain.
    """
    usable = [c for c in closes if c is not None and c > 0]
    return _rolling(
        usable,
        tenor + 1,
        lambda window: _realised_vol(window, tenor, trading_days_per_year),
        step=tenor,
    )


def _aligned_returns(left: BarSeries, right: BarSeries) -> tuple[list[float], list[float]]:
    """Log returns on the sessions BOTH series traded.

    Aligning on the intersection matters across time zones and holidays: pairing
    by position would compare a Tokyo Monday with a New York Friday.
    """
    right_by_day = {
        stamp[:_DATE_LENGTH]: close for stamp, close in zip(right.ts, right.close, strict=True)
    }
    own: list[float] = []
    other: list[float] = []
    for stamp, close in zip(left.ts, left.close, strict=True):
        match = right_by_day.get(stamp[:_DATE_LENGTH])
        if match is None:
            continue
        own.append(close)
        other.append(match)
    return paired_log_returns(own, other)


#: "YYYY-MM-DD".
_DATE_LENGTH = 10


def _absent_correlation_pair(label: str, reason: str, *, code: str) -> list[Cell]:
    """A correlation and its shift are absent together or not at all.

    Structural reasons produce INAPPLICABLE cells and everything else produces
    absent ones, decided by the code rather than by the caller: the two answer
    different questions — "no collector could ever fill this" against "something
    is owed and has not arrived" — and only the second belongs in the coverage
    denominator.
    """
    build = Cell.inapplicable if code in INAPPLICABLE_CODES else Cell.absent
    return [
        build(f"correlation_{label}", FAMILY_CORRELATION, reason, window=label, code=code),
        build(f"correlation_shift_{label}", FAMILY_CORRELATION, reason, window=label, code=code),
    ]


def _correlation_shift(
    own: list[float], other: list[float], window: int, inputs: IndicatorInputs
) -> tuple[float | None, str]:
    """The current correlation against the pair's OWN norm, in standard deviations.

    A pair that always sits at 0.9 and one that never correlates both deviate
    meaningfully from their own history, which a single global threshold cannot
    see.
    """
    norm_sessions = round(inputs.correlation_norm_window_years * inputs.trading_days_per_year)
    # NON-OVERLAPPING readings. Sampled every session, two consecutive windows
    # share all but one observation, so the series is smooth by construction and
    # its standard deviation measures that smoothness rather than the spread of
    # the correlation — which inflates every z-score computed from it. Stepping
    # by the window buys independence at the cost of readings, and the floor
    # below is what says when there are too few left to divide by.
    history = rolling_correlations(own, other, window=window, sample=norm_sessions, step=window)

    floor = inputs.correlation_norm_min_independent
    if len(history) < floor:
        return None, (
            f"only {len(history)} independent {window}-day correlation(s) fit in the "
            f"{inputs.correlation_norm_window_years}y norm window, below the {floor} floor "
            "in grid.correlation_norm_min_independent. Overlapping readings would give a "
            "standard deviation of their own smoothness and a z-score inflated by it, so "
            "the shift is reported absent rather than computed on one"
        )
    deviation = norm_deviation(history)
    if deviation is None:
        return None, "the pair's own correlation history has no dispersion to measure against"
    return deviation.z_score, ""


def _atm_implied_vol(
    inputs: IndicatorInputs, *, chain: _ChainSmile | None = None
) -> tuple[float | None, str, str | None]:
    """At-the-money implied vol, from whichever source ``data.atm_iv_prefer`` ranks first.

    Three sources can answer, and they are NOT equivalent:

    * the **chain smile** — exchange-quoted implied vols off ``chain_snapshots``,
      passed through the same hygiene the zombie test uses, because a smile too
      thin or too disorderly to price an exit off is equally unfit to publish as
      a grid cell, and running two standards would make the grid and the risk
      engine disagree about the same instrument on the same day;
    * the **vendor snapshot** — a figure the route served for the underlying;
    * the **solve** — one this repository inverted out of a mid price through a
      European model, on instruments that are American-style.

    The order used to be implicit: chain, then vendor snapshot, decided by which
    branch ran first. That was harmless while both were quotes. It stops being
    harmless once a solved number exists, so the order is now read from config and
    the default is ``vendor`` — an exchange-quoted figure outranks an inversion.

    Each source is read only if the ordering actually reaches it, and the two that
    come off the underlying snapshot share ONE read of it. This runs once per cell
    per family across the whole market x sector board, so reading all three
    eagerly would spend two extra store round trips on every cell the first
    source already answered.

    Returns the value, the provenance, and the market-data state of whatever it
    was read from.
    """
    if inputs.contract_id is None:
        return None, "no instrument is mapped to this cell", None

    chain = chain if chain is not None else _ChainSmile(inputs)
    hygiene_note = ""
    cached: list[Any] = []

    def latest_snapshot() -> Any:
        if not cached:
            rows = underlying_snapshots(
                inputs.store, inputs.contract_id, until=inputs.as_of, limit=1
            )
            cached.append(rows[-1] if rows else None)
        return cached[0]

    def read_chain() -> tuple[float | None, str, str | None] | None:
        nonlocal hygiene_note
        found, note = _chain_atm_implied_vol(chain)
        hygiene_note = note
        return found

    def read_vendor() -> tuple[float | None, str, str | None] | None:
        return _vendor_atm_implied_vol(latest_snapshot())

    def read_solved() -> tuple[float | None, str, str | None] | None:
        return _solved_atm_implied_vol(latest_snapshot())

    if inputs.atm_iv_prefer == ATM_IV_PREFER_SOLVED:
        order = (read_solved, read_chain, read_vendor)
    elif inputs.atm_iv_prefer == ATM_IV_PREFER_CHAIN_FIRST:
        # The solve is chain-derived, so "whatever the chain yields" puts it
        # second and the underlying snapshot last.
        order = (read_chain, read_solved, read_vendor)
    else:
        order = (read_chain, read_vendor, read_solved)

    for read in order:
        candidate = read()
        if candidate is not None:
            return candidate

    # Every ordering contains the chain, so by here it has been attempted and the
    # note describes why it produced nothing. No second read to find that out.
    #
    # "No option chain" and "a chain that carries no implied vol" are different
    # gaps with different remedies — reach the instrument, versus collect a
    # priced session — and the first was being reported for both. Measured
    # 2026-09-11: QQQ had 466 chain rows in the store and this said "no option
    # chain for QQQ". The count is already in hand from the attempt above.
    collected = _chain_row_count(inputs)
    if collected:
        return (
            None,
            (
                f"{inputs.symbol or inputs.contract_id} HAS a collected chain "
                f"({collected} contracts on the newest session) but no usable implied "
                "vol: no contract carried one, no vendor figure was served, and none "
                "was solved. The chain is reachable; what is missing is a priced "
                f"session to read or invert.{hygiene_note}"
            ),
            None,
        )
    return (
        None,
        (
            f"no option chain, no vendor implied vol and no solved implied vol for "
            f"{inputs.symbol or inputs.contract_id}. Indicator 7 exists only where a chain "
            f"does.{hygiene_note}"
        ),
        None,
    )


def _chain_row_count(inputs: IndicatorInputs) -> int:
    """How many contracts the newest collected session holds for this instrument.

    Only to tell "no chain was reachable" from "a chain was collected and carried
    no implied vol". Nothing is computed from the number.
    """
    if inputs.contract_id is None:
        return 0
    session = latest_chain_date(inputs.store, inputs.contract_id, until=inputs.as_of)
    if session is None:
        return 0
    return len(chain_rows(inputs.store, underlying_id=inputs.contract_id, snapshot_date=session))


@dataclass
class _ChainSmile:
    """The hygiene-filtered smile for one cell, built at most once.

    Two leaves of family 7 read the same smile — the ATM level and the skew — and
    building it twice would spend a second chain read on every cell of the board
    for a set of rows already in hand. It is lazy rather than eager for the reason
    ``_atm_implied_vol`` reads its sources one at a time: an ordering that answers
    from the vendor snapshot must not pay for a chain query it never used.
    """

    inputs: IndicatorInputs
    _built: bool = False
    _rows_read: bool = False
    _rows: list[Any] = field(default_factory=list)
    _per_expiry: dict[date, Smile | None] = field(default_factory=dict)
    smile: Smile | None = None
    session: date | None = None
    expiry: date | None = None
    tenor: int | None = None
    #: Why a chain that exists yielded no smile. Produced on the read that has the
    #: rows, rather than by re-reading the chain on the failure path.
    note: str = ""

    def rows(self) -> list[Any]:
        """The newest collected session's chain, read once per cell.

        Four leaves of family 7 want these rows — the ATM level, the skew, and
        both legs of the term structure — and every one of them is computed for
        every market x sector cell on the board. Reading the chain per leaf would
        multiply one query by four across the whole grid for rows already in
        hand.
        """
        if self._rows_read:
            return self._rows
        self._rows_read = True
        inputs = self.inputs
        if inputs.contract_id is None:
            return self._rows
        self.session = latest_chain_date(inputs.store, inputs.contract_id, until=inputs.as_of)
        if self.session is None:
            return self._rows
        self._rows = chain_rows(
            inputs.store, underlying_id=inputs.contract_id, snapshot_date=self.session
        )
        return self._rows

    def expiries(self) -> list[date]:
        """Listed expiries still ahead of ``as_of``, oldest first.

        An expiry already past has no time value left to imply anything from,
        which is the same exclusion :func:`_expiry_matching_tenor` makes.
        """
        return sorted({row.expiry for row in self.rows() if row.expiry > self.inputs.as_of})

    def at_expiry(self, expiry: date) -> Smile | None:
        """The hygiene-filtered smile for ONE expiry, memoised.

        The same rows and the same ``SmileFilters`` as every other reading off
        this chain. Two standards would make the term structure and the ATM level
        disagree about the same instrument on the same day.
        """
        if expiry in self._per_expiry:
            return self._per_expiry[expiry]
        inputs = self.inputs
        spot = inputs.daily.last_close
        rows = self.rows()
        built = (
            None
            if not rows or not spot or inputs.contract_id is None
            else Smile.from_rows(
                rows,
                spot=spot,
                underlying_id=inputs.contract_id,
                expiry=expiry,
                filters=inputs.smile_filters,
            )
        )
        self._per_expiry[expiry] = built
        return built

    def build(self) -> Smile | None:
        if self._built:
            return self.smile
        self._built = True
        inputs = self.inputs
        rows = self.rows()
        spot = inputs.daily.last_close
        if self.session is None or not spot or not rows:
            return None
        self.tenor = _reference_tenor(inputs)
        # Matched to the realised-vol tenor rather than taken from the front of
        # the curve: indicator 9 subtracts one from the other, and comparing a
        # three-day implied against a thirty-day realised measures the term
        # structure, not a dislocation.
        self.expiry = _expiry_matching_tenor(rows, as_of=inputs.as_of, tenor_days=self.tenor)
        if self.expiry is None:
            return None
        self.smile = self.at_expiry(self.expiry)
        if self.smile is None:
            self.note = (
                f" The {self.session.isoformat()} chain has rows but none survived smile "
                "hygiene (spread, book depth, delta band, convexity)."
            )
        return self.smile

    def where(self) -> str:
        """Which session and expiry a reading off this smile came from.

        Only ever called once :meth:`build` has returned a smile, which is the
        only way all three fields are set. It reports what it has rather than
        raising, because this is provenance text on a cell: a reading that
        arrives with a vaguer sentence beside it is better than one that fails the
        whole grid build over how it was labelled.
        """
        if self.session is None or self.expiry is None or self.tenor is None:
            return "an option chain whose session and expiry were not recorded"
        dte = (self.expiry - self.inputs.as_of).days
        return (
            f"the {self.session.isoformat()} chain at the {self.expiry.isoformat()} "
            f"expiry ({dte}d, matched to the {self.tenor}d realised tenor)"
        )


def _chain_atm_implied_vol(
    chain: _ChainSmile,
) -> tuple[tuple[float | None, str, str | None] | None, str]:
    """The ATM quote off the exchange-quoted smile, and why there is none if there is not.

    Returns ``(result, hygiene_note)``. The note belongs to the attempt that found
    the chain unusable, which is why it travels on the shared smile rather than
    being recovered by a second read.
    """
    smile = chain.build()
    quote = None if smile is None else smile.atm()
    if quote is None or smile is None:
        return None, chain.note
    return (
        quote.iv,
        (f"ATM on {chain.where()}, {quote.source}; {smile.hygiene.summary()}"),
        _worst_status(smile.market_data_statuses),
    ), ""


def _vendor_atm_implied_vol(latest: Any) -> tuple[float | None, str, str | None] | None:
    """The implied vol the route served for the underlying."""
    # `is not None`, not truthiness: absence and a reading of zero are different
    # statements, and every other reader of this column already distinguishes them.
    if latest is None or latest.implied_vol is None:
        return None
    return (
        latest.implied_vol,
        f"vendor implied vol from the {latest.snapshot_date.isoformat()} snapshot",
        latest.market_data_status,
    )


def _solved_atm_implied_vol(latest: Any) -> tuple[float | None, str, str | None] | None:
    """An implied vol this repository solved, never one a vendor quoted.

    Read from ``implied_vol_solved``, which is a different column from
    ``implied_vol`` on purpose — see migration 017. The provenance says so in
    words as well, because a cell reading "0.19" is indistinguishable from a
    quoted one unless the row beside it says how it was obtained.
    """
    if latest is None or latest.implied_vol_solved is None:
        return None
    method = latest.implied_vol_solved_source or "method unrecorded"
    return (
        latest.implied_vol_solved,
        (
            f"implied vol SOLVED from mid prices on the "
            f"{latest.snapshot_date.isoformat()} chain ({method}). Black-Scholes-Merton "
            "prices a EUROPEAN option and this underlying may be American-style, so "
            "early exercise is unmodelled here."
        ),
        latest.market_data_status,
    )


def _worst_status(statuses: dict[str, int]) -> str | None:
    """One label for a mixed set of quote states.

    Anything not on ``data.market_data_status_live`` wins, because SKILL.md's
    rule is that a table mixing frozen and live quotes is an indicative table.
    Ties among non-live values go to the most common one; a set of one is
    itself.
    """
    if not statuses:
        return None
    if len(statuses) == 1:
        return next(iter(statuses))
    return max(sorted(statuses), key=lambda name: statuses[name])


def _nearest_expiry(rows: list[Any]) -> date | None:
    expiries = sorted({row.expiry for row in rows})
    return expiries[0] if expiries else None


def _expiry_matching_tenor(rows: list[Any], *, as_of: date, tenor_days: int) -> date | None:
    """The listed expiry closest to ``tenor_days`` away, for a like-for-like spread.

    Indicator 9 subtracts a realised vol measured over the reference tenor from
    an implied vol. Taking the NEAREST expiry made that a comparison between a
    few days of implied and thirty of realised on a weekly-cycle underlying,
    which is a term-structure artefact rather than a dislocation: the front of
    the curve is routinely well above or below the thirty-day point for reasons
    that have nothing to do with an option being cheap.

    Expiries already past ``as_of`` are excluded — they have no time value left
    to imply anything from. ``None`` when nothing is listed ahead.
    """
    ahead = sorted({row.expiry for row in rows if row.expiry > as_of})
    if not ahead:
        return None
    return min(ahead, key=lambda expiry: abs((expiry - as_of).days - tenor_days))


def _vendor_iv_percentile(inputs: IndicatorInputs) -> tuple[float | None, str]:
    """The vendor's 52-week IV percentile, from the newest snapshot that HAS one.

    The read is bounded twice. ``until`` stops it seeing a snapshot recorded
    after the session being built; the age check stops it serving a percentile
    recorded months ago as though it were today's. Neither bound is cosmetic —
    an IV percentile is the most time-sensitive figure in the grid, and this
    function used to take ``limit=1`` with no age test at all, so a reading from
    an abandoned collector run was published as ``ok`` indefinitely.

    The date is always in the reason, whether the value is fresh or not. A
    percentile that does not say when it was measured cannot be argued with.
    """
    if inputs.contract_id is None:
        return None, "no instrument is mapped to this cell"
    snapshots = underlying_snapshots(inputs.store, inputs.contract_id, until=inputs.as_of, limit=1)
    if snapshots and snapshots[-1].iv_percentile_52w is not None:
        snapshot = snapshots[-1]
        age = (inputs.as_of - snapshot.snapshot_date).days
        stamp = snapshot.snapshot_date.isoformat()
        if age > inputs.freshness_days(INPUT_UNDERLYING_SNAPSHOT):
            return None, (
                f"the newest vendor IV percentile is from the {stamp} snapshot, {age} days "
                f"before {inputs.as_of.isoformat()} and beyond the "
                f"grid.freshness_days.{INPUT_UNDERLYING_SNAPSHOT} window. A percentile this "
                "old is not a reading about today, and serving it as one is how a stale "
                "figure reaches a table nobody re-checks."
            )
        return snapshot.iv_percentile_52w, f"vendor, from the {stamp} snapshot"
    return None, (
        "the vendor IV percentile is not served: no verified Client Portal field id "
        "exists for it, so ibkr.cp_api.marketdata.underlying_snapshot_fields."
        "implied_vol_percentile is null. Finding the id is open in ROADMAP.md; a "
        "session holding the IBKR connector can bridge the gap with "
        "record_iv_percentile in the meantime."
    )


def _self_iv_percentile(inputs: IndicatorInputs) -> tuple[float | None, str]:
    """Self-computed from accumulated snapshots, once enough have accrued.

    This is the figure the vendor cannot be trusted for on broad ETFs, and it is
    also the one that does not exist on day one. The reason says how many
    sessions have accumulated so the wait is visible rather than mysterious.
    """
    if inputs.contract_id is None:
        return None, "no instrument is mapped to this cell"

    # The switch is on ACCUMULATED snapshots, per BUILD-BRIEF, not on a perfect
    # window: a single session where no implied vol was served would otherwise
    # block it permanently. Bounded at `as_of` like every other store read here
    # — unbounded, a grid rebuilt for a past date counted snapshots recorded
    # after it and could switch to the self-computed percentile before the data
    # existed, which is a gate opening on evidence from its own future.
    accumulated = underlying_snapshot_count(inputs.store, inputs.contract_id, until=inputs.as_of)
    if accumulated < inputs.iv_self_min_sessions:
        return None, (
            f"{accumulated} of the {inputs.iv_self_min_sessions} snapshots in "
            f"data.iv_self_source_min_sessions had accumulated by {inputs.as_of.isoformat()}. "
            "Self-computed IV percentiles compound from day one and are worth nothing "
            "before then."
        )

    sessions = round(
        inputs.iv_percentile_window_weeks * inputs.trading_days_per_year / WEEKS_PER_YEAR
    )
    snapshots = underlying_snapshots(
        inputs.store, inputs.contract_id, until=inputs.as_of, limit=sessions
    )
    values = [s.implied_vol for s in snapshots if s.implied_vol is not None]
    if not values:
        return None, (
            f"{accumulated} snapshots had accumulated by {inputs.as_of.isoformat()}, but "
            f"none inside the most recent {inputs.iv_percentile_window_weeks} weeks "
            "carries an implied vol"
        )
    return percentile_of(values, values[-1]), (
        f"self-computed over {len(values)} of {accumulated} accumulated snapshots"
    )
