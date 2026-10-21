"""``obliskiq run-cycle``, ``serve``, ``status``, ``intents list`` and ``holdings``, in-process.

``run-cycle --mode shadow --dry-run`` runs on fixtures: the real order path over the fake broker
(only the transport is the test's), printing the plan and writing nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from fake_broker import ACCOUNT, FakeBroker
from service_rig import skeleton_market

from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import load_config
from obliskiq.domain.orders import (
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderSide,
    OrderTerms,
    Sleeve,
    TimeInForce,
)
from obliskiq.execution.intents import new_intent
from obliskiq.orchestration.health import write_heartbeat
from obliskiq.orchestration.lock import ServiceLock
from obliskiq.store import open_store
from obliskiq.store import orders as repo
from obliskiq.store import runs as run_repo


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


@pytest.fixture
def broker_env(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, fake_transport: Any
) -> Iterator[FakeBroker]:
    """A paper Gateway environment whose transport is the test's, over the fake broker."""
    from obliskiq.execution import runtime

    for name, value in {
        "OBLISK_IBKR_PROFILE": "paper",
        "OBLISK_IBKR_ACCOUNT_ID": ACCOUNT,
        "OBLISK_IBKR_TRANSPORT": "gateway",
        "OBLISK_IBKR_GATEWAY_USERNAME": "paper-user",
        "OBLISK_IBKR_GATEWAY_PASSWORD": "paper-pass",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(runtime, "build_transport", lambda *_a, **_k: fake_transport)
    with respx.mock(assert_all_called=False) as router:
        broker = FakeBroker(router)
        skeleton_market(broker)
        yield broker


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue()


def _settings(config_dir: Path) -> Any:
    return load_config(config_dir).settings


# ── run-cycle ──


def test_a_dry_run_on_fixtures_prints_a_sensible_plan_and_writes_nothing(
    broker_env: FakeBroker, data_dir: Path, config_dir: Path
) -> None:
    status, out, err = _run("run-cycle", "--mode", "shadow", "--dry-run")
    assert status == EXIT_OK, out + err
    assert "rebalance plan (shadow)" in out
    assert "managed capital 1000.00 (cap: a book of 1000.00 (started at 1000))" in out
    for role in ("world_equity", "em_equity", "euro_govt_bonds"):
        assert role in out
    assert "NAV: 100000" in out
    assert "dry run: nothing was written or sent" in out
    assert broker_env.placements == []
    settings = _settings(config_dir)
    with open_store(settings, data_dir) as store:
        assert repo.intents_in_states(store, list(IntentState)) == []
        assert run_repo.runs_since(store, since=date(2000, 1, 1)) == []


def test_a_cycle_in_another_mode_than_the_stores_is_refused(broker_env: FakeBroker) -> None:
    status, out, _ = _run("run-cycle", "--mode", "paper")
    assert status == EXIT_INVALID
    assert "the mode is shadow, not paper" in out
    assert broker_env.placements == []


def test_a_cycle_runs_and_records_itself(
    broker_env: FakeBroker, data_dir: Path, config_dir: Path
) -> None:
    status, out, _ = _run("run-cycle", "--mode", "shadow")
    assert "cycle " in out
    assert status in (EXIT_OK, 2)  # ok or degraded, by what the real clock's venues allow
    with open_store(_settings(config_dir), data_dir) as store:
        (record,) = run_repo.runs_since(store, since=date(2000, 1, 1))
    assert record.command == "cycle:manual"
    assert [s["step"] for s in record.detail["steps"]] == ["nav", "reserve", "rebalance", "execute"]


def test_a_command_that_opens_the_broker_refuses_while_another_holds_the_lock(
    broker_env: FakeBroker, data_dir: Path, config_dir: Path
) -> None:
    settings = _settings(config_dir)
    with ServiceLock(data_dir / settings.paths.service_lock, command="serve"):
        status, out, _ = _run("run-cycle", "--mode", "shadow", "--dry-run")
        assert status == EXIT_INVALID
        assert "another obliskiq process runs the executor" in out
        assert "serve" in out
        status, out, _ = _run("reconcile", "run")
        assert status == EXIT_INVALID
        assert "another obliskiq process" in out
    assert _run("run-cycle", "--mode", "shadow", "--dry-run")[0] == EXIT_OK


def test_run_cycle_and_serve_need_credentials(data_dir: Path) -> None:
    status, out, _ = _run("run-cycle", "--mode", "shadow")
    assert status != EXIT_OK
    assert "no credentials" in out
    status, out, _ = _run("serve")
    assert status != EXIT_OK
    assert "no credentials" in out


def test_a_cycle_whose_path_cannot_open_says_so(
    broker_env: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBLISK_IBKR_ACCOUNT_ID", "U1234567")  # not a paper account
    status, out, _ = _run("run-cycle", "--mode", "shadow", "--dry-run")
    assert status == EXIT_INVALID
    assert "the cycle stopped" in out


def test_a_cycle_that_broke_before_planning_prints_only_its_verdict(
    broker_env: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.orchestration.jobs import CycleResult, Jobs

    async def broken(self: Jobs, name: str, session_date: date, **_: Any) -> CycleResult:
        return CycleResult(status="error")

    monkeypatch.setattr(Jobs, "cycle", broken)
    status, out, _ = _run("run-cycle", "--mode", "shadow", "--dry-run")
    assert status != EXIT_OK
    assert "rebalance plan" not in out
    assert "NAV:" not in out
    assert "cycle error" in out


# ── serve ──


def test_serve_runs_the_service_until_it_stops(
    broker_env: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    import obliskiq.cli.service as cli

    ran: list[str] = []

    class FakeService:
        async def run(self, stop: Any) -> None:
            ran.append("ran")

    @asynccontextmanager
    async def opened(*args: Any, **kwargs: Any) -> AsyncIterator[FakeService]:
        yield FakeService()

    monkeypatch.setattr(cli, "open_service", opened)
    status, out, _ = _run("serve")
    assert (status, ran) == (EXIT_OK, ["ran"])
    assert "obliskiq service stopped" in out


def test_serve_reports_a_service_that_cannot_start(
    broker_env: FakeBroker, monkeypatch: pytest.MonkeyPatch, data_dir: Path, config_dir: Path
) -> None:
    monkeypatch.setenv("OBLISK_IBKR_ACCOUNT_ID", "U1234567")
    status, out, _ = _run("serve")
    assert status == EXIT_INVALID
    assert "the service stopped" in out
    settings = _settings(config_dir)
    with ServiceLock(data_dir / settings.paths.service_lock, command="run-cycle"):
        status, out, _ = _run("serve")
    assert status == EXIT_INVALID
    assert "refused: another obliskiq process" in out


# ── status, intents and holdings (no broker session) ──


def test_status_on_an_empty_store_says_nothing_has_run(data_dir: Path) -> None:
    status, out, _ = _run("status")
    assert status == EXIT_OK
    assert "mode shadow · kill switch off" in out
    assert "heartbeat: none recorded" in out
    assert "reconciliation: none yet" in out
    assert "NAV: none recorded" in out
    assert "none. Nothing has run" in out


def test_status_shows_the_heartbeat_runs_and_alerts(data_dir: Path, config_dir: Path) -> None:
    settings = _settings(config_dir)
    now = datetime.now(UTC)
    write_heartbeat(
        data_dir / settings.paths.heartbeat_file,
        now - timedelta(hours=1),
        {"status": "ok", "session": "authenticated"},
    )
    (data_dir / settings.paths.alert_state).write_text(
        '{"held": {"service.venue_blocked:XAMS": {"kind": "k", "sources": [], "sent": true, '
        '"since": ""}}}',
        encoding="utf-8",
    )
    with open_store(settings, data_dir) as store:
        run_repo.begin_run(
            store,
            run_id="r1",
            command="cycle:europe",
            session_date=now.date(),
            started_at=now,
            steps=("nav",),
        )
    status, out, _ = _run("status")
    assert status == EXIT_OK
    assert "STALE" in out
    assert "session authenticated" in out
    assert "open alerts: 1" in out
    assert "service.venue_blocked:XAMS" in out
    assert "! " in out
    assert "cycle:europe" in out
    assert "running" in out
    (data_dir / settings.paths.alert_state).write_text("not json", encoding="utf-8")
    assert "open alerts: 0" in _run("status")[1]


def test_intents_are_listed_open_or_all(data_dir: Path, config_dir: Path) -> None:
    settings = _settings(config_dir)
    now = datetime.now(UTC)
    intent = new_intent(
        settings,
        sleeve=Sleeve.CORE,
        role=IntentRole.ENTRY,
        terms=OrderTerms(
            conid=43614383,
            side=OrderSide.BUY,
            quantity=3,
            price=LimitPrice(limit=177),
            tif=TimeInForce.DAY,
            venue="SMART",
        ),
        reason="rebalance",
        mode=ExecutionMode.SHADOW,
        approval_required=False,
        created_at=now,
        expires_at=now + timedelta(hours=8),
    )
    with open_store(settings, data_dir) as store:
        repo.insert_intent(store, intent, coid=str(intent.intent_id), at=now)
    status, out, _ = _run("intents", "list")
    assert status == EXIT_OK
    assert "1 intent(s) open" in out
    assert "BUY 3 conid 43614383 limit 177" in out
    assert "1 intent(s)\n" in _run("intents", "list", "--all")[1]


def test_holdings_are_assigned_and_listed(data_dir: Path) -> None:
    status, out, _ = _run("holdings", "assign", "43614383", "core", "--reason", "moved in")
    assert (status, out.strip()) == (EXIT_OK, "conid 43614383 assigned to core")
    assert _run("holdings", "assign", "7089", "unmanaged", "--reason", "mine")[0] == EXIT_OK
    status, out, _ = _run("holdings", "assign", "1", "pension", "--reason", "r")
    assert status == EXIT_INVALID
    assert "is not one of core" in out
    status, out, _ = _run("holdings", "list")
    assert status == EXIT_OK
    assert "2 holding(s) registered" in out
    assert "conid 7089: unmanaged" in out
    assert "conid 43614383: core" in out
