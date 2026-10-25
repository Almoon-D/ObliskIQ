"""StrategyValue: what selling a position costs the strategy (MASTER_PLAN §5.15).

The reservation planner asks each sleeve, through this interface, what it knows of a position:
whether the strategy would exit it anyway, whether it is protected (inside its entry window, a
probe awaiting confirmation, a thesis with a pending catalyst) and until when, and its expected
return and its target as a share of its sleeve's budget. The core implements it
(``capital/core_value.py``); Sessions 15 and 17 implement it for the satellite and the decision
layer.

**An expected return counts only from a layer whose research outcome is sufficient**
(MASTER_PLAN §5.9): an estimate from a layer that is insufficient, inconclusive or not evaluable is
ignored, and the default ranking is the rule — the cash line and the part of a core role above its
new target cost least, an ordinary holding more, a protected one most.

:class:`DefaultStrategyValue` is that default for the sleeves that do not fill the interface in yet:
no exit signal, no expected return and no target, and a satellite or options position protected for
``capital.planner.entry_window_days`` after it was opened. Probes and catalysts are the
satellite's (Session 15); without them nothing is protected for those reasons.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from ..config.schema_runtime import CapitalConfig
from ..domain.orders import Sleeve

#: The four outcomes of a pre-registered test (MASTER_PLAN §5.9); only ``sufficient`` counts.
type Evidence = Literal["sufficient", "insufficient", "inconclusive", "not_evaluable"]
SUFFICIENT: Evidence = "sufficient"


class Protection(StrEnum):
    """Why a position should not be cut."""

    ENTRY_WINDOW = "entry_window"
    PROBE = "probe"
    PENDING_CATALYST = "pending_catalyst"


@dataclass(frozen=True, slots=True)
class ExpectedReturn:
    """A layer's expected annual return for a position, with the outcome of the test behind it."""

    annual: Decimal
    evidence: Evidence
    layer: str

    @property
    def usable(self) -> bool:
        return self.evidence == SUFFICIENT


@dataclass(frozen=True, slots=True)
class PositionFacts:
    """What the planner knows of a held position."""

    conid: int
    sleeve: Sleeve
    units: Decimal
    opened_on: date | None


@dataclass(frozen=True, slots=True)
class Protected:
    """A protection and the day it ends (``None``: unknown)."""

    why: Protection
    until: date | None


class StrategyValue(Protocol):
    """What a sleeve knows of its positions, for the reservation planner."""

    def would_exit(self, position: PositionFacts, *, today: date) -> bool:
        """Whether the strategy would sell the position anyway."""
        ...

    def protection(self, position: PositionFacts, *, today: date) -> Protected | None:
        """Why the position should not be cut, if it should not."""
        ...

    def expected_return(self, position: PositionFacts, *, today: date) -> ExpectedReturn | None:
        """The position's expected annual return, if a layer gives one."""
        ...

    def target_weight(self, position: PositionFacts, *, today: date) -> Decimal | None:
        """The position's target as a share of its sleeve's budget, if the sleeve states one."""
        ...


class DefaultStrategyValue:
    """The default ranking: entry windows protect theses; nothing else is known."""

    def __init__(self, capital: CapitalConfig) -> None:
        self._window = timedelta(days=capital.planner.entry_window_days)
        self._sleeves = frozenset(Sleeve(name) for name in capital.planner.protected_sleeves)

    def would_exit(self, position: PositionFacts, *, today: date) -> bool:
        del position, today  # no sleeve gives an exit signal yet (Sessions 13, 15, 17)
        return False

    def protection(self, position: PositionFacts, *, today: date) -> Protected | None:
        opened = position.opened_on
        if position.sleeve not in self._sleeves or opened is None:
            return None
        until = opened + self._window
        if today >= until:
            return None
        return Protected(Protection.ENTRY_WINDOW, until)

    def expected_return(self, position: PositionFacts, *, today: date) -> ExpectedReturn | None:
        del position, today  # no layer's outcome is sufficient yet (DECISIONS D87, D95)
        return None

    def target_weight(self, position: PositionFacts, *, today: date) -> Decimal | None:
        del position, today  # a sleeve states its targets through its own StrategyValue
        return None
