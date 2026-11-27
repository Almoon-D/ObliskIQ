"""One request to the SEC: the declared User-Agent, the shared retry policy and the fair-access
pause. Read by the insider, listing and fundamentals collectors and by the replay's loader.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from typing import Any

import httpx

from ..errors import UpstreamError
from ..util.http import RetryPolicy, get_with_retry


async def sec_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    policy: RetryPolicy,
    user_agent: str | None,
    spacing: float,
    sleep: Callable[[float], Any],
) -> httpx.Response:
    """One request to the SEC with the declared User-Agent, then the fair-access pause
    (``insiders.request_spacing_seconds``). Without a User-Agent nothing is asked."""
    if user_agent is None:
        raise UpstreamError("SEC_USER_AGENT is unset; EDGAR refuses a request without one")
    response = await get_with_retry(client, url, policy=policy, headers={"User-Agent": user_agent})
    await sleep(spacing)
    return response
