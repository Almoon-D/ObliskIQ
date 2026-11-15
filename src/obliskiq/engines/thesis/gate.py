"""The thesis book's deterministic gate, with a reason code per finding (MASTER_PLAN §5.5).

Every idea, whoever wrote it, passes the same two stages:

1. :func:`screen` judges a **draft** on its own words: a missing instrument, expectation, target,
   horizon or invalidation; levels that describe a fall of the instrument unless the idea is
   bearish and may be held as an option (a long put or nothing: the book never shorts); an entry
   rule outside its levels; dates that do not hold; a conviction only the owner may give.
   :func:`view_findings` adds the view against the instrument: a bearish idea held long is held
   only as an instrument of a defensive core role. A draft with a finding is not stored.
2. :func:`evaluate` judges a stored **thesis** against the market and the book, as the decision
   layer read them (:class:`GateFacts`): the mandate and the jurisdiction's eligibility (their
   verdicts, from the existing policies), an instrument another book holds, the levels against
   the price, the liquidity floor, the position, sector, country and currency caps, the minimum
   gain-to-loss ratio, a free slot, and a size worth trading.

A finding is **permanent** (the idea is refused, or expires if it was already approved) or it
**withholds** the idea for now: a later cycle may pass it. An unmeasured input withholds; it never
passes. The gate sizes by the book's rule alone (``book.py``): no model's probability reaches it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from ...config.schema_thesis import ThesisBookConfig
from ...domain.thesis import (
    Conviction,
    Expression,
    Thesis,
    ThesisDraft,
    ThesisInstrument,
    ThesisSource,
    View,
)
from .book import Slot, position_size

ZERO = Decimal(0)
ONE = Decimal(1)


class Reason(StrEnum):
    """Every way the gate finds against an idea, one code each."""

    INCOMPLETE = "incomplete"
    SHORT = "short"
    LEVELS = "levels"
    DATES = "dates"
    CONVICTION = "conviction"
    BEARISH_NOT_DEFENSIVE = "bearish_not_defensive"
    INSTRUMENT_UNKNOWN = "instrument_unknown"
    NOT_TRADABLE = "not_tradable"
    NO_CALENDAR = "no_calendar"
    UNRESOLVED = "unresolved"
    PROHIBITED_PRODUCT = "prohibited_product"
    INELIGIBLE = "ineligible"
    HELD_ELSEWHERE = "held_elsewhere"
    TARGET_PASSED = "target_passed"
    INVALIDATED = "invalidated"
    ILLIQUID = "illiquid"
    NOT_DESCRIBED = "not_described"
    NO_CAPITAL = "no_capital"
    NO_QUOTE = "no_quote"
    LIQUIDITY_UNMEASURED = "liquidity_unmeasured"
    GAIN_TO_LOSS = "gain_to_loss"
    ABOVE_ENTRY_MAX = "above_entry_max"
    BOOK_FULL = "book_full"
    SECTOR_CAP = "sector_cap"
    COUNTRY_CAP = "country_cap"
    CURRENCY_CAP = "currency_cap"
    TOO_SMALL = "too_small"
    VENUE_NOT_TRADABLE = "venue_not_tradable"
    NO_CASH = "no_cash"
    WORKING = "working"
    ENTRIES_BLOCKED = "entries_blocked"
    NOT_PRICED = "not_priced"
    NO_OPTIONS = "no_options"
    OPTION_GATES = "option_gates"
    OPTION_BUDGET = "option_budget"
    OPTION_FRICTION = "option_friction"
    HELD_BACK = "held_back"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing the gate found: its code, the words for the owner, and whether it is final."""

    code: Reason
    reason: str
    permanent: bool = False


def describe(findings: Sequence[Finding]) -> str:
    return "; ".join(f"{finding.code.value}: {finding.reason}" for finding in findings)


# ── the draft on its own words ──


def screen(draft: ThesisDraft, *, today: date) -> list[Finding]:
    """What a draft's own words rule out. Every finding here is final: the draft is not stored."""
    found: list[Finding] = []

    def fail(code: Reason, reason: str) -> None:
        found.append(Finding(code, reason, permanent=True))

    if (draft.symbol is None) == (draft.candidate is None):
        fail(Reason.INCOMPLETE, "name one instrument: a universe symbol or a stock candidate")
    if draft.candidate is not None and draft.isin is None:
        fail(
            Reason.INCOMPLETE,
            "a share outside the universe file needs its ISIN: the tax lots are kept by it",
        )
    if draft.expectation is None or not draft.expectation.strip():
        fail(Reason.INCOMPLETE, "it does not say what is expected and why")
    for name, value in (("target", draft.target), ("invalidation", draft.invalidation)):
        if value is None:
            fail(Reason.INCOMPLETE, f"it has no {name}: an idea without one is not traded")
        elif not value.is_finite() or value <= ZERO:
            fail(Reason.LEVELS, f"its {name} {value} is not a price")
    if draft.horizon is None:
        fail(Reason.INCOMPLETE, "it has no horizon: an idea without one is not traded")
    if draft.conviction is not Conviction.NORMAL and draft.source is not ThesisSource.OWNER:
        fail(Reason.CONVICTION, "only the owner marks a conviction above normal")
    if found:
        return found
    target, invalidation, horizon = draft.target, draft.invalidation, draft.horizon
    assert target is not None
    assert invalidation is not None
    assert horizon is not None
    if target == invalidation:
        fail(Reason.LEVELS, f"its target and its invalidation are both {target}")
    elif target < invalidation:
        if draft.expression is Expression.SHARES:
            fail(
                Reason.SHORT,
                f"its target {target} is below its invalidation {invalidation}: that describes "
                "a fall, and the book never shorts. A fall is held as a long put (--as option) "
                "or the view as a defensive instrument",
            )
        elif draft.view is not View.BEARISH:
            fail(
                Reason.LEVELS,
                f"its target {target} is below its invalidation {invalidation}: a fall is a "
                "bearish idea, and this one is not marked bearish",
            )
        elif draft.entry_max is not None:
            fail(Reason.LEVELS, "a fall held as a long put names no entry limit")
    elif draft.entry_max is not None and not invalidation < draft.entry_max < target:
        fail(
            Reason.LEVELS,
            f"its entry limit {draft.entry_max} is not between its invalidation {invalidation} "
            f"and its target {target}",
        )
    if horizon <= today:
        fail(Reason.DATES, f"its horizon {horizon.isoformat()} is not in the future")
    if draft.enter_by is not None and not today <= draft.enter_by <= horizon:
        fail(
            Reason.DATES,
            f"its last entry day {draft.enter_by.isoformat()} is not between today and its horizon",
        )
    return found


def view_findings(
    view: View, instrument: ThesisInstrument, *, falls: bool = False
) -> list[Finding]:
    """A bearish idea held long is held as what holds or gains in a fall, never as anything
    else. One whose levels describe the instrument's own fall (``falls``) is a long put on it."""
    if view is View.BEARISH and not falls and not instrument.defensive:
        return [
            Finding(
                Reason.BEARISH_NOT_DEFENSIVE,
                f"a bearish idea is held only as an instrument of a defensive core role; "
                f"{instrument.symbol} fills none (the book never shorts and holds no inverse "
                "product; its own fall is written as a target below the invalidation and held "
                "as a long put)",
                permanent=True,
            )
        ]
    return []


# ── the thesis against the market and the book ──


@dataclass(frozen=True, slots=True)
class GateFacts:
    """What the decision layer read for one thesis, this cycle.

    ``capital`` is the book's capital in the base currency. ``prohibited`` and ``ineligible`` are
    the mandate's and the jurisdiction policy's words (empty and ``None`` when they pass).
    ``held_elsewhere`` says who else holds or works the conid. ``reference`` is a fresh quote's
    reference in the instrument's currency and ``rate`` that currency in the base (``None``: not
    read). ``daily_value`` is the average daily traded value over ``sessions`` stored bars.
    ``slots`` are the book's positions and approved ideas, this thesis left out. ``minimum`` is
    the smallest trade worth making, in the base currency.
    """

    capital: Decimal
    base_currency: str
    prohibited: tuple[str, ...]
    ineligible: str | None
    held_elsewhere: str | None
    reference: Decimal | None
    rate: Decimal | None
    daily_value: Decimal | None
    sessions: int
    slots: tuple[Slot, ...]
    minimum: Decimal
    #: The conviction an unapproved thesis is sized at when it is not its own (``normal`` while
    #: H13 holds its level back); ``None``: its own.
    sized_as: Conviction | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    """The gate's answer: the findings, and when there is none the size (base currency) and the
    whole units it buys at the reference."""

    findings: tuple[Finding, ...]
    size_base: Decimal | None = None
    quantity: Decimal | None = None

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def permanent(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.permanent)


def gain_to_loss(thesis: Thesis, price: Decimal) -> Decimal | None:
    """The target's gain over the invalidation's loss at ``price`` of the thesis's instrument
    (a fall's levels read the other way round); ``None`` outside the levels."""
    low, high = sorted((thesis.invalidation, thesis.target))
    if not low < price < high:
        return None
    return abs(thesis.target - price) / abs(price - thesis.invalidation)


def _cap(
    found: list[Finding],
    code: Reason,
    what: str,
    key: str | None,
    used: Decimal,
    size: Decimal,
    limit: Decimal,
) -> None:
    if key is not None and used + size > limit:
        found.append(
            Finding(
                code,
                f"{what} {key} would hold {used + size:.2f} of the book, above its cap {limit:.2f}",
            )
        )


class ThesisGate:
    """Judges a stored thesis on what the decision layer read, by ``thesis`` configuration."""

    def __init__(self, thesis: ThesisBookConfig) -> None:
        self._thesis = thesis

    def market(self, thesis: Thesis, facts: GateFacts) -> list[Finding]:
        """What does not depend on how the thesis is held: the mandate's and the jurisdiction's
        words, who else holds the instrument, and its levels against the price of its
        instrument (an option's underlying)."""
        found: list[Finding] = []
        symbol = thesis.instrument.symbol
        sign = -1 if thesis.falls else 1
        beyond, short_of = ("below", "above") if thesis.falls else ("above", "below")
        found += [
            Finding(Reason.PROHIBITED_PRODUCT, why, permanent=True) for why in facts.prohibited
        ]
        if facts.ineligible is not None:
            found.append(Finding(Reason.INELIGIBLE, facts.ineligible, permanent=True))
        if facts.held_elsewhere is not None:
            found.append(Finding(Reason.HELD_ELSEWHERE, facts.held_elsewhere, permanent=True))
        price = facts.reference
        if price is None:
            found.append(Finding(Reason.NO_QUOTE, f"{symbol} has no fresh quote"))
        elif sign * (price - thesis.target) >= 0:
            found.append(
                Finding(
                    Reason.TARGET_PASSED,
                    f"{symbol} at {price} is at or {beyond} the target {thesis.target}",
                    permanent=True,
                )
            )
        elif sign * (price - thesis.invalidation) <= 0:
            found.append(
                Finding(
                    Reason.INVALIDATED,
                    f"{symbol} at {price} is at or {short_of} the invalidation "
                    f"{thesis.invalidation}",
                    permanent=True,
                )
            )
        else:
            ratio = gain_to_loss(thesis, price)
            assert ratio is not None  # the price is inside the levels
            if ratio < self._thesis.min_gain_to_loss:
                found.append(
                    Finding(
                        Reason.GAIN_TO_LOSS,
                        f"at {price} the target's gain is {ratio:.2f} times the invalidation's "
                        f"loss, below {self._thesis.min_gain_to_loss}",
                    )
                )
            if thesis.entry_max is not None and price > thesis.entry_max:
                found.append(
                    Finding(
                        Reason.ABOVE_ENTRY_MAX,
                        f"{symbol} at {price} is above its entry limit {thesis.entry_max}",
                    )
                )
        return found

    def slot(self, thesis: Thesis, size: Decimal, facts: GateFacts) -> list[Finding]:
        """A free slot and the caps for a position of ``size``, whatever it is held as."""
        found: list[Finding] = []
        self._book(found, thesis.instrument, size, facts)
        return found

    def evaluate(self, thesis: Thesis, facts: GateFacts) -> Verdict:
        """Judge ``thesis``, held as its instrument, on ``facts``. An approved thesis keeps its
        slot and its size: only what moves with the market is asked again."""
        found = self.market(thesis, facts)
        instrument = thesis.instrument
        symbol = instrument.symbol
        price = facts.reference
        size = thesis.size_base
        if size is None:
            if facts.capital <= ZERO:
                found.append(Finding(Reason.NO_CAPITAL, "the book has no capital in this mode"))
                return Verdict(tuple(found))
            size = position_size(facts.capital, facts.sized_as or thesis.conviction, self._thesis)
            self._book(found, instrument, size, facts)
        if facts.rate is None:
            found.append(Finding(Reason.NO_QUOTE, f"{instrument.currency} has no rate to the base"))
            return Verdict(tuple(found))
        self._liquidity(found, thesis, size / facts.rate, facts)
        if found or price is None:
            return Verdict(tuple(found))
        quantity = (size / (price * facts.rate)).to_integral_value(rounding=ROUND_FLOOR)
        if quantity < ONE or quantity * price * facts.rate < facts.minimum:
            found.append(
                Finding(
                    Reason.TOO_SMALL,
                    f"its size {size:.2f} buys {quantity} of {symbol} at {price}: less than one "
                    f"unit or below the minimum trade {facts.minimum:.2f}",
                )
            )
            return Verdict(tuple(found))
        return Verdict((), size, quantity)

    def _book(
        self, found: list[Finding], instrument: ThesisInstrument, size: Decimal, facts: GateFacts
    ) -> None:
        """A free slot, and the caps that keep the book from being one bet."""
        slots = facts.slots
        if len(slots) >= self._thesis.max_positions:
            found.append(
                Finding(
                    Reason.BOOK_FULL, f"the book holds its {self._thesis.max_positions} positions"
                )
            )

        def used(of: str, key: str | None) -> Decimal:
            return sum((slot.size_base for slot in slots if getattr(slot, of) == key), ZERO)

        capital = facts.capital
        _cap(
            found,
            Reason.SECTOR_CAP,
            "the sector",
            instrument.sector,
            used("sector", instrument.sector),
            size,
            self._thesis.caps.sector * capital,
        )
        _cap(
            found,
            Reason.COUNTRY_CAP,
            "the country",
            instrument.country,
            used("country", instrument.country),
            size,
            self._thesis.caps.country * capital,
        )
        foreign = None if instrument.currency == facts.base_currency else instrument.currency
        _cap(
            found,
            Reason.CURRENCY_CAP,
            "the currency",
            foreign,
            used("currency", foreign),
            size,
            self._thesis.caps.foreign_currency * capital,
        )

    def _liquidity(
        self, found: list[Finding], thesis: Thesis, value: Decimal, facts: GateFacts
    ) -> None:
        """The liquidity floor: the position is a small share of the average daily traded value."""
        sessions = self._thesis.liquidity.sessions
        share = self._thesis.liquidity.max_share_of_daily_value
        symbol = thesis.instrument.symbol
        if facts.daily_value is None or facts.sessions < sessions:
            found.append(
                Finding(
                    Reason.LIQUIDITY_UNMEASURED,
                    f"{symbol} has {facts.sessions} of the {sessions} sessions of traded "
                    "value its liquidity is measured on",
                )
            )
        elif value > share * facts.daily_value:
            found.append(
                Finding(
                    Reason.ILLIQUID,
                    f"a position of {value:.2f} is above {share:.2%} of "
                    f"{symbol}'s average daily traded value {facts.daily_value:.2f}",
                    permanent=thesis.size_base is None,
                )
            )
