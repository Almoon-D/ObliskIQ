"""``obliskiq flatten --confirm``: closing orders for every managed position.

Separate from the kill switch (MASTER_PLAN §5.12): the switch stops new risk; flatten sells what
the sleeves hold. For each long position the broker reports, assigned to a sleeve, flatten makes
an exit intent (role ``exit``, reason ``override``: an owner command) selling all of it at a
limit at the bid, and places it through :class:`~obliskiq.execution.exits.ExitCoordinator`,
which cuts the position's protective stop first and restores it if the exit ends unfilled (I5).
Every closing order passes the pre-trade gate: within the position (I2), the collar, the quote
policy and the calendars. They reduce risk, so they pass with the kill switch on (I3).

What flatten does not touch, it reports: ``unmanaged`` holdings and holdings no sleeve owns, a
short position at the broker (a buy would add risk), an instrument the gate cannot describe, and
a position with no bid to price its close. It does not cancel working buys: that is the kill
switch's.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass
from ..domain.orders import (
    UNMANAGED,
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderSide,
    OrderTerms,
    Sleeve,
)
from ..errors import ObliskError
from ..ibkr.orders import SMART_ROUTER
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .account import AccountReader
from .exits import ExitCoordinator
from .instruments import InstrumentDirectory
from .intents import new_intent
from .modes import Modes
from .state import IntentBook

#: The exit reason a flatten gives (lite's `exit_reasons_allowed`): the owner overrides.
FLATTEN_REASON = "override"


@dataclass(frozen=True, slots=True)
class FlattenLine:
    """What flatten did with one position: a closing order working (``closing``), a holding it
    leaves alone by rule (``skipped``), or neither (``outcome`` says why)."""

    conid: int
    quantity: Decimal
    closing: bool
    outcome: str
    intent_id: UUID | None = None
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class FlattenReport:
    lines: tuple[FlattenLine, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Every managed position has a working closing order, and nothing failed."""
        return self.error is None and all(line.closing or line.skipped for line in self.lines)

    def render(self) -> str:
        if self.error is not None:
            return f"flatten stopped: {self.error}"
        if not self.lines:
            return "flatten: no positions"
        rows = ["flatten:"]
        for line in self.lines:
            rows.append(f"  conid {line.conid} ({line.quantity}): {line.outcome}")
        return "\n".join(rows)


class Flattener:
    """Builds and places a closing order for each managed position."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        coordinator: ExitCoordinator,
        book: IntentBook,
        worker: StoreWorker,
        reader: AccountReader,
        directory: InstrumentDirectory,
        modes: Modes,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._coordinator = coordinator
        self._book = book
        self._worker = worker
        self._reader = reader
        self._directory = directory
        self._modes = modes
        self._now = now
        config = settings.execution.flatten
        self._ttl = timedelta(minutes=config.intent_ttl_minutes)
        self._tif = config.tif

    async def flatten(self, *, actor: str) -> FlattenReport:
        """Close every managed long position; report what was done with each."""
        try:
            positions = await self._reader.positions()
            mode = await self._modes.current()
        except ObliskError as exc:
            return FlattenReport(lines=(), error=f"the positions cannot be read: {exc}")
        lines = []
        for position in sorted(positions.positions, key=lambda p: p.conid):
            if position.quantity == 0:
                continue
            lines.append(await self._one(position.conid, position.quantity, mode, actor))
        return FlattenReport(lines=tuple(lines))

    async def _one(
        self, conid: int, quantity: Decimal, mode: ExecutionMode, actor: str
    ) -> FlattenLine:
        if quantity < 0:
            return FlattenLine(conid, quantity, False, "a short position: a buy would add risk")
        assignment = await self._worker.run(repo.holding_assignment, conid)
        if assignment is None or assignment.owner == UNMANAGED:
            why = "unmanaged" if assignment is not None else "held by no sleeve"
            return FlattenLine(conid, quantity, False, f"skipped: {why}", skipped=True)
        instrument = self._directory.describe(conid)
        if instrument is None:
            return FlattenLine(conid, quantity, False, "the instrument is not described")
        try:
            quote = await self._reader.quote(
                conid, option=instrument.asset_class is AssetClass.OPTION
            )
        except ObliskError as exc:
            return FlattenLine(conid, quantity, False, f"no quote: {exc}")
        bid = None if quote is None else quote.evidence.bid
        if bid is None:
            return FlattenLine(conid, quantity, False, "no bid to price the close")
        now = self._now()
        intent = new_intent(
            self._settings,
            sleeve=Sleeve(assignment.owner),
            role=IntentRole.EXIT,
            terms=OrderTerms(
                conid=conid,
                side=OrderSide.SELL,
                quantity=quantity,
                price=LimitPrice(limit=bid),
                tif=self._tif,
                venue=SMART_ROUTER,
            ),
            reason=FLATTEN_REASON,
            mode=mode,
            approval_required=False,
            created_at=now,
            expires_at=now + self._ttl,
        )
        await self._book.add(intent)
        for step in (IntentState.APPROVED, IntentState.STAGED):
            await self._book.advance(intent.intent_id, step, f"flatten, by {actor}")
        try:
            result = await self._coordinator.place_exit(intent.intent_id)
        except ObliskError as exc:  # one position's failure never stops the others
            return FlattenLine(conid, quantity, False, f"stopped: {exc}", intent.intent_id)
        if result.placed:
            return FlattenLine(conid, quantity, True, "closing", intent.intent_id)
        why = result.detail if result.refusal is None else f"refused ({result.refusal.reason})"
        return FlattenLine(conid, quantity, False, why, intent.intent_id)
