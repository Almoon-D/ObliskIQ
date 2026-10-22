"""The service assembled over the fake broker, on a simulated clock.

The order path is the one the service runs (``execution.runtime.assemble``), over the order rig's
read client, store and wall clock; the service is built by ``orchestration.assemble.build_service``.
:class:`SimClock` jumps the wall clock to each moment the service sleeps until, so days of the
service run in milliseconds. :func:`skeleton_market` gives the fake broker what the skeleton book
needs: quotes for its three vehicles and the EUR.USD pair, contract search rows, cash and a NAV.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fake_broker import EURUSD, FakeBroker
from order_rig import WallClock, whatif_answer

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.execution.runtime import OrderPath, assemble
from obliskiq.jurisdiction import load_profile
from obliskiq.orchestration.assemble import build_service
from obliskiq.orchestration.service import AlertHooks, Service
from obliskiq.reporting.alerts import AlertBook
from obliskiq.reporting.reporter import Reporter

#: The skeleton's vehicles (config/defaults/core.yaml, config/universe.yaml).
IWDA, EMIM, IBGM = 78999785, 153454120, 43614383
#: The venues the service's calendars cover: the universe directory's and the jobs' anchors.
SERVICE_VENUES = ("XAMS", "XETR", "XLON", "XNAS", "XNYS", "XTKS", "XTSE")


class SimClock:
    """The service's clock over the rig's wall clock: sleeping jumps to the moment."""

    def __init__(self, wall: WallClock) -> None:
        self.wall = wall

    def now(self) -> datetime:
        return self.wall.now

    async def sleep_until(self, moment: datetime, stop: asyncio.Event) -> None:
        await asyncio.sleep(0)
        if not stop.is_set() and moment > self.wall.now:
            self.wall.now = moment


@dataclass
class RecordingNotifier:
    """Keeps every message instead of sending it; ``fail`` makes sends fail."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    async def send(self, subject: str, body: str) -> bool:
        if self.fail:
            return False
        self.sent.append((subject, body))
        return True

    def subjects(self) -> list[str]:
        return [subject for subject, _ in self.sent]

    def alerts(self, kind: str) -> list[str]:
        return [body for subject, body in self.sent if subject == f"ObliskIQ alert: {kind}"]


#: Each instrument's currency, for the whatif's cost.
CURRENCY = {IWDA: "USD", EMIM: "EUR", IBGM: "EUR", EURUSD: "USD"}


def whatif_in_currency(order: dict[str, Any]) -> dict[str, Any]:
    """A whatif that costs the order at its limit in its instrument's currency."""
    conid = int(order.get("conid") or str(order.get("conidex")).split("@")[0])
    size = Decimal(str(order.get("quantity", order.get("fxQty", 1))))
    cost = size * Decimal(str(order.get("price", 1)))
    return whatif_answer(f"{cost:.2f}", CURRENCY.get(conid, "USD"))


def skeleton_market(broker: FakeBroker, *, usd: str = "20000") -> None:
    """Quotes, contract search rows, whatifs, cash in euros and dollars and a NAV for the
    skeleton."""
    broker.whatif_for = whatif_in_currency
    broker.quote(IWDA, bid="146.50", ask="146.60", last="146.55", sigma="15%")
    broker.quote(EMIM, bid="46.40", ask="46.44", last="46.42", sigma="18%")
    broker.quote(IBGM, bid="177.40", ask="177.60", last="177.50", sigma="5%")
    broker.quote(EURUSD, bid="1.1649", ask="1.1651", last="1.165", sigma="8%")
    broker.contracts.update(
        {
            IWDA: ("IWDA", "LSEETF", "USD"),
            EMIM: ("EMIM", "AEB", "EUR"),
            IBGM: ("IBGM", "AEB", "EUR"),
        }
    )
    broker.cash = {"EUR": (Decimal(10000), None), "USD": (Decimal(usd), Decimal("0.86"))}
    broker.nav = Decimal(100000)
    broker.marks.update({IWDA: Decimal("146.55"), EMIM: Decimal("46.42"), IBGM: Decimal("177.5")})


@dataclass
class ServiceRig:
    rig: Any
    path: OrderPath
    service: Service
    notifier: RecordingNotifier
    reporter: Reporter
    hooks: AlertHooks
    clock: SimClock

    @property
    def broker(self) -> FakeBroker:
        return self.rig.broker  # type: ignore[no-any-return]

    def placed(self) -> list[dict[str, Any]]:
        return [body for body in self.broker.placements if not body.get("isCcyConv")]

    def conversions(self) -> list[dict[str, Any]]:
        return [body for body in self.broker.placements if body.get("isCcyConv")]

    def fill_working(self) -> None:
        """The broker fills every working security order at its limit."""
        for order in self.broker.live():
            if order.sec_type != "CASH":
                self.broker.fill(order.order_id, price=str(order.price))


def build_service_rig(
    rig: Any,
    settings: ObliskConfig,
    calendars: Calendars,
    repo_root: Path,
    *,
    keep_session: bool = False,
    notifier: RecordingNotifier | None = None,
    collect: bool = False,
) -> ServiceRig:
    """The service over ``rig``'s client and store, with ``settings`` and ``calendars``. Its
    collection jobs are off unless ``collect``: the fake broker serves no market history."""
    if not collect:
        schedule = settings.collectors.schedule.model_copy(update={"enabled": False})
        collectors = settings.collectors.model_copy(update={"schedule": schedule})
        settings = settings.model_copy(update={"collectors": collectors})
    clock = SimClock(rig.clock)
    notifier = notifier if notifier is not None else RecordingNotifier()
    reporter = Reporter(notifier, AlertBook(rig.data_dir / settings.paths.alert_state))
    hooks = AlertHooks(reporter, now=clock.now)

    async def fills_conversions(seconds: float) -> None:
        rig.clock.advance(seconds)
        for order in rig.broker.live():
            if order.sec_type == "CASH":
                rig.broker.fill(order.order_id, price="1.1651")

    path = assemble(
        settings,
        client=rig.ibkr,
        worker=rig.worker,
        profile=load_profile(repo_root / "config", "es"),
        data_dir=rig.data_dir,
        calendars=calendars,
        alert=hooks.order,
        now=rig.clock,
        sleep=fills_conversions,
    )
    if not keep_session:
        path = dataclasses.replace(path, session=None)
    service = build_service(settings, path=path, reporter=reporter, hooks=hooks, clock=clock)
    return ServiceRig(rig, path, service, notifier, reporter, hooks, clock)
