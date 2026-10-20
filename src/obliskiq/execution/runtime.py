"""The order path assembled for a command: the read client, the order client, the store, the
modes, the kill switch, the write boundary, the pre-trade gate, the exit coordinator, flatten, and
the execution mechanics over them — pricing, the currency manager, protective exits,
reconciliation and the executor.

:func:`open_order_path` opens a brokerage session the way the read-only preflight does — the
profile and transport refusal first, so a paper run never reads an OAuth key, then the paper
guard, the transport and the account guard (``IbkrClient.start``) — and closes it, with the
store, when the command ends. :func:`assemble` wires the parts over a client that is already
started; tests drive it over the fake broker.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..env import Environment
from ..errors import ExecutionError
from ..ibkr.client import IbkrClient
from ..ibkr.guard import AccountGuard
from ..ibkr.orders import OrderClient
from ..ibkr.session import BrokerSession
from ..ibkr.transport import Transport, build_transport, transport_refusal
from ..ibkr.transport.gateway_login import LoginDriver
from ..jurisdiction.eligibility import ELIGIBILITY_POLICIES
from ..jurisdiction.profile import JurisdictionProfile
from ..quant.costs import CostBook
from ..store import open_store
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy
from ..util.timeutil import utc_now
from .account import IbkrAccountReader
from .approvals import ApprovalQueue
from .boundary import OrderAlertHook, WriteBoundary
from .currency import CurrencyManager
from .executor import Executor
from .exits import ExitCoordinator
from .flatten import Flattener
from .instruments import ConfiguredDirectory
from .killswitch import KillSwitch
from .ledger import StoreClearanceLedger
from .modes import Modes
from .pretrade import PretradeGate
from .pricing import Pricer
from .protective import ProtectiveManager
from .reconcile import Reconciler
from .replies import ReplyPolicy
from .shadow import ShadowFiller
from .state import IntentBook


@dataclass(frozen=True)
class OrderPath:
    """Everything a command that writes orders needs, wired once."""

    settings: ObliskConfig
    client: IbkrClient
    orders: OrderClient
    worker: StoreWorker
    book: IntentBook
    modes: Modes
    kill: KillSwitch
    boundary: WriteBoundary
    gate: PretradeGate
    coordinator: ExitCoordinator
    reader: IbkrAccountReader
    directory: ConfiguredDirectory
    calendars: Calendars
    approvals: ApprovalQueue
    flattener: Flattener
    pricer: Pricer
    currency: CurrencyManager
    protective: ProtectiveManager
    reconciler: Reconciler
    executor: Executor


def calendar_venues(settings: ObliskConfig, directory: ConfiguredDirectory) -> tuple[str, ...]:
    """The venues the directory's instruments trade on (an unmapped code is left to the gate,
    which refuses it)."""
    exchanges = settings.ibkr_exchanges
    return tuple(
        sorted(
            {exchanges[code].venue for code in directory.listing_exchanges() if code in exchanges}
        )
    )


def assemble(
    settings: ObliskConfig,
    *,
    client: IbkrClient,
    worker: StoreWorker,
    profile: JurisdictionProfile,
    data_dir: Path,
    calendars: Calendars,
    alert: OrderAlertHook | None = None,
    now: Callable[[], datetime] = utc_now,
) -> OrderPath:
    """The order path over a started client."""
    guard = client.guard
    orders = OrderClient(
        client.session, settings, guard, StoreClearanceLedger(worker, now=now), now=now
    )
    book = IntentBook(worker, coid_max_length=settings.execution.orders.coid_max_length, now=now)
    modes = Modes(worker, settings, profile=guard.profile, now=now)
    kill = KillSwitch(data_dir, worker, settings, now=now)
    boundary = WriteBoundary(
        client=orders,
        book=book,
        worker=worker,
        modes=modes,
        kill_switch=kill,
        guard=guard,
        replies=ReplyPolicy(settings),
        alert=alert,
        now=now,
    )
    directory = ConfiguredDirectory.from_settings(settings)
    reader = IbkrAccountReader(client, orders, settings, now=now)
    gate = PretradeGate(
        settings,
        worker=worker,
        modes=modes,
        kill_switch=kill,
        guard=guard,
        calendars=calendars,
        directory=directory,
        eligibility=ELIGIBILITY_POLICIES.resolve(profile, settings),
        reader=reader,
        alert=alert,
        now=now,
    )
    coordinator = ExitCoordinator(
        settings, gate=gate, boundary=boundary, book=book, worker=worker, alert=alert, now=now
    )
    flattener = Flattener(
        settings,
        coordinator=coordinator,
        book=book,
        worker=worker,
        reader=reader,
        directory=directory,
        modes=modes,
        now=now,
    )
    contracts = client.contracts
    pricer = Pricer(settings)
    currency = CurrencyManager(
        settings,
        gate=gate,
        boundary=boundary,
        book=book,
        worker=worker,
        reader=reader,
        directory=directory,
        rules=contracts,
        modes=modes,
        alert=alert,
        now=now,
    )
    protective = ProtectiveManager(
        settings,
        gate=gate,
        boundary=boundary,
        book=book,
        worker=worker,
        reader=reader,
        rules=contracts,
        modes=modes,
        alert=alert,
        now=now,
    )
    reconciler = Reconciler(
        settings,
        boundary=boundary,
        worker=worker,
        reader=reader,
        directory=directory,
        alert=alert,
        now=now,
    )
    executor = Executor(
        settings,
        orders=orders,
        gate=gate,
        boundary=boundary,
        book=book,
        worker=worker,
        reader=reader,
        contracts=contracts,
        directory=directory,
        calendars=calendars,
        modes=modes,
        coordinator=coordinator,
        currency=currency,
        pricer=pricer,
        protective=protective,
        reconciler=reconciler,
        shadow=ShadowFiller(CostBook.from_config(settings)),
        alert=alert,
        now=now,
    )
    return OrderPath(
        settings=settings,
        client=client,
        orders=orders,
        worker=worker,
        book=book,
        modes=modes,
        kill=kill,
        boundary=boundary,
        gate=gate,
        coordinator=coordinator,
        reader=reader,
        directory=directory,
        calendars=calendars,
        approvals=ApprovalQueue(settings, worker, book, now=now),
        flattener=flattener,
        pricer=pricer,
        currency=currency,
        protective=protective,
        reconciler=reconciler,
        executor=executor,
    )


@asynccontextmanager
async def open_order_path(
    env: Environment,
    settings: ObliskConfig,
    profile: JurisdictionProfile,
    *,
    data_dir: Path,
    transport: Transport | None = None,
    login_driver: LoginDriver | None = None,
    alert: OrderAlertHook | None = None,
) -> AsyncIterator[OrderPath]:
    """A started order path for one command; closed, with the store, when it ends."""
    if env.ibkr_profile is None:
        raise ExecutionError("OBLISK_IBKR_PROFILE is unset")
    refusal = transport_refusal(env) if transport is None else None
    if refusal is not None:
        raise ExecutionError(refusal)
    ibkr = settings.ibkr
    account_id = env.ibkr_account_id.get_secret_value() if env.ibkr_account_id else ""
    guard = AccountGuard(account_id, env.ibkr_profile, tuple(ibkr.paper_account_prefixes))
    guard.check_prefix()
    if transport is None:
        transport = build_transport(env, settings, data_dir=data_dir, login_driver=login_driver)
    session = BrokerSession(
        transport, ibkr, RetryPolicy.from_settings(settings.http), known_ids=(account_id,)
    )
    client = IbkrClient(session, settings, guard)
    await client.start()
    worker = StoreWorker(lambda: open_store(settings, data_dir))
    try:
        directory = ConfiguredDirectory.from_settings(settings)
        calendars = await asyncio.to_thread(
            Calendars.from_settings,
            settings,
            reference=utc_now().date(),
            venues=calendar_venues(settings, directory),
            data_dir=data_dir,
        )
        yield assemble(
            settings,
            client=client,
            worker=worker,
            profile=profile,
            data_dir=data_dir,
            calendars=calendars,
            alert=alert,
        )
    finally:
        await worker.aclose()
        await client.aclose()
