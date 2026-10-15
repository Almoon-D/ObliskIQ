"""One interface over the two ways of reaching the Client Portal Web API.

``oauth`` signs every request itself and talks to ``api.ibkr.com``; ``gateway`` talks to IBKR's
Client Portal Gateway on this machine, which holds the logged-in session and needs no signature.
Everything above (:mod:`obliskiq.ibkr.session`, the client) is the same for both.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ssl
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol


class TransportKind(StrEnum):
    OAUTH = "oauth"
    GATEWAY = "gateway"


class Transport(Protocol):
    """How requests reach IBKR and how a lost login is repaired."""

    @property
    def kind(self) -> TransportKind: ...

    @property
    def base_url(self) -> str:
        """The API root every endpoint path is appended to (origin plus ``ibkr.base_path``)."""
        ...

    def verify(self) -> ssl.SSLContext | bool:
        """TLS verification for the session's HTTP client. Never ``False``."""
        ...

    async def prepare(self) -> None:
        """Make the transport usable: a verified live session token, or a running, logged-in
        Gateway. Raises when it cannot."""
        ...

    async def refresh(self) -> None:
        """Ready the next request before it takes a pacing slot (OAuth renegotiates a token
        inside its refresh margin; the Gateway needs nothing). Raises when it cannot."""
        ...

    async def headers(self, method: str, url: str, params: Mapping[str, Any]) -> dict[str, str]:
        """Headers for one request (OAuth signs it; the Gateway needs none)."""
        ...

    async def recover(self) -> None:
        """Log in again after the session was lost (renegotiate, or re-run the Gateway login)."""
        ...

    async def aclose(self) -> None:
        """Release what the transport started (a Gateway process it launched)."""
        ...
