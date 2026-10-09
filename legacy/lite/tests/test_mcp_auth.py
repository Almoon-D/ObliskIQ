"""The server never serves a position ledger unauthenticated off loopback.

This is the one property of the MCP layer with no second line of defence. A gate
that fails still leaves the ledger's own integrity rules; an open port leaves
nothing at all.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from obliskiq.env import EnvVarError, Settings, load_settings
from obliskiq.server.app import HEALTH_PATH, build_http_app, build_server
from obliskiq.server.auth import BearerTokenMiddleware
from obliskiq.server.context import ServerContext

TOKEN = "a-token-nobody-should-be-able-to-guess"


@pytest.fixture()
def guarded_app():
    """A trivial ASGI app behind the same middleware the server uses."""

    async def ok(request):
        return PlainTextResponse("reached")

    app = Starlette(
        routes=[Route("/mcp", ok, methods=["GET"]), Route(HEALTH_PATH, ok, methods=["GET"])]
    )
    app.add_middleware(BearerTokenMiddleware, token=TOKEN, exempt_paths=(HEALTH_PATH,))
    return app


def test_a_request_without_a_token_is_refused(guarded_app):
    response = TestClient(guarded_app).get("/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower().startswith("bearer")


def test_the_challenge_body_names_nothing_useful(guarded_app):
    """An unauthenticated caller learns that the process is alive and no more."""
    body = TestClient(guarded_app).get("/mcp").text
    assert "obliskiq" not in body.lower()
    assert "ledger" not in body.lower()


def test_a_wrong_token_is_refused(guarded_app):
    client = TestClient(guarded_app)
    assert client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}x"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": TOKEN}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Basic " + TOKEN}).status_code == 401


def test_a_token_prefix_does_not_pass(guarded_app):
    """The comparison is over the whole token, not a prefix of it."""
    response = TestClient(guarded_app).get("/mcp", headers={"Authorization": f"Bearer {TOKEN[:8]}"})
    assert response.status_code == 401


def test_the_right_token_reaches_the_app(guarded_app):
    response = TestClient(guarded_app).get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.text == "reached"


def test_health_is_reachable_without_a_token(guarded_app):
    """A load balancer must be able to check liveness without a credential."""
    assert TestClient(guarded_app).get(HEALTH_PATH).status_code == 200


def test_middleware_refuses_an_empty_token():
    """An empty token would authenticate everything. Refuse to construct."""

    async def app(scope, receive, send):  # pragma: no cover - never called
        raise AssertionError("must not be reached")

    with pytest.raises(ValueError, match="without a token"):
        BearerTokenMiddleware(app, token="")


def test_settings_refuse_a_public_bind_with_no_token(monkeypatch, tmp_path):
    """The refusal happens at load, before anything binds."""
    monkeypatch.setenv("OBLISK_DB_PATH", str(tmp_path / "oblisk.db"))
    monkeypatch.setenv("OBLISK_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("OBLISK_MCP_PORT", "8000")
    monkeypatch.delenv("OBLISK_MCP_AUTH_TOKEN", raising=False)

    with pytest.raises(EnvVarError, match="OBLISK_MCP_AUTH_TOKEN"):
        load_settings(load_dotenv_file=False)


def test_a_public_bind_with_a_token_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("OBLISK_DB_PATH", str(tmp_path / "oblisk.db"))
    monkeypatch.setenv("OBLISK_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("OBLISK_MCP_PORT", "8000")
    monkeypatch.setenv("OBLISK_MCP_AUTH_TOKEN", TOKEN)

    settings = load_settings(load_dotenv_file=False)
    assert settings.binds_publicly
    assert settings.require_mcp_auth_token() == TOKEN


def test_loopback_without_a_token_still_loads(monkeypatch, tmp_path):
    """Local development is permitted. The CLI warns; it does not refuse."""
    monkeypatch.setenv("OBLISK_DB_PATH", str(tmp_path / "oblisk.db"))
    monkeypatch.setenv("OBLISK_MCP_HOST", "127.0.0.1")
    monkeypatch.delenv("OBLISK_MCP_AUTH_TOKEN", raising=False)

    settings = load_settings(load_dotenv_file=False)
    assert not settings.binds_publicly


def test_the_http_app_without_a_token_adds_no_middleware(config, tmp_path):
    """Loopback development has no token to enforce, and says so by structure."""
    context = ServerContext(settings=_settings(tmp_path), config=config)
    app = build_http_app(build_server(context), token=None)
    assert not any(middleware.cls is BearerTokenMiddleware for middleware in app.user_middleware)


def test_the_http_app_with_a_token_installs_the_middleware(config, tmp_path):
    context = ServerContext(settings=_settings(tmp_path), config=config)
    app = build_http_app(build_server(context), token=TOKEN)
    assert any(middleware.cls is BearerTokenMiddleware for middleware in app.user_middleware)


def _settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "oblisk.db",
        config_path=tmp_path / "config.yaml",
        log_level="INFO",
        log_format="json",
        timezone="UTC",
        mcp_host="127.0.0.1",
        mcp_port=8000,
        mcp_auth_token=None,
        fred_api_key=None,
        eia_api_key=None,
        marketdata_api_token=None,
        ibkr_cp_base_url=None,
        ibkr_connector_url=None,
        ibkr_consumer_key=None,
        ibkr_access_token=None,
        ibkr_access_token_secret=None,
        ibkr_signature_key_path=None,
        ibkr_encryption_key_path=None,
        ibkr_dh_param_path=None,
        ibkr_ca_bundle=None,
    )
