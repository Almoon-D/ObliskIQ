"""The shape of the IBKR and HTTP configuration.

Field maps and pacing groups are lists of records rather than mappings: an owner override replaces
the whole list, and the decoder and the rate limiter read each record by its attributes.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from ..domain.base import CurrencyCode, NonEmptyText
from ..domain.calendar import Mic
from .base import FrozenModel

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FIELD_ID_RE = re.compile(r"[0-9]{1,6}")
_PREFIX_RE = re.compile(r"D[A-Z]{0,3}")

#: IBKR's per-request ceilings (changelog 2025-12-10, docs/knowledge/ibkr.md): a configured
#: batch above them is refused by the API, so the configuration may not ask for one.
IBKR_MAX_CONIDS_PER_SNAPSHOT = 100
IBKR_MAX_FIELDS_PER_SNAPSHOT = 50

#: Endpoints IBKR paces on their own (docs/knowledge/ibkr.md, "Pacing"): each must sit in a rate
#: group, or an owner override could leave it paced by the global window alone.
IBKR_PACED_ENDPOINTS = (
    "iserver/marketdata/history",
    "iserver/marketdata/snapshot",
    "portfolio/accounts",
    "iserver/scanner/params",
    "iserver/scanner/run",
    "tickle",
)

#: The names the snapshot decoder knows. A field map may only name these.
type SnapshotFieldName = Literal[
    "last",
    "bid",
    "ask",
    "volume",
    "open_interest",
    "implied_vol",
    "historical_vol_30d",
    "implied_vol_percentile",
    "dividend_yield",
    "delta",
    "gamma",
    "vega",
    "theta",
    "market_data_status",
]


def _api_path(value: str) -> str:
    if not value.startswith("/") or value.endswith("/") or "//" in value or "?" in value:
        raise ValueError("must be an absolute path without a trailing slash, such as /v1/api")
    return value


def _relative_path(value: str) -> str:
    if value.startswith("/") or not value or " " in value:
        raise ValueError("must be an endpoint path without a leading slash, such as tickle")
    return value


def _https_origin(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.port is None:
        raise ValueError("must be an https:// origin with a port, such as https://localhost:5000")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("must be a bare origin without a path")
    return f"{parts.scheme}://{parts.netloc}"


def _https_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("must be an https:// URL")
    return value


def _sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("must be a SHA-256 digest: 64 lower-case hexadecimal characters")
    return value


def _field_id(value: str | None) -> str | None:
    if value is not None and not _FIELD_ID_RE.fullmatch(value):
        raise ValueError('must be a quoted numeric IBKR field id, such as "31", or null')
    return value


def _account_prefix(value: str) -> str:
    if not _PREFIX_RE.fullmatch(value):
        # IBKR paper accounts start with D (DU, DF); a live prefix (U, F) is never a paper one.
        raise ValueError("must be one to four capital letters starting with D, such as DU")
    return value


type ApiPath = Annotated[str, AfterValidator(_api_path)]
type EndpointPath = Annotated[str, AfterValidator(_relative_path)]
type HttpsOrigin = Annotated[str, AfterValidator(_https_origin)]
type HttpsUrl = Annotated[str, AfterValidator(_https_url)]
type Sha256 = Annotated[str, AfterValidator(_sha256)]
type FieldId = Annotated[str | None, AfterValidator(_field_id)]
type AccountPrefix = Annotated[str, AfterValidator(_account_prefix)]
type CssSelector = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
type IbkrPeriod = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,3}(min|h|d|w|m|y)$")]


class HttpConfig(FrozenModel):
    """The shared retry policy (``util.http.RetryPolicy``)."""

    timeout_seconds: PositiveFloat
    retries: NonNegativeInt
    base_delay_seconds: NonNegativeFloat
    max_delay_seconds: NonNegativeFloat
    jitter_seconds: NonNegativeFloat
    retry_status_codes: list[Annotated[int, Field(ge=400, le=599)]]
    retry_after_max_seconds: PositiveFloat

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must not be below base_delay_seconds")
        if self.retry_after_max_seconds < self.max_delay_seconds:
            raise ValueError("retry_after_max_seconds must not be below max_delay_seconds")
        return self


class OAuthConfig(FrozenModel):
    realm: NonEmptyText
    signature_method_rsa: Literal["RSA-SHA256"]
    signature_method_hmac: Literal["HMAC-SHA256"]
    live_session_token_path: ApiPath
    live_session_token_ttl_seconds: PositiveFloat
    live_session_token_refresh_margin_seconds: NonNegativeFloat
    dh_prime_bits_min: PositiveInt
    nonce_bytes: Annotated[int, Field(ge=8)]
    user_agent: NonEmptyText

    @model_validator(mode="after")
    def _margin_inside_ttl(self) -> Self:
        if self.live_session_token_refresh_margin_seconds >= self.live_session_token_ttl_seconds:
            raise ValueError("the refresh margin must be shorter than the token lifetime")
        return self


class SessionConfig(FrozenModel):
    ssodh_init_on_connect: bool
    compete: bool
    tickle_interval_seconds: PositiveFloat
    tickle_failure_grace: PositiveInt
    reauthenticate_attempts: PositiveInt
    reauthenticate_pause_seconds: NonNegativeFloat


class RateWindow(FrozenModel):
    """At most ``requests`` in any ``per_seconds`` interval."""

    requests: PositiveInt
    per_seconds: PositiveFloat


class RateGroup(FrozenModel):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
    paths: list[EndpointPath] = Field(min_length=1)
    windows: list[RateWindow] = Field(min_length=1)


class RateLimitConfig(FrozenModel):
    max_concurrency: PositiveInt
    global_windows: list[RateWindow] = Field(min_length=1)
    groups: list[RateGroup]
    breaker_failure_threshold: PositiveInt
    breaker_open_seconds: PositiveFloat

    @model_validator(mode="after")
    def _groups_are_distinct(self) -> Self:
        names = [group.name for group in self.groups]
        if len(names) != len(set(names)):
            raise ValueError("each rate group needs a distinct name")
        paths = [path for group in self.groups for path in group.paths]
        if len(paths) != len(set(paths)):
            raise ValueError("an endpoint path belongs to one rate group")
        unpaced = [
            endpoint
            for endpoint in IBKR_PACED_ENDPOINTS
            if not any(endpoint == p or endpoint.startswith(f"{p}/") for p in paths)
        ]
        if unpaced:
            raise ValueError(f"IBKR paces {', '.join(unpaced)} on its own; each needs a rate group")
        return self


class SnapshotField(FrozenModel):
    name: SnapshotFieldName
    id: FieldId


class MarketDataConfig(FrozenModel):
    max_conids_per_snapshot: Annotated[int, Field(ge=1, le=IBKR_MAX_CONIDS_PER_SNAPSHOT)]
    max_fields_per_snapshot: Annotated[int, Field(ge=1, le=IBKR_MAX_FIELDS_PER_SNAPSHOT)]
    snapshot_settle_retries: PositiveInt
    snapshot_settle_delay_seconds: NonNegativeFloat
    implied_vol_units: Literal["auto", "decimal", "percent"]
    implied_vol_units_fallback: Literal["decimal", "percent"]
    implied_vol_max_plausible: PositiveFloat
    implied_vol_min_plausible: NonNegativeFloat
    delta_max_plausible: PositiveFloat
    dividend_yield_units: Literal["auto", "decimal", "percent"]
    dividend_yield_units_fallback: Literal["decimal", "percent"]
    dividend_yield_max_plausible: PositiveFloat
    delayed_quote_bound_seconds: PositiveInt
    snapshot_fields: list[SnapshotField]
    underlying_snapshot_fields: list[SnapshotField]

    @model_validator(mode="after")
    def _field_maps_are_coherent(self) -> Self:
        for label, fields in (
            ("snapshot_fields", self.snapshot_fields),
            ("underlying_snapshot_fields", self.underlying_snapshot_fields),
        ):
            names = [field.name for field in fields]
            if len(names) != len(set(names)):
                raise ValueError(f"{label}: a name appears twice")
            ids = [field.id for field in fields if field.id is not None]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label}: a field id appears twice")
            if len(ids) > self.max_fields_per_snapshot:
                raise ValueError(f"{label}: more ids than max_fields_per_snapshot")
        if self.implied_vol_max_plausible <= self.implied_vol_min_plausible:
            raise ValueError("implied_vol_max_plausible must exceed implied_vol_min_plausible")
        return self


class PortfolioConfig(FrozenModel):
    max_position_pages: PositiveInt


class SecdefConfig(FrozenModel):
    exact_ticker_match_first: bool
    accept_first_on_no_exact_match: bool
    send_sec_type: bool


class HistoryConfig(FrozenModel):
    probe_request_days: PositiveInt
    chunk_days: PositiveInt
    max_chunks: PositiveInt
    span_stepdown_min_days: PositiveInt
    sessions_per_year: PositiveInt

    @model_validator(mode="after")
    def _floor_below_chunk(self) -> Self:
        if self.span_stepdown_min_days > self.chunk_days:
            raise ValueError("span_stepdown_min_days must not exceed chunk_days")
        return self


class TradingScheduleConfig(FrozenModel):
    skip_venues: list[Mic]
    missing_weekday_is_closed: bool


class GatewayLoginConfig(FrozenModel):
    route: Annotated[str, StringConstraints(pattern=r"^/\S+$")]
    username_selector: CssSelector
    password_selector: CssSelector
    paper_toggle_selector: CssSelector
    submit_selector: CssSelector
    error_selector: CssSelector
    success_text: NonEmptyText
    page_timeout_seconds: PositiveFloat
    success_timeout_seconds: PositiveFloat
    keystroke_delay_ms: NonNegativeInt
    presubmit_wait_seconds: NonNegativeFloat
    max_attempts: PositiveInt
    min_relogin_interval_seconds: NonNegativeFloat


class GatewayConfig(FrozenModel):
    download_url: HttpsUrl
    sha256: Sha256
    size_bytes: PositiveInt
    local_address: HttpsOrigin
    startup_timeout_seconds: PositiveFloat
    java_major_versions: list[PositiveInt] = Field(min_length=1)
    keystore_validity_days: PositiveInt
    probe_interval_seconds: PositiveFloat
    stop_grace_seconds: PositiveFloat
    login: GatewayLoginConfig


class PreflightConfig(FrozenModel):
    symbol: NonEmptyText
    listing_exchange: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9.]{2,12}$")]
    currency: CurrencyCode
    history_period: IbkrPeriod
    history_bar: IbkrPeriod


class IbkrConfig(FrozenModel):
    base_path: ApiPath
    request_timeout_seconds: PositiveFloat
    paper_account_prefixes: list[AccountPrefix] = Field(min_length=1)
    oauth: OAuthConfig
    session: SessionConfig
    ratelimit: RateLimitConfig
    marketdata: MarketDataConfig
    portfolio: PortfolioConfig
    secdef: SecdefConfig
    history: HistoryConfig
    trading_schedule: TradingScheduleConfig
    gateway: GatewayConfig
    preflight: PreflightConfig
