"""Bearer-token authentication for the HTTP transport.

Ported from ObliskIQ-lite-MCP (``server/auth.py`` @b1ebd49). This server exposes the owner's
cockpit: the book, the settings and the commands that change them. It must never be reachable
unauthenticated off the local machine: ``obliskiq cockpit serve`` refuses to listen beyond
loopback without ``OBLISK_MCP_AUTH_TOKEN``, and this module enforces the token on the wire.

Written as pure ASGI middleware rather than a Starlette ``BaseHTTPMiddleware``:
the streamable-HTTP transport streams responses, and ``BaseHTTPMiddleware``
buffers them. It also passes ``lifespan`` straight through, so the session
manager the transport starts in its lifespan is untouched.

The SDK's own auth is an OAuth resource-server flow with issuer metadata and
token introspection. That is the wrong shape for a single operator running one
container: what it needs is a static bearer token, compared in
constant time, and that is what this is.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hmac
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ..logging_setup import get_logger

log = get_logger("server.auth")

#: Protocol constants, not thresholds.
HTTP_UNAUTHORIZED = 401

#: The scheme the Authorization header must carry.
BEARER_SCHEME = "bearer"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class BearerTokenMiddleware:
    """Rejects any HTTP request without the configured bearer token.

    ``exempt_paths`` is for the health endpoint only. Everything else — the MCP
    endpoint above all — needs the token, including requests that would fail for
    other reasons: an unauthenticated caller learns nothing about this server
    beyond that it is alive.
    """

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        token: str,
        exempt_paths: Iterable[str] = (),
    ) -> None:
        if not token:
            raise ValueError(
                "BearerTokenMiddleware was constructed without a token. An empty token "
                "would authenticate every request; refuse to start instead."
            )
        self._app = app
        self._token = token
        self._exempt = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # lifespan and websocket scopes pass through untouched: the transport
            # starts its session manager in the lifespan, and swallowing it here
            # would leave the server accepting requests it cannot serve.
            await self._app(scope, receive, send)
            return

        if scope.get("path") in self._exempt:
            await self._app(scope, receive, send)
            return

        if self._authorised(scope):
            await self._app(scope, receive, send)
            return

        log.warning(
            "rejected an unauthenticated request",
            extra={"path": scope.get("path"), "client": _client(scope)},
        )
        await _unauthorised(send)

    def _authorised(self, scope: Scope) -> bool:
        presented = _bearer_token(scope)
        if presented is None:
            return False
        # Constant time: a short-circuiting comparison leaks the token prefix.
        return hmac.compare_digest(presented, self._token)


def _bearer_token(scope: Scope) -> str | None:
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() != b"authorization":
            continue
        value = raw_value.decode("latin-1").strip()
        scheme, _, credentials = value.partition(" ")
        if scheme.lower() != BEARER_SCHEME:
            return None
        return credentials.strip() or None
    return None


def _client(scope: Scope) -> str | None:
    client = scope.get("client")
    return None if not client else str(client[0])


async def _unauthorised(send: Send) -> None:
    """A spec-shaped challenge. The body names no detail an attacker can use."""
    body = b'{"error":"unauthorized"}'
    await send(
        {
            "type": "http.response.start",
            "status": HTTP_UNAUTHORIZED,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="obliskiq-cockpit"'),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
