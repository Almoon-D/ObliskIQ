"""The equity grid: every market x every sector x the fifteen families, written to the
measurement store.

Ported from lite's ``compute/build_grid.py``. Three properties make the output trustworthy rather
than merely large:

* **Markets are measured on their native index**, declared per cell in the universe (at most one
  instrument a cell); a market measured in another currency is refused at load.
* **A cell with no instrument is declared, never inferred.** A cell declared unmapped carries its
  declared reason verbatim and is ``inapplicable`` (no collector run fills it); a cell the
  universe maps and the store does not hold is ``n/a`` (``instrument_not_seeded``) and stays in
  the collection-health denominator, because seeding owes it. The four macro-only families still
  compute on a market's ``broad`` cell when nothing measures it, and only there: per-market macro
  is one fact, and twelve copies of it would make an unmeasured market look better collected.
* **Freshness is per input.** Each family is graded against the stores it actually reads: a
  family is as fresh as its worst requirement group, a group as fresh as its best member.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from ..config.schema import ObliskConfig
from ..config.schema_universe import Cell as DeclaredCell
from ..config.schema_universe import UniverseInstrument
from ..data.macro.catalog import MacroCatalog
from ..domain.enums import MarketDataStatus
from ..domain.measurement import EntityKind, Measurement, MeasurementStatus
from ..store import measurements as measurements_repo
from ..store import repos
from ..store import sources as sources_repo
from ..store.db import Store
from .cells import Cell, inputs_hash, to_measurement
from .frames import UNREPORTED, BarFrame, MacroReader, MarketSpec, market_spec, worst_status
from .indicators import INDICATOR_FUNCTIONS, CellInputs, CellInstrument, GridParams
from .vocabulary import FAMILY_INPUTS, MACRO_ONLY_FAMILIES, UNCLASSIFIED, Family, Input, Reason


def cell_id(market: str, sector: str) -> str:
    """The entity id of a grid cell."""
    return f"{market}/{sector}"


def split_cell_id(entity_id: str) -> tuple[str, str]:
    market, _, sector = entity_id.partition("/")
    return market, sector


@dataclass(frozen=True, slots=True)
class Freshness:
    """How current one input store is for a cell, against its own window."""

    source: Input
    newest: date | None
    as_of: date
    window_days: int

    @property
    def age_days(self) -> int | None:
        return None if self.newest is None else (self.as_of - self.newest).days

    @property
    def is_stale(self) -> bool:
        """Absent is not stale: a leaf with no input at all is ``n/a`` from its family."""
        age = self.age_days
        return age is not None and age > self.window_days

    def describe(self) -> str:
        return (
            f"the newest {self.source.value} input is from {self.newest}, {self.age_days} days "
            f"old, beyond the {self.window_days}-day grid.freshness_days.{self.source.value} window"
        )


def stale_reason(
    groups: Sequence[Sequence[Input]], freshness: Mapping[Input, Freshness]
) -> str | None:
    """Why a family's inputs are stale, or ``None``: a group is satisfied by one current
    member, and a group with nothing collected is absence, not staleness."""
    failures: list[str] = []
    for group in groups:
        usable = [freshness[source] for source in group if freshness[source].newest is not None]
        if not usable or any(not item.is_stale for item in usable):
            continue
        failures.append(min(usable, key=lambda item: item.age_days or 0).describe())
    if not failures:
        return None
    return (
        "; ".join(failures)
        + ". Every figure in this leaf is indicative, not live; freshness is measured on the "
        "input this family reads, so a leaf beside it may be current"
    )


@dataclass(frozen=True, slots=True)
class GridReport:
    """What one build produced, and what it could not."""

    as_of: date
    markets: int
    sectors: int
    measured_cells: int
    leaves: int
    populated: int
    absent: int
    stale: int
    inapplicable: int
    reasons: Mapping[str, int] = field(default_factory=dict)
    examples: Mapping[str, str] = field(default_factory=dict)

    @property
    def collectible(self) -> int:
        """Leaves some collector is supposed to be able to fill."""
        return self.leaves - self.inapplicable

    @property
    def coverage_pct(self) -> float:
        """Collection health: populated over collectible (never over every declared leaf, which
        would make the floor unreachable and the flag meaningless)."""
        return 0.0 if not self.collectible else self.populated / self.collectible

    @property
    def ambition_pct(self) -> float:
        """The ambition gap: how much of the declared grid anything is even trying to fill.
        Reported, never gated."""
        return 0.0 if not self.leaves else self.collectible / self.leaves

    def summary(self) -> str:
        lines = [
            f"equity grid for {self.as_of}: {self.markets} markets x {self.sectors} sectors, "
            f"{self.measured_cells} of them with a measuring instrument in the store.",
            f"{self.leaves} leaves: {self.populated} populated, {self.absent} n/a, {self.stale} "
            f"stale, {self.inapplicable} inapplicable.",
            f"collection coverage {self.populated}/{self.collectible} ({self.coverage_pct:.0%} "
            f"of what is collectible); ambition {self.collectible}/{self.leaves} "
            f"({self.ambition_pct:.0%} of the declared grid is collectible).",
        ]
        if self.reasons:
            lines.append("why leaves are missing:")
            for code, count in sorted(self.reasons.items(), key=lambda item: (-item[1], item[0])):
                lines.append(f"  {count:>5}  {code}")
                if code in self.examples:
                    lines.append(f"         e.g. {self.examples[code]}")
        return "\n".join(lines)


def tally(
    as_of: date,
    rows: Sequence[Measurement],
    *,
    markets: int,
    sectors: int,
    measured_cells: int,
) -> GridReport:
    """The report of a set of leaves, grouped on the code (the reason names specifics)."""
    statuses = Counter(row.status for row in rows)
    reasons: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for row in rows:
        if row.status is MeasurementStatus.OK:
            continue
        code = row.reason_code or UNCLASSIFIED
        reasons[code] += 1
        if row.reason is not None:
            examples.setdefault(code, row.reason)
    return GridReport(
        as_of=as_of,
        markets=markets,
        sectors=sectors,
        measured_cells=measured_cells,
        leaves=len(rows),
        populated=statuses[MeasurementStatus.OK],
        absent=statuses[MeasurementStatus.NA],
        stale=statuses[MeasurementStatus.STALE],
        inapplicable=statuses[MeasurementStatus.INAPPLICABLE],
        reasons=dict(reasons),
        examples=examples,
    )


def resolve_conid(store: Store, declared: UniverseInstrument, *, as_of: date) -> int | None:
    """The stored instrument standing for a declared one on ``as_of`` (following corrections), or
    ``None`` when it is not pinned or not seeded."""
    if declared.conid is None:
        return None
    found = repos.resolve_instrument(store, declared.conid, as_of=as_of)
    return None if found is None else found.conid


def cell_instrument(
    store: Store, declared: UniverseInstrument, *, as_of: date
) -> CellInstrument | None:
    conid = resolve_conid(store, declared, as_of=as_of)
    if conid is None:
        return None
    return CellInstrument(
        symbol=declared.symbol,
        conid=conid,
        asset_class=declared.asset_class,
        options_unavailable_reason=(
            None if declared.collects_chain else declared.options_unavailable_reason
        ),
    )


def _digest(rows: object) -> str:
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _Inputs:
    """One cell's inputs as sources and fingerprints, computed once per input."""

    store: Store
    reader: MacroReader
    settings: ObliskConfig
    as_of: date
    spec: MarketSpec
    conid: int | None
    benchmark: int | None
    daily: BarFrame
    benchmark_daily: BarFrame
    _done: dict[Input, tuple[str | None, object]] = field(default_factory=dict)

    def describe(self, source: Input) -> tuple[str | None, object]:
        if source not in self._done:
            self._done[source] = self._compute(source)
        return self._done[source]

    def _compute(self, source: Input) -> tuple[str | None, object]:
        conid = self.conid
        if source is Input.DAILY_BARS:
            if conid is None:
                return None, None
            return f"bars:{conid}", (
                _digest(self.daily),
                None if self.benchmark is None else _digest(self.benchmark_daily),
            )
        if source is Input.OPTION_CHAIN:
            if conid is None:
                return None, None
            session = repos.latest_chain_date(self.store, as_of=self.as_of, underlying_conid=conid)
            rows = (
                ()
                if session is None
                else repos.chain_rows(
                    self.store, as_of=self.as_of, underlying_conid=conid, snapshot_date=session
                )
            )
            return f"chain:{conid}", (session, _digest([r.model_dump() for r in rows]))
        if source is Input.UNDERLYING_SNAPSHOT:
            if conid is None:
                return None, None
            newest = repos.snapshots(self.store, conid, as_of=self.as_of, limit=1)
            count = repos.implied_vol_snapshot_count(self.store, conid, as_of=self.as_of)
            return f"snapshot:{conid}", (
                count,
                None if not newest else _digest(newest[-1].model_dump()),
            )
        ids = self.spec.series_ids
        prints = {
            series_id: None
            if (series := self.reader.series(series_id)) is None
            else _digest(
                [(o.reference_date, o.release_date, o.value, o.units) for o in series.observations]
            )
            for series_id in ids
        }
        if self.spec.cot is not None:
            code = self.settings.sources.positioning.contracts[self.spec.cot].code
            reports = sources_repo.cot_reports(self.store, code, as_of=self.as_of)
            prints[self.spec.cot] = _digest([r.model_dump() for r in reports])
        names = [*ids, *([self.spec.cot] if self.spec.cot else [])]
        return ("macro:" + ",".join(names) if names else None), prints


class EquityGridBuilder:
    """Computes every leaf of every market x sector and writes them as measurements."""

    def __init__(self, store: Store, settings: ObliskConfig) -> None:
        self._store = store
        self._settings = settings
        self._params = GridParams.from_settings(settings)
        self._catalog = MacroCatalog.from_settings(settings)
        self._universe = settings.universe
        self._declared = self._universe.cells()
        self._settings_digest = inputs_hash({"params": repr(self._params)})

    def build(self, *, as_of: date, computed_at: datetime) -> list[Measurement]:
        """Every leaf for ``as_of``; the caller writes them (``write``) or reports a dry run."""
        reader = MacroReader(self._store, self._catalog, as_of)
        grid = self._universe.grid
        benchmark = self._benchmark(as_of)
        rows: list[Measurement] = []
        for market in sorted(grid.markets):
            spec = market_spec(self._universe, market)
            for sector in grid.sectors:
                declared = self._declared[(market, sector)]
                rows += self._cell(
                    declared,
                    spec,
                    reader,
                    as_of=as_of,
                    benchmark=benchmark,
                    computed_at=computed_at,
                )
        return rows

    def write(self, rows: Sequence[Measurement]) -> int:
        return measurements_repo.upsert_measurements(self._store, rows)

    def report(self, rows: Sequence[Measurement], *, as_of: date) -> GridReport:
        grid = self._universe.grid
        measured = {
            row.entity_id
            for row in rows
            if row.source is not None and row.source.startswith(("bars:", "chain:", "snapshot:"))
        }
        return tally(
            as_of,
            rows,
            markets=len(grid.markets),
            sectors=len(grid.sectors),
            measured_cells=len(measured),
        )

    # ── internals ──

    def _benchmark(self, as_of: date) -> int | None:
        market = self._settings.grid.benchmark_market
        symbol = self._universe.grid.markets[market].native_index
        declared = None if symbol is None else self._universe.instrument(symbol)
        return None if declared is None else resolve_conid(self._store, declared, as_of=as_of)

    def _cell(
        self,
        declared: DeclaredCell,
        spec: MarketSpec,
        reader: MacroReader,
        *,
        as_of: date,
        benchmark: int | None,
        computed_at: datetime,
    ) -> list[Measurement]:
        instrument_config = (
            None if declared.symbol is None else self._universe.instrument(declared.symbol)
        )
        instrument = (
            None
            if instrument_config is None
            else cell_instrument(self._store, instrument_config, as_of=as_of)
        )
        inputs = CellInputs(
            store=self._store,
            settings=self._settings,
            params=self._params,
            reader=reader,
            as_of=as_of,
            market=declared.market,
            sector=declared.sector,
            spec=spec,
            instrument=instrument,
            benchmark=benchmark,
            benchmark_market=self._settings.grid.benchmark_market,
        )
        described = _Inputs(
            store=self._store,
            reader=reader,
            settings=self._settings,
            as_of=as_of,
            spec=spec,
            conid=inputs.conid,
            benchmark=benchmark,
            daily=inputs.daily,
            benchmark_daily=inputs.benchmark_daily,
        )
        broad = declared.sector == self._universe.grid.broad_sector
        freshness = self._freshness(inputs)
        quotes = self._quote_status(inputs)
        rows: list[Measurement] = []
        for family, compute in INDICATOR_FUNCTIONS:
            blanketed = instrument is None and not (broad and family in MACRO_ONLY_FAMILIES)
            if blanketed:
                cells = [
                    self._blanket(cell, declared, instrument_config) for cell in compute(inputs)
                ]
            else:
                groups = FAMILY_INPUTS[family]
                reason = stale_reason(groups, freshness)
                cells = [self._graded(cell, groups, quotes, reason) for cell in compute(inputs)]
            rows += self._stored(cells, family, inputs, described, computed_at, blanketed=blanketed)
        return rows

    def _stored(
        self,
        cells: Sequence[Cell],
        family: Family,
        inputs: CellInputs,
        described: _Inputs,
        computed_at: datetime,
        *,
        blanketed: bool,
    ) -> list[Measurement]:
        sources: list[str] = []
        prints: dict[str, object] = {"settings": self._settings_digest, "as_of": inputs.as_of}
        if not blanketed:
            for group in FAMILY_INPUTS[family]:
                for source in group:
                    name, fingerprint = described.describe(source)
                    if name is not None:
                        sources.append(name)
                    prints[source.value] = fingerprint
        else:
            prints["declared"] = self._declared[(inputs.market, inputs.sector)].reason
        return [
            to_measurement(
                cell,
                entity_kind=EntityKind.EQUITY_CELL,
                entity_id=cell_id(inputs.market, inputs.sector),
                as_of=inputs.as_of,
                source="+".join(dict.fromkeys(sources)) or None,
                fingerprint=prints,
                computed_at=computed_at,
            )
            for cell in cells
        ]

    @staticmethod
    def _graded(
        cell: Cell,
        groups: Sequence[Sequence[Input]],
        quotes: Mapping[Input, MarketDataStatus | None],
        stale: str | None,
    ) -> Cell:
        """A leaf labelled with the quote state of the first input it reads that has one, and
        stale when its family's inputs are."""
        status = next(
            (quotes[source] for group in groups for source in group if quotes.get(source)), None
        )
        labelled = cell.labelled(status)
        return labelled if stale is None else labelled.stale(stale)

    def _blanket(
        self, cell: Cell, declared: DeclaredCell, instrument: UniverseInstrument | None
    ) -> Cell:
        """A leaf of a cell nothing in the store measures: declared unmapped (inapplicable, with
        the declared reason) or mapped and not seeded (n/a: seeding owes it)."""
        if declared.reason is not None:
            return Cell.inapplicable(
                cell.leaf,
                cell.family,
                declared.reason,
                code=Reason.NO_INSTRUMENT,
                window=cell.window,
            )
        symbol = declared.symbol
        if instrument is not None and instrument.conid is None:
            reason = (
                f"the universe maps {symbol} to {declared.market}/{declared.sector} without a "
                "conid, so nothing is collected for it; `obliskiq universe pin` resolves one"
            )
        else:
            reason = (
                f"the universe maps {symbol} to {declared.market}/{declared.sector} and the store "
                "holds no instrument for it; run `obliskiq collect universe`"
            )
        return Cell.absent(
            cell.leaf, cell.family, reason, code=Reason.NOT_SEEDED, window=cell.window
        )

    def _freshness(self, inputs: CellInputs) -> dict[Input, Freshness]:
        """The newest row of each store this cell reads, bounded at ``as_of``."""
        as_of = inputs.as_of
        conid = inputs.conid
        days = self._settings.grid.freshness_days
        snapshot = (
            () if conid is None else repos.snapshots(self._store, conid, as_of=as_of, limit=1)
        )
        newest: dict[Input, date | None] = {
            Input.DAILY_BARS: None
            if conid is None
            else repos.latest_bar_date(self._store, conid, as_of=as_of),
            Input.OPTION_CHAIN: None
            if conid is None
            else repos.latest_chain_date(self._store, as_of=as_of, underlying_conid=conid),
            Input.UNDERLYING_SNAPSHOT: snapshot[-1].snapshot_date if snapshot else None,
            Input.MACRO: self._newest_release(inputs),
        }
        windows = {
            Input.DAILY_BARS: days.daily_bars,
            Input.OPTION_CHAIN: days.option_chain,
            Input.UNDERLYING_SNAPSHOT: days.underlying_snapshot,
            Input.MACRO: days.macro,
        }
        return {
            source: Freshness(source, newest[source], as_of, windows[source]) for source in Input
        }

    def _newest_release(self, inputs: CellInputs) -> date | None:
        """The newest release among the series this market reads: one late series should not make
        the market's macro side look current, nor one absent series make it look dead."""
        releases = [
            latest.release_date
            for series_id in inputs.spec.series_ids
            if (latest := inputs.reader.latest(series_id)) is not None
            and latest.release_date is not None
        ]
        if inputs.spec.cot is not None:
            code = self._settings.sources.positioning.contracts[inputs.spec.cot].code
            reports = sources_repo.cot_reports(self._store, code, as_of=inputs.as_of)
            if reports:
                releases.append(reports[-1].release_date)
        return max(releases, default=None)

    def _quote_status(self, inputs: CellInputs) -> dict[Input, MarketDataStatus | None]:
        """The quote state of the newest row in each quote-bearing store: the fallback label for a
        family that does not set the one it read."""
        conid = inputs.conid
        if conid is None:
            return {}
        snapshot = repos.snapshots(self._store, conid, as_of=inputs.as_of, limit=1)
        counts = Counter(
            UNREPORTED if row.market_data_status is None else row.market_data_status.value
            for row in inputs.chain.rows()
        )
        return {
            Input.OPTION_CHAIN: worst_status(counts),
            Input.UNDERLYING_SNAPSHOT: snapshot[-1].market_data_status if snapshot else None,
        }
