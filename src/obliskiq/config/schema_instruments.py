"""The shape of what may be traded: lite's ``instruments`` and ``tenor`` keys the pre-trade gate
reads, and the skeleton eligibility allowlist.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Final, Literal, Self, get_args

from pydantic import Field, PositiveInt, model_validator

from ..domain.base import NonEmptyText
from ..domain.instrument import TradedInstrument
from .base import FrozenModel

type ProhibitedCategory = Literal[
    "futures_contracts", "futures_options", "short_options", "leveraged_etps", "inverse_etps"
]

#: The mandate's prohibited categories (MASTER_PLAN §2): configuration may not drop one.
MANDATE_PROHIBITED: Final = frozenset(get_args(ProhibitedCategory.__value__))


class InstrumentsConfig(FrozenModel):
    """lite's ``instruments`` keys: what is never traded, and how it is recognised."""

    prohibited: list[ProhibitedCategory]
    allow_futures_options: bool
    leveraged_inverse_markers: list[NonEmptyText] = Field(min_length=1)

    @model_validator(mode="after")
    def _the_mandate_holds(self) -> Self:
        missing = sorted(MANDATE_PROHIBITED - set(self.prohibited))
        if missing:
            raise ValueError(
                f"prohibited must keep every category the mandate forbids; missing {missing}"
            )
        if self.allow_futures_options:
            raise ValueError(
                "allow_futures_options stays false: a futures option needs the never-exercise "
                "rule, which the options sleeve (Session 12) builds"
            )
        return self


class TenorConfig(FrozenModel):
    """lite's ``tenor`` key the pre-trade gate reads (Session 12 ports the rest)."""

    mandatory_close_trading_days: PositiveInt


class EligibilityConfig(FrozenModel):
    """The skeleton eligibility policy's allowlist (removed with it in Session 8)."""

    skeleton_allowlist: list[TradedInstrument]

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        conids = [instrument.conid for instrument in self.skeleton_allowlist]
        if len(conids) != len(set(conids)):
            raise ValueError("each conid is listed once")
        return self


class PositionsConfig(FrozenModel):
    """lite's ``positions`` key the currency manager reads (Session 17 restates the rest)."""

    max_single_foreign_currency_pct: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
