"""The cockpit: every read and every owner command the MCP tools, Telegram and the dashboard share.

One class, so a rule lives once: the MCP server (``server/app.py``) and the Telegram bot
(``server/telegram.py``) both call :class:`Cockpit`, which reads the store, the service's files
(heartbeat, alert state, board, dashboard, pause flag) and the owner's panel, and opens no
brokerage session: the service holds the one IBKR allows per username. What only a survey of the
broker shows (positions, targets, the reservation plan) comes from the board the service wrote at
its last digest, labelled with its age.

**Commands.** A command that adds risk or records money (a setting, a deposit, a reservation, an
answer, an approval) is a preview until it is repeated with ``confirm``; a command that only
lowers risk (a rejection, a pause, the kill switch) acts at once. Remote channels change only the
settings ``remote_settings`` lists, in the direction it allows (``config/changes.py``); the kill
switch is turned off only on the machine, since that loosens risk. A refusal is the answer, raised
as :class:`CockpitRefusal` with its reason in the owner's words.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import json
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .. import __version__
from ..capital.flows import CashLedger, LedgerRefusal
from ..capital.questions import OwnerQuestions, QuestionRefusal
from ..config.audit import SettingsAuditLog
from ..config.changes import (
    SettingsRefusal,
    apply,
    check_remote,
    owner_overrides,
    propose,
    settings_fingerprint,
)
from ..config.loader import LoadedConfig, load_config
from ..data.runner import source_health
from ..domain.capital import QuestionState
from ..domain.orders import TERMINAL_STATES, IntentState, KillSwitchRecord
from ..domain.thesis import OPEN_STATES
from ..env import Environment, repository_root
from ..errors import ObliskError
from ..execution.approvals import ApprovalQueue
from ..execution.killswitch import file_flag_on, set_file_flag
from ..execution.modes import forced_shadow_on
from ..execution.state import IntentBook
from ..grid.views import equity_grid, macro_board
from ..orchestration.health import read_heartbeat
from ..orchestration.pause import clear_pause, read_pause, set_pause
from ..reporting.board import read_board, read_patterns
from ..research.patterns import REGISTRY_PATH
from ..scoreboard.run import performance
from ..store import capital as capital_repo
from ..store import nav as nav_repo
from ..store import orders as repo
from ..store import runs as run_repo
from ..store import scoreboard as scoreboard_repo
from ..store import sources as source_repo
from ..store import theses as thesis_repo
from ..store.db import Store
from ..store.worker import StoreWorker
from .encode import to_jsonable


class CockpitRefusal(ObliskError):
    """A command the cockpit does not carry out, and why, in the owner's words."""


#: Error kinds whose message is already the owner's answer.
REFUSALS = (CockpitRefusal, SettingsRefusal, LedgerRefusal, QuestionRefusal)


def _amount(text: str) -> Decimal:
    try:
        value = Decimal(str(text).replace("_", ""))
    except InvalidOperation:
        raise CockpitRefusal(f"{text!r} is not an amount (digits, a point for decimals)") from None
    if not value.is_finite() or value <= 0:
        raise CockpitRefusal(f"{text!r} is not a positive amount")
    return value


def _day(text: str | None, today: date) -> date:
    if text is None or not str(text).strip():
        return today
    try:
        return date.fromisoformat(str(text).strip())
    except ValueError:
        raise CockpitRefusal(f"{text!r} is not a date in YYYY-MM-DD form") from None


def _question_id(text: str | int) -> int:
    raw = str(text).strip().upper().removeprefix("Q")
    if not raw.isdigit() or int(raw) <= 0:
        raise CockpitRefusal(f"{text!r} is not a question id (Q<number>)")
    return int(raw)


def preview(what: str) -> dict[str, Any]:
    """The answer to an unconfirmed command."""
    return {
        "confirmed": False,
        "preview": what,
        "next": "nothing was recorded: repeat the same command with confirm to record it",
    }


class Cockpit:
    """The owner's view of the bot and the commands it accepts, without a broker session."""

    def __init__(
        self,
        config: LoadedConfig,
        env: Environment,
        *,
        worker: StoreWorker,
        actor: str,
        channel: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._settings = config.settings
        self._env = env
        self._data_dir = env.require_data_dir()
        self._worker = worker
        self._actor = actor
        self._channel = channel
        self._now = now

    async def _fresh(self) -> LoadedConfig:
        """The configuration as the owner's panel holds it now: this process outlives changes to
        it (its own included), and a change is always proposed against the panel in force."""
        return await asyncio.to_thread(
            load_config,
            self._config.config_dir,
            owner_file=self._config.owner_file,
            overrides=self._env.config_overrides(),
        )

    def _file(self, name: str) -> Path:
        return self._data_dir / name

    def _bounded(self, items: list[Any]) -> tuple[list[Any], dict[str, Any]]:
        """``items`` capped at ``server.max_rows_per_response``, with a note when cut."""
        ceiling = self._settings.server.max_rows_per_response
        if len(items) <= ceiling:
            return items, {}
        return items[:ceiling], {
            "truncated": True,
            "truncated_note": f"{len(items)} rows, capped at server.max_rows_per_response "
            f"({ceiling}); narrow the request rather than reading a partial table as whole",
        }

    def _board(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """The service's last board and how old it is; ``None`` with why when there is none."""
        board = read_board(self._file(self._settings.paths.board))
        if board is None:
            return None, {
                "board": "none: the service has written no board yet (it writes one after each "
                "daily digest)"
            }
        at = datetime.fromisoformat(board["at"])
        age = (self._now().date() - at.date()).days
        meta: dict[str, Any] = {"read_by_service_at": board["at"], "age_days": age}
        if age > self._settings.server.stale_after_days:
            meta["stale"] = True
            meta["stale_note"] = (
                f"the board is {age} day(s) old, beyond server.stale_after_days "
                f"({self._settings.server.stale_after_days}): label anything taken from it as "
                "stale"
            )
        return board, meta

    # ── reads ──

    async def status(self) -> dict[str, Any]:
        """Mode, kill switch, pause, heartbeat, reconciliation, NAV and what waits for the owner."""
        settings = self._settings
        now = self._now()

        def read(store: Store) -> dict[str, Any]:
            change = repo.latest_mode_change(store)
            kill = repo.latest_kill_switch(store)
            latest = repo.latest_reconciliation(store)
            series = nav_repo.nav_series(
                store, since=now.date() - timedelta(days=7), as_of=now.date()
            )
            open_states = [state for state in IntentState if state not in TERMINAL_STATES]
            return {
                "mode": settings.execution.modes.initial if change is None else change.to_mode,
                "kill_database": kill is not None and kill.on,
                "reconciliation": None
                if latest is None
                else {"status": latest.status.value, "at": latest.at},
                "nav": None
                if not series
                else {
                    "value": series[-1].nav_base,
                    "currency": series[-1].currency,
                    "as_of": series[-1].as_of,
                },
                "open_intents": len(repo.intents_in_states(store, open_states)),
                "open_questions": len(capital_repo.questions(store, states=[QuestionState.OPEN])),
            }

        found = await self._worker.run(read)
        forced = forced_shadow_on(self._data_dir)
        kill_on = file_flag_on(self._data_dir) or found.pop("kill_database")
        beat, facts = read_heartbeat(self._file(settings.paths.heartbeat_file))
        heartbeat: dict[str, Any] = {"at": None, "note": "none recorded: the service has not run"}
        if beat is not None:
            age = (now - beat).total_seconds()
            heartbeat = {
                "at": beat,
                "age_seconds": round(age),
                "stale": age > 2 * settings.runtime.heartbeat_seconds,
                "status": facts.get("status"),
                "session": facts.get("session"),
                "last_cycle": facts.get("last_cycle"),
                "settings_current": facts.get("settings") == settings_fingerprint(settings),
            }
        return _json(
            {
                "version": __version__,
                "at": now,
                **found,
                "mode": "shadow (held by the FORCED_SHADOW file)" if forced else found["mode"],
                "kill_switch": "ON" if kill_on else "off",
                "pause": read_pause(self._file(settings.paths.pause_flag)).describe(),
                "heartbeat": heartbeat,
            }
        )

    async def health(self) -> dict[str, Any]:
        """The heartbeat's facts, each source's health, the open alerts and the recent runs."""
        settings = self._settings
        now = self._now()
        since = now.date() - timedelta(days=settings.runtime.status_report_days)
        runs = await self._worker.run(run_repo.runs_since, since=since)
        sources = await self._worker.run(source_repo.source_runs)
        health = source_health(settings, sources, now=now)
        beat, facts = read_heartbeat(self._file(settings.paths.heartbeat_file))
        rows, note = self._bounded(
            [
                {
                    "started_at": run.started_at,
                    "command": run.command,
                    "status": run.status,
                    "steps": f"{run.steps_completed}/{run.steps_planned}",
                }
                for run in runs
            ]
        )
        return _json(
            {
                "heartbeat": {"at": beat, **facts},
                "sources": {
                    "counts": dict(Counter(h.state.value for h in health)),
                    "not_ok": [
                        {"source": h.source, "state": h.state, "reason": h.reason}
                        for h in health
                        if h.state.value != "ok"
                    ],
                },
                "open_alerts": _open_alerts(self._file(settings.paths.alert_state)),
                "runs_since": since,
                "runs": rows,
                **note,
            }
        )

    async def portfolio(self) -> dict[str, Any]:
        """NAV and the book as the service last surveyed it: capital, positions, budgets."""
        board, meta = self._board()
        if board is None:
            return meta
        return {
            **meta,
            "base_currency": board["base_currency"],
            "nav": board["nav"],
            "twr": board["twr"],
            "capital": board["book"].get("capital"),
            "positions": board["book"].get("positions", []),
            "blocked": board["book"].get("blocked"),
            "budgets": board["budgets"],
        }

    async def targets(self) -> dict[str, Any]:
        """The core's target portfolio as the service last surveyed it."""
        board, meta = self._board()
        if board is None:
            return meta
        book = board["book"]
        return {
            **meta,
            "core": book.get("core"),
            "core_budget": book.get("core_budget"),
            "spendable": book.get("spendable"),
            "positions": book.get("positions", []),
            "blocked": book.get("blocked"),
        }

    async def intents(self, *, include_closed: bool = False) -> dict[str, Any]:
        """The open intents (every intent with ``include_closed``), newest first."""
        states = (
            list(IntentState)
            if include_closed
            else [state for state in IntentState if state not in TERMINAL_STATES]
        )
        stored = await self._worker.run(repo.intents_in_states, states)
        stored.sort(key=lambda s: s.intent.created_at, reverse=True)
        rows, note = self._bounded(
            [
                {
                    "intent_id": s.intent.intent_id,
                    "created_at": s.intent.created_at,
                    "mode": s.intent.mode,
                    "sleeve": s.intent.sleeve,
                    "role": s.intent.role,
                    "side": s.intent.side,
                    "conid": s.intent.conid,
                    "quantity": s.intent.terms.quantity,
                    "cash_quantity": s.intent.terms.cash_quantity,
                    "limit": s.intent.terms.price.limit,
                    "state": s.intent.state,
                    "reason": s.intent.reason,
                }
                for s in stored
            ]
        )
        return _json({"count": len(stored), "intents": rows, **note})

    async def theses(self) -> dict[str, Any]:
        """The thesis book's open theses and the book's plan as the service last read it."""
        today = self._now().date()
        theses = await self._worker.run(
            lambda store: thesis_repo.theses(store, as_of=today, states=OPEN_STATES)
        )
        rows, note = self._bounded(
            [
                {
                    "ref": t.ref,
                    "symbol": t.instrument.symbol,
                    "source": t.source,
                    "conviction": t.conviction,
                    "view": t.view,
                    "state": t.state,
                    "expectation": t.expectation,
                    "target": t.target,
                    "invalidation": t.invalidation,
                    "horizon": t.horizon,
                    "size_base": t.size_base,
                }
                for t in theses
            ]
        )
        board, meta = self._board()
        return _json(
            {
                "open": rows,
                "book": meta if board is None else {**meta, "lines": board["theses"]},
                **note,
            }
        )

    async def patterns(self) -> dict[str, Any]:
        """Every hypothesis in the research registry, with its status and decision."""
        root = repository_root()
        if root is None:
            return {"patterns": "no research registry beside this installation"}
        return {"patterns": read_patterns(root / REGISTRY_PATH)}

    async def grid_view(self, *, as_of: str | None = None) -> dict[str, Any]:
        """The equity grid (market x sector) of the newest build on or before ``as_of``."""
        day = _day(as_of, self._now().date())
        view = await self._worker.run(lambda store: equity_grid(store, self._settings, as_of=day))
        cells = [
            {
                "market": cell.market,
                "sector": cell.sector,
                "disclosure": cell.disclosure,
                "leaves": {name: _leaf(m) for name, m in sorted(cell.leaves.items())},
            }
            for cell in view.cells.values()
        ]
        rows, note = self._bounded(cells)
        return _json({"as_of": view.as_of, "session": view.session, "cells": rows, **note})

    async def macro_board(self, *, as_of: str | None = None) -> dict[str, Any]:
        """The macro board (region x asset class) of the newest build on or before ``as_of``."""
        day = _day(as_of, self._now().date())
        view = await self._worker.run(lambda store: macro_board(store, self._settings, as_of=day))
        cells = [
            {
                "region": cell.region,
                "asset_class": cell.asset_class,
                "inputs": cell.inputs,
                "leaves": {name: _leaf(m) for name, m in sorted(cell.leaves.items())},
            }
            for cell in view.cells.values()
        ]
        rows, note = self._bounded(cells)
        return _json({"as_of": view.as_of, "session": view.session, "cells": rows, **note})

    async def scoreboard(self) -> dict[str, Any]:
        """The account's time-weighted growth beside the references, each layer's live share and
        latest looks."""
        settings = self._settings
        day = self._now().date()
        since = day - timedelta(days=settings.runtime.status_report_days)
        view = await self._worker.run(performance, settings, since=since, as_of=day)
        shares = await self._worker.run(scoreboard_repo.current_shares)
        looks = await self._worker.run(scoreboard_repo.looks)
        recent, note = self._bounded(sorted(looks, key=lambda look: look.at, reverse=True))
        return _json(
            {
                "since": since,
                "as_of": day,
                "account": {"growth": view.account.growth, "reason": view.account.reason},
                "references": [
                    {"name": r.name, "growth": r.growth, "reason": r.reason}
                    for r in view.references
                ],
                "sleeves": view.sleeves,
                "live_shares": shares,
                "looks": recent,
                **note,
            }
        )

    async def settings(self) -> dict[str, Any]:
        """What the owner's panel sets, the settings remote channels may change and their values
        now, and whether the service runs on them."""
        config = await self._fresh()
        rows = await asyncio.to_thread(owner_overrides, config)
        settings = config.settings
        _, facts = read_heartbeat(self._file(settings.paths.heartbeat_file))
        flat = settings.model_dump(mode="json")
        remote: list[dict[str, Any]] = []
        for rule in settings.remote_settings.rules():
            node: Any = flat
            for part in rule.key.split("."):
                node = node[part]
            remote.append({**rule.model_dump(mode="json"), "value": node})
        return _json(
            {
                "owner_file": str(config.owner_file),
                "owner_settings": [
                    {"key": key, "default": default, "value": value} for key, default, value in rows
                ],
                "remote_changeable": remote,
                "service_runs_on_these": facts.get("settings") == settings_fingerprint(settings),
            }
        )

    async def list_cash(self) -> dict[str, Any]:
        """Reservations, recorded flows and unexplained cash events."""
        ledger = CashLedger(self._worker, base_currency=self._settings.account.base_currency)
        snapshot = await ledger.snapshot()
        return _json(
            {
                "reservations": snapshot.reservations,
                "flows": snapshot.flows,
                "unexplained_cash": snapshot.events,
            }
        )

    async def list_questions(self, *, include_closed: bool = False) -> dict[str, Any]:
        """The owner questions waiting for an answer (every question with ``include_closed``)."""
        questions = await OwnerQuestions(self._worker).listed(open_only=not include_closed)
        return _json(
            {
                "questions": [
                    {
                        "id": f"Q{q.question_id}",
                        "kind": q.kind,
                        "state": q.state,
                        "context": q.context,
                        "deadline": q.deadline,
                        "default": q.default,
                        "answer": q.answer,
                        "options": q.options,
                    }
                    for q in questions
                ]
            }
        )

    # ── commands ──

    async def update_setting(
        self, key: str, value: str, *, reason: str, confirm: bool
    ) -> dict[str, Any]:
        """Change one setting remotely: validated, permitted, written and audited on confirm."""
        if not reason.strip():
            raise CockpitRefusal("a settings change needs a reason (it is audited)")
        config = await self._fresh()
        change = await asyncio.to_thread(propose, config, key, value)
        check_remote(change, config.settings.remote_settings)
        if not confirm:
            return preview(f"{change.describe()}; applied at the service's next cycle boundary")
        audit = SettingsAuditLog.for_settings(self._settings, self._env)
        entries = await asyncio.to_thread(
            apply,
            change,
            config,
            audit,
            actor=self._actor,
            channel=self._channel,
            reason=reason,
            at=self._now(),
        )
        return {
            "confirmed": True,
            "changed": change.describe(),
            "audited": len(entries),
            "applies": "at the service's next cycle boundary",
        }

    async def cash_deposit(
        self, amount: str, currency: str, *, on: str | None, note: str | None, confirm: bool
    ) -> dict[str, Any]:
        """Money the owner added: managed capital rises by it and it is deployed to the targets."""
        ledger = CashLedger(self._worker, base_currency=self._settings.account.base_currency)
        deposit = ledger.deposit(
            _amount(amount),
            currency=currency.upper(),
            on=_day(on, self._now().date()),
            actor=self._actor,
            note=note,
        )
        what = f"deposit of {deposit.amount} {deposit.currency} on {deposit.on.isoformat()}"
        if not confirm:
            return preview(what)
        made = await ledger.record_deposit(deposit)
        return {"confirmed": True, "recorded": f"F{made.flow_id}", "what": what}

    async def cash_reserve(
        self, amount: str, currency: str, *, by: str, reason: str, confirm: bool
    ) -> dict[str, Any]:
        """A dated withdrawal: investable capital drops at once and the planner raises the cash."""
        ledger = CashLedger(self._worker, base_currency=self._settings.account.base_currency)
        due = _day(by, self._now().date())
        reservation = ledger.reservation(
            _amount(amount), currency=currency.upper(), by=due, reason=reason, actor=self._actor
        )
        what = (
            f"withdrawal of {reservation.amount} {reservation.currency} by "
            f"{reservation.due_date.isoformat()} ({reservation.reason}): investable capital drops "
            "by it at once and the planner raises the cash, asking before cutting a protected "
            "position"
        )
        if not confirm:
            return preview(what)
        made = await ledger.record_reservation(reservation)
        return {"confirmed": True, "recorded": f"R{made.reservation_id}", "what": what}

    async def answer_question(
        self, question_id: str | int, option: str, *, confirm: bool
    ) -> dict[str, Any]:
        """Answer an owner question with one of its options."""
        number = _question_id(question_id)
        question = await self._worker.run(capital_repo.get_question, number)
        if question is None:
            raise CockpitRefusal(f"there is no question Q{number}")
        chosen = question.option(option)
        if chosen is None:
            keys = ", ".join(o.key for o in question.options)
            raise CockpitRefusal(f"Q{number} has no option {option!r} (options: {keys})")
        what = f"Q{number}: {chosen.key}: {chosen.label}"
        if not confirm:
            return preview(what)
        answered = await OwnerQuestions(self._worker).answer(number, option, actor=self._actor)
        return {"confirmed": True, "answered": what, "effect": answered.effect}

    def _approvals(self) -> ApprovalQueue:
        book = IntentBook(
            self._worker, coid_max_length=self._settings.execution.orders.coid_max_length
        )
        return ApprovalQueue(self._settings, self._worker, book)

    async def approve_intent(self, intent_id: str, *, reason: str, confirm: bool) -> dict[str, Any]:
        """Release an intent held for the owner's approval (it adds risk: confirmed first)."""
        parsed = _intent_id(intent_id)
        stored = await self._worker.run(repo.get_intent, parsed)
        if stored is None:
            raise CockpitRefusal(f"there is no intent {parsed}")
        intent = stored.intent
        what = (
            f"approve {intent.side.value} conid {intent.conid} ({intent.sleeve.value}, "
            f"{intent.reason})"
        )
        if not confirm:
            return preview(what)
        await self._approvals().approve(parsed, actor=self._actor, reason=reason)
        return {"confirmed": True, "approved": str(parsed), "what": what}

    async def reject_intent(self, intent_id: str, *, reason: str) -> dict[str, Any]:
        """Reject an intent held for approval; it lowers risk, so it acts at once."""
        parsed = _intent_id(intent_id)
        await self._approvals().reject(parsed, actor=self._actor, reason=reason)
        return {"confirmed": True, "rejected": str(parsed)}

    async def set_pause(self, *, on: bool, reason: str) -> dict[str, Any]:
        """Pause or resume the decision cycles; the kill switch is untouched."""
        path = self._file(self._settings.paths.pause_flag)
        if on:
            if not reason.strip():
                raise CockpitRefusal("a pause needs a reason (it is recorded with it)")
            state = set_pause(path, actor=self._actor, reason=reason, at=self._now())
        else:
            state = clear_pause(path)
        return {"paused": state.on, "state": state.describe()}

    async def kill_switch(self, *, reason: str) -> dict[str, Any]:
        """Turn the kill switch ON: the file flag at once, then the database flag. Turning it
        off loosens risk, so it is done on the machine (``obliskiq kill off``)."""
        at = self._now()
        problems: list[str] = []
        file_set = False
        try:
            set_file_flag(self._data_dir, actor=self._actor, reason=reason, at=at)
            file_set = True
        except OSError as exc:
            problems.append(f"the file flag could not be written: {exc}")
        record = KillSwitchRecord(at=at, on=True, actor=self._actor, reason=reason)
        try:
            await self._worker.run(repo.record_kill_switch, record)
        except Exception as exc:  # the file flag alone still stops new risk
            problems.append(f"the database flag could not be written: {type(exc).__name__}")
            if not file_set:
                raise CockpitRefusal("kill switch NOT set: " + "; ".join(problems)) from exc
        return {
            "kill_switch": "ON",
            "effect": "orders that add risk are blocked; the service's sweep cancels working "
            "entry orders; protective exits stay. Turning it off is `obliskiq kill off` on the "
            "machine",
            "warnings": problems,
        }

    # ── the dashboard ──

    def dashboard_path(self) -> Path:
        return self._file(self._settings.paths.dashboard)


def _json(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` as JSON-ready primitives (``server/encode.py``)."""
    encoded: dict[str, Any] = to_jsonable(payload)
    return encoded


def _intent_id(text: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(text).strip())
    except ValueError:
        raise CockpitRefusal(f"{text!r} is not an intent id") from None


def _leaf(measurement: Any) -> dict[str, Any]:
    return {
        "value": measurement.value,
        "status": measurement.status,
        "reason": measurement.reason,
        "as_of": measurement.as_of,
    }


def _open_alerts(path: Path) -> list[str]:
    """The conditions the alert state holds (``reporting/alerts.py``)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return sorted(str(key) for key in data["held"])
    except OSError, ValueError, KeyError, TypeError, AttributeError:
        return []
