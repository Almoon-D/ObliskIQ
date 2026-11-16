"""The cockpit's bearer token and its JSON encoding, ported from ObliskIQ-lite-MCP
(``tests/test_mcp_auth.py`` and the encoding tests of ``tests/test_mcp_cli.py`` @b1ebd49), over
httpx's ASGI transport.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from obliskiq.server.app import HEALTH_PATH
from obliskiq.server.auth import BearerTokenMiddleware
from obliskiq.server.encode import to_jsonable

TOKEN = "a-token-nobody-should-be-able-to-guess"


async def _ok(_request: Request) -> Response:
    return PlainTextResponse("reached")


@pytest.fixture
def guarded_app() -> Starlette:
    """A trivial ASGI app behind the same middleware the cockpit uses."""
    app = Starlette(
        routes=[Route("/mcp", _ok, methods=["GET"]), Route(HEALTH_PATH, _ok, methods=["GET"])]
    )
    app.add_middleware(BearerTokenMiddleware, token=TOKEN, exempt_paths=(HEALTH_PATH,))
    return app


async def _get(app: Starlette, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.get(path, headers=headers)


async def test_a_request_without_a_token_is_refused(guarded_app: Starlette) -> None:
    response = await _get(guarded_app, "/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower().startswith("bearer")


async def test_the_challenge_body_names_nothing_useful(guarded_app: Starlette) -> None:
    """An unauthenticated caller learns that the process is alive and no more."""
    body = (await _get(guarded_app, "/mcp")).text
    assert "obliskiq" not in body.lower()
    assert "ledger" not in body.lower()


@pytest.mark.parametrize("header", [f"Bearer {TOKEN}x", TOKEN, f"Basic {TOKEN}", "Bearer "])
async def test_a_wrong_token_is_refused(guarded_app: Starlette, header: str) -> None:
    assert (await _get(guarded_app, "/mcp", {"Authorization": header})).status_code == 401


async def test_a_token_prefix_does_not_pass(guarded_app: Starlette) -> None:
    """The comparison is over the whole token, not a prefix of it."""
    response = await _get(guarded_app, "/mcp", {"Authorization": f"Bearer {TOKEN[:8]}"})
    assert response.status_code == 401


async def test_the_right_token_reaches_the_app(guarded_app: Starlette) -> None:
    response = await _get(guarded_app, "/mcp", {"Authorization": f"Bearer {TOKEN}"})
    assert (response.status_code, response.text) == (200, "reached")


async def test_health_is_reachable_without_a_token(guarded_app: Starlette) -> None:
    assert (await _get(guarded_app, HEALTH_PATH)).status_code == 200


async def test_the_lifespan_passes_through_untouched() -> None:
    """The transport starts its session manager in the lifespan; swallowing it would leave the
    server accepting requests it cannot serve."""
    seen: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    middleware = BearerTokenMiddleware(app, token=TOKEN)

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: dict[str, Any]) -> None:
        return None

    await middleware({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


def test_middleware_refuses_an_empty_token() -> None:
    """An empty token would authenticate everything. Refuse to construct."""

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("must not be reached")

    with pytest.raises(ValueError, match="without a token"):
        BearerTokenMiddleware(app, token="")


# ── encoding ──


def test_nan_and_infinity_become_null_not_numbers() -> None:
    """A NaN serialised as a number is a lie in the shape of data."""
    assert to_jsonable(float("nan")) is None
    assert to_jsonable(float("inf")) is None
    assert to_jsonable(float("-inf")) is None
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable(Decimal("NaN")) is None


def test_dates_and_tuples_survive_the_wire() -> None:
    @dataclass(frozen=True)
    class Row:
        when: date
        stamped: datetime
        tags: tuple[str, ...]
        value: float | None

    encoded = to_jsonable(Row(date(2026, 1, 15), datetime(2026, 1, 15, 9, 30), ("a", "b"), None))
    assert encoded == {
        "when": "2026-01-15",
        "stamped": "2026-01-15T09:30:00",
        "tags": ["a", "b"],
        "value": None,
    }


def test_nested_structures_are_converted_all_the_way_down() -> None:
    encoded = to_jsonable({"rows": [{"d": date(2026, 1, 15), "v": float("nan")}]})
    assert encoded == {"rows": [{"d": "2026-01-15", "v": None}]}


def test_money_keeps_its_exact_digits() -> None:
    assert to_jsonable(Decimal("1237.57")) == "1237.57"


class Colour(Enum):
    RED = "red"


class Item:
    """Anything that knows how to become a Python scalar (a numpy scalar, say)."""

    def item(self) -> int:
        return 7


def test_every_other_kind_becomes_a_primitive() -> None:
    class Model(BaseModel):
        at: date

    @dataclass(frozen=True)
    class Verdict:
        score: int

        @property
        def passed(self) -> bool:
            return self.score > 1

        @property
        def broken(self) -> bool:
            raise RuntimeError("derived, and failing")

    assert to_jsonable(Colour.RED) == "red"
    assert to_jsonable(Path("/x/y")) == "/x/y"
    assert to_jsonable({"b", "a"}) == ["a", "b"]
    assert to_jsonable(Item()) == 7
    assert to_jsonable(Model(at=date(2026, 1, 2))) == {"at": "2026-01-02"}
    assert to_jsonable(Verdict(2)) == {"score": 2, "passed": True}
    assert to_jsonable(b"raw") == "b'raw'"
    assert dataclasses.is_dataclass(Verdict)


def test_a_method_referenced_without_a_call_fails_loudly() -> None:
    with pytest.raises(TypeError, match="was referenced without being called"):
        to_jsonable(test_a_method_referenced_without_a_call_fails_loudly)


@dataclass(frozen=True)
class Base:
    score: int

    @property
    def label(self) -> str:
        return "base"


@dataclass(frozen=True)
class Child(Base):
    @property
    def label(self) -> str:
        return "child"


def test_an_overridden_property_is_read_once_from_the_subclass() -> None:
    assert to_jsonable(Child(1)) == {"score": 1, "label": "child"}
