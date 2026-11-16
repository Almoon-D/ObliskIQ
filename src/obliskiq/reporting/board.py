"""The board: what the service read at its last digest, as JSON for the cockpit and the dashboard.

The cockpit opens no brokerage session (one session per username, and the service holds it), so
what only a survey of the broker can show (the book's capital, positions and targets, the thesis
plan, the reservation plan) reaches it through this file: after each digest the service writes
the board (``paths.board``) atomically, and the cockpit reads it, labelled with its age. Amounts
are kept as exact decimal text, never floats. A part the digest could not read is carried as its
reason, never left out.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import os
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .. import __version__
from ..config.schema import ObliskConfig
from ..scoreboard.run import PerformanceView
from .digest import Digest, breaker_lines, capital_lines, thesis_lines
from .scoreboard import performance_lines

#: The board's format; a reader refuses another.
BOARD_FORMAT = 1


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _plan(digest: Digest) -> dict[str, Any]:
    plan = digest.plan
    if plan is None:
        return {"surveyed": False, "capital": None, "core": None, "positions": []}
    capital = plan.capital
    portfolio = plan.portfolio
    return {
        "surveyed": True,
        "at": plan.at.isoformat(),
        "blocked": plan.blocked,
        "capital": None
        if capital is None
        else {
            "mode": capital.mode,
            "detail": capital.detail,
            "nav": _text(capital.nav),
            "managed": _text(capital.managed),
            "unmanaged": _text(capital.unmanaged),
            "reserved": _text(capital.reserved),
            "investable": _text(capital.investable),
            "book_value": _text(capital.book_value),
            "book_cash": _text(capital.book_cash),
        },
        "core_budget": _text(plan.core_capital),
        "spendable": _text(plan.spendable),
        "core": None
        if portfolio is None
        else {
            "variant": portfolio.variant,
            "trigger": portfolio.trigger.value,
            "trend_as_of": None
            if portfolio.trend_as_of is None
            else portfolio.trend_as_of.isoformat(),
            "roles": [
                {
                    "role": role.role,
                    "status": role.status.value,
                    "target_weight": _text(role.target_weight),
                    "held_weight": _text(role.held_weight),
                    "reason": role.reason,
                }
                for role in portfolio.roles
            ],
        },
        "positions": [
            {
                "role": d.role,
                "symbol": d.symbol,
                "conid": d.conid,
                "held": _text(d.held),
                "value": _text(d.value),
                "target_weight": _text(d.weight),
                "target_value": _text(d.target),
                "action": d.action,
                "reason": d.reason,
            }
            for d in plan.decisions
        ],
    }


def _performance(view: PerformanceView | str) -> dict[str, Any]:
    if isinstance(view, str):
        return {"measured": False, "reason": view, "series": [], "lines": []}
    return {
        "measured": True,
        "since": view.since.isoformat(),
        "as_of": view.as_of.isoformat(),
        "series": [
            {"name": item.name, "growth": _text(item.growth), "reason": item.reason}
            for item in (view.account, *view.references)
        ],
        "lines": performance_lines(view),
    }


def _budgets(settings: ObliskConfig, shares: dict[str, Decimal]) -> dict[str, Any]:
    budgets = settings.capital.budgets
    return {
        "configured": {
            "core": _text(budgets.core),
            "equity_satellite": _text(budgets.equity_satellite),
            "option_premium": _text(budgets.option_premium),
            "cash_floor": _text(budgets.cash_floor),
            "thesis_book_share": _text(settings.thesis.capital_share),
        },
        "live_shares": {layer: _text(share) for layer, share in sorted(shares.items())},
    }


def _settings_in_force(settings: ObliskConfig) -> dict[str, Any]:
    """The settings the owner most often asks about: the mode's capital, the core's variant and
    every setting the remote channels may change, with its value now."""
    flat = settings.model_dump(mode="json")
    values: dict[str, Any] = {
        "capital.mode": settings.capital.mode,
        "capital.cap_base": _text(settings.capital.cap_base),
        "core.variant": settings.core.variant,
    }
    for rule in settings.remote_settings.rules():
        node: Any = flat
        for part in rule.key.split("."):
            node = node[part]
        values[rule.key] = node
    return values


def build_board(
    digest: Digest,
    settings: ObliskConfig,
    *,
    performance: PerformanceView | str,
    shares: dict[str, Decimal],
    patterns: Sequence[dict[str, Any]] | str,
) -> dict[str, Any]:
    """The board from a gathered digest and what the store adds to it."""
    rows = settings.reporting.digest_rows
    nav = digest.nav
    twr = digest.twr
    run = digest.reconcile
    return {
        "format": BOARD_FORMAT,
        "version": __version__,
        "at": digest.at.isoformat(),
        "day": digest.day.isoformat(),
        "base_currency": settings.account.base_currency,
        "mode": None if digest.mode is None else digest.mode.value,
        "kill_switch": digest.kill_on,
        "reconciliation": None
        if run is None
        else {"status": run.status.value, "at": run.at.isoformat()},
        "nav": None
        if nav is None
        else {"value": _text(nav.nav_base), "currency": nav.currency, "as_of": nav.as_of},
        "twr": None
        if twr is None
        else {
            "value": None if twr.value is None else str(twr.value),
            "since": twr.since,
            "until": twr.until,
            "reason": twr.reason,
        },
        "performance": _performance(performance),
        "budgets": _budgets(settings, shares),
        "breakers": breaker_lines(digest.breakers),
        "book": _plan(digest),
        "theses": thesis_lines(digest, rows)[2:],
        "capital_lines": [line for line in capital_lines(digest, rows) if line],
        "patterns": patterns if isinstance(patterns, str) else list(patterns),
        "intents": [
            {
                "intent_id": str(s.intent.intent_id),
                "created_at": s.intent.created_at.isoformat(),
                "sleeve": s.intent.sleeve.value,
                "role": s.intent.role.value,
                "side": s.intent.side.value,
                "size": _text(s.intent.terms.quantity or s.intent.terms.cash_quantity),
                "conid": s.intent.conid,
                "state": s.intent.state.value,
                "reason": s.intent.reason,
            }
            for s in digest.intents
        ],
        "venues": [
            {"venue": v.venue, "badge": v.badge, "text": v.text, "blocked": v.blocked}
            for v in digest.venues
        ],
        "health": list(digest.health),
        "problems": list(digest.problems),
        "alerts": dict(digest.alerts),
        "settings": _settings_in_force(settings),
    }


def read_patterns(path: Path) -> list[dict[str, Any]] | str:
    """Every hypothesis the research registry records: its id, hypothesis, title, lifecycle
    status and decision; why not, when the registry cannot be read (an installed wheel has
    none)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, yaml.YAMLError:
        return f"the research registry cannot be read at {path}"
    entries = data.get("patterns") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return f"{path} holds no list of patterns"
    found: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        outcome = entry.get("outcome")
        found.append(
            {
                "id": str(entry.get("id")),
                "hypothesis": str(entry.get("hypothesis")),
                "title": str(entry.get("title")),
                "status": str(entry.get("status")),
                "decision": outcome.get("decision") if isinstance(outcome, dict) else None,
            }
        )
    return found


def write_text_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def write_board(path: Path, board: dict[str, Any]) -> None:
    """Write the board atomically."""
    write_text_atomically(path, json.dumps(board, indent=1, sort_keys=True, default=str))


def read_board(path: Path) -> dict[str, Any] | None:
    """The last board written, or ``None`` when there is none or it cannot be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(data, dict) or data.get("format") != BOARD_FORMAT:
        return None
    return data
