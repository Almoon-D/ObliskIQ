"""The views: queries over the measurement store, on the axes the configuration declares.

* **The equity grid** (market x sector): every leaf of every cell of the newest build on or before
  the day asked, with each cell's disclosure (which instrument measured it, its kind, its
  currency hedge, its top-two concentration where a dated holdings file supplies one, and the
  declared reason where nothing does).
* **The macro board** (region x asset class), the same way.
* **Family reach**: how much of the declared grid each family can see, in cells (a family emits
  up to ten leaves a cell, so a leaf count would rank the wordy families as the far-reaching
  ones), worst first.
* **The correlation matrix**: every pair's leaves, a shift flagged beyond
  ``grid.correlation_shift_sd`` of its own norm.

Every leaf is returned with its status and reason: a leaf the store could not measure is shown
as such, never dropped and never a zero.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass
from ..domain.measurement import EntityKind, Measurement, MeasurementStatus
from ..store import measurements as measurements_repo
from ..store import universe as universe_repo
from ..store.db import Store
from .correlations import leaves_for
from .equity_grid import cell_id, resolve_conid
from .macro_board import board_id

#: The two largest holdings a concentration is measured on.
TOP_HOLDINGS: Final = 2


@dataclass(frozen=True, slots=True)
class Disclosure:
    """Which instrument measured a cell, and what it discloses about that measurement."""

    symbol: str | None
    kind: str
    seeded: bool
    hedged: bool | None
    top2_concentration: float | None
    note: str | None


@dataclass(frozen=True, slots=True)
class GridCellView:
    market: str
    sector: str
    leaves: Mapping[str, Measurement]
    disclosure: Disclosure


@dataclass(frozen=True, slots=True)
class EquityGridView:
    """The equity grid as built for ``session`` (``None``: nothing built by the day asked)."""

    as_of: date
    session: date | None
    markets: tuple[str, ...]
    sectors: tuple[str, ...]
    cells: Mapping[tuple[str, str], GridCellView] = field(default_factory=dict)

    def leaf(self, market: str, sector: str, leaf: str) -> Measurement | None:
        cell = self.cells.get((market, sector))
        return None if cell is None else cell.leaves.get(leaf)


@dataclass(frozen=True, slots=True)
class BoardCellView:
    region: str
    asset_class: str
    leaves: Mapping[str, Measurement]
    inputs: str


@dataclass(frozen=True, slots=True)
class MacroBoardView:
    as_of: date
    session: date | None
    regions: tuple[str, ...]
    asset_classes: tuple[str, ...]
    cells: Mapping[tuple[str, str], BoardCellView] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FamilyReach:
    """How much of the declared grid one family can see, in cells."""

    family: str
    cells_total: int
    cells_reachable: int
    cells_populated: int
    dominant_blocker: str | None
    blocked_cells: int

    @property
    def reach_pct(self) -> float:
        return 0.0 if not self.cells_total else self.cells_reachable / self.cells_total

    def describe(self) -> str:
        blocker = (
            f"; {self.blocked_cells} blocked by {self.dominant_blocker}"
            if self.dominant_blocker
            else ""
        )
        return (
            f"{self.family}: reaches {self.cells_reachable}/{self.cells_total} cells "
            f"({self.reach_pct:.0%}), {self.cells_populated} populated{blocker}"
        )


@dataclass(frozen=True, slots=True)
class CorrelationPairView:
    pair: str
    window: int
    leaves: Mapping[str, Measurement]
    flagged: bool


def _group(rows: tuple[Measurement, ...]) -> dict[str, dict[str, Measurement]]:
    out: dict[str, dict[str, Measurement]] = {}
    for row in rows:
        out.setdefault(row.entity_id, {})[row.leaf] = row
    return out


def _disclosure(
    store: Store, settings: ObliskConfig, market: str, sector: str, *, as_of: date
) -> Disclosure:
    universe = settings.universe
    declared_cell = universe.cells()[(market, sector)]
    if declared_cell.symbol is None:
        return Disclosure(None, "none", False, None, None, declared_cell.reason)
    instrument = universe.instrument(declared_cell.symbol)
    assert instrument is not None  # a measured cell's symbol is the instrument that claims it
    seeded = resolve_conid(store, instrument, as_of=as_of) is not None
    hedged = instrument.attributes.hedged
    kind = (
        "native_index"
        if universe.grid.markets[market].native_index == instrument.symbol
        else instrument.asset_class.value
    )
    note = instrument.coverage_note
    top2: float | None = None
    if instrument.asset_class in (AssetClass.ETF, AssetClass.FUND):
        holdings = universe_repo.holdings_of(store, instrument.symbol, as_of=as_of)
        weights = sorted((h.weight for h in holdings if h.weight is not None), reverse=True)
        flag = settings.grid.etf_top2_concentration_flag
        if len(weights) >= TOP_HOLDINGS:
            top2 = sum(weights[:TOP_HOLDINGS])
            if top2 > flag:
                note = (
                    f"{note or ''} TWO-STOCK READ: {instrument.symbol}'s top two holdings are "
                    f"{top2:.0%} of the fund, above the {flag:.0%} flag; this cell reads two "
                    "companies, not a sector or a country"
                ).strip()
        else:
            note = (
                f"{note or ''} TOP-TWO CONCENTRATION NOT SOURCED for {instrument.symbol}: no dated "
                "holdings file is held, so the two-stock test could not run; treat a sector or "
                "country claim through this fund as unverified"
            ).strip()
    return Disclosure(instrument.symbol, kind, seeded, hedged, top2, note)


def equity_grid(store: Store, settings: ObliskConfig, *, as_of: date) -> EquityGridView:
    """The equity grid of the newest build on or before ``as_of``."""
    grid = settings.universe.grid
    markets, sectors = tuple(sorted(grid.markets)), tuple(grid.sectors)
    session = measurements_repo.measured_session(store, EntityKind.EQUITY_CELL, as_of=as_of)
    if session is None:
        return EquityGridView(as_of, None, markets, sectors)
    grouped = _group(
        measurements_repo.session_measurements(store, EntityKind.EQUITY_CELL, as_of=session)
    )
    cells = {
        (market, sector): GridCellView(
            market,
            sector,
            grouped.get(cell_id(market, sector), {}),
            _disclosure(store, settings, market, sector, as_of=session),
        )
        for market in markets
        for sector in sectors
    }
    return EquityGridView(as_of, session, markets, sectors, cells)


def macro_board(store: Store, settings: ObliskConfig, *, as_of: date) -> MacroBoardView:
    """The macro board of the newest build on or before ``as_of``."""
    board = settings.universe.macro_board
    regions, classes = tuple(sorted(board.regions)), tuple(board.asset_classes)
    session = measurements_repo.measured_session(store, EntityKind.MACRO_CELL, as_of=as_of)
    if session is None:
        return MacroBoardView(as_of, None, regions, classes)
    grouped = _group(
        measurements_repo.session_measurements(store, EntityKind.MACRO_CELL, as_of=session)
    )
    claims = settings.universe.board_claims()
    cells: dict[tuple[str, str], BoardCellView] = {}
    for region in regions:
        for asset_class in classes:
            ref = board_id(region, asset_class)
            declared = board.regions[region].cells[asset_class]
            inputs = declared.unmapped or ", ".join(
                [*declared.series, *declared.roots, *([claims[ref]] if ref in claims else [])]
            )
            cells[(region, asset_class)] = BoardCellView(
                region, asset_class, grouped.get(ref, {}), inputs
            )
    return MacroBoardView(as_of, session, regions, classes, cells)


def family_reach(rows: tuple[Measurement, ...] | list[Measurement]) -> list[FamilyReach]:
    """Per-family reach of an equity-grid build, worst first: a cell is reached where any leaf
    of the family is not ``inapplicable``, populated where any is measured."""
    total: dict[str, set[str]] = {}
    reachable: dict[str, set[str]] = {}
    populated: dict[str, set[str]] = {}
    blockers: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        family = row.indicator
        total.setdefault(family, set()).add(row.entity_id)
        reachable.setdefault(family, set())
        populated.setdefault(family, set())
        blockers.setdefault(family, {})
        if row.status is MeasurementStatus.INAPPLICABLE:
            blockers[family].setdefault(row.reason_code or "", set()).add(row.entity_id)
            continue
        reachable[family].add(row.entity_id)
        if row.status.has_value:
            populated[family].add(row.entity_id)
    out: list[FamilyReach] = []
    for family in sorted(total):
        counted = Counter(
            {code: len(cells - reachable[family]) for code, cells in blockers[family].items()}
        ).most_common(1)
        blocker, blocked = counted[0] if counted and counted[0][1] else (None, 0)
        out.append(
            FamilyReach(
                family=family,
                cells_total=len(total[family]),
                cells_reachable=len(reachable[family]),
                cells_populated=len(populated[family]),
                dominant_blocker=blocker,
                blocked_cells=blocked,
            )
        )
    return sorted(out, key=lambda item: (item.cells_reachable, item.family))


def grid_reach(store: Store, *, as_of: date) -> list[FamilyReach]:
    """Family reach of the newest equity-grid build on or before ``as_of`` (empty when none)."""
    session = measurements_repo.measured_session(store, EntityKind.EQUITY_CELL, as_of=as_of)
    if session is None:
        return []
    return family_reach(
        measurements_repo.session_measurements(store, EntityKind.EQUITY_CELL, as_of=session)
    )


def correlation_matrix(
    store: Store, settings: ObliskConfig, *, as_of: date
) -> list[CorrelationPairView]:
    """Every pair and window of the newest matrix on or before ``as_of``, flagged where the
    shift lies beyond ``grid.correlation_shift_sd`` of the pair's own norm."""
    session = measurements_repo.measured_session(store, EntityKind.CORRELATION_PAIR, as_of=as_of)
    if session is None:
        return []
    grouped = _group(
        measurements_repo.session_measurements(store, EntityKind.CORRELATION_PAIR, as_of=session)
    )
    threshold = settings.grid.correlation_shift_sd
    out: list[CorrelationPairView] = []
    for pair, leaves in sorted(grouped.items()):
        for window in settings.grid.correlation_windows_days:
            names = leaves_for(window)
            subset = {name: leaves[name] for name in names if name in leaves}
            shift = subset.get(names[-1])
            flagged = (
                shift is not None
                and shift.value is not None
                and shift.status.has_value
                and abs(shift.value) > threshold
            )
            out.append(CorrelationPairView(pair, window, subset, flagged))
    return out
