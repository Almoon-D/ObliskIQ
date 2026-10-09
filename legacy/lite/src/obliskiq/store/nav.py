"""``nav_history``: the account's net asset value over time, and its one writer.

**Why this lives in ``store/`` and not beside its readers.** The upsert was a
method on ``compute.risk_engine.RiskEngine``, which is the wrong side of a
boundary the deployment depends on: the compute unit runs with
``PrivateNetwork=true`` and the collectors are the only thing that talks to a
broker, so the layer that can READ a NAV from the account could not reach the
code that writes one without importing the compute layer wholesale. Two
implementations of the same upsert would then have drifted, which is exactly what
the one-owner-per-fact rule exists to prevent. ``RiskEngine.record_nav``
delegates here and stays the name its callers know.

The reader is ``compute.frames.latest_nav_observation`` and there is deliberately
only one of those too.

**``as_of`` is the date the reading was TAKEN.** Never the date it is about. The
rolling-month drawdown breaker reads this table as a series, so back-dating a
figure read today onto an earlier session puts a number the account never had
into the window the breaker measures.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date

from ..util.timeutil import iso_date, utc_stamp
from .db import Store

#: The default provenance, and the value every row written before the collectors
#: could supply one carries. A collector passes its own route instead — ``cp_api``
#: or ``mcp`` — under the standing rule that every row records where it came from.
SOURCE_UNSPECIFIED = "ibkr"


def record_nav(store: Store, *, as_of: date, nav: float, source: str = SOURCE_UNSPECIFIED) -> None:
    """Append a NAV observation, keyed on the day it was read.

    An upsert rather than an insert: re-reading on a day already recorded
    corrects that day rather than failing, and cannot create two observations one
    of which the breaker would silently prefer.
    """
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO nav_history (as_of, nav_base, source, recorded_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (as_of) DO UPDATE SET "
            "nav_base = excluded.nav_base, source = excluded.source, "
            "recorded_at = excluded.recorded_at",
            (iso_date(as_of), nav, source, utc_stamp()),
        )
