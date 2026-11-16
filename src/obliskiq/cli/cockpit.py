"""``obliskiq cockpit serve``, ``obliskiq dashboard`` and ``obliskiq health``.

``cockpit serve`` runs the MCP cockpit (``server/app.py``) on streamable HTTP at ``server.host``
and ``server.port``, with the Telegram bot beside it when ``TELEGRAM_BOT_TOKEN`` and
``TELEGRAM_CHAT_ID`` are set; ``--stdio`` serves a local Claude Code session instead and binds
nothing. Beyond loopback it refuses to start without ``OBLISK_MCP_AUTH_TOKEN``. It opens no
brokerage session, so it runs beside the service. ``dashboard`` writes the dashboard again from
the service's last board; ``health`` exits 0 only when the service's heartbeat is fresh and says
ok, for a container's health check and ``deploy/update.sh``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TextIO

import uvicorn

from ..config.changes import MCP, TELEGRAM
from ..logging_setup import get_logger
from ..orchestration.health import read_heartbeat
from ..reporting.board import read_board, write_text_atomically
from ..reporting.dashboard import render_dashboard
from ..server.app import HEALTH_PATH, MCP_PATH, build_http_app, build_server
from ..server.cockpit import Cockpit
from ..server.telegram import TelegramBot
from ..store import open_store
from ..store.worker import StoreWorker

if TYPE_CHECKING:
    from .app import Loaded

log = get_logger("cli.cockpit")

EXIT_OK = 0
EXIT_INVALID = 1
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MCP_ACTOR = "owner (MCP cockpit)"
TELEGRAM_ACTOR = "owner (Telegram)"
#: Heartbeat statuses ``health`` accepts.
HEALTHY = frozenset({"ok", "degraded"})


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(text: str) -> datetime:
    """An ISO 8601 moment with its zone (``Z`` for UTC)."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"{text!r} has no time zone")
    return moment


async def _serve_http(
    loaded: Loaded, worker: StoreWorker, cockpit: Cockpit, token: str | None
) -> None:
    """The HTTP transport and, when configured, the Telegram bot, until a signal stops uvicorn."""
    settings = loaded.config.settings
    server_config = settings.server
    mcp = build_server(cockpit, host=server_config.host, port=server_config.port)
    app = build_http_app(mcp, cockpit, token=token)
    server = uvicorn.Server(
        uvicorn.Config(app, host=server_config.host, port=server_config.port, log_config=None)
    )
    env = loaded.env
    stop = asyncio.Event()
    bot_task: asyncio.Task[None] | None = None
    if env.telegram_bot_token is not None and env.telegram_chat_id is not None:
        bot = TelegramBot(
            Cockpit(loaded.config, env, worker=worker, actor=TELEGRAM_ACTOR, channel=TELEGRAM),
            settings,
            token=env.telegram_bot_token,
            chat_id=env.telegram_chat_id,
            state_path=env.require_data_dir() / settings.paths.telegram_state,
            now=_now,
        )
        bot_task = asyncio.create_task(bot.run(stop))
    log.info(
        "serving the cockpit",
        extra={
            "host": server_config.host,
            "port": server_config.port,
            "path": MCP_PATH,
            "health": HEALTH_PATH,
            "authenticated": bool(token),
            "telegram": bot_task is not None,
        },
    )
    try:
        await server.serve()
    finally:
        stop.set()
        if bot_task is not None:
            await bot_task


def _cockpit_serve(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    env = loaded.env
    data_dir = env.require_data_dir()
    host = settings.server.host
    token = None if env.mcp_auth_token is None else env.mcp_auth_token.get_secret_value()
    stdio = bool(loaded.args.stdio)
    if not stdio and host not in LOOPBACK_HOSTS and not token:
        print(
            f"refused: the cockpit would listen on {host}, beyond this machine, without "
            "OBLISK_MCP_AUTH_TOKEN; set the token or listen on 127.0.0.1",
            file=out,
        )
        return EXIT_INVALID
    if not stdio and not token:
        print(
            "warning: no OBLISK_MCP_AUTH_TOKEN; anything on this machine that reaches the port "
            "can read the cockpit and send its commands",
            file=out,
        )

    async def run() -> None:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            channel = MCP
            cockpit = Cockpit(loaded.config, env, worker=worker, actor=MCP_ACTOR, channel=channel)
            if stdio:
                await build_server(cockpit).run_stdio_async()
            else:
                await _serve_http(loaded, worker, cockpit, token)
        finally:
            await worker.aclose()

    asyncio.run(run())
    return EXIT_OK


def _dashboard(loaded: Loaded, out: TextIO) -> int:
    paths = loaded.config.settings.paths
    data_dir = loaded.env.require_data_dir()
    board = read_board(data_dir / paths.board)
    if board is None:
        print("no board yet: the service writes one after each daily digest", file=out)
        return EXIT_INVALID
    target = data_dir / paths.dashboard
    write_text_atomically(target, render_dashboard(board))
    print(f"dashboard written to {target} (board read at {board['at']})", file=out)
    return EXIT_OK


def _health(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    beat, facts = read_heartbeat(loaded.env.require_data_dir() / settings.paths.heartbeat_file)
    if beat is None:
        print("unhealthy: no heartbeat recorded", file=out)
        return EXIT_INVALID
    age = (_now() - beat).total_seconds()
    limit = loaded.args.max_age or 2 * settings.runtime.heartbeat_seconds
    status = facts.get("status")
    since: datetime | None = loaded.args.since
    if since is not None and beat < since:
        print(f"unhealthy: the last heartbeat ({beat.isoformat()}) is before {since}", file=out)
        return EXIT_INVALID
    if age > limit or status not in HEALTHY:
        print(f"unhealthy: heartbeat {age:.0f} s old (limit {limit} s), status {status}", file=out)
        return EXIT_INVALID
    print(f"healthy: heartbeat {age:.0f} s old, status {status}", file=out)
    return EXIT_OK


def add_cockpit_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cockpit = commands.add_parser("cockpit", help="the MCP cockpit and the Telegram bot")
    actions = cockpit.add_subparsers(dest="action", required=True, metavar="ACTION")
    serve = actions.add_parser(
        "serve", help="serve the cockpit on HTTP (and Telegram), or a local session on stdio"
    )
    serve.add_argument(
        "--stdio", action="store_true", help="serve one local Claude Code session over stdio"
    )
    serve.set_defaults(handler=_cockpit_serve)
    commands.add_parser(
        "dashboard", help="write the dashboard again from the service's last board"
    ).set_defaults(handler=_dashboard)
    health = commands.add_parser(
        "health", help="exit 0 when the service's heartbeat is fresh (a health check)"
    )
    health.add_argument(
        "--max-age",
        type=int,
        default=0,
        help="seconds a heartbeat may be old (default: twice runtime.heartbeat_seconds)",
    )
    health.add_argument(
        "--since",
        type=_aware,
        default=None,
        help="the heartbeat must be after this moment (ISO 8601 with its zone; deploy/update.sh)",
    )
    health.set_defaults(handler=_health)
