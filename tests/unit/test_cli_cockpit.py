"""``obliskiq cockpit serve``, ``dashboard`` and ``health``; ``serve``'s restart on changed
settings; the notifier's document. Nothing binds a port here: uvicorn and stdio are replaced.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from board_rig import digest, performance
from pydantic import SecretStr

from obliskiq.cli import cockpit as cli_cockpit
from obliskiq.cli import service as cli_service
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import load_config
from obliskiq.orchestration.health import write_heartbeat
from obliskiq.reporting.board import build_board, write_board
from obliskiq.reporting.notifier import LogNotifier, TelegramNotifier
from obliskiq.reporting.reporter import Reporter


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    return path


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


def _host(config_dir: Path, host: str) -> None:
    path = config_dir / "defaults" / "server.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("host: 127.0.0.1", f"host: {host}"),
        encoding="utf-8",
    )


# ── cockpit serve ──


def test_the_cockpit_refuses_to_listen_beyond_loopback_without_a_token(
    data_dir: Path, config_dir: Path
) -> None:
    _host(config_dir, "0.0.0.0")
    status, out = _run("cockpit", "serve")
    assert status == EXIT_INVALID
    assert "without OBLISK_MCP_AUTH_TOKEN" in out


def test_the_cockpit_serves_http_with_the_telegram_bot_beside_it(
    data_dir: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(config_dir, "0.0.0.0")
    monkeypatch.setenv("OBLISK_MCP_AUTH_TOKEN", "a-long-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    seen: dict[str, Any] = {}

    async def serve(self: Any) -> None:
        seen["host"] = self.config.host
        seen["middleware"] = [m.cls.__name__ for m in self.config.app.user_middleware]

    async def bot_run(self: Any, stop: Any) -> None:
        await stop.wait()
        seen["bot"] = "stopped"

    monkeypatch.setattr(cli_cockpit.uvicorn.Server, "serve", serve)
    monkeypatch.setattr(cli_cockpit.TelegramBot, "run", bot_run)
    status, out = _run("cockpit", "serve")
    assert status == EXIT_OK
    assert seen == {
        "host": "0.0.0.0",
        "middleware": ["BearerTokenMiddleware"],
        "bot": "stopped",
    }
    assert "warning" not in out


def test_the_cockpit_warns_on_loopback_without_a_token(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def serve(self: Any) -> None:
        return None

    monkeypatch.setattr(cli_cockpit.uvicorn.Server, "serve", serve)
    status, out = _run("cockpit", "serve")
    assert status == EXIT_OK
    assert "warning: no OBLISK_MCP_AUTH_TOKEN" in out


def test_the_cockpit_serves_one_local_session_on_stdio(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[str] = []

    async def stdio(self: Any) -> None:
        served.append(self.name)

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.run_stdio_async", stdio)
    status, out = _run("cockpit", "serve", "--stdio")
    assert status == EXIT_OK
    assert served == ["obliskiq-cockpit"]
    assert out == ""


# ── dashboard and health ──


def test_the_dashboard_is_written_from_the_last_board(data_dir: Path, config_dir: Path) -> None:
    status, out = _run("dashboard")
    assert (status, out.strip()) == (
        EXIT_INVALID,
        "no board yet: the service writes one after each daily digest",
    )
    settings = load_config(config_dir).settings
    board = build_board(
        digest(settings), settings, performance=performance(), shares={}, patterns=[]
    )
    write_board(data_dir / "board.json", board)
    status, out = _run("dashboard")
    assert status == EXIT_OK
    assert "dashboard written to" in out
    assert "XDWD" in (data_dir / "dashboard.html").read_text(encoding="utf-8")


def test_health_follows_the_heartbeat(data_dir: Path) -> None:
    assert _run("health") == (EXIT_INVALID, "unhealthy: no heartbeat recorded\n")
    now = datetime.now(UTC)
    write_heartbeat(data_dir / "heartbeat.json", now, {"status": "ok"})
    status, out = _run("health")
    assert status == EXIT_OK
    assert out.startswith("healthy:")
    write_heartbeat(data_dir / "heartbeat.json", now - timedelta(minutes=20), {"status": "ok"})
    assert _run("health")[0] == EXIT_INVALID
    assert _run("health", "--max-age", "3600")[0] == EXIT_OK
    write_heartbeat(data_dir / "heartbeat.json", now, {"status": "stopped"})
    assert "status stopped" in _run("health")[1]
    write_heartbeat(data_dir / "heartbeat.json", now - timedelta(seconds=30), {"status": "ok"})
    later = (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    status, out = _run("health", "--since", later)
    assert status == EXIT_INVALID
    assert "is before" in out
    earlier = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _run("health", "--since", earlier)[0] == EXIT_OK
    with pytest.raises(SystemExit):
        _run("health", "--since", "2026-11-16T10:00:00")


# ── serve restarts on changed settings ──


def test_serve_starts_again_on_changed_settings(
    data_dir: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    monkeypatch.setenv("OBLISK_IBKR_TRANSPORT", "gateway")
    monkeypatch.setenv("OBLISK_IBKR_ACCOUNT_ID", "DUTEST1")
    monkeypatch.setenv("OBLISK_IBKR_GATEWAY_USERNAME", "u")
    monkeypatch.setenv("OBLISK_IBKR_GATEWAY_PASSWORD", "p")
    runs: list[int] = []

    class FakeService:
        def __init__(self, settings: Any) -> None:
            self.settings = settings

        async def run(self, stop: Any, *, watch: Any) -> bool:
            runs.append(self.settings.thesis.max_positions)
            if len(runs) == 1:
                with (config_dir / "owner.yaml").open("a", encoding="utf-8") as handle:
                    handle.write("thesis:\n  max_positions: 6\n")
                return True
            return False

    @asynccontextmanager
    async def opened(env: Any, settings: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield FakeService(settings)

    monkeypatch.setattr(cli_service, "open_service", opened)
    write_heartbeat(data_dir / "settings-applied.json", datetime.now(UTC), {})
    status, out = _run("serve")
    assert status == EXIT_OK
    assert runs == [8, 6]
    assert "settings changed: the service starts again on them" in out
    assert "audited 1 setting change(s) no command recorded" in out


# ── the notifier's document ──


async def test_the_log_notifier_records_a_document(tmp_path: Path) -> None:
    assert await LogNotifier().send_document("caption", tmp_path / "d.html")


@respx.mock
async def test_telegram_sends_the_dashboard_as_a_document(
    tmp_path: Path, shipped_settings: Any
) -> None:
    page = tmp_path / "dashboard.html"
    page.write_text("<p>x</p>", encoding="utf-8")
    route = respx.post("https://api.telegram.org/bot1:t/sendDocument").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier(
        shipped_settings.reporting, token=SecretStr("1:t"), chat_id=SecretStr("42")
    )
    reporter = Reporter(notifier, book=None)  # type: ignore[arg-type]
    assert await reporter.report_file("ObliskIQ dashboard", page)
    body = route.calls[0].request.content
    assert b'name="document"; filename="dashboard.html"' in body
    assert b"<p>x</p>" in body
    route.mock(return_value=httpx.Response(400))
    assert not await notifier.send_document("c", page)
    route.mock(side_effect=httpx.ConnectError("down"))
    assert not await notifier.send_document("c", page)
    assert not await notifier.send_document("c", tmp_path / "absent.html")
    async with httpx.AsyncClient() as client:
        route.mock(return_value=httpx.Response(200, json={"ok": True}))
        shared = TelegramNotifier(
            shipped_settings.reporting, token=SecretStr("1:t"), chat_id=SecretStr("42"),
            client=client,
        )  # fmt: skip
        assert await shared.send_document("c", page)
