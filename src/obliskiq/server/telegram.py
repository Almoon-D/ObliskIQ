"""The owner's Telegram bot: read the bot, answer it, change it and stop it (MASTER_PLAN §5.17).

Long polling (``getUpdates``) from the cockpit's process; every reply goes back to the owner's
chat. Only the chat ``TELEGRAM_CHAT_ID`` names is served: a message from any other chat is
dropped unread (its sender learns nothing, not even that the bot exists). The last update handled
is kept in ``paths.telegram_state``, so a restart never handles a message twice.

Commands: ``/status``, ``/positions``, ``/intents``, ``/theses``, ``/questions``, ``/answer <id>
<option>``, ``/settings``, ``/set <key> <value> [reason]``, ``/deposit <amount> [currency]``,
``/withdraw <amount> [currency] <YYYY-MM-DD> [reason]``, ``/confirm <code>``, ``/pause [reason]``
(``/pause off`` resumes), ``/kill [reason]``. Free text goes to CommandParser's grammar
(``server/commands.py``). Anything that records money, answers a question or changes a setting is
echoed with a code and done only on ``/confirm <code>`` within ``server.telegram.confirm_minutes``;
``/pause`` and ``/kill`` only lower risk and act at once. Each open owner question is pushed once,
with its options and deadline.

The bot token is part of every request's path, so no URL and no exception text that could carry
it is ever logged.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from ..config.schema import ObliskConfig
from ..domain.owner_commands import (
    DepositCommand,
    OwnerCommand,
    SettingCommand,
    WithdrawalCommand,
)
from ..errors import ObliskError
from ..logging_setup import get_logger
from ..reporting.notifier import TELEGRAM_API
from .cockpit import REFUSALS, Cockpit
from .commands import parse_command

log = get_logger("server.telegram")

HTTP_OK = 200
CODE_DIGITS = 6
#: Seconds added to a poll's own timeout before the HTTP call gives up.
POLL_MARGIN_SECONDS = 10
DEFAULT_REASON = "from Telegram"
HELP = """\
ObliskIQ commands:
/status · /positions · /intents · /theses · /questions · /settings
/answer <Q id> <option>
/set <key> <value> [reason]
/deposit <amount> [currency]
/withdraw <amount> [currency] <YYYY-MM-DD> [reason]
/confirm <code> — carries out what was echoed
/pause [reason] · /pause off · /kill [reason]
Or write it: "I added 1.000 € to your cash", "en 15 días necesito retirar 500 €", "Q3 postpone".\
"""

type Action = Callable[[], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Pending:
    """An echoed command waiting for its code."""

    what: str
    action: Action
    expires: datetime


def _code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def _shown(value: Any) -> str:
    return "—" if value is None else str(value)


class TelegramBot:
    """The bot's loop: read the owner's messages, reply, push the open questions."""

    def __init__(
        self,
        cockpit: Cockpit,
        settings: ObliskConfig,
        *,
        token: SecretStr,
        chat_id: SecretStr,
        state_path: Path,
        now: Callable[[], datetime],
        client: httpx.AsyncClient | None = None,
        code: Callable[[], str] = _code,
    ) -> None:
        self._cockpit = cockpit
        self._settings = settings
        self._telegram = settings.server.telegram
        self._token = token
        self._chat_id = chat_id.get_secret_value()
        self._state_path = state_path
        self._now = now
        self._client = client
        self._code = code
        self._pending: dict[str, Pending] = {}
        state = self._read_state()
        self._offset: int = int(state.get("offset", 0))
        self._pushed: set[int] = {int(q) for q in state.get("pushed_questions", [])}

    # ── state ──

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        payload = {"offset": self._offset, "pushed_questions": sorted(self._pushed)}
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(f".{self._state_path.name}.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self._state_path)

    # ── the wire ──

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self._token.get_secret_value()}/{method}"

    async def _post(self, method: str, payload: dict[str, Any], *, timeout: float) -> Any:
        """One Bot API call; its ``result``, or ``None`` when it failed (logged, never raised)."""
        client = self._client if self._client is not None else httpx.AsyncClient()
        try:
            response = await client.post(self._url(method), json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            # The exception's text can carry the URL, and the URL the token.
            log.warning(
                "telegram call failed", extra={"method": method, "error": type(exc).__name__}
            )
            return None
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code != HTTP_OK:
            log.warning(
                "telegram refused a call", extra={"method": method, "status": response.status_code}
            )
            return None
        try:
            return response.json().get("result")
        except ValueError, AttributeError:
            return None

    async def send(self, text: str) -> bool:
        """Send ``text`` to the owner's chat as plain text, split under the message cap."""
        chunk = self._settings.reporting.telegram.chunk_characters
        parts: list[str] = []
        current = ""
        for line in text.split("\n"):
            while len(line) > chunk:
                if current:
                    parts.append(current)
                    current = ""
                parts.append(line[:chunk])
                line = line[chunk:]
            if current and len(current) + len(line) + 1 > chunk:
                parts.append(current)
                current = ""
            current += line + "\n"
        if current.strip():
            parts.append(current)
        timeout = self._settings.reporting.telegram.timeout_seconds
        delivered = True
        for part in parts:
            result = await self._post(
                "sendMessage", {"chat_id": self._chat_id, "text": part}, timeout=timeout
            )
            delivered = delivered and result is not None
        return delivered

    # ── the loop ──

    async def poll_once(self) -> int | None:
        """Read and handle the updates waiting: how many came from the owner's chat, ``None``
        when the poll failed."""
        timeout = self._telegram.poll_timeout_seconds
        updates = await self._post(
            "getUpdates",
            {"offset": self._offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + POLL_MARGIN_SECONDS,
        )
        if not isinstance(updates, list):
            return None
        handled = 0
        for update in updates:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id", ""))
            text = message.get("text")
            if chat != self._chat_id or not isinstance(text, str):
                log.warning("ignored a message from another chat or without text")
                continue
            handled += 1
            await self.send(await self.handle(text))
        self._save_state()
        return handled

    async def push_questions(self) -> int:
        """Send each open owner question not pushed yet, with its options and deadline."""
        listed = await self._cockpit.list_questions()
        sent = 0
        for question in listed["questions"]:
            number = int(str(question["id"]).removeprefix("Q"))
            if number in self._pushed:
                continue
            options = "\n".join(
                f"  {o['key']}{' (default)' if o['key'] == question['default'] else ''}: "
                f"{o['label']} (tax {_shown(o['tax_cost'])}, strategy "
                f"{_shown(o['strategy_cost'])})"
                for o in question["options"]
            )
            text = (
                f"Question {question['id']}: {question['context']}\nOptions:\n{options}\n"
                f"Answer by {question['deadline']} with /answer {question['id']} <option>; "
                f"without an answer the default ({question['default']}) applies."
            )
            if await self.send(text):
                self._pushed.add(number)
                sent += 1
        self._save_state()
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        """Poll and push until ``stop``; a failed round waits ``retry_seconds``."""
        while not stop.is_set():
            handled: int | None = None
            try:
                await self.push_questions()
                handled = await self.poll_once()
            except Exception:  # the bot outlives any one failure; the next round retries
                log.exception("a telegram round failed")
            if handled is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._telegram.retry_seconds)
                except TimeoutError:
                    continue

    # ── commands ──

    def _echo(self, what: str, action: Action) -> str:
        now = self._now()
        self._pending = {c: p for c, p in self._pending.items() if p.expires > now}
        code = self._code()
        minutes = self._telegram.confirm_minutes
        self._pending[code] = Pending(what, action, now + timedelta(minutes=minutes))
        return f"{what}\nNothing is done yet. Send /confirm {code} within {minutes} minutes."

    async def handle(self, text: str) -> str:
        """The reply to one message from the owner."""
        try:
            return await self._dispatch(text.strip())
        except REFUSALS as exc:
            return f"refused: {exc}"
        except ObliskError as exc:
            return f"failed: {exc}"

    async def _dispatch(self, text: str) -> str:
        if not text.startswith("/"):
            return await self._free_text(text)
        head, _, rest = text.partition(" ")
        command = head.split("@", 1)[0].lower()
        args = rest.split()
        reads: dict[str, Callable[[], Awaitable[str]]] = {
            "/status": self._status,
            "/positions": self._positions,
            "/intents": self._intents,
            "/theses": self._theses,
            "/questions": self._questions,
            "/settings": self._settings_reply,
        }
        if command in reads:
            return await reads[command]()
        if command == "/answer" and len(args) == 2:
            return await self._answer(args[0], args[1])
        if command == "/set" and len(args) >= 2:
            return await self._set(args[0], args[1], " ".join(args[2:]) or DEFAULT_REASON)
        if command == "/deposit" and args:
            return await self._deposit(args)
        if command == "/withdraw" and len(args) >= 2:
            return await self._withdraw(args)
        if command == "/confirm" and len(args) == 1:
            return await self._confirm(args[0])
        if command == "/pause":
            return await self._pause(rest.strip())
        if command == "/kill":
            done = await self._cockpit.kill_switch(reason=rest.strip() or "/kill from Telegram")
            return f"kill switch ON. {done['effect']}"
        return HELP

    async def _free_text(self, text: str) -> str:
        parsed = parse_command(
            text,
            today=self._now().date(),
            base_currency=self._settings.account.base_currency,
        )
        if parsed is None:
            return "I did not understand that.\n\n" + HELP
        return await self._command(parsed)

    async def _command(self, parsed: OwnerCommand) -> str:
        if isinstance(parsed, DepositCommand):
            return await self._deposit([str(parsed.amount), parsed.currency])
        if isinstance(parsed, WithdrawalCommand):
            return await self._withdraw(
                [str(parsed.amount), parsed.currency, parsed.due.isoformat()]
            )
        if isinstance(parsed, SettingCommand):
            return await self._set(parsed.key, parsed.value, DEFAULT_REASON)
        return await self._answer(f"Q{parsed.question_id}", parsed.option)

    async def _answer(self, question: str, option: str) -> str:
        cockpit = self._cockpit
        shown = await cockpit.answer_question(question, option, confirm=False)
        return self._echo(
            shown["preview"], lambda: cockpit.answer_question(question, option, confirm=True)
        )

    async def _set(self, key: str, value: str, reason: str) -> str:
        cockpit = self._cockpit
        shown = await cockpit.update_setting(key, value, reason=reason, confirm=False)
        return self._echo(
            shown["preview"],
            lambda: cockpit.update_setting(key, value, reason=reason, confirm=True),
        )

    async def _deposit(self, args: list[str]) -> str:
        cockpit = self._cockpit
        amount = args[0]
        currency = args[1] if len(args) > 1 else self._settings.account.base_currency
        shown = await cockpit.cash_deposit(amount, currency, on=None, note=None, confirm=False)
        return self._echo(
            shown["preview"],
            lambda: cockpit.cash_deposit(amount, currency, on=None, note=None, confirm=True),
        )

    async def _withdraw(self, args: list[str]) -> str:
        cockpit = self._cockpit
        amount = args[0]
        if len(args) >= 3 and not args[1][:1].isdigit():
            currency, due, words = args[1], args[2], args[3:]
        else:
            currency, due, words = self._settings.account.base_currency, args[1], args[2:]
        reason = " ".join(words) or "withdrawal announced on Telegram"
        shown = await cockpit.cash_reserve(amount, currency, by=due, reason=reason, confirm=False)
        return self._echo(
            shown["preview"],
            lambda: cockpit.cash_reserve(amount, currency, by=due, reason=reason, confirm=True),
        )

    async def _confirm(self, code: str) -> str:
        pending = self._pending.pop(code, None)
        if pending is None or pending.expires <= self._now():
            return "refused: no command waits for that code (it may have expired); send it again"
        done = await pending.action()
        details = ", ".join(f"{k}: {v}" for k, v in done.items() if k != "confirmed")
        return f"done: {details}"

    async def _pause(self, rest: str) -> str:
        if rest.lower() in ("off", "resume", "no"):
            state = await self._cockpit.set_pause(on=False, reason="")
        else:
            state = await self._cockpit.set_pause(on=True, reason=rest or "/pause from Telegram")
        return str(state["state"])

    # ── replies ──

    async def _status(self) -> str:
        s = await self._cockpit.status()
        beat = s["heartbeat"]
        heartbeat = (
            beat.get("note", "none")
            if beat.get("at") is None
            else f"{beat['at']} ({beat['age_seconds']} s ago{', STALE' if beat['stale'] else ''})"
            f", {beat['status']}, session {beat['session']}"
        )
        nav = s["nav"]
        run = s["reconciliation"]
        return "\n".join(
            [
                f"mode {s['mode']} · kill switch {s['kill_switch']} · {s['pause']}",
                f"heartbeat: {heartbeat}",
                "reconciliation: none yet"
                if run is None
                else f"reconciliation: {run['status']} at {run['at']}",
                "NAV: none recorded"
                if nav is None
                else f"NAV: {nav['value']} {nav['currency']} ({nav['as_of']})",
                f"open intents {s['open_intents']} · open questions {s['open_questions']}",
            ]
        )

    async def _positions(self) -> str:
        p = await self._cockpit.portfolio()
        if "positions" not in p:
            return str(p["board"])
        lines = [f"as the service read it at {p['read_by_service_at']}"]
        if p.get("stale"):
            lines.append(f"STALE: {p['stale_note']}")
        capital = p.get("capital")
        if capital is not None:
            lines.append(
                f"managed {capital['managed']} {p['base_currency']}, investable "
                f"{capital['investable']}"
            )
        lines += [
            f"{x['role']} {x['symbol']}: held {x['held']} = {_shown(x['value'])}; target "
            f"{x['target_value']}; {x['action']}"
            for x in p["positions"]
        ] or ["no position and no target"]
        if p.get("blocked"):
            lines.append(f"no rebalance now: {p['blocked']}")
        return "\n".join(lines)

    async def _intents(self) -> str:
        found = await self._cockpit.intents()
        lines = [f"{found['count']} open intent(s)"]
        lines += [
            f"{i['intent_id']} {i['sleeve']} {i['side']} conid {i['conid']} {i['state']}: "
            f"{i['reason']}"
            for i in found["intents"]
        ]
        return "\n".join(lines)

    async def _theses(self) -> str:
        found = await self._cockpit.theses()
        lines = [f"{len(found['open'])} open thesis(es)"]
        lines += [
            f"{t['ref']} {t['symbol']} {t['state']} ({t['conviction']}): target {t['target']}, "
            f"invalidation {t['invalidation']}, horizon {t['horizon']}"
            for t in found["open"]
        ]
        lines += [line.strip() for line in found["book"].get("lines", []) if line.strip()]
        return "\n".join(lines)

    async def _questions(self) -> str:
        listed = await self._cockpit.list_questions()
        questions = listed["questions"]
        if not questions:
            return "no question waits for you"
        return "\n".join(
            f"{q['id']} by {q['deadline']} (default {q['default']}): {q['context']}; options "
            f"{', '.join(o['key'] for o in q['options'])}"
            for q in questions
        )

    async def _settings_reply(self) -> str:
        found = await self._cockpit.settings()
        lines = ["Your settings:"]
        lines += [f"  {s['key']}: {s['value']}" for s in found["owner_settings"]]
        lines.append("Changeable here (/set), and how:")
        for rule in found["remote_changeable"]:
            how = (
                f"only {rule['tighter']}"
                if "tighter" in rule
                else f"between {rule['min']} and {rule['max']}"
            )
            lines.append(f"  {rule['key']} = {rule['value']} ({how})")
        if not found["service_runs_on_these"]:
            lines.append("The service runs on other settings until its next cycle boundary.")
        return "\n".join(lines)
