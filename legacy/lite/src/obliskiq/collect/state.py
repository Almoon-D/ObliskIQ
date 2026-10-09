"""Small facts a collector learns at runtime and should not re-learn every run.

The probed daily-bar per-call cap is the reason this exists.
``ibkr.cp_api.history.max_bars_per_request`` is documented and unverified, so the
backfill measures the real figure instead of trusting it — and measuring it on
every run would waste a request a day forever.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from ..logging_setup import get_logger
from ..store import Store
from ..util.timeutil import parse_stamp, utc_now, utc_stamp

log = get_logger("collect.state")

#: The probed daily-bar cap, as ``{"bars": int, "chunk_days": int, "probed_at": str}``.
KEY_DAILY_BAR_CAP = "backfill.daily_bar_cap"

#: An upstream that has told us to stop, as ``{"until": iso8601, "reason": str}``.
#: One key per upstream, built by :func:`cooldown_key`.
_COOLDOWN_PREFIX = "cooldown"

#: The upstream whose block this exists for: marketdata.app permits one IP per
#: account and answers a changed address with HTTP 403 and a five-minute wait.
UPSTREAM_MARKETDATA_APP = "marketdata_app"

#: When a collector run last completed the OAuth 1.0a handshake, as
#: ``{"authenticated_at": iso8601}``. **Timestamps only — no token material ever
#: goes in here.** The live session token lives in memory in the collector
#: process and is never persisted; what the store records is that a handshake
#: SUCCEEDED and when, which is the fact an operator and ``get_system_health``
#: actually need. Collectors are ``Type=oneshot``, so each run mints a fresh
#: token: this measures the last successful handshake rather than a live session,
#: and the reader has to say so.
KEY_CP_HANDSHAKE = "ibkr.cp_api.last_handshake"


def cooldown_key(upstream: str) -> str:
    return f"{_COOLDOWN_PREFIX}.{upstream}"


def set_cooldown(store: Store, upstream: str, *, seconds: float, reason: str) -> str:
    """Record that ``upstream`` should not be called again until later. Returns the time.

    A cooldown has to outlive the process that learned about it. The collectors
    are ``Type=oneshot``: each run is a new process that shares nothing with the
    last one but this store, so an in-memory breaker would never see two runs and
    every target in every run would rediscover the same block independently.
    """
    stamp = utc_stamp(utc_now() + timedelta(seconds=seconds))
    set_state(store, cooldown_key(upstream), {"until": stamp, "reason": reason})
    log.warning(
        "upstream cooled down; later runs will skip it until it lapses",
        extra={"upstream": upstream, "until": stamp, "reason": reason},
    )
    return stamp


def active_cooldown(store: Store, upstream: str, *, now: datetime | None = None) -> dict | None:
    """The cooldown still in force, or ``None``.

    A lapsed or unparseable record reads as absent: the failure mode to avoid is
    a stuck cooldown that silences a working route forever, and the block it
    guards against announces itself again immediately if it is still there.
    """
    held = get_state(store, cooldown_key(upstream))
    if not isinstance(held, dict):
        return None
    until = parse_stamp(str(held.get("until")))
    if until is None:
        log.warning("cooldown holds an unreadable time; ignoring it", extra={"upstream": upstream})
        return None
    return held if until > (now or utc_now()) else None


def get_state(store: Store, key: str) -> Any | None:
    """The stored value, or ``None``. Unparseable JSON is treated as absent."""
    row = store.execute("SELECT value FROM collector_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        log.warning("collector_state holds unparseable JSON; ignoring it", extra={"key": key})
        return None


def set_state(store: Store, key: str, value: Any) -> None:
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value, default=str), utc_stamp()),
        )


def record_cp_handshake(store: Store, *, now: datetime | None = None) -> str:
    """Note that the Client Portal handshake just succeeded. Returns the stamp.

    Called by the collector CLI once the OAuth 1.0a exchange and the brokerage
    session are up. It is deliberately NOT called from ``ibkr/session.py``: that
    layer holds credentials and takes no ``Store``, and giving it one would put
    the secret-handling code one import away from the database.
    """
    stamp = utc_stamp(now or utc_now())
    set_state(store, KEY_CP_HANDSHAKE, {"authenticated_at": stamp})
    return stamp


def last_cp_handshake(store: Store) -> datetime | None:
    """When a run last authenticated against Client Portal, or ``None``.

    ``None`` means no run has ever recorded one — on a store the collectors have
    filled, that is itself the finding, and it must never read as "recent".
    """
    held = get_state(store, KEY_CP_HANDSHAKE)
    if not isinstance(held, dict):
        return None
    return parse_stamp(str(held.get("authenticated_at")))
