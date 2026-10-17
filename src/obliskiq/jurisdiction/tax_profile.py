"""The tax data a jurisdiction profile carries (``config/jurisdictions/<id>.yaml: tax``).

The values of the rules live here, each with the source it was read from; the rules themselves
are the :class:`~obliskiq.jurisdiction.tax.TaxPolicy` the profile selects. Money and rates are
quoted strings, as in ``costs.yaml``. The articles behind each value are cited in
``docs/knowledge/tax-<id>.md`` (not tax advice).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from ..config.base import FrozenModel
from ..config.schema_costs import Amount, Fraction, Rate
from ..domain.base import CountryCode, NonEmptyText


class RateBracket(FrozenModel):
    """One step of a progressive scale: ``rate`` on the base up to ``up_to`` (``None``: above)."""

    up_to: Amount | None
    rate: Rate


class RateTable(FrozenModel):
    """A progressive rate scale, the tax years it is known to apply to and where it was read."""

    first_tax_year: int
    verified_through_tax_year: int
    verified_on: date
    source: NonEmptyText
    brackets: list[RateBracket] = Field(min_length=1)

    @model_validator(mode="after")
    def _brackets_ascend_and_end_open(self) -> Self:
        if self.verified_through_tax_year < self.first_tax_year:
            raise ValueError("verified_through_tax_year cannot precede first_tax_year")
        *bounded, last = self.brackets
        if last.up_to is not None:
            raise ValueError("the last bracket has up_to: null (it taxes everything above)")
        limits = [bracket.up_to for bracket in bounded if bracket.up_to is not None]
        if len(limits) != len(bounded):
            raise ValueError("only the last bracket may have up_to: null")
        if any(lower >= upper for lower, upper in pairwise(limits)):
            raise ValueError("bracket limits must strictly ascend")
        return self

    def covers(self, tax_year: int) -> bool:
        return self.first_tax_year <= tax_year <= self.verified_through_tax_year


class PaymentRule(FrozenModel):
    """When a tax year's tax is paid: ``month``/``day`` of the year ``years_after`` it."""

    month: int
    day: int
    years_after: PositiveInt
    source: NonEmptyText

    @model_validator(mode="after")
    def _the_day_exists_every_year(self) -> Self:
        try:
            # Year 1 is not a leap year: a day that exists in it exists in every year.
            date(date.min.year, self.month, self.day)
        except ValueError:
            raise ValueError(f"{self.month}-{self.day} is not a date in every year") from None
        return self


class WithholdingRow(FrozenModel):
    """The rate a source country withholds on income paid to a resident of this jurisdiction."""

    country: CountryCode
    rate: Fraction
    confirmation: Literal["official", "secondary"]
    source: NonEmptyText


class WashSaleRule(FrozenModel):
    """Months before and after a loss sale in which a homogeneous purchase defers the loss."""

    listed_months: PositiveInt
    unlisted_months: PositiveInt


class LossOffsetRule(FrozenModel):
    """How negative balances offset: across the income and gains components (a fraction of the
    other's positive balance) and in later years."""

    carry_forward_years: PositiveInt
    cross_offset_fraction: Fraction
    source: NonEmptyText


class ForeignTaxCreditRule(FrozenModel):
    """The credit for tax withheld abroad: at most the tax at the average effective rate on the
    foreign income, the rate kept to ``average_rate_percent_decimals`` decimals of a percent."""

    average_rate_percent_decimals: Annotated[int, Field(ge=0)]
    source: NonEmptyText


class SwapPartnerRule(FrozenModel):
    """What a swap partner must differ in from the security it replaces."""

    differ_in_issuer: bool
    differ_in_index: bool


class TaxProfile(FrozenModel):
    """A jurisdiction's tax data. Every field is required: a rule not stated is not assumed."""

    lot_method: Literal["fifo"]
    homogeneity: Literal["same_isin_or_issuer_and_index"]
    wash_sale: WashSaleRule
    loss_offset: LossOffsetRule
    savings_rate_table: RateTable
    payment: PaymentRule
    foreign_tax_credit: ForeignTaxCreditRule
    withholding_at_source: list[WithholdingRow]
    fund_switch_deferral_at_broker: bool
    swap_partner: SwapPartnerRule

    @model_validator(mode="after")
    def _one_withholding_row_per_country(self) -> Self:
        countries = [row.country for row in self.withholding_at_source]
        if len(countries) != len(set(countries)):
            raise ValueError("withholding_at_source has one row per country")
        return self
