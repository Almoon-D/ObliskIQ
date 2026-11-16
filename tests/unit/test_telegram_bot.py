"""The owner's Telegram bot: the owner's chat only, every command, free text through the grammar,
``/confirm`` codes that expire, questions pushed once, and failures that never raise or leak the
token. The Bot API is mocked with respx.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from board_rig import digest, intent, performance
from pydantic import SecretStr

from obliskiq.config import LoadedConfig, load_config
from obliskiq.config.changes import TELEGRAM
from obliskiq.domain.capital import OptionEffect, OwnerQuestion, QuestionKind, QuestionOption
from obliskiq.env import Environment
from obliskiq.execution.killswitch import file_flag_on
from obliskiq.orchestration.health import write_heartbeat
from obliskiq.reporting.board import build_board, write_board
from obliskiq.server.cockpit import Cockpit
from obliskiq.server.telegram import HELP, TelegramBot
from obliskiq.store import capital as capital_repo
from obliskiq.store import open_store
from obliskiq.store import orders as orders_repo
from obliskiq.store.worker import StoreWorker

NOW = datetime(2026, 11, 16, 21, 30, tzinfo=UTC)
TOKEN = "123:secret-token"
CHAT = "4242"
API = f"https://api.telegram.org/bot{TOKEN}"
SENT = {"ok": True, "result": {"message_id": 1}}


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def loaded(config_dir: Path) -> LoadedConfig:
    return load_config(config_dir)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    return path


@pytest.fixture
def store(loaded: LoadedConfig, data_dir: Path) -> Iterator[Any]:
    with open_store(loaded.settings, data_dir) as opened:
        yield opened


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def bot(
    loaded: LoadedConfig, data_dir: Path, store: Any, clock: Clock
) -> AsyncIterator[TelegramBot]:
    worker = StoreWorker(lambda: open_store(loaded.settings, data_dir))
    cockpit = Cockpit(
        loaded,
        Environment(_env_file=None),  # type: ignore[call-arg]
        worker=worker,
        actor="owner (Telegram)",
        channel=TELEGRAM,
        now=clock,
    )
    async with httpx.AsyncClient() as client:
        yield TelegramBot(
            cockpit,
            loaded.settings,
            token=SecretStr(TOKEN),
            chat_id=SecretStr(CHAT),
            state_path=data_dir / "telegram-state.json",
            now=clock,
            client=client,
            code=lambda: "123456",
        )
    await worker.aclose()


def _sent(router: respx.Route) -> list[str]:
    return [json.loads(call.request.content)["text"] for call in router.calls]


# ── commands ──


async def test_reads_answer_in_plain_words(
    bot: TelegramBot, loaded: LoadedConfig, data_dir: Path, store: Any
) -> None:
    status = await bot.handle("/status")
    assert "kill switch off · not paused" in status
    assert "heartbeat: none recorded" in status
    write_heartbeat(
        data_dir / "heartbeat.json", NOW - timedelta(hours=1), {"status": "ok", "session": "open"}
    )
    assert "STALE" in await bot.handle("/status@obliskiq_bot")
    assert "no board yet" in await bot.handle("/positions")
    board = build_board(
        digest(loaded.settings), loaded.settings, performance=performance(), shares={}, patterns=[]
    )
    write_board(data_dir / "board.json", board)
    positions = await bot.handle("/positions")
    assert "world_equity XDWD: held 5 = 612.50" in positions
    assert "no rebalance now" in positions
    made = intent(loaded.settings)
    orders_repo.insert_intent(store, made, coid=str(made.intent_id), at=NOW)
    assert "1 open intent(s)" in await bot.handle("/intents")
    assert "0 open thesis(es)" in await bot.handle("/theses")
    assert await bot.handle("/questions") == "no question waits for you"
    settings = await bot.handle("/settings")
    assert "core.variant: D50" in settings
    assert "thesis.caps.position = 0.25 (only lower)" in settings
    assert "between 0 and 0.05" in settings
    assert "The service runs on other settings" in settings
    assert await bot.handle("/nonsense") == HELP


async def test_a_stale_or_empty_board_is_labelled(
    bot: TelegramBot, loaded: LoadedConfig, data_dir: Path
) -> None:
    from board_rig import plan

    bare = plan()
    bare.capital = None
    bare.decisions = []
    bare.blocked = None
    old = NOW - timedelta(days=6)
    board = build_board(
        digest(loaded.settings, plan=bare, at=old, day=old.date()),
        loaded.settings,
        performance=performance(),
        shares={},
        patterns=[],
    )
    write_board(data_dir / "board.json", board)
    positions = await bot.handle("/positions")
    assert "STALE" in positions
    assert "no position and no target" in positions


async def test_a_command_is_echoed_and_done_only_on_its_code(
    bot: TelegramBot, clock: Clock, config_dir: Path
) -> None:
    echoed = await bot.handle("/set thesis.caps.position 0.20 tighter please")
    assert "thesis.caps.position: 0.25 -> 0.20" in echoed
    assert "/confirm 123456 within 10 minutes" in echoed
    assert await bot.handle("/confirm 999999") == (
        "refused: no command waits for that code (it may have expired); send it again"
    )
    done = await bot.handle("/confirm 123456")
    assert done.startswith("done: changed: thesis.caps.position: 0.25 -> 0.20")
    assert load_config(config_dir).settings.thesis.caps.position == Decimal("0.20")
    assert "refused" in await bot.handle("/confirm 123456")  # a code is used once


async def test_an_expired_code_does_nothing(bot: TelegramBot, clock: Clock, store: Any) -> None:
    await bot.handle("/deposit 500")
    clock.at = NOW + timedelta(minutes=11)
    assert "refused: no command waits" in await bot.handle("/confirm 123456")
    assert capital_repo.cash_flows(store, as_of=NOW.date()) == []


async def test_cash_commands_and_free_text(bot: TelegramBot, store: Any) -> None:
    assert "deposit of 500 USD" in await bot.handle("/deposit 500 usd")
    assert "deposit of 1237.57 EUR" in await bot.handle("I added 1237,57€ to your cash")
    assert (await bot.handle("/confirm 123456")).startswith("done: recorded: F1")
    withdraw = await bot.handle("/withdraw 400 EUR 2026-12-20 a trip")
    assert "withdrawal of 400 EUR by 2026-12-20 (a trip)" in withdraw
    plain = await bot.handle("/withdraw 300 2026-12-21")
    assert "withdrawal of 300 EUR by 2026-12-21 (withdrawal announced on Telegram)" in plain
    spanish = await bot.handle("en 15 días necesito retirar 1.000 €")
    assert "withdrawal of 1000 EUR by 2026-12-01" in spanish
    assert "thesis.caps.position: 0.25 -> 0.2" in await bot.handle("pon thesis.caps.position a 0.2")
    assert (await bot.handle("hello there")).startswith("I did not understand that.")


async def test_answers_and_their_refusals(bot: TelegramBot, store: Any) -> None:
    assert await bot.handle("/answer Q1 proceed") == "refused: there is no question Q1"
    await bot.handle("/withdraw 400 EUR 2026-12-20")
    await bot.handle("/confirm 123456")
    _question(store)
    assert "Q1: proceed: sell it" in await bot.handle("/answer Q1 proceed")
    assert "Q1: proceed: sell it" in await bot.handle("Q1 proceed")
    assert (await bot.handle("/confirm 123456")).startswith("done: answered: Q1")
    assert "Q1 by" not in await bot.handle("/questions")


async def test_pause_resume_and_kill_act_at_once(bot: TelegramBot, data_dir: Path) -> None:
    assert "a cloud session" in await bot.handle("/pause a cloud session")
    assert await bot.handle("/pause off") == "not paused"
    assert "/pause from Telegram" in await bot.handle("/pause")
    assert (await bot.handle("/kill")).startswith("kill switch ON.")
    assert file_flag_on(data_dir)


async def test_a_failure_inside_a_command_is_reported(
    bot: TelegramBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.errors import ObliskError

    async def broken() -> dict[str, Any]:
        raise ObliskError("the store is locked")

    monkeypatch.setattr(bot._cockpit, "status", broken)
    assert await bot.handle("/status") == "failed: the store is locked"


# ── the loop ──


def _question(store: Any) -> OwnerQuestion:
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


@respx.mock
async def test_only_the_owners_chat_is_served_and_the_offset_persists(
    bot: TelegramBot, data_dir: Path
) -> None:
    updates = respx.post(f"{API}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 7, "message": {"chat": {"id": 999}, "text": "/kill"}},
                    {"update_id": 8, "message": {"chat": {"id": int(CHAT)}, "text": "/status"}},
                    {"update_id": 9, "message": {"chat": {"id": int(CHAT)}}},
                ],
            },
        )
    )
    sent = respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(200, json=SENT))
    assert await bot.poll_once() == 1
    assert json.loads(updates.calls[0].request.content)["offset"] == 0
    assert len(sent.calls) == 1
    assert "mode" in _sent(sent)[0]
    assert not file_flag_on(data_dir)
    state = json.loads((data_dir / "telegram-state.json").read_text())
    assert state["offset"] == 10
    await bot.poll_once()
    assert json.loads(updates.calls[1].request.content)["offset"] == 10


@respx.mock
async def test_a_failed_poll_is_none_and_never_raises(bot: TelegramBot) -> None:
    respx.post(f"{API}/getUpdates").mock(side_effect=httpx.ConnectError("down"))
    assert await bot.poll_once() is None
    respx.post(f"{API}/getUpdates").mock(return_value=httpx.Response(409))
    assert await bot.poll_once() is None
    respx.post(f"{API}/getUpdates").mock(return_value=httpx.Response(200, content=b"not json"))
    assert await bot.poll_once() is None


@respx.mock
async def test_open_questions_are_pushed_once(bot: TelegramBot, store: Any) -> None:
    sent = respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(200, json=SENT))
    assert await bot.push_questions() == 0
    await bot.handle("/withdraw 400 EUR 2026-12-20")
    await bot.handle("/confirm 123456")
    _question(store)
    assert await bot.push_questions() == 1
    text = _sent(sent)[0]
    assert "Question Q1: R1 needs 400 EUR" in text
    assert "proceed (default): sell it (tax 12.5, strategy 20)" in text
    assert "postpone: later (tax —, strategy —)" in text
    assert await bot.push_questions() == 0


@respx.mock
async def test_a_question_that_could_not_be_sent_is_pushed_again(
    bot: TelegramBot, store: Any
) -> None:
    respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(500))
    await bot.handle("/withdraw 400 EUR 2026-12-20")
    await bot.handle("/confirm 123456")
    _question(store)
    assert await bot.push_questions() == 0
    respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(200, json=SENT))
    assert await bot.push_questions() == 1


@respx.mock
async def test_a_long_reply_is_split_under_the_cap(
    bot: TelegramBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(200, json=SENT))
    chunk = bot._settings.reporting.telegram.chunk_characters
    long_line = "x" * (chunk + 10)
    assert await bot.send(f"short\n{long_line}\n" + "y\n" * 3)
    parts = _sent(sent)
    assert parts[0] == "short\n"
    assert all(len(part) <= chunk + 1 for part in parts)
    assert "".join(parts).count("x") == chunk + 10
    many = "\n".join(["z" * 100] * (chunk // 100 + 5))
    before = len(sent.calls)
    assert await bot.send(many)
    assert len(sent.calls) - before == 2


async def test_the_loop_runs_until_stopped_and_waits_after_a_failure(
    bot: TelegramBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    calls: list[str] = []
    bot._telegram = bot._telegram.model_copy(update={"retry_seconds": 0.01})

    async def push() -> int:
        calls.append("push")
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 0

    async def poll() -> int | None:
        calls.append("poll")
        if calls.count("poll") == 1:
            return None
        stop.set()
        return 0

    monkeypatch.setattr(bot, "push_questions", push)
    monkeypatch.setattr(bot, "poll_once", poll)
    await asyncio.wait_for(bot.run(stop), timeout=5)
    assert calls == ["push", "push", "poll", "push", "poll"]


async def test_a_corrupt_state_file_starts_afresh(
    loaded: LoadedConfig, data_dir: Path, clock: Clock
) -> None:
    (data_dir / "telegram-state.json").write_text("[1, 2]", encoding="utf-8")
    worker = StoreWorker(lambda: open_store(loaded.settings, data_dir))
    cockpit = Cockpit(
        loaded, Environment(_env_file=None), worker=worker, actor="o", channel=TELEGRAM,  # type: ignore[call-arg]
    )  # fmt: skip
    fresh = TelegramBot(
        cockpit,
        loaded.settings,
        token=SecretStr(TOKEN),
        chat_id=SecretStr(CHAT),
        state_path=data_dir / "telegram-state.json",
        now=clock,
    )
    assert fresh._offset == 0
    with respx.mock:
        respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(200, json=SENT))
        assert await fresh.send("hello")  # its own client, opened and closed per call
    await worker.aclose()


def test_a_confirmation_code_has_six_digits() -> None:
    from obliskiq.server.telegram import _code

    code = _code()
    assert len(code) == 6
    assert code.isdigit()


@respx.mock
async def test_an_empty_reply_sends_nothing_and_a_long_first_line_is_cut(bot: TelegramBot) -> None:
    sent = respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(200, json=SENT))
    assert await bot.send("")
    assert sent.calls == []
    chunk = bot._settings.reporting.telegram.chunk_characters
    assert await bot.send("x" * (chunk + 1))
    assert [len(part) for part in _sent(sent)] == [chunk, 2]


async def test_open_questions_are_listed_and_the_settings_in_force_said(
    bot: TelegramBot, store: Any, loaded: LoadedConfig, data_dir: Path
) -> None:
    from obliskiq.config.changes import settings_fingerprint

    await bot.handle("/withdraw 400 EUR 2026-12-20")
    await bot.handle("/confirm 123456")
    _question(store)
    listed = await bot.handle("/questions")
    assert listed.startswith("Q1 by")
    assert "options proceed, postpone" in listed
    write_heartbeat(
        data_dir / "heartbeat.json", NOW, {"settings": settings_fingerprint(loaded.settings)}
    )
    assert "The service runs on other settings" not in await bot.handle("/settings")
