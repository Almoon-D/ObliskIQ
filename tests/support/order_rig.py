"""The order path assembled for tests: the fake broker, a store on its worker thread, the intent
book, modes, the kill switch, the reply policy and the write boundary, on a wall clock the test
moves; and, for Session 4a, the read client, the pre-trade gate over a directory of test
instruments, the exit coordinator, the approval queue and flatten. Session 4's tests make
clearances here directly — a test is not ``src/``, so the guard that only the gate constructs one
does not apply — and record them the way the gate records them; Session 4a's ask the gate.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fake_broker import ACCOUNT, FakeBroker

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_instruments import EligibilityConfig
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.instrument import TradedInstrument
from obliskiq.domain.orders import (
    ClearanceAction,
    ExecutionMode,
    HoldingAssignment,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    ProtectiveSpec,
    QuoteEvidence,
    RiskEffect,
    Sleeve,
    StopLimitPrice,
    TimeInForce,
)
from obliskiq.execution.account import IbkrAccountReader
from obliskiq.execution.approvals import ApprovalQueue
from obliskiq.execution.boundary import OrderAlert, WriteBoundary
from obliskiq.execution.currency import CurrencyManager
from obliskiq.execution.executor import Executor
from obliskiq.execution.exits import ExitCoordinator
from obliskiq.execution.flatten import Flattener
from obliskiq.execution.intents import new_intent
from obliskiq.execution.killswitch import KillSwitch
from obliskiq.execution.ledger import StoreClearanceLedger
from obliskiq.execution.modes import Modes
from obliskiq.execution.pretrade import PretradeGate
from obliskiq.execution.pricing import Pricer
from obliskiq.execution.protective import ProtectiveManager
from obliskiq.execution.reconcile import Reconciler
from obliskiq.execution.replies import ReplyPolicy
from obliskiq.execution.shadow import ShadowFiller
from obliskiq.execution.state import IntentBook
from obliskiq.ibkr.client import IbkrClient
from obliskiq.ibkr.guard import AccountGuard
from obliskiq.ibkr.orders import OrderClient
from obliskiq.ibkr.session import BrokerSession
from obliskiq.jurisdiction.eligibility import SkeletonEligibility
from obliskiq.quant.costs import CostBook
from obliskiq.store import open_database
from obliskiq.store import orders as repo
from obliskiq.store.orders import StoredIntent
from obliskiq.store.worker import StoreWorker

START = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
#: Inside every test venue's tradable window (New York, Xetra, Eurex) on that Tuesday.
OPEN = datetime(2026, 10, 13, 14, 30, tzinfo=UTC)
CONID = 265598

# ── the instruments the gate tests trade ──

STOCK = CONID
EUR_ETF = 89005163
US_ETF = 756733
OPTION = 600001  # a call on STOCK
ETP_OPTION = 600002  # a call on US_ETF, an ETP the policy does not allow: mandatory close
LEVERAGED = 600003
FUTURE = 600004
FUTURE_OPTION = 600005
EXPIRY = date(2027, 1, 15)
#: The venues the test instruments trade on.
VENUES = ("XETR", "XEUR", "XNAS", "XNYS")


def _instrument(
    conid: int, symbol: str, exchange: str, currency: str, asset: AssetClass, **kw: Any
) -> TradedInstrument:
    fields: dict[str, Any] = {
        "conid": conid,
        "symbol": symbol,
        "listing_exchange": exchange,
        "currency": currency,
        "asset_class": asset,
        "description": kw.pop("description", f"{symbol} test instrument"),
        "multiplier": Decimal(kw.pop("multiplier", "1")),
        "underlying_conid": kw.pop("underlying", None),
        "expiry": kw.pop("expiry", None),
    }
    return TradedInstrument(**fields)


INSTRUMENTS: dict[int, TradedInstrument] = {
    i.conid: i
    for i in (
        _instrument(STOCK, "STK1", "NYSE", "USD", AssetClass.STOCK),
        _instrument(EUR_ETF, "EXV1", "IBIS", "EUR", AssetClass.ETF),
        _instrument(US_ETF, "SPY", "ARCA", "USD", AssetClass.ETF),
        _instrument(
            OPTION,
            "STK1 C100",
            "CBOE",
            "USD",
            AssetClass.OPTION,
            multiplier="100",
            underlying=STOCK,
            expiry=EXPIRY,
        ),
        _instrument(
            ETP_OPTION,
            "SPY C600",
            "CBOE",
            "USD",
            AssetClass.OPTION,
            multiplier="100",
            underlying=US_ETF,
            expiry=EXPIRY,
        ),
        _instrument(
            LEVERAGED,
            "TQQQ",
            "NASDAQ",
            "USD",
            AssetClass.ETF,
            description="ProShares UltraPro QQQ",
        ),
        _instrument(FUTURE, "ESZ6", "NYSE", "USD", AssetClass.FUTURE),
        _instrument(
            FUTURE_OPTION,
            "ES C6000",
            "CBOE",
            "USD",
            AssetClass.OPTION,
            multiplier="50",
            underlying=FUTURE,
            expiry=EXPIRY,
        ),
    )
}

#: The allowlist the gate tests run under: every test instrument but the US ETF.
ALLOWED = (STOCK, EUR_ETF, OPTION, ETP_OPTION, LEVERAGED, FUTURE, FUTURE_OPTION)


def gate_settings(settings: ObliskConfig) -> ObliskConfig:
    """The shipped settings with the test instruments in the skeleton allowlist."""
    allowlist = [INSTRUMENTS[conid] for conid in ALLOWED]
    return settings.model_copy(
        update={
            "eligibility": EligibilityConfig(skeleton_allowlist=allowlist, skeleton_tax_facts=[])
        }
    )


def whole_account(settings: ObliskConfig) -> ObliskConfig:
    """``settings`` managing the whole account (the shipped default caps the book)."""
    capital = settings.capital.model_copy(update={"mode": "whole_account"})
    return settings.model_copy(update={"capital": capital})


def with_caps(settings: ObliskConfig, **caps: Any) -> ObliskConfig:
    """``settings`` with other pre-trade caps."""
    pretrade = settings.execution.pretrade
    new_caps = pretrade.caps.model_copy(update=caps)
    execution = settings.execution.model_copy(
        update={"pretrade": pretrade.model_copy(update={"caps": new_caps})}
    )
    return settings.model_copy(update={"execution": execution})


class Directory:
    """The test instruments, described; a test may add or drop one."""

    def __init__(self) -> None:
        self.instruments = dict(INSTRUMENTS)

    def describe(self, conid: int) -> TradedInstrument | None:
        return self.instruments.get(conid)


#: The limit offset the test stops rest by: 5 % below the stop.
TEST_OFFSET = Decimal("0.05")


def spec(stop: str, offset: Decimal = TEST_OFFSET) -> ProtectiveSpec:
    """A protective spec for a stop at ``stop``."""
    return ProtectiveSpec(stop=Decimal(stop), limit_offset_fraction=offset, basis="test spec")


def whatif_answer(total: str, currency: str, **extra: Any) -> dict[str, Any]:
    """A whatif the broker answers: the cost ``total`` in ``currency``, no margin change."""
    zero = {"current": "0", "change": "0", "after": "0"}
    answer: dict[str, Any] = {
        "amount": {
            "amount": f"{total} {currency} (10 Shares)",
            "commission": f"1 {currency}",
            "total": f"{total} {currency}",
        },
        "equity": {"current": "100,000", "change": "-1", "after": "99,999"},
        "initial": zero,
        "maintenance": zero,
        "position": {"current": "0", "change": "10", "after": "10"},
        "warn": None,
        "error": None,
    }
    answer.update(extra)
    return answer


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
    ibkr: IbkrClient
    reader: IbkrAccountReader
    directory: Directory
    calendars: Calendars
    gate: PretradeGate
    coordinator: ExitCoordinator
    approvals: ApprovalQueue
    flattener: Flattener
    alerts: list[OrderAlert] = field(default_factory=list)
    pricer: Pricer = None  # type: ignore[assignment]
    currency: CurrencyManager = None  # type: ignore[assignment]
    protective: ProtectiveManager = None  # type: ignore[assignment]
    reconciler: Reconciler = None  # type: ignore[assignment]
    executor: Executor = None  # type: ignore[assignment]

    def make_execution(
        self, *, sleep: Any = None, settings: ObliskConfig | None = None
    ) -> Executor:
        """The execution mechanics over this rig's gate (call ``make_gate`` first to change it);
        ``sleep`` is the wait a conversion's fill takes (by default the clock moves on)."""
        chosen = settings if settings is not None else self.settings
        contracts = self.ibkr.contracts

        async def moves_on(seconds: float) -> None:
            self.clock.advance(seconds)

        common: dict[str, Any] = {
            "gate": self.gate,
            "boundary": self.boundary,
            "book": self.book,
            "worker": self.worker,
            "reader": self.reader,
            "alert": self.alerts.append,
            "now": self.clock,
        }
        self.pricer = Pricer(chosen)
        self.currency = CurrencyManager(
            chosen,
            **common,
            directory=self.directory,
            rules=contracts,
            modes=self.modes,
            sleep=sleep if sleep is not None else moves_on,
        )
        self.protective = ProtectiveManager(chosen, **common, rules=contracts, modes=self.modes)
        self.reconciler = Reconciler(
            chosen,
            boundary=self.boundary,
            worker=self.worker,
            reader=self.reader,
            directory=self.directory,
            alert=self.alerts.append,
            now=self.clock,
        )
        self.executor = Executor(
            chosen,
            **common,
            orders=self.client,
            contracts=contracts,
            directory=self.directory,
            calendars=self.calendars,
            modes=self.modes,
            coordinator=self.coordinator,
            currency=self.currency,
            pricer=self.pricer,
            protective=self.protective,
            reconciler=self.reconciler,
            shadow=ShadowFiller(CostBook.from_config(chosen)),
        )
        return self.executor

    def make_gate(
        self, *, calendars: Calendars | None = None, settings: ObliskConfig | None = None
    ) -> PretradeGate:
        """A gate over this rig, with other calendars or settings; it replaces the rig's."""
        chosen = settings if settings is not None else self.settings
        self.gate = PretradeGate(
            chosen,
            worker=self.worker,
            modes=self.modes,
            kill_switch=self.kill,
            guard=self.guard,
            calendars=calendars if calendars is not None else self.calendars,
            directory=self.directory,
            eligibility=SkeletonEligibility(chosen.eligibility.skeleton_allowlist),
            reader=self.reader,
            alert=self.alerts.append,
            now=self.clock,
        )
        self.coordinator = ExitCoordinator(
            chosen,
            gate=self.gate,
            boundary=self.boundary,
            book=self.book,
            worker=self.worker,
            alert=self.alerts.append,
            now=self.clock,
        )
        self.flattener = Flattener(
            chosen,
            coordinator=self.coordinator,
            book=self.book,
            worker=self.worker,
            reader=self.reader,
            directory=self.directory,
            modes=self.modes,
            now=self.clock,
        )
        return self.gate

    def ready(self) -> None:
        """The state every gate test starts from: inside the venues' windows, cash in EUR and
        USD, a NAV, a real-time quote for every test instrument and a whatif in USD."""
        self.clock.now = OPEN
        broker = self.broker
        broker.cash = {"EUR": (Decimal(10000), None), "USD": (Decimal(20000), Decimal("0.86"))}
        broker.nav = Decimal(100000)
        for conid, instrument in INSTRUMENTS.items():
            if instrument.asset_class is AssetClass.OPTION:
                broker.quote(conid, bid="4.9", ask="5.1", last="5", option=True)
            else:
                broker.quote(conid)
        broker.whatif_answer = whatif_answer("1001.00", "USD")

    async def staged(
        self,
        conid: int = STOCK,
        *,
        side: OrderSide = OrderSide.BUY,
        quantity: str = "10",
        limit: str = "100",
        stop: str | None = None,
        role: IntentRole | None = None,
        reason: str | None = None,
        sleeve: Sleeve = Sleeve.CORE,
        approval_required: bool = False,
        state: IntentState = IntentState.STAGED,
    ) -> StoredIntent:
        """A stored intent on a test instrument: a buy is an entry, a sell an exit."""
        buying = side is OrderSide.BUY
        chosen_role = (
            role if role is not None else (IntentRole.ENTRY if buying else IntentRole.EXIT)
        )
        default_reason = {IntentRole.ENTRY: "entry", IntentRole.PROTECTIVE: "stop"}.get(
            chosen_role, "target"
        )
        protective = (
            spec(stop) if stop is not None and chosen_role is IntentRole.PROTECTIVE else None
        )
        return await self.intent(
            terms(side=side, quantity=quantity, limit=limit, stop=stop, conid=conid),
            role=chosen_role,
            reason=reason if reason is not None else default_reason,
            sleeve=sleeve,
            approval_required=approval_required,
            state=state,
            protective=protective,
        )

    async def hold(self, conid: int, quantity: str, *, owner: Any = Sleeve.CORE) -> None:
        """A position at the broker, assigned to ``owner`` (``None``: to no one)."""
        instrument = INSTRUMENTS.get(conid)
        self.broker.hold(conid, quantity, instrument.currency if instrument else "USD")
        if owner is not None:
            await self.assign(conid, owner)

    async def assign(self, conid: int, owner: Any = Sleeve.CORE) -> None:
        """Assign a holding (``"unmanaged"`` or a sleeve) in the registry."""
        await self.store(
            repo.assign_holding,
            HoldingAssignment(
                conid=conid,
                owner=owner,
                assigned_by="test",
                assigned_at=self.clock(),
                reason="test",
            ),
        )

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
        sleeve: Sleeve = Sleeve.CORE,
        approval_required: bool = False,
        protective: ProtectiveSpec | None = None,
        mode: ExecutionMode = ExecutionMode.PAPER,
        ttl: timedelta = timedelta(hours=6),
    ) -> StoredIntent:
        """A stored intent, moved along the legal path to ``state``."""
        chosen = order_terms if order_terms is not None else terms()
        intent = new_intent(
            self.settings,
            sleeve=sleeve,
            role=role,
            terms=chosen,
            reason=reason,
            mode=mode,
            approval_required=approval_required,
            created_at=self.clock(),
            expires_at=self.clock() + ttl,
            protective=protective,
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
    calendars: Calendars,
    profile: str = "paper",
    account: str = ACCOUNT,
) -> Rig:
    clock = WallClock()
    broker.trade_clock = clock
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
    ibkr = IbkrClient(session, settings, guard)
    if guard.profile == "paper" and account.startswith("DU"):
        await ibkr.start()
    reader = IbkrAccountReader(ibkr, client, settings, now=clock)
    rig = Rig(
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
        ibkr=ibkr,
        reader=reader,
        directory=Directory(),
        calendars=calendars,
        gate=None,  # type: ignore[arg-type]
        coordinator=None,  # type: ignore[arg-type]
        approvals=ApprovalQueue(settings, worker, book, now=clock),
        flattener=None,  # type: ignore[arg-type]
        alerts=alerts,
    )
    rig.make_gate()
    return rig
