"""Deposit deployment: new cash follows the targets, optionally in tranches (MASTER_PLAN §5.15).

A deposit raises managed capital at once, so the targets grow with it; how fast its cash may be
spent toward them is ``capital.deployment.tranche_days``: an equal share each day from the day it
counts, all of it by the last. The part not yet released is held back from what the book may
spend. A deposit counts from its own day in mode ``cap`` (the book takes it when the owner records
it) and from the day the broker's cash showed it in mode ``whole_account``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from ..config.schema_runtime import CapitalConfig
from ..domain.capital import CashFlow, Deposit

ZERO = Decimal(0)


def counted_from(deposit: Deposit, capital: CapitalConfig) -> date | None:
    """The day a deposit starts to count toward managed capital; ``None`` while it does not."""
    if not deposit.live:
        return None
    if capital.mode == "cap":
        return deposit.on if deposit.in_book else None
    return None if deposit.matched_at is None else max(deposit.on, deposit.matched_at.date())


def held_back(
    flows: Sequence[CashFlow], capital: CapitalConfig, *, today: date
) -> dict[str, Decimal]:
    """The part of each deposit not yet released for buying, by currency."""
    days = capital.deployment.tranche_days
    held: dict[str, Decimal] = {}
    for flow in flows:
        if not isinstance(flow, Deposit):
            continue
        start = counted_from(flow, capital)
        if start is None or today < start:
            continue
        released = min(days, (today - start).days + 1)
        rest = flow.amount * (days - released) / days
        if rest > ZERO:
            held[flow.currency] = held.get(flow.currency, ZERO) + rest
    return held
