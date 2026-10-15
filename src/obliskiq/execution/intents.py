"""New intents: an id from ``uuid.uuid7()`` and lite's discipline rules.

An entry buys (a sell that opens would be a short or a written option, which the mandate
forbids). lite's ``discipline`` block, as the order path reads it: entries are limit orders
(``entry_order_type: limit_only`` — a patient limit or an algo with a limit cap, never a
stop-limit), and an exit or a protective stop gives one of ``exit_reasons_allowed``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from datetime import datetime

from ..config.schema import ObliskConfig
from ..domain.orders import (
    ExecutionMode,
    IntentRole,
    OrderIntent,
    OrderSide,
    OrderTerms,
    Sleeve,
)
from ..errors import ExecutionError

#: Roles whose reason must be an allowed exit reason.
_EXIT_ROLES = frozenset({IntentRole.EXIT, IntentRole.PROTECTIVE})


class IntentRefusedError(ExecutionError):
    """An intent that breaks the order discipline is never created."""


def new_intent(
    settings: ObliskConfig,
    *,
    sleeve: Sleeve,
    role: IntentRole,
    terms: OrderTerms,
    reason: str,
    mode: ExecutionMode,
    approval_required: bool,
    created_at: datetime,
    expires_at: datetime,
    thesis_id: str | None = None,
    parent_id: uuid.UUID | None = None,
    protective_id: uuid.UUID | None = None,
) -> OrderIntent:
    """A proposed intent with a fresh uuid7, refused when it breaks lite's discipline."""
    discipline = settings.discipline
    if role is IntentRole.ENTRY and terms.side is OrderSide.SELL:
        raise IntentRefusedError("an entry buys: a sell would open a short or write an option")
    if (
        role is IntentRole.ENTRY
        and discipline.entry_order_type == "limit_only"
        and terms.price.kind == "stop_limit"
    ):
        raise IntentRefusedError("entries are limit orders (discipline.entry_order_type)")
    if role in _EXIT_ROLES and reason not in discipline.exit_reasons_allowed:
        raise IntentRefusedError(
            f"{reason!r} is not an allowed exit reason (discipline.exit_reasons_allowed: "
            f"{', '.join(discipline.exit_reasons_allowed)})"
        )
    return OrderIntent(
        intent_id=uuid.uuid7(),
        sleeve=sleeve,
        thesis_id=thesis_id,
        role=role,
        terms=terms,
        reason=reason,
        mode=mode,
        approval_required=approval_required,
        created_at=created_at,
        expires_at=expires_at,
        parent_id=parent_id,
        protective_id=protective_id,
    )
