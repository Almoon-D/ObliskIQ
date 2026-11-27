"""What a survey of the thesis book reads and decides: the shared vocabulary of its rebalancer
(``rebalance_thesis.py``) and of the option a thesis may be held as (``thesis_options.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..capital.managed import CapitalView
from ..capital.planner import FundingPlan
from ..domain.options import OptionPosition, ThesisOption
from ..domain.orders import ExecutionMode, Fill, OrderIntent
from ..domain.thesis import Thesis
from ..engines.options_sleeve.thesis_link import Chosen
from ..engines.options_sleeve.zombie import OptionRisk
from ..engines.thesis.gate import Finding
from ..execution.account import CashRead
from ..store.h13 import Holdbacks
from ..store.orders import IntentFill
from .vehicle_selector import VehicleMarket

ZERO = Decimal(0)

#: Who the book's own log entries are written by.
ACTOR = "thesis book"
#: The reason an entry intent gives.
ENTRY_REASON = "thesis"
#: What the plan does with one thesis. ``fund``: it is approved and the reserve sells for it.
RESOLVE, REFUSE, EXPIRE, WAIT, FUND, ENTER, HOLD, EXIT, CLOSE = (
    "resolve",
    "refuse",
    "expire",
    "wait",
    "fund",
    "enter",
    "hold",
    "exit",
    "close",
)


@dataclass(frozen=True, slots=True)
class ThesisDecision:
    """What the plan does with one thesis this cycle, and why."""

    thesis: Thesis
    action: str
    reason: str
    findings: tuple[Finding, ...] = ()
    quantity: Decimal = ZERO
    size_base: Decimal | None = None
    held: Decimal = ZERO
    fills: tuple[Fill, ...] = ()
    #: An approved thesis whose entry has filled becomes active when the plan is applied.
    opened: bool = False
    #: The traded instrument's currency in the base, when a close was read with one.
    rate: Decimal | None = None
    #: Something to write in the thesis's log when the plan is applied (why an ``auto`` idea is
    #: not held as an option).
    note: str | None = None
    #: A thesis held as an option: the contract (``chosen`` when this cycle chose it), the
    #: sleeve's position, the underlying's price, the session's zombie reading to record and the
    #: loss to book at its detection.
    option: ThesisOption | None = None
    chosen: Chosen | None = None
    position: OptionPosition | None = None
    underlying: Decimal | None = None
    review: OptionRisk | None = None
    detected_loss: Decimal | None = None
    #: The reserve's sales that fund a thesis approved without the cash for it (``fund``).
    funding: FundingPlan | None = None
    #: A candidate whose own checks passed this cycle (the gate's but the slot and the size): it
    #: is written into the idea ledger when the plan is applied, once (H14, D187).
    ledger: bool = False

    @property
    def conid(self) -> int | None:
        """What the decision trades or holds: the option when there is one, else the
        instrument."""
        return self.thesis.instrument.conid if self.option is None else self.option.option_conid

    @property
    def trades(self) -> bool:
        return self.action in (ENTER, EXIT)


@dataclass(frozen=True, slots=True)
class TwinClose:
    """A twin that reached one of its thesis's exits: the underlying's price and the reason."""

    thesis_id: int
    price: Decimal
    reason: str


@dataclass
class ThesisPlan:
    """A survey of the thesis book: its capital, every open thesis's decision, and why nothing
    is entered."""

    mode: ExecutionMode
    at: datetime
    capital: Decimal | None = None
    decisions: list[ThesisDecision] = field(default_factory=list)
    blocked: str | None = None
    entries_blocked: str | None = None
    #: Why new theses are entered below their size (the loss brake), when they are.
    entries_reduced: str | None = None
    #: The twins of option theses that reached one of their thesis's exits this cycle.
    twins: list[TwinClose] = field(default_factory=list)
    #: What the reserve's funding sales still working will bring, not yet counted toward an
    #: approved thesis this survey; and whether this survey already funds one (one a cycle).
    funding_credit: Decimal = ZERO
    funded: bool = False

    @property
    def trades(self) -> list[ThesisDecision]:
        return [decision for decision in self.decisions if decision.trades]

    def render(self) -> str:
        lines = [f"thesis book ({self.mode.value}) at {self.at.isoformat()}"]
        if self.capital is not None:
            lines.append(f"  the book's capital {self.capital:.2f}")
        if self.blocked is not None:
            lines.append(f"  nothing is decided: {self.blocked}")
        if self.entries_blocked is not None:
            lines.append(f"  no entry now: {self.entries_blocked}")
        if self.entries_reduced is not None:
            lines.append(f"  smaller entries now: {self.entries_reduced}")
        if not self.decisions and self.blocked is None:
            lines.append("  no open thesis: the book holds nothing")
        for d in self.decisions:
            t = d.thesis
            trade = f" {d.quantity}" if d.trades else ""
            what = "" if d.option is None else f" as {d.option.symbol}"
            lines.append(
                f"  {t.ref} {t.instrument.symbol}{what} ({t.source.value}, {t.conviction.value}, "
                f"{t.state.value}) held {d.held}: {d.action}{trade} ({d.reason})"
            )
        lines += [
            f"  T{twin.thesis_id}'s twin in the underlying leaves at {twin.price} ({twin.reason})"
            for twin in self.twins
        ]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class BookRead:
    """What one survey read for every thesis."""

    view: CapitalView
    cash: CashRead
    capital: Decimal
    spendable: Decimal
    minimum: Decimal
    fills: Sequence[IntentFill]
    busy: Sequence[OrderIntent]
    market: Mapping[int, VehicleMarket]
    owners: Mapping[int, str]
    today: date
    at: datetime
    #: What H13's looks hold back on the live account (``None`` in shadow and paper), and each
    #: analyst thesis's segment.
    holdbacks: Holdbacks | None = None
    segments: Mapping[int, str] = field(default_factory=dict)
    #: The configured segment each analyst thesis came from (``None`` before segments).
    segment_names: Mapping[int, str | None] = field(default_factory=dict)
    #: The share of its size a new thesis is entered at (the loss brake's, D193).
    entry_size: Decimal = Decimal(1)
