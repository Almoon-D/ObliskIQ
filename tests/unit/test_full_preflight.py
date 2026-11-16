"""``obliskiq preflight --full`` on the fake broker: every step in order, a whatif for the core
vehicle's minimum quantity that is never placed, the Telegram test when it is configured, and the
walk stopping at the first failure. The session's status and the history are stubbed on the client
(the fake broker serves the order path's endpoints).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from order_rig import EUR_ETF, whatif_answer

from obliskiq.cli import app as cli_app
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.errors import ObliskError
from obliskiq.execution.full_preflight import FULL_STEPS, FullPreflight
from obliskiq.execution.runtime import OrderPath
from obliskiq.ibkr.preflight import PreflightReport
from obliskiq.jurisdiction import load_profile

REPO = Path(__file__).resolve().parents[2]


class Telegram:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send(self, subject: str, body: str) -> bool:
        self.sent.append(subject)
        return self.ok

    async def send_document(self, caption: str, path: Path) -> bool:
        return self.ok


def path_of(rig: Any) -> OrderPath:
    preflight = rig.settings.ibkr.preflight.model_copy(update={"whatif_conid": EUR_ETF})
    ibkr = rig.settings.ibkr.model_copy(update={"preflight": preflight})
    return OrderPath(
        settings=rig.settings.model_copy(update={"ibkr": ibkr}),
        client=rig.ibkr,
        orders=rig.client,
        worker=rig.worker,
        book=rig.book,
        modes=rig.modes,
        kill=rig.kill,
        boundary=rig.boundary,
        gate=rig.gate,
        coordinator=rig.coordinator,
        reader=rig.reader,
        directory=rig.directory,
        calendars=rig.calendars,
        approvals=rig.approvals,
        flattener=rig.flattener,
        pricer=rig.pricer,
        currency=rig.currency,
        protective=rig.protective,
        reconciler=rig.reconciler,
        executor=rig.executor,
        data_dir=rig.data_dir,
        profile=load_profile(REPO / "config", "es"),
    )


@pytest.fixture
def rig(order_rig: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    order_rig.ready()
    order_rig.broker.contracts = {EUR_ETF: ("EXV1", "IBIS", "EUR")}
    order_rig.broker.whatif_for = lambda _order: whatif_answer("95.90", "EUR")

    async def status() -> Any:
        return SimpleNamespace(authenticated=True, competing=False)

    async def bars(conid: int, *, period: str, bar: str) -> Any:
        return SimpleNamespace(bars=[1, 2, 3])

    monkeypatch.setattr(order_rig.ibkr.session, "auth_status", status)
    monkeypatch.setattr(order_rig.ibkr.history, "bars", bars)
    return order_rig


def steps(report: PreflightReport) -> dict[str, Any]:
    return {step.name: step for step in report.steps}


async def test_every_step_passes_and_nothing_is_placed(rig: Any) -> None:
    telegram = Telegram()
    report = await FullPreflight(path_of(rig), telegram=telegram, now=rig.clock).run()
    assert report.ok, report.render()
    assert [step.name for step in report.steps] == list(FULL_STEPS)
    found = steps(report)
    assert found["auth"].detail == "authenticated"
    assert "EXV1 (89005163): availability" in found["snapshot"].detail
    assert found["history"].detail == "3 bar(s)"
    assert "previewed, not placed: commission" in found["whatif"].detail
    assert found["telegram"].detail == "test message delivered"
    assert telegram.sent == ["ObliskIQ preflight"]
    assert rig.broker.placements == []
    assert "DUTEST1" not in report.render(("DUTEST1",))


async def test_without_telegram_the_test_message_is_skipped(rig: Any) -> None:
    report = await FullPreflight(path_of(rig), telegram=None, now=rig.clock).run()
    assert report.ok
    assert "skipped" in steps(report)["telegram"].detail


async def test_an_undelivered_test_message_fails_its_step(rig: Any) -> None:
    report = await FullPreflight(path_of(rig), telegram=Telegram(ok=False), now=rig.clock).run()
    assert not report.ok
    assert report.failed_step is not None
    assert report.failed_step.name == "telegram"


async def test_an_unauthenticated_session_stops_the_walk(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def status() -> Any:
        return SimpleNamespace(authenticated=False, competing=True)

    monkeypatch.setattr(rig.ibkr.session, "auth_status", status)
    report = await FullPreflight(path_of(rig), telegram=None, now=rig.clock).run()
    assert [step.name for step in report.steps] == ["auth"]
    assert "not reached" in report.render()


async def test_a_competing_session_is_named(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def status() -> Any:
        return SimpleNamespace(authenticated=True, competing=True)

    monkeypatch.setattr(rig.ibkr.session, "auth_status", status)
    report = await FullPreflight(path_of(rig), telegram=None, now=rig.clock).run()
    assert steps(report)["auth"].detail == "authenticated (a competing session)"


async def test_a_quote_without_a_row_or_a_tick_stops_before_the_whatif(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.broker.rules_answer = {"orderTypes": ["limit"], "sizeIncrement": 1}
    report = await FullPreflight(path_of(rig), telegram=None, now=rig.clock).run()
    assert report.failed_step is not None
    assert report.failed_step.name == "whatif"
    assert "no previewable order" in report.failed_step.detail

    async def no_quote(conid: int, *, option: bool) -> None:
        return None

    monkeypatch.setattr(rig.reader, "quote", no_quote)
    report = await FullPreflight(path_of(rig), telegram=None, now=rig.clock).run()
    assert report.failed_step is not None
    assert report.failed_step.name == "snapshot"


async def test_a_whatif_the_broker_refuses_fails_its_step(rig: Any) -> None:
    rig.broker.whatif_for = lambda _order: whatif_answer("95.90", "EUR", error="no permission")
    report = await FullPreflight(path_of(rig), telegram=None, now=rig.clock).run()
    assert report.failed_step is not None
    assert report.failed_step.name == "whatif"
    assert "no permission" in report.failed_step.detail


# ── the command ──


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


def _cli(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


@pytest.fixture
def broker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> None:
    monkeypatch.setenv("OBLISK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    monkeypatch.setenv("OBLISK_IBKR_TRANSPORT", "gateway")
    monkeypatch.setenv("OBLISK_IBKR_ACCOUNT_ID", "DU1234567")
    monkeypatch.setenv("OBLISK_IBKR_GATEWAY_USERNAME", "u")
    monkeypatch.setenv("OBLISK_IBKR_GATEWAY_PASSWORD", "p")


@pytest.mark.usefixtures("broker_env")
def test_the_command_reports_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    seen: dict[str, Any] = {}

    def walked(loaded: Any, work: Any) -> PreflightReport:
        seen["telegram"] = work.__closure__ is not None
        report = PreflightReport(planned=FULL_STEPS)
        report.add("auth", True, "authenticated as DU1234567")
        return report

    monkeypatch.setattr(cli_app, "_with_order_path", walked)
    status, out = _cli("preflight", "--full")
    assert status == EXIT_OK
    assert out.startswith("IBKR full preflight:")
    assert "DU1234567" not in out
    assert seen["telegram"] is True


@pytest.mark.usefixtures("broker_env")
def test_the_command_says_why_it_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(loaded: Any, work: Any) -> None:
        raise ObliskError("the service holds the lock")

    monkeypatch.setattr(cli_app, "_with_order_path", broken)
    assert _cli("preflight", "--full") == (
        EXIT_INVALID,
        "preflight stopped: the service holds the lock\n",
    )
