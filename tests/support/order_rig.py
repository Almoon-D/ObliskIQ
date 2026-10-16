"""The order path assembled for tests: the fake broker, a store on its worker thread, the intent
book, modes, the kill switch, the reply policy and the write boundary, on a wall clock the test
moves. Clearances are made here directly — a test is not ``src/``, so the guard that only the
gate constructs one does not apply — and recorded the way the gate records them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fake_broker import ACCOUNT, FakeBroker

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import (
    ClearanceAction,
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    QuoteEvidence,
    RiskEffect,
    Sleeve,
    StopLimitPrice,
    TimeInForce,
)
from obliskiq.execution.boundary import OrderAlert, WriteBoundary
from obliskiq.execution.intents import new_intent
from obliskiq.execution.killswitch import KillSwitch
from obliskiq.execution.ledger import StoreClearanceLedger
from obliskiq.execution.modes import Modes
from obliskiq.execution.replies import ReplyPolicy
from obliskiq.execution.state import IntentBook
from obliskiq.ibkr.guard import AccountGuard
from obliskiq.ibkr.orders import OrderClient
from obliskiq.ibkr.session import BrokerSession
from obliskiq.store import open_database
from obliskiq.store import orders as repo
from obliskiq.store.orders import StoredIntent
from obliskiq.store.worker import StoreWorker

START = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
CONID = 265598


class WallClock:
    """The wall clock the order path reads; a test moves it."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def terms(
    *,
    side: OrderSide = OrderSide.BUY,
    quantity: str | None = "10",
    cash: str | None = None,
    limit: str = "100",
    stop: str | None = None,
    conid: int = CONID,
    conversion: bool = False,
    venue: str = "SMART",
) -> OrderTerms:
    price: Any = (
        LimitPrice(limit=Decimal(limit))
        if stop is None
        else StopLimitPrice(stop=Decimal(stop), limit=Decimal(limit))
    )
    return OrderTerms(
        conid=conid,
        side=side,
        quantity=None if quantity is None or cash is not None else Decimal(quantity),
        cash_quantity=None if cash is None else Decimal(cash),
        price=price,
        tif=TimeInForce.DAY,
        venue=venue,
        conversion=conversion,
    )


def quote(
    *,
    status: MarketDataStatus = MarketDataStatus.REALTIME,
    delay: int | None = 0,
    bid: str = "99.9",
    ask: str = "100.1",
    at: datetime = START,
    conid: int = CONID,
) -> QuoteEvidence:
    return QuoteEvidence(
        conid=conid,
        status=status,
        delay_seconds=delay,
        observed_at=at,
        bid=Decimal(bid),
        ask=Decimal(ask),
    )


@dataclass
class Rig:
    settings: ObliskConfig
    session: BrokerSession
    broker: FakeBroker
    worker: StoreWorker
    db_path: Path
    data_dir: Path
    clock: WallClock
    guard: AccountGuard
    ledger: StoreClearanceLedger
    client: OrderClient
    book: IntentBook
    modes: Modes
    kill: KillSwitch
    replies: ReplyPolicy
    boundary: WriteBoundary
    alerts: list[OrderAlert] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.worker.aclose()

    async def store(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.worker.run(function, *args, **kwargs)

    async def to_paper(self) -> None:
        await self.modes.upgrade(ExecutionMode.PAPER, actor="test", reason="test rig")

    async def intent(
        self,
        order_terms: OrderTerms | None = None,
        *,
        role: IntentRole = IntentRole.ENTRY,
        reason: str = "entry",
        state: IntentState = IntentState.STAGED,
    ) -> StoredIntent:
        """A stored intent, moved along the legal path to ``state``."""
        chosen = order_terms if order_terms is not None else terms()
        intent = new_intent(
            self.settings,
            sleeve=Sleeve.CORE,
            role=role,
            terms=chosen,
            reason=reason,
            mode=ExecutionMode.PAPER,
            approval_required=False,
            created_at=self.clock(),
            expires_at=self.clock() + timedelta(hours=6),
        )
        stored = await self.book.add(intent)
        path = [IntentState.APPROVED, IntentState.STAGED, IntentState.SUBMITTED]
        for step in path:
            if (await self.book.get(intent.intent_id)).intent.state is state:
                break
            await self.book.advance(intent.intent_id, step, "test path")
        return stored

    async def clear(
        self,
        intent: OrderIntent,
        *,
        effect: RiskEffect = RiskEffect.ADDS,
        action: ClearanceAction = ClearanceAction.PLACE,
        order_id: str | None = None,
        order_terms: OrderTerms | None = None,
        evidence: QuoteEvidence | None = None,
        asset_class: AssetClass = AssetClass.STOCK,
        sigma: float | None = 0.3,
        ttl_seconds: int = 60,
        record: bool = True,
    ) -> PretradeClearance:
        """A clearance as the gate would issue it, recorded in the store."""
        now = self.clock()
        clearance = PretradeClearance(
            clearance_id=uuid.uuid4(),
            intent_id=intent.intent_id,
            action=action,
            order_id=order_id,
            terms=order_terms if order_terms is not None else intent.terms,
            effect=effect,
            asset_class=asset_class,
            quote=evidence if evidence is not None else quote(at=now, conid=intent.conid),
            sigma=sigma,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        if record:
            await self.store(repo.record_clearance, clearance)
        return clearance


async def build_rig(
    *,
    settings: ObliskConfig,
    session: BrokerSession,
    broker: FakeBroker,
    tmp_path: Path,
    profile: str = "paper",
    account: str = ACCOUNT,
) -> Rig:
    clock = WallClock()
    db_path = tmp_path / "orders.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    store_config = settings.store
    worker = StoreWorker(
        lambda: open_database(
            db_path,
            busy_timeout_ms=store_config.busy_timeout_ms,
            synchronous=store_config.synchronous,
        )
    )
    guard = AccountGuard(account_id=account, profile=profile, paper_prefixes=("DU",))  # type: ignore[arg-type]
    ledger = StoreClearanceLedger(worker, now=clock)
    client = OrderClient(session, settings, guard, ledger, now=clock)
    book = IntentBook(worker, coid_max_length=settings.execution.orders.coid_max_length, now=clock)
    modes = Modes(worker, settings, profile=profile, now=clock)  # type: ignore[arg-type]
    kill = KillSwitch(data_dir, worker, settings, now=clock)
    replies = ReplyPolicy(settings)
    alerts: list[OrderAlert] = []
    boundary = WriteBoundary(
        client=client,
        book=book,
        worker=worker,
        modes=modes,
        kill_switch=kill,
        guard=guard,
        replies=replies,
        alert=alerts.append,
        now=clock,
    )
    return Rig(
        settings=settings,
        session=session,
        broker=broker,
        worker=worker,
        db_path=db_path,
        data_dir=data_dir,
        clock=clock,
        guard=guard,
        ledger=ledger,
        client=client,
        book=book,
        modes=modes,
        kill=kill,
        replies=replies,
        boundary=boundary,
        alerts=alerts,
    )
