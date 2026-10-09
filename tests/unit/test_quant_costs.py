"""What a trade costs: schedules chosen by venue, asset class and currency, taxes by issuer,
friction tiers, and an explicit status wherever a cost cannot be priced.

Ported in intent from the legacy ``test_transaction_costs.py``. Expected values are arithmetic on
the configured figures, written out in each test.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_calendars import Provenance
from obliskiq.config.schema_costs import (
    CommissionSchedule,
    CostsConfig,
    EquityFriction,
    OptionFriction,
    TaxRule,
)
from obliskiq.domain.enums import AssetClass
from obliskiq.quant.costs import (
    CostBook,
    CostGap,
    FrictionInputs,
    OrderTerms,
    adjust_position_for_costs,
    breakeven_move,
)

D = Decimal
TODAY = date(2026, 10, 9)
SOURCE = Provenance(origin="test", verify_live=False)
SOURCE_UNVERIFIED = Provenance(origin="test", verify_live=True)


@pytest.fixture(scope="module")
def book(shipped_settings: ObliskConfig) -> CostBook:
    return CostBook.from_config(shipped_settings)


def shares(quantity: str = "100", price: str = "100", venue: str = "XNAS", currency: str = "USD"):
    return OrderTerms(venue, AssetClass.STOCK, currency, D(quantity), D(price))


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


def tax(**overrides: Any) -> TaxRule:
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
    return TaxRule(**fields)


def custom_book(
    settings: ObliskConfig,
    commissions: list[CommissionSchedule] | None = None,
    taxes: list[TaxRule] | None = None,
) -> CostBook:
    return CostBook(
        commissions=commissions or [],
        taxes=taxes or [],
        friction=FrictionInputs.from_config(settings),
    )


# ── commission ──


def test_a_us_share_pays_the_per_share_rate_above_the_minimum(book):
    estimate = book.commission(shares("1000", "50"))
    assert estimate.amount == D("5.000")  # 1000 * 0.005
    assert estimate.gap is None
    assert estimate.schedule_id == "us_stock"
    assert estimate.unverified


def test_a_small_us_order_pays_the_minimum(book):
    assert book.commission(shares("10", "100")).amount == D("1.00")  # 0.05 -> minimum


def test_a_penny_stock_order_is_capped_at_a_fraction_of_its_value(book):
    # 10,000 shares at 0.02 is 200 of value; 50 of per-share commission exceeds the 1 % cap (2.00).
    assert book.commission(shares("10000", "0.02")).amount == D("2.0000")


def test_the_cap_has_the_last_word_over_the_minimum(book):
    # 10 shares at 0.02 is 0.20 of value: the 1.00 minimum would exceed the 1 % cap (0.002).
    assert book.commission(shares("10", "0.02")).amount == D("0.0020")


def test_an_empty_order_pays_nothing(book):
    estimate = book.commission(shares("0", "100"))
    assert estimate.amount == D(0)
    assert estimate.gap is None


def test_an_etf_takes_the_same_us_schedule(book):
    etf = OrderTerms("XNYS", AssetClass.ETF, "USD", D(100), D(400))
    assert book.commission(etf).schedule_id == "us_stock"


def test_an_option_pays_per_contract_with_a_minimum(book):
    ten = OrderTerms("XNYS", AssetClass.OPTION, "USD", D(10), D("10"))
    assert book.commission(ten).amount == D("6.50")  # 10 * 0.65
    one = OrderTerms("XNYS", AssetClass.OPTION, "USD", D(1), D("10"))
    assert book.commission(one).amount == D("1.00")  # 0.65 -> minimum


def test_a_eurex_option_has_no_established_minimum(book):
    one = OrderTerms("XEUR", AssetClass.OPTION, "EUR", D(1), D("10"))
    estimate = book.commission(one)
    assert estimate.amount == D("1.50")
    assert estimate.schedule_id == "eurex_option"


def test_a_us_treasury_pays_a_fraction_of_face_with_a_minimum(book):
    large = OrderTerms("BONDDESK", AssetClass.BOND, "USD", D(1_000_000))
    assert book.commission(large).amount == D("20.00000")  # 0.002 % of 1,000,000
    small = OrderTerms("BONDDESK", AssetClass.BOND, "USD", D(10_000))
    assert book.commission(small).amount == D("5.00")  # 0.20 -> minimum 5


def test_a_euro_bond_pays_up_to_its_tier_and_is_unpriced_beyond_it(book):
    inside = OrderTerms("BONDDESK", AssetClass.BOND, "EUR", D(5_000))
    assert book.commission(inside).amount == D("5.000")  # 0.1 % of 5,000
    floor = OrderTerms("BONDDESK", AssetClass.BOND, "EUR", D(100))
    assert book.commission(floor).amount == D("1.00")  # minimum
    edge = OrderTerms("BONDDESK", AssetClass.BOND, "EUR", D(8_500))
    assert book.commission(edge).amount == D("8.500")
    beyond = book.commission(OrderTerms("BONDDESK", AssetClass.BOND, "EUR", D(8_501)))
    assert beyond.amount is None
    assert beyond.gap is CostGap.BEYOND_TIER
    assert "above the 8500" in beyond.detail


@pytest.mark.parametrize(
    "terms",
    [
        OrderTerms("XETR", AssetClass.STOCK, "EUR", D(10), D(100)),  # no European share schedule
        OrderTerms("XNAS", AssetClass.STOCK, "EUR", D(10), D(100)),  # right venue, wrong currency
        OrderTerms("XTKS", AssetClass.ETF, "JPY", D(100), D(3000)),
        OrderTerms("XNAS", AssetClass.FUND, "USD", D(10), D(100)),
    ],
)
def test_no_schedule_is_a_status_never_zero(book, terms):
    estimate = book.commission(terms)
    assert estimate.amount is None
    assert estimate.gap is CostGap.NO_SCHEDULE
    assert estimate.schedule_id is None
    assert terms.venue in estimate.detail


def test_two_schedules_for_one_key_are_a_configuration_error(shipped_settings):
    twice = custom_book(shipped_settings, [schedule(id="one"), schedule(id="two")])
    with pytest.raises(ValueError, match=r"several commission schedules match.*one, two"):
        twice.commission(shares())


def test_a_capped_per_share_schedule_needs_a_price(shipped_settings):
    capped = custom_book(shipped_settings, [schedule()])
    with pytest.raises(ValueError, match="needs the order's price"):
        capped.commission(OrderTerms("XNAS", AssetClass.STOCK, "USD", D(10)))


def test_a_schedule_with_no_cap_does_not_need_a_price(shipped_settings):
    uncapped = custom_book(shipped_settings, [schedule(maximum_fraction_of_value=None)])
    estimate = uncapped.commission(OrderTerms("XNAS", AssetClass.STOCK, "USD", D(1000)))
    assert estimate.amount == D("5.000")


def test_a_schedule_with_no_minimum_charges_what_the_rate_gives(shipped_settings):
    flat = custom_book(shipped_settings, [schedule(minimum=None, maximum_fraction_of_value=None)])
    assert flat.commission(shares("10", "100")).amount == D("0.050")


def test_a_verified_schedule_is_not_labelled_unverified(shipped_settings):
    verified = custom_book(shipped_settings, [schedule(source=SOURCE)])
    assert not verified.commission(shares()).unverified
    unverified = custom_book(shipped_settings, [schedule(source=SOURCE_UNVERIFIED)])
    assert unverified.commission(shares()).unverified


# ── transaction tax ──


def apply(book: CostBook, country: str | None, side="buy", on=TODAY, **kw):
    return book.transaction_tax(
        issuer_country=country,
        asset_class=AssetClass.STOCK,
        side=side,
        notional=D(10_000),
        on=on,
        **kw,
    )


def test_the_uk_charges_stamp_duty_reserve_tax_on_purchases_only(book):
    bought = apply(book, "GB")
    assert bought.amount == D("50.000")  # 0.5 % of 10,000
    assert bought.applies
    assert bought.confirmation == "official"
    assert apply(book, "GB", side="sell").applies is False


def test_hong_kong_charges_both_sides_from_its_effective_date(book):
    assert apply(book, "HK", side="buy").amount == D("10.000")
    assert apply(book, "HK", side="sell").amount == D("10.000")
    before = apply(book, "HK", on=date(2023, 11, 16))
    assert before.applies is False  # the configured rate was not in force yet
    on_the_day = apply(book, "HK", on=date(2023, 11, 17))
    assert on_the_day.applies


def test_a_transaction_tax_on_listed_issuers_is_assumed_when_the_list_is_not_given(book):
    assumed = apply(book, "ES")
    assert assumed.amount == D("20.000")  # 0.2 %
    assert assumed.scope_assumed
    assert apply(book, "ES", taxable_issuer=True).scope_assumed is False
    off_the_list = apply(book, "ES", taxable_issuer=False)
    assert off_the_list.applies is False
    assert off_the_list.amount == D(0)


def test_the_french_tax_starts_when_its_current_rate_did(book):
    assert apply(book, "FR", on=date(2025, 3, 31)).applies is False
    france = apply(book, "FR", on=date(2025, 4, 1))
    assert france.amount == D("40.000")
    assert france.confirmation == "secondary"
    assert "tabled" in france.note


def test_a_tax_whose_rate_is_not_known_is_unknown_never_zero(book):
    italy = apply(book, "IT")
    assert italy.gap is CostGap.TAX_UNKNOWN
    assert italy.rule_id == "it_financial_transaction_tax"
    assert italy.applies
    assert italy.unverified
    assert "could not be stated" in italy.detail
    assert apply(book, "IT", taxable_issuer=False).applies is False


def test_an_issuer_with_no_rule_is_not_modelled_rather_than_untaxed(book):
    us = apply(book, "US")
    assert (us.amount, us.applies, us.gap) == (D(0), False, None)
    assert "no tax rule for US" in us.detail
    unknown_country = apply(book, None)
    assert unknown_country.applies is False
    assert "issuer country not given" in unknown_country.detail


def test_a_tax_is_for_its_asset_classes_only(book):
    etf = book.transaction_tax(
        issuer_country="GB",
        asset_class=AssetClass.ETF,
        side="buy",
        notional=D(10_000),
        on=TODAY,
    )
    assert etf.applies is False


def test_the_highest_rate_stands_when_several_apply(shipped_settings):
    two = custom_book(
        shipped_settings,
        taxes=[tax(id="low", rate=D("0.001")), tax(id="high", rate=D("0.004"))],
    )
    estimate = apply(two, "GB")
    assert estimate.rule_id == "high"
    assert estimate.amount == D("40.000")


def test_a_tax_with_no_notional_costs_nothing_but_is_still_reported(shipped_settings):
    only = custom_book(shipped_settings, taxes=[tax()])
    estimate = only.transaction_tax(
        issuer_country="GB",
        asset_class=AssetClass.STOCK,
        side="buy",
        notional=None,
        on=TODAY,
    )
    assert estimate.amount == D(0)
    assert estimate.applies


# ── friction ──


def test_impact_grows_with_the_square_root_of_participation_and_is_capped(book):
    # coefficient 0.10 * sqrt(1,000,000 / 100,000,000) = 0.10 * 0.1
    assert book.market_impact_fraction(D(1_000_000), D(100_000_000)) == D("0.010")
    assert book.market_impact_fraction(D(4_000_000), D(100_000_000)) == D("0.020")
    assert book.market_impact_fraction(D(100_000_000), D(100_000_000)) == D("0.05")  # capped


@pytest.mark.parametrize("adv", [None, D(0), D(-5)])
def test_impact_is_zero_without_a_usable_average_daily_value(book, adv):
    assert book.market_impact_fraction(D(1_000_000), adv) == D(0)


def test_impact_is_zero_for_an_order_with_no_value(book):
    assert book.market_impact_fraction(D(0), D(1_000_000)) == D(0)


def test_slippage_is_the_flat_tier_plus_impact(book):
    flat = book.equity_slippage_fraction(D(10_000))
    assert flat == D("0.001")
    illiquid = book.equity_slippage_fraction(D(10_000), liquid=False)
    assert illiquid == D("0.005")
    with_impact = book.equity_slippage_fraction(D(1_000_000), D(100_000_000))
    assert with_impact == D("0.011")


def test_option_half_spread_follows_the_underlying_unless_told(book):
    assert book.option_half_spread_fraction("SPY") == D("0.005")
    assert book.option_half_spread_fraction("spy") == D("0.005")
    assert book.option_half_spread_fraction("AAPL") == D("0.015")
    assert book.option_half_spread_fraction(None) == D("0.015")
    assert book.option_half_spread_fraction("AAPL", liquid_underlying=True) == D("0.005")
    assert book.option_half_spread_fraction("SPY", liquid_underlying=False) == D("0.015")


def test_a_share_is_liquid_at_the_configured_market_capitalisation(book):
    assert book.is_liquid_share(D("10000000000"))
    assert not book.is_liquid_share(D("9999999999"))
    assert not book.is_liquid_share(None)


# ── a leg ──


def leg(book: CostBook, terms: OrderTerms, **kw):
    kw.setdefault("side", "buy")
    kw.setdefault("on", TODAY)
    return book.leg_cost(terms, **kw)


def test_a_liquid_share_costs_a_fifth_of_a_percent_plus_commission(book):
    cost = leg(book, shares("100", "100"), market_cap_usd=D("3000000000000"), issuer_country="US")
    assert cost.liquidity_tier == "liquid"
    assert cost.spread_cost == D("10.000")  # 0.1 % of 10,000
    assert cost.slippage_cost == D("10.000")
    assert cost.commission.amount == D("1.00")  # the IBKR per-share schedule, not a flat zero
    assert cost.tax.applies is False
    assert cost.total == D("21.000")
    assert cost.total_fraction == D("0.0021")
    assert cost.total_fraction < D("0.0025")  # the legacy bound for a liquid name


def test_an_illiquid_share_costs_about_one_percent(book):
    cost = leg(book, shares("100", "100"), market_cap_usd=D("500000000"), issuer_country="US")
    assert cost.liquidity_tier == "illiquid"
    assert cost.total_fraction > D("0.009")


def test_an_unknown_market_capitalisation_is_costed_as_illiquid_and_says_so(book):
    cost = leg(book, shares("100", "100"), issuer_country="US")
    assert cost.liquidity_tier == "illiquid_assumed"
    assert cost.spread_cost == D("50.000")
    assert "market_cap_unknown" in cost.unverified


def test_impact_enters_the_slippage_of_a_share_leg(book):
    cost = leg(
        book,
        shares("10000", "100"),  # 1,000,000 of value
        market_cap_usd=D("3000000000000"),
        average_daily_value=D(100_000_000),
        issuer_country="US",
    )
    assert cost.slippage_cost == D("11000.000")  # (0.001 + 0.010) of 1,000,000


def test_an_option_leg_books_the_half_spread_and_no_slippage(book):
    ten = OrderTerms("XNYS", AssetClass.OPTION, "USD", D(10), D("10"))
    cost = leg(book, ten, multiplier=D(100), underlying_symbol="AAPL", issuer_country="US")
    assert cost.notional == D(10_000)
    assert cost.liquidity_tier == "single_name_option"
    assert cost.spread_cost == D("150.000")  # 1.5 % of 10,000
    assert cost.slippage_cost == D(0)
    assert cost.commission.amount == D("6.50")
    assert cost.total == D("156.500")


def test_an_option_on_a_deep_underlying_takes_the_tight_tier(book):
    ten = OrderTerms("XNYS", AssetClass.OPTION, "USD", D(10), D("10"))
    cost = leg(book, ten, multiplier=D(100), underlying_symbol="SPY", issuer_country="US")
    assert cost.liquidity_tier == "liquid_option"
    assert cost.spread_cost == D("50.000")
    forced = leg(book, ten, multiplier=D(100), underlying_symbol="AAPL", liquid_underlying=True)
    assert forced.liquidity_tier == "liquid_option"


def test_a_leg_with_a_gap_has_no_total(book):
    european = OrderTerms("XETR", AssetClass.STOCK, "EUR", D(100), D(100))
    cost = leg(book, european, market_cap_usd=D("3000000000000"), issuer_country="DE")
    assert cost.gaps == (CostGap.NO_SCHEDULE,)
    assert cost.total is None
    assert cost.total_fraction is None
    assert cost.spread_cost == D("10.000")  # the parts that can be priced still are


def test_an_italian_share_has_no_total_until_its_tax_rate_is_known(book):
    milan = OrderTerms("XMIL", AssetClass.STOCK, "EUR", D(100), D(100))
    cost = leg(book, milan, issuer_country="IT")
    assert CostGap.TAX_UNKNOWN in cost.gaps
    assert CostGap.NO_SCHEDULE in cost.gaps
    assert cost.total is None


def test_an_asset_class_with_no_spread_model_is_a_gap(book):
    bond = OrderTerms("BONDDESK", AssetClass.BOND, "EUR", D(5_000), D(1))
    cost = leg(book, bond)
    assert cost.gaps == (CostGap.NO_FRICTION_MODEL,)
    assert cost.liquidity_tier == "not_modelled"
    assert (cost.spread_cost, cost.slippage_cost, cost.total) == (None, None, None)
    assert cost.commission.amount == D("5.000")


def test_a_leg_needs_a_price(book):
    with pytest.raises(ValueError, match="needs the order's price"):
        leg(book, OrderTerms("XNAS", AssetClass.STOCK, "USD", D(10)))


def test_a_leg_with_no_notional_has_no_fraction(book):
    zero = shares("0", "100")
    cost = leg(book, zero, market_cap_usd=D("3000000000000"), issuer_country="US")
    assert cost.notional == D(0)
    assert cost.total_fraction is None


def test_gaps_accumulate_across_components(book):
    fund = OrderTerms("XETR", AssetClass.FUND, "EUR", D(10), D(1))
    cost = leg(book, fund, issuer_country="IT")
    assert cost.gaps == (CostGap.NO_SCHEDULE, CostGap.NO_FRICTION_MODEL)


def test_every_unverified_label_has_an_origin(book):
    cost = leg(
        book,
        shares("100", "100"),
        issuer_country="ES",
    )
    labels = set(cost.unverified)
    assert {"market_cap_unknown", "friction_assumption", "commission:us_stock"} <= labels
    assert "tax_scope_assumed:es_financial_transaction_tax" in labels
    origins = book.origins(cost)
    assert set(origins) == labels
    assert "assumptions, not measurements" in origins["friction_assumption"]
    assert "IBKR fixed schedule" in origins["commission:us_stock"]
    assert "secondary" in origins["tax:es_financial_transaction_tax"]
    assert "no market capitalisation" in origins["market_cap_unknown"]
    no_tax = leg(book, shares("100", "100"), issuer_country="US")
    assert "no transaction tax is configured" in book.origins(no_tax)["tax_not_modelled"]


# ── sizing and breakeven ──


def test_a_position_shrinks_by_its_costs(book):
    cost = leg(book, shares("100", "100"), market_cap_usd=D("3000000000000"), issuer_country="US")
    assert adjust_position_for_costs(D(10_000), cost) == D("9979.000")
    assert adjust_position_for_costs(D(5), cost) == D(0)  # never negative


def test_a_position_cannot_be_adjusted_by_a_cost_nobody_knows(book):
    unknown = leg(book, OrderTerms("XETR", AssetClass.STOCK, "EUR", D(100), D(100)))
    assert adjust_position_for_costs(D(10_000), unknown) is None


def test_breakeven_pays_every_cost_twice(book):
    cost = leg(book, shares("100", "100"), market_cap_usd=D("3000000000000"), issuer_country="US")
    price, move = breakeven_move(D(180), cost)
    assert move == D("0.0042")  # 2 * 0.21 %
    assert price == D(180) * D("1.0042")


def test_no_breakeven_without_a_priced_leg(book):
    unknown = leg(book, OrderTerms("XETR", AssetClass.STOCK, "EUR", D(100), D(100)))
    assert breakeven_move(D(100), unknown) is None


# ── the configuration ──


def test_every_figure_is_exact_decimal(book):
    cost = leg(book, shares("3", "33.33"), market_cap_usd=D("3000000000000"), issuer_country="US")
    assert isinstance(cost.total, Decimal)
    assert cost.spread_cost == D("3") * D("33.33") * D("0.001")  # no float rounding anywhere


def test_a_float_in_the_configuration_is_refused_with_a_hint():
    with pytest.raises(
        ValidationError, match=r'write money and rates as quoted strings, such as "0.005"'
    ):
        schedule(rate=0.005)
    with pytest.raises(ValidationError, match="quoted strings"):
        schedule(minimum=1.0)


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        (
            {"maximum_fraction_of_value": D("0.01"), "kind": "per_contract"},
            "per_share schedules only",
        ),
        ({"up_to_face": D(100)}, "fraction_of_face schedules only"),
        (
            {"kind": "fraction_of_face", "rate": D("1.5"), "maximum_fraction_of_value": None},
            "below 1",
        ),
        ({"venues": []}, "null .any venue. or a non-empty list"),
        ({"rate": D(0)}, "rate"),
        ({"asset_classes": []}, "asset_classes"),
    ],
)
def test_an_incoherent_commission_schedule_is_refused(overrides, complaint):
    with pytest.raises(ValidationError, match=complaint):
        schedule(**overrides)


def test_a_rate_that_is_not_entered_cannot_be_marked_confirmed():
    with pytest.raises(ValidationError, match="cannot be marked confirmed"):
        tax(rate=None, confirmation="official")
    with pytest.raises(ValidationError, match="not confirmed against any source"):
        tax(rate=D("0.005"), confirmation="none")
    assert tax(rate=None, confirmation="none")


def test_a_tax_rate_is_a_fraction():
    with pytest.raises(ValidationError):
        tax(rate=D(1))
    with pytest.raises(ValidationError):
        tax(sides=[])


def test_friction_tiers_cannot_invert(shipped_settings):
    equity = shipped_settings.costs.friction.equity.model_dump()
    equity["illiquid_spread_fraction"] = D("0.0001")
    with pytest.raises(ValidationError, match="illiquid tier cannot cost less"):
        EquityFriction.model_validate(equity)
    option = shipped_settings.costs.friction.option.model_dump()
    option["single_name_half_spread_fraction"] = D("0.0001")
    with pytest.raises(ValidationError, match="single-name tier cannot cost less"):
        OptionFriction.model_validate(option)


def test_identifiers_must_be_unique(shipped_settings):
    data = shipped_settings.costs.model_dump()
    data["commissions"].append(data["commissions"][0])
    with pytest.raises(ValidationError, match="commissions ids must be unique"):
        CostsConfig.model_validate(data)
    data = shipped_settings.costs.model_dump()
    data["taxes"].append(data["taxes"][0])
    with pytest.raises(ValidationError, match="taxes ids must be unique"):
        CostsConfig.model_validate(data)


def test_every_shipped_row_carries_a_source_and_awaits_a_live_reading(shipped_settings):
    costs = shipped_settings.costs
    for row in [*costs.commissions, *costs.taxes]:
        assert row.source.origin
        assert row.source.verify_live
    assert costs.friction.source.verify_live
    assert {t.issuer_country for t in costs.taxes} == {"GB", "HK", "ES", "FR", "IT"}


def test_the_shipped_commission_keys_do_not_collide(shipped_settings):
    seen: set[tuple[str, AssetClass, str]] = set()
    for row in shipped_settings.costs.commissions:
        for venue in row.venues or ["*"]:
            for asset_class in row.asset_classes:
                key = (venue, asset_class, row.currency)
                assert key not in seen, key
                seen.add(key)


# ── every field of a record is read ──


def _attributes_read(path: Path) -> set[str]:
    return {
        node.attr
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute)
    }


@pytest.mark.parametrize("model", [CommissionSchedule, TaxRule, EquityFriction, OptionFriction])
def test_every_field_of_a_cost_record_is_read_by_the_cost_module(model, repo_root):
    """A field nothing reads is a decision that looks made and is not; the keyed-config scan only
    sees a list as one key, so each record's fields are checked here."""
    read = _attributes_read(repo_root / "src" / "obliskiq" / "quant" / "costs.py")
    unread = sorted(set(model.model_fields) - read)
    assert unread == [], f"{model.__name__} fields nothing in quant/costs.py reads: {unread}"
