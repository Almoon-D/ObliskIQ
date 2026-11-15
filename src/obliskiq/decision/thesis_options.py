"""A thesis held as a long call or a long put (MASTER_PLAN §5.5, §5.6; DECISIONS D157, D167).

The thesis book's rebalancer hands this module the theses that may be, or are, held as an option.
It decides nothing the options sleeve does not: the contract is the sleeve's choice
(``engines/options_sleeve/thesis_link.py``), and this module adds what only the decision layer
reads, the account and the live quote.

* **Entry.** The thesis's levels are judged on the underlying's price as any thesis's are; the
  sleeve chooses the contract and its block; the option is described to the order path and
  judged by the mandate and the jurisdiction's policy with its underlying; a slot and the caps
  are asked for the premium; the premium at the live quote must still fit the pool and what the
  book may spend. The order is an entry intent like any other: the pre-trade gate asks for
  real-time quotes of the option and its underlying and never lets an option be sold beyond the
  long position. An option carries no stop: its loss is its premium.
* **The position.** When the block's fills are in and no order works, the sleeve's position is
  opened and its premium committed in the penalty ledger.
* **Exits**, in this order: the thesis's own (its invalidation, its target and its horizon, on
  the underlying's price; an invalidation books its loss at detection), the mandatory close on
  the session the sleeve's dates give, a confirmed zombie (reviewed once a session off the
  stored chain), and the scaled exit (``option_sleeve.scaled_exit``, once, in whole contracts).
* **The close.** The realised result goes to the penalty ledger (a loss costs its multiple, a
  gain is recorded and repays nothing), the position closes and the thesis is graded.
* **The twin** is the same thesis in its underlying at the book's base size, entered at the
  underlying's price when the option was chosen: a figure in the store, never an order. It
  leaves by the thesis's own exits alone, so it may outlive an option the sleeve closed.

Reads are made in :meth:`ThesisOptions.enter`, :meth:`held` and :meth:`twins`; every write is in
the methods the rebalancer calls when it applies a plan.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from ..calendars.service import Calendars
from ..capital.managed import CapitalReader
from ..config.schema import ObliskConfig
from ..domain.instrument import TradedInstrument
from ..domain.options import Conversion, OptionPosition, ThesisOption
from ..domain.orders import Fill, OrderSide
from ..domain.thesis import Expression, Thesis, ThesisGrade, ThesisState
from ..engines.options_sleeve.penalty import PenaltyLedger
from ..engines.options_sleeve.thesis_link import Chosen, ThesisLink
from ..engines.options_sleeve.zombie import OptionRisk, ZombieAssessment, ZombieTest
from ..engines.thesis.book import Slot, base_size
from ..engines.thesis.exits import (
    INVALIDATION,
    MANDATORY_CLOSE,
    OVERRIDE,
    SCALED_EXIT,
    TARGET,
    THESIS_EXITS,
    ZOMBIE,
    exit_due,
)
from ..engines.thesis.gate import Finding, GateFacts, Reason, ThesisGate, describe
from ..errors import CalendarError
from ..execution.instruments import UniverseDirectory, prohibited_reasons
from ..jurisdiction.eligibility import EligibilityPolicy
from ..store import options as options_repo
from ..store import theses as thesis_repo
from ..store import thesis_options as link_repo
from ..store.db import Store
from ..store.worker import StoreWorker
from .thesis_plan import (
    ACTOR,
    CLOSE,
    ENTER,
    EXIT,
    EXPIRE,
    HOLD,
    WAIT,
    ZERO,
    BookRead,
    ThesisDecision,
    ThesisPlan,
    TwinClose,
)


def average(fills: list[Fill]) -> tuple[Decimal, Decimal]:
    """The quantity of ``fills`` and their average price."""
    quantity = sum((fill.quantity for fill in fills), ZERO)
    return quantity, sum((fill.quantity * fill.price for fill in fills), ZERO) / quantity


def grade_option(
    thesis: Thesis, chosen: ThesisOption, fills: list[Fill], *, exit_reason: str, at: datetime
) -> ThesisGrade:
    """The grade of a thesis held as an option: the return on the premium paid, beside the one
    its target stated (the intrinsic value there over the premium)."""
    buys = [fill for fill in fills if fill.side is OrderSide.BUY]
    sells = [fill for fill in fills if fill.side is OrderSide.SELL]
    if not buys or not sells or thesis.mode is None or thesis.opened_at is None:
        raise ValueError(f"{thesis.ref} has no closed option position to grade")
    quantity, premium = average(buys)
    _, received = average(sells)
    target = thesis.target
    strike = Decimal(repr(chosen.strike))
    intrinsic = max(ZERO, target - strike if chosen.is_call else strike - target)
    return ThesisGrade(
        thesis_id=thesis.thesis_id,
        graded_at=at,
        mode=thesis.mode,
        source=thesis.source,
        conviction=thesis.conviction,
        held_as=Expression.OPTION,
        exit_reason=exit_reason,
        quantity=quantity,
        entry_price=premium,
        exit_price=received,
        reached=exit_reason == TARGET,
        payoff=received / premium - 1,
        stated_payoff=intrinsic / premium - 1,
        days_held=max(0, (at.date() - thesis.opened_at.date()).days),
        horizon_days=max(0, (thesis.horizon - thesis.opened_at.date()).days),
    )


class ThesisOptions:
    """Decides and records the options the thesis book's theses are held as."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        capital: CapitalReader,
        directory: UniverseDirectory,
        calendars: Calendars,
        worker: StoreWorker,
        eligibility: EligibilityPolicy,
        gate: ThesisGate,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._capital = capital
        self._directory = directory
        self._calendars = calendars
        self._worker = worker
        self._eligibility = eligibility
        self._gate = gate
        self._now = now
        self._link = ThesisLink(settings, calendars)
        self._scaled = settings.option_sleeve.scaled_exit
        self._qualifying = frozenset(settings.option_sleeve.qualifying_exit_reasons)

    @property
    def link(self) -> ThesisLink:
        return self._link

    async def describe(self) -> None:
        """Describe to the order path every option a thesis was approved as, so a restarted
        service can still value, exit and reconcile them."""
        for chosen in await self._worker.run(link_repo.every):
            self._directory.add(chosen.traded())

    # ── entry ──

    def _facts(
        self,
        thesis: Thesis,
        read: BookRead,
        slots: tuple[Slot, ...],
        reference: Decimal | None,
        rate: Decimal | None,
        prohibited: tuple[str, ...] = (),
    ) -> GateFacts:
        conid = thesis.instrument.conid
        taken = any(slot.conid == conid for slot in slots)
        return GateFacts(
            capital=read.capital,
            base_currency=self._settings.account.base_currency,
            prohibited=prohibited,
            ineligible=None,
            held_elsewhere=(
                f"another thesis already holds a slot on {thesis.instrument.symbol}"
                if taken
                else None
            ),
            reference=reference,
            rate=rate,
            daily_value=None,
            sessions=0,
            slots=slots,
            minimum=read.minimum,
        )

    def _contract(self, thesis: Thesis, chosen: Chosen, read: BookRead) -> ThesisOption | str:
        """The chosen contract as the store and the order path name it; why, when it cannot be
        ordered."""
        row = chosen.candidate.row
        underlying = chosen.candidate.underlying
        exchange = row.option_exchange or underlying.option_exchange
        if row.option_conid is None or exchange is None or row.multiplier is None:
            return "the chain names no contract id, exchange or multiplier for it"
        kind = "PUT" if thesis.falls else "CALL"
        label = f"{underlying.symbol} {row.expiry.isoformat()} {row.strike:g} {kind}"
        return ThesisOption(
            thesis_id=thesis.thesis_id,
            option_conid=row.option_conid,
            underlying_conid=row.underlying_conid,
            symbol=row.option_symbol or label,
            exchange=exchange,
            description=f"{label} on {underlying.description}",
            currency=chosen.candidate.currency,
            expiry=row.expiry,
            strike=row.strike,
            right=row.right,
            multiplier=row.multiplier,
            contracts=chosen.contracts,
            # Every gate was applied, the mandatory close among them, so the session is known.
            close_by=chosen.candidate.close_by or row.expiry,
            prob_target_at_entry=chosen.prob_target,
            underlying_entry=Decimal(repr(chosen.spot)),
            twin_size_base=base_size(read.capital, self._settings.thesis),
        )

    async def enter(
        self,
        thesis: Thesis,
        underlying: TradedInstrument,
        reference: Decimal | None,
        rate: Decimal | None,
        read: BookRead,
        slots: tuple[Slot, ...],
        spent: Decimal,
        plan: ThesisPlan,
    ) -> ThesisDecision | list[Finding]:
        """The entry of ``thesis`` as an option, or every reason it is not one now (a permanent
        reason: never)."""
        symbol = thesis.instrument.symbol
        facts = self._facts(
            thesis,
            read,
            slots,
            reference,
            rate,
            tuple(prohibited_reasons(underlying, None, self._settings)),
        )
        found = self._gate.market(thesis, facts)
        if any(finding.permanent for finding in found):
            return found
        spendable = max(ZERO, read.spendable - spent)
        chosen = await self._worker.run(
            self._link.choose,
            thesis,
            as_of=read.today,
            investable=read.view.investable,
            book_capital=read.capital,
            spendable=spendable,
        )
        if isinstance(chosen, list):
            return [*found, *chosen]
        contract = self._contract(thesis, chosen, read)
        if isinstance(contract, str):
            return [*found, Finding(Reason.OPTION_GATES, contract)]
        option = contract.traded()
        self._directory.add(option)
        barred = [
            Finding(Reason.PROHIBITED_PRODUCT, why, permanent=True)
            for why in prohibited_reasons(option, underlying, self._settings)
        ]
        verdict = self._eligibility.assess(option, underlying=underlying)
        if not verdict.eligible:
            barred.append(Finding(Reason.INELIGIBLE, verdict.reason, permanent=True))
        if barred:
            return barred
        found += self._gate.slot(thesis, chosen.premium_base, facts)
        if plan.entries_blocked is not None:
            found.append(Finding(Reason.ENTRIES_BLOCKED, plan.entries_blocked))
        found += self._venue(option, read.at)
        live = await self._capital.valuer.unit(option.conid, read.cash)
        if live.unit is None:
            found.append(Finding(Reason.NO_QUOTE, f"{contract.symbol} has no quote"))
        else:
            premium = live.unit * chosen.contracts
            ceiling = min(chosen.status.available, spendable)
            if premium > ceiling:
                found.append(
                    Finding(
                        Reason.OPTION_BUDGET,
                        f"at its live quote the block costs {premium:.2f}, above the "
                        f"{ceiling:.2f} the pool and the book's cash allow",
                    )
                )
        if found:
            return found
        return ThesisDecision(
            thesis,
            ENTER,
            f"the sleeve's gates passed on {symbol}: {chosen.summary()}",
            quantity=Decimal(chosen.contracts),
            size_base=chosen.premium_base,
            option=contract,
            chosen=chosen,
            underlying=reference,
        )

    def _venue(self, option: TradedInstrument, at: datetime) -> list[Finding]:
        try:
            venue = self._calendars.venue_for_ibkr(option.listing_exchange)
            status = self._calendars.tradability(venue, at, conid=option.conid)
        except CalendarError as exc:
            return [Finding(Reason.VENUE_NOT_TRADABLE, f"its venue cannot be judged: {exc}")]
        if status.ok:
            return []
        return [
            Finding(Reason.VENUE_NOT_TRADABLE, f"{venue} is not tradable now ({status.reason})")
        ]

    # ── a thesis approved as an option ──

    @staticmethod
    def _fills(
        thesis: Thesis, chosen: ThesisOption, read: BookRead
    ) -> tuple[Decimal, list[Fill], str, bool]:
        """The contracts held, the fills that made them, the last sale's reason and whether a
        part was already sold at the scaled exit."""
        since = thesis.approved_at
        mine = [
            entry
            for entry in read.fills
            if since is not None
            and entry.fill.conid == chosen.option_conid
            and entry.fill.executed_at >= since
        ]
        held = sum(
            (e.fill.quantity if e.fill.side is OrderSide.BUY else -e.fill.quantity for e in mine),
            ZERO,
        )
        sales = [entry.reason for entry in mine if entry.fill.side is OrderSide.SELL]
        return held, [e.fill for e in mine], sales[-1] if sales else OVERRIDE, SCALED_EXIT in sales

    async def held(
        self, thesis: Thesis, reference: Decimal | None, read: BookRead, plan: ThesisPlan
    ) -> ThesisDecision:
        """What the plan does with a thesis approved as an option: wait for its entry, hold it,
        leave by one of its exits, or close and grade it."""
        chosen = await self._worker.run(link_repo.get, thesis.thesis_id)
        assert chosen is not None  # an approval as an option records its contract with it
        self._directory.add(chosen.traded())
        held, fills, last_sale, scaled = self._fills(thesis, chosen, read)
        working = [intent for intent in read.busy if intent.conid == chosen.option_conid]
        bought = [fill for fill in fills if fill.side is OrderSide.BUY]
        position = (
            None
            if chosen.position_id is None
            else await self._worker.run(options_repo.get_position, chosen.position_id)
        )

        base = ThesisDecision(
            thesis,
            HOLD,
            "an order still works on it",
            held=held,
            fills=tuple(fills),
            opened=bool(bought) and not working,
            rate=await self._capital.valuer.rate(chosen.currency, read.cash),
            option=chosen,
            position=position,
            underlying=reference,
        )

        if not bought:
            if working:
                waits = (Finding(Reason.WORKING, "its entry order is working"),)
                return replace(base, action=WAIT, reason=describe(waits), findings=waits)
            if read.today > thesis.enter_by:
                return replace(base, action=EXPIRE, reason="its entry window ended without a fill")
            return await self._reenter(base, read, plan)
        if working:
            return base
        if held <= ZERO:
            return replace(base, action=CLOSE, reason=last_sale)
        return await self._manage(base, bought, scaled, read)

    async def _reenter(
        self, base: ThesisDecision, read: BookRead, plan: ThesisPlan
    ) -> ThesisDecision:
        """An approved option whose order ended without a fill: the same contract and block
        again, while the thesis's levels and the pool still allow it."""
        thesis, chosen = base.thesis, base.option
        assert chosen is not None  # a thesis approved as an option has its contract
        found = self._gate.market(thesis, self._facts(thesis, read, (), base.underlying, base.rate))
        permanent = [finding for finding in found if finding.permanent]
        if permanent:
            return replace(
                base, action=EXPIRE, reason=describe(permanent), findings=tuple(permanent)
            )
        if read.today >= chosen.close_by:
            return replace(
                base,
                action=EXPIRE,
                reason=f"its contract must be sold by {chosen.close_by.isoformat()}",
            )
        if plan.entries_blocked is not None:
            found.append(Finding(Reason.ENTRIES_BLOCKED, plan.entries_blocked))
        found += self._venue(chosen.traded(), read.at)
        status = await self._worker.run(
            self._link.status,
            as_of=read.today,
            investable=read.view.investable,
            book_capital=read.capital,
        )
        live = await self._capital.valuer.unit(chosen.option_conid, read.cash)
        if live.unit is None:
            found.append(Finding(Reason.NO_QUOTE, f"{chosen.symbol} has no quote"))
        elif live.unit * chosen.contracts > min(status.available, read.spendable):
            locked = f" (locked by {status.locked_by})" if status.locked_by else ""
            found.append(
                Finding(
                    Reason.OPTION_BUDGET,
                    f"the block costs {live.unit * chosen.contracts:.2f}, above what the pool "
                    f"and the book's cash allow{locked}",
                )
            )
        if found:
            return replace(base, action=WAIT, reason=describe(found), findings=tuple(found))
        return replace(
            base,
            action=ENTER,
            reason=f"its entry again: {chosen.contracts} x {chosen.symbol}",
            quantity=Decimal(chosen.contracts),
            size_base=thesis.size_base,
        )

    async def _manage(
        self, base: ThesisDecision, bought: list[Fill], scaled: bool, read: BookRead
    ) -> ThesisDecision:
        """A held option: the thesis's exits, the sleeve's, or hold."""
        thesis, chosen, held = base.thesis, base.option, base.held
        assert chosen is not None  # a thesis approved as an option has its contract
        live = await self._capital.valuer.unit(chosen.option_conid, read.cash)
        _, premium = average(bought)
        # What one contract cost, in the base currency at today's rate (``None``: no rate).
        unit_paid = (
            None if live.rate is None else premium * Decimal(repr(chosen.multiplier)) * live.rate
        )
        due = exit_due(thesis, base.underlying, read.today)
        if due is not None:
            loss = None
            if due == INVALIDATION and live.unit is not None and unit_paid is not None:
                loss = max(ZERO, (unit_paid - live.unit) * held)
            return replace(base, action=EXIT, reason=due, quantity=held, detected_loss=loss)
        if read.today >= chosen.close_by:
            return replace(base, action=EXIT, reason=MANDATORY_CLOSE, quantity=held)
        review: OptionRisk | None = None
        position = base.position
        if position is not None:
            reading = await self._worker.run(self._link.review, position, as_of=read.today)
            if isinstance(reading, OptionRisk):
                review = reading
                assessed = await self._worker.run(self._assess, position, reading, False)
                if assessed.fires:
                    return replace(base, action=EXIT, reason=ZOMBIE, quantity=held, review=review)
        if not scaled and live.unit is not None and unit_paid is not None:
            part = (held * self._scaled.fraction).to_integral_value(rounding=ROUND_FLOOR)
            if live.unit >= self._scaled.premium_multiple * unit_paid and part >= 1:
                return replace(base, action=EXIT, reason=SCALED_EXIT, quantity=part, review=review)
        return replace(
            base, reason=f"the underlying at {base.underlying} inside its levels", review=review
        )

    def _assess(
        self, store: Store, position: OptionPosition, risk: OptionRisk, record: bool
    ) -> ZombieAssessment:
        return ZombieTest(store, self._settings, now=self._now).review(
            position, risk, record=record
        )

    # ── the twins ──

    async def twins(self, read: BookRead, plan: ThesisPlan) -> list[TwinClose]:
        """The open twins that reached one of their thesis's exits on the underlying's price."""
        closing: list[TwinClose] = []
        for chosen in await self._worker.run(link_repo.open_twins):
            thesis = await self._worker.run(thesis_repo.get_thesis, chosen.thesis_id)
            if thesis is None or thesis.mode is not plan.mode:
                continue
            reference = await self._reference(chosen.underlying_conid, read)
            due = exit_due(thesis, reference, read.today)
            if due is not None and reference is not None:
                closing.append(TwinClose(chosen.thesis_id, reference, due))
        return closing

    async def _reference(self, conid: int, read: BookRead) -> Decimal | None:
        described = self._directory.describe(conid)
        value = await self._capital.valuer.unit(conid, read.cash)
        if described is None or value.unit is None or value.rate is None:
            return None
        return value.unit / (described.multiplier * value.rate)

    # ── applying a plan ──

    async def approve(self, decision: ThesisDecision, plan: ThesisPlan) -> Thesis:
        """Approve a candidate as the option the sleeve chose, with its contract and its twin."""
        thesis, chosen, picked = decision.thesis, decision.option, decision.chosen
        assert chosen is not None  # an option's entry carries its contract
        assert picked is not None  # and what the sleeve read to choose it
        assert decision.size_base is not None  # and the premium its block was sized at
        size = decision.size_base
        at = self._now()

        def write(store: Store) -> Thesis:
            with store.transaction():
                approved = thesis_repo.approve(
                    store,
                    thesis.thesis_id,
                    mode=plan.mode,
                    size_base=size,
                    held_as=Expression.OPTION,
                    at=at,
                    actor=ACTOR,
                    detail=(
                        f"approved as a long option at {size:.2f} of premium: "
                        f"{picked.summary()}; {picked.friction.reason}; "
                        f"{picked.activation.reason()}"
                    ),
                )
                link_repo.record(store, chosen)
                return approved

        return await self._worker.run(write)

    async def apply(self, decision: ThesisDecision) -> None:
        """What a held option's decision writes before any order: its position once the block's
        fills are in, the session's zombie review, and a loss booked at its detection."""
        chosen = decision.option
        if chosen is None or not decision.opened:
            return
        buys = [fill for fill in decision.fills if fill.side is OrderSide.BUY]
        rate = decision.rate
        today = self._now().date()

        def write(store: Store) -> None:
            with store.transaction():
                position = decision.position
                if position is None:
                    if rate is None:
                        return
                    position = self._open(store, decision.thesis, chosen, buys, rate, today)
                if position.position_id is None or not position.is_open:
                    return
                ledger = PenaltyLedger(store, self._settings, now=self._now)
                if decision.review is not None:
                    self._assess(store, position, decision.review, True)
                booked = ledger.booked_loss_penalty(position.position_id)
                if decision.detected_loss is not None and booked <= ZERO:
                    ledger.book_loss_detected(
                        position_id=position.position_id,
                        expected_loss=decision.detected_loss,
                        on=today,
                        note="the thesis's invalidation was reached; the penalty is booked at "
                        "detection",
                    )

        await self._worker.run(write)

    def _open(
        self,
        store: Store,
        thesis: Thesis,
        chosen: ThesisOption,
        buys: list[Fill],
        rate: Decimal,
        today: date,
    ) -> OptionPosition:
        """The sleeve's position of a filled block, its premium committed in the ledger."""
        contracts, premium = average(buys)
        local = premium * Decimal(repr(chosen.multiplier)) * contracts
        base = self._settings.account.base_currency
        opened_on = max(fill.executed_at for fill in buys).date()
        position = options_repo.open_position(
            store,
            OptionPosition(
                position_id=None,
                underlying_conid=chosen.underlying_conid,
                option_conid=chosen.option_conid,
                expiry=chosen.expiry,
                strike=chosen.strike,
                right=chosen.right,
                contracts=int(contracts),
                multiplier=chosen.multiplier,
                premium_per_unit=premium,
                currency=chosen.currency,
                premium_base=local * rate,
                fx_to_base=None if chosen.currency == base else rate,
                opened_on=opened_on,
                close_by=max(chosen.close_by, opened_on),
                target=float(thesis.target),
                prob_target_at_entry=chosen.prob_target_at_entry,
                catalyst_date=None,
                intent_id=None,
            ),
        )
        assert position.position_id is not None  # the store numbered it
        link_repo.set_position(store, thesis.thesis_id, position.position_id)
        PenaltyLedger(store, self._settings, now=self._now).book_premium_committed(
            position_id=position.position_id,
            premium=position.premium_base,
            on=today,
            conversion=(
                None
                if chosen.currency == base
                else Conversion(currency=chosen.currency, quote_amount=local, fx_to_base=rate)
            ),
        )
        return position

    async def close(self, thesis: Thesis, decision: ThesisDecision) -> None:
        """A thesis whose option is gone: the realised result in the penalty ledger, the
        position closed, the thesis closed and graded, and its twin closed with it when the
        exit was the thesis's own."""
        chosen = decision.option
        assert chosen is not None  # only an option's decision reaches here
        fills = list(decision.fills)
        at = self._now()
        today = at.date()
        rate = decision.rate
        graded = grade_option(thesis, chosen, fills, exit_reason=decision.reason, at=at)
        sold = sum(
            (f.quantity * f.price for f in fills if f.side is OrderSide.SELL), ZERO
        ) * Decimal(repr(chosen.multiplier))

        def write(store: Store) -> None:
            with store.transaction():
                current = link_repo.get(store, thesis.thesis_id)
                position = (
                    None
                    if current is None or current.position_id is None
                    else options_repo.get_position(store, current.position_id)
                )
                if position is not None and position.position_id is not None and rate is not None:
                    ledger = PenaltyLedger(store, self._settings, now=self._now)
                    result = sold * rate - position.premium_base
                    booked = ledger.booked_loss_penalty(position.position_id)
                    if result < ZERO or booked > ZERO:
                        ledger.reconcile_loss_closed(
                            position_id=position.position_id,
                            actual_loss=max(ZERO, -result),
                            on=today,
                        )
                    if result >= ZERO:
                        ledger.record_option_gain(
                            position_id=position.position_id, realised_gain=result, on=today
                        )
                    options_repo.close_position(
                        store, position.position_id, closed_on=today, exit_reason=decision.reason
                    )
                thesis_repo.move(
                    store,
                    thesis.thesis_id,
                    ThesisState.CLOSED,
                    at=at,
                    actor=ACTOR,
                    detail=(
                        f"closed by {decision.reason}: the option returned {graded.payoff:.2%} "
                        f"on its premium against the {graded.stated_payoff:.2%} its target "
                        f"stated, {graded.days_held} of {graded.horizon_days} days"
                    ),
                    exit_reason=decision.reason,
                )
                thesis_repo.record_grade(store, graded)
                if (
                    current is not None
                    and current.twin_open
                    and decision.reason in THESIS_EXITS
                    and decision.underlying is not None
                ):
                    link_repo.close_twin(
                        store,
                        thesis.thesis_id,
                        price=decision.underlying,
                        reason=decision.reason,
                        at=at,
                    )

        await self._worker.run(write)

    async def close_twins(self, plan: ThesisPlan) -> None:
        at = self._now()

        def write(store: Store) -> None:
            for twin in plan.twins:
                current = link_repo.get(store, twin.thesis_id)
                if current is not None and current.twin_open:
                    link_repo.close_twin(
                        store, twin.thesis_id, price=twin.price, reason=twin.reason, at=at
                    )

        await self._worker.run(write)

    async def repay(self, decision: ThesisDecision, gain: Decimal) -> None:
        """A thesis held as its instrument closed with a realised ``gain`` (base currency) on a
        qualifying exit: it repays the penalty balance one for one while there is one."""
        conid = decision.thesis.instrument.conid
        if conid is None or gain <= ZERO or decision.reason not in self._qualifying:
            return
        today = self._now().date()

        def write(store: Store) -> None:
            ledger = PenaltyLedger(store, self._settings, now=self._now)
            if ledger.balance(as_of=today) > ZERO:
                ledger.book_qualifying_gain(
                    conid=conid, realised_gain=gain, exit_reason=decision.reason, on=today
                )

        await self._worker.run(write)
