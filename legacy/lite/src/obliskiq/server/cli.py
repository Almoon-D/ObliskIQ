"""``obliskiq-mcp`` — the MCP server as a standalone entrypoint.

The third entrypoint, and the only one that binds a port. It is deliberately not
runnable from the collector or compute systemd units: those are oneshots that
hold no session, and this holds a position ledger it can write to. The blast
radii are different and so are the units.

Two subcommands, no default. ``stdio`` for a local Claude Code session, ``http``
for a container or a VM. Guessing between them from the environment would mean a
misconfigured deployment binds silently to the wrong thing.

Exit codes match the other entrypoints: 0 clean, 1 the server could not start.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ..env import LOOPBACK_HOSTS
from ..errors import ObliskError
from ..logging_setup import configure_logging, get_logger
from .app import HEALTH_PATH, MCP_PATH, build_http_app, build_server
from .context import ServerContext

log = get_logger("server.cli")

EXIT_OK = 0
EXIT_FAILED = 1

TRANSPORTS = ("stdio", "http")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obliskiq-mcp",
        description=(
            "The obliskiq-lite MCP server. Wraps the store, the grid, the ledgers and "
            "the risk engine as tools. Touches no network of its own: live quotes, "
            "positions and NAV are passed in by the caller."
        ),
    )
    parser.add_argument(
        "transport",
        choices=list(TRANSPORTS),
        help=(
            "'stdio' for a local Claude Code session, which binds nothing and needs no "
            "token; 'http' for streamable-HTTP on OBLISK_MCP_HOST:OBLISK_MCP_PORT, "
            "which refuses to start unauthenticated off loopback"
        ),
    )
    parser.add_argument("--config", help="path to config.yaml; defaults to $OBLISK_CONFIG_PATH")
    parser.add_argument("--log-level", help="override OBLISK_LOG_LEVEL")
    parser.add_argument("--log-format", choices=["json", "text"], help="override OBLISK_LOG_FORMAT")
    parser.add_argument(
        "--stateless",
        action="store_true",
        help=(
            "http only: serve every request without a persistent session. Use behind a "
            "load balancer that will not pin a client to one instance"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # load_settings() validates: off loopback with no OBLISK_MCP_AUTH_TOKEN it
        # raises here, before anything binds.
        context = ServerContext.load(config_path=args.config)
        configure_logging(
            level=args.log_level or context.settings.log_level,
            fmt=args.log_format or context.settings.log_format,
            utc=context.config.require_bool("runtime.logging.utc_timestamps"),
        )
        if args.transport == "stdio":
            _serve_stdio(context)
        else:
            _serve_http(context, stateless=args.stateless)
    except ObliskError as exc:
        configure_logging()
        log.error("the MCP server could not start", extra={"error": str(exc)})
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        log.info("the MCP server stopped")
    return EXIT_OK


def _serve_stdio(context: ServerContext) -> None:
    """Local transport. Reachable only by the process that spawned it."""
    log.info(
        "serving over stdio",
        extra={"db_path": str(context.settings.db_path), "authenticated": False},
    )
    build_server(context).run(transport="stdio")


def _serve_http(context: ServerContext, *, stateless: bool) -> None:
    """Network transport. The token is enforced here, not merely documented."""
    import uvicorn

    settings = context.settings
    host = settings.mcp_host
    port = settings.require_mcp_port()
    token = settings.mcp_auth_token

    if settings.binds_publicly:
        # Raises when unset. This server exposes a position ledger it can write
        # to; it must never be reachable unauthenticated.
        token = settings.require_mcp_auth_token()
    elif not token:
        log.warning(
            "starting on loopback with NO auth token. Anything that can reach this port "
            "can read the ledger and write to it. Set OBLISK_MCP_AUTH_TOKEN before "
            "binding anywhere a browser or another user can reach.",
            extra={"host": host, "port": port, "loopback": sorted(LOOPBACK_HOSTS - {""})},
        )

    mcp = build_server(context, host=host, port=port, stateless_http=stateless)
    app = build_http_app(mcp, token=token)
    log.info(
        "serving streamable HTTP",
        extra={
            "host": host,
            "port": port,
            "path": MCP_PATH,
            "health": HEALTH_PATH,
            "authenticated": bool(token),
            "stateless": stateless,
        },
    )
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
