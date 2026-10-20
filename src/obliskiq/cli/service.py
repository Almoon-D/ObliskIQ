"""``obliskiq serve``, ``run-cycle``, ``status``, ``intents list`` and ``holdings list|assign``.

``serve`` runs the service until SIGTERM or SIGINT (``orchestration/service.py``). ``run-cycle
--mode shadow|paper`` runs one decision cycle now, in the mode the store holds (set it first with
``obliskiq mode set``); ``--dry-run`` reads the broker and prints the plan and writes nothing.
Both take the service lock: one process talks to the broker and runs the executor at a time.
``status`` reads the store, the heartbeat file and the alert state, and opens no broker session,
so it is safe beside a running service; so are ``intents list`` and ``holdings``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
import json
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from ..domain.orders import (
    TERMINAL_STATES,
    UNMANAGED,
    ExecutionMode,
    HoldingAssignment,
    HoldingOwner,
    IntentState,
    Sleeve,
)
from ..errors import ObliskError
from ..execution.killswitch import file_flag_on
from ..orchestration.assemble import build_notifier, build_reporter, open_service
from ..orchestration.clock import SystemClock
from ..orchestration.health import read_heartbeat
from ..orchestration.lock import ServiceLock, ServiceLockedError
from ..orchestration.runlog import exit_code
from ..reporting.summary import CycleSummary, format_cycle
from ..store import nav as nav_repo
from ..store import open_store
from ..store import orders as repo
from ..store import runs as run_repo

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1
#: The run log's command for a cycle the owner runs by hand.
MANUAL_CYCLE = "manual"
OWNER_ACTOR = "owner (command line)"


def _lock(loaded: Loaded, command: str) -> ServiceLock:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    return ServiceLock(data_dir / settings.paths.service_lock, command=command)


def _serve(loaded: Loaded, out: TextIO) -> int:
    """Run the service until SIGTERM or SIGINT."""
    from .app import _broker_refusal

    refused = _broker_refusal(loaded.env, out)
    if refused is not None:
        return refused
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()

    async def serve() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        reporter = build_reporter(settings, build_notifier(settings, loaded.env), data_dir)
        async with open_service(
            loaded.env,
            settings,
            loaded.profile,
            data_dir=data_dir,
            clock=SystemClock(),
            reporter=reporter,
        ) as service:
            await service.run(stop)

    try:
        with _lock(loaded, "serve"):
            print("obliskiq service starting; SIGTERM or Ctrl-C stops it", file=out)
            asyncio.run(serve())
    except ServiceLockedError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    except ObliskError as exc:
        print(f"the service stopped: {exc}", file=out)
        return EXIT_INVALID
    print("obliskiq service stopped", file=out)
    return EXIT_OK


def _run_cycle(loaded: Loaded, out: TextIO) -> int:
    """One decision cycle now, in the mode the store holds; ``--dry-run`` writes nothing."""
    from .app import _broker_refusal

    refused = _broker_refusal(loaded.env, out)
    if refused is not None:
        return refused
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    wanted = ExecutionMode(loaded.args.mode)
    dry_run = bool(loaded.args.dry_run)

    async def cycle() -> int:
        reporter = build_reporter(settings, build_notifier(settings, loaded.env), data_dir)
        clock = SystemClock()
        async with open_service(
            loaded.env,
            settings,
            loaded.profile,
            data_dir=data_dir,
            clock=clock,
            reporter=reporter,
        ) as service:
            current = await service.mode()
            if current is not wanted:
                print(
                    f"refused: the mode is {current.value}, not {wanted.value}; change it with "
                    f"`obliskiq mode set {wanted.value} --reason ...` first",
                    file=out,
                )
                return EXIT_INVALID
            await service.prepare(dry_run=dry_run)
            result = await service.jobs.cycle(MANUAL_CYCLE, clock.now().date(), dry_run=dry_run)
            if result.plan is not None:
                print(result.plan.render(), file=out)
            if result.nav is not None:
                print(f"NAV: {result.nav.reason}", file=out)
            if not dry_run:
                summary = CycleSummary(
                    name=MANUAL_CYCLE,
                    mode=current,
                    status=result.status,
                    plan=None,
                    made=tuple(result.made),
                    report=result.report,
                )
                print(format_cycle(summary)[1], file=out)
            else:
                print("dry run: nothing was written or sent", file=out)
            print(f"cycle {result.status}", file=out)
            return exit_code(result.status)

    try:
        with _lock(loaded, "run-cycle"):
            return asyncio.run(cycle())
    except ServiceLockedError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    except ObliskError as exc:
        print(f"the cycle stopped: {exc}", file=out)
        return EXIT_INVALID


def _status(loaded: Loaded, out: TextIO) -> int:
    """The service's state from the store and its files; opens no broker session."""
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    now = datetime.now(UTC)
    since = now.date() - timedelta(days=settings.runtime.status_report_days)
    with open_store(settings, data_dir) as store:
        change = repo.latest_mode_change(store)
        kill = repo.latest_kill_switch(store)
        latest = repo.latest_reconciliation(store)
        runs = run_repo.runs_since(store, since=since)
        series = nav_repo.nav_series(store, since=since, as_of=now.date())
        open_states = [state for state in IntentState if state not in TERMINAL_STATES]
        open_intents = repo.intents_in_states(store, open_states)
    mode = settings.execution.modes.initial if change is None else change.to_mode
    kill_on = file_flag_on(data_dir) or (kill is not None and kill.on)
    print(f"mode {mode.value} · kill switch {'ON' if kill_on else 'off'}", file=out)
    beat, facts = read_heartbeat(data_dir / settings.paths.heartbeat_file)
    if beat is None:
        print("heartbeat: none recorded (the service has not run here)", file=out)
    else:
        age = (now - beat).total_seconds()
        stale = age > 2 * settings.runtime.heartbeat_seconds
        state = facts.get("status", "?")
        print(
            f"heartbeat: {beat.isoformat()} ({age:.0f} s ago{', STALE' if stale else ''}), "
            f"{state}; session {facts.get('session', '?')}",
            file=out,
        )
    print(
        "reconciliation: none yet"
        if latest is None
        else f"reconciliation: {latest.status.value} at {latest.at.isoformat()}",
        file=out,
    )
    print(
        "NAV: none recorded"
        if not series
        else f"NAV: {series[-1].nav_base} {series[-1].currency} ({series[-1].as_of.isoformat()})",
        file=out,
    )
    print(f"open intents: {len(open_intents)}", file=out)
    alerts = _open_alerts(data_dir / settings.paths.alert_state)
    print(f"open alerts: {len(alerts)}", file=out)
    for key in alerts:
        print(f"  {key}", file=out)
    print(f"runs since {since.isoformat()}, most recent first:", file=out)
    if not runs:
        print("  none. Nothing has run: that is not a healthy state, only an empty one.", file=out)
    for run in runs:
        mark = "!" if run.status in ("error", "partial", "interrupted", "running") else " "
        print(
            f" {mark} {run.started_at.isoformat()} {run.command:<14} {run.status:<11} "
            f"{run.steps_completed}/{run.steps_planned} steps",
            file=out,
        )
    return EXIT_OK


def _open_alerts(path: Path) -> list[str]:
    """The conditions the alert state holds (``reporting/alerts.py``)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return sorted(str(key) for key in data["held"])
    except OSError, ValueError, KeyError, TypeError, AttributeError:
        return []


def _intents_list(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    states = (
        list(IntentState)
        if loaded.args.all
        else [state for state in IntentState if state not in TERMINAL_STATES]
    )
    with open_store(settings, data_dir) as store:
        stored = repo.intents_in_states(store, states)
    stored.sort(key=lambda s: s.intent.created_at)
    print(f"{len(stored)} intent(s){'' if loaded.args.all else ' open'}", file=out)
    for entry in stored:
        intent = entry.intent
        size = (
            intent.terms.quantity
            if intent.terms.quantity is not None
            else intent.terms.cash_quantity
        )
        print(
            f"  {intent.intent_id}  {intent.created_at.strftime('%Y-%m-%d %H:%M')}  "
            f"{intent.mode.value:<6} {intent.sleeve.value:<9} {intent.role.value:<10} "
            f"{intent.side.value} {size} conid {intent.conid} limit {intent.terms.price.limit}  "
            f"{intent.state.value}  ({intent.reason})",
            file=out,
        )
    return EXIT_OK


def _holdings_list(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    with open_store(settings, loaded.env.require_data_dir()) as store:
        assignments = repo.holding_assignments(store)
    print(f"{len(assignments)} holding(s) registered", file=out)
    for a in assignments:
        owner = a.owner if a.owner == UNMANAGED else Sleeve(a.owner).value
        print(
            f"  conid {a.conid}: {owner} (by {a.assigned_by}, {a.assigned_at.isoformat()}: "
            f"{a.reason})",
            file=out,
        )
    return EXIT_OK


def _holdings_assign(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    owner_text: str = loaded.args.owner
    owner: HoldingOwner
    if owner_text == UNMANAGED:
        owner = UNMANAGED
    else:
        try:
            owner = Sleeve(owner_text)
        except ValueError:
            names = ", ".join([*(s.value for s in Sleeve), UNMANAGED])
            print(f"refused: {owner_text!r} is not one of {names}", file=out)
            return EXIT_INVALID
    assignment = HoldingAssignment(
        conid=loaded.args.conid,
        owner=owner,
        assigned_by=OWNER_ACTOR,
        assigned_at=datetime.now(UTC),
        reason=loaded.args.reason,
    )
    with open_store(settings, loaded.env.require_data_dir()) as store:
        repo.assign_holding(store, assignment)
    print(f"conid {assignment.conid} assigned to {owner_text}", file=out)
    return EXIT_OK


def add_service_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    serve = commands.add_parser("serve", help="run the service until SIGTERM (MASTER_PLAN §5.14)")
    serve.set_defaults(handler=_serve)

    cycle = commands.add_parser("run-cycle", help="run one decision cycle now")
    cycle.add_argument(
        "--mode",
        required=True,
        choices=[ExecutionMode.SHADOW.value, ExecutionMode.PAPER.value],
        help="the mode the cycle runs in; it must be the mode the store holds",
    )
    cycle.add_argument(
        "--dry-run", action="store_true", help="read the broker and print the plan; write nothing"
    )
    cycle.set_defaults(handler=_run_cycle)

    status = commands.add_parser(
        "status", help="mode, kill switch, heartbeat, runs and alerts (no broker session)"
    )
    status.set_defaults(handler=_status)

    intents = commands.add_parser("intents", help="the order intents")
    intent_actions = intents.add_subparsers(dest="action", required=True, metavar="ACTION")
    listing = intent_actions.add_parser("list", help="open intents (--all: every intent)")
    listing.add_argument("--all", action="store_true", help="every intent, closed ones too")
    listing.set_defaults(handler=_intents_list)

    holdings = commands.add_parser("holdings", help="the holdings registry")
    holding_actions = holdings.add_subparsers(dest="action", required=True, metavar="ACTION")
    holding_actions.add_parser("list", help="every holding and its owner").set_defaults(
        handler=_holdings_list
    )
    assign = holding_actions.add_parser("assign", help="assign a holding to a sleeve")
    assign.add_argument("conid", type=int, help="the contract id")
    assign.add_argument("owner", help="core, satellite, options or unmanaged")
    assign.add_argument("--reason", required=True, help="why (audited)")
    assign.set_defaults(handler=_holdings_assign)
