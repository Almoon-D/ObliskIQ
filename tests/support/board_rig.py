"""A gathered digest with every part filled, for the board, the dashboard and the cockpit's tests.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from obliskiq.capital.managed import CapitalView
from obliskiq.capital.twr import Period, TwrResult
from obliskiq.config import ObliskConfig
from obliskiq.decision.rebalance_core import RebalancePlan, RoleDecision
from obliskiq.domain.core import RoleStatus, RoleTarget, TargetPortfolio, Trigger
from obliskiq.domain.orders import (
    ExecutionMode,
    IntentRole,
    LimitPrice,
    OrderIntent,
    OrderSide,
    OrderTerms,
    ReconcileRun,
    ReconcileStatus,
    Sleeve,
    TimeInForce,
)
from obliskiq.execution.intents import new_intent
from obliskiq.reporting.digest import Digest, VenueLine
from obliskiq.scoreboard.performance import IndexPoint, Series
from obliskiq.scoreboard.run import PerformanceView
from obliskiq.store.nav import NavReading
from obliskiq.store.orders import StoredIntent

AT = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)
D = Decimal


def decision(action: str, **kw: Any) -> RoleDecision:
    fields: dict[str, Any] = {
        "role": "world_equity",
        "conid": 163606916,
        "symbol": "XDWD",
        "weight": D("0.95"),
        "held": D(5),
        "value": D("612.50"),
        "target": D("902.50"),
        "action": action,
        "quantity": D(2),
        "notional": D("245"),
        "reason": "back to its target",
    }
    fields.update(kw)
    return RoleDecision(**fields)


def role(name: str, status: RoleStatus, weight: str) -> RoleTarget:
    return RoleTarget(
        role=name, status=status, static_weight=D(weight), scale=D(1), target_weight=D(weight),
        held_weight=D(0), reduction_held=False, vehicles=(), reason="r",
    )  # fmt: skip


def plan() -> RebalancePlan:
    return RebalancePlan(
        mode=ExecutionMode.PAPER,
        at=AT,
        capital=CapitalView(
            mode="cap",
            nav=D(100000),
            managed=D(1000),
            unmanaged=D(0),
            reserved=D(50),
            investable=D(950),
            book_value=D("612.50"),
            book_cash=D("387.50"),
            holdings=(),
            detail="a book of 1000.00 (started at 1000)",
        ),
        core_capital=D(902),
        spendable=D(337),
        portfolio=TargetPortfolio(
            variant="D50",
            as_of=AT.date(),
            trend_as_of=date(2026, 10, 30),
            budget=D(902),
            trigger=Trigger.BAND,
            roles=(
                role("world_equity", RoleStatus.FILLED, "0.95"),
                role("cash", RoleStatus.FILLED, "0.05"),
            ),
            vehicles=(),
            trades=(),
            withheld=(),
            notes=(),
        ),
        decisions=[decision("buy"), decision("skip", conid=None, symbol="<none>", value=None)],
        blocked="a <script> in a reason",
    )


def intent(settings: ObliskConfig) -> OrderIntent:
    return new_intent(
        settings,
        sleeve=Sleeve.CORE,
        role=IntentRole.ENTRY,
        terms=OrderTerms(
            conid=163606916, side=OrderSide.BUY, quantity=2, price=LimitPrice(limit=D("122.5")),
            tif=TimeInForce.DAY, venue="SMART",
        ),
        reason="core rebalance",
        mode=ExecutionMode.PAPER,
        approval_required=False,
        created_at=AT,
        expires_at=AT + timedelta(hours=8),
    )  # fmt: skip


def stored(settings: ObliskConfig) -> StoredIntent:
    made = intent(settings)
    return StoredIntent(intent=made, coid=str(made.intent_id))


def digest(settings: ObliskConfig, **kw: Any) -> Digest:
    fields: dict[str, Any] = {
        "day": AT.date(),
        "at": AT,
        "mode": ExecutionMode.PAPER,
        "kill_on": False,
        "reconcile": ReconcileRun(at=AT, status=ReconcileStatus.GREEN, findings=()),
        "nav": NavReading(AT.date(), D(1000), "EUR", "cp", AT),
        "twr": TwrResult(
            periods=(Period(date(2026, 11, 9), AT.date(), D(1000), D(1012), D(0), D("0.012")),),
            value=D("0.012"),
            reason=None,
        ),
        "plan": plan(),
        "intents": [stored(settings)],
        "venues": [VenueLine("XETR", "🟢", "regular", False), VenueLine("XNYS", "🚨", "x", True)],
        "health": ["last heartbeat now"],
        "alerts": {"service.venue_blocked:XNYS": "service.venue_blocked"},
        "problems": ["the mode cannot be read"],
    }
    fields.update(kw)
    return Digest(**fields)


def performance() -> PerformanceView:
    days = (date(2026, 11, 9), AT.date())
    return PerformanceView(
        since=days[0],
        as_of=days[1],
        account=Series(
            "account", (IndexPoint(days[0], D(1)), IndexPoint(days[1], D("1.012"))), None
        ),
        references=(Series("world equity", (), "no marks"),),
        sleeves="no fill yet",
    )
