"""A thesis's own exits (MASTER_PLAN §5.5): its target, its invalidation and its horizon's end.

The reasons are lite's exit reasons (``discipline.exit_reasons_allowed``). The protective stop
that rests at the broker at the invalidation is the execution layer's
(``execution/protective.py``); this rule is what the book checks itself each cycle, so a position
also leaves where no stop rests (shadow) or a stop is missing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Final

from ...domain.thesis import Thesis

TARGET: Final = "target"
INVALIDATION: Final = "invalidation"
TIME_WINDOW: Final = "time_window"
#: The reason of a protective stop's own fill (``execution/protective.py``).
STOP: Final = "stop"
#: The exit of a position whose thesis has no claim on it any more.
OVERRIDE: Final = "override"


def exit_due(thesis: Thesis, reference: Decimal | None, today: date) -> str | None:
    """The exit ``thesis`` has reached, or ``None``. Without a price only the horizon can end
    it: a level is never judged on a quote that was not read."""
    if reference is not None and reference <= thesis.invalidation:
        return INVALIDATION
    if reference is not None and reference >= thesis.target:
        return TARGET
    if today > thesis.horizon:
        return TIME_WINDOW
    return None
