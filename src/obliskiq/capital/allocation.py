"""The capital allocation rule on the live account (DECISIONS D65, D149, D155, D156; MASTER_PLAN
§5.19; research/preregistrations/scoreboard.md "Capital").

Shares are of investable capital. On the live account each layer's share is its latest
``allocation_events`` row, which only the scoreboard's monthly run writes, by these rules and with
every change audited; no step needs an owner command:

* **The stock book** takes its first share on a sufficient outcome and its activation condition,
  then steps up at its looks when the pooled re-test is still sufficient, no gap breach is
  outstanding and the live-account minimums of months and closed positions are met; never above
  investable capital less the cash floor. Paper months and the twin count for nothing.
* **Any other layer** with a sufficient outcome takes a share of its sleeve's budget and steps the
  same way, to the sleeve's budget at most.
* **The thesis book** takes the owner's exploration share while its paper record is not below
  H13's boundary, steps by H13's looks on its live record, and goes back to shadow below one.
* **A twin breach** sends a layer back to shadow, and its share to zero.
* **The reserve** (the core) holds whatever the layers do not, less the cash floor.

The rule's numbers are the pre-registrations', handed in as :class:`StepRule`: this module is read
by the live decision path, which never reads research (DECISIONS D64).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..config.schema import ObliskConfig
from ..domain.orders import ExecutionMode, Sleeve
from ..store import scoreboard as repo
from ..store.db import Store
from ..store.worker import StoreWorker
from .budgets import Allocation, SleeveBudgets, book_allocation

ZERO = Decimal(0)
ONE = Decimal(1)

#: The layers whose shares the rule sets by name; any other is a registry entry's id.
STOCK_BOOK: Final = "stock_book"
THESIS_BOOK: Final = "thesis_book"
#: Between a registry layer's sleeve and its id in its allocation key.
LAYER_SEPARATOR: Final = ":"
#: The actor of every change the rule makes.
ALLOCATION_ACTOR: Final = "allocation rule"

_LIVE_MODES = frozenset({ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE})


@dataclass(frozen=True, slots=True)
class StepRule:
    """Where a layer's share starts, how it steps and what a step needs (the layer's
    pre-registration's, else the scoreboard's)."""

    first: Decimal
    step: Decimal
    ceiling: Decimal
    every_months: int
    min_months_first: int
    min_months_between: int
    min_closed_first: int
    min_closed_between: int


@dataclass(frozen=True, slots=True)
class StepEvidence:
    """What the rule reads at a look. ``retest_sufficient`` is ``None`` when the pooled re-test
    could not be run (it then counts as not sufficient: an untested gate never passes)."""

    sufficient: bool
    activation: bool
    twin_breached: bool
    gap_breach_outstanding: bool
    live_months: int
    months_since_step: int
    closed: int
    closed_since_step: int
    steps_taken: int
    retest_sufficient: bool | None


@dataclass(frozen=True, slots=True)
class ShareDecision:
    """The share a look leaves a layer with, and why."""

    share: Decimal
    changed: bool
    reason: str


def step_share(current: Decimal, rule: StepRule, evidence: StepEvidence) -> ShareDecision:
    """The share of a layer whose capital follows a sufficient test: its first share, a step at a
    look, a return to zero on a twin breach, or no change."""
    if evidence.twin_breached:
        return ShareDecision(
            ZERO, current != ZERO, "its twin breached its boundary: back to shadow"
        )
    if not evidence.sufficient or not evidence.activation:
        missing = "a sufficient outcome" if not evidence.sufficient else "its activation condition"
        return ShareDecision(ZERO, current != ZERO, f"no share without {missing}")
    if current == ZERO:
        return ShareDecision(
            rule.first, True, "a sufficient outcome and its activation condition: its first share"
        )
    first = evidence.steps_taken == 0
    months_needed = rule.min_months_first if first else rule.min_months_between
    closed_needed = rule.min_closed_first if first else rule.min_closed_between
    closed_seen = evidence.closed if first else evidence.closed_since_step
    blockers: list[str] = []
    if current >= rule.ceiling:
        blockers.append("it is at its ceiling")
    if evidence.months_since_step < rule.every_months:
        blockers.append(
            f"{evidence.months_since_step} live months since its last change, a look every "
            f"{rule.every_months}"
        )
    months_seen = evidence.live_months if first else evidence.months_since_step
    if months_seen < months_needed:
        blockers.append(f"fewer than {months_needed} live-account months")
    if closed_seen < closed_needed:
        blockers.append(f"{closed_seen} closed positions, {closed_needed} needed")
    if evidence.gap_breach_outstanding:
        blockers.append("a gap breach is outstanding")
    if evidence.retest_sufficient is not True:
        blockers.append(
            "the pooled re-test could not be run"
            if evidence.retest_sufficient is None
            else "the pooled re-test is no longer sufficient"
        )
    if blockers:
        return ShareDecision(current, False, "no step: " + "; ".join(blockers))
    return ShareDecision(
        min(rule.ceiling, current + rule.step), True, "a step at its look: every condition holds"
    )


# ── the allocation the live decision path reads ──


@dataclass(frozen=True, slots=True)
class LiveShares:
    """Each layer's share of investable capital on the live account, by sleeve."""

    satellite: Decimal
    options: Decimal
    thesis: Decimal

    @classmethod
    def none(cls) -> LiveShares:
        return cls(ZERO, ZERO, ZERO)


def layer_key(sleeve: Sleeve, entry: str) -> str:
    """The allocation key of a registry entry's layer: its sleeve, then its id."""
    return f"{sleeve.value}{LAYER_SEPARATOR}{entry}"


def _sleeve_of(layer: str) -> Sleeve | None:
    if layer in (STOCK_BOOK, THESIS_BOOK):
        return Sleeve.SATELLITE
    prefix, separator, _ = layer.partition(LAYER_SEPARATOR)
    if not separator:
        return None
    try:
        return Sleeve(prefix)
    except ValueError:
        return None


def live_shares(shares: Mapping[str, Decimal]) -> LiveShares:
    """The recorded shares summed by sleeve: the stock book and the thesis book are the
    satellite's; a registry entry's layer counts toward the sleeve its key names. A key naming
    no sleeve counts toward none, so its share stays in the reserve."""
    satellite = options = ZERO
    for layer, share in shares.items():
        sleeve = _sleeve_of(layer)
        if sleeve is Sleeve.SATELLITE:
            satellite += share
        elif sleeve is Sleeve.OPTIONS:
            options += share
    return LiveShares(satellite, options, shares.get(THESIS_BOOK, ZERO))


def recorded_shares(store: Store) -> dict[str, Decimal]:
    """Each layer's latest recorded share (the store's ``allocation_events``)."""
    return repo.current_shares(store)


def live_allocation(settings: ObliskConfig, investable: Decimal, shares: LiveShares) -> Allocation:
    """What each sleeve may use of ``investable`` on the live account: the layers' recorded
    shares, within the satellite's ceiling and what the cash floor leaves; the reserve holds the
    rest."""
    budgets = settings.capital.budgets
    room = ONE - budgets.cash_floor
    satellite = min(shares.satellite, settings.capital.satellite_ceiling, room)
    options = min(shares.options, budgets.option_premium, room - satellite)
    core = room - satellite - options
    return Allocation(
        investable=investable,
        core=investable * core,
        satellite=investable * satellite,
        options=investable * options,
        cash_floor=investable * budgets.cash_floor,
        thesis_book=investable * min(shares.thesis, satellite),
    )


async def allocation_now(
    worker: StoreWorker, settings: ObliskConfig, investable: Decimal, mode: ExecutionMode
) -> Allocation:
    """The allocation in ``mode``: shadow and paper keep the configured book share (D159); the
    live account takes the rule's recorded shares (none recorded: the reserve holds it all)."""
    if mode not in _LIVE_MODES:
        return book_allocation(settings, investable, mode)
    shares = await worker.run(recorded_shares)
    return live_allocation(settings, investable, live_shares(shares))


def sleeve_budget(settings: ObliskConfig, sleeve: Sleeve) -> Decimal:
    """A sleeve's configured budget, the ceiling of a layer in it."""
    return SleeveBudgets(settings.capital).share(sleeve)
