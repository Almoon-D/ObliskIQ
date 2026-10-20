"""Protective exits: stop-limits resting at the broker (MASTER_PLAN §5.12, DECISIONS D67).

A position whose latest filled entry carried a protective spec, or that has a working protective
stop, is protected. Its stops rest at the broker as sell stop-limits with the time in force of
``execution.protective.tif`` (GTC: they survive the session and a dead machine), so the broker
protects the position whether or not the bot is running.

* **The spec.** :func:`limit_offset` sets how far below its stop a limit sits: the larger of
  ``sigma_multiple`` times the instrument's daily volatility and its overnight-gap quantile, never
  below ``floor_fraction``, never beyond ``max_fraction`` (a limit that deep acts as a market
  order). A missing input carries no weight and the spec's basis says so.
* **Coverage.** :meth:`ProtectiveManager.protect` (after a fill) and
  :meth:`ProtectiveManager.verify` (every heartbeat) make the working stops cover the position less
  the exits working beside it: a stop is placed, resized up or down, or cancelled. Each placement
  and resize is cleared by the pre-trade gate as an order that reduces risk (I2, I3), so it goes
  through with the kill switch on; tradability is not waived. A stop the heartbeat finds missing or
  short is re-placed and the owner alerted; a stop larger than the position is cut, because a
  stop that triggers beyond the position would sell short. A stop placed for an intent the broker
  does not list yet counts as covering (the gate counts it too, until the broker lists it or its
  intent ends), so a listing lag never doubles a stop.
* **Roll-ups.** :meth:`ProtectiveManager.roll_up` raises every working stop of a position (never
  lowers one), with its limit at the spec's offset below the new stop.
* **Gaps.** A stop-limit becomes a limit when it triggers, and a gap through its limit leaves it
  unfilled. :meth:`ProtectiveManager.reprice_gaps` moves such a limit toward the bid by
  ``gap.step_fraction`` of the reference every ``gap.step_interval_minutes``, at most
  ``gap.max_steps`` times, each a modify the gate clears as reducing risk and holds to the collar.
  A limit beyond the collar is the owner's emergency limit, off by default; when it is off, a gap
  past the collar is alerted and the stop rests where the collar allows. Never a market order.
* **Partial exits** go through :class:`~obliskiq.execution.exits.ExitCoordinator`, which cuts the
  stop first and restores it if the exit ends unfilled (I5).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from ..config.schema import ObliskConfig
from ..domain.orders import (
    UNMANAGED,
    BrokerOrder,
    ClearanceAction,
    IntentRole,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderTerms,
    ProtectiveSpec,
    Sleeve,
    StopLimitPrice,
)
from ..errors import ObliskError
from ..ibkr.models import ContractRules
from ..ibkr.order_decode import LiveOrders
from ..ibkr.orders import SMART_ROUTER, PlacementStatus, is_live
from ..logging_setup import get_logger
from ..store import orders as repo
from ..store.orders import StoredIntent
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .account import AccountReader, PositionsRead
from .boundary import OrderAlert, OrderAlertHook, OrderAlertKind, WriteBoundary, raise_alert
from .intents import new_intent
from .modes import Modes
from .pretrade import PretradeGate, PretradeRefusal
from .pricing import tick_for, to_tick
from .state import BROKER_STATES, IntentBook

log = get_logger("execution.protective")

ZERO = Decimal(0)

#: The exit reason a protective stop gives (lite's ``exit_reasons_allowed``).
STOP_REASON = "stop"

#: The detail every gap reprice is recorded under, so the steps taken can be counted.
GAP_REPRICE = "gap reprice"


class ContractRulesReader(Protocol):
    """The contract's trading rules (its tick)."""

    async def rules(
        self, conid: int, *, exchange: str | None = None, is_buy: bool = True
    ) -> ContractRules: ...


def limit_offset(
    settings: ObliskConfig, *, daily_sigma: Decimal | None, gap_quantile: Decimal | None
) -> tuple[Decimal, str]:
    """How far below its stop a protective limit sits, and the basis of that figure.

    The larger of ``sigma_multiple`` times the daily volatility and the overnight-gap quantile,
    never below the floor nor beyond the maximum. A missing input carries no weight.
    """
    offsets = settings.execution.protective.limit_offset
    terms = [("floor", offsets.floor_fraction)]
    if daily_sigma is not None:
        terms.append(
            (f"{offsets.sigma_multiple} x daily sigma", offsets.sigma_multiple * daily_sigma)
        )
    if gap_quantile is not None:
        terms.append(("gap quantile", gap_quantile))
    name, offset = max(terms, key=lambda term: term[1])
    missing = [
        what
        for what, value in (("daily sigma", daily_sigma), ("gap quantile", gap_quantile))
        if value is None
    ]
    basis = f"{name} {offset:.4f}" + (f"; no {' or '.join(missing)}" if missing else "")
    if offset > offsets.max_fraction:
        return offsets.max_fraction, f"{basis}; capped at {offsets.max_fraction}"
    return offset, basis


def protective_spec(
    settings: ObliskConfig,
    stop: Decimal,
    *,
    daily_sigma: Decimal | None,
    gap_quantile: Decimal | None,
) -> ProtectiveSpec:
    """The spec for a stop at ``stop``, its offset from the instrument's volatility and gaps."""
    offset, basis = limit_offset(settings, daily_sigma=daily_sigma, gap_quantile=gap_quantile)
    return ProtectiveSpec(stop=stop, limit_offset_fraction=offset, basis=basis)


def stop_limit(stop: Decimal, spec: ProtectiveSpec, tick: Decimal) -> Decimal:
    """The limit of a stop at ``stop``: the spec's offset below it, rounded up to a whole tick,
    so it never sits further below the stop than the spec allows."""
    return min(stop, to_tick(spec.lowest_limit(stop), tick, up=True))


@dataclass(frozen=True, slots=True)
class ProtectOutcome:
    """What one coverage pass did for one position."""

    conid: int
    held: Decimal
    needed: Decimal
    covered: Decimal
    action: str
    detail: str


@dataclass(frozen=True, slots=True)
class _Stop:
    """A protective intent that may be at the broker, and what it covers."""

    stored: StoredIntent
    order: BrokerOrder | None
    covers: Decimal

    @property
    def intent(self) -> OrderIntent:
        return self.stored.intent


@dataclass(frozen=True, slots=True)
class _Survey:
    """One position as coverage needs it."""

    conid: int
    held: Decimal
    exits: Decimal
    spec: ProtectiveSpec | None
    template: OrderIntent | None
    stops: tuple[_Stop, ...]

    @property
    def needed(self) -> Decimal:
        return max(ZERO, self.held - self.exits)

    @property
    def covered(self) -> Decimal:
        return sum((stop.covers for stop in self.stops), ZERO)


class ProtectiveManager:
    """Keeps every protected position covered by stop-limits resting at the broker."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        gate: PretradeGate,
        boundary: WriteBoundary,
        book: IntentBook,
        worker: StoreWorker,
        reader: AccountReader,
        rules: ContractRulesReader,
        modes: Modes,
        alert: OrderAlertHook | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._gate = gate
        self._boundary = boundary
        self._book = book
        self._worker = worker
        self._reader = reader
        self._rules = rules
        self._modes = modes
        self._alert = alert
        self._now = now
        protective = settings.execution.protective
        self._tif = protective.tif
        self._ttl = timedelta(days=protective.intent_ttl_days)
        gap = protective.gap
        self._step_fraction = gap.step_fraction
        self._max_steps = gap.max_steps
        self._step_interval = timedelta(minutes=gap.step_interval_minutes)
        self._emergency_on = gap.emergency.enabled
        self._emergency_fraction = gap.emergency.fraction_below_bid
        self._grace = timedelta(seconds=settings.execution.reconcile.grace_seconds)

    # ── reading a position ──

    async def _survey(self, conid: int, live: LiveOrders, positions: PositionsRead) -> _Survey:
        intents = await self._worker.run(repo.intents_for_conid, conid)
        listed = {order.coid: order for order in live.orders if order.coid is not None}
        spec: ProtectiveSpec | None = None
        template: OrderIntent | None = None
        exits = ZERO
        stops: list[_Stop] = []
        for stored in intents:
            intent = stored.intent
            selling = intent.side is OrderSide.SELL
            if intent.role is IntentRole.ENTRY:
                if await self._worker.run(repo.fills_for_intent, intent.intent_id):
                    spec, template = intent.protective, None
            elif selling and intent.role is IntentRole.PROTECTIVE:
                spec = intent.protective or spec
                template = intent
                if intent.state in BROKER_STATES:
                    stops.append(await self._stop(stored, listed.get(stored.coid)))
            elif selling and intent.role is IntentRole.EXIT and intent.state in BROKER_STATES:
                exits += await self._open(stored, listed.get(stored.coid))
        return _Survey(
            conid=conid,
            held=positions.quantity(conid),
            exits=exits,
            spec=spec,
            template=template,
            stops=tuple(stops),
        )

    async def _open(self, stored: StoredIntent, order: BrokerOrder | None) -> Decimal:
        """What an intent's order may still sell: the broker's remaining size, else its terms
        less its recorded fills."""
        if order is not None and not is_live(order):
            return ZERO
        if order is not None and order.remaining_quantity is not None:
            return order.remaining_quantity
        fills = await self._worker.run(repo.fills_for_intent, stored.intent.intent_id)
        quantity = stored.intent.terms.quantity or ZERO
        return max(ZERO, quantity - sum((fill.quantity for fill in fills), ZERO))

    async def _stop(self, stored: StoredIntent, order: BrokerOrder | None) -> _Stop:
        """A working protective intent. Listed, it covers its remaining size. Unlisted, it covers
        what it may still sell while it is new (inside the grace period, or never acknowledged);
        past that its status is read, and a status that cannot be read covers nothing: the stop
        is then placed again, which the gate refuses while the old one may still be working, and
        the owner hears of it."""
        if order is None:
            placed = await self._worker.run(repo.broker_orders_for_intent, stored.intent.intent_id)
            fresh = [o for o in placed if self._now() - o.observed_at <= self._grace]
            if placed and not fresh:
                try:
                    order = await self._boundary.sync_order(placed[-1].order_id)
                except ObliskError as exc:
                    log.warning(
                        "a protective stop's status cannot be read",
                        extra={"intent": str(stored.intent.intent_id), "error": str(exc)},
                    )
                    return _Stop(stored=stored, order=None, covers=ZERO)
        return _Stop(stored=stored, order=order, covers=await self._open(stored, order))

    # ── coverage ──

    async def protect(self, conid: int) -> ProtectOutcome:
        """After a fill: make the position's stops cover it. Quiet: nothing here is an alert."""
        try:
            live = await self._reader.live_orders()
            positions = await self._reader.positions()
        except ObliskError as exc:
            return ProtectOutcome(conid, ZERO, ZERO, ZERO, "unread", f"cannot read: {exc}")
        if not live.complete:
            return ProtectOutcome(conid, ZERO, ZERO, ZERO, "unread", "live orders incomplete")
        await self._boundary.record_orders(list(live.orders))
        survey = await self._survey(conid, live, positions)
        return await self._cover(survey)

    async def verify(self) -> list[ProtectOutcome]:
        """The heartbeat: every protected position covered, a missing or short stop re-placed and
        alerted, a stop beyond its position cut."""
        try:
            live = await self._reader.live_orders()
            positions = await self._reader.positions()
        except ObliskError as exc:
            self._raise(None, f"the protective stops cannot be verified: {exc}")
            return []
        if not live.complete:
            self._raise(
                None, "the protective stops cannot be verified: the live orders are partial"
            )
            return []
        await self._boundary.record_orders(list(live.orders))
        conids = {p.conid for p in positions.positions if p.quantity > 0}
        working = await self._worker.run(repo.intents_in_states, BROKER_STATES)
        conids |= {s.intent.conid for s in working if s.intent.role is IntentRole.PROTECTIVE}
        outcomes = []
        for conid in sorted(conids):
            survey = await self._survey(conid, live, positions)
            if survey.spec is None and not survey.stops:
                continue  # not a protected position
            outcome = await self._cover(survey)
            outcomes.append(outcome)
            if outcome.action in ("placed", "resized up", "refused"):
                anchor = survey.stops[0].intent.intent_id if survey.stops else None
                self._raise(
                    anchor,
                    f"conid {conid}: {survey.needed} to protect, {survey.covered} covered; "
                    f"{outcome.action}: {outcome.detail}",
                )
        return outcomes

    async def _cover(self, survey: _Survey) -> ProtectOutcome:
        needed, covered = survey.needed, survey.covered

        def outcome(action: str, detail: str) -> ProtectOutcome:
            return ProtectOutcome(survey.conid, survey.held, needed, covered, action, detail)

        if covered == needed:
            return outcome("none", "covered")
        if covered > needed:
            return outcome("resized down", await self._cut(survey))
        if survey.spec is None:
            return outcome("refused", "no protective spec to place a stop by")
        missing = needed - covered
        listed = [s for s in survey.stops if s.order is not None and is_live(s.order)]
        if listed:
            stop = listed[0]
            failure = await self._resize(stop, stop.covers + missing)
            if failure is None:
                return outcome("resized up", f"stop {stop.intent.intent_id} up by {missing}")
            return outcome("refused", failure)
        failure = await self._place(survey, missing)
        if failure is None:
            return outcome("placed", f"a stop for {missing}")
        return outcome("refused", failure)

    async def _cut(self, survey: _Survey) -> str:
        """Bring the stops down to what the position needs: largest first, cancelled at zero."""
        excess = survey.covered - survey.needed
        done = []
        for stop in sorted(survey.stops, key=lambda s: s.covers, reverse=True):
            if excess <= 0:
                break
            if stop.order is None or not is_live(stop.order):
                continue
            target = stop.covers - min(excess, stop.covers)
            failure = await self._resize(stop, target)
            if failure is not None:
                self._raise(stop.intent.intent_id, f"a stop beyond its position: {failure}")
                return failure
            excess -= stop.covers - target
            done.append(f"{stop.order.order_id} to {target}")
        return "; ".join(done) or "nothing listed to cut"

    async def _resize(self, stop: _Stop, covers: Decimal) -> str | None:
        """Make ``stop`` cover ``covers`` (a cancel at zero); ``None`` when the broker took it."""
        order = stop.order
        if order is None:  # pragma: no cover - callers resize only listed stops
            return "the stop is not listed"
        if covers <= 0:
            answer = await self._boundary.cancel(order.order_id)
            return None if answer.error is None else f"the cancel failed: {answer.error}"
        filled = order.filled_quantity or ZERO
        terms = stop.intent.terms.model_copy(update={"quantity": filled + covers})
        return await self._modify(stop.intent, order.order_id, terms)

    async def _modify(
        self, intent: OrderIntent, order_id: str, terms: OrderTerms, *, detail: str | None = None
    ) -> str | None:
        clearance = await self._gate.clear(
            intent, action=ClearanceAction.MODIFY, order_id=order_id, terms=terms
        )
        if isinstance(clearance, PretradeRefusal):
            return clearance.reason
        try:
            result = await self._boundary.modify(
                intent.intent_id, order_id=order_id, terms=terms, clearance=clearance
            )
        except ObliskError as exc:
            return str(exc)
        if result.status is not PlacementStatus.PLACED:
            return f"{result.status.value}: {result.detail}"
        if detail is not None:
            await self._book.fact(intent.intent_id, detail)
        return None

    async def _tick(self, conid: int, price: Decimal) -> Decimal | None:
        try:
            rules = await self._rules.rules(conid, is_buy=False)
        except ObliskError:
            return None
        return tick_for(rules, price)

    async def _place(self, survey: _Survey, quantity: Decimal) -> str | None:
        """A new protective intent for ``quantity``, at the latest stop's terms or the spec."""
        spec = survey.spec
        if spec is None:  # pragma: no cover - the caller checks
            return "no spec"
        template = survey.template
        if template is not None and template.terms.price.kind == "stop_limit":
            price = template.terms.price
        else:
            tick = await self._tick(survey.conid, spec.stop)
            if tick is None:
                return "the contract's rules give no tick for the stop"
            price = StopLimitPrice(stop=spec.stop, limit=stop_limit(spec.stop, spec, tick))
        sleeve = await self._sleeve(survey.conid, template)
        if sleeve is None:
            return "the holding is assigned to no sleeve"
        try:
            mode = await self._modes.current()
        except Exception as exc:  # the mode unreadable: nothing is placed
            return f"the mode cannot be read ({type(exc).__name__})"
        now = self._now()
        intent = new_intent(
            self._settings,
            sleeve=sleeve,
            role=IntentRole.PROTECTIVE,
            terms=OrderTerms(
                conid=survey.conid,
                side=OrderSide.SELL,
                quantity=quantity,
                price=price,
                tif=self._tif,
                venue=template.terms.venue if template is not None else SMART_ROUTER,
            ),
            reason=STOP_REASON,
            mode=mode,
            approval_required=False,
            created_at=now,
            expires_at=now + self._ttl,
            parent_id=None if template is None else template.parent_id,
            protective=spec,
        )
        await self._book.add(intent)
        for step in (IntentState.APPROVED, IntentState.STAGED):
            await self._book.advance(intent.intent_id, step, "protects the position")
        clearance = await self._gate.clear(intent)
        if isinstance(clearance, PretradeRefusal):
            await self._book.advance(intent.intent_id, IntentState.REJECTED, clearance.reason)
            return clearance.reason
        try:
            placement = await self._boundary.place(intent.intent_id, clearance)
        except ObliskError as exc:
            return f"not placed: {exc}"
        if placement.status not in (PlacementStatus.PLACED, PlacementStatus.ALREADY_PLACED):
            return f"{placement.status.value}: {placement.detail}"
        return None

    async def _sleeve(self, conid: int, template: OrderIntent | None) -> Sleeve | None:
        assignment = await self._worker.run(repo.holding_assignment, conid)
        if assignment is not None and assignment.owner != UNMANAGED:
            return Sleeve(assignment.owner)
        return None if template is None else template.sleeve

    # ── roll-ups ──

    async def roll_up(self, conid: int, stop: Decimal) -> list[str]:
        """Raise every working stop of ``conid`` to ``stop`` (never lower one); what failed."""
        working = await self._worker.run(repo.intents_for_conid, conid)
        failures = []
        for stored in working:
            intent = stored.intent
            price = intent.terms.price
            if (
                intent.role is not IntentRole.PROTECTIVE
                or intent.state not in BROKER_STATES
                or price.kind != "stop_limit"
                or price.stop >= stop
                or intent.protective is None
            ):
                continue
            tick = await self._tick(conid, stop)
            orders = await self._worker.run(repo.broker_orders_for_intent, intent.intent_id)
            live = [order for order in orders if is_live(order)]
            if tick is None or not live:
                failures.append(f"{intent.intent_id}: no tick or no working order")
                continue
            raised = StopLimitPrice(stop=stop, limit=stop_limit(stop, intent.protective, tick))
            terms = intent.terms.model_copy(update={"price": raised})
            failure = await self._modify(intent, live[-1].order_id, terms)
            if failure is not None:
                failures.append(f"{intent.intent_id}: {failure}")
        return failures

    # ── gaps ──

    async def reprice_gaps(self) -> list[str]:
        """Reprice every triggered stop a gap left unfilled one step toward the bid."""
        working = await self._worker.run(repo.intents_in_states, BROKER_STATES)
        done = []
        for stored in working:
            intent = stored.intent
            price = intent.terms.price
            if intent.role is not IntentRole.PROTECTIVE or price.kind != "stop_limit":
                continue
            outcome = await self._gap_step(stored, price)
            if outcome is not None:
                done.append(outcome)
        return done

    async def _gap_step(self, stored: StoredIntent, price: StopLimitPrice) -> str | None:
        intent = stored.intent
        try:
            quote = await self._reader.quote(intent.conid, option=False)
        except ObliskError:
            return None
        reference = None if quote is None else quote.evidence.reference()
        bid = None if quote is None else quote.evidence.bid
        if reference is None or bid is None or reference > price.stop or bid >= price.limit:
            return None  # not triggered, or the bid still reaches the limit
        history = await self._book.history(intent.intent_id)
        steps = [event for event in history if event.detail.startswith(GAP_REPRICE)]
        if steps and self._now() - steps[-1].at < self._step_interval:
            return None
        if len(steps) >= self._max_steps:
            return None
        orders = await self._worker.run(repo.broker_orders_for_intent, intent.intent_id)
        live = [order for order in orders if is_live(order)]
        tick = await self._tick(intent.conid, bid)
        if not live or tick is None:
            return None
        last = len(steps) + 1 == self._max_steps
        if last and self._emergency_on:
            target = bid * (1 - self._emergency_fraction)
        else:
            target = max(bid, price.limit - self._step_fraction * reference)
        limit = to_tick(target, tick, up=False)
        if limit >= price.limit or limit <= 0:
            return None
        terms = intent.terms.model_copy(
            update={"price": StopLimitPrice(stop=price.stop, limit=limit)}
        )
        detail = f"{GAP_REPRICE} {len(steps) + 1} of {self._max_steps}: limit {limit}"
        failure = await self._modify(intent, live[-1].order_id, terms, detail=detail)
        if failure is None:
            return f"{intent.intent_id}: {detail}"
        self._raise(
            intent.intent_id,
            f"a triggered stop at {price.stop} rests unfilled at {price.limit} with the bid at "
            f"{bid}; it cannot be repriced further: {failure}",
            kind=OrderAlertKind.STOP_GAPPED,
        )
        return None

    # ── alerts ──

    def _raise(
        self,
        intent_id: UUID | None,
        detail: str,
        *,
        kind: OrderAlertKind = OrderAlertKind.STOP_MISSING,
    ) -> None:
        raise_alert(
            self._alert, OrderAlert(kind=kind, intent_id=intent_id, detail=detail, at=self._now())
        )
