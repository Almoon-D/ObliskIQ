"""``obliskiq cash``, ``questions``, ``holdings external`` and ``tax report``.

The owner's cash flows (MASTER_PLAN §5.15): ``cash deposit`` records money added, ``cash reserve``
a dated withdrawal (investable capital drops at once and the reservation planner raises the
cash), ``cash withdrawn`` the money taken, ``cash cancel`` a reservation or a flow the broker has
not shown, ``cash classify`` a broker cash change nothing explains, ``cash list`` all of them.
Every command that records something first echoes what it would do and records nothing without
``--confirm``. ``questions list|answer`` are the owner questions the planner asks; ``holdings
external add|list|remove`` the holdings kept at brokers with no readable API (lite's
external_positions); ``tax report`` the year's realised results and deferred losses as CSV. None
opens a brokerage session, so all are safe beside a running service.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from ..capital.external import external_book
from ..capital.flows import CashLedger, LedgerRefusal, parse_record_id
from ..capital.questions import OwnerQuestions, QuestionRefusal
from ..domain.capital import (
    Adjustment,
    AdjustmentKind,
    CashEventState,
    CashFlow,
    ExternalHolding,
    QuestionState,
    ReservationState,
)
from ..errors import ObliskError
from ..execution.instruments import ConfiguredDirectory
from ..jurisdiction import TAX_POLICIES
from ..store import capital as capital_repo
from ..store import lots as lots_repo
from ..store import open_store
from ..store.worker import StoreWorker
from ..tax.book import TaxBook
from ..tax.report import annual_csv, year_bounds

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1
OWNER_ACTOR = "owner (command line)"
#: What the owner may classify a cash event as.
CLASSIFICATIONS = (*(kind.value for kind in AdjustmentKind), "deposit", "withdrawal")


def _amount(text: str) -> Decimal:
    try:
        value = Decimal(text.replace("_", ""))
    except InvalidOperation:
        raise LedgerRefusal(
            f"{text!r} is not an amount: write digits with a point for decimals, such as 1237.57"
        ) from None
    return value


def _run[T](loaded: Loaded, work: Callable[[StoreWorker], Awaitable[T]]) -> T:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()

    async def go() -> T:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            return await work(worker)
        finally:
            await worker.aclose()

    return asyncio.run(go())


def _ledger(loaded: Loaded, worker: StoreWorker) -> CashLedger:
    return CashLedger(worker, base_currency=loaded.config.settings.account.base_currency)


def _unconfirmed(out: TextIO) -> int:
    print("nothing was recorded: run the same command with --confirm to record it", file=out)
    return EXIT_INVALID


def _describe_flow(flow: CashFlow) -> str:
    label = f"F{flow.flow_id}" if flow.flow_id is not None else "a new flow"
    state = (
        "cancelled"
        if not flow.live
        else "shown by the broker's cash"
        if flow.matched_at is not None
        else "not shown by the broker's cash"
    )
    kind = flow.category.value if isinstance(flow, Adjustment) else flow.kind.value
    book = "" if flow.in_book else ", outside the book"
    return f"{label} {kind} {flow.amount} {flow.currency} on {flow.on.isoformat()} ({state}{book})"


# ── cash ──


def _cash_deposit(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args
    capital = loaded.config.settings.capital

    async def work(worker: StoreWorker) -> int:
        ledger = _ledger(loaded, worker)
        deposit = ledger.deposit(
            _amount(args.amount),
            currency=args.currency,
            on=args.date,
            actor=OWNER_ACTOR,
            note=args.note,
        )
        days = capital.deployment.tranche_days
        when = (
            "at once, in the managed book"
            if capital.mode == "cap"
            else "once the broker's cash shows it"
        )
        spread = "at once" if days == 1 else f"in equal daily tranches over {days} days"
        print(
            f"deposit of {deposit.amount} {deposit.currency} on {deposit.on.isoformat()}: managed "
            f"capital rises by it {when}, and it is deployed toward the targets {spread}",
            file=out,
        )
        if not args.confirm:
            return _unconfirmed(out)
        made = await ledger.record_deposit(deposit)
        print(f"recorded F{made.flow_id}", file=out)
        return EXIT_OK

    return _guarded(loaded, work, out)


def _cash_reserve(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args
    buffer = loaded.config.settings.capital.reservations.buffer_days

    async def work(worker: StoreWorker) -> int:
        ledger = _ledger(loaded, worker)
        reservation = ledger.reservation(
            _amount(args.amount),
            currency=args.currency,
            by=args.by,
            reason=args.reason,
            actor=OWNER_ACTOR,
        )
        print(
            f"withdrawal of {reservation.amount} {reservation.currency} by "
            f"{reservation.due_date.isoformat()} ({reservation.reason}): investable capital drops "
            "by it at once and the targets are recomputed; the planner raises the cash so it "
            f"settles at least {buffer} day(s) before, cheapest sources first, and asks before "
            "cutting a protected position",
            file=out,
        )
        if not args.confirm:
            return _unconfirmed(out)
        made = await ledger.record_reservation(reservation)
        print(f"recorded R{made.reservation_id}", file=out)
        return EXIT_OK

    return _guarded(loaded, work, out)


def _cash_withdrawn(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args

    async def work(worker: StoreWorker) -> int:
        kind, number = parse_record_id(args.id)
        if kind != "reservation":
            raise LedgerRefusal("`cash withdrawn` ends a reservation: give its R<number>")
        ledger = _ledger(loaded, worker)
        reservation = await ledger.open_reservation(number)
        withdrawal = ledger.withdrawal_for(reservation, actor=OWNER_ACTOR)
        print(
            f"R{number}: {withdrawal.amount} {withdrawal.currency} taken out on "
            f"{withdrawal.on.isoformat()}; the reservation closes and managed capital falls by it",
            file=out,
        )
        if not args.confirm:
            return _unconfirmed(out)
        made = await ledger.record_withdrawal(withdrawal)
        print(f"recorded F{made.flow_id}; R{number} is withdrawn", file=out)
        return EXIT_OK

    return _guarded(loaded, work, out)


def _cash_cancel(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args

    async def work(worker: StoreWorker) -> int:
        kind, number = parse_record_id(args.id)
        ledger = _ledger(loaded, worker)
        if kind == "reservation":
            reservation = await ledger.open_reservation(number)
            print(
                f"cancel R{number}: {reservation.amount} {reservation.currency} by "
                f"{reservation.due_date.isoformat()} is no longer needed",
                file=out,
            )
        elif kind == "flow":
            flow = await worker.run(capital_repo.get_flow, number)
            if flow is None:
                raise LedgerRefusal(f"there is no flow F{number}")
            print(f"cancel {_describe_flow(flow)}", file=out)
        else:
            raise LedgerRefusal("a cash event is classified, not cancelled (`cash classify`)")
        if not args.confirm:
            return _unconfirmed(out)
        print(await ledger.cancel(kind, number, actor=OWNER_ACTOR), file=out)
        return EXIT_OK

    return _guarded(loaded, work, out)


def _cash_classify(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args

    async def work(worker: StoreWorker) -> int:
        kind, number = parse_record_id(args.id)
        if kind != "event":
            raise LedgerRefusal("`cash classify` explains a cash event: give its E<number>")
        ledger = _ledger(loaded, worker)
        event = await ledger.pending_event(number)
        classification = (
            args.kind if args.kind in ("deposit", "withdrawal") else AdjustmentKind(args.kind)
        )
        flow = ledger.classification(
            event, classification, actor=OWNER_ACTOR, in_book=not args.outside_book, note=args.note
        )
        print(
            f"E{number}: {event.amount} {event.currency} is a {args.kind}; recorded as "
            f"{_describe_flow(flow)}, it explains the change and lifts the freeze on new buys at "
            "the next reconciliation",
            file=out,
        )
        if not args.confirm:
            return _unconfirmed(out)
        made = await ledger.classify(event, flow, actor=OWNER_ACTOR)
        print(f"recorded F{made.flow_id}; E{number} is classified", file=out)
        return EXIT_OK

    return _guarded(loaded, work, out)


def _cash_list(loaded: Loaded, out: TextIO) -> int:
    async def work(worker: StoreWorker) -> int:
        snapshot = await _ledger(loaded, worker).snapshot()
        open_ones = [r for r in snapshot.reservations if r.state is ReservationState.OPEN]
        print(f"reservations ({len(open_ones)} open):", file=out)
        for r in snapshot.reservations:
            print(
                f"  R{r.reservation_id} {r.state.value:<9} {r.amount} {r.currency} by "
                f"{r.due_date.isoformat()} ({r.reason})",
                file=out,
            )
        print(f"flows ({len(snapshot.flows)}):", file=out)
        for flow in snapshot.flows:
            print(f"  {_describe_flow(flow)}", file=out)
        pending = [e for e in snapshot.events if e.state is CashEventState.PENDING]
        print(f"unexplained cash ({len(pending)} pending):", file=out)
        for event in snapshot.events:
            print(
                f"  E{event.event_id} {event.state.value:<10} {event.amount} {event.currency} "
                f"since {event.detected_at.isoformat()}",
                file=out,
            )
        return EXIT_OK

    return _guarded(loaded, work, out)


def _guarded(loaded: Loaded, work: Callable[[StoreWorker], Awaitable[int]], out: TextIO) -> int:
    try:
        return _run(loaded, work)
    except (LedgerRefusal, QuestionRefusal) as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID


# ── questions ──


def _questions_list(loaded: Loaded, out: TextIO) -> int:
    async def work(worker: StoreWorker) -> int:
        questions = await OwnerQuestions(worker).listed(open_only=not loaded.args.all)
        print(f"{len(questions)} question(s){'' if loaded.args.all else ' open'}", file=out)
        for q in questions:
            state = (
                f"open until {q.deadline.isoformat()}"
                if q.state is QuestionState.OPEN
                else f"{q.state.value}: {q.answer}"
            )
            print(f"  Q{q.question_id} ({q.kind.value}, {state}): {q.context}", file=out)
            for option in q.options:
                tax = "unknown" if option.tax_cost is None else f"{option.tax_cost:.2f}"
                strategy = (
                    "unknown" if option.strategy_cost is None else f"{option.strategy_cost:.2f}"
                )
                default = " (the default)" if option.key == q.default else ""
                print(
                    f"    {option.key}{default}: {option.label}; tax {tax}, strategy {strategy}",
                    file=out,
                )
        return EXIT_OK

    return _guarded(loaded, work, out)


def _questions_answer(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args
    text = str(args.id).strip().upper().removeprefix("Q")
    if not text.isdigit():
        print(f"refused: {args.id!r} is not a question id (Q<number>)", file=out)
        return EXIT_INVALID

    async def work(worker: StoreWorker) -> int:
        questions = OwnerQuestions(worker)
        question = await worker.run(capital_repo.get_question, int(text))
        if question is None:
            raise QuestionRefusal(f"there is no question Q{text}")
        option = question.option(args.option)
        if option is None:
            keys = ", ".join(o.key for o in question.options)
            raise QuestionRefusal(f"Q{text} has no option {args.option!r} (options: {keys})")
        print(f"Q{text}: {option.key}: {option.label}", file=out)
        if not args.confirm:
            print("nothing was answered: run the same command with --confirm", file=out)
            return EXIT_INVALID
        answered = await questions.answer(int(text), args.option, actor=OWNER_ACTOR)
        print(f"answered: {answered.effect}", file=out)
        return EXIT_OK

    return _guarded(loaded, work, out)


# ── external holdings ──


def _external_add(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args
    settings = loaded.config.settings
    today = datetime.now(UTC).date()
    try:
        quantity = _amount(args.quantity)
        value = _amount(args.value)
        if not quantity.is_finite() or not value.is_finite():
            raise LedgerRefusal("the quantity and the value are finite amounts")
        holding = ExternalHolding(
            broker=args.broker.strip(),
            symbol=args.symbol.strip(),
            isin=args.isin,
            quantity=quantity,
            currency=args.currency,
            market_value_base=value,
            last_updated=args.as_of or today,
            notes=args.notes,
            sector=args.sector,
            country=args.country,
        )
    except (LedgerRefusal, ValueError) as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    with open_store(settings, loaded.env.require_data_dir()) as store:
        saved = capital_repo.upsert_external(store, holding, at=datetime.now(UTC))
    print(
        f"external holding {saved.holding_id}: {saved.broker} {saved.symbol} {saved.quantity} "
        f"worth {saved.market_value_base} {settings.account.base_currency}, confirmed "
        f"{saved.last_updated.isoformat()}",
        file=out,
    )
    return EXIT_OK


def _external_list(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    with open_store(settings, loaded.env.require_data_dir()) as store:
        holdings = capital_repo.external_holdings(store)
    book = external_book(holdings, settings.positions, as_of=datetime.now(UTC).date())
    print(
        f"{len(book.holdings)} external holding(s), {book.total_base:.2f} "
        f"{settings.account.base_currency} in all",
        file=out,
    )
    stale = set(book.stale)
    for h in book.holdings:
        mark = " STALE" if book.label(h) in stale else ""
        print(
            f"  {h.holding_id} {h.broker} {h.symbol} {h.isin or ''} {h.quantity} {h.currency} "
            f"worth {h.market_value_base}, confirmed {h.last_updated.isoformat()}{mark}",
            file=out,
        )
    reason = book.reason()
    if reason is not None:
        print(f"UNRELIABLE: {reason}", file=out)
    return EXIT_OK


def _external_remove(loaded: Loaded, out: TextIO) -> int:
    with open_store(loaded.config.settings, loaded.env.require_data_dir()) as store:
        removed = capital_repo.remove_external(store, loaded.args.holding_id)
    if not removed:
        print(f"refused: there is no external holding {loaded.args.holding_id}", file=out)
        return EXIT_INVALID
    print(f"external holding {loaded.args.holding_id} removed", file=out)
    return EXIT_OK


# ── tax ──


def _tax_report(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    year: int = loaded.args.year
    first, last = year_bounds(year)

    async def work(worker: StoreWorker) -> str:
        policy = TAX_POLICIES.resolve(loaded.profile, settings)
        book = TaxBook(
            settings, policy, worker=worker, directory=ConfiguredDirectory.from_settings(settings)
        )
        engine = await book.engine()
        realisations = await worker.run(lots_repo.realisations_between, since=first, until=last)
        settled = {s.tax_year: s for s in engine.ledger.settled()}
        summary = settled.get(year) or engine.ledger.provisional(year)
        identities = {str(f.conid): f.isin for f in settings.eligibility.skeleton_tax_facts}
        return annual_csv(
            year,
            realisations=realisations,
            lots=engine.lots(),
            windows=engine.loss_windows(),
            summary=summary,
            settled=year in settled,
            identities=identities,
        )

    try:
        text = _run(loaded, work)
    except ObliskError as exc:
        print(f"the report cannot be made: {exc}", file=out)
        return EXIT_INVALID
    target: Path | None = loaded.args.out
    if target is None:
        print(text, end="", file=out)
    else:
        target.write_text(text, encoding="utf-8")
        print(f"the {year} report is in {target} (not tax advice)", file=out)
    return EXIT_OK


# ── the parser ──


def add_capital_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cash = commands.add_parser("cash", help="deposits, withdrawals and unexplained cash")
    actions = cash.add_subparsers(dest="action", required=True, metavar="ACTION")

    def confirm(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--confirm", action="store_true", help="record it: without it nothing is recorded"
        )

    deposit = actions.add_parser("deposit", help="money you added to the account")
    deposit.add_argument("amount", help="the amount, such as 1237.57")
    deposit.add_argument("--currency", help="its currency (default: the base currency)")
    deposit.add_argument("--date", type=date.fromisoformat, help="the day it was made (YYYY-MM-DD)")
    deposit.add_argument("--note", help="anything worth recording")
    confirm(deposit)
    deposit.set_defaults(handler=_cash_deposit)

    reserve = actions.add_parser("reserve", help="money you will take out by a date")
    reserve.add_argument("amount", help="the amount, such as 1000")
    reserve.add_argument(
        "--by", type=date.fromisoformat, required=True, help="the day you need it (YYYY-MM-DD)"
    )
    reserve.add_argument("--currency", help="its currency (default: the base currency)")
    reserve.add_argument("--reason", help="what it is for")
    confirm(reserve)
    reserve.set_defaults(handler=_cash_reserve)

    withdrawn = actions.add_parser("withdrawn", help="you took a reserved amount out")
    withdrawn.add_argument("id", help="the reservation, R<number> (`cash list` shows it)")
    confirm(withdrawn)
    withdrawn.set_defaults(handler=_cash_withdrawn)

    cancel = actions.add_parser("cancel", help="cancel a reservation or an unshown flow")
    cancel.add_argument("id", help="R<number> or F<number> (`cash list` shows them)")
    confirm(cancel)
    cancel.set_defaults(handler=_cash_cancel)

    classify = actions.add_parser("classify", help="explain an unexplained cash change")
    classify.add_argument("id", help="the cash event, E<number>")
    classify.add_argument("kind", choices=CLASSIFICATIONS, help="what it is")
    classify.add_argument(
        "--outside-book",
        action="store_true",
        help="it does not belong to the managed book (an unmanaged holding's dividend, say)",
    )
    classify.add_argument("--note", help="anything worth recording")
    confirm(classify)
    classify.set_defaults(handler=_cash_classify)

    actions.add_parser("list", help="reservations, flows and cash events").set_defaults(
        handler=_cash_list
    )

    questions = commands.add_parser("questions", help="the questions the bot needs you to answer")
    question_actions = questions.add_subparsers(dest="action", required=True, metavar="ACTION")
    listing = question_actions.add_parser("list", help="open questions (--all: every question)")
    listing.add_argument("--all", action="store_true", help="answered and expired ones too")
    listing.set_defaults(handler=_questions_list)
    answer = question_actions.add_parser("answer", help="answer a question")
    answer.add_argument("id", help="the question, Q<number> or its number")
    answer.add_argument("option", help="the option's key (`questions list` shows them)")
    answer.add_argument("--confirm", action="store_true", help="answer: without it nothing is")
    answer.set_defaults(handler=_questions_answer)

    tax = commands.add_parser("tax", help="the tax lots on the live path")
    tax_actions = tax.add_subparsers(dest="action", required=True, metavar="ACTION")
    report = tax_actions.add_parser(
        "report", help="a year's realised results and deferred losses as CSV (not tax advice)"
    )
    report.add_argument("--year", type=int, required=True, help="the tax year")
    report.add_argument("--out", type=Path, help="write the CSV here instead of printing it")
    report.set_defaults(handler=_tax_report)


def add_external_commands(
    holding_actions: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """``holdings external add|list|remove``."""
    external = holding_actions.add_parser(
        "external", help="holdings at brokers with no readable API, entered by hand"
    )
    actions = external.add_subparsers(dest="external_action", required=True, metavar="ACTION")
    add = actions.add_parser(
        "add", help="record or update a holding (keyed on broker, symbol, ISIN)"
    )
    add.add_argument("--broker", required=True, help="where it is held, such as DEGIRO")
    add.add_argument("--symbol", required=True, help="its ticker as the broker shows it")
    add.add_argument("--isin", help="its ISIN, part of the key")
    add.add_argument("--quantity", required=True, help="units held")
    add.add_argument("--currency", required=True, help="the listing's currency")
    add.add_argument("--value", required=True, help="its value now in the base currency")
    add.add_argument(
        "--as-of",
        type=date.fromisoformat,
        help="when you last confirmed the figure (default today); staleness is measured from it",
    )
    add.add_argument("--sector", help="its sector, for the sector caps")
    add.add_argument("--country", help="its country (two letters), for the country caps")
    add.add_argument("--notes", help="anything worth recording")
    add.set_defaults(handler=_external_add)
    actions.add_parser("list", help="every external holding, the stale ones marked").set_defaults(
        handler=_external_list
    )
    remove = actions.add_parser("remove", help="you no longer hold it")
    remove.add_argument("holding_id", type=int, help="its id (`holdings external list`)")
    remove.set_defaults(handler=_external_remove)
