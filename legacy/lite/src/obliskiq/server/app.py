"""Assembling the server: the tool surface, the transports, the auth.

Two transports, and the difference matters:

**stdio** is how Claude Code runs a local server. It binds nothing, is reachable
only by the process that spawned it, and therefore needs no token.

**streamable-http** is how a remote client — claude.ai, or Claude Code against a
VM — reaches the same tools. It binds a port, so the bearer token is not
optional off loopback and the process refuses to start without one.

The instructions string below is what a client shows the model before any tool
is called. It carries the two things most likely to be got wrong: call the
sleeve first, and this server holds no live market data.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..logging_setup import get_logger
from .auth import BearerTokenMiddleware
from .context import ServerContext
from .tools import register_all

log = get_logger("server.app")

SERVER_NAME = "obliskiq-lite-mcp"

#: Unauthenticated, and deliberately says nothing but that the process is alive.
HEALTH_PATH = "/healthz"

#: Where the streamable-HTTP transport listens. The MCP default.
MCP_PATH = "/mcp"

INSTRUCTIONS = """\
The computation layer for the obliskiq-lite volatility-dislocation workflow. It \
owns the SQLite store, the grid, the ledgers and the risk maths; the obliskiq-lite \
skill owns the judgment.

Call get_sleeve_status FIRST in any session. The option budget defines the \
universe, and producing a full analysis before computing it is the most repeated \
failure mode in this account.

This server holds NO live market data and makes no network requests. Everything \
it returns was collected by a scheduled run and computed offline, so every \
response carries the date it was measured on. Live quotes, positions and NAV come \
from the IBKR connector and are passed IN as tool arguments.

Ledger writes validate against config.yaml and reject gate violations here, in the \
server. A refusal is the answer, not a fault: quote its reason rather than working \
around it.\
"""


def build_server(context: ServerContext, **settings: Any) -> FastMCP:
    """The FastMCP instance with every tool registered."""
    mcp = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS, **settings)
    register_all(mcp, context)

    @mcp.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        """Liveness only. Reachable without a token, and reveals nothing else."""
        return JSONResponse({"status": "ok"})

    return mcp


def build_http_app(mcp: FastMCP, *, token: str | None) -> Starlette:
    """The streamable-HTTP app, with the bearer check in front of it.

    ``token`` is ``None`` only when binding loopback with none configured, which
    the CLI has already warned about. Everywhere else the caller resolved it
    through ``Settings.require_mcp_auth_token`` and the process would not have
    reached here without one.
    """
    app = mcp.streamable_http_app()
    if token:
        app.add_middleware(BearerTokenMiddleware, token=token, exempt_paths=(HEALTH_PATH,))
        log.info("bearer authentication is enabled", extra={"exempt": [HEALTH_PATH]})
    return app
