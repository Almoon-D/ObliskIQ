"""The grid: every market x every sector x the fifteen indicator families.

The core deliverable, and it is **computed, not reasoned through**. Roughly ten
markets by eleven sectors by fifteen families is a table to compute and a set of
rankings to read; nobody reasons over sixteen hundred cells, and pretending
otherwise is how a session spends its whole budget describing the grid instead
of using it.

Three properties make the output trustworthy rather than merely large.

**Markets are measured on their NATIVE INDEX contract.** KOSPI, not EWY. A
USD-listed wrapper embeds a dollar trade in every reading, and analysing the
world through one is an American thesis wearing a global label. The measurement
instrument per cell is declared in ``universe.yaml`` and at most one instrument
may claim a cell.

**Where an ETF measures a cell it carries its disclosure.** Top-two
concentration, the hedged flag and a coverage note go into ``grid_coverage``
beside the cell. Above ``data.etf_top2_concentration_flag`` the note says the
output is a two-stock analysis rather than a country analysis — the exact claim
that once produced a flatly wrong reading of China from offshore internet ADRs.

**A missing cell is ``n/a`` with a reason.** Never a silent proxy, never an
estimate presented as data. Most cells are missing on day one, and the coverage
report says how many and why. That count is the honest state of the grid, not a
failure to hide.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from ..collect.universe import BROAD_SECTOR, load_universe_file
from ..config import Config
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..store.db import Store
from ..util.timeutil import iso_date, parse_date, utc_stamp
from .frames import (
    chain_rows,
    contract_id_for_symbol,
    grid_instruments,
    latest_bar_date,
    latest_chain_date,
    macro_series,
    underlying_snapshots,
)
from .indicators import (
    FAMILIES,
    FAMILY_INPUTS,
    INPUT_DAILY_BARS,
    INPUT_MACRO,
    INPUT_OPTION_CHAIN,
    INPUT_SOURCES,
    INPUT_UNDERLYING_SNAPSHOT,
    MACRO_ONLY_FAMILIES,
    REASON_NO_INSTRUMENT,
    REASON_NON_FINITE,
    REASON_NOT_SEEDED,
    STATUS_INAPPLICABLE,
    STATUS_NA,
    STATUS_OK,
    STATUS_STALE,
    Cell,
    IndicatorInputs,
    all_cells,
)

log = get_logger("compute.grid")

#: What kind of instrument measured a cell, written to ``grid_coverage``.
KIND_NATIVE_INDEX = "native_index"
KIND_ETF = "etf"
KIND_EQUITY = "equity"
KIND_NONE = "none"

#: Asset classes, as ``universe.asset_class`` records them.
ASSET_ETF = "ETF"
ASSET_INDEX = "IND"


@dataclass(frozen=True)
class CoverageRow:
    """Which instrument measured a cell, and what it discloses."""

    market: str
    sector: str
    contract_id: int | None
    symbol: str | None
    instrument_kind: str
    is_native_index: bool
    top2_concentration: float | None
    hedged: bool | None
    coverage_note: str | None


@dataclass(frozen=True)
class GridReport:
    """What one build produced, and what it could not."""

    as_of: date
    markets: int
    sectors: int
    cells_written: int
    populated: int
    absent: int
    stale: int
    #: Cells no collector could ever fill — no instrument mapped, no reachable
    #: option chain, no declared macro series. Excluded from the coverage
    #: denominator and reported as the ambition gap instead.
    inapplicable: int
    families: tuple[str, ...]
    #: Reason CODE -> count. Grouped, so the histogram is a dozen lines rather
    #: than one per market x sector.
    reasons: dict[str, int] = field(default_factory=dict)
    #: One worked example of each code, so a count stays actionable.
    examples: dict[str, str] = field(default_factory=dict)
    measured_cells: int = 0

    @property
    def collectible(self) -> int:
        """Leaves some collector is supposed to be able to fill."""
        return self.cells_written - self.inapplicable

    @property
    def coverage_pct(self) -> float:
        """COLLECTION HEALTH: populated over collectible.

        The denominator is deliberately not ``cells_written``. Counting cells no
        collector could ever fill made ``grid.min_coverage_pct`` unreachable —
        141 of 156 market x sector cells had no instrument, capping coverage at
        about 9.6% against a 20% floor — so every build reported DEGRADED and the
        signal said nothing on the day collection actually broke.
        """
        return 0.0 if not self.collectible else self.populated / self.collectible

    @property
    def ambition_pct(self) -> float:
        """THE AMBITION GAP: collectible over total.

        How much of the declared grid anything is even trying to fill. Reported,
        never gated: the gap between what universe.yaml declares and what it maps
        an instrument to is the point of declaring the axes rather than deriving
        them, and it must stay visible without being counted as a collector's
        failure.
        """
        return 0.0 if not self.cells_written else self.collectible / self.cells_written

    def summary(self) -> str:
        lines = [
            f"grid for {self.as_of.isoformat()}: {self.markets} markets x {self.sectors} "
            f"sectors, {self.measured_cells} of them with a measurement instrument.",
            f"{self.cells_written} leaf cells across {len(self.families)} indicator "
            f"families: {self.populated} populated, {self.absent} n/a, {self.stale} stale, "
            f"{self.inapplicable} inapplicable.",
            f"collection coverage {self.populated}/{self.collectible} "
            f"({self.coverage_pct:.0%} of what is collectible at all); ambition gap "
            f"{self.collectible}/{self.cells_written} "
            f"({self.ambition_pct:.0%} of the declared grid is collectible).",
        ]
        if self.reasons:
            lines.append("why cells are missing:")
            for code, count in sorted(self.reasons.items(), key=lambda item: -item[1]):
                lines.append(f"  {count:>5}  {code}")
                example = self.examples.get(code)
                if example:
                    lines.append(f"         e.g. {example}")
        return "\n".join(lines)


#: The macro series a market declares in ``universe.yaml``. Freshness of the
#: macro side of a market is measured across all of them.
_MACRO_SERIES_KEYS = (
    "policy_rate_series",
    "long_rate_series",
    "cpi_series",
    "fx_series",
    "cot_series",
)


@dataclass(frozen=True)
class Freshness:
    """How current one input store is, against its own configured window."""

    source: str
    newest: date | None
    age_days: int | None
    window_days: int

    @property
    def is_stale(self) -> bool:
        """Absent is not stale. A cell with no input at all is ``n/a`` with a
        reason from the family that could not compute it — calling that stale
        would put a freshness verdict on data that was never collected."""
        return self.age_days is not None and self.age_days > self.window_days

    def describe(self) -> str:
        stamp = "never" if self.newest is None else self.newest.isoformat()
        return (
            f"the newest {self.source} input is from {stamp}, {self.age_days} days old, "
            f"beyond the {self.window_days}-day grid.freshness_days.{self.source} window"
        )


def _stale_reason(groups: Sequence[Sequence[str]], freshness: dict[str, Freshness]) -> str | None:
    """Why this family's inputs are stale, or ``None`` when they are not.

    A family is as fresh as its worst requirement GROUP, and a group as fresh as
    its best member: `implied_vol` reads the chain and falls back to the vendor
    snapshot, so either being current is enough, while `iv_hv_spread` needs an
    implied vol from one of those AND a realised vol from bars, so an old chain
    makes the spread indicative against today's bars.
    """
    failures: list[str] = []
    for group in groups:
        present = [freshness[source] for source in group if source in freshness]
        usable = [item for item in present if item.age_days is not None]
        if not usable:
            continue  # nothing collected for this group: absence, not staleness
        if any(not item.is_stale for item in usable):
            continue  # one current member satisfies the group
        freshest = min(usable, key=lambda item: item.age_days or 0)
        failures.append(freshest.describe())
    if not failures:
        return None
    return (
        "; ".join(failures)
        + ". Every figure in this cell is indicative, not live. Freshness is measured on the "
        "input this indicator actually reads, so a cell beside it may be current."
    )


def _first_status(groups: Sequence[Sequence[str]], quotes: dict[str, str | None]) -> str | None:
    """The quote state of the first input in declaration order that has one.

    Declaration order is preference order — `implied_vol` lists the chain before
    the vendor snapshot because it reads them that way — so the first hit is the
    row the value most likely came from.
    """
    for group in groups:
        for source in group:
            status = quotes.get(source)
            if status:
                return status
    return None


class GridBuilder:
    """Computes every cell and writes it, with its reason when it is absent."""

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config
        # One window per input source. data.grid_staleness_days remains the
        # daily-bar figure, so splitting freshness apart re-tunes nothing on
        # its own — it only stops one store's age being asserted about four.
        freshness = config.section("grid.freshness_days")
        self._freshness_days = {source: freshness.require_int(source) for source in INPUT_SOURCES}
        self._top2_flag = config.require_float("data.etf_top2_concentration_flag")
        self._benchmark_market = config.require_str("grid.benchmark_market")

        universe = load_universe_file(config.require_str("collectors.universe.path"), config)
        if not universe.sectors or not universe.markets:
            raise ConfigError(
                "the grid needs both a sector axis and a market axis, declared under "
                "`sectors:` and `markets:` in universe.yaml. They are declared rather than "
                "derived so that a market x sector cell with no mapped instrument is an "
                "explicit n/a with a reason; derived from whatever happens to exist, that "
                "cell would simply never appear and the gap would be invisible."
            )
        self._sectors = list(universe.sectors)
        self._markets = dict(universe.markets)
        # (market, sector) -> the declared reason nothing measures it. The grid
        # used to synthesise this prose itself, which made every unmapped cell
        # read as though a person had considered it; only the loader's
        # completeness check makes that true.
        self._unmapped = dict(universe.unmapped_cells)

        benchmark_spec = self._markets.get(self._benchmark_market)
        if benchmark_spec is None:
            raise ConfigError(
                f"grid.benchmark_market is {self._benchmark_market!r}, which is not a "
                f"declared market. Declared: {sorted(self._markets)}."
            )
        self._benchmark_symbol = str(benchmark_spec.get("native_index") or "")

    @property
    def sectors(self) -> list[str]:
        return list(self._sectors)

    @property
    def markets(self) -> dict[str, dict[str, Any]]:
        return dict(self._markets)

    # ── build ──

    def build(self, *, as_of: date, write: bool = True) -> GridReport:
        """Compute every cell for every market x sector, and write them."""
        benchmark_id = (
            contract_id_for_symbol(self._store, self._benchmark_symbol)
            if self._benchmark_symbol
            else None
        )
        measuring = self._measuring_instruments()

        cells: list[tuple[str, str, int | None, Cell]] = []
        coverage: list[CoverageRow] = []
        for market in sorted(self._markets):
            for sector in self._sectors:
                instrument = measuring.get((market, sector))
                coverage.append(self._coverage_row(market, sector, instrument))
                cells.extend(
                    (market, sector, _contract_id(instrument), cell)
                    for cell in self._cells_for(
                        market=market,
                        sector=sector,
                        instrument=instrument,
                        as_of=as_of,
                        benchmark_id=benchmark_id,
                    )
                )

        if write:
            self._write_cells(cells, as_of=as_of)
            self._write_coverage(coverage, as_of=as_of)

        statuses = Counter(cell.status for _, _, _, cell in cells)
        # Grouped on the CODE, not the prose: the reason names the specific market
        # and series, so a histogram keyed on it is one line per cell.
        reasons = Counter(
            cell.reason_code or _UNCLASSIFIED for _, _, _, cell in cells if cell.status != STATUS_OK
        )
        examples = {}
        for _, _, _, cell in cells:
            code = cell.reason_code or _UNCLASSIFIED
            if cell.status != STATUS_OK and cell.reason and code not in examples:
                examples[code] = cell.reason
        report = GridReport(
            as_of=as_of,
            markets=len(self._markets),
            sectors=len(self._sectors),
            cells_written=len(cells),
            populated=statuses.get(STATUS_OK, 0),
            absent=statuses.get(STATUS_NA, 0),
            stale=statuses.get(STATUS_STALE, 0),
            inapplicable=statuses.get(STATUS_INAPPLICABLE, 0),
            families=FAMILIES,
            reasons=dict(reasons),
            examples=examples,
            measured_cells=len(measuring),
        )
        log.info(
            "grid built",
            extra={
                "as_of": iso_date(as_of),
                "cells": report.cells_written,
                "populated": report.populated,
                "absent": report.absent,
                "stale": report.stale,
                "inapplicable": report.inapplicable,
                "collectible": report.collectible,
            },
        )
        return report

    # ── internals ──

    def _measuring_instruments(self) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for row in grid_instruments(self._store):
            market, sector = row.get("market"), row.get("sector")
            if not market or not sector:
                continue
            out[(str(market), str(sector))] = row
        return out

    def _cells_for(
        self,
        *,
        market: str,
        sector: str,
        instrument: dict[str, Any] | None,
        as_of: date,
        benchmark_id: int | None,
    ) -> list[Cell]:
        spec = self._markets.get(market, {})
        contract_id = _contract_id(instrument)

        inputs = IndicatorInputs.bind(
            self._store,
            self._config,
            as_of=as_of,
            market=market,
            sector=sector,
            contract_id=contract_id,
            symbol=None if instrument is None else str(instrument.get("symbol")),
            market_spec=spec,
            benchmark_contract_id=benchmark_id,
            benchmark_market=self._benchmark_market,
            options_unavailable_reason=_options_unavailable_reason(instrument),
            asset_class=(
                None
                if instrument is None
                else str(instrument.get("asset_class") or "").upper() or None
            ),
        )
        cells = all_cells(inputs)

        if contract_id is not None:
            freshness = self._freshness(contract_id, as_of, spec)
            quotes = self._quote_status(contract_id, as_of)
            return [self._graded(cell, freshness, quotes) for cell in cells]

        # Nothing in the store measures this cell, and the two ways that happens
        # are not the same claim.
        #
        # DECLARED in unmapped_cells: somebody looked, and the reason they wrote
        # is the one that reaches the cell. INAPPLICABLE — no collector run fills
        # a cell with no instrument behind it, so counting it as a collection
        # failure made the coverage floor unreachable. It is still reported, as
        # the ambition ratio, which is what it actually is.
        #
        # NOT declared: universe.yaml maps an instrument here and the store has
        # no row for it, so the universe collector owes it. That is a COLLECTION
        # gap and stays in the denominator; calling it structural would blame the
        # declaration for a seeding failure and hide the failure in the ambition
        # gap, where nobody is looking for it.
        declared = self._unmapped.get((market, sector))

        def blanket(cell: Cell) -> Cell:
            if declared is not None:
                return Cell.inapplicable(
                    cell.indicator,
                    cell.family,
                    declared,
                    window=cell.window,
                    code=REASON_NO_INSTRUMENT,
                )
            return Cell.absent(
                cell.indicator,
                cell.family,
                f"universe.yaml maps an instrument to {market}/{sector} and the store "
                "holds no row for it, so nothing could be measured. Run "
                "`obliskiq-collect universe`; if the conid does not resolve, pin one on "
                "the row or declare the cell under unmapped_cells with its reason.",
                window=cell.window,
                code=REASON_NOT_SEEDED,
            )

        # THE MACRO BLANKET, and its one exception. Indicators 11-13 and 15 read
        # the market spec and the macro store and touch no instrument data at
        # all, so they compute perfectly well for a market nothing measures.
        # Blanketing them was costing china and south_africa their entire macro
        # row for want of an equity fund — and it applies to BOTH cases above,
        # because a policy rate does not become unreadable when the equity fund
        # beside it fails to seed either. (south_africa has since left the axis:
        # nothing on this route measured it. The exception is not about that one
        # market and both of its branches stay pinned in tests/test_compute_grid.py.)
        #
        # The blanket still covers the other eleven sectors, and deliberately:
        # per-market macro is one fact, and repeating it across twelve sector
        # cells would inflate coverage twelvefold with duplicates of a single
        # reading. The `broad` cell is where a market-level fact belongs.
        if sector != BROAD_SECTOR:
            return [blanket(cell) for cell in cells]

        # Read only now, and only for the one cell that can use it: an unmapped
        # market has eleven other sectors, and computing four stores' freshness
        # for each of them would be a hundred queries an answer is never taken
        # from.
        freshness = self._freshness(None, as_of, spec)
        quotes = self._quote_status(None, as_of)
        return [
            self._graded(cell, freshness, quotes)
            if cell.family in MACRO_ONLY_FAMILIES
            else blanket(cell)
            for cell in cells
        ]

    def _graded(
        self, cell: Cell, freshness: dict[str, Freshness], quotes: dict[str, str | None]
    ) -> Cell:
        """One cell, against the freshness of the inputs ITS family actually reads.

        This used to be one verdict on the newest daily bar, stamped on all
        every leaf of the market x sector. Both directions of that were
        wrong and neither was visible: a policy-rate cell, which reads a
        published macro series and no bar at all, went stale because an equity
        bar was old; and a months-old vendor IV percentile read `ok` because the
        bars beside it were fresh.
        """
        labelled = cell.labelled(_first_status(FAMILY_INPUTS.get(cell.family, ()), quotes))
        reason = _stale_reason(FAMILY_INPUTS.get(cell.family, ()), freshness)
        return labelled if reason is None else labelled.stale(reason)

    def _freshness(
        self, contract_id: int | None, as_of: date, spec: dict[str, Any]
    ) -> dict[str, Freshness]:
        """The age of the newest row in each store this cell's families read.

        Every read is bounded at ``as_of``. The bar read was not, which meant a
        grid rebuilt for a past date measured freshness against bars recorded
        after it: ``age`` went negative, the comparison passed, and the cell was
        stamped `ok` on data that did not exist on the day it claims. Same class
        as the unbounded snapshot count, in the function that decides staleness.

        The bar read is also a ``MAX`` now rather than a windowed series. It used
        to ask for a lookback of a few multiples of the staleness window, so an
        instrument whose newest bar fell OUTSIDE that lookback returned nothing
        and read as "no bars at all" — absence, not staleness. The stalest
        instruments were the ones reported `ok`.

        ``contract_id`` is None on a market with no measuring instrument, whose
        `broad` cell still carries macro. The three instrument-backed stores are
        then EMPTY rather than old — ``newest`` is None, so ``age_days`` is None,
        so :attr:`Freshness.is_stale` is False and :func:`_stale_reason` skips
        the group. Absent is not stale, here as everywhere else.
        """
        snapshots = (
            underlying_snapshots(self._store, contract_id, until=as_of, limit=1)
            if contract_id is not None
            else []
        )

        newest: dict[str, date | None] = {
            INPUT_DAILY_BARS: (
                None
                if contract_id is None
                else latest_bar_date(self._store, contract_id, until=as_of)
            ),
            INPUT_OPTION_CHAIN: (
                None
                if contract_id is None
                else latest_chain_date(self._store, contract_id, until=as_of)
            ),
            INPUT_UNDERLYING_SNAPSHOT: snapshots[-1].snapshot_date if snapshots else None,
            INPUT_MACRO: self._newest_macro(spec, as_of),
        }
        return {
            source: Freshness(
                source=source,
                newest=latest,
                age_days=None if latest is None else (as_of - latest).days,
                window_days=int(self._freshness_days[source]),
            )
            for source, latest in newest.items()
        }

    def _newest_macro(self, spec: dict[str, Any], as_of: date) -> date | None:
        """The newest RELEASE among the macro series this market's cells read.

        Release, not reference: aligning on the month a print describes is what
        builds look-ahead bias into every percentile derived from it, and
        :meth:`MacroSeries.known_at` already refuses to. The newest across the
        market's declared series, because one late-publishing series should not
        make the whole macro side of a market look current, nor one absent
        series make it look dead.
        """
        newest: date | None = None
        for key in _MACRO_SERIES_KEYS:
            series_id = spec.get(key)
            if not series_id:
                continue
            series = macro_series(self._store, str(series_id))
            observation = series.known_at(as_of)
            if observation is None:
                continue
            stamp = observation.release_date or observation.reference_date
            if stamp is not None and (newest is None or stamp > newest):
                newest = stamp
        return newest

    def _quote_status(self, contract_id: int | None, as_of: date) -> dict[str, str | None]:
        """The market-data state of the newest row in each quote-bearing store.

        A fallback: the two families that hold the smile they measured set their
        own and this never overwrites them. Everything else — the IV percentile
        above all — reads one row here and would otherwise publish a FROZEN
        figure with nothing saying so.

        With no instrument there are no quotes to be in any state, and the macro
        cells that still compute there have no quote state at all — so both
        entries stay None rather than being invented.
        """
        if contract_id is None:
            return {INPUT_OPTION_CHAIN: None, INPUT_UNDERLYING_SNAPSHOT: None}
        snapshots = underlying_snapshots(self._store, contract_id, until=as_of, limit=1)
        chain_session = latest_chain_date(self._store, contract_id, until=as_of)
        chain_status: str | None = None
        if chain_session is not None:
            rows = chain_rows(self._store, underlying_id=contract_id, snapshot_date=chain_session)
            statuses = {row.market_data_status for row in rows if row.market_data_status}
            chain_status = sorted(statuses)[0] if statuses else None
        return {
            INPUT_OPTION_CHAIN: chain_status,
            INPUT_UNDERLYING_SNAPSHOT: snapshots[-1].market_data_status if snapshots else None,
        }

    def _coverage_row(
        self, market: str, sector: str, instrument: dict[str, Any] | None
    ) -> CoverageRow:
        if instrument is None:
            # The declared reason, verbatim, or the seeding gap. A generic
            # "explicit coverage gap" said the same eleven words about every
            # unmapped cell on the board, which told a reader nothing they could
            # act on and made the word "explicit" untrue.
            declared = self._unmapped.get((market, sector))
            return CoverageRow(
                market=market,
                sector=sector,
                contract_id=None,
                symbol=None,
                instrument_kind=KIND_NONE,
                is_native_index=False,
                top2_concentration=None,
                hedged=None,
                coverage_note=declared
                or (
                    "universe.yaml maps an instrument to this cell and the store holds no "
                    "row for it. A collection gap, not a declared one: run "
                    "`obliskiq-collect universe`."
                ),
            )

        asset_class = str(instrument.get("asset_class") or "").upper()
        is_index = bool(instrument.get("is_native_index"))
        if is_index or asset_class == ASSET_INDEX:
            kind = KIND_NATIVE_INDEX
        elif asset_class == ASSET_ETF:
            kind = KIND_ETF
        else:
            kind = KIND_EQUITY

        note = instrument.get("coverage_note")
        top2 = instrument.get("top2_concentration")
        if kind == KIND_ETF:
            note = self._etf_note(str(instrument.get("symbol")), top2, note)
        return CoverageRow(
            market=market,
            sector=sector,
            contract_id=_contract_id(instrument),
            symbol=str(instrument.get("symbol")),
            instrument_kind=kind,
            is_native_index=is_index,
            top2_concentration=None if top2 is None else float(top2),
            hedged=None if instrument.get("hedged") is None else bool(instrument.get("hedged")),
            coverage_note=note,
        )

    def _etf_note(self, symbol: str, top2: Any, declared: Any) -> str:
        base = str(declared) if declared else ""
        if top2 is None:
            return (
                f"{base} TOP-TWO CONCENTRATION NOT SOURCED for {symbol}: there is no free "
                "global holdings feed, so the two-stock test cannot run. Treat any "
                "sector or country claim through this fund as unverified until a dated "
                "holdings disclosure supplies the figure."
            ).strip()
        if float(top2) > self._top2_flag:
            return (
                f"{base} TWO-STOCK ANALYSIS: {symbol}'s top two holdings are "
                f"{float(top2):.0%} of the fund, above the "
                f"{self._top2_flag:.0%} flag. This cell is a read on two companies, not "
                "on a sector or a country."
            ).strip()
        return base or None  # type: ignore[return-value]

    def _write_cells(
        self, cells: Sequence[tuple[str, str, int | None, Cell]], *, as_of: date
    ) -> None:
        stamp = utc_stamp()
        # The one physical write site for every leaf of every family: a value
        # that reached here non-finite is caught here, whichever of the fifteen
        # families produced it. See _sanitize_non_finite.
        cells = [
            (market, sector, contract_id, _sanitize_non_finite(cell))
            for market, sector, contract_id, cell in cells
        ]
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO grid (
                    as_of, market, sector, indicator, value, status, reason, window,
                    indicator_family, contract_id, units, reason_code, n_obs,
                    market_data_status, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (as_of, market, sector, indicator) DO UPDATE SET
                    value = excluded.value,
                    status = excluded.status,
                    reason = excluded.reason,
                    window = excluded.window,
                    indicator_family = excluded.indicator_family,
                    contract_id = excluded.contract_id,
                    units = excluded.units,
                    reason_code = excluded.reason_code,
                    n_obs = excluded.n_obs,
                    market_data_status = excluded.market_data_status,
                    computed_at = excluded.computed_at
                """,
                [
                    (
                        iso_date(as_of),
                        market,
                        sector,
                        cell.indicator,
                        cell.value,
                        cell.status,
                        cell.reason,
                        cell.window,
                        cell.family,
                        contract_id,
                        cell.units,
                        cell.reason_code,
                        cell.n_obs,
                        cell.market_data_status,
                        stamp,
                    )
                    for market, sector, contract_id, cell in cells
                ],
            )

    def _write_coverage(self, rows: Sequence[CoverageRow], *, as_of: date) -> None:
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO grid_coverage (
                    as_of, market, sector, contract_id, symbol, instrument_kind,
                    is_native_index, top2_concentration, hedged, coverage_note, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (as_of, market, sector) DO UPDATE SET
                    contract_id = excluded.contract_id,
                    symbol = excluded.symbol,
                    instrument_kind = excluded.instrument_kind,
                    is_native_index = excluded.is_native_index,
                    top2_concentration = excluded.top2_concentration,
                    hedged = excluded.hedged,
                    coverage_note = excluded.coverage_note,
                    computed_at = excluded.computed_at
                """,
                [
                    (
                        iso_date(as_of),
                        row.market,
                        row.sector,
                        row.contract_id,
                        row.symbol,
                        row.instrument_kind,
                        int(row.is_native_index),
                        row.top2_concentration,
                        None if row.hedged is None else int(row.hedged),
                        row.coverage_note,
                        stamp,
                    )
                    for row in rows
                ],
            )


def _sanitize_non_finite(cell: Cell) -> Cell:
    """A non-finite value never reaches the store labelled ``ok``.

    Every leaf is expected to guard its own arithmetic — the fifteen families do,
    and ``compute/stats.py``'s functions return ``None`` rather than a number
    nobody could defend. This is the backstop for the one that does not: the
    schema's own ``CHECK (status = 'ok' OR reason IS NOT NULL)`` says nothing
    about an ``ok`` row needing a VALUE, so a ``nan`` (which SQLite stores as
    ``NULL``) or an ``inf`` (which it stores faithfully) can reach ``grid`` as a
    cell claiming to stand behind a reading it does not have. From there
    ``server/encode.py``'s non-finite-to-null rule turns the ``inf`` into JSON
    ``null`` on the way out, and a caller receives ``{"value": null, "status":
    "ok", "reason": null}`` — the one shape this design exists to refuse: an
    absence presented as a measurement. Demoted to ``n/a`` here instead, with the
    sample count it still carries and a reason a coverage report can group on.
    """
    if cell.status != STATUS_OK or cell.value is None or math.isfinite(cell.value):
        return cell
    return replace(
        cell,
        value=None,
        status=STATUS_NA,
        reason=(
            f"{cell.indicator} computed {cell.value!r}, which is not a finite number. "
            "Reported as absent rather than as a measurement nobody could defend."
        ),
        reason_code=REASON_NON_FINITE,
    )


def _options_unavailable_reason(instrument: dict[str, Any] | None) -> str | None:
    """Why no option chain is reachable for this instrument, or None if one is.

    A row that collects a chain always returns None, whatever else it declares:
    the chain is demonstrably reachable, so a missing snapshot is a collection
    gap and belongs in the coverage denominator. Otherwise the row's own declared
    reason is used — and a row that declares nothing also returns None, so an
    undeclared instrument stays a collection gap rather than quietly excusing
    itself from the metric. The loader is what stops that being the silent case.
    """
    if instrument is None:
        return None
    if instrument.get("collect_chain"):
        return None
    reason = instrument.get("options_unavailable_reason")
    return str(reason) if reason else None


def coverage_report(store: Store, *, as_of: date) -> GridReport:
    """Re-derive the coverage report from what is actually in the store.

    The self-audit reports grid coverage — cells populated, cells n/a, and why —
    and it reads the table rather than trusting the builder's own tally.
    """
    rows = store.execute(
        "SELECT status, reason, reason_code, indicator_family FROM grid WHERE as_of = ?",
        (iso_date(as_of),),
    ).fetchall()
    statuses = Counter(str(row["status"]) for row in rows)
    reasons = Counter(
        str(row["reason_code"] or _UNCLASSIFIED) for row in rows if row["status"] != STATUS_OK
    )
    examples: dict[str, str] = {}
    for row in rows:
        code = str(row["reason_code"] or _UNCLASSIFIED)
        if row["status"] != STATUS_OK and row["reason"] and code not in examples:
            examples[code] = str(row["reason"])
    cells = store.execute(
        "SELECT COUNT(*) FROM grid_coverage WHERE as_of = ? AND instrument_kind != ?",
        (iso_date(as_of), KIND_NONE),
    ).fetchone()
    markets = store.execute(
        "SELECT COUNT(DISTINCT market), COUNT(DISTINCT sector) FROM grid WHERE as_of = ?",
        (iso_date(as_of),),
    ).fetchone()
    families = sorted({str(row["indicator_family"]) for row in rows if row["indicator_family"]})
    return GridReport(
        as_of=as_of,
        markets=int(markets[0]) if markets else 0,
        sectors=int(markets[1]) if markets else 0,
        cells_written=len(rows),
        populated=statuses.get(STATUS_OK, 0),
        absent=statuses.get(STATUS_NA, 0),
        stale=statuses.get(STATUS_STALE, 0),
        inapplicable=statuses.get(STATUS_INAPPLICABLE, 0),
        families=tuple(families),
        reasons=dict(reasons),
        examples=examples,
        measured_cells=int(cells[0]) if cells else 0,
    )


@dataclass(frozen=True)
class FamilyReach:
    """How much of the declared grid one indicator family can actually see.

    A ranked view orders the cells of ONE family, and the four views are printed
    the same way whatever they ordered. Before this, `widest_iv_hv` ranked the
    two cells in the whole grid with a reachable option chain while looking
    exactly like `most_extended`, which ranks a hundred — and the only way to
    discover that was to read universe.yaml and work out which rows set
    ``collect_chain``. A denominator nobody can see is a denominator nobody
    checks.

    Counted in CELLS, not leaves: a family emits between one and ten leaves per
    market x sector, so a leaf count would rank the wordy families above the
    reaching ones. A cell counts as reached when any leaf of the family there is
    not ``inapplicable``, and as populated when any is ``ok`` or ``stale``.
    """

    family: str
    cells_total: int
    cells_reachable: int
    cells_populated: int
    #: The structural code that keeps this family out of most of the grid, and
    #: how many CELLS it accounts for — not leaves. A family emits up to ten
    #: leaves per cell, so a leaf count beside a cell count would read as though
    #: the wordier families were the more obstructed ones. None where nothing is
    #: inapplicable.
    dominant_blocker: str | None = None
    blocked_cells: int = 0

    @property
    def reach_pct(self) -> float:
        return 0.0 if not self.cells_total else self.cells_reachable / self.cells_total

    def describe(self) -> str:
        blocker = (
            ""
            if not self.dominant_blocker
            else f"; {self.blocked_cells} blocked by {self.dominant_blocker}"
        )
        return (
            f"{self.family}: reaches {self.cells_reachable}/{self.cells_total} cells "
            f"({self.reach_pct:.0%}), {self.cells_populated} populated{blocker}"
        )


def family_reach(store: Store, *, as_of: date) -> list[FamilyReach]:
    """Per-family reach, read back from ``grid`` rather than recomputed.

    The grid build already decided every cell's status against the declarations,
    so deriving this from the table keeps one owner for the fact. Ordered by
    reach, ascending: the families a session should trust least come first.
    """
    rows = store.execute(
        "SELECT indicator_family, market, sector, status, reason_code "
        "FROM grid WHERE as_of = ? AND indicator_family IS NOT NULL",
        (iso_date(as_of),),
    ).fetchall()

    total: dict[str, set[tuple[str, str]]] = {}
    reachable: dict[str, set[tuple[str, str]]] = {}
    populated: dict[str, set[tuple[str, str]]] = {}
    # code -> the CELLS it blocks, deduplicated. Counting rows would report a
    # family's leaf count multiplied by its blocked cells, which is a number
    # about neither.
    blockers: dict[str, dict[str, set[tuple[str, str]]]] = {}
    for row in rows:
        family = str(row["indicator_family"])
        cell = (str(row["market"]), str(row["sector"]))
        status = str(row["status"])
        total.setdefault(family, set()).add(cell)
        reachable.setdefault(family, set())
        populated.setdefault(family, set())
        blockers.setdefault(family, {})
        if status == STATUS_INAPPLICABLE:
            if row["reason_code"]:
                blockers[family].setdefault(str(row["reason_code"]), set()).add(cell)
            continue
        reachable[family].add(cell)
        if status in (STATUS_OK, STATUS_STALE):
            populated[family].add(cell)

    out: list[FamilyReach] = []
    for family in sorted(total):
        # A cell can be blocked in one family by two codes across its leaves, so
        # the shares need not sum to the unreached count. The dominant one is
        # what a reader acts on.
        counted = Counter(
            {code: len(cells - reachable[family]) for code, cells in blockers[family].items()}
        ).most_common(1)
        out.append(
            FamilyReach(
                family=family,
                cells_total=len(total[family]),
                cells_reachable=len(reachable[family]),
                cells_populated=len(populated[family]),
                dominant_blocker=counted[0][0] if counted and counted[0][1] else None,
                blocked_cells=counted[0][1] if counted else 0,
            )
        )
    return sorted(out, key=lambda item: (item.cells_reachable, item.family))


def latest_grid_date(store: Store) -> date | None:
    row = store.execute("SELECT MAX(as_of) FROM grid").fetchone()
    return parse_date(row[0]) if row and row[0] else None


@dataclass(frozen=True)
class GridCell:
    """One cell, with the disclosure that makes it readable.

    A value without its window, its units and the instrument that produced it is
    not usable evidence: 3.1% over five days and 3.1% over a year are different
    facts, and a figure measured on a US-listed wrapper is a different claim
    about Korea than one measured on the KOSPI.
    """

    as_of: date
    market: str
    sector: str
    indicator: str
    indicator_family: str | None
    value: float | None
    status: str
    reason: str | None
    reason_code: str | None
    window: str | None
    units: str | None
    contract_id: int | None
    computed_at: str | None
    #: The market-data state of the quotes behind this value — FROZEN, DELAYED,
    #: REALTIME, whatever the route reported. NULL on a macro cell, which reads a
    #: published series and has no quote state, and where the route said nothing.
    #: SKILL.md requires anything frozen or delayed to be labelled indicative in
    #: every table it appears in; this is how the grid carries that label.
    market_data_status: str | None = None
    #: How many readings the value was computed FROM. Migration 007 added the
    #: column so a reader can tell a five-year percentile from a twenty-one-point
    #: one; it was written and then dropped by every reader between here and the
    #: MCP response, so the honesty it exists for never reached the consumer.
    #: NULL where the measurement has no meaningful count, never 0 as a stand-in.
    n_obs: int | None = None
    #: The instrument disclosure from ``grid_coverage`` for this market x sector.
    coverage: CoverageRow | None = None


def read_cell_history(
    store: Store,
    *,
    market: str,
    sector: str,
    indicator: str,
    until: date | None = None,
    limit: int = 0,
) -> list[GridCell]:
    """One cell across the sessions the grid has accumulated, newest first.

    ``grid`` is keyed on ``(as_of, market, sector, indicator)`` and every build
    upserts its own session, so the table has always been a time series — and
    nothing could read it as one. Every reader took a single ``as_of``, so a
    consumer asked to tell a rising reading from a falling one could see only the
    window statistics computed INSIDE one snapshot.

    Newest first because the question is almost always "what has it been doing
    lately", and a caller that wants the series in order can reverse a short list
    more cheaply than the store can scan a long one.

    A session where the cell was not measured is returned with its status and
    reason like any other, rather than skipped: a gap in the middle of a series
    is information, and silently closing it up would make the series look
    continuous when it is not.
    """
    sql = (
        "SELECT as_of, market, sector, indicator, indicator_family, value, status, reason, "
        "reason_code, window, units, contract_id, market_data_status, computed_at, n_obs "
        "FROM grid WHERE market = ? AND sector = ? AND indicator = ?"
    )
    params: list[object] = [market, sector, indicator]
    if until is not None:
        sql += " AND as_of <= ?"
        params.append(iso_date(until))
    sql += " ORDER BY as_of DESC"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    cells: list[GridCell] = []
    for row in store.execute(sql, params).fetchall():
        session = parse_date(row["as_of"])
        if session is None:
            continue
        cells.append(
            GridCell(
                as_of=session,
                market=str(row["market"]),
                sector=str(row["sector"]),
                indicator=str(row["indicator"]),
                indicator_family=row["indicator_family"],
                value=None if row["value"] is None else float(row["value"]),
                status=str(row["status"]),
                reason=row["reason"],
                reason_code=row["reason_code"],
                window=row["window"],
                units=row["units"],
                n_obs=None if row["n_obs"] is None else int(row["n_obs"]),
                contract_id=None if row["contract_id"] is None else int(row["contract_id"]),
                computed_at=row["computed_at"],
                market_data_status=row["market_data_status"],
            )
        )
    return cells


def read_cells(
    store: Store,
    *,
    as_of: date,
    market: str | None = None,
    sector: str | None = None,
    indicator: str | None = None,
    limit: int | None = None,
) -> list[GridCell]:
    """Cells for one session, narrowed by whichever axes the caller named.

    Every cell carries its own reason when it is not ``ok``; the schema enforces
    that and this reader never drops it. A cell that does not exist is absent
    from the result rather than returned as a zero.
    """
    sql = (
        "SELECT as_of, market, sector, indicator, indicator_family, value, status, reason, "
        "reason_code, window, units, contract_id, market_data_status, computed_at, n_obs "
        "FROM grid WHERE as_of = ?"
    )
    params: list[object] = [iso_date(as_of)]
    for column, value in (("market", market), ("sector", sector), ("indicator", indicator)):
        if value is not None:
            sql += f" AND {column} = ?"
            params.append(value)
    sql += " ORDER BY market, sector, indicator"
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    rows = store.execute(sql, params).fetchall()
    coverage = _coverage_index(store, as_of=as_of)
    cells: list[GridCell] = []
    for row in rows:
        session = parse_date(row["as_of"])
        if session is None:
            continue
        cells.append(
            GridCell(
                as_of=session,
                market=str(row["market"]),
                sector=str(row["sector"]),
                indicator=str(row["indicator"]),
                indicator_family=row["indicator_family"],
                value=None if row["value"] is None else float(row["value"]),
                status=str(row["status"]),
                reason=row["reason"],
                reason_code=row["reason_code"],
                window=row["window"],
                units=row["units"],
                n_obs=None if row["n_obs"] is None else int(row["n_obs"]),
                contract_id=None if row["contract_id"] is None else int(row["contract_id"]),
                computed_at=row["computed_at"],
                market_data_status=row["market_data_status"],
                coverage=coverage.get((str(row["market"]), str(row["sector"]))),
            )
        )
    return cells


def _coverage_index(store: Store, *, as_of: date) -> dict[tuple[str, str], CoverageRow]:
    rows = store.execute(
        "SELECT market, sector, contract_id, symbol, instrument_kind, is_native_index, "
        "top2_concentration, hedged, coverage_note FROM grid_coverage WHERE as_of = ?",
        (iso_date(as_of),),
    ).fetchall()
    return {
        (str(row["market"]), str(row["sector"])): CoverageRow(
            market=str(row["market"]),
            sector=str(row["sector"]),
            contract_id=None if row["contract_id"] is None else int(row["contract_id"]),
            symbol=row["symbol"],
            instrument_kind=str(row["instrument_kind"]),
            is_native_index=bool(row["is_native_index"]),
            top2_concentration=(
                None if row["top2_concentration"] is None else float(row["top2_concentration"])
            ),
            hedged=None if row["hedged"] is None else bool(row["hedged"]),
            coverage_note=row["coverage_note"],
        )
        for row in rows
    }


def _contract_id(instrument: dict[str, Any] | None) -> int | None:
    if instrument is None:
        return None
    value = instrument.get("contract_id")
    return None if value is None else int(value)


#: A cell absent with no code is a bug in whichever family produced it, so the
#: histogram names it rather than hiding it in a bucket that looks deliberate.
_UNCLASSIFIED = "unclassified"

#: How far back to look when checking freshness: a few staleness windows, so a
#: gap longer than the window still finds the last bar that exists.
