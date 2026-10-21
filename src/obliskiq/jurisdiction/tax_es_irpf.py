"""The ``es_irpf`` tax policy: Spain's personal income tax on the savings base.

The rules, each cited in ``docs/knowledge/tax-es.md`` with an official source (not tax advice):

- **FIFO** for homogeneous securities (LIRPF art. 37.2; art. 94.1.a for collective investments).
- **Homogeneity** (RIRPF art. 8), applied conservatively: the same ISIN, or the same issuer and
  the same tracked index. Two share lines of one company are homogeneous; two ETFs of one issuer
  on one index (an accumulating and a distributing class) are homogeneous.
- **The two-month rule** (LIRPF art. 33.5.f): a loss on listed securities is not computed while
  homogeneous securities bought within the window before or after the sale are held; it is
  integrated as they are sold. Unlisted securities have the longer window of art. 33.5.g.
- **Offsets and carry-forward** (art. 49): the income and gains components offset each other up
  to a fraction of the positive one, and what remains negative carries forward.
- **The savings scale** (arts. 66 and 76) and **the payment date** (the yearly order approving
  the return) come from the profile, as does **withholding at source** and the **foreign-tax
  credit** (art. 80).
- **Switches**: an ETF switch is a sale (art. 94.1.a excludes listed funds from the deferral);
  a switch between unlisted funds is deferred only when the profile says the broker supports it.
- **Swap partners** differ from the security they replace as the profile's swap rule requires,
  and are never homogeneous with it.
- **Currency balances** (LIRPF arts. 14.2.e, 33.1 and 46.b): a foreign currency is a holding
  whose gain or loss arises when it is paid out, kept as lots when the profile says so; its loss
  is deferred while the currency is bought back within the window after the disposal
  (art. 33.5.e, the profile's conservative reading). The two-month rule of art. 33.5.f covers
  listed securities only, so it does not apply to a currency.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Final

from ..domain.tax import COLLECTIVE_KINDS, SecurityIdentity, SecurityKind
from .tax import ProfileTaxRules, WashSaleWindow
from .tax_profile import TaxProfile

ES_IRPF_POLICY_ID: Final = "es_irpf"


class EsIrpfTaxPolicy(ProfileTaxRules):
    """Spain's IRPF rules on the savings base, with the profile's values."""

    def __init__(self, tax: TaxProfile) -> None:
        super().__init__(ES_IRPF_POLICY_ID, tax)
        self._wash_sale = tax.wash_sale
        self._fund_switch_at_broker = tax.fund_switch_deferral_at_broker
        self._swap = tax.swap_partner

    def homogeneous(self, first: SecurityIdentity, second: SecurityIdentity) -> bool:
        if first.key == second.key:
            return True
        if first.isin is not None and first.isin == second.isin:
            return True
        if first.kind is not second.kind and not (
            first.kind in COLLECTIVE_KINDS and second.kind in COLLECTIVE_KINDS
        ):
            return False
        return (
            first.issuer is not None
            and first.issuer == second.issuer
            and first.tracked_index == second.tracked_index
        )

    def wash_sale_window(self, security: SecurityIdentity) -> WashSaleWindow | None:
        if security.kind is SecurityKind.CURRENCY:
            return self._currency_window()
        rule = self._wash_sale
        months = rule.listed_months if security.listed else rule.unlisted_months
        return WashSaleWindow(before_months=months, after_months=months)

    def switch_deferral(self, sold: SecurityIdentity, bought: SecurityIdentity) -> bool:
        funds = sold.kind is SecurityKind.FUND and bought.kind is SecurityKind.FUND
        unlisted = not sold.listed and not bought.listed
        return funds and unlisted and self._fund_switch_at_broker

    def _own_rules(self) -> tuple[str, ...]:
        wash, swap = self._wash_sale, self._swap
        return (
            f"two-month rule: a loss is deferred by a homogeneous purchase within "
            f"{wash.listed_months} months (listed) or {wash.unlisted_months} months (unlisted) of "
            "the sale (LIRPF art. 33.5.f and g)",
            "ETF switches are sales; fund switches are deferred only if the broker supports "
            f"them (supported: {self._fund_switch_at_broker}; LIRPF art. 94.1.a)",
            f"swap partners differ in issuer: {swap.differ_in_issuer}, in tracked index: "
            f"{swap.differ_in_index}",
        )

    def swap_partner_allowed(self, held: SecurityIdentity, candidate: SecurityIdentity) -> bool:
        return not self.homogeneous(held, candidate) and self._differs_as_a_swap_partner(
            held, candidate
        )
