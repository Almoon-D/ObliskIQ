"""The data sources besides IBKR (``config/sources.yaml``, section ``sources``).

A source is declared with what consumes it (DECISIONS D66): a registered hypothesis, a Hunter
component, a view or a safety rule. A ``built`` source has a fetcher, a schedule and a rule for
when its rows became public; a ``declared`` one has neither, only the condition that would build
it. The macro series, the CFTC contracts, the curated events and feeds, the halt feed and the
catalyst calendar are declared here too, each naming its consumers.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from ..domain.base import NonEmptyText
from ..domain.calendar import Mic, TimeZoneName
from .base import FrozenModel, Identifier
from .schema_runtime import Anchor

#: How the date a row became public is known (MASTER_PLAN §5.18).
type ReleaseDatePolicy = Literal[
    "from_api", "reference_plus_lag", "same_as_reference", "as_collected", "unknown"
]
#: A native frequency; nothing is ever resampled between them.
type Frequency = Literal["daily", "weekly", "monthly", "quarterly", "annual"]
#: Which FRED vintage supplies a value; the release date always comes from the initial release.
type ValueVintage = Literal["initial", "latest"]
type SourceArea = Literal["macro", "positioning", "insiders", "events", "news", "calendar", "halts"]
type SourceStatus = Literal["built", "declared"]
type FeedKind = Literal["central_bank", "regulator", "issuer"]

#: The consumer kinds a source may name, and the form of each reference.
CONSUMER_KINDS = ("hypothesis", "component", "view", "safety")
_CONSUMER_RE = re.compile(r"(hypothesis|component|view|safety):([A-Za-z][A-Za-z0-9_]*)")
_HYPOTHESIS_RE = re.compile(r"H[0-9]+")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_SERIES_ID_RE = re.compile(r"[A-Z][A-Z0-9_]*")


def _consumer(value: str) -> str:
    match = _CONSUMER_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            "must be hypothesis:<id>, component:<name>, view:<name> or safety:<name>, such as "
            "view:macro_board"
        )
    if match.group(1) == "hypothesis" and not _HYPOTHESIS_RE.fullmatch(match.group(2)):
        raise ValueError("a hypothesis is named by its registry id, such as hypothesis:H6")
    return value


def _env_name(value: str) -> str:
    if not _ENV_NAME_RE.fullmatch(value):
        raise ValueError("must be an environment variable name, such as FRED_API_KEY")
    return value


def _series_id(value: str) -> str:
    if not _SERIES_ID_RE.fullmatch(value):
        raise ValueError("must be upper-case letters, digits and underscores, such as DGS10")
    return value


type ConsumerRef = Annotated[str, AfterValidator(_consumer)]
type EnvName = Annotated[str, AfterValidator(_env_name)]
type SeriesId = Annotated[str, AfterValidator(_series_id)]
type Url = Annotated[str, Field(pattern=r"^https://\S+$")]


def consumer_parts(reference: str) -> tuple[str, str]:
    """``view:macro_board`` → ``("view", "macro_board")``."""
    kind, _, name = reference.partition(":")
    return kind, name


class ConsumersConfig(FrozenModel):
    """The names a consumer reference may use, besides the research registry's hypotheses."""

    components: list[Identifier] = Field(min_length=1)
    views: list[Identifier] = Field(min_length=1)
    safety: list[Identifier] = Field(min_length=1)

    def problems(self, references: list[str], where: str) -> list[str]:
        """The references that name nothing declared (a hypothesis is checked by a test)."""
        known = {
            "component": set(self.components),
            "view": set(self.views),
            "safety": set(self.safety),
        }
        found = []
        for reference in references:
            kind, name = consumer_parts(reference)
            if kind in known and name not in known[kind]:
                found.append(f"{where} names {reference}, which is not declared")
        return found


class SourceSchedule(FrozenModel):
    """A moment of one venue's session: its open or close moved by an offset."""

    venue: Mic
    anchor: Anchor
    offset_minutes: int


class ProviderConfig(FrozenModel):
    """One source: built (a fetcher on a schedule) or declared (what would build it)."""

    name: NonEmptyText
    area: SourceArea
    status: SourceStatus
    credential: EnvName | None
    consumers: list[ConsumerRef]
    activation: NonEmptyText | None
    release_date_policy: ReleaseDatePolicy | None
    release_lag_days: NonNegativeInt | None
    schedule: list[SourceSchedule]
    stale_after_hours: PositiveInt | None
    bias_notes: list[NonEmptyText]

    @model_validator(mode="after")
    def _built_or_declared(self) -> Self:
        if self.status == "built":
            missing = [
                name
                for name, value in (
                    ("release_date_policy", self.release_date_policy),
                    ("release_lag_days", self.release_lag_days),
                    ("stale_after_hours", self.stale_after_hours),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"a built source states {', '.join(missing)}")
            if not self.consumers:
                raise ValueError("a built source names what consumes it (DECISIONS D66)")
            if self.activation is not None:
                raise ValueError("a built source runs; its activation is null")
        else:
            if self.activation is None:
                raise ValueError("a declared source states the condition that would build it")
            if self.consumers or self.schedule:
                raise ValueError(
                    "a declared source has no consumer and no schedule; one that is consumed is "
                    "built"
                )
        return self


class ReleaseOverride(FrozenModel):
    """A series' own release rule where its source's does not fit it."""

    policy: ReleaseDatePolicy
    lag_days: NonNegativeInt


class MacroSeriesConfig(FrozenModel):
    """One macro series at its native frequency."""

    id: SeriesId
    source: Identifier
    frequency: Frequency
    units: NonEmptyText
    group: Identifier
    key: NonEmptyText | None
    release: ReleaseOverride | None
    value_vintage: ValueVintage | None
    consumers: list[ConsumerRef] = Field(min_length=1)

    @model_validator(mode="after")
    def _addressing(self) -> Self:
        if (self.source == "fred") != (self.value_vintage is not None):
            raise ValueError(
                f"{self.id}: value_vintage is FRED's (initial or latest) and null elsewhere"
            )
        if (self.source == "fred") != (self.key is None):
            raise ValueError(f"{self.id}: a FRED series is addressed by its id; others by a key")
        return self


class MacroCollectionConfig(FrozenModel):
    default_window_years: PositiveInt
    refetch_after_seconds: NonNegativeInt
    max_observations_per_series: PositiveInt
    stop_after_consecutive_failures: PositiveInt


class CommodityHistoryConfig(FrozenModel):
    window_years: PositiveInt
    frequency: Literal["native", "monthly"]
    aggregation_method: Literal["avg", "sum", "eop"]


class FredConfig(FrozenModel):
    observations_url: Url
    vintage_dates_url: Url
    series_url: Url
    max_vintages_per_call: PositiveInt


class EcbConfig(FrozenModel):
    data_url: Url


class BaseUrlConfig(FrozenModel):
    base_url: Url


class OnsConfig(FrozenModel):
    base_url: Url
    #: Where the ONS publishes: its UTC update stamps are read as days there.
    timezone: TimeZoneName


class StatcanConfig(FrozenModel):
    vectors_url: Url


class DataUrlConfig(FrozenModel):
    data_url: Url


class FrbConfig(FrozenModel):
    ebp_url: Url


class MacroConfig(FrozenModel):
    collection: MacroCollectionConfig
    commodity_history: CommodityHistoryConfig
    fred: FredConfig
    ecb: EcbConfig
    eia: BaseUrlConfig
    ons: OnsConfig
    statcan: StatcanConfig
    boj: DataUrlConfig
    bis: DataUrlConfig
    frb: FrbConfig
    series: list[MacroSeriesConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [series.id for series in self.series]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"macro.series declares {', '.join(duplicated)} more than once")
        return self


class CotContractConfig(FrozenModel):
    """One CFTC contract: its market code and what reads it."""

    code: Annotated[str, Field(pattern=r"^[0-9A-Z+]{6}$")]
    name: NonEmptyText
    consumers: list[ConsumerRef] = Field(min_length=1)


class PositioningConfig(FrozenModel):
    annual_url_template: Annotated[str, Field(pattern=r"^https://\S*\{year\}\S*$")]
    socrata_url: Url
    socrata_page_size: PositiveInt
    incremental_weeks: PositiveInt
    backfill_start_year: Annotated[int, Field(ge=1986)]
    percentile_window_weeks: PositiveInt
    percentile_min_weeks: PositiveInt
    velocity_weeks: PositiveInt
    contracts: dict[SeriesId, CotContractConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.percentile_min_weeks > self.percentile_window_weeks:
            raise ValueError("percentile_min_weeks cannot exceed percentile_window_weeks")
        codes = [contract.code for contract in self.contracts.values()]
        duplicated = sorted({c for c in codes if codes.count(c) > 1})
        if duplicated:
            raise ValueError(f"contracts name market code(s) {duplicated} more than once")
        return self


class InsidersConfig(FrozenModel):
    daily_index_url_template: Annotated[
        str, Field(pattern=r"^https://\S*\{year\}\S*\{quarter\}\S*\{day\}\S*$")
    ]
    archive_url: Url
    tickers_url: Url
    forms: list[NonEmptyText] = Field(min_length=1)
    request_spacing_seconds: NonNegativeFloat
    lookback_days: PositiveInt
    purchase_code: Annotated[str, Field(pattern=r"^[A-Z]$")]
    cluster_window_days: PositiveInt


class PolymarketConfig(FrozenModel):
    gamma_url: Url
    curated_events: list[NonEmptyText]
    discovery_tags: list[NonEmptyText]
    discovery_limit: PositiveInt
    min_discovery_liquidity: NonNegativeFloat


class KalshiConfig(FrozenModel):
    api_url: Url
    curated_series: list[NonEmptyText]
    max_markets_per_series: PositiveInt


class GdeltQueryConfig(FrozenModel):
    query: NonEmptyText
    theme: Identifier


class GdeltConfig(FrozenModel):
    doc_url: Url
    timespan: Annotated[str, Field(pattern=r"^[0-9]+[dhm]$")]
    request_spacing_seconds: NonNegativeFloat
    rate_limited_backoff_seconds: PositiveFloat
    rate_limited_retries: NonNegativeInt
    min_history_days: Annotated[int, Field(ge=2)]
    queries: dict[Identifier, GdeltQueryConfig] = Field(min_length=1)


class EventsConfig(FrozenModel):
    polymarket: PolymarketConfig
    kalshi: KalshiConfig
    velocity_hours: PositiveInt
    gdelt: GdeltConfig


class FeedConfig(FrozenModel):
    url: Url
    kind: FeedKind
    region: Identifier | None
    symbol: NonEmptyText | None

    @model_validator(mode="after")
    def _issuer_names_its_symbol(self) -> Self:
        if (self.kind == "issuer") != (self.symbol is not None):
            raise ValueError("an issuer's feed names its symbol; no other feed does")
        return self


class NewsConfig(FrozenModel):
    max_body_bytes: PositiveInt
    max_summary_chars: PositiveInt
    feeds: dict[Identifier, FeedConfig] = Field(min_length=1)


class HaltFeedConfig(FrozenModel):
    url: Url
    origin: NonEmptyText
    markets: dict[NonEmptyText, Mic] = Field(min_length=1)
    time_venue: Mic
    refresh_minutes: PositiveInt
    alert_after_failures: PositiveInt


class HaltsConfig(FrozenModel):
    nasdaq_trader: HaltFeedConfig


class CatalystConfig(FrozenModel):
    """One dated catalyst and the page that states its date."""

    kind: Identifier
    region: Identifier
    day: date
    source: Url


class EarningsConfig(FrozenModel):
    refresh_days: PositiveInt
    horizon_days: PositiveInt
    past_reports: NonNegativeInt


class CatalystsConfig(FrozenModel):
    events: list[CatalystConfig] = Field(min_length=1)
    earnings: EarningsConfig

    @model_validator(mode="after")
    def _unique(self) -> Self:
        keys = [(event.kind, event.day) for event in self.events]
        duplicated = sorted({f"{k} {d}" for k, d in keys if keys.count((k, d)) > 1})
        if duplicated:
            raise ValueError(f"catalysts list {', '.join(duplicated)} more than once")
        return self


class SourcesConfig(FrozenModel):
    """Every source besides IBKR, and what it serves."""

    consumers: ConsumersConfig
    providers: dict[Identifier, ProviderConfig] = Field(min_length=1)
    macro: MacroConfig
    positioning: PositioningConfig
    insiders: InsidersConfig
    events: EventsConfig
    news: NewsConfig
    halts: HaltsConfig
    catalysts: CatalystsConfig

    def provider(self, name: str) -> ProviderConfig:
        return self.providers[name]

    def built(self) -> dict[str, ProviderConfig]:
        return {name: p for name, p in self.providers.items() if p.status == "built"}

    def references(self) -> list[tuple[str, str]]:
        """Every consumer reference with where it is made."""
        found = [(r, f"providers.{n}") for n, p in self.providers.items() for r in p.consumers]
        found += [(r, f"macro.series {s.id}") for s in self.macro.series for r in s.consumers]
        found += [
            (r, f"positioning.contracts.{n}")
            for n, c in self.positioning.contracts.items()
            for r in c.consumers
        ]
        return found

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        problems: list[str] = []
        for reference, where in self.references():
            problems += self.consumers.problems([reference], where)
        built = self.built()
        for series in self.macro.series:
            provider = built.get(series.source)
            if provider is None or provider.area != "macro":
                problems.append(
                    f"macro.series {series.id}: {series.source} is no built macro source"
                )
        for area, name in (
            ("positioning", "cftc"),
            ("insiders", "sec_edgar"),
            ("events", "polymarket"),
            ("events", "kalshi"),
            ("events", "gdelt"),
            ("news", "rss"),
            ("halts", "nasdaq_trader"),
            ("calendar", "earnings"),
        ):
            provider = built.get(name)
            if provider is None or provider.area != area:
                problems.append(f"providers.{name} must be a built {area} source")
        if problems:
            raise ValueError("; ".join(problems))
        return self
