"""The shape of the configuration.

Every model is frozen and refuses unknown keys, and no field carries a default value: the
values live in ``config/defaults/*.yaml`` and ``config/owner.yaml``, so a key missing from
both fails validation instead of taking a number nobody chose. A section joins this schema
with its first consumer.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, PositiveInt, model_validator

from ..domain.base import CurrencyCode
from ..domain.calendar import Mic
from ..domain.enums import AssetClass
from .base import FrozenModel, Identifier
from .schema_calendars import (
    CalendarsConfig,
    IbkrExchangeCode,
    IbkrExchangeConfig,
    LunchBreakConfig,
    SeasonalCloseConfig,
    SyntheticCalendarConfig,
    VenueConfig,
)
from .schema_collectors import CollectorsConfig
from .schema_costs import CostsConfig
from .schema_execution import DisciplineConfig, ExecutionConfig
from .schema_ibkr import HttpConfig, IbkrConfig
from .schema_instruments import (
    InstrumentsConfig,
    PositionsConfig,
    TenorConfig,
)
from .schema_llm import LlmConfig
from .schema_market_data import MarketDataConfig
from .schema_numerics import LiquidityConfig, NumericsConfig, SmileConfig
from .schema_research import ResearchConfig
from .schema_runtime import (
    CapitalConfig,
    CoreConfig,
    ReportingConfig,
    RuntimeConfig,
    TradingConfig,
)
from .schema_sources import SourcesConfig
from .schema_universe import UniverseConfig
from .schema_vehicles import BrokerCapabilitiesConfig, VehiclesConfig


def _file_name_in_data_dir(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.name
        or value.endswith("/")
        or "\\" in value
    ):
        raise ValueError(
            "must be a relative path inside the data directory, such as settings-audit.jsonl"
        )
    return value


type DataFileName = Annotated[str, AfterValidator(_file_name_in_data_dir)]


class AccountConfig(FrozenModel):
    """The account as the owner describes it."""

    base_currency: CurrencyCode


class PathsConfig(FrozenModel):
    """Where runtime files live, relative to ``OBLISK_DATA_DIR``."""

    settings_audit_log: DataFileName
    database: DataFileName
    research_database: DataFileName
    manual_incidents: DataFileName
    ibkr_gateway: DataFileName
    heartbeat_file: DataFileName
    alert_state: DataFileName
    service_lock: DataFileName


class StoreConfig(FrozenModel):
    """SQLite settings. Write-ahead logging and foreign keys are fixed in code, not offered."""

    busy_timeout_ms: PositiveInt
    synchronous: Literal["NORMAL", "FULL", "EXTRA"]


class LoggingConfig(FrozenModel):
    """Log level and format. ``OBLISK_LOG_LEVEL``/``OBLISK_LOG_FORMAT`` override them."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    format: Literal["json", "text"]


class ObliskConfig(FrozenModel):
    """The effective configuration: expert defaults, then the owner's overrides."""

    account: AccountConfig
    jurisdiction: Identifier
    paths: PathsConfig
    logging: LoggingConfig
    store: StoreConfig
    numerics: NumericsConfig
    smile: SmileConfig
    liquidity: LiquidityConfig
    costs: CostsConfig
    calendars: CalendarsConfig
    venues: dict[Mic, VenueConfig]
    synthetic_calendars: dict[Mic, SyntheticCalendarConfig]
    lunch_breaks: dict[Mic, LunchBreakConfig]
    seasonal_closes: dict[Mic, SeasonalCloseConfig]
    ibkr_exchanges: dict[IbkrExchangeCode, IbkrExchangeConfig]
    http: HttpConfig
    ibkr: IbkrConfig
    discipline: DisciplineConfig
    execution: ExecutionConfig
    instruments: InstrumentsConfig
    tenor: TenorConfig
    positions: PositionsConfig
    research: ResearchConfig
    runtime: RuntimeConfig
    capital: CapitalConfig
    core: CoreConfig
    trading: TradingConfig
    reporting: ReportingConfig
    market_data: MarketDataConfig
    collectors: CollectorsConfig
    universe: UniverseConfig
    vehicles: VehiclesConfig
    broker_capabilities: BrokerCapabilitiesConfig
    sources: SourcesConfig
    llm: LlmConfig

    @model_validator(mode="after")
    def _calendar_references_resolve(self) -> Self:
        """Every venue named by a side section or an IBKR code must be declared in ``venues``."""
        known = set(self.venues)
        problems: list[str] = []
        sections: dict[str, set[str]] = {
            "synthetic_calendars": set(self.synthetic_calendars),
            "lunch_breaks": set(self.lunch_breaks),
            "seasonal_closes": set(self.seasonal_closes),
            "calendars.venue_order_offsets": set(self.calendars.venue_order_offsets),
        }
        for section, mics in sections.items():
            problems += [
                f"{section}.{mic}: not a venue declared in venues" for mic in sorted(mics - known)
            ]
        problems += [
            f"ibkr_exchanges.{code}.venue: {entry.venue} is not a venue declared in venues"
            for code, entry in sorted(self.ibkr_exchanges.items())
            if entry.venue not in known
        ]
        problems += [
            f"ibkr.trading_schedule.skip_venues: {mic} is not a venue declared in venues"
            for mic in sorted(set(self.ibkr.trading_schedule.skip_venues) - known)
        ]
        problems += [
            f"seasonal_closes.{mic}: a synthetic calendar has no library close to replace"
            for mic in sorted(set(self.seasonal_closes) & set(self.synthetic_calendars))
        ]
        anchors = [
            (f"runtime.cycles.{cycle.name}", cycle.venue) for cycle in self.runtime.cycles
        ] + [
            ("runtime.digest", self.runtime.digest.venue),
            ("collectors.schedule.fallback", self.collectors.schedule.fallback_venue),
        ]
        problems += [
            f"{where}.venue: {mic} is not a venue declared in venues"
            for where, mic in anchors
            if mic not in known
        ]
        problems += [
            f"market_data.feeds.{feed.id}: {mic} is not a venue declared in venues"
            for feed in self.market_data.feeds
            for mic in feed.venues
            if mic not in known
        ]
        problems += [
            f"collectors.candidates.exchanges: {code} is not in ibkr_exchanges"
            for code in sorted(set(self.collectors.candidates.exchanges.values()))
            if code not in self.ibkr_exchanges
        ]
        problems += [
            f"research.data.yahoo.{field}.{code}: not in ibkr_exchanges"
            for field, codes in (
                ("suffixes", self.research.data.yahoo.suffixes),
                ("digits", self.research.data.yahoo.digits),
            )
            for code in sorted(codes)
            if code not in self.ibkr_exchanges
        ]
        problems += [
            f"collectors.candidates.sectors: {sector} is not a sector of the grid"
            for sector in sorted(set(self.collectors.candidates.sectors.values()))
            if sector not in self.universe.grid.sectors
        ]
        problems += self._universe_problems()
        problems += self._role_problems()
        problems += self._capability_problems()
        problems += self._paper_order_problems()
        problems += self._currency_problems()
        problems += self._skeleton_problems()
        problems += self._source_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _universe_problems(self) -> list[str]:
        """Every exchange code the universe sends maps to a venue, and every instrument lists in
        one of its venue's currencies."""
        exchanges = self.ibkr_exchanges
        problems: list[str] = []
        for instrument in self.universe.instruments:
            for field, code in (
                ("listing_exchange", instrument.listing_exchange),
                ("history_exchange", instrument.history_exchange),
                ("option_exchange", instrument.option_exchange),
            ):
                if code is not None and code not in exchanges:
                    problems.append(
                        f"universe {instrument.symbol}.{field}: {code} is not in ibkr_exchanges"
                    )
            entry = exchanges.get(instrument.listing_exchange)
            if entry is None or entry.venue not in self.venues:
                continue
            if instrument.currency not in self.venues[entry.venue].currencies:
                problems.append(
                    f"universe {instrument.symbol}: {instrument.currency} is not one of "
                    f"venues.{entry.venue}.currencies"
                )
        problems += [
            f"universe futures root {root.root}.exchange: {root.exchange} is not in ibkr_exchanges"
            for root in self.universe.futures_roots
            if root.exchange not in exchanges
        ]
        return problems

    def _role_problems(self) -> list[str]:
        """Every core role an instrument fills is declared, and every role's exposure series is
        a dataset the research store loads."""
        roles = self.core.roles
        problems = [
            f"universe {instrument.symbol}.exposures.core_roles: {role} is not in core.roles"
            for instrument in self.universe.instruments
            for role in instrument.exposures.core_roles
            if role not in roles
        ]
        datasets = {dataset.id for dataset in self.research.datasets}
        fx = self.research.dataset(self.vehicles.correlation.fx_dataset)
        if fx is None or fx.vendor != "ecb":
            problems.append(
                f"vehicles.correlation.fx_dataset: {self.vehicles.correlation.fx_dataset} is not "
                "an ecb dataset of research.datasets"
            )
        problems += [
            f"core.roles.{name}.exposure_series: {role.exposure_dataset} is not a dataset of "
            "research.datasets"
            for name, role in sorted(roles.items())
            if role.exposure_dataset not in datasets
        ]
        return problems

    def _capability_problems(self) -> list[str]:
        """Each capability check names a configured venue, and its probe is a universe instrument
        of its asset class there (for options, an option underlying whose chain is asked there)."""
        problems: list[str] = []
        for check in self.broker_capabilities.checks:
            where = f"broker_capabilities.checks {check.asset_class.value}@{check.venue}"
            if check.venue not in self.venues:
                problems.append(f"{where}: {check.venue} is not a venue declared in venues")
            if check.probe is None:
                continue
            declared = self.universe.instrument(check.probe)
            if declared is None:
                problems.append(f"{where}: probe {check.probe} is not a universe instrument")
                continue
            if check.asset_class is AssetClass.OPTION:
                code = declared.option_exchange or declared.listing_exchange
                fits = declared.collects_chain
            else:
                code = declared.listing_exchange
                fits = declared.asset_class is check.asset_class
            entry = self.ibkr_exchanges.get(code)
            if not fits or entry is None or entry.venue != check.venue:
                what = (
                    "an option chain"
                    if check.asset_class is AssetClass.OPTION
                    else f"an instrument of class {check.asset_class.value}"
                )
                problems.append(f"{where}: probe {check.probe} is not {what} on {check.venue}")
        return problems

    def _currency_problems(self) -> list[str]:
        base = self.account.base_currency
        return [
            f"execution.currency.pairs: {pair.symbol} does not name the base currency {base}; "
            "conversions are to and from the base currency"
            for pair in self.execution.currency.pairs
            if base not in (pair.base, pair.quote)
        ]

    @model_validator(mode="after")
    def _relogin_alerted_within_a_heartbeat(self) -> Self:
        """A failed re-login must reach the owner within one heartbeat (DECISIONS D68): the
        keepalive's slowest path to its alert, failed tickles then every re-login attempt and
        pause, fits inside ``runtime.heartbeat_seconds``."""
        session = self.ibkr.session
        slowest = (
            session.tickle_interval_seconds * session.tickle_failure_grace
            + session.reauthenticate_attempts * session.reauthenticate_pause_seconds
        )
        if slowest > self.runtime.heartbeat_seconds:
            raise ValueError(
                f"runtime.heartbeat_seconds ({self.runtime.heartbeat_seconds}) is shorter than "
                f"the keepalive's slowest path to a failed re-login's alert ({slowest:.0f} s: "
                "ibkr.session tickle_interval_seconds x tickle_failure_grace plus "
                "reauthenticate_attempts x reauthenticate_pause_seconds)"
            )
        return self

    def _tradable(self, conid: int) -> str | None:
        """Why ``conid`` cannot be traded as configured: it must be a universe instrument pinned to
        it, a tradable candidate, with its resolved description. ``None`` when it can."""
        declared = self.universe.pinned(conid)
        if declared is None:
            return f"{conid} is not pinned by a universe instrument"
        if not declared.tradable:
            return f"{conid} ({declared.symbol}) is not a tradable candidate of the universe"
        if declared.description is None:
            return f"{conid} ({declared.symbol}) has no resolved description in the universe"
        return None

    def _skeleton_problems(self) -> list[str]:
        problems: list[str] = []
        for target in self.core.skeleton_targets:
            why = self._tradable(target.conid)
            if why is not None:
                problems.append(f"core.skeleton_targets.{target.role}: {why}")
                continue
            declared = self.universe.pinned(target.conid)
            if declared is not None and target.role not in declared.exposures.core_roles:
                problems.append(
                    f"core.skeleton_targets.{target.role}: {declared.symbol} does not declare the "
                    "role in its exposures.core_roles"
                )
        return problems

    def _paper_order_problems(self) -> list[str]:
        preflight = self.ibkr.preflight
        checked = [
            ("ibkr.preflight.paper_order.conids", conid) for conid in preflight.paper_order.conids
        ]
        checked.append(("ibkr.preflight.execution_test.conid", preflight.execution_test.conid))
        return [
            f"{where}: {why}"
            for where, conid in checked
            if (why := self._tradable(conid)) is not None
        ]

    def _source_problems(self) -> list[str]:
        """The sources' venues exist; the halt feed only reaches venues that accept a single-stock
        feed; an issuer feed names a universe instrument; every series the universe names is
        declared (a series the board or a commodity reads has a source and a release rule)."""
        sources = self.sources
        known = set(self.venues)
        problems = [
            f"sources.providers.{name}.schedule: {moment.venue} is not a venue declared in venues"
            for name, provider in sorted(sources.providers.items())
            for moment in provider.schedule
            if moment.venue not in known
        ]
        halts = sources.halts.nasdaq_trader
        for code, mic in sorted(halts.markets.items()):
            venue = self.venues.get(mic)
            if venue is None:
                problems.append(f"sources.halts.nasdaq_trader.markets.{code}: {mic} is not a venue")
            elif venue.incident_source.type != "structured_feed" or (
                venue.incident_source.scope != "single_stock"
            ):
                problems.append(
                    f"sources.halts.nasdaq_trader.markets.{code}: venues.{mic}.incident_source "
                    "does not accept a single-stock feed"
                )
        if halts.time_venue not in known:
            problems.append(
                f"sources.halts.nasdaq_trader.time_venue: {halts.time_venue} is not a venue"
            )
        symbols = {instrument.symbol for instrument in self.universe.instruments}
        problems += [
            f"sources.news.feeds.{name}.symbol: {feed.symbol} is not a universe instrument"
            for name, feed in sorted(sources.news.feeds.items())
            if feed.symbol is not None and feed.symbol not in symbols
        ]
        declared = {series.id for series in sources.macro.series} | set(
            sources.positioning.contracts
        )
        named: list[tuple[str, str]] = [
            (f"universe.macro_board.{region}.{cell}", series)
            for region, entry in self.universe.macro_board.regions.items()
            for cell, config in entry.cells.items()
            for series in config.series
        ]
        for root in self.universe.futures_roots:
            for field, series in (
                ("cot_series", root.cot_series),
                ("inventory_series", root.inventory_series),
                ("history_series", root.history_series),
            ):
                if series is not None:
                    named.append((f"universe futures root {root.root}.{field}", series))
        named += [
            (f"universe commodity proxy {proxy.symbol}.reference", proxy.reference)
            for proxy in self.universe.commodity_proxies
            if proxy.reference_kind == "macro" and proxy.reference is not None
        ]
        problems += [
            f"{where}: {series} is not declared in sources (macro.series or positioning.contracts)"
            for where, series in named
            if series not in declared
        ]
        return problems
