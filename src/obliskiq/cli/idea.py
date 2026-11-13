"""``obliskiq idea add|list|show|withdraw``: the owner's ideas for the thesis book (MASTER_PLAN
§5.5).

``idea add`` hands the engine one idea: the instrument (a universe symbol, or ``--candidate`` with
a stock candidate's key and its ``--isin``), what is expected, a target, an invalidation and a
horizon, with a conviction (normal, high or max). An idea that lacks one of them is refused with
the reason and nothing is stored. A stored idea is a candidate: the service's next cycle judges it
at the gate, trades it inside the book's rails and grades it. ``idea list`` shows the open ideas
(``--all``: every one), ``idea show`` one idea with its log and its grade, ``idea withdraw`` takes
back an idea that has not filled. None opens a brokerage session, so all are safe beside a running
service.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, TextIO

from ..decision.thesis_intake import ThesisIntake
from ..domain.thesis import (
    OPEN_STATES,
    Conviction,
    Thesis,
    ThesisDraft,
    ThesisSource,
    View,
    parse_ref,
)
from ..engines.thesis.gate import describe
from ..errors import ObliskError
from ..store import open_store
from ..store import theses as thesis_repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1
OWNER_ACTOR = "owner (command line)"


class _Refused(ObliskError):
    """What the owner typed cannot be read."""


def _price(text: str | None, name: str) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text.replace("_", ""))
    except InvalidOperation:
        raise _Refused(f"--{name} {text!r} is not a price, such as 123.45") from None


def _day(text: str | None, name: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _Refused(f"--{name} {text!r} is not a date, such as 2027-06-30") from None


def _run(loaded: Loaded, work: Callable[[StoreWorker], Awaitable[int]], out: TextIO) -> int:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()

    async def go() -> int:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            return await work(worker)
        finally:
            await worker.aclose()

    try:
        return asyncio.run(go())
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID


def _line(thesis: Thesis) -> str:
    size = "" if thesis.size_base is None else f", size {thesis.size_base:.2f}"
    return (
        f"{thesis.ref} {thesis.instrument.symbol} {thesis.state.value} ({thesis.source.value}, "
        f"{thesis.conviction.value}, {thesis.view.value}): target {thesis.target}, invalidation "
        f"{thesis.invalidation}, horizon {thesis.horizon.isoformat()}, enter by "
        f"{thesis.enter_by.isoformat()}{size}"
    )


def _add(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args

    async def work(worker: StoreWorker) -> int:
        draft = ThesisDraft(
            source=ThesisSource.OWNER,
            conviction=Conviction(args.conviction),
            view=View.BEARISH if args.bearish else View.BULLISH,
            symbol=args.symbol,
            candidate=args.candidate,
            isin=args.isin,
            expectation=args.expect,
            target=_price(args.target, "target"),
            invalidation=_price(args.invalidation, "invalidation"),
            horizon=_day(args.horizon, "horizon"),
            entry_max=_price(args.entry_max, "entry-max"),
            enter_by=_day(args.enter_by, "enter-by"),
        )
        intake = ThesisIntake(loaded.config.settings, worker=worker, now=utc_now)
        stored = await intake.submit(draft, actor=OWNER_ACTOR)
        if isinstance(stored, list):
            print(f"refused, nothing was stored: {describe(stored)}", file=out)
            return EXIT_INVALID
        print(f"stored: {_line(stored)}", file=out)
        print("the service judges it at its next cycle (obliskiq idea show to follow it)", file=out)
        return EXIT_OK

    try:
        return _run(loaded, work, out)
    except ValueError as exc:  # a field the draft's own types refuse (an ISIN's check digit)
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID


def _list(loaded: Loaded, out: TextIO) -> int:
    async def work(worker: StoreWorker) -> int:
        states = None if loaded.args.all else OPEN_STATES
        found = await worker.run(thesis_repo.theses, as_of=utc_now().date(), states=states)
        if not found:
            print(
                "no idea" if loaded.args.all else "no open idea: the book holds nothing", file=out
            )
        for thesis in found:
            print(_line(thesis), file=out)
        return EXIT_OK

    return _run(loaded, work, out)


def _thesis_id(loaded: Loaded) -> int:
    thesis_id = parse_ref(loaded.args.thesis)
    if thesis_id is None:
        raise _Refused(f"{loaded.args.thesis!r} names no idea: write T3 or 3")
    return thesis_id


def _show(loaded: Loaded, out: TextIO) -> int:
    async def work(worker: StoreWorker) -> int:
        thesis_id = _thesis_id(loaded)
        thesis = await worker.run(thesis_repo.get_thesis, thesis_id)
        if thesis is None:
            raise _Refused(f"no idea {loaded.args.thesis}")
        print(_line(thesis), file=out)
        instrument = thesis.instrument
        pinned = f"conid {instrument.conid}" if instrument.pinned else "not pinned yet"
        print(f"  instrument: {instrument.description} ({pinned})", file=out)
        print(f"  expects: {thesis.expectation}", file=out)
        if thesis.entry_max is not None:
            print(f"  entered only at or below {thesis.entry_max}", file=out)
        for entry in await worker.run(thesis_repo.log_entries, thesis_id):
            print(
                f"  {entry.at.isoformat()} {entry.kind.value} ({entry.actor}): {entry.detail}",
                file=out,
            )
        grades = await worker.run(thesis_repo.grades, as_of=utc_now().date())
        for grade in (g for g in grades if g.thesis_id == thesis_id):
            reached = "reached" if grade.reached else "did not reach"
            print(
                f"  grade: {reached} its target; payoff {grade.payoff:.2%} against "
                f"{grade.stated_payoff:.2%} stated; {grade.days_held} of {grade.horizon_days} "
                f"days; closed by {grade.exit_reason}",
                file=out,
            )
        return EXIT_OK

    return _run(loaded, work, out)


def _withdraw(loaded: Loaded, out: TextIO) -> int:
    async def work(worker: StoreWorker) -> int:
        intake = ThesisIntake(loaded.config.settings, worker=worker, now=utc_now)
        done = await intake.withdraw(_thesis_id(loaded), actor=OWNER_ACTOR)
        if isinstance(done, str):
            print(f"refused: {done}", file=out)
            return EXIT_INVALID
        print(f"withdrawn: {_line(done)}", file=out)
        return EXIT_OK

    return _run(loaded, work, out)


def add_idea_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    idea = commands.add_parser("idea", help="your ideas for the thesis book")
    actions = idea.add_subparsers(dest="action", required=True, metavar="ACTION")

    add = actions.add_parser("add", help="hand the engine an idea")
    add.add_argument("symbol", nargs="?", help="a symbol of config/universe.yaml")
    add.add_argument("--candidate", help="a stock candidate's key, for a share outside it")
    add.add_argument("--isin", help="that share's ISIN (the tax lots are kept by it)")
    add.add_argument("--expect", help="what you expect and why")
    add.add_argument("--target", help="the price the idea aims at")
    add.add_argument("--invalidation", help="the price that proves the idea wrong")
    add.add_argument("--horizon", help="the day the idea ends, YYYY-MM-DD")
    add.add_argument(
        "--conviction",
        choices=[level.value for level in Conviction],
        default=Conviction.NORMAL.value,
        help="how strongly you hold it: it multiplies the size inside the position cap",
    )
    add.add_argument(
        "--bearish",
        action="store_true",
        help="a bearish view, held as a defensive instrument (never a short)",
    )
    add.add_argument("--entry-max", help="enter only at or below this price")
    add.add_argument("--enter-by", help="the last day to enter, YYYY-MM-DD")
    add.set_defaults(handler=_add)

    listing = actions.add_parser("list", help="the open ideas (--all: every idea)")
    listing.add_argument("--all", action="store_true", help="ended ideas too")
    listing.set_defaults(handler=_list)

    show = actions.add_parser("show", help="one idea, its log and its grade")
    show.add_argument("thesis", help="the idea, such as T3")
    show.set_defaults(handler=_show)

    withdraw = actions.add_parser("withdraw", help="take back an idea that has not filled")
    withdraw.add_argument("thesis", help="the idea, such as T3")
    withdraw.set_defaults(handler=_withdraw)
