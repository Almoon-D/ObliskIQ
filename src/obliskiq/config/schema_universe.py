"""The shape of ``config/universe.yaml``: every instrument the system reads, and the axes it is
measured on (MASTER_PLAN §5.3, §5.7).

* **Instruments** carry roles (``data``, ``tradable_candidate``, ``option_underlying``), the
  attributes eligibility is computed from (asset class, domicile, UCITS and KID status, share
  class and its currency hedge, fee, currency, listing venue, lot size, inception) and the
  exposures they represent (core roles, a grid cell, macro-board entities). An instrument whose
  issuer publishes its holdings names the file: it feeds the instrument's look-through and, for
  the market it names, that market's stock candidates (``data/market/candidates.py``). Reading any
  instrument as data is always allowed; whether one may be bought is the jurisdiction's
  eligibility policy, computed from these attributes, never a flag here.
* **The grid** (market x sector) is declared, not derived: every cell is measured by exactly one
  instrument or declared unmapped with its reason, and a matrix with a hole is refused, because a
  status inferred from an absence cannot tell "nobody looked" from "there is nothing".
* **The macro board** (region x asset class) the same way: every cell names its inputs (an
  instrument that claims it, macro series, futures roots) or why it has none.
* **Futures roots** are curves, data only. **Commodity proxies** name the equity that stands in
  for a commodity and the series that tests it.

Ported from ObliskIQ-lite-MCP's ``collect/universe.py`` (the loader's invariants) and
``universe.yaml`` (its content), regrouped into roles, attributes and exposures.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, Field, PositiveInt, StringConstraints, model_validator

from ..domain.base import Conid, CountryCode, CurrencyCode, NonEmptyText
from ..domain.enums import AssetClass, InstrumentRole, KidStatus, ShareClass
from ..domain.tax import Isin
from .base import FrozenModel, Identifier
from .schema_calendars import IbkrExchangeCode

#: ``unmapped_cells[].sectors`` may name this instead of a list: every sector of the market that
#: no instrument measures and no earlier block claims (lite's ``SECTORS_REMAINING``).
SECTORS_REMAINING: Final = "remaining"

#: What a universe instrument may be. An option is a chain row and a future a curve rung, never
#: a declared instrument; a currency pair is a conversion (``execution.currency.pairs``).
DECLARABLE_CLASSES: Final = frozenset(
    {
        AssetClass.STOCK,
        AssetClass.ETF,
        AssetClass.ETC,
        AssetClass.BOND,
        AssetClass.FUND,
        AssetClass.INDEX,
    }
)

#: Packaged products: their holdings, share class and fee describe them.
FUND_CLASSES: Final = frozenset({AssetClass.ETF, AssetClass.ETC, AssetClass.FUND})

#: How many problems of one kind an error lists before it summarises the rest.
_SHOWN = 8


def _symbol(value: str) -> str:
    text = value.strip()
    if not text or text != value or text != text.upper():
        raise ValueError("must be an upper-case symbol without surrounding spaces, such as EXV1")
    return value


type Symbol = Annotated[str, AfterValidator(_symbol)]
#: How a cell's inputs combine, such as ``10y_minus_2y`` (lite's slope labels).
type Label = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_]*$")]
type Fee = Annotated[Decimal, Field(ge=0, lt=1, allow_inf_nan=False)]


def _listing(problems: list[str], header: str) -> str:
    shown = ", ".join(problems[:_SHOWN])
    more = len(problems) - _SHOWN
    return f"{header}: {shown}" + (f", and {more} more" if more > 0 else "")


# ── the grid ──


class GridMarketConfig(FrozenModel):
    """One market of the grid: the currency everything local is denominated in, its country,
    and the native index (or locally listed fund) that measures its broad cell."""

    currency: CurrencyCode
    country: CountryCode
    native_index: Symbol | None


class UnmappedCellsConfig(FrozenModel):
    """Cells of one market that no instrument measures, and why."""

    market: Identifier
    sectors: list[Identifier] | Literal["remaining"]
    reason: NonEmptyText


class GridConfig(FrozenModel):
    """The equity grid's axes: sectors (``broad`` is the market itself) and markets."""

    sectors: list[Identifier] = Field(min_length=1)
    broad_sector: Identifier
    markets: dict[Identifier, GridMarketConfig] = Field(min_length=1)
    unmapped_cells: list[UnmappedCellsConfig]

    @model_validator(mode="after")
    def _axes(self) -> Self:
        if len(set(self.sectors)) != len(self.sectors):
            raise ValueError("grid.sectors lists a sector twice")
        if self.broad_sector not in self.sectors:
            raise ValueError(f"grid.broad_sector {self.broad_sector} is not in grid.sectors")
        return self


# ── the macro board ──


class MacroCellConfig(FrozenModel):
    """One region x asset-class cell: the macro series and futures roots it reads (an instrument
    claims a cell through its own exposures), or the reason it has none."""

    series: list[NonEmptyText]
    roots: list[Symbol]
    label: Label | None
    unmapped: NonEmptyText | None


class MacroRegionConfig(FrozenModel):
    currency: CurrencyCode | None
    cells: dict[Identifier, MacroCellConfig]


#: The asset classes the board knows how to read (grid/macro_board.py), and what each reads:
#: ``instrument`` an instrument that claims the cell, ``one`` a single series, ``pair`` two series
#: combined by the cell's label, ``levels`` one series or a labelled pair, ``roots`` futures curves.
BOARD_ASSET_CLASSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "equity_index": "instrument",
        "rate_2y": "one",
        "rate_10y": "one",
        "curve": "pair",
        "real_rate": "pair",
        "fx_vs_base": "one",
        "credit": "one",
        "vol_index": "levels",
        "commodities": "roots",
    }
)

#: The labels a cell may carry: the asset class each belongs to and how many series it combines.
#: The board computes each by name, so a label nothing computes is refused here.
BOARD_LABELS: Final[Mapping[str, tuple[str, int]]] = MappingProxyType(
    {
        "cot_positioning": ("equity_index", 1),
        "10y_minus_2y": ("curve", 2),
        "10y_minus_policy": ("curve", 2),
        "policy_minus_cpi_index_yoy": ("real_rate", 2),
        "policy_minus_cpi_rate": ("real_rate", 2),
        "short_over_long": ("vol_index", 2),
    }
)

#: The label prefix that makes a real-rate cell's first series the region's policy rate, and the
#: curve label that makes a curve's second series the policy rate.
POLICY_FIRST_PREFIX: Final = "policy_minus_"
POLICY_SECOND_LABEL: Final = "10y_minus_policy"


def _cell_shape_problems(ref: str, kind: str, cell: MacroCellConfig) -> list[str]:
    """A cell's series, roots and label fit what its asset class reads."""
    count = len(cell.series)
    rule = BOARD_LABELS.get(cell.label) if cell.label is not None else None
    problems: list[str] = []
    if cell.label is not None and (rule is None or rule[0] != ref.partition(".")[2]):
        problems.append(f"{ref} carries label {cell.label}, which the board does not compute there")
    elif rule is not None and count != rule[1]:
        problems.append(f"{ref}: label {cell.label} combines {rule[1]} series, not {count}")
    if cell.roots and kind != "roots":
        problems.append(f"{ref} names futures roots; only a commodities cell reads them")
    if kind == "roots" and cell.series:
        problems.append(f"{ref} names series; a commodities cell reads futures roots")
    if kind == "one" and count > 1:
        problems.append(f"{ref} reads one series, not {count}")
    if kind == "pair" and count and cell.label is None:
        problems.append(f"{ref} combines two series and must say how (a label)")
    if kind == "levels" and count > 1 and cell.label is None:
        problems.append(f"{ref} names {count} series without a label saying how they combine")
    if kind == "instrument" and count and cell.label is None:
        problems.append(f"{ref} names series without a label saying what they are")
    return problems


class MacroBoardConfig(FrozenModel):
    asset_classes: list[Identifier] = Field(min_length=1)
    regions: dict[Identifier, MacroRegionConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _every_cell_declared(self) -> Self:
        if len(set(self.asset_classes)) != len(self.asset_classes):
            raise ValueError("macro_board.asset_classes lists an asset class twice")
        unknown = sorted(set(self.asset_classes) - set(BOARD_ASSET_CLASSES))
        if unknown:
            raise ValueError(
                f"macro_board.asset_classes names {', '.join(unknown)}, which the board does not "
                "know how to read"
            )
        wanted = set(self.asset_classes)
        problems = []
        for name, region in sorted(self.regions.items()):
            missing = sorted(wanted - set(region.cells))
            extra = sorted(set(region.cells) - wanted)
            problems += [f"{name}.{cell} is not declared" for cell in missing]
            problems += [f"{name}.{cell} is not an asset class of the board" for cell in extra]
            for cell_name, cell in sorted(region.cells.items()):
                kind = BOARD_ASSET_CLASSES.get(cell_name)
                if kind is not None:
                    problems += _cell_shape_problems(f"{name}.{cell_name}", kind, cell)
            policy = self.policy_series(name)
            curve = region.cells.get("curve")
            if (
                policy is not None
                and curve is not None
                and curve.label == POLICY_SECOND_LABEL
                and curve.series[1] != policy
            ):
                problems.append(
                    f"{name}: the real rate's policy rate {policy} and the curve's "
                    f"{curve.series[1]} disagree"
                )
        if problems:
            raise ValueError(_listing(problems, "macro_board.regions"))
        return self

    def policy_series(self, region: str) -> str | None:
        """The region's policy rate: the first series of a ``policy_minus_*`` real rate, else the
        second of a ``10y_minus_policy`` curve; ``None`` when neither declares one."""
        cells = self.regions[region].cells
        real = cells.get("real_rate")
        if (
            real is not None
            and real.label is not None
            and real.label.startswith(POLICY_FIRST_PREFIX)
        ):
            return real.series[0]
        curve = cells.get("curve")
        if curve is not None and curve.label == POLICY_SECOND_LABEL:
            return curve.series[1]
        return None


# ── instruments ──


class InstrumentAttributes(FrozenModel):
    """What eligibility, tax and the vehicle selector read of an instrument. Unknown is ``null``,
    never a default: an unknown ``ucits`` is not ``false`` and an unknown lot is not 1."""

    domicile: CountryCode | None
    ucits: bool | None
    kid: KidStatus | None
    share_class: ShareClass | None
    #: A share class that hedges its currency, and the currency it hedges to.
    hedged: bool | None
    hedge_currency: CurrencyCode | None
    #: The ongoing charge, a decimal fraction a year (0.002 is 0.20 %).
    fee: Fee | None
    lot_size: PositiveInt | None
    inception: date | None
    isin: Isin | None
    issuer: NonEmptyText | None
    tracked_index: NonEmptyText | None
    replication: Literal["physical", "sampled", "synthetic", "futures"] | None
    #: The curve a futures-replicating product rolls (its contango rule reads it).
    futures_root: Symbol | None
    bond_issuer: Literal["government", "corporate"] | None
    #: Where these facts come from, with the date they were read.
    source: NonEmptyText

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.hedged is True) != (self.hedge_currency is not None):
            raise ValueError("a hedged share class names hedge_currency, and only it does")
        if (self.replication == "futures") != (self.futures_root is not None):
            raise ValueError("futures replication names its futures_root, and only it does")
        return self


class GridExposure(FrozenModel):
    """The grid cell an instrument represents; ``measures`` makes it the cell's measurement."""

    market: Identifier
    sector: Identifier
    measures: bool


class InstrumentExposures(FrozenModel):
    """What an instrument stands for: a grid cell, macro-board cells (``region.asset_class``),
    core roles, the country a concentration cap counts it against, a commodity bucket."""

    grid: GridExposure | None
    macro_board: list[NonEmptyText]
    core_roles: list[Identifier]
    country: CountryCode | None
    commodity_bucket: Identifier | None


class HoldingsSource(FrozenModel):
    """The issuer's published holdings file: the instrument's look-through, and for the market
    ``candidates_for`` names, that market's stock candidates."""

    url: NonEmptyText
    format: Literal["ishares_csv"]
    candidates_for: Identifier | None


class UniverseInstrument(FrozenModel):
    """One declared instrument. ``conid`` pins it (nothing is searched); without one it is
    resolved through IBKR's contract search on its listing venue and currency. The listing venue
    identifies the contract; ``history_exchange`` is what a history request sends (``null``:
    none, which some venues need) and ``option_exchange`` what a chain request sends (``null``:
    undeclared, the chain's own list decides)."""

    symbol: Symbol
    conid: Conid | None
    listing_exchange: IbkrExchangeCode
    history_exchange: IbkrExchangeCode | None
    option_exchange: IbkrExchangeCode | None
    currency: CurrencyCode
    asset_class: AssetClass
    #: The contract's resolved description, which the leveraged and inverse markers read;
    #: ``null`` until resolved. An instrument without one is never traded.
    description: NonEmptyText | None
    roles: list[InstrumentRole] = Field(min_length=1)
    #: Why the instrument is not a tradable candidate (required exactly when it is not).
    data_only_reason: NonEmptyText | None
    attributes: InstrumentAttributes
    exposures: InstrumentExposures
    holdings: HoldingsSource | None
    coverage_note: NonEmptyText | None
    collect_bars: bool
    bars_off_reason: NonEmptyText | None
    #: Why no option chain is reachable (required on a cell-measuring row without a chain).
    options_unavailable_reason: NonEmptyText | None

    @property
    def tradable(self) -> bool:
        return InstrumentRole.TRADABLE_CANDIDATE in self.roles

    @property
    def collects_chain(self) -> bool:
        return InstrumentRole.OPTION_UNDERLYING in self.roles

    @property
    def measured_cell(self) -> tuple[str, str] | None:
        grid = self.exposures.grid
        return (grid.market, grid.sector) if grid is not None and grid.measures else None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        where = self.symbol
        if self.asset_class not in DECLARABLE_CLASSES:
            raise ValueError(
                f"{where}: asset_class {self.asset_class.value} is not declared in the universe "
                "(an option is a chain row, a future a curve rung, a currency pair a conversion)"
            )
        if len(set(self.roles)) != len(self.roles):
            raise ValueError(f"{where}: a role is listed twice")
        if self.tradable == (self.data_only_reason is not None):
            raise ValueError(
                f"{where}: data_only_reason says why an instrument is not a tradable candidate, "
                "and is set exactly when it is not"
            )
        if self.collect_bars == (self.bars_off_reason is not None):
            raise ValueError(f"{where}: bars_off_reason is set exactly when collect_bars is false")
        if self.collects_chain and self.options_unavailable_reason is not None:
            raise ValueError(
                f"{where}: an option underlying's chain cannot also be unreachable; drop whichever "
                "is untrue"
            )
        if self.option_exchange is not None and not self.collects_chain:
            raise ValueError(
                f"{where}: option_exchange is declared but no chain is collected, so nothing "
                "reads it"
            )
        if (
            self.measured_cell is not None
            and not self.collects_chain
            and self.options_unavailable_reason is None
        ):
            raise ValueError(
                f"{where}: measures a grid cell, collects no chain and gives no "
                "options_unavailable_reason; 'no chain today' and 'no chain ever' are different "
                "claims, so declare which"
            )
        if self.asset_class is AssetClass.INDEX and self.tradable:
            raise ValueError(f"{where}: an index is a level, not something to buy")
        return self


# ── futures roots and commodity proxies ──


class FuturesRootConfig(FrozenModel):
    """A futures curve, collected as data. A futures contract is never a tradable candidate."""

    root: Symbol
    exchange: IbkrExchangeCode
    currency: CurrencyCode
    bucket: Identifier
    seasonal: bool
    description: NonEmptyText
    tracked: bool
    #: How the commodity can be expressed without the contract, or that it cannot.
    expression: NonEmptyText
    cot_series: NonEmptyText | None
    inventory_series: NonEmptyText | None
    history_series: NonEmptyText | None


class CommodityProxyConfig(FrozenModel):
    """An equity standing in for a commodity, and the series it is regressed against."""

    symbol: Symbol
    root: Symbol | None
    reference: NonEmptyText | None
    reference_kind: Literal["macro", "curve", "none"]
    note: NonEmptyText

    @model_validator(mode="after")
    def _reference(self) -> Self:
        if (self.reference_kind == "none") != (self.reference is None):
            raise ValueError(
                f"commodity proxy {self.symbol}: a reference is named exactly when its kind is "
                "macro or curve"
            )
        return self


# ── stock candidates ──


class ScannerRunConfig(FrozenModel):
    """One IBKR market-scanner run whose stocks are candidates of ``market``."""

    market: Identifier
    instrument: NonEmptyText
    location: NonEmptyText
    scan_type: NonEmptyText


class CandidateGapConfig(FrozenModel):
    """Grid cells declared to have no stock candidates, and why."""

    market: Identifier
    sectors: list[Identifier] = Field(min_length=1)
    reason: NonEmptyText


class CandidatesConfig(FrozenModel):
    scanners: list[ScannerRunConfig]
    gaps: list[CandidateGapConfig]


# ── the universe ──


@dataclass(frozen=True, slots=True)
class Cell:
    """One grid cell as declared: measured by ``symbol``, or unmapped for ``reason``."""

    market: str
    sector: str
    symbol: str | None
    reason: str | None


class UniverseConfig(FrozenModel):
    grid: GridConfig
    macro_board: MacroBoardConfig
    #: The commodity axis (lite's ``commodities.buckets``): orthogonal to sectors, since gold is a
    #: monetary bucket and a materials sector, and pooling the two axes correlates nonsense.
    commodity_buckets: list[Identifier] = Field(min_length=1)
    instruments: list[UniverseInstrument] = Field(min_length=1)
    futures_roots: list[FuturesRootConfig]
    commodity_proxies: list[CommodityProxyConfig]
    candidates: CandidatesConfig

    # ── lookups ──

    def instrument(self, symbol: str) -> UniverseInstrument | None:
        """The instrument declared as ``symbol`` (symbols are unique)."""
        wanted = symbol.strip().upper()
        return next((i for i in self.instruments if i.symbol == wanted), None)

    def pinned(self, conid: int) -> UniverseInstrument | None:
        """The instrument pinned to ``conid``."""
        return next((i for i in self.instruments if i.conid == conid), None)

    def cells(self) -> dict[tuple[str, str], Cell]:
        """Every grid cell, measured or unmapped (the validators guarantee each is one of them)."""
        measured = {
            cell: instrument.symbol
            for instrument in self.instruments
            if (cell := instrument.measured_cell) is not None
        }
        out: dict[tuple[str, str], Cell] = {
            cell: Cell(cell[0], cell[1], symbol, None) for cell, symbol in measured.items()
        }
        for block in self.grid.unmapped_cells:
            for sector in self._claimed(block, out):
                out[(block.market, sector)] = Cell(block.market, sector, None, block.reason)
        return out

    def _claimed(self, block: UnmappedCellsConfig, taken: dict[tuple[str, str], Cell]) -> list[str]:
        if block.sectors == SECTORS_REMAINING:
            return [s for s in self.grid.sectors if (block.market, s) not in taken]
        return list(block.sectors)

    def board_claims(self) -> dict[str, str]:
        """``region.asset_class`` -> the instrument that claims the macro-board cell."""
        return {ref: i.symbol for i in self.instruments for ref in i.exposures.macro_board}

    def candidate_sources(self, market: str) -> list[UniverseInstrument]:
        """The instruments whose holdings file supplies ``market``'s stock candidates."""
        return [
            i
            for i in self.instruments
            if i.holdings is not None and i.holdings.candidates_for == market
        ]

    # ── invariants ──

    @model_validator(mode="after")
    def _instruments_are_distinct(self) -> Self:
        symbols = [i.symbol for i in self.instruments]
        twice = sorted({s for s in symbols if symbols.count(s) > 1})
        if twice:
            raise ValueError(_listing(twice, "instruments declared twice"))
        conids = [i.conid for i in self.instruments if i.conid is not None]
        pinned_twice = sorted({str(c) for c in conids if conids.count(c) > 1})
        if pinned_twice:
            raise ValueError(_listing(pinned_twice, "conids pinned by two instruments"))
        return self

    @model_validator(mode="after")
    def _grid_matrix_is_complete(self) -> Self:
        grid = self.grid
        problems: list[str] = []
        measured: dict[tuple[str, str], str] = {}
        for instrument in self.instruments:
            exposure = instrument.exposures.grid
            if exposure is None:
                continue
            cell = (exposure.market, exposure.sector)
            if exposure.market not in grid.markets or exposure.sector not in grid.sectors:
                problems.append(f"{instrument.symbol} names {cell[0]}/{cell[1]}, not on the axes")
                continue
            if not exposure.measures:
                continue
            if cell in measured:
                problems.append(
                    f"{instrument.symbol} and {measured[cell]} both measure {cell[0]}/{cell[1]}"
                )
            measured[cell] = instrument.symbol
        problems += self._native_index_problems(measured)
        declared: dict[tuple[str, str], str] = {}
        for index, block in enumerate(grid.unmapped_cells):
            if block.market not in grid.markets:
                problems.append(
                    f"unmapped_cells[{index}] names market {block.market}, not on the axis"
                )
                continue
            claimed = self._claimed(
                block,
                {cell: Cell(cell[0], cell[1], None, None) for cell in [*measured, *declared]},
            )
            if not claimed:
                problems.append(
                    f"unmapped_cells[{index}] ({block.market}) claims no cell: every sector is "
                    "measured or already declared, so delete the block"
                )
            for sector in claimed:
                cell = (block.market, sector)
                if sector not in grid.sectors:
                    problems.append(
                        f"unmapped_cells[{index}] names sector {sector}, not on the axis"
                    )
                elif cell in measured:
                    problems.append(
                        f"{block.market}/{sector} is declared unmapped and measured by "
                        f"{measured[cell]}"
                    )
                elif cell in declared:
                    problems.append(f"{block.market}/{sector} is declared unmapped twice")
                else:
                    declared[cell] = block.reason
        undeclared = [
            f"{market}/{sector}"
            for market in sorted(grid.markets)
            for sector in grid.sectors
            if (market, sector) not in measured and (market, sector) not in declared
        ]
        if undeclared:
            problems.append(
                _listing(
                    undeclared,
                    "cells neither measured by an instrument nor declared under unmapped_cells",
                )
            )
        if problems:
            raise ValueError("grid: " + "; ".join(problems))
        return self

    def _native_index_problems(self, measured: dict[tuple[str, str], str]) -> list[str]:
        """Each market's native index measures its broad cell in the market's own currency (the
        anti-wrapper rule: a foreign-currency listing reports that currency's return under this
        market's name)."""
        problems: list[str] = []
        broad = self.grid.broad_sector
        for name, market in sorted(self.grid.markets.items()):
            if market.native_index is None:
                continue
            instrument = self.instrument(market.native_index)
            if instrument is None or measured.get((name, broad)) != instrument.symbol:
                problems.append(
                    f"markets.{name}.native_index {market.native_index} does not measure "
                    f"{name}/{broad}"
                )
            elif instrument.currency != market.currency:
                problems.append(
                    f"markets.{name} is denominated in {market.currency} and measured on "
                    f"{instrument.symbol} in {instrument.currency}; declare the native index null "
                    "rather than measure another currency's return"
                )
        return problems

    @model_validator(mode="after")
    def _macro_board_is_complete(self) -> Self:
        board = self.macro_board
        roots = {root.root for root in self.futures_roots}
        claims: dict[str, str] = {}
        problems: list[str] = []
        for instrument in self.instruments:
            for ref in instrument.exposures.macro_board:
                region_name, _, asset_class = ref.partition(".")
                known = board.regions.get(region_name)
                cells = known.cells if known is not None else {}
                if asset_class not in cells:
                    problems.append(f"{instrument.symbol} claims {ref}, not a cell of the board")
                elif ref in claims:
                    problems.append(f"{instrument.symbol} and {claims[ref]} both claim {ref}")
                else:
                    claims[ref] = instrument.symbol
        for region_name, region in sorted(board.regions.items()):
            for name, cell in sorted(region.cells.items()):
                ref = f"{region_name}.{name}"
                mapped = bool(cell.series or cell.roots) or ref in claims
                if mapped == (cell.unmapped is not None):
                    problems.append(
                        f"{ref} must either name its inputs or say why it has none, not both"
                    )
                problems += [
                    f"{ref} names root {root}, not a declared futures root"
                    for root in cell.roots
                    if root not in roots
                ]
        if problems:
            raise ValueError(_listing(problems, "macro_board"))
        return self

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        symbols = {i.symbol for i in self.instruments}
        roots = [root.root for root in self.futures_roots]
        problems: list[str] = []
        if len(set(roots)) != len(roots):
            problems.append("a futures root is declared twice")
        buckets = set(self.commodity_buckets)
        if len(buckets) != len(self.commodity_buckets):
            problems.append("commodity_buckets lists a bucket twice")
        problems += [
            f"futures root {root.root} names bucket {root.bucket}, not in commodity_buckets"
            for root in self.futures_roots
            if root.bucket not in buckets
        ]
        for instrument in self.instruments:
            bucket = instrument.exposures.commodity_bucket
            if bucket is not None and bucket not in buckets:
                problems.append(
                    f"{instrument.symbol} names bucket {bucket}, not in commodity_buckets"
                )
            root = instrument.attributes.futures_root
            if root is not None and root not in roots:
                problems.append(f"{instrument.symbol} rolls {root}, not a declared futures root")
            holdings = instrument.holdings
            if holdings is not None and holdings.candidates_for not in (None, *self.grid.markets):
                problems.append(
                    f"{instrument.symbol} supplies candidates for {holdings.candidates_for}, "
                    "not a grid market"
                )
        proxied = [proxy.symbol for proxy in self.commodity_proxies]
        if len(set(proxied)) != len(proxied):
            problems.append("a commodity proxy is declared twice")
        for proxy in self.commodity_proxies:
            if proxy.symbol not in symbols:
                problems.append(f"commodity proxy {proxy.symbol} is not a declared instrument")
            if proxy.root is not None and proxy.root not in roots:
                problems.append(f"commodity proxy {proxy.symbol} names root {proxy.root}")
            if proxy.reference_kind == "curve" and proxy.reference not in roots:
                problems.append(
                    f"commodity proxy {proxy.symbol} references curve {proxy.reference}, not a "
                    "declared futures root"
                )
        problems += self._candidate_problems()
        if problems:
            raise ValueError(_listing(problems, "universe references"))
        return self

    def _candidate_problems(self) -> list[str]:
        """Every market has a candidate source, a scanner, or gaps declared for all its cells."""
        markets = self.grid.markets
        candidates = self.candidates
        problems = [
            f"candidates.scanners names market {run.market}, not a grid market"
            for run in candidates.scanners
            if run.market not in markets
        ]
        gaps: dict[str, set[str]] = {}
        for gap in candidates.gaps:
            if gap.market not in markets:
                problems.append(f"candidates.gaps names market {gap.market}, not a grid market")
                continue
            unknown = sorted(set(gap.sectors) - set(self.grid.sectors))
            problems += [f"candidates.gaps names sector {s}, not on the axis" for s in unknown]
            gaps.setdefault(gap.market, set()).update(gap.sectors)
        scanned = {run.market for run in candidates.scanners}
        for market in sorted(markets):
            sourced = bool(self.candidate_sources(market)) or market in scanned
            if not sourced and gaps.get(market, set()) != set(self.grid.sectors):
                problems.append(
                    f"market {market} has no candidate source and not every cell declares a gap"
                )
        return problems
