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
#: The sleeve's own exits of an option a thesis is held as (MASTER_PLAN §5.6).
ZOMBIE: Final = "zombie"
MANDATORY_CLOSE: Final = "mandatory_close"
SCALED_EXIT: Final = "scaled_exit"
#: The exits that are the thesis's own: its twin in the underlying leaves by these alone.
THESIS_EXITS: Final = frozenset({TARGET, INVALIDATION, TIME_WINDOW})


def exit_due(thesis: Thesis, reference: Decimal | None, today: date) -> str | None:
    """The exit ``thesis`` has reached, or ``None``. ``reference`` is the price of the thesis's
    instrument (an option's underlying); levels that describe a fall are read the other way
    round. Without a price only the horizon can end it: a level is never judged on a quote that
    was not read."""
    if reference is not None:
        sign = -1 if thesis.falls else 1
        if sign * (reference - thesis.invalidation) <= 0:
            return INVALIDATION
        if sign * (reference - thesis.target) >= 0:
            return TARGET
    if today > thesis.horizon:
        return TIME_WINDOW
    return None
