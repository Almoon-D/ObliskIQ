"""The MCP cockpit: the tools, the transports and the routes beside them (MASTER_PLAN §5.17).

Ported from ObliskIQ-lite-MCP (``server/app.py`` @b1ebd49): FastMCP, two transports, a bearer
token in front of HTTP, and an unauthenticated ``/healthz`` that says only that the process is
alive. **stdio** is how a local Claude Code session runs it: it binds nothing. **streamable
HTTP** is how a remote client reaches it: it binds the cockpit's port, and the token is required
off loopback. ``/dashboard`` serves the day's dashboard behind the same token.

Every tool calls :class:`~obliskiq.server.cockpit.Cockpit`; a refusal reaches the client as a
tool error whose text is the reason, to be quoted rather than worked around. :data:`TOOLS` is the
exact list, in the order a session should call them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from ..errors import ObliskError
from ..logging_setup import get_logger
from .auth import BearerTokenMiddleware
from .cockpit import REFUSALS, Cockpit

log = get_logger("server.app")

SERVER_NAME = "obliskiq-cockpit"
#: Unauthenticated, and says nothing but that the process is alive.
HEALTH_PATH = "/healthz"
#: The day's dashboard, behind the token.
DASHBOARD_PATH = "/dashboard"
#: Where the streamable-HTTP transport listens. The MCP default.
MCP_PATH = "/mcp"
HTTP_NOT_FOUND = 404

#: Every tool, in the order a session should call them: what the bot is doing, then what it
#: holds and plans, then the evidence, then the settings and the owner's commands.
TOOLS = (
    "get_status",
    "get_health",
    "get_portfolio",
    "get_targets",
    "get_intents",
    "get_theses",
    "get_patterns",
    "get_grid_view",
    "get_macro_board",
    "get_scoreboard",
    "get_settings",
    "update_setting",
    "cash_deposit",
    "cash_reserve",
    "list_cash",
    "list_questions",
    "answer_question",
    "approve_intent",
    "reject_intent",
    "set_pause",
    "kill_switch",
)

INSTRUCTIONS = """\
The owner's cockpit for ObliskIQ, an autonomous portfolio manager on one IBKR account. Call \
get_status first: it says the mode, the kill switch, the pause and how fresh the service's \
heartbeat is.

Never invent a number. Every figure comes from a tool, with the moment it was read; the \
book's positions and targets come from the service's last board, and a stale one is labelled \
stale. This server opens no brokerage session and holds no live quotes.

A refusal is the answer, not a fault: quote its reason. Commands that add risk or record money \
(update_setting, cash_deposit, cash_reserve, answer_question, approve_intent) return a preview \
until called again with confirm=true; show the preview to the owner and confirm only on their \
word. reject_intent, set_pause and kill_switch act at once. Remote changes may only tighten risk \
or manage budgets within bounds; loosening a risk limit, and turning the kill switch off, are \
done on the machine.\
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
PROTECTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)


async def call(name: str, work: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Run one tool: a refusal passes through as its reason; anything else is named."""
    try:
        return await work()
    except REFUSALS as exc:
        raise ToolError(str(exc)) from exc
    except ObliskError as exc:
        raise ToolError(f"{name}: {exc}") from exc
    except Exception as exc:
        log.exception("a cockpit tool failed", extra={"tool": name})
        raise ToolError(f"{name} failed: {type(exc).__name__}") from exc


def build_server(cockpit: Cockpit, **settings: Any) -> FastMCP:
    """The FastMCP instance with every tool and the two routes registered."""
    mcp = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS, **settings)

    @mcp.tool(annotations=READ_ONLY)
    async def get_status() -> dict[str, Any]:
        """Mode, kill switch, pause, heartbeat, reconciliation, NAV, open intents and questions."""
        return await call("get_status", cockpit.status)

    @mcp.tool(annotations=READ_ONLY)
    async def get_health() -> dict[str, Any]:
        """The heartbeat's facts, each data source's health, open alerts and recent runs."""
        return await call("get_health", cockpit.health)

    @mcp.tool(annotations=READ_ONLY)
    async def get_portfolio() -> dict[str, Any]:
        """NAV, time-weighted return, managed capital, positions and sleeve budgets."""
        return await call("get_portfolio", cockpit.portfolio)

    @mcp.tool(annotations=READ_ONLY)
    async def get_targets() -> dict[str, Any]:
        """The core's target portfolio: variant, trigger, each role's target and what trades."""
        return await call("get_targets", cockpit.targets)

    @mcp.tool(annotations=READ_ONLY)
    async def get_intents(include_closed: bool = False) -> dict[str, Any]:
        """Order intents, newest first: open ones, or every one with include_closed."""
        return await call("get_intents", lambda: cockpit.intents(include_closed=include_closed))

    @mcp.tool(annotations=READ_ONLY)
    async def get_theses() -> dict[str, Any]:
        """The thesis book: open theses and the book's plan, record and second opinions."""
        return await call("get_theses", cockpit.theses)

    @mcp.tool(annotations=READ_ONLY)
    async def get_patterns() -> dict[str, Any]:
        """Every hypothesis in the research registry with its lifecycle status and decision."""
        return await call("get_patterns", cockpit.patterns)

    @mcp.tool(annotations=READ_ONLY)
    async def get_grid_view(as_of: str | None = None) -> dict[str, Any]:
        """The equity grid (market x sector) on or before as_of (YYYY-MM-DD; default today)."""
        return await call("get_grid_view", lambda: cockpit.grid_view(as_of=as_of))

    @mcp.tool(annotations=READ_ONLY)
    async def get_macro_board(as_of: str | None = None) -> dict[str, Any]:
        """The macro board (region x asset class) on or before as_of (default today)."""
        return await call("get_macro_board", lambda: cockpit.macro_board(as_of=as_of))

    @mcp.tool(annotations=READ_ONLY)
    async def get_scoreboard() -> dict[str, Any]:
        """Time-weighted growth beside the references, each layer's live share and its looks."""
        return await call("get_scoreboard", cockpit.scoreboard)

    @mcp.tool(annotations=READ_ONLY)
    async def get_settings() -> dict[str, Any]:
        """The owner's settings, those a remote change may touch and how, and their values."""
        return await call("get_settings", cockpit.settings)

    @mcp.tool(annotations=WRITE)
    async def update_setting(
        key: str, value: str, reason: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Change one whitelisted setting (tighten a risk limit, or a budget within bounds)."""
        return await call(
            "update_setting",
            lambda: cockpit.update_setting(key, value, reason=reason, confirm=confirm),
        )

    @mcp.tool(annotations=WRITE)
    async def cash_deposit(
        amount: str,
        currency: str,
        on: str | None = None,
        note: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Record money the owner added (amount as digits, on as YYYY-MM-DD, default today)."""
        return await call(
            "cash_deposit",
            lambda: cockpit.cash_deposit(amount, currency, on=on, note=note, confirm=confirm),
        )

    @mcp.tool(annotations=WRITE)
    async def cash_reserve(
        amount: str, currency: str, by: str, reason: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Reserve money the owner will withdraw by a date (YYYY-MM-DD)."""
        return await call(
            "cash_reserve",
            lambda: cockpit.cash_reserve(amount, currency, by=by, reason=reason, confirm=confirm),
        )

    @mcp.tool(annotations=READ_ONLY)
    async def list_cash() -> dict[str, Any]:
        """Reservations, recorded deposits and withdrawals, and unexplained cash changes."""
        return await call("list_cash", cockpit.list_cash)

    @mcp.tool(annotations=READ_ONLY)
    async def list_questions(include_closed: bool = False) -> dict[str, Any]:
        """The questions the bot needs the owner to answer, with options, costs and deadline."""
        return await call(
            "list_questions", lambda: cockpit.list_questions(include_closed=include_closed)
        )

    @mcp.tool(annotations=WRITE)
    async def answer_question(
        question_id: str, option: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Answer an owner question (Q<number>) with one of its option keys."""
        return await call(
            "answer_question",
            lambda: cockpit.answer_question(question_id, option, confirm=confirm),
        )

    @mcp.tool(annotations=WRITE)
    async def approve_intent(intent_id: str, reason: str, confirm: bool = False) -> dict[str, Any]:
        """Release an intent held for the owner's approval."""
        return await call(
            "approve_intent",
            lambda: cockpit.approve_intent(intent_id, reason=reason, confirm=confirm),
        )

    @mcp.tool(annotations=PROTECTIVE)
    async def reject_intent(intent_id: str, reason: str) -> dict[str, Any]:
        """Reject an intent held for approval (acts at once)."""
        return await call("reject_intent", lambda: cockpit.reject_intent(intent_id, reason=reason))

    @mcp.tool(annotations=PROTECTIVE)
    async def set_pause(paused: bool, reason: str = "") -> dict[str, Any]:
        """Pause (paused=true) or resume the decision cycles; protective exits stay."""
        return await call("set_pause", lambda: cockpit.set_pause(on=paused, reason=reason))

    @mcp.tool(annotations=PROTECTIVE)
    async def kill_switch(reason: str) -> dict[str, Any]:
        """Turn the kill switch ON: block new risk, cancel working entries, keep exits."""
        return await call("kill_switch", lambda: cockpit.kill_switch(reason=reason))

    return mcp


def build_http_app(mcp: FastMCP, cockpit: Cockpit, *, token: str | None) -> Starlette:
    """The streamable-HTTP app with ``/healthz`` and ``/dashboard`` beside it, and the bearer
    check in front of everything but ``/healthz``. ``token`` is ``None`` only on loopback with
    none configured, which the CLI warns about."""

    async def healthz(_request: Request) -> Response:
        """Liveness only. Reachable without a token, and reveals nothing else."""
        return JSONResponse({"status": "ok"})

    async def dashboard(_request: Request) -> Response:
        """The day's dashboard as the service last wrote it."""
        try:
            page = cockpit.dashboard_path().read_text(encoding="utf-8")
        except OSError:
            return PlainTextResponse(
                "no dashboard yet: the service writes one after each daily digest",
                status_code=HTTP_NOT_FOUND,
            )
        return HTMLResponse(page)

    app = mcp.streamable_http_app()
    app.add_route(HEALTH_PATH, healthz, methods=["GET"], include_in_schema=False)
    app.add_route(DASHBOARD_PATH, dashboard, methods=["GET"], include_in_schema=False)
    if token:
        app.add_middleware(BearerTokenMiddleware, token=token, exempt_paths=(HEALTH_PATH,))
    return app
