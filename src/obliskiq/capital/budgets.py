"""The sleeve budgets (MASTER_PLAN §11 row 2) and the owner's satellite ceiling, behind one reader.

``capital.budgets`` gives each sleeve its share of investable capital: the core, the equity
satellite, the option premium, and a cash floor that stays cash. A sleeve with no active layer —
no sufficient research outcome and activation condition yet (D65, D66) — leaves its budget in the
core, Session 17's rule, so an inactive satellite never strands capital as idle cash. Session 18's
capital allocation moves the satellite's share, never above ``capital.satellite_ceiling``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

from ..config.schema_runtime import CapitalConfig
from ..domain.orders import Sleeve


@dataclass(frozen=True, slots=True)
class Allocation:
    """Each sleeve's budget and the cash floor, as amounts of investable capital (base currency)."""

    investable: Decimal
    core: Decimal
    satellite: Decimal
    options: Decimal
    cash_floor: Decimal

    def of(self, sleeve: Sleeve) -> Decimal:
        return {Sleeve.CORE: self.core, Sleeve.SATELLITE: self.satellite}.get(sleeve, self.options)


class SleeveBudgets:
    """The budgets ``capital.budgets`` sets and the ceiling the satellite's may move to."""

    def __init__(self, capital: CapitalConfig) -> None:
        self._budgets = capital.budgets
        self._ceiling = capital.satellite_ceiling

    @property
    def satellite_ceiling(self) -> Decimal:
        """The most the satellite's share may be moved to (Session 18)."""
        return self._ceiling

    def share(self, sleeve: Sleeve) -> Decimal:
        """The configured share of investable capital ``sleeve`` is budgeted."""
        return {
            Sleeve.CORE: self._budgets.core,
            Sleeve.SATELLITE: self._budgets.equity_satellite,
        }.get(sleeve, self._budgets.option_premium)

    def allocate(self, investable: Decimal, *, active: Collection[Sleeve]) -> Allocation:
        """The amounts of ``investable`` each sleeve may use. ``active`` names the sleeves whose
        layers trade; an inactive sleeve's share stays in the core. What no budget names stays
        cash, the cash floor at least."""
        if Sleeve.CORE not in active:
            raise ValueError("the core is always active: inactive budgets stay in it")
        core = self.share(Sleeve.CORE)
        satellite = options = Decimal(0)
        if Sleeve.SATELLITE in active:
            satellite = self.share(Sleeve.SATELLITE)
        else:
            core += self.share(Sleeve.SATELLITE)
        if Sleeve.OPTIONS in active:
            options = self.share(Sleeve.OPTIONS)
        else:
            core += self.share(Sleeve.OPTIONS)
        return Allocation(
            investable=investable,
            core=investable * core,
            satellite=investable * satellite,
            options=investable * options,
            cash_floor=investable * self._budgets.cash_floor,
        )
