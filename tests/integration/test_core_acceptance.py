"""Session 13's acceptance: each variant Session 4c registered produces a target portfolio through
the live rebalancer, from fixtures, for a small and a large account.

The rebalancer runs on the shipped universe and capabilities: its vehicle selector proposes the
Xetra lines the fake broker quotes, gold and broad commodities have no proposable vehicle (their
venues' capabilities are unverified) and stay in cash, and a tilted variant reads its trend on
the month-end closes stored for those vehicles.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from capital_rig import make_rebalancer
from core_rig import month_ends, seed_closes, seed_vehicle
from fake_broker import FakeBroker
from order_rig import build_rig
from service_rig import SERVICE_VENUES

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.domain.core import RoleStatus, Trigger
from obliskiq.domain.orders import ExecutionMode

#: Monday 26 October 2026, 10:00 in Frankfurt: Xetra trades.
OPEN = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
#: The Xetra lines the selector proposes for the core's roles, and their quoted prices in euros.
XETRA = {
    355720826: ("SPPW", "40"),
    163606916: ("XDWD", "120"),
    385047109: ("VFEA", "60"),
    280737629: ("XMME", "70"),
    53507356: ("DBXQ", "220"),
    53507360: ("X57E", "50"),
    48701963: ("DBXG", "230"),
    43614368: ("IBCL", "150"),
    255605037: ("TIUP", "120"),
    220580484: ("XUTD", "30"),
    46041702: ("XEON", "145"),
}
SIZES = {"small": "1000", "large": "1000000"}


@pytest.fixture(scope="module")
def calendars(shipped_settings: ObliskConfig) -> Calendars:
    return Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=SERVICE_VENUES
    )


@pytest.fixture
async def rig(
    make_session: Callable[..., Any],
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    with respx.mock(assert_all_called=False) as router:
        broker = FakeBroker(router)
        built = await build_rig(
            settings=shipped_settings,
            session=make_session(),
            broker=broker,
            tmp_path=tmp_path,
            calendars=calendars,
        )
        for conid, (symbol, price) in XETRA.items():
            low, high = Decimal(price) - Decimal("0.01"), Decimal(price) + Decimal("0.01")
            broker.quote(conid, bid=str(low), ask=str(high), last=price, sigma="10%")
            broker.contracts[conid] = (symbol, "IBIS2", "EUR")
            closes = {
                day: float(price) * (1 + 0.005 * i)
                for i, day in enumerate(month_ends(date(2026, 9, 30), 28))
            }
            await built.store(seed_vehicle, conid, symbol)
            await built.store(seed_closes, conid, closes)
        broker.cash = {"EUR": (Decimal(2_000_000), None)}
        broker.nav = Decimal(2_000_000)
        built.clock.now = OPEN
        yield built
        await built.aclose()


def sized(settings: ObliskConfig, variant: str, cap: str) -> ObliskConfig:
    capital = settings.capital.model_copy(update={"cap_base": Decimal(cap)})
    core = settings.core.model_copy(update={"variant": variant})
    return settings.model_copy(update={"capital": capital, "core": core})


@pytest.mark.parametrize("size", sorted(SIZES))
async def test_every_registered_variant_produces_a_target_portfolio(
    rig: Any, shipped_settings: ObliskConfig, size: str
) -> None:
    for name in shipped_settings.core.variants:
        settings = sized(shipped_settings, name, SIZES[size])
        rebalancer = make_rebalancer(rig, settings)
        plan = await rebalancer.survey(ExecutionMode.SHADOW)
        assert plan.blocked is None, (name, size, plan.blocked)
        portfolio = plan.portfolio
        assert portfolio is not None
        variant = rebalancer.variant
        assert (portfolio.variant, portfolio.trigger) == (name, Trigger.START)
        assert portfolio.budget == Decimal(SIZES[size]) * Decimal("0.95")
        assert (portfolio.trend_as_of is not None) == variant.tilts
        roles = {role.role: role for role in portfolio.roles}
        assert set(roles) == set(variant.roles)
        for role in ("gold", "broad_commodities"):
            if role in roles:
                assert roles[role].status is RoleStatus.UNFILLED
                assert roles[role].target_weight == 0
        filled = [r for r in roles.values() if r.status is RoleStatus.FILLED and r.target_weight]
        assert filled, (name, size)
        assert all("untilted" not in r.reason for r in filled)  # the stored history suffices
        assert sum((v.weight for v in portfolio.vehicles), Decimal(0)) <= 1
        named = {v.conid for v in portfolio.vehicles if v.weight > 0}
        assert named <= set(XETRA)
        buys = {d.role for d in plan.trades}
        if size == "large":
            # A million buys whole units of every filled role at its target.
            assert buys == {r.role for r in filled}, name
        else:
            # A thousand buys what a unit's price and the minimum trade allow, and says why not.
            assert all(d.reason for d in plan.decisions)
        assert f"variant {name}: start" in plan.render()


async def test_the_chosen_variant_holds_its_risk_balance_on_the_proposable_roles(
    rig: Any, shipped_settings: ObliskConfig
) -> None:
    plan = await make_rebalancer(rig, sized(shipped_settings, "A", "1000000")).survey(
        ExecutionMode.SHADOW
    )
    portfolio = plan.portfolio
    assert portfolio is not None
    weights = {v.conid: v.weight for v in portfolio.vehicles}
    variant = make_rebalancer(rig, shipped_settings).variant
    # Each filled role's best-ranked vehicle carries the role's static weight; gold's and broad
    # commodities' 16 % stays in cash rather than being spread over the other roles.
    world = weights[355720826]
    assert abs(world - variant.weights["world_equity"]) < Decimal("0.0001")
    left = variant.weights["gold"] + variant.weights["broad_commodities"]
    assert sum(weights.values(), Decimal(0)) == pytest.approx(1 - left, abs=Decimal("0.0001"))
