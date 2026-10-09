"""Attacks on the cost model: every way a leg could be priced when it should not be.

The rule under test (``docs/DECISIONS.md`` D31, ``quant/costs.py``): an unknown cost is a status,
never a number. A leg that cannot be priced has no total and names its gap; money is ``Decimal``
end to end; a transaction tax is a conservative upper bound over every rule in force; commission
selection never falls through to a neighbouring schedule; malformed input is refused.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_calendars import Provenance
from obliskiq.config.schema_costs import CommissionSchedule, TaxRule
from obliskiq.domain.enums import AssetClass
from obliskiq.quant.costs import (
    CommissionEstimate,
    CostBook,
    CostGap,
    FrictionInputs,
    LegCost,
    OrderTerms,
    TaxEstimate,
    adjust_position_for_costs,
    breakeven_move,
)

D = Decimal
ZERO = D(0)
TODAY = date(2026, 10, 9)
SOURCE = Provenance(origin="test", verify_live=False)
#: A market capitalisation far above the liquid threshold.
LIQUID_CAP = D("3000000000000")
EQUITY_LIKE = frozenset({AssetClass.STOCK, AssetClass.ETF, AssetClass.ETC})
NOT_MODELLED = [AssetClass.BOND, AssetClass.FUND, AssetClass.INDEX, AssetClass.FUTURE]

# ── strategies ──

quantities = st.decimals(min_value=D("0.0001"), max_value=D("10000000"), places=4)
prices = st.decimals(min_value=D("0.0001"), max_value=D("100000"), places=4)
amounts = st.decimals(min_value=D(0), max_value=D("100000000"), places=2)
non_positive = st.decimals(min_value=D("-1E9"), max_value=D(0), places=4) | st.just(D("-0"))
non_finite = st.sampled_from([D("NaN"), D("-NaN"), D("sNaN"), D("Infinity"), D("-Infinity")])
whole_contracts = st.integers(min_value=1, max_value=10_000).map(Decimal)
trade_dates = st.dates(min_value=date(2019, 1, 1), max_value=date(2031, 12, 31))


# ── helpers ──


@pytest.fixture(scope="module")
def book(shipped_settings: ObliskConfig) -> CostBook:
    return CostBook.from_config(shipped_settings)


def shipped_row(settings: ObliskConfig, row_id: str) -> CommissionSchedule:
    return next(row for row in settings.costs.commissions if row.id == row_id)


def shares(
    quantity: str | Decimal = "100",
    price: str | Decimal = "100",
    venue: str = "XNAS",
    currency: str = "USD",
    asset_class: AssetClass = AssetClass.STOCK,
) -> OrderTerms:
    return OrderTerms(venue, asset_class, currency, D(quantity), D(price))


def contracts(quantity: str | Decimal = "10", premium: str | Decimal | None = "10") -> OrderTerms:
    return OrderTerms(
        "XNYS", AssetClass.OPTION, "USD", D(quantity), None if premium is None else D(premium)
    )


def face(amount: str | Decimal, currency: str = "EUR", price: str | None = None) -> OrderTerms:
    return OrderTerms(
        "XAMS", AssetClass.BOND, currency, D(amount), None if price is None else D(price)
    )


def schedule(**overrides: Any) -> CommissionSchedule:
    fields: dict[str, Any] = {
        "id": "test",
        "venues": ["XNAS"],
        "asset_classes": [AssetClass.STOCK],
        "currency": "USD",
        "kind": "per_share",
        "rate": D("0.005"),
        "minimum": D("1.00"),
        "maximum_fraction_of_value": D("0.01"),
        "up_to_face": None,
        "scope_note": "test schedule",
        "source": SOURCE,
    }
    fields.update(overrides)
    return CommissionSchedule(**fields)


def rule(**overrides: Any) -> TaxRule:
    fields: dict[str, Any] = {
        "id": "test_tax",
        "issuer_country": "GB",
        "asset_classes": [AssetClass.STOCK],
        "sides": ["buy"],
        "rate": D("0.005"),
        "effective_from": None,
        "needs_issuer_list": False,
        "provision": "a provision",
        "confirmation": "official",
        "note": "a caveat",
        "source": SOURCE,
    }
    fields.update(overrides)
    if fields["rate"] is None:
        fields["confirmation"] = "none"
    return TaxRule(**fields)


def book_of(
    settings: ObliskConfig,
    commissions: list[CommissionSchedule] | None = None,
    taxes: list[TaxRule] | None = None,
) -> CostBook:
    return CostBook(
        commissions=commissions or [],
        taxes=taxes or [],
        friction=FrictionInputs.from_config(settings),
    )


def leg(costs: CostBook, terms: OrderTerms, **kw: Any) -> LegCost:
    kw.setdefault("side", "buy")
    kw.setdefault("on", TODAY)
    return costs.leg_cost(terms, **kw)


def gb_tax(costs: CostBook, **kw: Any) -> TaxEstimate:
    fields: dict[str, Any] = {
        "issuer_country": "GB",
        "asset_class": AssetClass.STOCK,
        "side": "buy",
        "notional": D(10_000),
        "on": TODAY,
    }
    fields.update(kw)
    return costs.transaction_tax(**fields)


def refused_or_unpriced(call: Callable[[], LegCost]) -> bool:
    """Fail-closed either way: the leg is refused, or it is returned with no total."""
    try:
        cost = call()
    except ValueError:
        return True
    return cost.total is None


def in_force(
    tax_rule: TaxRule,
    *,
    asset_class: AssetClass,
    side: str,
    on: date,
    taxable_issuer: bool | None,
) -> bool:
    """Whether a GB rule covers a leg, written from the rule's definition (D31)."""
    return (
        tax_rule.issuer_country == "GB"
        and asset_class in tax_rule.asset_classes
        and side in tax_rule.sides
        and (tax_rule.effective_from is None or tax_rule.effective_from <= on)
        and not (tax_rule.needs_issuer_list and taxable_issuer is False)
    )


# ── order terms ──


@given(quantity=quantities, price=prices)
def test_positive_control_any_positive_finite_order_is_accepted(quantity, price):
    terms = OrderTerms("XNAS", AssetClass.STOCK, "USD", quantity, price)
    assert terms.value() == quantity * price


@given(bad=non_positive | non_finite, price=prices)
def test_a_non_positive_or_non_finite_quantity_is_refused(bad, price):
    with pytest.raises(ValueError, match="quantity must be positive"):
        OrderTerms("XNAS", AssetClass.STOCK, "USD", bad, price)


@given(quantity=quantities, bad=non_positive | non_finite)
def test_a_non_positive_or_non_finite_price_is_refused(quantity, bad):
    with pytest.raises(ValueError, match="price must be positive"):
        OrderTerms("XNAS", AssetClass.STOCK, "USD", quantity, bad)


@pytest.mark.parametrize("not_a_decimal", [10.0, 10, True])
def test_money_that_is_not_a_decimal_is_never_accepted(not_a_decimal):
    with pytest.raises((AttributeError, TypeError, ValueError)):
        OrderTerms("XNAS", AssetClass.STOCK, "USD", not_a_decimal, D(1))
    with pytest.raises((AttributeError, TypeError, ValueError)):
        OrderTerms("XNAS", AssetClass.STOCK, "USD", D(1), not_a_decimal)


# ── commission: which schedule ──

VENUES = ["XNAS", "XNYS", "XEUR", "XLON"]
CURRENCIES = ["USD", "EUR", "GBP"]
schedule_keys = st.tuples(
    st.none() | st.lists(st.sampled_from(VENUES), min_size=1, max_size=3, unique=True),
    st.lists(st.sampled_from(list(AssetClass)), min_size=1, max_size=3, unique=True),
    st.sampled_from(CURRENCIES),
)


@given(
    keys=st.lists(schedule_keys, max_size=4),
    venue=st.sampled_from([*VENUES, "XHKG"]),
    asset_class=st.sampled_from(list(AssetClass)),
    currency=st.sampled_from([*CURRENCIES, "JPY"]),
)
def test_a_schedule_is_chosen_by_its_exact_key_or_not_at_all(
    shipped_settings, keys, venue, asset_class, currency
):
    rows = [
        schedule(
            id=f"row{i}",
            venues=venues,
            asset_classes=classes,
            currency=row_currency,
            kind="per_contract",
            rate=D(1),
            minimum=None,
            maximum_fraction_of_value=None,
        )
        for i, (venues, classes, row_currency) in enumerate(keys)
    ]
    costs = book_of(shipped_settings, commissions=rows)
    terms = OrderTerms(venue, asset_class, currency, D(3), D(1))
    expected = [
        row.id
        for row in rows
        if (row.venues is None or venue in row.venues)
        and asset_class in row.asset_classes
        and row.currency == currency
    ]
    if len(expected) > 1:
        with pytest.raises(ValueError, match="several commission schedules match"):
            costs.commission(terms)
        return
    estimate = costs.commission(terms)
    if not expected:
        assert (estimate.amount, estimate.gap, estimate.schedule_id) == (
            None,
            CostGap.NO_SCHEDULE,
            None,
        )
    else:
        assert (estimate.amount, estimate.gap, estimate.schedule_id) == (D(3), None, expected[0])


@pytest.mark.parametrize(
    "terms",
    [
        shares(venue="xnas"),  # the venue in lower case
        shares(currency="usd"),  # the currency in lower case
        shares(asset_class=AssetClass.ETC),  # equity-like, but the US row lists stock and etf
        OrderTerms("XNYS", AssetClass.OPTION, "EUR", D(1), D(1)),  # a US option venue in euros
        OrderTerms("XEUR", AssetClass.OPTION, "USD", D(1), D(1)),  # Eurex in dollars
        shares(venue="XEUR", currency="EUR"),  # a share on Eurex is not a Eurex option
        OrderTerms("XCBO", AssetClass.OPTION, "USD", D(1), D(1)),  # an unmapped option venue
        shares(asset_class=AssetClass.FUTURE),
        shares(asset_class=AssetClass.INDEX),
        face("10000", currency="GBP"),  # bond rows take any venue, but none is in sterling
    ],
)
def test_selection_never_falls_through_to_a_neighbouring_schedule(book, terms):
    estimate = book.commission(terms)
    assert estimate.amount is None
    assert estimate.gap is CostGap.NO_SCHEDULE
    assert estimate.schedule_id is None


def test_a_wildcard_row_and_a_venue_row_on_one_key_refuse_rather_than_pick(shipped_settings):
    overlapping = book_of(
        shipped_settings, [schedule(id="anywhere", venues=None), schedule(id="nasdaq")]
    )
    with pytest.raises(ValueError, match="anywhere, nasdaq"):
        overlapping.commission(shares())
    apart = book_of(
        shipped_settings,
        [schedule(id="anywhere", venues=None, currency="EUR"), schedule(id="nasdaq")],
    )
    assert apart.commission(shares()).schedule_id == "nasdaq"


def test_the_book_prices_from_the_settings_it_is_given(shipped_settings):
    costs = shipped_settings.costs
    doubled = [
        row.model_copy(update={"rate": row.rate * 2}) if row.id == "us_stock" else row
        for row in costs.commissions
    ]
    settings = shipped_settings.model_copy(
        update={"costs": costs.model_copy(update={"commissions": doubled})}
    )
    shipped = CostBook.from_config(shipped_settings).commission(shares("1000", "50")).amount
    assert shipped == D("5.000")
    assert CostBook.from_config(settings).commission(shares("1000", "50")).amount == D("10.000")


def test_an_override_that_empties_the_lists_prices_no_leg(shipped_settings):
    empty = shipped_settings.model_copy(
        update={"costs": shipped_settings.costs.model_copy(update={"commissions": [], "taxes": []})}
    )
    cost = leg(
        CostBook.from_config(empty), shares(), market_cap_usd=LIQUID_CAP, issuer_country="GB"
    )
    assert cost.commission.gap is CostGap.NO_SCHEDULE
    assert cost.total is None
    assert adjust_position_for_costs(D(10_000), cost) is None


# ── commission: per share ──


@given(quantity=quantities, price=prices)
def test_a_per_share_fee_is_the_rate_floored_by_the_minimum_and_capped_by_value(
    book, shipped_settings, quantity, price
):
    row = shipped_row(shipped_settings, "us_stock")
    amount = book.commission(shares(quantity, price)).amount
    cap = quantity * price * row.maximum_fraction_of_value
    assert type(amount) is Decimal
    assert ZERO < amount <= cap
    assert amount == min(max(quantity * row.rate, row.minimum), cap)


@pytest.mark.parametrize(
    ("quantity", "price", "expected"),
    [
        ("200", "100", "1.000"),  # the rate lands exactly on the 1.00 minimum
        ("199", "100", "1.00"),  # one share short of it: the minimum
        ("201", "100", "1.005"),  # one share past it: the rate
        ("1000", "0.5", "5.000"),  # the rate lands exactly on the 1 % cap of 500
        ("1000", "0.4999", "4.999"),  # one tick of price lower: the cap
        ("1", "0.0001", "0.000001"),  # the smallest order: the cap undercuts the minimum
    ],
)
def test_the_per_share_boundaries_on_both_edges(book, quantity, price, expected):
    estimate = book.commission(shares(quantity, price))
    assert estimate.gap is None
    assert estimate.amount == D(expected)


def test_a_zero_value_cap_is_refused_rather_than_pricing_commission_at_zero():
    with pytest.raises(ValidationError):
        schedule(maximum_fraction_of_value=D(0))


# ── commission: per contract ──


@given(count=whole_contracts, premium=prices)
def test_an_option_fee_is_per_contract_with_its_minimum_and_ignores_the_premium(
    book, shipped_settings, count, premium
):
    row = shipped_row(shipped_settings, "us_option")
    priced = book.commission(contracts(count, premium)).amount
    unpriced = book.commission(contracts(count, None)).amount
    assert priced == unpriced == max(count * row.rate, row.minimum)


@given(count=whole_contracts, more=whole_contracts)
def test_more_contracts_never_cost_less(book, count, more):
    fewer = book.commission(contracts(count)).amount
    assert book.commission(contracts(count + more)).amount >= fewer


@pytest.mark.parametrize(("count", "expected"), [("1", "1.00"), ("2", "1.30")])
def test_the_option_minimum_on_both_sides_of_one_contract(book, count, expected):
    assert book.commission(contracts(count)).amount == D(expected)


# ── commission: fraction of face ──


@pytest.mark.parametrize("amount", ["8500", "8500.00", "8.5E+3"])
def test_the_eur_bond_tier_covers_its_boundary_however_it_is_written(book, amount):
    estimate = book.commission(face(amount))
    assert estimate.gap is None
    assert estimate.amount == D("8.5")


@pytest.mark.parametrize("amount", ["8500.01", "8501", "8500.00000000000000000001"])
def test_one_unit_past_the_eur_bond_tier_is_beyond_it_never_extrapolated(book, amount):
    estimate = book.commission(face(amount))
    assert (estimate.amount, estimate.gap, estimate.schedule_id) == (
        None,
        CostGap.BEYOND_TIER,
        "eur_bond",
    )


@given(amount=st.decimals(min_value=D("0.01"), max_value=D("8500"), places=2))
def test_inside_the_eur_bond_tier_the_fee_is_the_rate_floored_by_the_minimum(
    book, shipped_settings, amount
):
    row = shipped_row(shipped_settings, "eur_bond")
    assert book.commission(face(amount)).amount == max(amount * row.rate, row.minimum)


@given(amount=st.decimals(min_value=D("8500.01"), max_value=D("1E12"), places=2))
def test_past_the_eur_bond_tier_no_fee_is_ever_given(book, amount):
    estimate = book.commission(face(amount))
    assert estimate.amount is None
    assert estimate.gap is CostGap.BEYOND_TIER


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("1000", "EUR", "1.000"),  # 0.1 % lands exactly on the EUR 1 minimum
        ("999.99", "EUR", "1.00"),
        ("1000.01", "EUR", "1.00001"),
        ("250000", "USD", "5.00000"),  # 0.002 % lands exactly on the USD 5 minimum
        ("249999", "USD", "5.00"),
        ("1000000000", "USD", "20000.00000"),  # the Treasury row has no upper tier
    ],
)
def test_the_face_minimum_on_both_edges(book, amount, currency, expected):
    estimate = book.commission(face(amount, currency))
    assert estimate.gap is None
    assert estimate.amount == D(expected)


# ── transaction tax ──

rule_specs = st.fixed_dictionaries(
    {
        "rate": st.none() | st.decimals(min_value=D("0.0001"), max_value=D("0.05"), places=4),
        "sides": st.lists(st.sampled_from(["buy", "sell"]), min_size=1, max_size=2, unique=True),
        "asset_classes": st.lists(
            st.sampled_from([AssetClass.STOCK, AssetClass.ETF]), min_size=1, max_size=2, unique=True
        ),
        "effective_from": st.none() | trade_dates,
        "needs_issuer_list": st.booleans(),
        "issuer_country": st.sampled_from(["GB", "FR"]),
    }
)


def build_rules(specs: list[dict[str, Any]]) -> list[TaxRule]:
    return [rule(id=f"r{i}", **spec) for i, spec in enumerate(specs)]


@given(
    specs=st.lists(rule_specs, max_size=5),
    asset_class=st.sampled_from([AssetClass.STOCK, AssetClass.ETF]),
    side=st.sampled_from(["buy", "sell"]),
    on=trade_dates,
    taxable_issuer=st.none() | st.booleans(),
    notional=st.decimals(min_value=D("0.01"), max_value=D("1E9"), places=2),
)
def test_a_tax_is_an_upper_bound_over_every_rule_in_force(
    shipped_settings, specs, asset_class, side, on, taxable_issuer, notional
):
    rules = build_rules(specs)
    estimate = book_of(shipped_settings, taxes=rules).transaction_tax(
        issuer_country="GB",
        asset_class=asset_class,
        side=side,
        notional=notional,
        on=on,
        taxable_issuer=taxable_issuer,
    )
    live = [
        r
        for r in rules
        if in_force(r, asset_class=asset_class, side=side, on=on, taxable_issuer=taxable_issuer)
    ]
    if not live:
        assert (estimate.applies, estimate.gap, estimate.rule_id) == (False, None, None)
        return
    assert estimate.applies
    if any(r.rate is None for r in live):
        assert estimate.gap is CostGap.TAX_UNKNOWN
        return
    assert estimate.gap is None
    assert type(estimate.amount) is Decimal
    for r in live:
        assert estimate.amount >= notional * r.rate
    highest = max(r.rate for r in live)
    assert estimate.amount == notional * highest
    chosen = next(r for r in live if r.id == estimate.rule_id)
    assert chosen.rate == highest
    assert estimate.scope_assumed == (chosen.needs_issuer_list and taxable_issuer is None)


@given(
    specs=st.lists(rule_specs, max_size=5),
    first=trade_dates,
    second=trade_dates,
    taxable_issuer=st.none() | st.booleans(),
)
def test_a_later_trade_date_never_lowers_the_tax(
    shipped_settings, specs, first, second, taxable_issuer
):
    costs = book_of(shipped_settings, taxes=build_rules(specs))
    early, late = sorted((first, second))
    before = gb_tax(costs, on=early, taxable_issuer=taxable_issuer)
    after = gb_tax(costs, on=late, taxable_issuer=taxable_issuer)
    if before.applies:
        assert after.applies
    if before.gap is CostGap.TAX_UNKNOWN:
        assert after.gap is CostGap.TAX_UNKNOWN
    if before.gap is None and after.gap is None:
        assert after.amount >= before.amount


@given(specs=st.lists(rule_specs, max_size=5), on=trade_dates)
def test_not_saying_whether_an_issuer_is_listed_costs_as_much_as_saying_it_is(
    shipped_settings, specs, on
):
    costs = book_of(shipped_settings, taxes=build_rules(specs))
    unsaid = gb_tax(costs, on=on, taxable_issuer=None)
    listed = gb_tax(costs, on=on, taxable_issuer=True)
    unlisted = gb_tax(costs, on=on, taxable_issuer=False)
    assert (unsaid.amount, unsaid.gap, unsaid.rule_id) == (
        listed.amount,
        listed.gap,
        listed.rule_id,
    )
    assert not listed.scope_assumed
    assert not unlisted.scope_assumed
    if listed.gap is None:
        assert unlisted.gap is None
        assert unlisted.amount <= listed.amount


def test_saying_an_issuer_is_off_the_list_drops_only_the_list_rule(shipped_settings):
    costs = book_of(
        shipped_settings,
        taxes=[
            rule(id="listed", rate=D("0.004"), needs_issuer_list=True),
            rule(id="flat", rate=D("0.001")),
        ],
    )
    unsaid = gb_tax(costs)
    assert (unsaid.rule_id, unsaid.amount, unsaid.scope_assumed) == ("listed", D("40.000"), True)
    listed = gb_tax(costs, taxable_issuer=True)
    assert (listed.rule_id, listed.amount, listed.scope_assumed) == ("listed", D("40.000"), False)
    unlisted = gb_tax(costs, taxable_issuer=False)
    assert (unlisted.rule_id, unlisted.amount, unlisted.applies) == ("flat", D("10.000"), True)


def test_an_unknown_rate_beats_any_known_one_until_its_rule_is_out_of_scope(shipped_settings):
    known = rule(id="known", rate=D("0.01"))
    unknown_listed = rule(id="unknown", rate=None, needs_issuer_list=True)
    costs = book_of(shipped_settings, taxes=[known, unknown_listed])
    assert gb_tax(costs).gap is CostGap.TAX_UNKNOWN
    assert gb_tax(costs, taxable_issuer=True).gap is CostGap.TAX_UNKNOWN
    off_the_list = gb_tax(costs, taxable_issuer=False)
    assert (off_the_list.gap, off_the_list.rule_id, off_the_list.amount) == (
        None,
        "known",
        D("100.00"),
    )


@pytest.mark.parametrize(
    ("on", "applies"),
    [(date(2028, 2, 28), False), (date(2028, 2, 29), True), (date(2028, 3, 1), True)],
)
def test_a_rule_starts_on_its_effective_date_across_a_leap_day(shipped_settings, on, applies):
    costs = book_of(shipped_settings, taxes=[rule(effective_from=date(2028, 2, 29))])
    assert gb_tax(costs, on=on).applies is applies


def test_positive_control_a_fully_known_leg_is_priced_to_the_exact_figure(book):
    # A UK issuer's share on a US venue: the per-share row, the liquid tiers and UK SDRT, which
    # follows the issuer. Spread 10 + slippage 10 + commission 1.00 + tax 50.
    cost = leg(book, shares(), market_cap_usd=LIQUID_CAP, issuer_country="GB")
    assert cost.gaps == ()
    assert cost.tax.rule_id == "gb_stamp_duty_reserve_tax"
    assert cost.total == D("71.000")


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("taxable_issuer", [None, True])
def test_an_italian_issuer_alone_is_enough_to_leave_a_leg_unpriced(book, side, taxable_issuer):
    cost = leg(
        book,
        shares(),
        side=side,
        market_cap_usd=LIQUID_CAP,
        issuer_country="IT",
        taxable_issuer=taxable_issuer,
    )
    assert cost.gaps == (CostGap.TAX_UNKNOWN,)
    assert None not in (cost.spread_cost, cost.slippage_cost, cost.commission.amount)
    assert cost.total is None
    assert adjust_position_for_costs(D(10_000), cost) is None
    assert breakeven_move(D(100), cost) is None


def test_a_share_without_an_issuer_country_has_no_total(book):
    cost = leg(book, shares(), market_cap_usd=LIQUID_CAP)
    assert cost.gaps == (CostGap.TAX_UNKNOWN,)
    assert cost.tax.amount is None
    assert cost.total is None


def test_a_non_share_without_an_issuer_country_is_labelled_not_modelled(book):
    ten = OrderTerms("XNYS", AssetClass.OPTION, "USD", D(10), D("10"))
    cost = leg(book, ten, multiplier=D(100), underlying_symbol="SPY")
    assert cost.tax.applies is False
    assert "tax_not_modelled" in cost.unverified


@pytest.mark.parametrize("country", ["gb", "GBR", "", " GB"])
def test_a_malformed_issuer_country_refuses_rather_than_dropping_the_tax(book, country):
    assert refused_or_unpriced(
        lambda: leg(book, shares(), market_cap_usd=LIQUID_CAP, issuer_country=country)
    )


@pytest.mark.parametrize("side", ["BUY", "Buy", "", "short"])
def test_a_malformed_side_refuses_rather_than_dropping_the_tax(book, side):
    assert refused_or_unpriced(
        lambda: leg(book, shares(), side=side, market_cap_usd=LIQUID_CAP, issuer_country="GB")
    )


def test_a_tax_whose_rate_is_unknown_carries_no_amount(book):
    italy = book.transaction_tax(
        issuer_country="IT",
        asset_class=AssetClass.STOCK,
        side="buy",
        notional=D(10_000),
        on=TODAY,
    )
    assert italy.gap is CostGap.TAX_UNKNOWN
    assert italy.amount is None


@pytest.mark.parametrize("notional", ["-10000", "NaN", "Infinity"])
def test_a_tax_on_a_negative_or_non_finite_notional_is_refused(book, notional):
    try:
        estimate = gb_tax(book, notional=D(notional))
    except ValueError:
        return
    assert estimate.gap is not None


# ── one leg ──

leg_inputs = st.fixed_dictionaries(
    {
        "venue": st.sampled_from(["XNAS", "XNYS", "XEUR", "XLON", "XETR"]),
        "asset_class": st.sampled_from(list(AssetClass)),
        "currency": st.sampled_from(["USD", "EUR", "GBP"]),
        "quantity": st.integers(min_value=1, max_value=20_000).map(Decimal),
        "price": prices,
        "multiplier": st.sampled_from([D(1), D(10), D(100)]),
        "issuer_country": st.none() | st.sampled_from(["GB", "HK", "ES", "FR", "IT", "US", "DE"]),
        "side": st.sampled_from(["buy", "sell"]),
        "on": trade_dates,
        "taxable_issuer": st.none() | st.booleans(),
        "market_cap_usd": st.none() | st.decimals(min_value=D(0), max_value=D("1E13"), places=0),
        "average_daily_value": st.none()
        | st.decimals(min_value=D(1), max_value=D("1E10"), places=0),
        "underlying_symbol": st.none() | st.sampled_from(["SPY", "spy", "AAPL"]),
    }
)


def leg_from(costs: CostBook, inputs: dict[str, Any]) -> LegCost:
    asset_class = inputs["asset_class"]
    terms = OrderTerms(
        inputs["venue"], asset_class, inputs["currency"], inputs["quantity"], inputs["price"]
    )
    return costs.leg_cost(
        terms,
        side=inputs["side"],
        on=inputs["on"],
        issuer_country=inputs["issuer_country"],
        taxable_issuer=inputs["taxable_issuer"],
        market_cap_usd=inputs["market_cap_usd"],
        average_daily_value=inputs["average_daily_value"],
        underlying_symbol=inputs["underlying_symbol"],
        multiplier=inputs["multiplier"] if asset_class is AssetClass.OPTION else None,
    )


@given(inputs=leg_inputs, target=amounts, entry=prices)
def test_every_leg_is_either_fully_priced_or_names_why_not(book, inputs, target, entry):
    cost = leg_from(book, inputs)
    parts = (cost.spread_cost, cost.slippage_cost, cost.commission.amount, cost.tax.amount)
    assert all(part is None or type(part) is Decimal for part in (*parts, cost.notional))
    assert (cost.commission.amount is None) == (cost.commission.gap is not None)
    if cost.commission.gap is not None:
        assert cost.commission.gap in cost.gaps
    if cost.tax.gap is not None:
        assert cost.tax.gap in cost.gaps
    if inputs["asset_class"] not in EQUITY_LIKE and inputs["asset_class"] is not AssetClass.OPTION:
        assert CostGap.NO_FRICTION_MODEL in cost.gaps
        assert cost.spread_cost is None
    adjusted = adjust_position_for_costs(target, cost)
    breakeven = breakeven_move(entry, cost)
    if cost.gaps:
        assert (cost.total, cost.total_fraction, adjusted, breakeven) == (None, None, None, None)
        return
    spread, slippage, commission, tax_amount = parts
    total = cost.total
    assert total == spread + slippage + commission + tax_amount
    assert total > ZERO
    assert cost.total_fraction == total / cost.notional
    assert ZERO <= adjusted <= target
    if target > ZERO:
        assert adjusted < target
    price, drag = breakeven
    assert drag == cost.total_fraction * 2
    assert price > entry


@given(quantity=st.integers(min_value=1, max_value=50_000).map(Decimal), price=prices, cap=amounts)
def test_an_unknown_market_cap_never_costs_less_than_a_known_one(book, quantity, price, cap):
    unknown = leg(book, shares(quantity, price), issuer_country="US")
    known = leg(book, shares(quantity, price), issuer_country="US", market_cap_usd=cap)
    assert unknown.total >= known.total


@given(count=whole_contracts, premium=prices)
def test_an_unknown_underlying_never_costs_less_than_a_deep_one(book, count, premium):
    unknown = leg(book, contracts(count, premium), multiplier=D(100), issuer_country="US")
    deep = leg(
        book,
        contracts(count, premium),
        multiplier=D(100),
        issuer_country="US",
        underlying_symbol="SPY",
    )
    assert unknown.total >= deep.total


@pytest.mark.parametrize("multiplier", [D(-1), D(0), D("-Infinity"), D("NaN"), D("sNaN"), D("-0")])
def test_an_option_leg_refuses_a_multiplier_that_is_not_positive(book, multiplier):
    with pytest.raises((ValueError, ArithmeticError)):
        leg(book, contracts(), multiplier=multiplier, underlying_symbol="SPY")


@pytest.mark.parametrize(
    ("terms", "multiplier"),
    [
        (shares(), "-1"),
        (shares(), "0"),
        (shares(), "NaN"),
        (shares(), "Infinity"),
        (shares(asset_class=AssetClass.ETF), "-100"),
        (contracts(), "Infinity"),
    ],
)
def test_a_multiplier_that_is_not_positive_and_finite_is_refused_on_any_leg(
    book, terms, multiplier
):
    assert refused_or_unpriced(
        lambda: leg(
            book,
            terms,
            market_cap_usd=LIQUID_CAP,
            issuer_country="US",
            underlying_symbol="SPY",
            multiplier=D(multiplier),
        )
    )


def test_a_negative_multiplier_cannot_grow_a_position_past_its_target(book):
    target = D(10_000)
    try:
        cost = leg(book, shares(), market_cap_usd=LIQUID_CAP, issuer_country="US", multiplier=D(-1))
    except ValueError:
        return
    adjusted = adjust_position_for_costs(target, cost)
    assert adjusted is None or adjusted <= target


@pytest.mark.parametrize("field", ["market_cap_usd", "average_daily_value"])
def test_a_nan_friction_input_refuses_rather_than_pricing(book, field):
    with pytest.raises((ValueError, ArithmeticError)):
        leg(book, shares(), issuer_country="US", **{field: D("NaN")})


def test_an_asset_class_given_as_a_bare_string_never_prices_an_option(book):
    bare = OrderTerms("XNYS", "option", "USD", D(10), D(10))  # not the enum member
    cost = leg(book, bare, underlying_symbol="SPY", issuer_country="US")
    assert cost.total is None


def test_a_bond_past_its_tier_names_both_gaps(book):
    cost = leg(book, face("9000", price="1"))
    assert cost.gaps == (CostGap.BEYOND_TIER, CostGap.NO_FRICTION_MODEL)
    assert cost.total is None


@pytest.mark.parametrize(
    "contradiction",
    [
        {"tax": TaxEstimate(ZERO, CostGap.TAX_UNKNOWN, "unknown", True, True, False, "unknown")},
        {"commission": CommissionEstimate(D(5), CostGap.BEYOND_TIER, "tier", True, "beyond")},
    ],
)
def test_a_leg_whose_parts_contradict_its_gaps_has_no_total(book, contradiction):
    priced = leg(book, shares(), market_cap_usd=LIQUID_CAP, issuer_country="US")
    assert priced.total is not None
    assert dataclasses.replace(priced, **contradiction).total is None


# ── sizing and breakeven ──


@given(target=st.decimals(min_value=D("-1E9"), max_value=D(0), places=2))
def test_a_non_positive_target_never_becomes_a_position(book, target):
    cost = leg(book, shares(), market_cap_usd=LIQUID_CAP, issuer_country="US")
    assert adjust_position_for_costs(target, cost) == ZERO


@pytest.mark.parametrize("target", [D("NaN"), D("sNaN")])
def test_a_nan_target_refuses_rather_than_sizing(book, target):
    cost = leg(book, shares(), market_cap_usd=LIQUID_CAP, issuer_country="US")
    with pytest.raises((ValueError, ArithmeticError)):
        adjust_position_for_costs(target, cost)


@pytest.mark.parametrize("entry", ["0", "-1", "NaN", "Infinity"])
def test_a_breakeven_from_a_non_positive_or_non_finite_entry_is_refused(book, entry):
    cost = leg(book, shares(), market_cap_usd=LIQUID_CAP, issuer_country="US")
    with pytest.raises(ValueError, match=r"entry|price"):
        breakeven_move(D(entry), cost)
