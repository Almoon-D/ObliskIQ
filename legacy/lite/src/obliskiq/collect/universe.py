"""The tracked universe: what the collectors walk.

Two sources feed one table.

**Declared rows** come from ``universe.yaml``. They are the native index
contracts, the option-chain names and the commodity proxies, and they are never
reconciled away — a declared instrument that stops appearing in the account is
still something the grid wants a reading on.

**Portfolio rows** are folded in from the live account so a position bought
outside a session still gets collected. They are marked ``origin='portfolio_sync'``
and stamped ``last_seen_at`` on every sync; one that has not been seen for
``collectors.universe.portfolio_stale_days`` stops being collected, because a
closed position that keeps drawing option-chain calls burns API budget forever
for data nobody will read.

Rows are never deleted. ``bars``, ``chain_snapshots`` and ``curve_snapshots`` all
reference ``universe`` with ``ON DELETE CASCADE``, so deleting a row to tidy up
would silently destroy the irreplaceable history collected against it.
Reconciliation clears the collection flags instead.

Two separations that are easy to get wrong
------------------------------------------

**Macro series are not instruments.** €STR, SOFR and every other FRED / ECB /
ECB / CFTC / EIA series is keyed by series id in the ``macro`` table and has no
conid. Handing one to ``/iserver/secdef/search`` would resolve it to whatever
ticker happens to look similar, and that row would then sit in the universe
generating chain requests forever. The loader rejects the collision outright.

**PRIIPs restricts the CASH instrument, not the option.** An EU retail account
cannot buy SPY for want of a KID, but it may trade listed options on it — which is
precisely why the option sleeve exists. ``priips_restricted`` therefore has no
bearing on collection; only ``collect_chain`` decides that.

**Leveraged and inverse ETPs are rejected here, at ingestion.** Not later, at
recommendation time. A row that never enters the universe cannot be recommended
by a future code path that forgot to check, and the rejection is recorded in
``universe_rejections`` so an exclusion is auditable rather than invisible.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import Config
from ..errors import ConfigError, StoreError
from ..instruments import LeveragedInverseFilter, Rejection
from ..logging_setup import get_logger
from ..store import Store
from ..util.numeric import safe_float, safe_int
from ..util.timeutil import age_seconds, utc_stamp
from .nav import NavCollector
from .retry import TargetRetryPolicy, pace, with_target_retry
from .runlog import CollectorRun
from .source import UNDERLYING_SEC_TYPES, MarketData

log = get_logger("collect.universe")

COLLECTOR_NAME = "universe"

ORIGIN_DECLARED = "declared"
ORIGIN_PORTFOLIO = "portfolio_sync"

#: Legal values of commodity_proxies[].reference_kind in universe.yaml.
PROXY_KIND_MACRO = "macro"
PROXY_KIND_CURVE = "curve"
PROXY_KIND_NONE = "none"
PROXY_REFERENCE_KINDS = frozenset({PROXY_KIND_MACRO, PROXY_KIND_CURVE, PROXY_KIND_NONE})

#: True of every futures root, so it is the fallback when a root says no more.
ROOT_UNTRADEABLE_REASON = "futures contract prohibited under instruments.prohibited"

#: The market-level row of the sector axis, measured on the native index. Named
#: here because the loader, the grid and the census all need to say "the market
#: itself" and three string literals would drift apart.
BROAD_SECTOR = "broad"

#: How many undeclared cells an error lists before it summarises the rest.
_UNDECLARED_SHOWN = 8

#: Seconds in a day, for turning a stale-days threshold into an age comparison.
SECONDS_PER_DAY = 86400.0

#: ``/iserver/secdef/search`` has no ETF security type; an ETF searches as a
#: stock. Everything else passes through unchanged.
_SEC_TYPE_FOR_SEARCH = {"ETF": "STK"}


@dataclass
class Instrument:
    """One declared row of ``universe.yaml``."""

    symbol: str
    asset_class: str
    currency: str
    conid: int | None = None
    exchange: str | None = None
    market: str | None = None
    sector: str | None = None
    is_native_index: bool = False
    listing_domicile: str | None = None
    priips_restricted: bool = False
    tradable: bool = True
    macro_signal: bool = False
    untradeable_reason: str | None = None
    collect_chain: bool = False
    collect_bars: bool = True
    listed_on: str | None = None
    #: Cap dimension. `market` groups measurements; `country` constrains
    #: concentration, and an ADR must not count against the wrong one.
    country: str | None = None
    #: Commodity bucket, orthogonal to `sector`.
    bucket: str | None = None
    #: This row measures its market x sector grid cell. At most one per cell.
    grid_measures: bool = False
    #: ETF disclosure. None means not sourced — never an invented figure.
    top2_concentration: float | None = None
    hedged: bool | None = None
    coverage_note: str | None = None
    #: Continuous dividend yield, decimal. Declared rather than collected: there
    #: is no free global dividend feed, and a yield inferred from a trailing
    #: payment is a number nobody could audit afterwards. None means nobody has
    #: declared one — never "this instrument pays nothing", which is a claim
    #: with evidence behind it.
    dividend_yield: float | None = None
    #: When that yield was sourced. A yield is a fact about a date, and an
    #: undated one cannot be told apart from a stale one.
    dividend_yield_as_of: str | None = None
    #: The exchange a HISTORY request should send, which is not always the
    #: listing venue. Probed live 2026-08-29: a TSEJ-listed share returns "No
    #: market data permissions" with exchange=TSEJ and full daily history with
    #: the exchange omitted. None means send none.
    history_exchange: str | None = None
    #: The exchange an OPTION request should send, which is not the listing venue
    #: either: measured 2026-09-10, the Madrid Santander line lists its options on
    #: EUREX and MEFFRV and the Paris TotalEnergies line on EUREX and MONEP. None
    #: means undeclared — NOT that SMART is right; see ``ChainCollector._exchange_for``.
    option_exchange: str | None = None
    #: Why no option chain is reachable for this instrument, in words. Required
    #: on a grid-measuring row that does not collect one, because "no chain
    #: today" and "no chain ever" are different claims and only the first is a
    #: collection failure.
    options_unavailable_reason: str | None = None
    #: Declared leverage. Both are rejected at ingestion and never stored.
    leveraged: bool = False
    inverse: bool = False

    @property
    def search_sec_type(self) -> str:
        return _SEC_TYPE_FOR_SEARCH.get(self.asset_class.upper(), self.asset_class.upper())


@dataclass
class FuturesRoot:
    """One declared row of the ``futures_roots`` section."""

    root: str
    exchange: str | None = None
    currency: str | None = None
    bucket: str | None = None
    description: str | None = None
    tracked: bool = True
    seasonal: bool = False
    #: A futures contract is prohibited, so this is false on every root. The
    #: curve remains a data call — see instruments.futures_data_use.
    tradable: bool = False
    #: The curve is a regime input whether or not the commodity can be held.
    macro_signal: bool = True
    untradeable_reason: str | None = None
    #: Declared macro series carrying this root's positioning, its inventory and
    #: its LONG price history. Named rather than inferred from the root code: a
    #: mapping the code guessed would be a mapping nobody could audit.
    cot_series: str | None = None
    inventory_series: str | None = None
    history_series: str | None = None


@dataclass
class CommodityProxy:
    """An equity standing in for a commodity, and the series that tests it."""

    symbol: str
    root: str | None
    reference: str | None
    #: macro (a declared macro.series id) | curve (front-month curve history) |
    #: none (no free reference price exists; unreliable by declaration).
    reference_kind: str
    note: str | None = None


@dataclass(frozen=True)
class UnmappedCell:
    """A market x sector cell no instrument measures, and the declared reason.

    The grid used to mark such a cell ``inapplicable`` by INFERRING it from the
    absence of a ``grid_measures`` row. That is the one move this design refuses
    everywhere else, and it is the same shape as the ``options_unavailable_reason``
    gap closed earlier: "nobody looked" and "somebody looked and there is nothing"
    are different claims, and an absent row does not tell them apart. The reason
    written in ``universe.yaml`` reaches the cell verbatim.
    """

    market: str
    sector: str
    reason: str


@dataclass
class UniverseFile:
    instruments: list[Instrument] = field(default_factory=list)
    futures_roots: list[FuturesRoot] = field(default_factory=list)
    commodity_proxies: list[CommodityProxy] = field(default_factory=list)
    #: The grid's sector axis, declared rather than derived: a cell with no
    #: mapped instrument must be an explicit n/a, and a derived axis would make
    #: that cell simply never exist.
    sectors: list[str] = field(default_factory=list)
    #: The grid's market axis, with each market's currency and macro series ids.
    markets: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: (market, sector) -> why nothing measures it. Every pair on the axes is
    #: either measured by an instrument or declared here; the loader refuses a
    #: universe where one is neither.
    unmapped_cells: dict[tuple[str, str], str] = field(default_factory=dict)
    #: Declared rows refused entry, with the reason. Never silently dropped.
    rejected: list[Rejection] = field(default_factory=list)


def load_universe_file(path: str | Path, config: Config) -> UniverseFile:
    """Parse ``universe.yaml`` and validate it against config.

    Raises rather than skipping a bad row. A universe silently missing an
    instrument produces a grid with a hole in it and no record of why.
    """
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ConfigError(
            f"universe file not found: {resolved}. The collectors have nothing to "
            "walk without it; see collectors.universe.path in config.yaml."
        )
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"universe file {resolved} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"universe file {resolved} must be a mapping at the top level")

    macro_ids = _declared_macro_series(config)
    sectors = _load_sectors(resolved, raw)
    leverage_filter = LeveragedInverseFilter(config)
    instruments: list[Instrument] = []
    rejected: list[Rejection] = []
    seen: set[str] = set()
    measured_cells: dict[tuple[str, str], str] = {}

    for index, row in enumerate(raw.get("instruments") or []):
        if not isinstance(row, dict):
            raise ConfigError(f"{resolved}: instruments[{index}] is not a mapping")
        instrument = _instrument_from_row(resolved, index, row, sectors)
        rejection = leverage_filter.check_declared(
            instrument.symbol, leveraged=instrument.leveraged, inverse=instrument.inverse
        )
        if rejection is not None:
            # Rejected at ingestion, not at recommendation time. Recorded, so the
            # exclusion is auditable rather than a row that quietly never appeared.
            log.warning(
                "instrument rejected at ingestion",
                extra={"symbol": instrument.symbol, "reason": rejection.reason},
            )
            rejected.append(rejection)
            continue
        if instrument.grid_measures:
            cell = (instrument.market or "", instrument.sector or "")
            if not cell[0] or not cell[1]:
                raise ConfigError(
                    f"{resolved}: instrument {instrument.symbol!r} sets grid_measures but "
                    "has no market and sector. A measurement instrument must say which "
                    "cell it measures."
                )
            if cell in measured_cells:
                raise ConfigError(
                    f"{resolved}: {instrument.symbol!r} and {measured_cells[cell]!r} both "
                    f"claim to measure {cell[0]}/{cell[1]}. Exactly one instrument may "
                    "measure a cell, or the grid reports whichever the loader saw last."
                )
            measured_cells[cell] = instrument.symbol
        upper = instrument.symbol.upper()
        if upper in macro_ids:
            raise ConfigError(
                f"{resolved}: instrument {instrument.symbol!r} collides with the macro "
                "series of the same name declared under macro.series in config.yaml. "
                "Macro series are keyed by series id and have no contract id; a row "
                "here would be handed to the IBKR conid resolver and matched against "
                "whatever ticker looks closest."
            )
        if upper in seen:
            raise ConfigError(f"{resolved}: instrument {instrument.symbol!r} is declared twice")
        seen.add(upper)
        instruments.append(instrument)

    roots: list[FuturesRoot] = []
    root_names: set[str] = set()
    for index, row in enumerate(raw.get("futures_roots") or []):
        if not isinstance(row, dict):
            raise ConfigError(f"{resolved}: futures_roots[{index}] is not a mapping")
        if not row.get("root"):
            raise ConfigError(f"{resolved}: futures_roots[{index}] has no root")
        root = FuturesRoot(
            root=str(row["root"]).upper(),
            exchange=_optional_str(row.get("exchange")),
            currency=_optional_str(row.get("currency")),
            bucket=_optional_str(row.get("bucket")),
            description=_optional_str(row.get("description")),
            tracked=bool(row.get("tracked", True)),
            seasonal=bool(row.get("seasonal", False)),
            tradable=bool(row.get("tradable", False)),
            macro_signal=bool(row.get("macro_signal", True)),
            # True of every root, so it is the default rather than a required
            # field. Declaring it says something MORE — whether the commodity has
            # an accessible wrapper or is a macro signal and nothing else — and
            # that distinction decides whether a view is actionable at all.
            untradeable_reason=_optional_str(row.get("untradeable_reason"))
            or ROOT_UNTRADEABLE_REASON,
            cot_series=_optional_str(row.get("cot_series")),
            inventory_series=_optional_str(row.get("inventory_series")),
            history_series=_optional_str(row.get("history_series")),
        )
        for label, series_id in (
            ("cot_series", root.cot_series),
            ("inventory_series", root.inventory_series),
            ("history_series", root.history_series),
        ):
            if series_id and series_id.upper() not in macro_ids:
                raise ConfigError(
                    f"{resolved}: futures root {root.root!r} names {label} "
                    f"{series_id!r}, which is not declared under macro.series in "
                    "config.yaml. A series nothing collects is a cell that can never "
                    "resolve."
                )
        if root.tradable:
            raise ConfigError(
                f"{resolved}: futures root {root.root!r} declares tradable: true. A "
                "futures contract is a prohibited instrument under "
                "instruments.prohibited; only its curve is a data call."
            )
        if root.root in root_names:
            raise ConfigError(f"{resolved}: futures root {root.root!r} is declared twice")
        root_names.add(root.root)
        roots.append(root)

    _validate_buckets(resolved, roots, config)
    markets = _load_markets(resolved, raw, instruments)
    proxies = _load_proxies(resolved, raw, seen, root_names)
    unmapped = _load_unmapped_cells(resolved, raw, sectors, markets, measured_cells)
    return UniverseFile(
        instruments=instruments,
        futures_roots=roots,
        commodity_proxies=proxies,
        sectors=sectors,
        markets=markets,
        unmapped_cells=unmapped,
        rejected=rejected,
    )


def _load_sectors(source: Path, raw: dict[str, Any]) -> list[str]:
    """The declared sector axis, or empty.

    Empty is legal here and fatal in :mod:`obliskiq.compute.build_grid`. The
    collectors do not need an axis to walk an instrument list, and requiring one
    would mean every collector fixture had to declare a grid it never uses. The
    grid refuses to run without it and names the remedy.
    """
    sectors = [str(s) for s in (raw.get("sectors") or [])]
    if len(set(sectors)) != len(sectors):
        raise ConfigError(f"{source}: the sectors list contains duplicates")
    return sectors


def _load_markets(
    source: Path, raw: dict[str, Any], instruments: list[Instrument]
) -> dict[str, dict[str, Any]]:
    markets = raw.get("markets") or {}
    if not markets:
        # Legal here, fatal in build_grid. See _load_sectors.
        return {}
    if not isinstance(markets, dict):
        raise ConfigError(f"{source}: markets must be a mapping of market name to spec")

    measuring = {i.symbol.upper(): i for i in instruments if i.grid_measures}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in markets.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"{source}: markets.{name} is not a mapping")
        if not spec.get("currency"):
            raise ConfigError(f"{source}: markets.{name} is missing 'currency'")
        if "native_index" not in spec:
            raise ConfigError(
                f"{source}: markets.{name} is missing 'native_index'. Declare it null if "
                "nothing measures the market's broad cell — an explicit gap is auditable "
                "and an absent key is not."
            )
        # A null native_index is a DECLARED gap: the market keeps its place on the
        # axis for its macro series, and its broad cell reports no_instrument_mapped
        # rather than being quietly filled with the nearest listed thing.
        if spec["native_index"] is None:
            out[str(name)] = dict(spec)
            continue
        index_symbol = str(spec["native_index"]).upper()
        if index_symbol not in measuring:
            raise ConfigError(
                f"{source}: markets.{name} names native_index {index_symbol!r}, which is "
                "not a declared instrument with grid_measures set."
            )
        instrument = measuring[index_symbol]
        if instrument.market != name:
            raise ConfigError(
                f"{source}: markets.{name} names {index_symbol!r}, whose own market is "
                f"{instrument.market!r}."
            )
        if instrument.sector != BROAD_SECTOR:
            raise ConfigError(
                f"{source}: markets.{name} names {index_symbol!r}, whose sector is "
                f"{instrument.sector!r}. The native index measures the market's BROAD cell."
            )
        # THE ANTI-WRAPPER RULE, made checkable. It used to be spelled
        # `is_native_index`, which a fund row could simply assert; what the rule is
        # actually about is the currency trade a foreign listing embeds in every
        # reading. A market measured on an instrument denominated in some other
        # currency reports that currency's returns under this market's label, and
        # indicator 11 would then decompose them against an fx_series that does not
        # apply to them. So the denomination has to match, and a market with no
        # instrument in its own currency declares a gap instead.
        if (instrument.currency or "").upper() != str(spec["currency"]).upper():
            raise ConfigError(
                f"{source}: markets.{name} is denominated in {spec['currency']!r} but is "
                f"measured on {index_symbol!r}, which is denominated in "
                f"{instrument.currency!r}. A market measured in a foreign currency reports "
                "that currency's return under this market's name, and its fx_series no "
                "longer describes the leg being decomposed. Declare native_index null and "
                "leave the cell an explicit gap rather than measuring the wrong thing."
            )
        out[str(name)] = dict(spec)
    return out


#: ``unmapped_cells[].sectors`` may name this instead of a list: every sector of
#: the market not measured and not claimed by an earlier block.
SECTORS_REMAINING = "remaining"


def _load_unmapped_cells(
    source: Path,
    raw: dict[str, Any],
    sectors: list[str],
    markets: dict[str, dict[str, Any]],
    measured: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    """Every declared gap, expanded to one reason per cell.

    Refuses three things, and each one is a status that would otherwise be
    inferred from an absence: a pair neither measured nor declared, a pair
    claimed twice, and a block that claims nothing. The last matters as much as
    the others — a ``remaining`` block left behind after its market was mapped is
    prose asserting a gap that no longer exists, which is the same failure as a
    config key nothing reads.

    Skipped entirely without both axes. A collector fixture declares an
    instrument list and no grid, and requiring it to enumerate a grid it never
    uses would make every such fixture carry this file's whole taxonomy.
    """
    blocks = raw.get("unmapped_cells") or []
    if not sectors or not markets:
        if blocks:
            raise ConfigError(
                f"{source}: unmapped_cells is declared without both a `sectors:` and a "
                "`markets:` axis. A gap is a claim about a cell, and without the axes "
                "there are no cells for it to be about."
            )
        return {}
    if not isinstance(blocks, list):
        raise ConfigError(f"{source}: unmapped_cells must be a list of blocks")

    out: dict[tuple[str, str], str] = {}
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ConfigError(f"{source}: unmapped_cells[{index}] is not a mapping")
        market = _optional_str(block.get("market"))
        reason = _optional_str(block.get("reason"))
        if not market:
            raise ConfigError(f"{source}: unmapped_cells[{index}] names no market")
        if market not in markets:
            raise ConfigError(
                f"{source}: unmapped_cells[{index}] names market {market!r}, which is not "
                f"on the declared market axis {sorted(markets)}."
            )
        if not reason:
            raise ConfigError(
                f"{source}: unmapped_cells[{index}] ({market}) gives no reason. A cell "
                "declared inapplicable with no reason is the inference this block exists "
                "to replace."
            )

        declared = block.get("sectors")
        if declared == SECTORS_REMAINING:
            claimed = [
                sector
                for sector in sectors
                if (market, sector) not in measured and (market, sector) not in out
            ]
            if not claimed:
                raise ConfigError(
                    f"{source}: unmapped_cells[{index}] claims the remaining sectors of "
                    f"{market!r} and there are none left — every one is measured or "
                    "already declared above. Delete the block: prose asserting a gap that "
                    "no longer exists is worse than no prose."
                )
        elif isinstance(declared, list):
            claimed = [str(sector) for sector in declared]
            if not claimed:
                raise ConfigError(
                    f"{source}: unmapped_cells[{index}] ({market}) declares an empty "
                    "sector list and therefore claims nothing."
                )
        else:
            raise ConfigError(
                f"{source}: unmapped_cells[{index}] ({market}) must set `sectors` to a "
                f"list of sector names or to the literal {SECTORS_REMAINING!r}."
            )

        for sector in claimed:
            if sector not in sectors:
                raise ConfigError(
                    f"{source}: unmapped_cells[{index}] names sector {sector!r} for "
                    f"{market!r}, which is not on the declared sector axis {sectors!r}."
                )
            cell = (market, sector)
            if cell in measured:
                raise ConfigError(
                    f"{source}: {market}/{sector} is declared unmapped and is also "
                    f"measured by {measured[cell]!r}. A cell cannot be both; drop "
                    "whichever is untrue."
                )
            if cell in out:
                raise ConfigError(
                    f"{source}: {market}/{sector} is declared unmapped twice. Two reasons "
                    "for one cell means the grid would report whichever the loader saw "
                    "last."
                )
            out[cell] = reason

    undeclared = [
        f"{market}/{sector}"
        for market in sorted(markets)
        for sector in sectors
        if (market, sector) not in measured and (market, sector) not in out
    ]
    if undeclared:
        shown = ", ".join(undeclared[:_UNDECLARED_SHOWN])
        more = len(undeclared) - _UNDECLARED_SHOWN
        raise ConfigError(
            f"{source}: {len(undeclared)} market x sector cell(s) are neither measured by "
            f"an instrument nor declared under unmapped_cells: {shown}"
            + (f", and {more} more" if more > 0 else "")
            + ". Map an instrument with grid_measures: true, or declare the gap with its "
            "reason. A status inferred from an absence is not a declared status, and the "
            "grid reports these cells as the ambition gap on the strength of that reason."
        )
    return out


def _load_proxies(
    source: Path, raw: dict[str, Any], symbols: set[str], roots: set[str]
) -> list[CommodityProxy]:
    proxies: list[CommodityProxy] = []
    seen: set[str] = set()
    for index, row in enumerate(raw.get("commodity_proxies") or []):
        if not isinstance(row, dict):
            raise ConfigError(f"{source}: commodity_proxies[{index}] is not a mapping")
        symbol = (_optional_str(row.get("symbol")) or "").upper()
        if not symbol:
            raise ConfigError(f"{source}: commodity_proxies[{index}] has no symbol")
        if symbol not in symbols:
            raise ConfigError(
                f"{source}: commodity_proxies[{index}] names {symbol!r}, which is not a "
                "declared instrument. A proxy with no bar history cannot be regressed."
            )
        if symbol in seen:
            raise ConfigError(f"{source}: commodity proxy {symbol!r} is declared twice")
        seen.add(symbol)
        kind = (_optional_str(row.get("reference_kind")) or "").lower()
        if kind not in PROXY_REFERENCE_KINDS:
            raise ConfigError(
                f"{source}: commodity proxy {symbol!r} declares reference_kind {kind!r}; "
                f"it must be one of {sorted(PROXY_REFERENCE_KINDS)}."
            )
        reference = _optional_str(row.get("reference"))
        if kind != PROXY_KIND_NONE and not reference:
            raise ConfigError(
                f"{source}: commodity proxy {symbol!r} has reference_kind {kind!r} but no "
                "reference series. Use reference_kind: none where no free price series "
                "exists — the proxy is then unreliable by declaration and says so."
            )
        root = (_optional_str(row.get("root")) or "").upper() or None
        if root and root not in roots:
            raise ConfigError(
                f"{source}: commodity proxy {symbol!r} names root {root!r}, which is not "
                "a declared futures root."
            )
        if kind == PROXY_KIND_CURVE and reference and reference.upper() not in roots:
            raise ConfigError(
                f"{source}: commodity proxy {symbol!r} references curve {reference!r}, "
                "which is not a declared futures root."
            )
        proxies.append(
            CommodityProxy(
                symbol=symbol,
                root=root,
                reference=reference,
                reference_kind=kind,
                note=_optional_str(row.get("note")),
            )
        )
    return proxies


def _instrument_from_row(
    source: Path, index: int, row: dict[str, Any], sectors: list[str]
) -> Instrument:
    symbol = _optional_str(row.get("symbol"))
    if not symbol:
        raise ConfigError(f"{source}: instruments[{index}] has no symbol")
    asset_class = (_optional_str(row.get("asset_class")) or "").upper()
    if asset_class not in UNDERLYING_SEC_TYPES:
        raise ConfigError(
            f"{source}: instrument {symbol!r} has asset_class {asset_class!r}; it must be "
            f"one of {sorted(UNDERLYING_SEC_TYPES)}. An option or a note is a holding, "
            "not an underlying root."
        )
    currency = _optional_str(row.get("currency"))
    if not currency:
        raise ConfigError(f"{source}: instrument {symbol!r} has no currency")

    sector = _optional_str(row.get("sector"))
    if sector and sector not in sectors:
        raise ConfigError(
            f"{source}: instrument {symbol!r} declares sector {sector!r}, which is not on "
            f"the declared sector axis {sectors!r}. The axis is fixed so that cells are "
            "comparable across markets."
        )

    tradable = bool(row.get("tradable", True))
    reason = _optional_str(row.get("untradeable_reason"))
    if not tradable and not reason:
        raise ConfigError(
            f"{source}: instrument {symbol!r} is not tradable and gives no "
            "untradeable_reason. A silent exclusion is a coverage gap nobody can "
            "audit later."
        )

    exchange = _optional_str(row.get("exchange"))
    # An ABSENT key means "use the listing venue"; a key present and null means
    # "send no exchange at all". The two cannot be told apart by .get(), and the
    # difference is what makes TSEJ history reachable.
    history_exchange = (
        _optional_str(row.get("history_exchange")) if "history_exchange" in row else exchange
    )

    grid_measures = bool(row.get("grid_measures", False))
    collect_chain = bool(row.get("collect_chain", False))
    options_reason = _optional_str(row.get("options_unavailable_reason"))
    if grid_measures and not collect_chain and not options_reason:
        raise ConfigError(
            f"{source}: instrument {symbol!r} measures a grid cell, collects no option "
            "chain, and gives no options_unavailable_reason. Grid indicators 7, 8 and 9 "
            "need a chain, and whether one is merely uncollected or structurally "
            "unreachable decides whether those cells count against collection health. "
            "Declare it either way; the absence of a flag is not an answer."
        )
    if collect_chain and options_reason:
        raise ConfigError(
            f"{source}: instrument {symbol!r} collects an option chain and also declares "
            "options_unavailable_reason. It cannot be both reachable and unreachable — "
            "drop whichever is untrue."
        )

    option_exchange = _optional_str(row.get("option_exchange"))
    if option_exchange and not collect_chain:
        raise ConfigError(
            f"{source}: instrument {symbol!r} declares option_exchange {option_exchange!r} "
            "and does not collect an option chain. Nothing reads the venue, so the "
            "declaration looks made and is not."
        )

    return Instrument(
        symbol=symbol.upper(),
        asset_class=asset_class,
        currency=currency.upper(),
        conid=safe_int(row.get("conid"), None),
        exchange=exchange,
        history_exchange=history_exchange,
        option_exchange=option_exchange,
        options_unavailable_reason=options_reason,
        market=_optional_str(row.get("market")),
        sector=sector,
        is_native_index=bool(row.get("is_native_index", False)),
        listing_domicile=_optional_str(row.get("listing_domicile")),
        priips_restricted=bool(row.get("priips_restricted", False)),
        tradable=tradable,
        macro_signal=bool(row.get("macro_signal", False)),
        untradeable_reason=reason,
        collect_chain=collect_chain,
        collect_bars=bool(row.get("collect_bars", True)),
        listed_on=_optional_str(row.get("listed_on")),
        country=_optional_str(row.get("country")),
        bucket=_optional_str(row.get("bucket")),
        grid_measures=grid_measures,
        top2_concentration=_optional_float(row.get("top2_concentration")),
        dividend_yield=_optional_float(row.get("dividend_yield")),
        dividend_yield_as_of=_optional_str(row.get("dividend_yield_as_of")),
        hedged=None if row.get("hedged") is None else bool(row.get("hedged")),
        coverage_note=_optional_str(row.get("coverage_note")),
        leveraged=bool(row.get("leveraged", False)),
        inverse=bool(row.get("inverse", False)),
    )


def _declared_macro_series(config: Config) -> set[str]:
    """Every macro series id, plus the risk-free-rate series, upper-cased."""
    ids = {
        str(entry["id"]).upper()
        for entry in config.get("macro.series", [])
        if isinstance(entry, dict) and entry.get("id")
    }
    ids.update(
        str(series).upper()
        for series in config.get("macro.risk_free_rate.series_by_currency", {}).values()
    )
    return ids


def _validate_buckets(source: Path, roots: list[FuturesRoot], config: Config) -> None:
    allowed = {str(bucket) for bucket in config.require_list("commodities.buckets")}
    unknown = sorted({r.bucket for r in roots if r.bucket and r.bucket not in allowed})
    if unknown:
        raise ConfigError(
            f"{source}: futures roots name buckets {unknown!r} that are not in "
            f"commodities.buckets ({sorted(allowed)})."
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    """None stays None: an unsourced ETF concentration is absent, never zero."""
    if value is None:
        return None
    return safe_float(value, None)


# ── seeding ──


def assert_seeded(store: Store, config: Config) -> None:
    """Refuse to run a collector against an empty universe.

    A run that reports success over zero targets is indistinguishable from a
    healthy one in the log, and that is exactly the failure this package exists to
    prevent.
    """
    if not config.require_bool("collectors.universe.require_seeded"):
        return
    row = store.execute("SELECT COUNT(*) FROM universe").fetchone()
    if row and int(row[0]) > 0:
        return
    raise StoreError(
        "the universe table is empty, so there is nothing to collect. Seed it first:\n"
        "    obliskiq-collect universe\n"
        "Set collectors.universe.require_seeded to false only if an empty run is "
        "genuinely what you want."
    )


class UniverseSync:
    """Seeds and reconciles ``universe`` and ``futures_roots``."""

    def __init__(self, store: Store, config: Config, market_data: MarketData) -> None:
        self._store = store
        self._config = config
        self._market_data = market_data
        self._policy = TargetRetryPolicy.from_config(config)
        cfg = config.section("collectors.universe")
        self._path = cfg.require_str("path")
        self._portfolio_union = cfg.require_bool("portfolio_union")
        self._portfolio_sec_types = {
            str(sec_type).upper() for sec_type in cfg.require_list("portfolio_sec_types")
        }
        self._stale_days = cfg.require_int("portfolio_stale_days")
        self._record_nav = cfg.require_bool("record_nav")
        self._leverage_filter = LeveragedInverseFilter(config)

    async def run(self, *, run: CollectorRun, dry_run: bool = False) -> int:
        """Seed from the file, union the portfolio, reconcile. Returns rows written."""
        universe = load_universe_file(self._path, self._config)
        written = 0
        resolved: dict[str, int] = {}

        self._record_rejections(universe.rejected, run, dry_run=dry_run)
        written += await self._sync_instruments(
            universe.instruments, run, resolved, dry_run=dry_run
        )
        written += self._sync_futures_roots(universe.futures_roots, run, dry_run=dry_run)
        written += self._sync_commodity_proxies(universe.commodity_proxies, dry_run=dry_run)
        if self._portfolio_union:
            written += await self._sync_portfolio(run, dry_run=dry_run)
        else:
            log.info("portfolio union disabled by collectors.universe.portfolio_union")
        if self._record_nav:
            await self._sync_nav(run, dry_run=dry_run)

        run.notes["superseded_rows_retired"] = self._retire_superseded(resolved, dry_run=dry_run)
        retired = self._reconcile(dry_run=dry_run)
        run.notes["portfolio_rows_retired"] = retired
        return written

    # ── declared instruments ──

    async def _sync_instruments(
        self,
        instruments: list[Instrument],
        run: CollectorRun,
        resolved: dict[str, int],
        *,
        dry_run: bool,
    ) -> int:
        """Resolve and upsert every declared row.

        ``resolved`` collects symbol -> conid for the rows that actually resolved,
        and is what :meth:`_retire_superseded` reads. A symbol that failed to
        resolve is absent from it rather than present with a stale value, so a
        transient outage can never look like a conid correction.
        """
        written = 0
        for instrument in instruments:
            try:
                conid = await self._conid_for(instrument)
            except Exception as exc:
                run.failed(instrument.symbol, f"conid resolution failed: {exc!r}")
                continue
            if conid is None:
                run.failed(
                    instrument.symbol,
                    "no conid resolved. Add an explicit conid: to universe.yaml if the "
                    "symbol is ambiguous or dual-listed.",
                )
                continue
            if not dry_run:
                self._upsert_instrument(instrument, conid)
            resolved[instrument.symbol.upper()] = conid
            written += 1
            run.succeeded(instrument.symbol, 1, conid=conid, origin=ORIGIN_DECLARED)
        return written

    async def _conid_for(self, instrument: Instrument) -> int | None:
        """Explicit conid wins outright; nothing is searched for it.

        This is the escape hatch for dual-listed and ambiguous symbols, where
        ``/iserver/secdef/search`` ranks by its own relevance and can resolve a
        short ticker to a foreign listing or an unsponsored ADR.
        """
        if instrument.conid is not None:
            log.debug(
                "using the explicit conid from universe.yaml; no search issued",
                extra={"symbol": instrument.symbol, "conid": instrument.conid},
            )
            return instrument.conid

        async def resolve() -> int | None:
            # The declared listing venue goes WITH the search. It was on the row
            # the whole time and never entered the decision, which is precisely
            # how ASML's AEB row came to hold a NASDAQ conid: every listing of a
            # company answers the same bare ticker, so the exact-match rule can
            # only ever return the API's first choice.
            return await self._market_data.resolve_conid(
                instrument.symbol,
                sec_type=instrument.search_sec_type,
                exchange=instrument.exchange,
            )

        conid = await with_target_retry(self._policy, instrument.symbol, resolve)
        await pace(self._policy)
        return conid

    def _upsert_instrument(self, instrument: Instrument, conid: int) -> None:
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO universe (
                    contract_id, symbol, exchange, currency, asset_class, market, sector,
                    is_native_index, listing_domicile, priips_restricted, tradable,
                    macro_signal, untradeable_reason, source, origin, last_seen_at,
                    collect_chain, collect_bars, listed_on, country, bucket,
                    grid_measures, top2_concentration, hedged, coverage_note,
                    dividend_yield, dividend_yield_as_of, history_exchange,
                    option_exchange, options_unavailable_reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (contract_id) DO UPDATE SET
                    symbol = excluded.symbol,
                    exchange = excluded.exchange,
                    currency = excluded.currency,
                    asset_class = excluded.asset_class,
                    market = excluded.market,
                    sector = excluded.sector,
                    is_native_index = excluded.is_native_index,
                    listing_domicile = excluded.listing_domicile,
                    priips_restricted = excluded.priips_restricted,
                    tradable = excluded.tradable,
                    macro_signal = excluded.macro_signal,
                    untradeable_reason = excluded.untradeable_reason,
                    -- A row declared in universe.yaml is a declared row from then on,
                    -- even if the portfolio sync happened to create it first.
                    origin = ?,
                    last_seen_at = excluded.last_seen_at,
                    collect_chain = excluded.collect_chain,
                    collect_bars = excluded.collect_bars,
                    listed_on = excluded.listed_on,
                    country = excluded.country,
                    bucket = excluded.bucket,
                    grid_measures = excluded.grid_measures,
                    top2_concentration = excluded.top2_concentration,
                    hedged = excluded.hedged,
                    coverage_note = excluded.coverage_note,
                    dividend_yield = excluded.dividend_yield,
                    dividend_yield_as_of = excluded.dividend_yield_as_of,
                    history_exchange = excluded.history_exchange,
                    option_exchange = excluded.option_exchange,
                    options_unavailable_reason = excluded.options_unavailable_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    conid,
                    instrument.symbol,
                    instrument.exchange,
                    instrument.currency,
                    instrument.asset_class,
                    instrument.market,
                    instrument.sector,
                    int(instrument.is_native_index),
                    instrument.listing_domicile,
                    int(instrument.priips_restricted),
                    int(instrument.tradable),
                    int(instrument.macro_signal),
                    instrument.untradeable_reason,
                    self._market_data.source,
                    ORIGIN_DECLARED,
                    stamp,
                    int(instrument.collect_chain),
                    int(instrument.collect_bars),
                    instrument.listed_on,
                    instrument.country,
                    instrument.bucket,
                    int(instrument.grid_measures),
                    instrument.top2_concentration,
                    None if instrument.hedged is None else int(instrument.hedged),
                    instrument.coverage_note,
                    instrument.dividend_yield,
                    instrument.dividend_yield_as_of,
                    instrument.history_exchange,
                    instrument.option_exchange,
                    instrument.options_unavailable_reason,
                    stamp,
                    ORIGIN_DECLARED,
                ),
            )

    # ── futures roots ──

    def _sync_futures_roots(
        self, roots: list[FuturesRoot], run: CollectorRun, *, dry_run: bool
    ) -> int:
        stamp = utc_stamp()
        written = 0
        for root in roots:
            if not dry_run:
                with self._store.transaction() as connection:
                    connection.execute(
                        "INSERT INTO futures_roots (root, exchange, currency, bucket, "
                        "description, tracked, seasonal, tradable, macro_signal, "
                        "untradeable_reason, cot_series, inventory_series, "
                        "history_series, last_seen_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (root) DO UPDATE SET "
                        "exchange = excluded.exchange, currency = excluded.currency, "
                        "bucket = excluded.bucket, description = excluded.description, "
                        "tracked = excluded.tracked, seasonal = excluded.seasonal, "
                        "tradable = excluded.tradable, "
                        "macro_signal = excluded.macro_signal, "
                        "untradeable_reason = excluded.untradeable_reason, "
                        "cot_series = excluded.cot_series, "
                        "inventory_series = excluded.inventory_series, "
                        "history_series = excluded.history_series, "
                        "last_seen_at = excluded.last_seen_at",
                        (
                            root.root,
                            root.exchange,
                            root.currency,
                            root.bucket,
                            root.description,
                            int(root.tracked),
                            int(root.seasonal),
                            int(root.tradable),
                            int(root.macro_signal),
                            root.untradeable_reason,
                            root.cot_series,
                            root.inventory_series,
                            root.history_series,
                            stamp,
                        ),
                    )
            written += 1
            run.succeeded(f"futures:{root.root}", 1, bucket=root.bucket)
        return written

    # ── rejections and the proxy map ──

    def _record_rejections(
        self, rejections: list[Rejection], run: CollectorRun, *, dry_run: bool
    ) -> None:
        run.notes["rejected_at_ingestion"] = [r.symbol for r in rejections]
        if dry_run or not rejections:
            return
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            connection.executemany(
                "INSERT INTO universe_rejections (rejected_at, symbol, exchange, origin, "
                "reason, detail) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (rejected_at, symbol, origin) DO UPDATE SET "
                "reason = excluded.reason, detail = excluded.detail",
                [(stamp, r.symbol, None, ORIGIN_DECLARED, r.reason, r.detail) for r in rejections],
            )

    def _sync_commodity_proxies(self, proxies: list[CommodityProxy], *, dry_run: bool) -> int:
        if dry_run or not proxies:
            return len(proxies)
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            connection.executemany(
                "INSERT INTO commodity_proxy_map (symbol, root, reference, reference_kind, "
                "note, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (symbol) DO UPDATE SET root = excluded.root, "
                "reference = excluded.reference, reference_kind = excluded.reference_kind, "
                "note = excluded.note, updated_at = excluded.updated_at",
                [(p.symbol, p.root, p.reference, p.reference_kind, p.note, stamp) for p in proxies],
            )
        return len(proxies)

    # ── portfolio union ──

    async def _sync_portfolio(self, run: CollectorRun, *, dry_run: bool) -> int:
        try:
            positions = await with_target_retry(
                self._policy, "portfolio", self._market_data.portfolio_positions
            )
        except Exception as exc:
            run.failed("portfolio", f"could not read account positions: {exc!r}")
            return 0

        stamp = utc_stamp()
        written = 0
        skipped: list[str] = []
        rejected: list[Rejection] = []
        for row in positions:
            sec_type = str(row.get("assetClass") or row.get("secType") or "").upper()
            conid = safe_int(row.get("conid"), None)
            if sec_type not in self._portfolio_sec_types or conid is None:
                # An option, a warrant, a bond or a structured note is a holding.
                # It is not an underlying root and must never be treated as one:
                # a chain request against an option conid is meaningless, and a bar
                # series against a note is unobtainable.
                skipped.append(f"{row.get('ticker') or row.get('contractDesc')}:{sec_type}")
                continue
            symbol = str(row.get("ticker") or row.get("contractDesc") or conid).upper()
            rejection = self._leverage_filter.check_description(
                symbol, _optional_str(row.get("contractDesc")) or _optional_str(row.get("name"))
            )
            if rejection is not None:
                # A held leveraged ETP is a fact about the account, not a licence
                # to fold it into the universe: the row would then be a candidate
                # for every code path downstream. Recorded and refused.
                rejected.append(rejection)
                continue
            if not dry_run:
                self._upsert_portfolio_row(row, conid, sec_type, stamp)
            written += 1

        if rejected and not dry_run:
            rejection_stamp = utc_stamp()
            with self._store.transaction() as connection:
                connection.executemany(
                    "INSERT INTO universe_rejections (rejected_at, symbol, exchange, "
                    "origin, reason, detail) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (rejected_at, symbol, origin) DO UPDATE SET "
                    "reason = excluded.reason, detail = excluded.detail",
                    [
                        (rejection_stamp, r.symbol, None, ORIGIN_PORTFOLIO, r.reason, r.detail)
                        for r in rejected
                    ],
                )

        run.succeeded(
            "portfolio",
            written,
            positions_seen=len(positions),
            skipped_not_an_underlying=len(skipped),
            rejected_leveraged_or_inverse=[r.symbol for r in rejected],
        )
        if skipped:
            log.info(
                "portfolio rows skipped: not underlying roots",
                extra={"skipped": skipped[: len(skipped)]},
            )
        return written

    def _upsert_portfolio_row(
        self, row: dict[str, Any], conid: int, sec_type: str, stamp: str
    ) -> None:
        symbol = str(row.get("ticker") or row.get("contractDesc") or conid).upper()
        currency = str(row.get("currency") or "").upper() or None
        exchange = _optional_str(row.get("listingExchange"))
        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO universe (
                    contract_id, symbol, exchange, currency, asset_class, source,
                    origin, last_seen_at, collect_chain, collect_bars, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (contract_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at,
                    exchange = COALESCE(universe.exchange, excluded.exchange),
                    -- A held position is worth bars again even if a previous
                    -- reconciliation retired it. Declared rows keep their own
                    -- flags; this only ever re-enables.
                    collect_bars = 1
                """,
                (
                    conid,
                    symbol,
                    exchange,
                    currency or self._config.require_str("account.base_currency"),
                    sec_type,
                    self._market_data.source,
                    ORIGIN_PORTFOLIO,
                    stamp,
                    0,
                    1,
                    stamp,
                ),
            )

    # ── reconciliation ──

    def _retire_superseded(self, resolved: dict[str, int], *, dry_run: bool) -> int:
        """Stop collecting a declared row whose symbol now resolves elsewhere.

        Correcting a conid does not remove the old row: ``bars``,
        ``chain_snapshots`` and ``curve_snapshots`` cascade off ``universe``, so a
        delete would take real history with it. Left alone, though, the old row
        keeps its collection flags and the collectors walk the instrument twice
        under one symbol — which is what re-pointing ASML from the NASDAQ
        NY-registered line to the Amsterdam one would otherwise have caused.

        The rule is narrow on purpose: a row is retired only when ANOTHER row
        carrying the same symbol resolved to a DIFFERENT conid in this run. The
        loader already refuses a duplicate symbol among declared instruments, so
        the match is unambiguous, and a symbol that failed to resolve is not in
        ``resolved`` at all — so an upstream outage retires nothing.
        """
        if not resolved:
            return 0
        placeholders = ",".join("?" for _ in resolved)
        rows = self._store.execute(
            "SELECT contract_id, symbol FROM universe "
            f"WHERE origin = ? AND symbol IN ({placeholders}) "
            "AND (collect_chain = 1 OR collect_bars = 1)",
            (ORIGIN_DECLARED, *resolved),
        ).fetchall()
        superseded = [
            (row["contract_id"], row["symbol"])
            for row in rows
            if resolved[str(row["symbol"]).upper()] != int(row["contract_id"])
        ]
        if not superseded or dry_run:
            return len(superseded)

        with self._store.transaction() as connection:
            connection.executemany(
                "UPDATE universe SET collect_chain = 0, collect_bars = 0, updated_at = ? "
                "WHERE contract_id = ?",
                [(utc_stamp(), contract_id) for contract_id, _ in superseded],
            )
        log.warning(
            "retired declared rows whose symbol now resolves to a different conid",
            extra={
                "retired": [
                    {"symbol": symbol, "superseded_conid": contract_id}
                    for contract_id, symbol in superseded
                ],
            },
        )
        return len(superseded)

    # ── the account NAV ──

    async def _sync_nav(self, run: CollectorRun, *, dry_run: bool) -> None:
        """Record one NAV observation from the account ledger.

        This collector already resolves the account id for the portfolio union,
        so the reading costs one further call on a session it holds — and it runs
        an hour before the weekly census, which cannot compute an affordability
        ceiling without one. The reading itself belongs to :class:`NavCollector`,
        which the daily sequence also runs: two cadences, one writer.
        """
        await NavCollector(self._store, self._config, self._market_data).run(run, dry_run=dry_run)

    def _reconcile(self, *, dry_run: bool) -> int:
        """Stop collecting portfolio rows that have not been seen for a while.

        Rows are never deleted: ``bars``, ``chain_snapshots`` and
        ``curve_snapshots`` cascade off ``universe``, so a delete would take the
        irreplaceable history with it. Clearing the flags stops the API spend and
        keeps the record.
        """
        cutoff = self._stale_days * SECONDS_PER_DAY
        candidates = self._store.execute(
            "SELECT contract_id, symbol, last_seen_at FROM universe "
            "WHERE origin = ? AND (collect_chain = 1 OR collect_bars = 1)",
            (ORIGIN_PORTFOLIO,),
        ).fetchall()

        stale = [
            (row["contract_id"], row["symbol"])
            for row in candidates
            if (age := age_seconds(row["last_seen_at"])) is None or age > cutoff
        ]
        if not stale or dry_run:
            return len(stale)

        with self._store.transaction() as connection:
            connection.executemany(
                "UPDATE universe SET collect_chain = 0, collect_bars = 0, updated_at = ? "
                "WHERE contract_id = ?",
                [(utc_stamp(), contract_id) for contract_id, _ in stale],
            )
        log.info(
            "retired portfolio rows no longer held",
            extra={
                "stale_days": self._stale_days,
                "retired": [symbol for _, symbol in stale],
            },
        )
        return len(stale)
