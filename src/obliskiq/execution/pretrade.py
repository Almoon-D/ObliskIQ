"""The pre-trade gate: the only issuer of a :class:`~obliskiq.domain.orders.PretradeClearance`.

No placement or modify reaches the broker without a clearance (I1, SESSION_PROMPTS Session 4
item 4): the order client spends one in the store, bound to the intent, the action, the broker
order and the exact terms, before it sends anything. A guard test refuses any other constructor
of a clearance, and any other caller of ``record_clearance``, under ``src/``.

This build of the gate refuses every intent with ``pretrade_unavailable``: the checks themselves
— mandate, eligibility, holdings, no short (I2), class by effect (I3), cash, caps, quote policy,
calendars, kill switch, mode, approval, the stop resize of an exit (I5) — are Session 4a's, and
until they exist nothing may be cleared.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from ..domain.orders import ClearanceAction, OrderIntent, OrderTerms, PretradeClearance

#: The reason code of every refusal while the checks are not built.
PRETRADE_UNAVAILABLE: Final = "pretrade_unavailable"


@dataclass(frozen=True, slots=True)
class PretradeRefusal:
    """Why an intent was not cleared: a reason code and the words for the owner."""

    intent_id: UUID
    code: str
    reason: str


class PretradeGate:
    """Clears an intent for one write, or refuses it with a reason code."""

    async def clear(
        self,
        intent: OrderIntent,
        *,
        action: ClearanceAction = ClearanceAction.PLACE,
        order_id: str | None = None,
        terms: OrderTerms | None = None,
    ) -> PretradeClearance | PretradeRefusal:
        """A clearance for ``action`` on ``intent`` (``terms`` and ``order_id`` for a modify).

        Refuses every intent until Session 4a builds the checks.
        """
        asked = terms if terms is not None else intent.terms
        what = action.value if order_id is None else f"{action.value} of order {order_id}"
        return PretradeRefusal(
            intent_id=intent.intent_id,
            code=PRETRADE_UNAVAILABLE,
            reason=(
                f"{what} ({asked.side.value} conid {asked.conid}) refused: the pre-trade checks "
                "are not built yet (Session 4a), so nothing is cleared"
            ),
        )
