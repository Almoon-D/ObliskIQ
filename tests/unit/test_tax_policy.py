"""The tax policies: Spain's IRPF rules and the generic template, with the shipped profile's
values, checked against figures computed by hand from the law (docs/knowledge/tax-es.md).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.domain.tax import SecurityIdentity, SecurityKind, currency_identity
from obliskiq.errors import PolicyError
from obliskiq.jurisdiction import TAX_POLICIES, JurisdictionProfile, load_profile
from obliskiq.jurisdiction.tax import WashSaleWindow
from obliskiq.jurisdiction.tax_es_irpf import EsIrpfTaxPolicy
from obliskiq.jurisdiction.tax_generic import GenericTaxPolicy
from obliskiq.jurisdiction.tax_profile import TaxProfile

D = Decimal


@pytest.fixture(scope="module")
def tax_profile(repo_root) -> TaxProfile:
    profile = load_profile(repo_root / "config", "es")
    assert profile.tax is not None
    return profile.tax


@pytest.fixture(scope="module")
def es(tax_profile: TaxProfile) -> EsIrpfTaxPolicy:
    return EsIrpfTaxPolicy(tax_profile)


def etf(key: str, *, issuer: str = "iShares", index: str = "MSCI World", isin: str | None = None,
        listed: bool = True) -> SecurityIdentity:  # fmt: skip
    return SecurityIdentity(
        key=key, kind=SecurityKind.ETF, isin=isin, issuer=issuer, tracked_index=index, listed=listed
    )


def share(key: str, issuer: str, isin: str | None = None) -> SecurityIdentity:
    return SecurityIdentity(
        key=key, kind=SecurityKind.SHARE, isin=isin, issuer=issuer, tracked_index=None, listed=True
    )


def fund(key: str, *, listed: bool = False) -> SecurityIdentity:
    return SecurityIdentity(
        key=key,
        kind=SecurityKind.FUND,
        isin=None,
        issuer="Vanguard",
        tracked_index=f"index of {key}",
        listed=listed,
    )


# ── the savings scale (LIRPF arts. 66.1 and 76, from 2025) ──


@pytest.mark.parametrize(
    ("base", "tax"),
    [
        # Each figure is the BOE table's cumulative quota plus the bracket rate on the remainder.
        ("0", "0"),
        ("6000", "1140"),  # 6,000 x 19 %
        ("10000", "1980"),  # 1,140 + 4,000 x 21 %
        ("50000", "10380"),  # the BOE's cuota for 50,000
        ("60000", "12680"),  # 10,380 + 10,000 x 23 %
        ("200000", "44880"),
        ("300000", "71880"),
        ("400000", "101880"),  # 71,880 + 100,000 x 30 %
    ],
)
def test_the_savings_scale_matches_the_published_quotas(es, base, tax):
    assert es.savings_tax(D(base)) == D(tax)


def test_a_negative_or_non_finite_base_is_refused(es):
    for base in (D(-1), D("NaN"), D("Infinity")):
        with pytest.raises(ValueError, match="savings base"):
            es.savings_tax(base)


def test_the_rate_table_says_which_years_it_covers(es):
    assert es.rate_table_covers(2025)
    assert es.rate_table_covers(2026)
    assert not es.rate_table_covers(2024)
    assert not es.rate_table_covers(2027)
    assert "2025 to 2026" in es.rate_table_label()
    assert "Ley 7/2024" in es.rate_table_label()


def test_a_years_tax_is_paid_on_30_june_of_the_next_year(es):
    assert es.payment_date(2025) == date(2026, 6, 30)
    assert "Orden HAC/277/2026" in es.payment_label()


# ── withholding and the foreign-tax credit (art. 80) ──


def test_withholding_comes_from_the_profile_and_an_unstated_country_has_none(es):
    assert es.withholding_rate("US") == D("0.15")
    assert es.withholding_rate("FR") is None


def test_the_foreign_tax_credit_is_the_lesser_of_tax_paid_and_tax_at_the_average_rate(es):
    # 100 of US income in a base of 100: tax 19, average rate 19.00 %, 15 withheld -> credit 15.
    assert (
        es.foreign_tax_credit(withheld=D(15), foreign_income=D(100), tax=D(19), base=D(100)) == 15
    )
    # 100 of foreign income withheld at 30: the credit stops at 19 % of it.
    assert (
        es.foreign_tax_credit(withheld=D(30), foreign_income=D(100), tax=D(19), base=D(100)) == 19
    )
    # The average rate is kept to two decimals of a percent: 1,980 / 10,000 = 19.80 %.
    credit = es.foreign_tax_credit(
        withheld=D(500), foreign_income=D(1000), tax=D(1980), base=D(10000)
    )
    assert credit == D("198.0")


def test_no_credit_without_withholding_income_base_or_tax(es):
    for kwargs in (
        {"withheld": D(0), "foreign_income": D(100), "tax": D(19), "base": D(100)},
        {"withheld": D(15), "foreign_income": D(0), "tax": D(19), "base": D(100)},
        {"withheld": D(15), "foreign_income": D(100), "tax": D(0), "base": D(0)},
    ):
        assert es.foreign_tax_credit(**kwargs) == 0


# ── homogeneity (RIRPF art. 8, conservative) and the wash-sale window ──


def test_the_same_isin_is_homogeneous_whatever_else_differs(es):
    first = etf("A", isin="IE00B4L5Y983")
    second = etf("B", issuer="Other", index="Other", isin="IE00B4L5Y983")
    assert es.homogeneous(first, second)


def test_one_issuer_on_one_index_is_homogeneous(es):
    accumulating = etf("ACC", isin="IE00B4L5Y983")
    distributing = etf("DIST", isin="IE00B0M62Q58")
    assert es.homogeneous(accumulating, distributing)


def test_another_issuer_or_another_index_is_not_homogeneous(es):
    held = etf("A")
    assert not es.homogeneous(held, etf("B", issuer="Xtrackers"))
    assert not es.homogeneous(held, etf("C", index="FTSE All-World"))


def test_shares_of_one_issuer_are_homogeneous_and_a_share_is_not_an_etf(es):
    assert es.homogeneous(share("ABC.A", "Abc Corp"), share("ABC.B", "Abc Corp"))
    assert not es.homogeneous(share("ABC.A", "Abc Corp"), share("XYZ", "Xyz Corp"))
    assert not es.homogeneous(share("ISH", "iShares"), etf("A"))


def test_the_window_is_two_months_listed_and_a_year_unlisted(es):
    assert es.wash_sale_window(etf("A")) == WashSaleWindow(before_months=2, after_months=2)
    assert es.wash_sale_window(fund("F")) == WashSaleWindow(before_months=12, after_months=12)


def test_a_currency_loss_waits_only_for_a_buy_back_after_it(es, tax_profile):
    """Art. 33.5.e, read conservatively: the currency bought back within the year after the
    disposal; currency already held never takes the loss."""
    assert es.currency_lots
    assert es.wash_sale_window(currency_identity("USD")) == WashSaleWindow(
        before_months=0, after_months=tax_profile.currency_gains.loss_reacquisition_months
    )
    assert es.homogeneous(currency_identity("USD"), currency_identity("USD"))
    assert not es.homogeneous(currency_identity("USD"), currency_identity("GBP"))
    assert not es.homogeneous(currency_identity("USD"), etf("A"))
    free = tax_profile.model_copy(
        update={
            "currency_gains": tax_profile.currency_gains.model_copy(
                update={"loss_reacquisition_months": None}
            )
        }
    )
    assert EsIrpfTaxPolicy(free).wash_sale_window(currency_identity("USD")) is None
    assert "no buy-back rule" in " ".join(EsIrpfTaxPolicy(free).describe())
    untaxed = tax_profile.model_copy(
        update={
            "currency_gains": tax_profile.currency_gains.model_copy(update={"kept_as_lots": False})
        }
    )
    assert not EsIrpfTaxPolicy(untaxed).currency_lots
    assert "not kept as lots" in " ".join(EsIrpfTaxPolicy(untaxed).describe())


# ── switches (art. 94.1.a) and swap partners ──


def test_an_etf_switch_is_a_sale(es):
    assert not es.switch_deferral(etf("A"), etf("B", issuer="Xtrackers"))
    assert not es.switch_deferral(fund("F", listed=True), fund("G", listed=True))


def test_a_fund_switch_is_deferred_only_when_the_profile_says_the_broker_supports_it(
    es, tax_profile
):
    assert not es.switch_deferral(fund("F"), fund("G"))
    supported = EsIrpfTaxPolicy(
        tax_profile.model_copy(update={"fund_switch_deferral_at_broker": True})
    )
    assert supported.switch_deferral(fund("F"), fund("G"))
    assert not supported.switch_deferral(fund("F"), etf("A"))


def test_a_swap_partner_differs_in_issuer_and_index(es, tax_profile):
    held = etf("A")
    assert es.swap_partner_allowed(held, etf("X", issuer="Xtrackers", index="FTSE Developed"))
    assert not es.swap_partner_allowed(held, etf("B", issuer="Xtrackers"))
    assert not es.swap_partner_allowed(held, etf("C", index="FTSE Developed"))
    assert not es.swap_partner_allowed(held, held)
    loose = tax_profile.model_copy(
        update={
            "swap_partner": tax_profile.swap_partner.model_copy(update={"differ_in_index": False})
        }
    )
    relaxed = EsIrpfTaxPolicy(loose)
    # Another issuer on the same index is now allowed: it is not homogeneous either.
    assert relaxed.swap_partner_allowed(held, etf("B", issuer="Xtrackers"))
    # A held security without an issuer cannot be shown to differ from anything.
    anonymous = share("ANON", "Anon")
    assert not es.swap_partner_allowed(
        anonymous.model_copy(update={"issuer": None}), share("OTHER", "Other")
    )


def test_the_lot_method_and_offsets_come_from_the_profile(es):
    assert es.lot_method == "fifo"
    assert es.carry_forward_years == 4
    assert es.cross_offset_fraction == D("0.25")
    assert es.policy_id == "es_irpf"


# ── the generic template ──


def test_the_generic_policy_has_no_wash_sale_rule_and_no_switch_deferral(tax_profile):
    generic = GenericTaxPolicy("generic", tax_profile)
    assert generic.wash_sale_window(etf("A")) is None
    assert generic.wash_sale_window(currency_identity("USD")) == WashSaleWindow(0, 12)
    assert not generic.switch_deferral(fund("F"), fund("G"))
    assert generic.homogeneous(etf("A"), etf("A"))
    assert generic.homogeneous(etf("A", isin="IE00B4L5Y983"), etf("B", isin="IE00B4L5Y983"))
    assert not generic.homogeneous(etf("A"), etf("B"))
    assert generic.swap_partner_allowed(etf("A"), etf("X", issuer="Xtrackers", index="FTSE"))
    assert not generic.swap_partner_allowed(etf("A"), etf("A"))
    assert generic.savings_tax(D(10000)) == D(1980)


# ── the registry ──


def test_the_shipped_profile_resolves_es_irpf(repo_root, shipped_settings: ObliskConfig):
    profile = load_profile(repo_root / "config", "es")
    policy = TAX_POLICIES.resolve(profile, shipped_settings)
    assert policy.policy_id == "es_irpf"
    generic = TAX_POLICIES.resolve(
        profile.model_copy(update={"policies": {"tax": "generic"}}), shipped_settings
    )
    assert generic.policy_id == "generic"


def test_a_profile_without_tax_data_cannot_build_a_tax_policy(shipped_settings: ObliskConfig):
    profile = JurisdictionProfile(id="es", name="Spain", policies={"tax": "es_irpf"})
    with pytest.raises(PolicyError, match="carries no tax section"):
        TAX_POLICIES.resolve(profile, shipped_settings)


# ── the profile's validation ──


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"savings_rate_table__brackets": [{"up_to": "6000", "rate": "0.19"}]},
            "last bracket has up_to: null",
        ),
        (
            {
                "savings_rate_table__brackets": [
                    {"up_to": None, "rate": "0.19"},
                    {"up_to": None, "rate": "0.21"},
                ]
            },
            "only the last bracket",
        ),
        (
            {
                "savings_rate_table__brackets": [
                    {"up_to": "6000", "rate": "0.19"},
                    {"up_to": "6000", "rate": "0.21"},
                    {"up_to": None, "rate": "0.23"},
                ]
            },
            "strictly ascend",
        ),
        ({"savings_rate_table__verified_through_tax_year": 2024}, "cannot precede"),
        ({"payment__month": 2, "payment__day": 30}, "not a date in every year"),
        ({"payment__month": 2, "payment__day": 29}, "not a date in every year"),
        ({"payment__month": 13, "payment__day": 1}, "not a date in every year"),
        (
            {
                "withholding_at_source": [
                    {"country": "US", "rate": "0.15", "confirmation": "official", "source": "x"},
                    {"country": "US", "rate": "0.10", "confirmation": "official", "source": "y"},
                ]
            },
            "one row per country",
        ),
        ({"savings_rate_table__brackets__0__rate": 0.19}, "quoted strings"),
    ],
)
def test_an_inconsistent_tax_profile_is_refused(tax_profile, changes, message):
    data = tax_profile.model_dump(mode="json")
    for dotted, value in changes.items():
        node: Any = data
        *path, last = dotted.split("__")
        for key in path:
            node = node[int(key)] if key.isdigit() else node[key]
        node[int(last) if last.isdigit() else last] = value
    with pytest.raises(ValidationError, match=message):
        TaxProfile.model_validate(data)


def test_a_collective_investment_names_its_issuer_and_index():
    with pytest.raises(ValidationError, match="names its issuer and the index"):
        SecurityIdentity(
            key="E", kind=SecurityKind.ETF, isin=None, issuer=None, tracked_index="X", listed=True
        )
    with pytest.raises(ValidationError, match="twelve-character ISIN"):
        etf("E", isin="IE00")


def test_a_policy_describes_its_rules_with_their_sources(es, tax_profile):
    lines = es.describe()
    assert lines[0] == "policy es_irpf; lots fifo; homogeneity same_isin_or_issuer_and_index"
    joined = "\n".join(lines)
    for fragment in ("LIRPF art. 49", "Ley 7/2024", "Orden HAC/277/2026", "LIRPF art. 80",
                     "withholding at source US 0.15 (official", "2 months (listed)",
                     "supported: False", "in tracked index: True"):  # fmt: skip
        assert fragment in joined
    generic = GenericTaxPolicy("generic", tax_profile).describe()
    assert generic[-1] == "no wash-sale rule; every switch is a sale"
