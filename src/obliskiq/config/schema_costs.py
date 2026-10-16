"""The shape of the cost configuration: commission schedules, transaction taxes and the friction
assumptions.

Money and rates are ``Decimal`` and must be written as quoted strings (or integers): a bare YAML
``0.005`` is a float, and a float cannot hold a price exactly. A list is replaced as a whole by an
owner override, so a record's rows are checked here, not merged.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from ..domain.base import CountryCode, CurrencyCode, NonEmptyText
from ..domain.calendar import Mic
from ..domain.enums import AssetClass
from .base import FrozenModel
from .schema_calendars import Provenance


def _not_a_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError(
            f'is the float {value}: write money and rates as quoted strings, such as "{value}" '
            "(a float cannot hold a price exactly)"
        )
    return value


type Amount = Annotated[Decimal, BeforeValidator(_not_a_float), Field(ge=0)]
type Fraction = Annotated[Decimal, BeforeValidator(_not_a_float), Field(ge=0, lt=1)]
type Rate = Annotated[Decimal, BeforeValidator(_not_a_float), Field(gt=0, lt=1)]


class CommissionSchedule(FrozenModel):
    """One commission schedule, chosen by (venue, asset class, currency).

    ``per_share``: ``rate`` per share, ``minimum`` per order, ``maximum_fraction_of_value`` of the
    trade value. ``per_contract``: ``rate`` per contract and ``minimum`` per order.
    ``fraction_of_value``: ``rate`` as a fraction of the trade value and ``minimum`` per order.
    ``fraction_of_face``: ``rate`` as a fraction of face value, ``minimum`` per order, defined only
    up to ``up_to_face``. A ``minimum`` of ``None`` means no minimum is established.
    """

    id: NonEmptyText
    venues: list[Mic] | None
    asset_classes: list[AssetClass] = Field(min_length=1)
    currency: CurrencyCode
    kind: Literal["per_share", "per_contract", "fraction_of_value", "fraction_of_face"]
    rate: Annotated[Decimal, BeforeValidator(_not_a_float), Field(gt=0)]
    minimum: Amount | None
    maximum_fraction_of_value: Rate | None
    up_to_face: Amount | None
    scope_note: NonEmptyText
    source: Provenance

    @model_validator(mode="after")
    def _fields_fit_the_kind(self) -> Self:
        if self.maximum_fraction_of_value is not None and self.kind != "per_share":
            raise ValueError("maximum_fraction_of_value applies to per_share schedules only")
        if self.up_to_face is not None and self.kind != "fraction_of_face":
            raise ValueError("up_to_face applies to fraction_of_face schedules only")
        if self.kind in ("fraction_of_value", "fraction_of_face") and self.rate >= 1:
            raise ValueError(f"a {self.kind} rate is a fraction below 1")
        if self.venues is not None and not self.venues:
            raise ValueError("venues is null (any venue) or a non-empty list")
        return self


class TaxRule(FrozenModel):
    """A transaction tax on an issuer's securities. ``rate`` ``None`` means it is not known."""

    id: NonEmptyText
    issuer_country: CountryCode
    asset_classes: list[AssetClass] = Field(min_length=1)
    sides: list[Literal["buy", "sell"]] = Field(min_length=1)
    rate: Rate | None
    effective_from: date | None
    needs_issuer_list: bool
    provision: NonEmptyText
    confirmation: Literal["official", "secondary", "none"]
    note: NonEmptyText
    source: Provenance

    @model_validator(mode="after")
    def _a_confirmed_rate_exists(self) -> Self:
        if self.rate is None and self.confirmation != "none":
            raise ValueError("a rate that is not entered cannot be marked confirmed")
        if self.rate is not None and self.confirmation == "none":
            raise ValueError("a rate that was not confirmed against any source is not entered")
        return self


class EquityFriction(FrozenModel):
    liquid_market_cap_usd: Amount
    liquid_spread_fraction: Fraction
    illiquid_spread_fraction: Fraction
    liquid_slippage_fraction: Fraction
    illiquid_slippage_fraction: Fraction
    impact_coefficient: Amount
    impact_cap_fraction: Fraction

    @model_validator(mode="after")
    def _illiquid_costs_at_least_liquid(self) -> Self:
        if (
            self.illiquid_spread_fraction < self.liquid_spread_fraction
            or self.illiquid_slippage_fraction < self.liquid_slippage_fraction
        ):
            raise ValueError("an illiquid tier cannot cost less than the liquid one")
        return self


class OptionFriction(FrozenModel):
    liquid_half_spread_fraction: Fraction
    single_name_half_spread_fraction: Fraction
    liquid_underlyings: list[NonEmptyText]

    @model_validator(mode="after")
    def _single_name_costs_at_least_liquid(self) -> Self:
        if self.single_name_half_spread_fraction < self.liquid_half_spread_fraction:
            raise ValueError("a single-name tier cannot cost less than the liquid one")
        return self


class FrictionConfig(FrozenModel):
    source: Provenance
    equity: EquityFriction
    option: OptionFriction


class CostsConfig(FrozenModel):
    commissions: list[CommissionSchedule]
    taxes: list[TaxRule]
    friction: FrictionConfig

    @model_validator(mode="after")
    def _identifiers_are_unique(self) -> Self:
        for label, ids in (
            ("commissions", [c.id for c in self.commissions]),
            ("taxes", [t.id for t in self.taxes]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} ids must be unique")
        return self
