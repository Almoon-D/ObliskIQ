"""What the sources besides IBKR deliver: CFTC positioning, SEC Form 4 filings, prediction-market
snapshots, GDELT's daily counts, news items and their extractions, earnings dates, and each
source's health.

Every record carries the date or instant it became known: a report's release date, a filing's
filing date, a snapshot's observation, a collection's stamp. Reads as of a date use it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, NonNegativeFloat, NonNegativeInt, PositiveInt, model_validator

from .base import DomainModel, NonEmptyText, UtcDatetime

#: A probability or a rank, in [0, 1].
type Probability = Annotated[float, Field(ge=0, le=1)]
#: A tone score in [-1, 1].
type Tone = Annotated[float, Field(ge=-1, le=1)]
type PredictionVenue = Literal["polymarket", "kalshi"]
#: A run's verdict as the run log names it.
type RunVerdict = Literal["ok", "degraded", "partial", "error", "skipped"]
type FeedKind = Literal["central_bank", "regulator", "issuer"]


# ── positioning ──


class CotReport(DomainModel):
    """One weekly CFTC report of one contract (legacy futures-only, all positions).

    ``report_date`` is the Tuesday the positions are of; ``release_date`` when the report was
    public. A leg the report leaves blank is ``None``, never zero.
    """

    contract_code: NonEmptyText
    report_date: date
    release_date: date
    market_name: NonEmptyText
    open_interest: PositiveInt
    noncomm_long: NonNegativeInt | None
    noncomm_short: NonNegativeInt | None
    comm_long: NonNegativeInt | None
    comm_short: NonNegativeInt | None
    nonrept_long: NonNegativeInt | None
    nonrept_short: NonNegativeInt | None
    source: Literal["cftc_annual", "cftc_socrata"]
    collected_at: UtcDatetime

    @model_validator(mode="after")
    def _released_after(self) -> Self:
        if self.release_date < self.report_date:
            raise ValueError("a report is released on or after the date its positions are of")
        return self

    @property
    def net_noncommercial_share(self) -> float | None:
        """Non-commercial long minus short, as a fraction of open interest; ``None`` without both
        legs. A share, not a count: a percentile of a raw count over years ranks how large the
        market grew, not how crowded it is (lite)."""
        if self.noncomm_long is None or self.noncomm_short is None:
            return None
        return (self.noncomm_long - self.noncomm_short) / self.open_interest

    @property
    def net_commercial_share(self) -> float | None:
        """Commercial (hedgers') long minus short, as a fraction of open interest."""
        if self.comm_long is None or self.comm_short is None:
            return None
        return (self.comm_long - self.comm_short) / self.open_interest


class PositioningStatus(StrEnum):
    OK = "ok"
    WARMING_UP = "warming_up"
    NO_DATA = "no_data"


class PositioningReading(DomainModel):
    """A contract's crowding as known on ``as_of``: the newest released share, its inclusive
    percentile within the trailing window, and its change over the velocity window.

    ``percentile`` is ``None`` until the window holds the warm-up; ``velocity`` is ``None`` until
    the report that many weeks back exists. Neither is ever filled in.
    """

    contract_id: NonEmptyText
    as_of: date
    status: PositioningStatus
    report_date: date | None
    release_date: date | None
    share: float | None
    percentile: Probability | None
    window: NonNegativeInt
    velocity: float | None
    reason: str


# ── insiders ──


class InsiderOwner(DomainModel):
    """A reporting owner of a Form 4 filing and their relationship to the issuer."""

    owner_cik: PositiveInt
    owner_name: NonEmptyText
    is_director: bool
    is_officer: bool
    is_ten_percent: bool
    is_other: bool
    officer_title: NonEmptyText | None


class InsiderTransaction(DomainModel):
    """One transaction line of a Form 4 filing."""

    line: NonNegativeInt
    derivative: bool
    security_title: NonEmptyText
    transaction_date: date
    code: NonEmptyText
    shares: NonNegativeFloat | None
    price: NonNegativeFloat | None
    acquired: bool | None
    shares_after: NonNegativeFloat | None
    direct: bool | None


class InsiderFiling(DomainModel):
    """A Form 4 filing as EDGAR disseminated it; public on ``filed_on``."""

    accession: NonEmptyText
    form: NonEmptyText
    issuer_cik: PositiveInt
    issuer_symbol: NonEmptyText | None
    filed_on: date
    accepted_at: UtcDatetime
    period_of_report: date | None
    owners: tuple[InsiderOwner, ...] = Field(min_length=1)
    transactions: tuple[InsiderTransaction, ...]
    collected_at: UtcDatetime


class ClusterInputs(DomainModel):
    """The inputs of an insider-purchase cluster for one issuer, known on ``as_of``.

    Counted over the filings dated in ``[as_of - window_days + 1, as_of]``: open-market purchases
    (the purchase code, acquired, non-derivative) by owners who are officers or directors. The
    rule that calls a cluster is H2's and the Hunter's (Sessions 15 and 21), not this record's.
    """

    issuer_cik: PositiveInt
    as_of: date
    window_days: PositiveInt
    filings: NonNegativeInt
    buyers: NonNegativeInt
    officers: NonNegativeInt
    directors: NonNegativeInt
    shares: NonNegativeFloat
    value: NonNegativeFloat
    unpriced_lines: NonNegativeInt
    first_filed: date | None
    last_filed: date | None


# ── prediction markets ──


class EventMarket(DomainModel):
    """A market on a prediction venue and the outcome whose probability is recorded."""

    venue: PredictionVenue
    market_id: NonEmptyText
    event_id: NonEmptyText | None
    question: NonEmptyText
    outcome: NonEmptyText
    closes_at: UtcDatetime | None
    curated: bool
    tags: tuple[NonEmptyText, ...]


class EventSnapshot(DomainModel):
    """The probability of a market's recorded outcome when it was read."""

    venue: PredictionVenue
    market_id: NonEmptyText
    observed_at: UtcDatetime
    probability: Probability
    bid: Probability | None
    ask: Probability | None
    last_price: Probability | None
    volume: NonNegativeFloat | None
    liquidity: NonNegativeFloat | None


class EventReading(DomainModel):
    """A market's newest probability as known at ``at`` and its change since the newest
    snapshot at least the velocity window older; ``velocity`` is ``None`` without one."""

    venue: PredictionVenue
    market_id: NonEmptyText
    at: UtcDatetime
    observed_at: UtcDatetime
    probability: Probability
    velocity: float | None
    reference_at: UtcDatetime | None


# ── GDELT ──


class GdeltDay(DomainModel):
    """One complete UTC day of a GDELT query."""

    query_id: NonEmptyText
    day: date
    articles: NonNegativeInt
    monitored: PositiveInt
    tone: float | None
    collected_at: UtcDatetime

    @property
    def share(self) -> float:
        """Matching articles as a fraction of everything GDELT monitored that day."""
        return self.articles / self.monitored


class PulseReading(DomainModel):
    """A query's newest complete day as known on ``as_of``, with the Welford z-scores of its
    article share and tone against every earlier day held then. ``None`` scores while warming up."""

    query_id: NonEmptyText
    as_of: date
    day: date | None
    share: float | None
    tone: float | None
    share_z: float | None
    tone_z: float | None
    history: NonNegativeInt
    warming_up: bool


# ── news and extractions ──


class NewsItem(DomainModel):
    """One feed item, as received (``raw``) and as read."""

    item_id: NonEmptyText
    feed: NonEmptyText
    kind: FeedKind
    region: NonEmptyText | None
    symbol: NonEmptyText | None
    guid: NonEmptyText | None
    link: NonEmptyText | None
    title: NonEmptyText
    summary: str | None
    published_at: UtcDatetime | None
    collected_at: UtcDatetime
    raw: NonEmptyText
    fallback_tone: Tone | None


class ExtractionStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    FAILED = "failed"


class EventExtraction(DomainModel):
    """One EventExtractor call for an item: its output (validated JSON) or why there is none."""

    item_id: NonEmptyText
    produced_at: UtcDatetime
    provider: NonEmptyText
    model_id: NonEmptyText
    prompt_version: PositiveInt
    prompt_hash: NonEmptyText
    status: ExtractionStatus
    attempts: NonNegativeInt
    output: str | None
    error: str | None
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cost_usd: NonNegativeFloat

    @model_validator(mode="after")
    def _output_iff_ok(self) -> Self:
        if (self.status is ExtractionStatus.OK) != (self.output is not None):
            raise ValueError("an extraction holds an output exactly when it is ok")
        if self.status is not ExtractionStatus.OK and not self.error:
            raise ValueError("an extraction that is not ok says why")
        return self


# ── calendars ──


class EarningsDate(DomainModel):
    """A report date one read of a research-grade source listed for a symbol."""

    symbol: NonEmptyText
    collected_on: date
    report_at: UtcDatetime
    source: NonEmptyText
    grade: Literal["research"]


class Catalyst(DomainModel):
    """A dated catalyst: a central-bank decision, a key release, an earnings report."""

    kind: NonEmptyText
    subject: NonEmptyText
    day: date
    source: NonEmptyText
    grade: Literal["official", "research"]


# ── health ──


class SourceState(StrEnum):
    OK = "ok"
    STALE = "stale"
    FAILING = "failing"


class SourceRun(DomainModel):
    """A source's last run as recorded."""

    source: NonEmptyText
    last_attempt_at: UtcDatetime
    last_success_at: UtcDatetime | None
    last_status: RunVerdict
    last_rows: NonNegativeInt
    consecutive_failures: NonNegativeInt
    detail: str | None


class SourceHealth(DomainModel):
    """A source's health as reported: ``failing`` when its last run failed, ``stale`` when it has
    not succeeded within its declared window (or never has), ``ok`` otherwise."""

    source: NonEmptyText
    state: SourceState
    last_attempt_at: UtcDatetime | None
    last_success_at: UtcDatetime | None
    rows: NonNegativeInt
    reason: str
