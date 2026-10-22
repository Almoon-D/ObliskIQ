"""TaxPolicy: how a jurisdiction taxes what the account realises (MASTER_PLAN §5.16).

A :class:`TaxPolicy` answers the questions the lot engine and the tax ledger (``obliskiq.tax``)
ask: which lots a sale consumes, whether two securities are homogeneous, whether a loss sale is
deferred by a nearby purchase, how negative balances offset and carry forward, the tax on a
savings base, when a year's tax is paid, what a source country withholds and how much of it is
credited, and whether a switch or a swap partner keeps the tax deferred.

The values come from the profile (``config/jurisdictions/<id>.yaml: tax``) through
:class:`ProfileTaxRules`, which every shipped policy builds on; a policy module holds only the
rules that differ. No rate, window or year is written in code.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal, Protocol

from ..domain.tax import SecurityIdentity
from ..errors import PolicyError
from .profile import JurisdictionProfile
from .tax_profile import TaxProfile

#: The policy kind a profile selects under ``policies``.
TAX_KIND: Final = "tax"

ZERO = Decimal(0)
#: A percent is a hundredth.
PERCENT = Decimal(100)

type LotMethod = Literal["fifo"]


@dataclass(frozen=True, slots=True)
class WashSaleWindow:
    """Months before and after a loss sale in which buying back a homogeneous security defers
    the loss. ``before_months`` zero: only a purchase after the sale defers it."""

    before_months: int
    after_months: int


class TaxPolicy(Protocol):
    """How one jurisdiction taxes realised gains, losses and income."""

    @property
    def policy_id(self) -> str: ...

    @property
    def lot_method(self) -> LotMethod: ...

    @property
    def carry_forward_years(self) -> int:
        """Years after the one it arose in during which a negative balance may be used."""
        ...

    @property
    def cross_offset_fraction(self) -> Decimal:
        """The share of one component's positive balance the other's negative may absorb."""
        ...

    def homogeneous(self, first: SecurityIdentity, second: SecurityIdentity) -> bool:
        """Whether a purchase of ``second`` counts as buying back ``first``."""
        ...

    def wash_sale_window(self, security: SecurityIdentity) -> WashSaleWindow | None:
        """The window around a loss sale of ``security``, or ``None`` with no such rule."""
        ...

    @property
    def currency_lots(self) -> bool:
        """Whether foreign-currency balances are kept as lots whose gains are taxed."""
        ...

    def savings_tax(self, base: Decimal) -> Decimal:
        """The tax on a non-negative savings base under the profile's rate table."""
        ...

    def rate_table_covers(self, tax_year: int) -> bool:
        """Whether the rate table was verified for ``tax_year``."""
        ...

    def rate_table_label(self) -> str: ...

    def payment_date(self, tax_year: int) -> date:
        """When the tax of ``tax_year`` is paid."""
        ...

    def payment_label(self) -> str: ...

    def withholding_rate(self, source_country: str) -> Decimal | None:
        """The rate ``source_country`` withholds; ``None`` when the profile does not state it."""
        ...

    def foreign_tax_credit(
        self, *, withheld: Decimal, foreign_income: Decimal, tax: Decimal, base: Decimal
    ) -> Decimal:
        """The credit for tax withheld abroad against ``tax`` on ``base``."""
        ...

    def switch_deferral(self, sold: SecurityIdentity, bought: SecurityIdentity) -> bool:
        """Whether selling ``sold`` to buy ``bought`` keeps the gain deferred."""
        ...

    @property
    def retained_income_deferred(self) -> bool:
        """Whether a fund's retained income (an accumulating class) is taxed only on sale, while
        a distribution is taxed when paid."""
        ...

    def swap_partner_allowed(self, held: SecurityIdentity, candidate: SecurityIdentity) -> bool:
        """Whether ``candidate`` may replace ``held`` without its purchase deferring a loss."""
        ...

    def describe(self) -> tuple[str, ...]:
        """The rules the policy applies, each with its source, for a report."""
        ...


def require_tax_profile(profile: JurisdictionProfile) -> TaxProfile:
    """The profile's tax data; a policy cannot be built without it."""
    if profile.tax is None:
        raise PolicyError(
            f"jurisdiction profile '{profile.id}' selects a tax policy but carries no tax "
            f"section; add tax: to jurisdictions/{profile.id}.yaml"
        )
    return profile.tax


class ProfileTaxRules:
    """The rules every shipped policy reads the same way from the profile's tax data: the
    progressive scale, the payment date, withholding, the foreign-tax credit and the offsets.

    A policy subclasses it and states the rules that are its own.
    """

    def __init__(self, policy_id: str, tax: TaxProfile) -> None:
        self._policy_id = policy_id
        self._tax = tax
        self._withholding: Mapping[str, Decimal] = {
            row.country: row.rate for row in tax.withholding_at_source
        }

    @property
    def policy_id(self) -> str:
        return self._policy_id

    @property
    def lot_method(self) -> LotMethod:
        return self._tax.lot_method

    @property
    def carry_forward_years(self) -> int:
        return self._tax.loss_offset.carry_forward_years

    @property
    def cross_offset_fraction(self) -> Decimal:
        return self._tax.loss_offset.cross_offset_fraction

    @property
    def currency_lots(self) -> bool:
        return self._tax.currency_gains.kept_as_lots

    @property
    def retained_income_deferred(self) -> bool:
        return self._tax.retained_income.deferred

    def _currency_window(self) -> WashSaleWindow | None:
        """The currency rule's window: a buy-back after a loss disposal only."""
        months = self._tax.currency_gains.loss_reacquisition_months
        return None if months is None else WashSaleWindow(before_months=0, after_months=months)

    def savings_tax(self, base: Decimal) -> Decimal:
        if not base.is_finite() or base < ZERO:
            raise ValueError(f"a savings base is finite and not negative, got {base}")
        tax = ZERO
        lower = ZERO
        for bracket in self._tax.savings_rate_table.brackets:
            upper = base if bracket.up_to is None else bracket.up_to
            if base > lower:
                tax += (min(base, upper) - lower) * bracket.rate
            lower = upper
        return tax

    def rate_table_covers(self, tax_year: int) -> bool:
        return self._tax.savings_rate_table.covers(tax_year)

    def rate_table_label(self) -> str:
        table = self._tax.savings_rate_table
        return (
            f"savings rate table for tax years {table.first_tax_year} to "
            f"{table.verified_through_tax_year}, verified {table.verified_on.isoformat()} "
            f"({table.source})"
        )

    def payment_date(self, tax_year: int) -> date:
        payment = self._tax.payment
        return date(tax_year + payment.years_after, payment.month, payment.day)

    def payment_label(self) -> str:
        payment = self._tax.payment
        return (
            f"paid on {payment.month:02d}-{payment.day:02d} of the year "
            f"{payment.years_after} after the tax year ({payment.source})"
        )

    def withholding_rate(self, source_country: str) -> Decimal | None:
        return self._withholding.get(source_country)

    def foreign_tax_credit(
        self, *, withheld: Decimal, foreign_income: Decimal, tax: Decimal, base: Decimal
    ) -> Decimal:
        if withheld <= ZERO or foreign_income <= ZERO or base <= ZERO or tax <= ZERO:
            return ZERO
        decimals = self._tax.foreign_tax_credit.average_rate_percent_decimals
        quantum = Decimal(1).scaleb(-decimals)
        average_percent = (tax / base * PERCENT).quantize(quantum)
        ceiling = min(foreign_income, base) * average_percent / PERCENT
        return min(withheld, ceiling, tax)

    def describe(self) -> tuple[str, ...]:
        tax = self._tax
        offset = tax.loss_offset
        shared = (
            f"policy {self._policy_id}; lots {tax.lot_method}; homogeneity {tax.homogeneity}",
            f"negative balances carried forward {offset.carry_forward_years} years, offsetting "
            f"up to {offset.cross_offset_fraction} of the other component ({offset.source})",
            self.rate_table_label(),
            f"tax {self.payment_label()}",
            f"foreign-tax credit at the average effective rate ({tax.foreign_tax_credit.source})",
            self._currency_rule(),
            f"a fund's retained income taxed only on sale: {tax.retained_income.deferred} "
            f"({tax.retained_income.source})",
            *(
                f"withholding at source {row.country} {row.rate} ({row.confirmation}: {row.source})"
                for row in tax.withholding_at_source
            ),
        )
        return shared + self._own_rules()

    def _own_rules(self) -> tuple[str, ...]:
        """The rules a policy states beyond the shared ones."""
        return ()

    def _currency_rule(self) -> str:
        currency = self._tax.currency_gains
        if not currency.kept_as_lots:
            return f"foreign-currency balances are not kept as lots ({currency.source})"
        months = currency.loss_reacquisition_months
        deferral = (
            "no buy-back rule"
            if months is None
            else f"a loss deferred while the currency is bought back within {months} months"
        )
        return (
            f"foreign-currency balances kept as {currency.lot_method} lots, a gain or loss on "
            f"each disposal; {deferral} ({currency.source})"
        )

    def _differs_as_a_swap_partner(
        self, held: SecurityIdentity, candidate: SecurityIdentity
    ) -> bool:
        rule = self._tax.swap_partner
        if rule.differ_in_issuer and (held.issuer is None or held.issuer == candidate.issuer):
            return False
        return not (
            rule.differ_in_index
            and (held.tracked_index is None or held.tracked_index == candidate.tracked_index)
        )
