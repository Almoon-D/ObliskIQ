"""The MCP cockpit: the exact tool list, every tool through the MCP server, the commands' previews
and confirmations, the refusals quoted as tool errors, and the HTTP app's token, health check
and dashboard. No brokerage session is opened anywhere.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from board_rig import digest, intent, performance
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from obliskiq.config import LoadedConfig, load_config
from obliskiq.config.changes import MCP, settings_fingerprint
from obliskiq.domain.capital import OptionEffect, OwnerQuestion, QuestionKind, QuestionOption
from obliskiq.env import Environment
from obliskiq.errors import ObliskError
from obliskiq.execution.killswitch import file_flag_on
from obliskiq.orchestration.health import write_heartbeat
from obliskiq.reporting.board import build_board, write_board
from obliskiq.server import app as server_app
from obliskiq.server.app import TOOLS, build_http_app, build_server, call
from obliskiq.server.auth import BearerTokenMiddleware
from obliskiq.server.cockpit import Cockpit, CockpitRefusal
from obliskiq.store import capital as capital_repo
from obliskiq.store import open_store
from obliskiq.store import orders as orders_repo
from obliskiq.store.worker import StoreWorker

NOW = datetime(2026, 11, 16, 21, 30, tzinfo=UTC)
TOKEN = "a-test-token-that-is-long-enough"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    return path


@pytest.fixture
def loaded(config_dir: Path) -> LoadedConfig:
    return load_config(config_dir)


@pytest.fixture
def store(loaded: LoadedConfig, data_dir: Path) -> Iterator[Any]:
    with open_store(loaded.settings, data_dir) as opened:
        yield opened


@pytest.fixture
async def cockpit(loaded: LoadedConfig, data_dir: Path, store: Any) -> AsyncIterator[Cockpit]:
    settings = loaded.settings
    worker = StoreWorker(lambda: open_store(settings, data_dir))
    yield Cockpit(
        loaded,
        Environment(_env_file=None),  # type: ignore[call-arg]
        worker=worker,
        actor="owner (MCP cockpit)",
        channel=MCP,
        now=lambda: NOW,
    )
    await worker.aclose()


@pytest.fixture
def mcp(cockpit: Cockpit) -> FastMCP:
    return build_server(cockpit)


async def _call(mcp: FastMCP, name: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments)
    assert isinstance(result, tuple)
    structured: dict[str, Any] = result[1]
    return structured


def _board(loaded: LoadedConfig, data_dir: Path, *, at: datetime = NOW) -> None:
    settings = loaded.settings
    board = build_board(
        digest(settings, at=at, day=at.date()),
        settings,
        performance=performance(),
        shares={},
        patterns=[],
    )
    write_board(data_dir / settings.paths.board, board)


async def _question(mcp: FastMCP, store: Any) -> OwnerQuestion:
    reserve = {"amount": "400", "currency": "EUR", "by": "2026-12-20", "reason": "a trip"}
    await _call(mcp, "cash_reserve", **reserve, confirm=True)
    reservation = capital_repo.reservations(store)[0]
    return capital_repo.record_question(
        store,
        OwnerQuestion(
            kind=QuestionKind.PROTECTED_CUT,
            reservation_id=reservation.reservation_id,
            context="R1 needs 400 EUR from a protected position",
            options=(
                QuestionOption(
                    key="proceed", effect=OptionEffect.PROCEED, label="sell it",
                    tax_cost=Decimal("12.5"), strategy_cost=Decimal(20),
                ),
                QuestionOption(
                    key="postpone", effect=OptionEffect.POSTPONE, label="later",
                    tax_cost=None, strategy_cost=None, due_date=NOW.date() + timedelta(days=60),
                ),
            ),
            default="proceed",
            deadline=NOW + timedelta(days=5),
            created_at=NOW,
        ),
        actor="planner",
    )  # fmt: skip


# ── the tool list ──


async def test_the_cockpit_has_exactly_its_tools(mcp: FastMCP) -> None:
    assert [tool.name for tool in await mcp.list_tools()] == list(TOOLS)
    assert len(TOOLS) == 21


# ── reads ──


async def test_status_reads_the_store_and_the_service_files(
    mcp: FastMCP, loaded: LoadedConfig, data_dir: Path
) -> None:
    found = await _call(mcp, "get_status")
    assert found["mode"] == loaded.settings.execution.modes.initial.value
    assert found["kill_switch"] == "off"
    assert found["pause"] == "not paused"
    assert found["heartbeat"]["at"] is None
    assert found["nav"] is None
    write_heartbeat(
        data_dir / "heartbeat.json",
        NOW - timedelta(seconds=30),
        {"status": "ok", "session": "open", "settings": settings_fingerprint(loaded.settings)},
    )
    (data_dir / "FORCED_SHADOW").write_text("x", encoding="utf-8")
    found = await _call(mcp, "get_status")
    assert found["heartbeat"]["age_seconds"] == 30
    assert found["heartbeat"]["settings_current"] is True
    assert found["mode"] == "shadow (held by the FORCED_SHADOW file)"


async def test_health_lists_sources_alerts_and_runs(
    mcp: FastMCP, data_dir: Path, loaded: LoadedConfig
) -> None:
    (data_dir / "alert-state.json").write_text(json.dumps({"held": {"a:b": 1}}), encoding="utf-8")
    found = await _call(mcp, "get_health")
    assert found["open_alerts"] == ["a:b"]
    assert found["runs"] == []
    assert "counts" in found["sources"]
    (data_dir / "alert-state.json").write_text("garbage", encoding="utf-8")
    assert (await _call(mcp, "get_health"))["open_alerts"] == []


async def test_the_book_comes_from_the_board_with_its_age(
    mcp: FastMCP, loaded: LoadedConfig, data_dir: Path
) -> None:
    for tool in ("get_portfolio", "get_targets"):
        assert "the service has written no board yet" in (await _call(mcp, tool))["board"]
    _board(loaded, data_dir)
    portfolio = await _call(mcp, "get_portfolio")
    assert portfolio["age_days"] == 0
    assert "stale" not in portfolio
    assert portfolio["positions"][0]["symbol"] == "XDWD"
    assert portfolio["capital"]["investable"] == "950"
    targets = await _call(mcp, "get_targets")
    assert targets["core"]["variant"] == "D50"
    _board(loaded, data_dir, at=NOW - timedelta(days=5))
    stale = await _call(mcp, "get_portfolio")
    assert stale["stale"] is True
    assert "5 day(s) old" in stale["stale_note"]


async def test_intents_theses_patterns_grid_board_and_scoreboard(
    mcp: FastMCP, loaded: LoadedConfig, store: Any, data_dir: Path
) -> None:
    made = intent(loaded.settings)
    orders_repo.insert_intent(store, made, coid=str(made.intent_id), at=NOW)
    found = await _call(mcp, "get_intents")
    assert found["count"] == 1
    assert found["intents"][0]["intent_id"] == str(made.intent_id)
    assert (await _call(mcp, "get_intents", include_closed=True))["count"] == 1
    theses = await _call(mcp, "get_theses")
    assert theses["open"] == []
    assert "no board" in theses["book"]["board"]
    _board(loaded, data_dir)
    assert "lines" in (await _call(mcp, "get_theses"))["book"]
    patterns = (await _call(mcp, "get_patterns"))["patterns"]
    assert any(p["hypothesis"] == "H1" for p in patterns)
    grid = await _call(mcp, "get_grid_view", as_of="2026-11-13")
    assert (grid["session"], grid["cells"]) == (None, [])
    board = await _call(mcp, "get_macro_board")
    assert board["cells"] == []
    scoreboard = await _call(mcp, "get_scoreboard")
    assert scoreboard["live_shares"] == {}
    assert scoreboard["account"]["growth"] is None


async def test_patterns_without_a_source_checkout_say_so(
    mcp: FastMCP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("obliskiq.server.cockpit.repository_root", lambda: None)
    assert "no research registry" in (await _call(mcp, "get_patterns"))["patterns"]


async def test_settings_show_the_panel_the_rules_and_whether_they_run(
    mcp: FastMCP, loaded: LoadedConfig, data_dir: Path
) -> None:
    found = await _call(mcp, "get_settings")
    assert {"key": "core.variant", "default": "A", "value": "D50"} in found["owner_settings"]
    rule = next(r for r in found["remote_changeable"] if r["key"] == "thesis.caps.position")
    assert (rule["tighter"], rule["value"]) == ("lower", "0.25")
    assert found["service_runs_on_these"] is False


async def test_long_tables_are_capped_and_say_so(
    cockpit: Cockpit, loaded: LoadedConfig, store: Any
) -> None:
    capped = loaded.settings.model_copy(
        update={"server": loaded.settings.server.model_copy(update={"max_rows_per_response": 1})}
    )
    cockpit._settings = capped
    for _ in range(2):
        made = intent(loaded.settings)
        orders_repo.insert_intent(store, made, coid=str(made.intent_id), at=NOW)
    found = await cockpit.intents()
    assert (found["count"], len(found["intents"]), found["truncated"]) == (2, 1, True)


# ── commands ──


async def test_a_setting_is_previewed_then_changed_and_audited(
    mcp: FastMCP, config_dir: Path
) -> None:
    shown = await _call(
        mcp, "update_setting", key="thesis.caps.position", value="0.20", reason="tighter"
    )
    assert shown["confirmed"] is False
    assert "thesis.caps.position: 0.25 -> 0.20" in shown["preview"]
    done = await _call(
        mcp,
        "update_setting",
        key="thesis.caps.position",
        value="0.20",
        reason="tighter",
        confirm=True,
    )
    assert (done["confirmed"], done["audited"]) == (True, 1)
    assert load_config(config_dir).settings.thesis.caps.position == Decimal("0.20")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"key": "thesis.caps.position", "value": "0.30", "reason": "x"}, "loosens a risk limit"),
        (
            {"key": "thesis.max_positions", "value": "6", "reason": "x"},
            "cannot be changed remotely",
        ),
        ({"key": "thesis.caps.position", "value": "0.20", "reason": " "}, "needs a reason"),
    ],
)
async def test_a_remote_setting_change_that_loosens_is_refused_with_its_reason(
    mcp: FastMCP, arguments: dict[str, str], message: str
) -> None:
    with pytest.raises(ToolError, match=message):
        await mcp.call_tool("update_setting", arguments)


async def test_cash_is_previewed_then_recorded(mcp: FastMCP, store: Any) -> None:
    shown = await _call(mcp, "cash_deposit", amount="1237.57", currency="eur")
    assert shown == {
        "confirmed": False,
        "preview": "deposit of 1237.57 EUR on 2026-11-16",
        "next": "nothing was recorded: repeat the same command with confirm to record it",
    }
    assert (await _call(mcp, "cash_deposit", amount="1237.57", currency="EUR", confirm=True))[
        "recorded"
    ] == "F1"
    reserve = {"amount": "400", "currency": "EUR", "by": "2026-12-20", "reason": "a trip"}
    assert "investable capital drops" in (await _call(mcp, "cash_reserve", **reserve))["preview"]
    assert (await _call(mcp, "cash_reserve", **reserve, confirm=True))["recorded"] == "R1"
    listed = await _call(mcp, "list_cash")
    assert len(listed["reservations"]) == 1
    assert len(listed["flows"]) == 1


@pytest.mark.parametrize(
    ("amount", "on", "message"),
    [("abc", None, "is not an amount"), ("-5", None, "not a positive amount"), ("5", "soon", "")],
)
async def test_bad_cash_input_is_refused(
    mcp: FastMCP, amount: str, on: str | None, message: str
) -> None:
    with pytest.raises(ToolError, match=message or "not a date"):
        await mcp.call_tool("cash_deposit", {"amount": amount, "currency": "EUR", "on": on})


async def test_a_question_is_listed_previewed_and_answered(mcp: FastMCP, store: Any) -> None:
    question = await _question(mcp, store)
    listed = await _call(mcp, "list_questions")
    assert listed["questions"][0]["id"] == f"Q{question.question_id}"
    shown = await _call(mcp, "answer_question", question_id="Q1", option="proceed")
    assert shown["preview"] == "Q1: proceed: sell it"
    done = await _call(mcp, "answer_question", question_id="1", option="proceed", confirm=True)
    assert done["confirmed"] is True
    assert (await _call(mcp, "list_questions"))["questions"] == []
    assert len((await _call(mcp, "list_questions", include_closed=True))["questions"]) == 1


@pytest.mark.parametrize(
    ("question_id", "option", "message"),
    [
        ("Q9", "proceed", "there is no question Q9"),
        ("Q1", "later", "has no option"),
        ("x", "a", ""),
    ],
)
async def test_a_wrong_answer_is_refused(
    mcp: FastMCP, store: Any, question_id: str, option: str, message: str
) -> None:
    await _question(mcp, store)
    with pytest.raises(ToolError, match=message or "is not a question id"):
        await mcp.call_tool("answer_question", {"question_id": question_id, "option": option})


async def test_an_intent_held_for_approval_is_approved_on_confirm_or_rejected(
    mcp: FastMCP, loaded: LoadedConfig, store: Any
) -> None:
    held = []
    for _ in range(2):
        made = intent(loaded.settings).model_copy(update={"approval_required": True})
        orders_repo.insert_intent(store, made, coid=str(made.intent_id), at=NOW)
        held.append(str(made.intent_id))
    shown = await _call(mcp, "approve_intent", intent_id=held[0], reason="fine")
    assert shown["preview"] == "approve BUY conid 163606916 (core, core rebalance)"
    done = await _call(mcp, "approve_intent", intent_id=held[0], reason="fine", confirm=True)
    assert done["approved"] == held[0]
    rejected = await _call(mcp, "reject_intent", intent_id=held[1], reason="no")
    assert rejected == {"confirmed": True, "rejected": held[1]}
    with pytest.raises(ToolError, match="is not an intent id"):
        await mcp.call_tool("approve_intent", {"intent_id": "nope", "reason": "x"})
    with pytest.raises(ToolError, match="there is no intent"):
        await mcp.call_tool(
            "approve_intent", {"intent_id": "00000000-0000-7000-8000-000000000000", "reason": "x"}
        )


async def test_pause_and_resume(mcp: FastMCP) -> None:
    paused = await _call(mcp, "set_pause", paused=True, reason="a cloud session")
    assert paused["paused"] is True
    assert "a cloud session" in (await _call(mcp, "get_status"))["pause"]
    assert (await _call(mcp, "set_pause", paused=False))["paused"] is False
    with pytest.raises(ToolError, match="needs a reason"):
        await mcp.call_tool("set_pause", {"paused": True})


async def test_the_kill_switch_goes_on_at_once(mcp: FastMCP, data_dir: Path, store: Any) -> None:
    found = await _call(mcp, "kill_switch", reason="something is wrong")
    assert found["kill_switch"] == "ON"
    assert found["warnings"] == []
    assert file_flag_on(data_dir)
    latest = orders_repo.latest_kill_switch(store)
    assert latest is not None
    assert latest.on
    assert (await _call(mcp, "get_status"))["kill_switch"] == "ON"


async def test_the_kill_switch_holds_with_one_flag_and_says_when_neither_could_be_set(
    cockpit: Cockpit, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_file(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only")

    async def broken_store(*args: Any, **kwargs: Any) -> None:
        raise ObliskError("the store is gone")

    monkeypatch.setattr("obliskiq.server.cockpit.set_file_flag", broken_file)
    found = await cockpit.kill_switch(reason="x")
    assert found["warnings"] == ["the file flag could not be written: read-only"]
    monkeypatch.setattr(cockpit._worker, "run", broken_store)
    with pytest.raises(CockpitRefusal, match="kill switch NOT set"):
        await cockpit.kill_switch(reason="x")


# ── how failures reach the client ──


async def test_a_failure_is_named_and_never_a_traceback() -> None:
    async def obliskiq_error() -> dict[str, Any]:
        raise ObliskError("the store is locked")

    async def unexpected() -> dict[str, Any]:
        raise RuntimeError("secret detail")

    with pytest.raises(ToolError, match="get_x: the store is locked"):
        await call("get_x", obliskiq_error)
    with pytest.raises(ToolError, match=r"^get_x failed: RuntimeError$"):
        await call("get_x", unexpected)


# ── the HTTP app ──


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def test_the_http_app_guards_everything_but_its_health_check(
    cockpit: Cockpit, data_dir: Path
) -> None:
    app = build_http_app(build_server(cockpit), cockpit, token=TOKEN)
    async with _client(app) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/dashboard")).status_code == 401
        assert (await client.post("/mcp", json={})).status_code == 401
        wrong = {"Authorization": "Basic abc"}
        assert (await client.get("/dashboard", headers=wrong)).status_code == 401
        good = {"Authorization": f"Bearer {TOKEN}"}
        missing = await client.get("/dashboard", headers=good)
        assert missing.status_code == 404
        assert "no dashboard yet" in missing.text
        (data_dir / "dashboard.html").write_text("<p>today</p>", encoding="utf-8")
        assert (await client.get("/dashboard", headers=good)).text == "<p>today</p>"


async def test_without_a_token_the_app_is_open_on_loopback(cockpit: Cockpit) -> None:
    app = build_http_app(build_server(cockpit), cockpit, token=None)
    async with _client(app) as client:
        assert (await client.get("/dashboard")).status_code == 404


def test_an_empty_token_never_authenticates() -> None:
    with pytest.raises(ValueError, match="without a token"):
        BearerTokenMiddleware(server_app.build_http_app, token="")


async def test_grid_and_board_cells_carry_each_leaf_with_its_status(
    mcp: FastMCP, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    leaf = SimpleNamespace(value=0.21, status="ok", reason=None, as_of=NOW.date())
    grid = SimpleNamespace(
        as_of=NOW.date(),
        session=NOW.date(),
        cells={
            ("US", "tech"): SimpleNamespace(
                market="US", sector="tech", disclosure={"symbol": "XLK"}, leaves={"rv_20": leaf}
            )
        },
    )
    board = SimpleNamespace(
        as_of=NOW.date(),
        session=NOW.date(),
        cells={
            ("EU", "equity"): SimpleNamespace(
                region="EU", asset_class="equity", inputs="h", leaves={"trend": leaf}
            )
        },
    )
    monkeypatch.setattr("obliskiq.server.cockpit.equity_grid", lambda *_a, **_k: grid)
    monkeypatch.setattr("obliskiq.server.cockpit.macro_board", lambda *_a, **_k: board)
    cell = (await _call(mcp, "get_grid_view"))["cells"][0]
    assert cell["leaves"]["rv_20"] == {
        "value": 0.21,
        "status": "ok",
        "reason": None,
        "as_of": "2026-11-16",
    }
    assert (await _call(mcp, "get_macro_board"))["cells"][0]["region"] == "EU"


async def test_a_kill_switch_the_store_cannot_record_still_holds_by_its_file(
    cockpit: Cockpit, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_store(*args: Any, **kwargs: Any) -> None:
        raise ObliskError("the store is gone")

    monkeypatch.setattr(cockpit._worker, "run", broken_store)
    found = await cockpit.kill_switch(reason="x")
    assert found["warnings"] == ["the database flag could not be written: ObliskError"]
    assert file_flag_on(data_dir)


async def test_a_second_remote_change_is_proposed_against_the_first(
    mcp: FastMCP, data_dir: Path
) -> None:
    for value in ("0.20", "0.15"):
        done = await _call(
            mcp,
            "update_setting",
            key="thesis.caps.position",
            value=value,
            reason="tighter",
            confirm=True,
        )
        assert done["audited"] == 1
    from obliskiq.config.audit import SettingsAuditLog

    entries = SettingsAuditLog(data_dir / "settings-audit.jsonl").entries()
    assert [(e.old, e.new) for e in entries] == [("0.25", "0.20"), ("0.20", "0.15")]
