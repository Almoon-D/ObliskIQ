"""The thesis book's vocabulary (MASTER_PLAN §5.5, DECISIONS D156): a written idea, its
lifecycle, its log and its grade.

A **draft** is what a source hands in; it may lack a field, and then it is refused with the reason
and never stored. A **thesis** is a complete idea in the store: the instrument, what is expected,
a target, a horizon, an invalidation and an entry rule, all fixed before the first order. The
position is always long. A target above the invalidation is held as the instrument or as a long
call on it; a bearish view is a thesis on an instrument that holds or gains in a fall, or levels
that describe the instrument's own fall (the target below the invalidation), held as a long put
or as nothing, never a short (DECISIONS D157).

**Lifecycle.** ``candidate`` (complete, waiting for the gate) → ``approved`` (the gate passed and
a slot is taken; from here its text and triggers never change, a store trigger) → ``active`` (its
entry filled) → ``closed`` (its position left by one of its own exits). A thesis that never fills
ends ``refused`` (the gate), ``withdrawn`` (its source) or ``expired`` (its entry window passed).
Every change is appended to the thesis's log.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import AfterValidator, Field, model_validator

from .base import Conid, CountryCode, CurrencyCode, DomainModel, NonEmptyText, UtcDatetime
from .enums import AssetClass
from .orders import ExecutionMode
from .tax import Isin


def _positive(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("must be a positive price")
    return value


type Price = Annotated[Decimal, AfterValidator(_positive)]


class ThesisSource(StrEnum):
    """Who wrote the idea. Each source is graded apart."""

    OWNER = "owner"
    ANALYST = "analyst"


class Conviction(StrEnum):
    """How strongly its source holds the idea; above ``normal`` it is the owner's to give."""

    NORMAL = "normal"
    HIGH = "high"
    MAX = "max"


class View(StrEnum):
    """What the idea expects of the market it speaks of. The position is long either way."""

    BULLISH = "bullish"
    BEARISH = "bearish"


class Expression(StrEnum):
    """What a thesis may be held as: its instrument, a long option on it, or ``auto`` (the option
    when the options sleeve's gates pass, otherwise the instrument). What an approved thesis is
    held as is one of the first two."""

    SHARES = "shares"
    OPTION = "option"
    AUTO = "auto"


class ThesisState(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    ACTIVE = "active"
    CLOSED = "closed"
    REFUSED = "refused"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


#: States a thesis never leaves.
ENDED_STATES: Final = frozenset(
    {ThesisState.CLOSED, ThesisState.REFUSED, ThesisState.WITHDRAWN, ThesisState.EXPIRED}
)
#: States that hold one of the book's slots.
SLOT_STATES: Final = frozenset({ThesisState.APPROVED, ThesisState.ACTIVE})
#: States the book still works on.
OPEN_STATES: Final = frozenset({ThesisState.CANDIDATE, *SLOT_STATES})


class LogKind(StrEnum):
    """What an entry of a thesis's log records."""

    INTAKE = "intake"
    RESOLVED = "resolved"
    WITHHELD = "withheld"
    STATE = "state"
    ORDER = "order"
    REUNDERWRITE = "reunderwrite"


#: What starts the key of a stock named by the SEC's listings (its CIK follows).
SEC_KEY_PREFIX: Final = "sec:"


def sec_key(cik: int) -> str:
    """The candidate key of the company the SEC lists under ``cik``."""
    return f"{SEC_KEY_PREFIX}{cik}"


class ThesisInstrument(DomainModel):
    """What a thesis trades. A universe instrument is pinned and described when the idea is
    handed in. A share outside the universe file (``candidate_key``) is pinned (``conid``, listing
    venue and currency) when IBKR's contract search resolves it: a stock candidate of a holdings
    file carries its ISIN from the start; a stock the SEC lists (its key ``sec:<CIK>``, its
    ``symbol`` the SEC's ticker) may lack one, and then reaches no broker until the owner gives it
    (DECISIONS D195). ``sector`` and ``country`` are ``None`` where nothing states them."""

    symbol: NonEmptyText
    candidate_key: NonEmptyText | None
    conid: Conid | None
    listing_exchange: NonEmptyText | None
    currency: CurrencyCode | None
    asset_class: AssetClass
    description: NonEmptyText
    sector: NonEmptyText | None
    country: CountryCode | None
    defensive: bool
    isin: Isin | None

    @model_validator(mode="after")
    def _pinned_whole(self) -> Self:
        parts = (self.conid, self.listing_exchange, self.currency)
        if any(part is None for part in parts) and any(part is not None for part in parts):
            raise ValueError("a pinned instrument names its conid, listing venue and currency")
        if self.candidate_key is None and self.conid is None:
            raise ValueError("a universe instrument is pinned when the idea is handed in")
        if self.candidate_key is not None and self.asset_class is not AssetClass.STOCK:
            raise ValueError("a share outside the universe file is a share")
        if self.candidate_key is not None and self.isin is None and self.sec_cik is None:
            raise ValueError("a stock candidate of a holdings file carries its ISIN")
        return self

    @property
    def pinned(self) -> bool:
        return self.conid is not None

    @property
    def sec_cik(self) -> int | None:
        """The SEC's CIK of a stock named by its listings; ``None`` for any other instrument."""
        key = self.candidate_key
        if key is None or not key.startswith(SEC_KEY_PREFIX):
            return None
        digits = key.removeprefix(SEC_KEY_PREFIX)
        return int(digits) if digits.isdecimal() else None


class ThesisDraft(DomainModel):
    """An idea as a source hands it in. Exactly one of ``symbol`` (a universe instrument),
    ``candidate`` (a stock candidate's key, with its ``isin``) and ``listing`` (a ticker on the
    SEC's latest listings, its ``isin`` optional) names the instrument. A field left out is
    ``None``: the intake refuses the draft and says which."""

    source: ThesisSource
    conviction: Conviction = Conviction.NORMAL
    view: View = View.BULLISH
    expression: Expression = Expression.SHARES
    symbol: NonEmptyText | None = None
    candidate: NonEmptyText | None = None
    listing: NonEmptyText | None = None
    isin: Isin | None = None
    expectation: str | None = None
    target: Decimal | None = None
    invalidation: Decimal | None = None
    horizon: date | None = None
    entry_max: Decimal | None = None
    enter_by: date | None = None
    #: The probability its source states that the target comes first (H14); none where the source
    #: states none (the owner's ideas).
    probability: Annotated[float, Field(ge=0, le=1)] | None = None


class Thesis(DomainModel):
    """A complete idea in the store, and where it stands."""

    #: :data:`UNSTORED` until the store assigns it.
    thesis_id: Annotated[int, Field(ge=0)]
    source: ThesisSource
    conviction: Conviction
    view: View
    expression: Expression = Expression.SHARES
    instrument: ThesisInstrument
    expectation: NonEmptyText
    target: Price
    invalidation: Price
    horizon: date
    #: The entry rule: bought only at or below ``entry_max`` (when given), on or before
    #: ``enter_by``, while the gain-to-loss ratio holds at the price.
    entry_max: Price | None
    enter_by: date
    state: ThesisState
    created_at: UtcDatetime
    #: Fixed at approval: the mode whose record the thesis belongs to, its size in the base
    #: currency (an option's: the premium its block was sized at) and what it is held as.
    mode: ExecutionMode | None = None
    size_base: Price | None = None
    held_as: Expression | None = None
    approved_at: UtcDatetime | None = None
    opened_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    exit_reason: NonEmptyText | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.target == self.invalidation:
            raise ValueError("the target and the invalidation are two levels")
        if self.falls and (
            self.view is not View.BEARISH
            or self.expression is Expression.SHARES
            or self.entry_max is not None
        ):
            raise ValueError(
                "a target below the invalidation is a bearish idea held as a long put, with no "
                "entry limit: the book never shorts"
            )
        if self.entry_max is not None and not self.invalidation < self.entry_max < self.target:
            raise ValueError("entry_max lies between the invalidation and the target")
        if not self.created_at.date() <= self.enter_by <= self.horizon:
            raise ValueError("the entry window ends on or before the horizon, not in the past")
        if self.conviction is not Conviction.NORMAL and self.source is not ThesisSource.OWNER:
            raise ValueError("only the owner marks a conviction above normal")
        approved = (
            self.mode is not None,
            self.size_base is not None,
            self.approved_at is not None,
            self.held_as is not None,
        )
        if any(approved) != all(approved):
            raise ValueError("the mode, the size, the holding and the approval are fixed together")
        if self.held_as is Expression.AUTO or (
            self.held_as is not None
            and self.expression is not Expression.AUTO
            and self.held_as is not self.expression
        ):
            raise ValueError("a thesis is held as its instrument or an option, as it allowed")
        if self.falls and self.held_as is Expression.SHARES:
            raise ValueError("a fall is held as a long put or as nothing")
        return self

    @property
    def falls(self) -> bool:
        """Whether its levels describe a fall of the instrument: the target below the
        invalidation, held as a long put or as nothing."""
        return self.target < self.invalidation

    @property
    def as_option(self) -> bool:
        return self.held_as is Expression.OPTION

    @property
    def ref(self) -> str:
        """How the thesis is named to the owner and on its intents."""
        return thesis_ref(self.thesis_id)


#: The id of a thesis the store has not numbered yet.
UNSTORED: Final = 0
#: The letter a thesis's reference starts with.
REF_PREFIX: Final = "T"


def thesis_ref(thesis_id: int) -> str:
    return f"{REF_PREFIX}{thesis_id}"


def parse_ref(text: str) -> int | None:
    """The id a reference names (``T3`` or ``3``); ``None`` when it names none."""
    digits = text.strip().upper().removeprefix(REF_PREFIX)
    return int(digits) if digits.isdecimal() and int(digits) >= 1 else None


class ThesisLogEntry(DomainModel):
    """One entry of a thesis's append-only log (the re-underwrite log of MASTER_PLAN §5.5)."""

    thesis_id: Annotated[int, Field(ge=1)]
    seq: Annotated[int, Field(ge=1)]
    at: UtcDatetime
    actor: NonEmptyText
    kind: LogKind
    detail: NonEmptyText


class ThesisGrade(DomainModel):
    """A closed thesis against what it wrote: whether the target was reached, the payoff against
    the one stated, the days against the horizon."""

    thesis_id: Annotated[int, Field(ge=1)]
    graded_at: UtcDatetime
    mode: ExecutionMode
    source: ThesisSource
    conviction: Conviction
    #: What the graded position was; an option's prices are premiums, its payoff the return on
    #: the premium paid.
    held_as: Expression = Expression.SHARES
    exit_reason: NonEmptyText
    quantity: Price
    entry_price: Price
    exit_price: Price
    reached: bool
    #: The return realised from the average entry to the average exit, before costs and tax.
    payoff: Annotated[Decimal, Field(allow_inf_nan=False)]
    #: The return the thesis stated: its target over the average entry, less one.
    stated_payoff: Annotated[Decimal, Field(allow_inf_nan=False)]
    days_held: Annotated[int, Field(ge=0)]
    horizon_days: Annotated[int, Field(ge=0)]
