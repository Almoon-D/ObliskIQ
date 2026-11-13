"""The sleeve budgets (MASTER_PLAN §11 row 2) and the owner's satellite ceiling, behind one reader.

``capital.budgets`` gives each sleeve its share of investable capital: the core, the equity
satellite, the option premium, and a cash floor that stays cash. A sleeve with no active layer —
no sufficient research outcome and activation condition yet (D65, D66) — leaves its budget in the
core, Session 17's rule, so an inactive satellite never strands capital as idle cash. Session 18's
capital allocation moves the satellite's share, never above ``capital.satellite_ceiling``.

**The thesis book** is the satellite's one layer that trades (MASTER_PLAN §5.5): in shadow and in
paper it takes ``thesis.capital_share`` of investable capital out of the satellite's budget, and
the rest of that budget stays in the core (:func:`book_allocation`, DECISIONS D159). On the live
account its share is the capital allocation rule's; until that rule exists it has none there.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

from ..config.schema import ObliskConfig
from ..config.schema_runtime import CapitalConfig
from ..domain.orders import ExecutionMode, Sleeve

ZERO = Decimal(0)

#: The modes in which the thesis book takes its configured share: no real money is at risk there.
_THESIS_MODES = frozenset({ExecutionMode.SHADOW, ExecutionMode.PAPER})


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

    def allocate(
        self,
        investable: Decimal,
        *,
        active: Collection[Sleeve],
        satellite_share: Decimal | None = None,
    ) -> Allocation:
        """The amounts of ``investable`` each sleeve may use. ``active`` names the sleeves whose
        layers trade; an inactive sleeve's share stays in the core. An active satellite whose
        layers use only ``satellite_share`` of its budget leaves the rest in the core too. What
        no budget names stays cash, the cash floor at least."""
        if Sleeve.CORE not in active:
            raise ValueError("the core is always active: inactive budgets stay in it")
        core = self.share(Sleeve.CORE)
        satellite = options = ZERO
        if Sleeve.SATELLITE in active:
            budget = self.share(Sleeve.SATELLITE)
            satellite = budget if satellite_share is None else satellite_share
            if satellite > budget:
                raise ValueError("a satellite layer takes at most the satellite's budget")
            core += budget - satellite
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


def thesis_share(settings: ObliskConfig, mode: ExecutionMode) -> Decimal:
    """The thesis book's share of investable capital in ``mode``: ``thesis.capital_share`` in
    shadow and in paper; nothing on the live account, where the capital allocation rule gives it
    one (Session 18)."""
    return settings.thesis.capital_share if mode in _THESIS_MODES else ZERO


def book_allocation(settings: ObliskConfig, investable: Decimal, mode: ExecutionMode) -> Allocation:
    """What each sleeve may use of ``investable`` in ``mode``: the core, and the satellite's one
    trading layer, the thesis book, when it has a share."""
    share = thesis_share(settings, mode)
    budgets = SleeveBudgets(settings.capital)
    if share > ZERO:
        return budgets.allocate(
            investable, active={Sleeve.CORE, Sleeve.SATELLITE}, satellite_share=share
        )
    return budgets.allocate(investable, active={Sleeve.CORE})
