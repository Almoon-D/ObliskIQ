"""The option screen: which contracts survive every gate, and how they sort.

The census (:mod:`obliskiq.compute.universe_census`) and the screener answer two
different questions off **one** evaluation. The census asks *how many* clear
every gate simultaneously and writes the count down weekly; the screener asks
*which ones*, and splits the survivors into the four buckets SKILL.md's
dislocation table is built from. Both call :meth:`OptionScreen.evaluate`, so a
gate can never be applied in one place and forgotten in the other.

Buckets are descriptions of survivors, not a sixth gate. ``cheap`` and
``extended`` come from ``screen.cheap_iv_percentile_max`` and
``screen.extended_iv_percentile_min``; ``coiled`` is compression without the
implied-vol reading that would make it cheap, and ``expensive`` is everything
between the two boundaries. A contract whose IV percentile never came back is
bucketed ``unclassified`` rather than assumed ordinary — the same rule the
census applies to an untested gate.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..collect.vol import realised_vol as annualised_realised_vol
from ..collect.vol import recent_daily_closes
from ..config import Config
from ..logging_setup import get_logger
from ..store.db import Store
from .frames import (
    QUALITY_LIVE,
    ChainRow,
    bar_series,
    chain_rows,
    classify_market_data_status,
    latest_chain_date,
    latest_nav,
    underlying_snapshots,
    universe_row,
    worst_market_data_quality,
)
from .fx_engine import BaseCurrencyRates
from .gates import AffordabilityCeiling, CandidateGates, GateResult
from .indicators import _rolling_realised_vol
from .penalty import PenaltyLedger
from .stats import distance_from_low, drawdown_from_peak, percentile_of

log = get_logger("compute.screen")

#: Which realised-vol figure a candidate's IV-HV spread was measured against.
#: Recorded per candidate because the two are 16.8% apart on live data and the
#: difference decides the ranking — see Candidate.iv_minus_hv.
HV_SOURCE_SELF = "self"
HV_SOURCE_VENDOR = "vendor"


@dataclass(frozen=True)
class UnderlyingState:
    """The underlying's own state, for the conditions *coiled* adds to *cheap*.

    Each leg is ``None`` when the store holds too little to measure it, and an
    unmeasured leg never promotes a strike: an untested condition is not a
    passed one here either.
    """

    realised_vol_percentile: float | None
    distance_from_low: float | None
    distance_from_high: float | None
    #: ``screen.cheap_iv_percentile_max``, reused as the "compressed" and "near
    #: the low" boundary. SKILL.md defines coiled with the same vocabulary it
    #: defines cheap in — the bottom of a range — and a second threshold that
    #: could drift from this one would make the two definitions disagree.
    cheap_max: float

    @property
    def rv_compressed(self) -> bool | None:
        """Realised vol in the bottom of its own long-run range."""
        if self.realised_vol_percentile is None:
            return None
        return self.realised_vol_percentile <= self.cheap_max

    @property
    def near_the_low(self) -> bool | None:
        """Price near the low of the window, and well off its high.

        Both halves, because either alone is satisfiable by a market that has
        gone nowhere: a flat series sits near its low AND near its high.
        """
        if self.distance_from_low is None or self.distance_from_high is None:
            return None
        return (
            self.distance_from_low <= self.cheap_max and self.distance_from_high <= -self.cheap_max
        )


#: Buckets the survivors are split into. SKILL.md's dislocation screen is one
#: ranked table split exactly this way.
BUCKET_CHEAP = "cheap"
BUCKET_EXPENSIVE = "expensive"
BUCKET_COILED = "coiled"
BUCKET_EXTENDED = "extended"
BUCKET_UNCLASSIFIED = "unclassified"

BUCKETS = (BUCKET_CHEAP, BUCKET_EXPENSIVE, BUCKET_COILED, BUCKET_EXTENDED)


@dataclass(frozen=True)
class Candidate:
    """One strike, its gate outcome, and the evidence behind its bucket."""

    underlying_id: int
    symbol: str | None
    expiry: date
    strike: float
    right: str
    dte: int
    bid: float | None
    ask: float | None
    mid: float | None
    iv: float | None
    delta: float | None
    open_interest: int | None
    volume: int | None
    spread_pct_of_mid: float | None
    contract_id: int | None
    #: Vendor 52-week IV percentile for the UNDERLYING, not the strike. ``None``
    #: when none was served, which leaves the IV-percentile gate untested.
    iv_percentile_52w: float | None
    #: The VENDOR's realised vol, from the same snapshot. Kept for the
    #: cross-check BUILD-BRIEF asks for, and NOT what the spread is computed on
    #: — see :attr:`iv_minus_hv`.
    historical_vol_30d: float | None
    #: Realised vol computed here from the stored daily closes, at
    #: ``data.strike_bounds_rv_tenor_days``, through the same function the grid
    #: and the chain collector's strike bands use.
    realised_vol_self: float | None
    #: Which of the two the spread was computed on: ``self`` or ``vendor``.
    #: ``None`` when there was no spread to compute.
    hv_source: str | None
    bucket: str
    gates: GateResult
    #: The route that collected this quote: mcp | cp_api | marketdata_app.
    source: str | None = None
    #: The market-data state it was captured in — FROZEN, DELAYED, REALTIME, or
    #: None where the route reported nothing.
    market_data_status: str | None = None
    #: That status classified against ``data.market_data_status_live``: live,
    #: indicative or unreported. SKILL.md requires anything frozen or delayed to
    #: be labelled indicative in every table it appears in, and this screen is
    #: one of the tables.
    market_data_quality: str | None = None

    @property
    def is_indicative(self) -> bool:
        """True unless the quote behind this row is confirmed live.

        Unreported counts as indicative. Migration 006 is explicit that a NULL
        status means the route said nothing, never that it said realtime, and a
        screen that treated silence as freshness would put the label on exactly
        the rows least entitled to it.
        """
        return self.market_data_quality != QUALITY_LIVE

    @property
    def premium_per_contract(self) -> float | None:
        return self.mid

    @property
    def realised_vol(self) -> float | None:
        """The realised vol the spread is measured against.

        Self-computed where there are enough stored closes, the vendor figure
        otherwise. Never a blend: :attr:`hv_source` says which one this is.
        """
        return (
            self.realised_vol_self
            if self.realised_vol_self is not None
            else self.historical_vol_30d
        )

    @property
    def iv_minus_hv(self) -> float | None:
        """The spread SKILL.md ranks the dislocation screen on.

        **Measured against SELF-COMPUTED realised vol, not the vendor's.** The
        two are not interchangeable and the difference decides the ranking.
        Measured live on ASML, 2026-08-24: IBKR's ``historical-vol.annual_pct``
        read 0.5399 where the same daily closes give 0.4623, 16.8% apart. Against
        an ATM implied of 0.4604 that is a spread of -0.0794 or -0.0019 — a
        factor of 42, and the difference between "cheap volatility, rank it top"
        and "fairly priced, ignore it".

        The vendor figure is preferred against on two grounds. It is not
        reproducible: no close-to-close (sample, population or zero-mean),
        Parkinson or Garman-Klass estimator over any window from 5 to 39 days
        comes within 6% of it, on the very bars IBKR serves for the same
        contract. And the grid's own indicator 9 already computes the spread
        self-computed, so using the vendor figure here made the screen and the
        grid disagree about the same instrument on the same day.
        """
        realised = self.realised_vol
        if self.iv is None or realised is None:
            return None
        return self.iv - realised


@dataclass(frozen=True)
class ScreenResult:
    """Everything the screen found, and what it could not measure."""

    as_of: date
    snapshot_date: date | None
    nav: float | None
    ceiling: AffordabilityCeiling | None
    contracts_scanned: int
    survivors: tuple[Candidate, ...]
    #: Passed every gate that could be applied, but left at least one unmeasured.
    #: Reported apart: an untested gate is not a passed gate.
    incomplete: tuple[Candidate, ...]
    truncated: bool
    reason: str | None = None

    def by_bucket(self) -> dict[str, list[Candidate]]:
        out: dict[str, list[Candidate]] = {name: [] for name in BUCKETS}
        for candidate in self.survivors:
            out.setdefault(candidate.bucket, []).append(candidate)
        return out

    @property
    def market_data_quality(self) -> str | None:
        """One label for the whole table, worst row first.

        SKILL.md: never mix frozen and live quotes unmarked. A table holding one
        frozen quote among fifty live ones is an indicative table, so the table
        is labelled by its weakest row and each row still carries its own."""
        rows = (*self.survivors, *self.incomplete)
        return worst_market_data_quality(
            row.market_data_quality for row in rows if row.market_data_quality is not None
        )

    @property
    def market_data_statuses(self) -> dict[str, int]:
        """The distinct statuses across the table, with counts."""
        counts: dict[str, int] = {}
        for row in (*self.survivors, *self.incomplete):
            if row.market_data_status:
                counts[row.market_data_status] = counts.get(row.market_data_status, 0) + 1
        return counts


class OptionScreen:
    """One evaluation of every gate, shared by the census and the screener."""

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config
        self._gates = CandidateGates(config)
        self._penalty = PenaltyLedger(store, config)
        # The affordability gates compare a premium in the CONTRACT's currency
        # against a ceiling in base currency, so they need a rate. An absent one
        # leaves the gate untested rather than assuming parity.
        self._fx = BaseCurrencyRates(store, config)
        # Keyed by (underlying, as_of): the same underlying read for two
        # different sessions is two different windows of closes.
        self._rv_cache: dict[tuple[int, date | None], float | None] = {}
        self._rv_tenor = config.require_int("data.strike_bounds_rv_tenor_days")
        self._trading_days_per_year = config.require_float("data.trading_days_per_year")
        self._cheap_max = config.require_float("screen.cheap_iv_percentile_max")
        self._extended_min = config.require_float("screen.extended_iv_percentile_min")
        # What "coiled" needs beyond a cheap strike: the underlying's own state.
        self._long_run_years = config.require_int("data.weekly_window_years")
        # Calendar days per year, from numerics.bsm.day_count_basis — the same
        # constant every other window in this build converts years with. Not a
        # literal 365: no threshold or window is written as one in Python.
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")
        self._percentile_floor = config.require_int("grid.min_observations.percentile")
        self._state_cache: dict[tuple[int, date | None], UnderlyingState] = {}
        self._live_statuses = tuple(
            str(value) for value in config.require_list("data.market_data_status_live")
        )

    @property
    def gates(self) -> CandidateGates:
        return self._gates

    @property
    def fx(self) -> BaseCurrencyRates:
        """Shared so the screener and the entry guard convert through one cache.

        Two converters would be two chances to read a different rate for the
        same decision, and the guard exists precisely to agree with the screen.
        """
        return self._fx

    def affordability(self, nav: float) -> AffordabilityCeiling:
        """The ceiling the universe is filtered by, before any screening.

        The single place the sleeve's three subtractions are assembled — gross
        budget, penalty balance, premium already committed to open positions.
        The screener, the census and the entry guard all reach the ceiling
        through here, so none of them can be looking at a different pool.
        """
        return self._gates.affordability(
            nav=nav,
            penalty_balance=self._penalty.balance(),
            committed_premium=self._penalty.committed_premium(),
        )

    def evaluate(
        self,
        row: ChainRow,
        ceiling: AffordabilityCeiling,
        as_of: date,
        iv_cache: dict[int, float | None],
    ) -> GateResult:
        """Every gate, against one strike. The single gate path in the codebase."""
        underlying = universe_row(self._store, row.underlying_id) or {}
        result = self._gates.option_underlying(underlying)
        result = result.merge(self._gates.tenor(dte=(row.expiry - as_of).days))
        result = result.merge(
            self._gates.liquidity(
                spread_pct_of_mid=row.spread_pct_of_mid,
                open_interest=row.open_interest,
                volume=row.volume,
            )
        )
        currency = underlying.get("currency")
        result = result.merge(
            self._gates.price_ceiling(
                premium_per_share=row.mid,
                ceiling=ceiling,
                currency=currency,
                fx_to_base=self._fx.to_base(currency, as_of=as_of),
            )
        )
        if row.underlying_id not in iv_cache:
            iv_cache[row.underlying_id] = self.iv_percentile(row.underlying_id, as_of=as_of)
        return result.merge(self._gates.iv_percentile(iv_cache[row.underlying_id]))

    def iv_percentile(self, contract_id: int, *, as_of: date | None = None) -> float | None:
        """The vendor IV percentile for an underlying, when one was served.

        Null until a verified Client Portal field id exists for it — see
        ``ibkr.cp_api.marketdata.underlying_snapshot_fields``. A null leaves the
        gate untested rather than passed.

        ``as_of`` bounds the read to snapshots taken on or before that session.
        """
        snapshots = underlying_snapshots(self._store, contract_id, until=as_of, limit=1)
        if not snapshots:
            return None
        return snapshots[-1].iv_percentile_52w

    def bucket(
        self,
        iv_percentile: float | None,
        *,
        iv_below_hv: bool,
        rv_compressed: bool | None = None,
        near_the_low: bool | None = None,
    ) -> str:
        """Which of SKILL.md's four descriptions this strike answers to.

        **Cheap is BOTH conditions**, as SKILL.md defines it: implied vol
        materially below realised, *and* an IV percentile at or below
        ``screen.cheap_iv_percentile_max``. The two labels were inverted — a
        strike meeting both was returned as ``coiled`` and one meeting only the
        percentile as ``cheap`` — so the screen's headline bucket named the case
        where implied vol is NOT below realised.

        **Coiled is cheap plus the underlying's own state**: realised vol
        compressed against its long-run range, and price near the 52-week low.
        Those are properties of the underlying rather than of a strike, and when
        the store cannot measure them the row stays ``cheap`` rather than being
        promoted on an untested condition.
        """
        if iv_percentile is None:
            return BUCKET_UNCLASSIFIED
        if iv_percentile >= self._extended_min:
            return BUCKET_EXTENDED
        if iv_percentile <= self._cheap_max and iv_below_hv:
            if rv_compressed and near_the_low:
                return BUCKET_COILED
            return BUCKET_CHEAP
        return BUCKET_EXPENSIVE

    def screen(
        self,
        *,
        as_of: date,
        nav: float | None = None,
        symbols: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> ScreenResult:
        """Survivors of every gate, split into buckets, richest evidence first."""
        resolved_nav = nav if nav is not None else latest_nav(self._store)
        snapshot_date = latest_chain_date(self._store, until=as_of)

        if snapshot_date is None:
            return self._no_data(as_of, resolved_nav, "no chain snapshots have been collected")
        if resolved_nav is None:
            return self._no_data(
                as_of,
                None,
                "no NAV is available, so the affordability ceiling cannot be computed. "
                "The budget defines the universe, and discovering it last is the most "
                "repeated failure mode in this account.",
            )

        ceiling = self.affordability(resolved_nav)
        rows = chain_rows(self._store, snapshot_date=snapshot_date)
        wanted = self._underlying_filter(symbols)

        iv_cache: dict[int, float | None] = {}
        # Realised vol is a property of the UNDERLYING, and a chain contributes
        # one candidate per surviving strike — hundreds of rows asking the same
        # question of the same instrument. Cleared per run rather than held on
        # the instance, because the bars underneath it change between runs.
        self._rv_cache.clear()
        survivors: list[Candidate] = []
        incomplete: list[Candidate] = []
        scanned = 0

        for row in rows:
            if wanted is not None and row.underlying_id not in wanted:
                continue
            scanned += 1
            outcome = self.evaluate(row, ceiling, as_of, iv_cache)
            if not outcome.passed:
                continue
            candidate = self._to_candidate(row, outcome, as_of, iv_cache)
            if outcome.fully_tested:
                survivors.append(candidate)
            else:
                incomplete.append(candidate)

        # Cheapest volatility first. See _dislocation_key: sorting on the SIGNED
        # spread descending put the most expensive options at the top and, with
        # a limit applied, truncated away the cheap tail the screen exists to
        # find.
        survivors.sort(key=_dislocation_key)
        incomplete.sort(key=_dislocation_key)

        truncated = False
        if limit is not None and limit > 0:
            truncated = len(survivors) > limit or len(incomplete) > limit
            survivors = survivors[:limit]
            incomplete = incomplete[:limit]

        return ScreenResult(
            as_of=as_of,
            snapshot_date=snapshot_date,
            nav=resolved_nav,
            ceiling=ceiling,
            contracts_scanned=scanned,
            survivors=tuple(survivors),
            incomplete=tuple(incomplete),
            truncated=truncated,
        )

    # ── internals ──

    def _to_candidate(
        self,
        row: ChainRow,
        outcome: GateResult,
        as_of: date,
        iv_cache: dict[int, float | None],
    ) -> Candidate:
        underlying = universe_row(self._store, row.underlying_id) or {}
        snapshots = underlying_snapshots(self._store, row.underlying_id, until=as_of, limit=1)
        snapshot = snapshots[-1] if snapshots else None
        percentile = iv_cache.get(row.underlying_id)
        vendor_hv = snapshot.historical_vol_30d if snapshot else None
        self_hv = self._realised_vol(row.underlying_id, as_of=as_of)
        hv = self_hv if self_hv is not None else vendor_hv
        # "Materially below realised", the first half of SKILL.md's cheap test.
        iv_below_hv = row.iv is not None and hv is not None and row.iv < hv
        state = self._underlying_state(row.underlying_id, as_of=as_of)
        symbol = underlying.get("symbol")
        return Candidate(
            underlying_id=row.underlying_id,
            symbol=str(symbol) if symbol is not None else None,
            expiry=row.expiry,
            strike=row.strike,
            right=row.right,
            dte=(row.expiry - as_of).days,
            bid=row.bid,
            ask=row.ask,
            mid=row.mid,
            iv=row.iv,
            delta=row.delta,
            open_interest=row.open_interest,
            volume=row.volume,
            spread_pct_of_mid=row.spread_pct_of_mid,
            contract_id=row.contract_id,
            iv_percentile_52w=percentile,
            historical_vol_30d=vendor_hv,
            realised_vol_self=self_hv,
            hv_source=(
                None
                if hv is None
                else (HV_SOURCE_SELF if self_hv is not None else HV_SOURCE_VENDOR)
            ),
            bucket=self.bucket(
                percentile,
                iv_below_hv=iv_below_hv,
                rv_compressed=state.rv_compressed,
                near_the_low=state.near_the_low,
            ),
            gates=outcome,
            source=row.source,
            market_data_status=row.market_data_status,
            market_data_quality=classify_market_data_status(
                row.market_data_status, self._live_statuses
            ),
        )

    def _underlying_state(self, underlying_id: int, *, as_of: date | None) -> UnderlyingState:
        """Realised-vol compression and range position, from the stored bars.

        These are the two conditions SKILL.md attaches to *coiled* beyond a
        cheap strike, and they are properties of the UNDERLYING rather than of
        any one contract — so they are computed once per underlying per session
        and cached.

        Every leg is ``None`` when the store cannot measure it, and a ``None``
        never promotes a row to coiled. The screener does not have a laxer
        second reading to fall back on any more than the smile does.
        """
        key = (underlying_id, as_of)
        if key in self._state_cache:
            return self._state_cache[key]

        since = (
            None
            if as_of is None
            else as_of - timedelta(days=round(self._long_run_years * self._calendar_days_per_year))
        )
        series = bar_series(self._store, underlying_id, since=since, until=as_of)
        closes = list(series.close)

        rv_percentile: float | None = None
        history = _rolling_realised_vol(closes, self._rv_tenor, self._trading_days_per_year)
        if len(history) >= self._percentile_floor:
            rv_percentile = percentile_of(history, history[-1])

        low_distance = distance_from_low(closes) if closes else None
        high_distance = drawdown_from_peak(closes) if closes else None

        state = UnderlyingState(
            realised_vol_percentile=rv_percentile,
            distance_from_low=low_distance,
            distance_from_high=high_distance,
            cheap_max=self._cheap_max,
        )
        self._state_cache[key] = state
        return state

    def _realised_vol(self, underlying_id: int, *, as_of: date | None = None) -> float | None:
        """Realised vol from the stored closes, or ``None`` when there are too few.

        The same function the chain collector sizes its strike bands with and the
        grid measures indicator 9 with. Reused rather than reimplemented: three
        implementations of "realised vol" would be three chances to rank a
        contract on a number nothing else in the build agrees with.

        Memoised per underlying for the duration of one run. ``None`` is a real
        answer here — too few closes — so absence is tested with ``in`` rather
        than by truthiness, which would recompute it for every strike.
        """
        key = (underlying_id, as_of)
        if key not in self._rv_cache:
            closes = recent_daily_closes(
                self._store, underlying_id, limit=self._rv_tenor + 1, until=as_of
            )
            self._rv_cache[key] = annualised_realised_vol(
                closes,
                tenor_days=self._rv_tenor,
                trading_days_per_year=self._trading_days_per_year,
            )
        return self._rv_cache[key]

    def _underlying_filter(self, symbols: Sequence[str] | None) -> set[int] | None:
        if not symbols:
            return None
        wanted = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        if not wanted:
            return None
        placeholders = ", ".join("?" for _ in wanted)
        rows = self._store.execute(
            f"SELECT contract_id FROM universe WHERE UPPER(symbol) IN ({placeholders})",
            sorted(wanted),
        ).fetchall()
        return {int(row[0]) for row in rows}

    def _no_data(self, as_of: date, nav: float | None, reason: str) -> ScreenResult:
        return ScreenResult(
            as_of=as_of,
            snapshot_date=None,
            nav=nav,
            ceiling=None,
            contracts_scanned=0,
            survivors=(),
            incomplete=(),
            truncated=False,
            reason=reason,
        )


def _dislocation_key(candidate: Candidate) -> float:
    """Sort key: the MOST NEGATIVE IV-minus-HV first, ascending.

    This screen exists to find cheap volatility — implied below realised, which
    is a negative spread — and it sorted on the signed spread *descending*, so
    the most expensive options came first and ``limit`` truncated away exactly
    the tail being looked for. ``iv_minus_hv``'s own docstring says the
    difference is between "cheap volatility, rank it top" and "fairly priced,
    ignore it"; the ordering said the opposite.

    Not absolute magnitude, which is what ``rankings.widest_iv_hv`` uses. That
    view ranks the grid on dislocation of either sign, because an expensive cell
    is a finding about a market. This is a list of contracts to BUY, and the
    gates upstream have already refused anything above
    ``screen.cheap_iv_percentile_max``, so both tails are not in play here.

    A candidate with no computable spread sorts LAST: an absent number is not a
    cheap one.
    """
    spread = candidate.iv_minus_hv
    return float("inf") if spread is None else spread
