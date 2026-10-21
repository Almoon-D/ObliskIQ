"""Alerts said once while their condition holds, the Telegram notifier, the run summary and the
digest.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid7

import httpx
import pytest
import respx
from pydantic import SecretStr
from service_rig import RecordingNotifier

from obliskiq.calendars.status import Disagreement, DisagreementKind
from obliskiq.capital.managed import CapitalView
from obliskiq.config import ObliskConfig
from obliskiq.decision.rebalance_basic import RebalancePlan, RoleDecision
from obliskiq.domain.orders import (
    ExecutionMode,
    Fill,
    OrderSide,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
)
from obliskiq.execution.boundary import OrderAlert, OrderAlertKind
from obliskiq.execution.currency import ConversionOutcome
from obliskiq.execution.executor import CycleReport
from obliskiq.execution.killswitch import SweepReport
from obliskiq.ibkr.session import SessionAlert, SessionAlertKind
from obliskiq.reporting.alerts import (
    AlertBook,
    Severity,
    condition_key,
    from_disagreement,
    from_order,
    from_session,
    from_sweep,
)
from obliskiq.reporting.digest import Digest, VenueLine, render_digest
from obliskiq.reporting.notifier import LogNotifier, TelegramNotifier, to_telegram_html
from obliskiq.reporting.reporter import Reporter
from obliskiq.reporting.summary import CycleSummary, format_cycle
from obliskiq.store.nav import NavReading
from obliskiq.store.runs import RunRow

AT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def order_alert(kind: OrderAlertKind = OrderAlertKind.STOP_MISSING, **kw: Any) -> OrderAlert:
    fields: dict[str, Any] = {
        "kind": kind,
        "intent_id": None,
        "detail": "conid 7089 held 10, covered 0",
        "at": AT,
    }
    fields.update(kw)
    return OrderAlert(**fields)


# ── alerts ──


def test_alerts_are_made_from_every_source() -> None:
    stop = from_order(order_alert())
    assert (stop.kind, stop.severity, stop.condition) == (
        "order.stop_missing",
        Severity.CRITICAL,
        True,
    )
    assert stop.key == "order.stop_missing:conid # held #, covered #"
    intent = uuid7()
    declined = from_order(order_alert(OrderAlertKind.WARNING_DECLINED, intent_id=intent))
    assert declined.key == f"order.warning_declined:{intent}"
    assert declined.severity is Severity.WARNING
    lost = from_session(SessionAlert(SessionAlertKind.SESSION_LOST, "tickle said so", AT))
    assert (lost.key, lost.severity) == ("session.session_lost", Severity.WARNING)
    failed = from_session(SessionAlert(SessionAlertKind.REAUTHENTICATION_FAILED, "no", AT))
    assert failed.severity is Severity.CRITICAL
    back = from_session(SessionAlert(SessionAlertKind.REAUTHENTICATED, "reset", AT))
    assert (back.condition, back.clears, back.severity) == (False, ("session.",), Severity.INFO)
    assert back.message().startswith("🔵 session.reauthenticated: logged in again")
    found = Disagreement(
        venue="XAMS",
        day=date(2026, 10, 26),
        kind=DisagreementKind.CLOSE,
        source="ibkr_trading_schedule",
        calendar="16:30",
        observed="17:30",
    )
    calendar = from_disagreement(found, AT)
    assert not calendar.condition
    assert "XAMS 2026-10-26 close" in calendar.text
    assert from_sweep(SweepReport(listed=2, kept=("1",)), AT) is None
    sweep = from_sweep(SweepReport(listed=2, cancelled=("1",), failed=("2 (Error)",)), AT)
    assert sweep is not None
    assert sweep.severity is Severity.CRITICAL
    assert "failed 2" in sweep.text
    quiet = from_sweep(SweepReport(listed=1, cancelled=("1",)), AT)
    assert quiet is not None
    assert quiet.severity is Severity.WARNING
    unread = from_sweep(SweepReport(error="the live orders could not be read"), AT)
    assert unread is not None
    assert "could not be read" in unread.text
    assert condition_key("k", "3 of 4.5") == "k:# of #"


def test_a_condition_is_said_once_until_its_round_ends_without_it(tmp_path: Path) -> None:
    book = AlertBook(tmp_path / "alerts.json")
    stop = from_order(order_alert())
    assert book.raised(stop, "heartbeat")  # new: send
    book.sent(stop.key)
    assert not book.raised(stop, "heartbeat")  # said already
    assert not book.raised(stop, "cycle")  # another job raising it changes nothing
    assert book.end_round("heartbeat", {stop.key}) == []
    assert book.end_round("heartbeat", set()) == []  # the cycle still holds it
    assert book.end_round("cycle", set()) == [stop.key]  # nobody holds it: cleared
    assert book.raised(stop, "heartbeat")  # raised again after it cleared: said again
    unsent = from_order(order_alert(OrderAlertKind.CONVERSION_FAILED))
    assert book.raised(unsent, "cycle")
    assert book.raised(unsent, "cycle")  # never delivered: still due


def test_an_event_is_always_sent_and_may_clear_conditions(tmp_path: Path) -> None:
    book = AlertBook(tmp_path / "alerts.json")
    lost = from_session(SessionAlert(SessionAlertKind.SESSION_LOST, "x", AT))
    book.raised(lost, "session")
    book.sent(lost.key)
    back = from_session(SessionAlert(SessionAlertKind.REAUTHENTICATED, "x", AT))
    assert book.raised(back, "session")
    assert book.raised(back, "session")
    assert book.active == {}
    downgrade = from_sweep(SweepReport(listed=1, cancelled=("1",)), AT)
    assert downgrade is not None
    assert book.raised(downgrade, "sweep")


def test_the_book_survives_a_restart_and_sets_aside_a_file_it_cannot_read(tmp_path: Path) -> None:
    path = tmp_path / "alerts.json"
    book = AlertBook(path)
    stop = from_order(order_alert())
    book.raised(stop, "heartbeat")
    book.sent(stop.key)
    again = AlertBook(path)
    assert again.active == {stop.key: "order.stop_missing"}
    assert not again.raised(stop, "heartbeat")
    path.write_text("{not json", encoding="utf-8")
    fresh = AlertBook(path)
    assert fresh.active == {}
    assert [p.name.startswith("alerts.json.corrupt-") for p in tmp_path.iterdir()].count(True) == 1
    assert AlertBook(None).raised(stop, "heartbeat")  # no file: kept in memory only


def test_a_book_that_cannot_be_saved_or_set_aside_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("a file where a directory should be", encoding="utf-8")
    book = AlertBook(blocked / "alerts.json")
    stop = from_order(order_alert())
    assert book.raised(stop, "heartbeat")  # the save fails and is logged
    book.sent(stop.key)
    assert not book.raised(stop, "heartbeat")
    path = tmp_path / "alerts.json"
    path.write_text("[]", encoding="utf-8")

    def refuse(self: Path, target: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(Path, "replace", refuse)
    assert AlertBook(path).active == {}


# ── the reporter ──


async def test_the_reporter_sends_due_alerts_and_clears_finished_conditions(tmp_path: Path) -> None:
    notifier = RecordingNotifier()
    reporter = Reporter(notifier, AlertBook(tmp_path / "alerts.json"))
    stop = from_order(order_alert())
    reporter.push(stop, "heartbeat")
    reporter.push(stop, "heartbeat")
    assert await reporter.end_round("heartbeat") == []
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == "ObliskIQ alert: order.stop_missing"
    assert await reporter.end_round("heartbeat") == [stop.key]
    notifier.fail = True
    reporter.push(stop, "heartbeat")
    assert await reporter.flush() == 0
    assert await reporter.report("s", "b") is False


# ── the notifier ──


def test_telegram_html_is_escaped_bold_and_split_without_loss() -> None:
    chunks = to_telegram_html("A & B", "x < y > z & <script>", chunk=3800)
    assert chunks[0].startswith("<b>A &amp; B</b>")
    assert "&lt;script&gt;" in chunks[0]
    assert "<script>" not in chunks[0]
    body = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    many = to_telegram_html("s", body, chunk=1000)
    assert len(many) > 1
    assert all(len(c) <= 1000 + 1 for c in many)
    assert "".join(many).count("line 199") == 1
    long_line = to_telegram_html("s", "y" * 2500, chunk=1000)
    assert "".join(long_line).count("y") == 2500


async def test_telegram_delivers_to_the_chat_and_never_raises(
    shipped_settings: ObliskConfig, caplog: pytest.LogCaptureFixture
) -> None:
    token = SecretStr("123:secret-token")
    notifier = TelegramNotifier(shipped_settings.reporting, token=token, chat_id=SecretStr("42"))
    with respx.mock(assert_all_called=False) as router:
        route = router.post("https://api.telegram.org/bot123:secret-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
        assert await notifier.send("subject", "body")
        sent = json.loads(route.calls.last.request.content)
        assert sent == {"chat_id": "42", "text": "<b>subject</b>\n\nbody\n", "parse_mode": "HTML"}
        route.mock(return_value=httpx.Response(400, json={"ok": False}))
        assert not await notifier.send("subject", "body")
        route.mock(side_effect=httpx.ConnectError("refused"))
        assert not await notifier.send("subject", "body")
    assert "secret-token" not in caplog.text
    assert await LogNotifier().send("s", "b")


async def test_telegram_over_a_given_client(shipped_settings: ObliskConfig) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=r"https://api\.telegram\.org/.*").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with httpx.AsyncClient() as client:
            notifier = TelegramNotifier(
                shipped_settings.reporting,
                token=SecretStr("1:t"),
                chat_id=SecretStr("2"),
                client=client,
            )
            assert await notifier.send("s", "b")


# ── the run summary ──


def _decision(action: str, **kw: Any) -> RoleDecision:
    fields: dict[str, Any] = {
        "role": "euro_govt_bonds",
        "conid": 43614383,
        "symbol": "IBGM",
        "weight": Decimal("0.68"),
        "held": Decimal(0),
        "value": Decimal(0),
        "target": Decimal(680),
        "action": action,
        "quantity": Decimal(3),
        "notional": Decimal("532.50"),
        "reason": "back to its target",
    }
    fields.update(kw)
    return RoleDecision(**fields)


def _capital() -> CapitalView:
    return CapitalView(
        mode="cap",
        nav=Decimal(100000),
        managed=Decimal(1000),
        unmanaged=Decimal(0),
        reserved=Decimal(0),
        investable=Decimal(1000),
        book_value=Decimal(0),
        book_cash=Decimal(1000),
        holdings=(),
        detail="a book of 1000.00 (started at 1000)",
    )


def test_a_cycle_summary_counts_and_lists_what_it_did() -> None:
    plan = RebalancePlan(
        mode=ExecutionMode.PAPER,
        at=AT,
        capital=_capital(),
        spendable=Decimal(975),
        decisions=[
            _decision("buy"),
            _decision("sell", symbol="EMIM"),
            _decision("skip", symbol="IWDA", reason="XLON is not tradable now"),
            _decision("hold", symbol="X"),
        ],
    )
    report = CycleReport()
    report.add(uuid7(), "placed", "order 1001")
    report.add(uuid7(), "waiting", "quiet")
    report.conversions.append(ConversionOutcome("USD", None, Decimal(187), "filled", "Filled"))
    report.reconcile = ReconcileRun(
        at=AT,
        status=ReconcileStatus.DRIFT,
        findings=(ReconcileFinding(kind="position", detail="conid 1 differs", freezes=True),),
    )
    summary = CycleSummary("europe", ExecutionMode.PAPER, "degraded", plan, (), report, ("x",))
    assert summary.eventful
    subject, body = format_cycle(summary)
    assert subject == "ObliskIQ europe cycle (paper): no new trades"
    assert "🟢 BUY 3 IBGM" in body
    assert "🔴 SELL 3 EMIM" in body
    assert "held back IWDA (euro_govt_bonds): XLON is not tradable now" in body
    assert "placed: order 1001" in body
    assert "quiet" not in body
    assert "conversion into USD (187): filled" in body
    assert "reconciliation drift" in body
    assert "[position] conid 1 differs" in body
    assert "error: x" in body
    assert body.endswith("Paper mode: orders go to the IBKR paper account.")
    blocked = RebalancePlan(mode=ExecutionMode.SHADOW, at=AT, blocked="the kill switch is on")
    quiet = CycleSummary("us", ExecutionMode.SHADOW, "ok", blocked, (), CycleReport())
    assert not quiet.eventful
    assert "no rebalance: the kill switch is on" in format_cycle(quiet)[1]
    assert format_cycle(quiet)[1].endswith("nothing was sent to the broker.")
    live = CycleSummary("us", ExecutionMode.LIVE, "ok", None, (), None)
    assert format_cycle(live)[1].endswith("Mode live: real orders.")


# ── the digest ──


def test_the_digest_renders_every_part_and_flags_what_needs_attention() -> None:
    plan = RebalancePlan(
        mode=ExecutionMode.SHADOW,
        at=AT,
        capital=_capital(),
        decisions=[
            _decision("hold", held=Decimal(3), value=Decimal("532.5")),
            _decision("skip", value=None, symbol="IWDA"),
        ],
        blocked="reconciliation is drift: new risk is frozen",
    )
    run = RunRow(
        run_id="r",
        command="cycle:europe",
        session_date=AT.date(),
        started_at=AT,
        finished_at=AT,
        status="partial",
        steps_planned=3,
        steps_completed=3,
        exit_code=1,
        detail={"steps": [{"step": "nav", "status": "error", "reason": "no NAV"}], "error": "boom"},
    )
    fill = Fill(
        execution_id="e",
        coid="c",
        conid=43614383,
        side=OrderSide.BUY,
        quantity=Decimal(3),
        price=Decimal("177.6"),
        commission=Decimal(3),
        executed_at=AT,
        simulated=True,
    )
    digest = Digest(
        day=AT.date(),
        at=AT,
        mode=ExecutionMode.SHADOW,
        kill_on=False,
        reconcile=ReconcileRun(at=AT, status=ReconcileStatus.GREEN, findings=()),
        nav=NavReading(AT.date(), Decimal(100000), "EUR", "cp", AT),
        plan=plan,
        fills=[fill],
        runs=[run],
        venues=[
            VenueLine("XAMS", "🟢", "regular", False),
            VenueLine("XNYS", "🚨", "blocked", True),
        ],
        health=["last heartbeat now"],
        alerts={"service.venue_blocked:XNYS": "service.venue_blocked"},
        problems=["the mode cannot be read"],
    )
    subject, body = render_digest(digest, rows=10, base_currency="EUR")
    assert subject == "ObliskIQ daily digest 2026-10-26 (shadow): 4 need attention"
    assert "NAV: 100,000.00 EUR" in body
    assert "managed 1,000.00 EUR (a book of 1000.00 (started at 1000))" in body
    assert "no rebalance now: reconciliation is drift" in body
    assert "held 3 = 532.50 (53.2%)" in body
    assert "IWDA: held 0 = unvalued" in body
    assert "177.6 (simulated)" in body
    assert "cycle:europe partial: nav: error no NAV; boom" in body
    assert "🚨 XNYS blocked" in body
    assert "cannot read: the mode cannot be read" in body
    assert "Open alerts (1)" in body
    empty = Digest(day=AT.date(), at=AT)
    subject, body = render_digest(empty, rows=1, base_currency="EUR")
    assert "(unreadable)" in subject
    assert "nothing needs attention" in subject
    assert "kill switch ON" in body
    assert "not surveyed" in body
    assert "NAV: no reading" in body
    assert "Reconciliation: none yet" in body
    many = Digest(day=AT.date(), at=AT, fills=[fill, fill])
    assert "… and 1 more" in render_digest(many, rows=1, base_currency="EUR")[1]


def test_the_notifier_is_telegram_only_with_its_token_and_chat(
    shipped_settings: ObliskConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.env import Environment
    from obliskiq.orchestration.assemble import build_notifier

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert isinstance(build_notifier(shipped_settings, Environment(_env_file=None)), LogNotifier)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:t")
    assert isinstance(build_notifier(shipped_settings, Environment(_env_file=None)), LogNotifier)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "2")
    notifier = build_notifier(shipped_settings, Environment(_env_file=None))
    assert isinstance(notifier, TelegramNotifier)
