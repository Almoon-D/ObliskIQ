"""The core engine over the order rig: a variant of the three funds the fake broker serves, a
vehicle source that pins them, and the rebalancer wired as ``orchestration/assemble.py`` wires it.

The rig's variant ``rig`` holds three roles of variant A, weighted as A weights them
(``research/preregistrations/core-allocation.md``: WORLD 9 %, EM 7 %, EGB_INT 34 %) scaled to fill
the book. It does not tilt, so it reads no month-end closes.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Collection, Iterable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_core import BandedWeightsVariant
from obliskiq.decision.core_vehicles import RoleChoice, VehicleChoice
from obliskiq.decision.vehicle_selector import RankedVehicle
from obliskiq.domain.enums import AssetClass

#: The rig's three funds (config/universe.yaml): iShares Core MSCI World on the LSE in dollars, Core
#: MSCI EM IMI and the euro government 7-10 year fund on Euronext Amsterdam in euros.
IWDA, EMIM, IBGM = 78999785, 153454120, 43614383
#: The rig's roles, each filled by one of them.
RIG_PINS = {
    "world_equity": (IWDA, None),
    "em_equity": (EMIM, None),
    "euro_govt_intermediate": (IBGM, None),
}
#: The rig's variant: the weights of its roles.
RIG_ROLES = {
    "world_equity": Decimal("0.18"),
    "em_equity": Decimal("0.14"),
    "euro_govt_intermediate": Decimal("0.68"),
}


def rig_settings(settings: ObliskConfig) -> ObliskConfig:
    """``settings`` with the variant ``rig`` registered, and chosen unless a test chose another
    (the shipped choice is A, which the rig's three funds cannot fill)."""
    if any(v.name == "rig" for v in settings.core.variants):
        return settings
    variant = BandedWeightsVariant(name="rig", kind="banded_weights", weights=RIG_ROLES)
    chosen = "rig" if settings.core.variant == "A" else settings.core.variant
    core = settings.core.model_copy(
        update={"variant": chosen, "variants": [*settings.core.variants, variant]}
    )
    return settings.model_copy(update={"core": core})


def vehicle(conid: int, symbol: str = "X", venue: str = "XAMS", currency: str = "EUR") -> Any:
    return RankedVehicle(
        symbol=symbol,
        conid=conid,
        venue=venue,
        currency=currency,
        asset_class=AssetClass.ETF,
        hedged=False,
        score=Decimal(1),
        scores=(),
        all_in_cost=None,
        passed=(),
    )


class PinnedVehicles:
    """Fills each role with the contract it is pinned to (and a swap partner when pinned);
    a role pinned to nothing is unfilled."""

    def __init__(
        self, pins: Mapping[str, tuple[int, int | None]], *, why: str = "no vehicle pinned"
    ) -> None:
        self._pins = dict(pins)
        self._why = why
        self.asked: list[tuple[list[str], set[int], bool]] = []

    async def choose(
        self, roles: Iterable[str], *, held: Collection[int], aware: bool, today: date
    ) -> VehicleChoice:
        del today
        asked = list(dict.fromkeys(roles))
        self.asked.append((asked, set(held), aware))
        chosen: dict[str, RoleChoice] = {}
        for role in asked:
            pin = self._pins.get(role)
            if pin is None:
                chosen[role] = RoleChoice(role, None, None, self._why)
                continue
            primary, partner = pin
            chosen[role] = RoleChoice(
                role,
                vehicle(primary, f"P{primary}"),
                vehicle(partner, f"P{partner}") if aware and partner is not None else None,
                "",
            )
        return VehicleChoice(chosen)
