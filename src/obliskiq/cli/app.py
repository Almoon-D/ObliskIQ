"""The ``obliskiq`` command line.

``obliskiq config validate`` checks the whole configuration: the expert defaults, the
owner's panel, the jurisdiction profile it selects and the policies that profile declares.
``obliskiq settings show`` prints every effective setting and the file it comes from.
``obliskiq kill on|off|status`` sets, clears or shows the kill switch; ``kill on`` works even when
the configuration does not load (it then sets the file flag alone). ``obliskiq mode show|set``
shows or changes the execution mode (upgrades by this explicit command, audited).
``obliskiq flatten --confirm`` places closing orders for every managed position (separate from
the kill switch). ``obliskiq preflight --paper-order-test`` places and cancels one paper order per
configured instrument. ``obliskiq approvals list|approve|reject`` works the approval queue.
``obliskiq serve``, ``run-cycle``, ``status``, ``intents list`` and ``holdings list|assign`` run
and inspect the service (``cli/service.py``). ``obliskiq cash``, ``questions``, ``holdings
external`` and ``tax report`` are the owner's cash flows, questions, external holdings and tax
lots (``cli/capital.py``). Every command that opens a brokerage session takes
the service lock, so it refuses while the service runs.
``obliskiq research data load|coverage``, ``research power``, ``research run`` and
``research lock`` drive the research harness (``cli/research.py``).
Any problem is printed for the owner, in plain words, with exit status 1 and no traceback.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
import sqlite3
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .. import __version__
from ..config import LoadedConfig, load_config
from ..domain.orders import ExecutionMode, KillSwitchRecord, ReconcileStatus
from ..env import Environment, default_env_file, load_environment
from ..errors import ConfigError, EnvVarError, ModeChangeRefusedError, ObliskError, StoreError
from ..execution.approvals import ApprovalQueue
from ..execution.killswitch import clear_file_flag, file_flag_on, set_file_flag
from ..execution.live_test import ExecutionTest
from ..execution.modes import Modes
from ..execution.paper_test import PaperOrderTest
from ..execution.runtime import OrderPath, open_order_path
from ..execution.state import IntentBook
from ..ibkr.preflight import run_read_only
from ..ibkr.transport import missing_credentials, transport_refusal
from ..jurisdiction import (
    ELIGIBILITY_POLICIES,
    SHIPPED_POLICY_KINDS,
    TAX_POLICIES,
    JurisdictionProfile,
    check_declared_policies,
    load_profile,
)
from ..logging_setup import configure_logging
from ..orchestration.lock import ServiceLock
from ..store import open_store
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .capital import add_capital_commands
from .research import add_research_commands
from .service import add_service_commands

EXIT_OK = 0
EXIT_INVALID = 1
#: The preflight found no IBKR credentials in the environment: nothing was attempted.
EXIT_NO_CREDENTIALS = 2

#: Longest value ``settings show`` prints in full; longer ones (a list of phase windows) are cut.
VALUE_LIMIT = 48

#: Who the audit names for a change made from the command line.
OWNER_ACTOR = "owner (command line)"


@dataclass(frozen=True)
class Loaded:
    """Everything a command needs: the validated configuration and the active profile."""

    config: LoadedConfig
    profile: JurisdictionProfile
    env: Environment
    args: argparse.Namespace = field(default_factory=argparse.Namespace)


type Handler = Callable[[Loaded, TextIO], int]


def load_all(config_dir: Path, env: Environment) -> Loaded:
    """Load the configuration and its jurisdiction profile, failing loudly on any problem."""
    loaded = load_config(config_dir, overrides=env.config_overrides())
    settings = loaded.settings
    configure_logging(settings.logging.level, settings.logging.format)
    profile = load_profile(config_dir, settings.jurisdiction)
    check_declared_policies(profile, SHIPPED_POLICY_KINDS)
    # Each declared policy's data must build it.
    for kind in (ELIGIBILITY_POLICIES, TAX_POLICIES):
        if kind.kind in profile.policies:
            kind.resolve(profile, settings)
    return Loaded(config=loaded, profile=profile, env=env)


def _config_validate(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    profile = loaded.profile
    declared = sorted(profile.policies.items())
    policies = ", ".join(f"{kind}: {pid}" for kind, pid in declared) or "none declared"
    print(f"configuration is valid ({', '.join(loaded.config.files)})", file=out)
    print(f"  base currency  {settings.account.base_currency}", file=out)
    print(f"  jurisdiction   {profile.id} ({profile.name})", file=out)
    print(f"  policies       {policies}", file=out)
    return EXIT_OK


def _shorten(text: str) -> str:
    return text if len(text) <= VALUE_LIMIT else f"{text[: VALUE_LIMIT - 1]}…"


def _settings_show(loaded: Loaded, out: TextIO) -> int:
    profile = loaded.profile
    rows = [("SETTING", "VALUE", "SOURCE")]
    rows += [(key, _shorten(str(value)), source) for key, value, source in loaded.config.leaves()]
    key_width = max(len(row[0]) for row in rows)
    value_width = max(len(row[1]) for row in rows)
    print(f"Effective settings — jurisdiction {profile.id} ({profile.name})", file=out)
    print("", file=out)
    for key, value, source in rows:
        print(f"{key:<{key_width}}  {value:<{value_width}}  {source}", file=out)
    return EXIT_OK


def _broker_refusal(env: Environment, out: TextIO) -> int | None:
    """The exit status when the environment cannot reach IBKR; ``None`` when it can."""
    # An unset profile is reported as unset (below), not as a refused combination.
    refusal = transport_refusal(env) if env.ibkr_profile is not None else None
    if refusal is not None:
        # Before the credential check: a refused combination never asks for its keys.
        print(f"refused: {refusal}", file=out)
        return EXIT_INVALID
    missing = missing_credentials(env)
    if missing:
        print("no credentials", file=out)
        print(f"  unset: {', '.join(missing)}", file=out)
        return EXIT_NO_CREDENTIALS
    return None


def _account(env: Environment) -> str:
    return env.ibkr_account_id.get_secret_value() if env.ibkr_account_id else ""


def _preflight(loaded: Loaded, out: TextIO) -> int:
    """``preflight --read-only`` walks the IBKR read path; ``--paper-order-test`` places and
    cancels one paper order per configured instrument; ``--execution-test`` runs Session 5's
    live steps (``--finish`` for the second phase)."""
    env = loaded.env
    if loaded.args.finish and not loaded.args.execution_test:
        print("--finish goes with --execution-test", file=out)
        return EXIT_INVALID
    refused = _broker_refusal(env, out)
    if refused is not None:
        return refused
    if loaded.args.paper_order_test:
        return _paper_order_test(loaded, out)
    if loaded.args.execution_test:
        return _execution_test(loaded, out)
    report = asyncio.run(run_read_only(env, loaded.config.settings, data_dir=env.data_dir))
    print(report.render((_account(env),)), file=out)
    return EXIT_OK if report.ok else EXIT_INVALID


def _with_order_path[T](loaded: Loaded, run: Callable[[OrderPath], Awaitable[T]]) -> T:
    """Run ``run`` over an order path opened for this command, and close it, under the service
    lock: one process talks to the broker and runs the executor at a time (a running service
    must be stopped first; ``ServiceLockedError`` says so)."""
    data_dir = loaded.env.require_data_dir()

    async def go() -> T:
        async with open_order_path(
            loaded.env,
            loaded.config.settings,
            loaded.profile,
            data_dir=data_dir,
        ) as path:
            return await run(path)

    lock_path = data_dir / loaded.config.settings.paths.service_lock
    with ServiceLock(lock_path, command=str(loaded.args.command)):
        return asyncio.run(go())


def _paper_order_test(loaded: Loaded, out: TextIO) -> int:
    try:
        report = _with_order_path(loaded, lambda path: PaperOrderTest(path).run())
    except ObliskError as exc:
        print(f"paper order test stopped: {exc}", file=out)
        return EXIT_INVALID
    print(report.render((_account(loaded.env),)), file=out)
    return EXIT_OK if report.ok else EXIT_INVALID


def _execution_test(loaded: Loaded, out: TextIO) -> int:
    """Session 5's live steps on paper: phase one, or phase two with ``--finish``."""
    finish = bool(loaded.args.finish)
    try:
        report = _with_order_path(
            loaded,
            lambda path: ExecutionTest(path).finish() if finish else ExecutionTest(path).run(),
        )
    except ObliskError as exc:
        print(f"execution test stopped: {exc}", file=out)
        return EXIT_INVALID
    text = report.render((_account(loaded.env),))
    print(text.replace("IBKR paper order test:", "IBKR execution test:", 1), file=out)
    return EXIT_OK if report.ok else EXIT_INVALID


def _reconcile(loaded: Loaded, out: TextIO) -> int:
    """``reconcile run`` (one reconciliation) or ``reconcile accept`` (the owner accepts the
    broker's state as the new anchor, lifting a drift's freeze)."""
    refused = _broker_refusal(loaded.env, out)
    if refused is not None:
        return refused
    accept = loaded.args.action == "accept"
    try:
        run = _with_order_path(
            loaded,
            lambda path: (
                path.reconciler.accept(actor=OWNER_ACTOR, reason=loaded.args.reason)
                if accept
                else path.reconciler.run()
            ),
        )
    except ObliskError as exc:
        print(f"reconciliation stopped: {exc}", file=out)
        return EXIT_INVALID
    print(f"reconciliation {run.status.value} at {run.at.isoformat()}", file=out)
    for finding in run.findings:
        mark = "freezes" if finding.freezes else "note"
        print(f"  [{mark}] {finding.kind}: {finding.detail}", file=out)
    return EXIT_OK if run.status is ReconcileStatus.GREEN else EXIT_INVALID


def _flatten(loaded: Loaded, out: TextIO) -> int:
    """``flatten --confirm``: closing orders for every managed position."""
    if not loaded.args.confirm:
        print(
            "flatten sells every managed position at the bid; nothing was done. Run "
            "`obliskiq flatten --confirm` to go ahead.",
            file=out,
        )
        return EXIT_INVALID
    refused = _broker_refusal(loaded.env, out)
    if refused is not None:
        return refused
    try:
        report = _with_order_path(loaded, lambda path: path.flattener.flatten(actor=OWNER_ACTOR))
    except ObliskError as exc:
        print(f"flatten stopped: {exc}", file=out)
        return EXIT_INVALID
    print(report.render(), file=out)
    return EXIT_OK if report.ok else EXIT_INVALID


def _approval_queue(loaded: Loaded) -> tuple[ApprovalQueue, StoreWorker]:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    worker = StoreWorker(lambda: open_store(settings, data_dir))
    book = IntentBook(worker, coid_max_length=settings.execution.orders.coid_max_length)
    return ApprovalQueue(settings, worker, book), worker


def _approvals_list(loaded: Loaded, out: TextIO) -> int:
    async def pending() -> list[str]:
        queue, worker = _approval_queue(loaded)
        try:
            return [
                f"  {stored.intent.intent_id}  {stored.intent.sleeve.value}  "
                f"{stored.intent.side.value} conid {stored.intent.conid}  {stored.intent.reason}"
                for stored in await queue.pending()
            ]
        finally:
            await worker.aclose()

    rows = asyncio.run(pending())
    print(f"{len(rows)} intent(s) waiting for approval", file=out)
    for row in rows:
        print(row, file=out)
    return EXIT_OK


def _approvals_decide(loaded: Loaded, out: TextIO) -> int:
    approve = loaded.args.action == "approve"
    try:
        intent_id = uuid.UUID(loaded.args.intent_id)
    except ValueError:
        print(f"refused: {loaded.args.intent_id!r} is not an intent id", file=out)
        return EXIT_INVALID

    async def decide() -> None:
        queue, worker = _approval_queue(loaded)
        try:
            call = queue.approve if approve else queue.reject
            await call(intent_id, actor=OWNER_ACTOR, reason=loaded.args.reason)
        finally:
            await worker.aclose()

    try:
        asyncio.run(decide())
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    print(f"intent {intent_id} {'approved' if approve else 'rejected'}", file=out)
    return EXIT_OK


def _kill_on_flags(env: Environment, loaded: Loaded | None, reason: str, out: TextIO) -> int:
    """Set the file flag and, when the configuration loaded, the database flag."""
    data_dir = env.require_data_dir()
    at = utc_now()
    problems: list[str] = []
    file_set = database_set = False
    try:
        set_file_flag(data_dir, actor=OWNER_ACTOR, reason=reason, at=at)
        file_set = True
    except OSError as exc:
        problems.append(f"the file flag could not be written: {exc}")
    if loaded is None:
        problems.append("the configuration did not load, so the database flag was not set")
    else:
        try:
            with open_store(loaded.config.settings, data_dir) as store:
                record = KillSwitchRecord(at=at, on=True, actor=OWNER_ACTOR, reason=reason)
                repo.record_kill_switch(store, record)
            database_set = True
        except (StoreError, OSError, sqlite3.Error) as exc:
            problems.append(f"the database flag could not be written: {exc}")
    if not (file_set or database_set):
        print("kill switch NOT set: " + "; ".join(problems), file=out)
        return EXIT_INVALID
    print(
        "kill switch ON: orders that add risk are blocked; working ones are cancelled only "
        "where the service's sweep runs, so check the broker's open orders",
        file=out,
    )
    for problem in problems:
        print(f"  warning: {problem}", file=out)
    return EXIT_OK


def _kill_on(loaded: Loaded, out: TextIO) -> int:
    return _kill_on_flags(loaded.env, loaded, loaded.args.reason, out)


def _kill_off(loaded: Loaded, out: TextIO) -> int:
    data_dir = loaded.env.require_data_dir()
    with open_store(loaded.config.settings, data_dir) as store:
        record = KillSwitchRecord(
            at=utc_now(), on=False, actor=OWNER_ACTOR, reason=loaded.args.reason
        )
        repo.record_kill_switch(store, record)
        clear_file_flag(data_dir)
        latest = repo.latest_kill_switch(store)
    if file_flag_on(data_dir) or latest is None or latest.on:
        print("kill switch still ON: a flag could not be cleared", file=out)
        return EXIT_INVALID
    print("kill switch OFF", file=out)
    return EXIT_OK


def _kill_status(loaded: Loaded, out: TextIO) -> int:
    data_dir = loaded.env.require_data_dir()
    file_on = file_flag_on(data_dir)
    try:
        with open_store(loaded.config.settings, data_dir) as store:
            latest = repo.latest_kill_switch(store)
        database = "on" if latest is not None and latest.on else "off"
    except StoreError, OSError, sqlite3.Error:
        database = "unreadable"
    on = file_on or database != "off"
    print(f"kill switch {'ON' if on else 'OFF'}", file=out)
    print(f"  file flag      {'on' if file_on else 'off'}", file=out)
    print(f"  database flag  {database}", file=out)
    return EXIT_OK


def _modes(loaded: Loaded) -> tuple[Modes, StoreWorker]:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    worker = StoreWorker(lambda: open_store(settings, data_dir))
    return Modes(worker, settings, profile=loaded.env.ibkr_profile), worker


def _mode_show(loaded: Loaded, out: TextIO) -> int:
    async def show() -> ExecutionMode:
        modes, worker = _modes(loaded)
        try:
            return await modes.current()
        finally:
            await worker.aclose()

    print(f"execution mode {asyncio.run(show()).value}", file=out)
    return EXIT_OK


def _mode_set(loaded: Loaded, out: TextIO) -> int:
    target = ExecutionMode(loaded.args.mode)
    reason: str = loaded.args.reason

    async def change() -> str:
        modes, worker = _modes(loaded)
        try:
            current = await modes.current()
            if target is current:
                return f"execution mode is already {current.value}"
            if target.rank > current.rank:
                await modes.upgrade(target, actor=OWNER_ACTOR, reason=reason)
            else:
                await modes.downgrade(target, actor=OWNER_ACTOR, reason=reason)
            return f"execution mode {current.value} -> {target.value}"
        finally:
            await worker.aclose()

    try:
        print(asyncio.run(change()), file=out)
    except ModeChangeRefusedError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obliskiq",
        description="ObliskIQ: an autonomous, risk-first portfolio manager for one IBKR account.",
    )
    parser.add_argument("--version", action="version", version=f"obliskiq {__version__}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        help="the directory holding owner.yaml, defaults/ and jurisdictions/ "
        "(default: OBLISK_CONFIG_DIR, else config/ of the source checkout)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="read environment variables from this file "
        "(default: .env at the root of the source checkout, when present)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    config = commands.add_parser("config", help="check the configuration")
    config_actions = config.add_subparsers(dest="action", required=True, metavar="ACTION")
    validate = config_actions.add_parser(
        "validate", help="validate the defaults, the owner's settings and the jurisdiction"
    )
    validate.set_defaults(handler=_config_validate)

    settings = commands.add_parser("settings", help="the owner's settings")
    settings_actions = settings.add_subparsers(dest="action", required=True, metavar="ACTION")
    show = settings_actions.add_parser(
        "show", help="print every effective setting and where its value comes from"
    )
    show.set_defaults(handler=_settings_show)

    preflight = commands.add_parser("preflight", help="check the IBKR connection end to end")
    how = preflight.add_mutually_exclusive_group(required=True)
    how.add_argument(
        "--read-only",
        action="store_true",
        help="read only: authentication, accounts, guard, positions, cash, a snapshot, history, "
        "contract rules, algos and the trading schedule for one stock; no order endpoint",
    )
    how.add_argument(
        "--paper-order-test",
        action="store_true",
        help="paper only: place one order that cannot fill per configured instrument, read its "
        "status and reply messages, and cancel it (needs mode paper)",
    )
    how.add_argument(
        "--execution-test",
        action="store_true",
        help="paper only: a conversion, a buy that fills, its protective stop and IBKR's "
        "reporting lag; then, with --finish in a new process, the stop after the restart and "
        "the sale (needs mode paper)",
    )
    preflight.add_argument(
        "--finish",
        action="store_true",
        help="with --execution-test: phase two, after the process was restarted",
    )
    preflight.set_defaults(handler=_preflight)

    reconcile = commands.add_parser(
        "reconcile", help="the broker against the store; drift freezes new risk"
    )
    reconcile_actions = reconcile.add_subparsers(dest="action", required=True, metavar="ACTION")
    reconcile_actions.add_parser("run", help="reconcile once and print the findings").set_defaults(
        handler=_reconcile
    )
    accept = reconcile_actions.add_parser(
        "accept",
        help="accept the broker's positions and cash as they are now (lifts a drift's freeze)",
    )
    accept.add_argument("--reason", required=True, help="why the broker is right (audited)")
    accept.set_defaults(handler=_reconcile)

    flatten = commands.add_parser(
        "flatten", help="place closing orders for every managed position (not the kill switch)"
    )
    flatten.add_argument(
        "--confirm", action="store_true", help="go ahead: without it nothing is done"
    )
    flatten.set_defaults(handler=_flatten)

    approvals = commands.add_parser("approvals", help="the approval queue")
    approval_actions = approvals.add_subparsers(dest="action", required=True, metavar="ACTION")
    approval_actions.add_parser("list", help="intents waiting for approval").set_defaults(
        handler=_approvals_list
    )
    for verb in ("approve", "reject"):
        decide = approval_actions.add_parser(verb, help=f"{verb} an intent held for approval")
        decide.add_argument("intent_id", help="the intent's id (approvals list shows it)")
        decide.add_argument("--reason", required=True, help="why (audited)")
        decide.set_defaults(handler=_approvals_decide)

    kill = commands.add_parser("kill", help="the kill switch")
    kill_actions = kill.add_subparsers(dest="action", required=True, metavar="ACTION")
    kill_on = kill_actions.add_parser(
        "on", help="block every order that adds risk and cancel the working ones"
    )
    kill_on.add_argument("--reason", default="set from the command line", help="for the audit")
    kill_on.set_defaults(handler=_kill_on)
    kill_off = kill_actions.add_parser("off", help="clear the kill switch")
    kill_off.add_argument("--reason", required=True, help="why it is safe again (audited)")
    kill_off.set_defaults(handler=_kill_off)
    kill_actions.add_parser("status", help="show the kill switch").set_defaults(
        handler=_kill_status
    )

    mode = commands.add_parser("mode", help="the execution mode")
    mode_actions = mode.add_subparsers(dest="action", required=True, metavar="ACTION")
    mode_actions.add_parser("show", help="show the current mode").set_defaults(handler=_mode_show)
    mode_set = mode_actions.add_parser(
        "set", help="upgrade (an explicit, audited command) or downgrade the mode"
    )
    mode_set.add_argument("mode", choices=[m.value for m in ExecutionMode])
    mode_set.add_argument("--reason", required=True, help="why (audited)")
    mode_set.set_defaults(handler=_mode_set)

    add_service_commands(commands)
    add_capital_commands(commands)
    add_research_commands(commands)
    return parser


def run(
    argv: Sequence[str] | None,
    *,
    env_file: Path | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Parse ``argv`` and run the command. Returns the exit status.

    ``env_file`` is the env file read when ``--env-file`` is not given.
    """
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    try:
        env = load_environment(env_file=args.env_file if args.env_file is not None else env_file)
    except EnvVarError as exc:
        print(f"obliskiq: {exc}", file=stderr)
        return EXIT_INVALID
    try:
        config_dir: Path = (
            args.config_dir if args.config_dir is not None else (env.resolved_config_dir())
        )
        loaded = load_all(config_dir.expanduser(), env)
    except (ConfigError, EnvVarError) as exc:
        print(f"obliskiq: {exc}", file=stderr)
        if handler is _kill_on:
            # The kill switch must work when everything else fails: the file flag alone.
            try:
                return _kill_on_flags(env, None, args.reason, stdout)
            except EnvVarError as missing:
                print(f"obliskiq: {missing}", file=stderr)
        return EXIT_INVALID
    try:
        return handler(Loaded(loaded.config, loaded.profile, loaded.env, args), stdout)
    except (EnvVarError, StoreError, sqlite3.Error) as exc:
        print(f"obliskiq: {exc}", file=stderr)
        return EXIT_INVALID


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``obliskiq`` script."""
    return run(argv, env_file=default_env_file(), stdout=sys.stdout, stderr=sys.stderr)
