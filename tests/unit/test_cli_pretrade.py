"""``obliskiq flatten``, ``obliskiq preflight --paper-order-test`` and ``obliskiq approvals``,
run in-process. The brokerage session is replaced: these tests check the command's own
branches; the order path itself runs on the fake broker elsewhere.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from obliskiq.cli import app
from obliskiq.cli.app import EXIT_INVALID, EXIT_NO_CREDENTIALS, EXIT_OK, run
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
from obliskiq.errors import ExecutionError
from obliskiq.execution.flatten import FlattenLine, FlattenReport
from obliskiq.execution.intents import new_intent
from obliskiq.execution.paper_test import PaperOrderReport
from obliskiq.store import open_store
from obliskiq.store import orders as repo


@pytest.fixture(autouse=True)
def _restore_root_logging():
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
def credentials(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    for name, value in {
        "OBLISK_IBKR_PROFILE": "paper",
        "OBLISK_IBKR_ACCOUNT_ID": "DUTEST1",
        "OBLISK_IBKR_TRANSPORT": "gateway",
        "OBLISK_IBKR_GATEWAY_USERNAME": "paper-user",
        "OBLISK_IBKR_GATEWAY_PASSWORD": "paper-pass",
    }.items():
        monkeypatch.setenv(name, value)


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue()


class FakePath:
    """What a command reaches through the order path, scripted."""

    def __init__(self, report: FlattenReport) -> None:
        self.flattener = self
        self.report = report

    async def flatten(self, *, actor: str) -> FlattenReport:
        assert actor == app.OWNER_ACTOR
        return self.report


def _open_with(path: Any, opened: list[Any] | None = None) -> Any:
    @asynccontextmanager
    async def opener(env: Any, settings: Any, profile: Any, *, data_dir: Path) -> Any:
        if opened is not None:
            opened.append((profile.id, data_dir))
        if isinstance(path, Exception):
            raise path
        yield path

    return opener


# ── flatten ──


def test_flatten_does_nothing_without_confirm(credentials: None) -> None:
    status, out, _ = _run("flatten")
    assert status == EXIT_INVALID
    assert "nothing was done" in out


def test_flatten_without_credentials_attempts_nothing(data_dir: Path) -> None:
    status, out, _ = _run("flatten", "--confirm")
    assert status == EXIT_NO_CREDENTIALS
    assert out.startswith("no credentials")


def test_flatten_refuses_a_transport_the_profile_does_not_allow(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "live")
    status, out, _ = _run("flatten", "--confirm")
    assert status == EXIT_INVALID
    assert out.startswith("refused:")


def test_flatten_prints_what_it_did(
    credentials: None, monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    closing = FlattenLine(8158, Decimal(10), True, "closing")
    opened: list[Any] = []
    monkeypatch.setattr(
        app, "open_order_path", _open_with(FakePath(FlattenReport((closing,))), opened)
    )
    status, out, _ = _run("flatten", "--confirm")
    assert status == EXIT_OK
    assert "conid 8158 (10): closing" in out
    assert opened == [("es", data_dir)]
    refused = FlattenLine(8158, Decimal(10), False, "refused (collar)")
    monkeypatch.setattr(app, "open_order_path", _open_with(FakePath(FlattenReport((refused,)))))
    status, out, _ = _run("flatten", "--confirm")
    assert status == EXIT_INVALID


def test_flatten_reports_an_order_path_that_does_not_open(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "open_order_path", _open_with(ExecutionError("the login failed")))
    status, out, _ = _run("flatten", "--confirm")
    assert status == EXIT_INVALID
    assert out.strip() == "flatten stopped: the login failed"


# ── the paper order test ──


def test_the_paper_order_test_without_credentials_attempts_nothing(data_dir: Path) -> None:
    status, out, _ = _run("preflight", "--paper-order-test")
    assert status == EXIT_NO_CREDENTIALS
    assert "OBLISK_IBKR_ACCOUNT_ID" in out


def test_the_two_preflights_are_exclusive(credentials: None) -> None:
    with pytest.raises(SystemExit):
        _run("preflight", "--read-only", "--paper-order-test")
    with pytest.raises(SystemExit):
        _run("preflight")


def test_the_paper_order_test_prints_its_report_with_the_account_masked(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeTest:
        def __init__(self, path: Any) -> None:
            self.path = path

        async def run(self) -> PaperOrderReport:
            report = PaperOrderReport()
            report.add("EXV1 place", True, "placed for DUTEST1")
            return report

    monkeypatch.setattr(app, "open_order_path", _open_with(object()))
    monkeypatch.setattr(app, "PaperOrderTest", FakeTest)
    status, out, _ = _run("preflight", "--paper-order-test")
    assert status == EXIT_OK
    assert "IBKR paper order test" in out
    assert "DUTEST1" not in out


def test_a_paper_order_test_that_cannot_start_says_so(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "open_order_path", _open_with(ExecutionError("no session")))
    status, out, _ = _run("preflight", "--paper-order-test")
    assert status == EXIT_INVALID
    assert "paper order test stopped: no session" in out


# ── approvals ──


def _held_intent(config_dir: Path, data_dir: Path) -> uuid.UUID:
    settings = load_config(config_dir).settings
    now = datetime.now(UTC)
    intent = new_intent(
        settings,
        sleeve=Sleeve.CORE,
        role=IntentRole.ENTRY,
        terms=OrderTerms(
            conid=8158,
            side=OrderSide.BUY,
            quantity=Decimal(1),
            price=LimitPrice(limit=Decimal(30)),
            tif=TimeInForce.DAY,
            venue="SMART",
        ),
        reason="entry",
        mode=ExecutionMode.PAPER,
        approval_required=True,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    with open_store(settings, data_dir) as store:
        repo.insert_intent(store, intent, coid=str(intent.intent_id), at=now)
    return intent.intent_id


def test_the_owner_lists_and_approves_held_intents(data_dir: Path, config_dir: Path) -> None:
    status, out, _ = _run("approvals", "list")
    assert (status, out.strip()) == (EXIT_OK, "0 intent(s) waiting for approval")
    held = _held_intent(config_dir, data_dir)
    status, out, _ = _run("approvals", "list")
    assert out.startswith("1 intent(s) waiting for approval")
    assert str(held) in out
    status, out, _ = _run("approvals", "approve", str(held), "--reason", "checked")
    assert (status, out.strip()) == (EXIT_OK, f"intent {held} approved")
    settings = load_config(config_dir).settings
    with open_store(settings, data_dir) as store:
        stored = repo.get_intent(store, held)
        decision = repo.approval_for(store, held)
    assert stored is not None
    assert stored.intent.state is IntentState.APPROVED
    assert decision is not None
    assert decision.actor == app.OWNER_ACTOR


def test_the_owner_rejects_a_held_intent(data_dir: Path, config_dir: Path) -> None:
    held = _held_intent(config_dir, data_dir)
    status, out, _ = _run("approvals", "reject", str(held), "--reason", "not now")
    assert (status, out.strip()) == (EXIT_OK, f"intent {held} rejected")
    status, out, _ = _run("approvals", "approve", str(held), "--reason", "changed my mind")
    assert status == EXIT_INVALID
    assert "only a proposed intent" in out


def test_an_approval_names_an_intent_id(data_dir: Path) -> None:
    status, out, _ = _run("approvals", "approve", "not-an-id", "--reason", "x")
    assert status == EXIT_INVALID
    assert "is not an intent id" in out
    status, out, _ = _run("approvals", "approve", str(uuid.uuid7()), "--reason", "x")
    assert status == EXIT_INVALID
    assert "no intent" in out
