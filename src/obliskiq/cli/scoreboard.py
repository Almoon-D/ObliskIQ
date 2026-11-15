"""``obliskiq scoreboard`` and ``obliskiq promotion``: the forward records, the reports, the
month-end run, lifting a freeze, the promotion gate and its drills (MASTER_PLAN §5.19).

``scoreboard grade`` prints every layer's record; ``scoreboard report weekly|monthly`` writes the
report to the data directory, prints it and, with ``--send``, sends its summary; ``scoreboard
month`` runs a month's looks and allocation (the service runs it on the first digest of a month);
``scoreboard unfreeze`` is the owner lifting a gap breach's freeze after the investigation.
``promotion status`` prints the gate's verdict on the next level; ``promotion drill`` records an
operational drill the owner ran.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, TextIO

from ..calendars.service import Calendars
from ..domain.orders import ExecutionMode
from ..env import repository_root
from ..errors import ObliskError
from ..execution.instruments import UniverseDirectory, describe_pinned
from ..execution.modes import Modes, PromotionCheck
from ..jurisdiction import TAX_POLICIES
from ..orchestration.assemble import build_notifier
from ..orchestration.scoreboard_jobs import write_report
from ..reporting import scoreboard as report
from ..research.patterns import REGISTRY_PATH
from ..scoreboard.layers import layer_state, registry_layers
from ..scoreboard.service import Scoreboard, previous_month
from ..store import open_store
from ..store import scoreboard as repo
from ..store.scoreboard import DRILL_KINDS, Drill, LayerEvent
from ..store.worker import StoreWorker
from ..tax.book import TaxBook
from ..util.timeutil import utc_now

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1
OWNER_ACTOR = "owner"
#: Days a weekly and a monthly report look back.
WEEK_DAYS = 7


def _run[T](loaded: Loaded, work: Callable[[Scoreboard, Modes, StoreWorker], Awaitable[T]]) -> T:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()

    async def go() -> T:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            directory = UniverseDirectory.from_settings(settings)
            await describe_pinned(directory, worker)
            tax = TaxBook(
                settings,
                TAX_POLICIES.resolve(loaded.profile, settings),
                worker=worker,
                directory=directory,
            )
            board = Scoreboard(
                settings,
                worker,
                root=repository_root(),
                calendars=Calendars.from_settings(
                    settings, reference=utc_now().date(), data_dir=data_dir
                ),
                tax=tax,
                now=utc_now,
            )
            modes = Modes(worker, settings, profile=loaded.env.ibkr_profile, data_dir=data_dir)
            return await work(board, modes, worker)
        finally:
            await worker.aclose()

    return asyncio.run(go())


async def promotion_verdict(
    loaded: Loaded, worker: StoreWorker, current: ExecutionMode, target: ExecutionMode
) -> PromotionCheck:
    """The promotion gate's verdict on ``target``, for the owner's upgrade command; a gate that
    cannot be read is a verdict not passed."""
    settings = loaded.config.settings
    board = Scoreboard(
        settings,
        worker,
        root=repository_root(),
        calendars=Calendars.from_settings(
            settings, reference=utc_now().date(), data_dir=loaded.env.require_data_dir()
        ),
        tax=None,
        now=utc_now,
    )
    try:
        verdict = await board.verdict(current, target)
    except ObliskError as exc:
        return PromotionCheck(target, False, f"the gate could not be read: {exc}")
    if verdict is None:
        return PromotionCheck(target, False, f"{current.value} is the top of the ladder")
    return PromotionCheck(target, verdict.passed, verdict.summary())


def _grade(loaded: Loaded, out: TextIO) -> int:
    async def work(board: Scoreboard, _: Modes, __: StoreWorker) -> str:
        found = await board.grades(utc_now().date())
        if isinstance(found, str):
            return f"not graded: {found}"
        lines = [f"forward records on {found.as_of.isoformat()}"]
        if not found.records and found.panel is None:
            lines.append("  no layer has a call yet")
        for name, item in found.records.items():
            verdict = "pending" if item.decision is None else item.decision.outcome.value
            lines.append(
                f"  {name}: {item.graded} graded of {item.calls} calls, {item.cohorts} cohorts, "
                f"{item.effective:.1f} effective observations, {verdict}"
            )
        for name, state in found.calibration.items():
            lines.append(
                f"  {name}: {'calibrated' if state.calibrated else 'uncalibrated'} "
                f"(shrink {state.shrink:.2f}): {state.reason}"
            )
        return "\n".join(lines)

    print(_run(loaded, work), file=out)
    return EXIT_OK


def _report(loaded: Loaded, out: TextIO) -> int:
    kind: str = loaded.args.kind
    monthly = kind == "monthly"
    data_dir = loaded.env.require_data_dir()

    async def work(board: Scoreboard, modes: Modes, _: StoreWorker) -> tuple[str, str, str]:
        as_of = utc_now().date()
        since = (
            (as_of.replace(day=1) - timedelta(days=1)).replace(day=1)
            if monthly
            else as_of - timedelta(days=WEEK_DAYS)
        )
        inputs = await board.inputs(
            mode=await modes.current(), since=since, as_of=as_of, monthly=monthly
        )
        text = report.monthly(inputs) if monthly else report.weekly(inputs)
        subject, body = report.summary(inputs, kind=kind)
        return text, subject, body

    text, subject, body = _run(loaded, work)
    path = write_report(data_dir, kind, utc_now().date(), text)
    print(text, file=out)
    print(f"written to {path}", file=out)
    if loaded.args.send:
        notifier = build_notifier(loaded.config.settings, loaded.env)
        sent = asyncio.run(notifier.send(subject, body))
        print("summary sent" if sent else "the summary could not be sent", file=out)
    return EXIT_OK


def _month(loaded: Loaded, out: TextIO) -> int:
    month: str = loaded.args.month or previous_month(utc_now().date())

    async def work(board: Scoreboard, _: Modes, __: StoreWorker) -> str:
        changes, _alerts = await board.month_end(month)
        if isinstance(changes, str):
            return f"nothing run: {changes}"
        lines = [f"month end {month}"]
        lines += [f"  look {look.layer} {look.boundary}: {look.detail}" for look in changes.looks]
        lines += [f"  {event.layer} {event.event}: {event.detail}" for event in changes.events]
        lines += [
            f"  {share.layer} -> {share.share:.2%}: {share.reason}" for share in changes.shares
        ]
        if len(lines) == 1:
            lines.append("  nothing changed")
        return "\n".join(lines)

    print(_run(loaded, work), file=out)
    return EXIT_OK


def _unfreeze(loaded: Loaded, out: TextIO) -> int:
    layer: str = loaded.args.layer
    reason: str = loaded.args.reason
    root = repository_root()
    known = {
        entry.id: entry for entry in ([] if root is None else registry_layers(root / REGISTRY_PATH))
    }

    async def work(_: Scoreboard, __: Modes, worker: StoreWorker) -> str:
        entry = known.get(layer)
        if entry is None:
            return f"refused: {layer} is not a layer in shadow or active"
        state = layer_state(entry.status, await worker.run(repo.layer_events, layer))
        if not state.frozen:
            return f"refused: {layer} is not frozen"
        await worker.run(
            repo.record_layer_event,
            LayerEvent(utc_now(), layer, "unfreeze", OWNER_ACTOR, reason),
        )
        return f"{layer} unfrozen: its new entries resume"

    text = _run(loaded, work)
    print(text, file=out)
    return EXIT_INVALID if text.startswith("refused") else EXIT_OK


def _status(loaded: Loaded, out: TextIO) -> int:
    target = None if loaded.args.to is None else ExecutionMode(loaded.args.to)

    async def work(board: Scoreboard, modes: Modes, _: StoreWorker) -> list[str]:
        mode = await modes.current()
        verdict = await board.verdict(mode, target)
        if verdict is None:
            return [f"execution mode {mode.value}: the top of the ladder"]
        lines = [f"execution mode {mode.value}; {verdict.summary()}"]
        for criterion in verdict.criteria:
            mark = {True: "met", False: "NOT MET", None: "UNMEASURED"}[criterion.passed]
            lines.append(f"  [{mark}] {criterion.name}: {criterion.detail}")
        lines.append("an upgrade is still the owner's command: obliskiq mode set <level>")
        return lines

    print("\n".join(_run(loaded, work)), file=out)
    return EXIT_OK


def _drill(loaded: Loaded, out: TextIO) -> int:
    kind = loaded.args.kind
    passed: bool = loaded.args.result == "passed"
    detail: str = loaded.args.detail

    async def work(_: Scoreboard, modes: Modes, worker: StoreWorker) -> str:
        mode = await modes.current()
        await worker.run(
            repo.record_drill, Drill(utc_now(), kind, mode, passed, OWNER_ACTOR, detail)
        )
        return f"{kind} drill recorded in {mode.value}: {'passed' if passed else 'failed'}"

    print(_run(loaded, work), file=out)
    return EXIT_OK


def add_scoreboard_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    scoreboard = commands.add_parser("scoreboard", help="forward records, reports, monitoring")
    actions = scoreboard.add_subparsers(dest="action", required=True, metavar="ACTION")
    actions.add_parser("grade", help="every layer's forward record").set_defaults(handler=_grade)
    reporting = actions.add_parser("report", help="write the weekly or monthly report")
    reporting.add_argument("kind", choices=["weekly", "monthly"])
    reporting.add_argument("--send", action="store_true", help="send its summary to the owner")
    reporting.set_defaults(handler=_report)
    month = actions.add_parser("month", help="run a month's looks and allocation")
    month.add_argument("--month", help="YYYY-MM (default: the month before this one)")
    month.set_defaults(handler=_month)
    unfreeze = actions.add_parser("unfreeze", help="lift a gap breach's freeze (owner)")
    unfreeze.add_argument("layer")
    unfreeze.add_argument("--reason", required=True, help="what the investigation found")
    unfreeze.set_defaults(handler=_unfreeze)

    promotion = commands.add_parser("promotion", help="the promotion ladder L0 to L3")
    steps = promotion.add_subparsers(dest="action", required=True, metavar="ACTION")
    status = steps.add_parser("status", help="the gate's verdict on the next level")
    status.add_argument("--to", choices=[m.value for m in ExecutionMode])
    status.set_defaults(handler=_status)
    drill = steps.add_parser("drill", help="record an operational drill the owner ran")
    drill.add_argument("kind", choices=list(DRILL_KINDS))
    drill.add_argument("result", choices=["passed", "failed"])
    drill.add_argument("--detail", required=True, help="what was done and seen")
    drill.set_defaults(handler=_drill)
