"""Health: the heartbeat file and the optional ping.

Each heartbeat rewrites ``paths.heartbeat_file`` in the data directory (atomically: a reader never
sees half a file) with when it ran and what it saw, so a supervisor, ``obliskiq status`` or the
owner can tell a live service from a hung one by its age. When ``OBLISK_HEALTH_PING_URL`` is set,
the heartbeat then fetches it (a dead man's switch such as a healthchecks.io check): a service that
stops pinging is noticed by the outside even when everything here, Telegram included, is down.
A ping that fails is logged, never raised; its URL is a secret and is never logged.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from ..logging_setup import get_logger
from ..util.timeutil import parse_stamp, utc_stamp

log = get_logger("orchestration.health")

HTTP_OK_FIRST = 200
HTTP_OK_LAST = 299


def write_heartbeat(path: Path, at: datetime, facts: dict[str, Any]) -> None:
    """Rewrite the heartbeat file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {"at": utc_stamp(at), **facts}
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def read_heartbeat(path: Path) -> tuple[datetime | None, dict[str, Any]]:
    """When the last heartbeat ran and what it wrote; ``(None, {})`` when there is none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    at = parse_stamp(str(data.get("at", "")))
    return at, data


async def ping(url: SecretStr, *, timeout: float, client: httpx.AsyncClient | None = None) -> bool:
    """Fetch the ping URL; ``True`` when it answered 2xx. Never raises."""
    owned = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        response = await http.get(url.get_secret_value(), timeout=timeout)
    except httpx.HTTPError as exc:
        log.warning("the health ping failed", extra={"error": type(exc).__name__})
        return False
    finally:
        if owned:
            await http.aclose()
    ok = HTTP_OK_FIRST <= response.status_code <= HTTP_OK_LAST
    if not ok:
        log.warning("the health ping was refused", extra={"status": response.status_code})
    return ok
