"""The walk-forward simulator against hand calculations: delta execution with costs, tax paid at
its payment date and at liquidation, the log growth of the liquidated portfolio, and its
refusals. Costs here are a flat 0.1 % commission with no friction, so every figure can be
worked out on paper; the shipped schedules are exercised by the study tests.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from datetime import date
from decimal import Decimal

import pytest

from obliskiq.config.schema_calendars import Provenance
from obliskiq.config.schema_costs import CommissionSchedule
from obliskiq.domain.enums import AssetClass
from obliskiq.domain.research import Observation
from obliskiq.domain.tax import SecurityIdentity, SecurityKind
from obliskiq.errors import SimulationError
from obliskiq.jurisdiction import load_profile
from obliskiq.jurisdiction.tax_es_irpf import EsIrpfTaxPolicy
from obliskiq.quant.costs import CostBook, FrictionInputs
from obliskiq.research.simulate import (
    Book,
    MarketView,
    Panel,
    SimInstrument,
    SimulationSettings,
    Simulator,
    TaxPayment,
)
from obliskiq.research.strategies import BuyAndHold, FixedWeights

D = Decimal
DATES = [
    date(2024, 1, 31),
    date(2024, 7, 31),
    date(2025, 1, 31),
    date(2025, 7, 31),
    date(2025, 12, 31),
]
SOURCE = Provenance(origin="test", verify_live=False)


def flat_costs(rate: str = "0.001") -> CostBook:
    commission = CommissionSchedule(
        id="flat", venues=None, asset_classes=[AssetClass.ETF], currency="EUR",
        kind="fraction_of_value", rate=D(rate), minimum=None, maximum_fraction_of_value=None,
        up_to_face=None, scope_note="test", source=SOURCE,
    )  # fmt: skip
    usd = commission.model_copy(update={"id": "flat_usd", "currency": "USD"})
    friction = FrictionInputs(
        liquid_market_cap_usd=D(0), liquid_spread_fraction=D(0), illiquid_spread_fraction=D(0),
        liquid_slippage_fraction=D(0), illiquid_slippage_fraction=D(0), impact_coefficient=D(0),
        impact_cap_fraction=D(0), liquid_half_spread_fraction=D(0),
        single_name_half_spread_fraction=D(0), liquid_underlyings=frozenset(), unverified=False,
        origin="test",
    )  # fmt: skip
    return CostBook([commission, usd], [], friction)


def identity(key: str, issuer: str) -> SecurityIdentity:
    return SecurityIdentity(
        key=key, kind=SecurityKind.ETF, isin=None, issuer=issuer, tracked_index=f"{key} index",
        listed=True,
    )  # fmt: skip


def instrument(key: str, issuer: str, *, currency: str = "EUR", income: str | None = None):
    return SimInstrument(
        key=key, venue="XETR", asset_class=AssetClass.ETF, currency=currency,
        security=identity(key, issuer), issuer_country=None, market_cap_usd=D(0),
        income_country=income,
    )  # fmt: skip


def series(*points: tuple[date, float]) -> tuple[Observation, ...]:
    return tuple(Observation(observed_on=d, available_on=d, value=v) for d, v in points)


A_PRICES = series(*zip(DATES, [100.0, 150.0, 150.0, 150.0, 150.0], strict=True))
B_PRICES = series(*zip(DATES, [100.0, 100.0, 100.0, 100.0, 100.0], strict=True))


@pytest.fixture(scope="module")
def es(repo_root) -> EsIrpfTaxPolicy:
    profile = load_profile(repo_root / "config", "es")
    assert profile.tax is not None
    return EsIrpfTaxPolicy(profile.tax)


def settings(**overrides) -> SimulationSettings:
    fields = {
        "base_currency": "EUR",
        "initial_capital": D(10000),
        "no_trade_band": D("0.005"),
        "max_price_age_days": 7,
        "max_fx_age_days": 7,
        "whole_units": True,
        "days_per_year": 365.25,
        "cost_multiplier": D(1),
    }
    fields.update(overrides)
    return SimulationSettings(**fields)


def simulator(es, panel: Panel | None = None, costs: CostBook | None = None, **kw) -> Simulator:
    return Simulator(
        [instrument("A", "Issuer A"), instrument("B", "Issuer B")],
        panel or Panel(prices={"A": A_PRICES, "B": B_PRICES}, income={}, fx={}),
        costs or flat_costs(),
        es,
        settings(**kw),
    )


HALF = {"A": D("0.5"), "B": D("0.5")}


def test_buy_and_hold_of_one_asset_is_liquidated_and_taxed_at_the_horizon(es):
    result = simulator(es).run(BuyAndHold("hold A", {"A": D(1)}), [DATES[0], DATES[-1]])
    # 100 units would cost 10,010 with the commission: scaled to 99 units, 9,909.90 paid.
    # At the horizon 99 units at 150: 14,850 less 14.85 -> 14,835.15; a gain of 4,925.25 is
    # taxed at 19 % (935.7975). Cash: 90.10 + 14,835.15 - 935.7975 = 13,989.4525.
    assert result.liquidation_value == D("13989.4525")
    assert result.taxes == (TaxPayment(2025, D("935.7975"), DATES[-1]),)
    assert result.costs == D("24.75")
    years = 700 / 365.25
    assert result.years == pytest.approx(years)
    assert result.log_growth == pytest.approx(math.log(13989.4525 / 10000) / years)
    assert result.nav == pytest.approx((10000 - 9.9, 90.1 + 99 * 150))
    assert result.trades == 2


def test_a_rotating_portfolio_pays_tax_during_the_horizon_that_buy_and_hold_defers(es):
    rotating = simulator(es).run(FixedWeights("50/50 rebalanced", HALF), DATES)
    holding = simulator(es).run(BuyAndHold("50/50 held", HALF), DATES)
    # Rebalanced. 31 Jan 2024: 49 A and 49 B (4,904.90 each with commission), 190.20 cash.
    # 31 Jul 2024: A has risen to 150; the account is worth 12,440.20 and each half 6,220.10:
    # sell 8 A (proceeds 1,198.80 against a cost of 800.80: a gain of 398.00) and buy 13 B
    # (1,301.30). 31 Jan 2025: no trade. 31 Jul 2025: 2024's tax, 398 x 19 % = 75.62, is due
    # (paid by 30 June); one B is sold to keep the halves (a loss of 0.20) and the tax is paid.
    # 31 Dec 2025: everything is sold; 2025's gains are -0.20 + 2,039.75 - 9.60 - 2.60 =
    # 2,027.35 and its tax 385.1965.
    assert rotating.taxes == (
        TaxPayment(2024, D("75.62"), date(2025, 7, 31)),
        TaxPayment(2025, D("385.1965"), DATES[-1]),
    )
    assert rotating.liquidation_value == D("11964.5335")
    # Held. Nothing is sold before the horizon, so 2024 owes nothing and the whole gain waits:
    # 2,437.75 on A less 9.80 on B, taxed 461.3105 at the horizon.
    assert holding.taxes == (TaxPayment(2025, D("461.3105"), DATES[-1]),)
    assert holding.liquidation_value == D("11966.6395")
    paid_early = [t for t in rotating.taxes if t.paid_on < DATES[-1]]
    assert paid_early
    assert not [t for t in holding.taxes if t.paid_on < DATES[-1]]
    summary_2024 = rotating.summaries[0]
    assert (summary_2024.tax_year, summary_2024.gains, summary_2024.payment_date) == (
        2024,
        D("398.0"),
        date(2025, 6, 30),
    )
    assert rotating.nav[1] == pytest.approx(87.7 + 41 * 150 + 62 * 100)


def test_the_no_trade_band_keeps_small_changes_off_the_market(es):
    drift = Panel(
        prices={
            "A": series(*zip(DATES[:3], [100.0, 100.4, 100.4], strict=True)),
            "B": series(*zip(DATES[:3], [100.0, 100.0, 100.0], strict=True)),
        },
        income={},
        fx={},
    )
    result = simulator(es, drift, whole_units=False).run(FixedWeights("50/50", HALF), DATES[:3])
    # The 0.4 % drift moves each target by about 0.2 %, inside the 0.5 % band: only the entry and
    # the exit trade.
    assert result.trades == 4


def test_income_is_withheld_at_source_and_taxed_with_a_credit(es):
    sim = Simulator(
        [instrument("US", "US Issuer", currency="USD", income="US")],
        Panel(
            prices={"US": series((DATES[0], 100.0), (DATES[1], 100.0), (DATES[-1], 100.0))},
            income={"US": series((date(2024, 3, 15), 2.0))},
            fx={
                "USD": series(
                    (DATES[0], 2.0), (date(2024, 3, 15), 2.0), (DATES[1], 2.0), (DATES[-1], 2.0)
                )
            },
        ),
        flat_costs(),
        es,
        settings(),
    )
    result = sim.run(BuyAndHold("hold", {"US": D(1)}), [DATES[0], DATES[1], DATES[-1]])
    # 199 units bought (20,000 USD of capital at 2 USD per EUR, less the commission). The
    # dividend of 2 USD a unit is 199 EUR gross, 29.85 withheld in the United States.
    assert result.withheld == D("29.85")
    first = result.summaries[0]
    # 2024: income 199, tax 37.81 at 19 %, credit 29.85: 7.96 due by 30 June 2025.
    assert (first.income, first.tax_before_credit, first.foreign_tax_credit) == (
        D("199"),
        D("37.81"),
        D("29.85"),
    )
    assert result.taxes[0] == TaxPayment(2024, D("7.96"), DATES[-1])


def test_tax_with_too_little_cash_is_paid_by_selling(es):
    class SwitchThenHold:
        name = "switch"

        def targets(self, view: MarketView, book: Book):
            if view.as_of == DATES[0]:
                return {"A": D(1)}
            if view.as_of == DATES[1]:
                return {"B": D(1)}
            return None

    for band in ("0.005", "0.5"):
        # With a wide band the holding is not trimmed by the rebalance, so B is sold to pay.
        result = simulator(es, no_trade_band=D(band)).run(SwitchThenHold(), DATES)
        # The 2024 gain on A is taxed in July 2025 although the strategy holds.
        assert result.taxes[0].tax_year == 2024
        assert result.taxes[0].paid_on == date(2025, 7, 31)
        assert result.nav[3] < result.nav[2]


def test_the_cost_multiplier_scales_every_leg(es):
    once = simulator(es).run(BuyAndHold("hold", {"A": D(1)}), [DATES[0], DATES[-1]])
    twice = simulator(es, cost_multiplier=D(2)).run(
        BuyAndHold("hold", {"A": D(1)}), [DATES[0], DATES[-1]]
    )
    assert twice.costs > once.costs
    assert twice.liquidation_value < once.liquidation_value


def test_fractional_units_buy_the_whole_target(es):
    result = simulator(es, whole_units=False).run(
        BuyAndHold("hold", {"A": D(1)}), [DATES[0], DATES[-1]]
    )
    assert result.nav[0] == pytest.approx(10000 - 10000 * 0.001 / 1.001, rel=1e-6)


# ── refusals: nothing is priced or held that cannot be ──


def test_a_held_position_without_a_price_stops_the_run(es):
    gap = Panel(prices={"A": series((DATES[0], 100.0)), "B": B_PRICES}, income={}, fx={})
    with pytest.raises(SimulationError, match="A has no price on 2024-07-31"):
        simulator(es, gap).run(FixedWeights("A", {"A": D(1)}), DATES[:2])


def test_a_price_published_later_is_not_seen():
    late = Panel(
        prices={"A": (Observation(observed_on=DATES[0], available_on=DATES[1], value=100.0),)},
        income={},
        fx={},
    )
    assert MarketView(late, DATES[0]).prices("A") == ()
    assert MarketView(late, DATES[1]).prices("A") == ((DATES[0], 100.0),)
    revised = Panel(
        prices={
            "A": (
                Observation(observed_on=DATES[0], available_on=DATES[0], value=100.0),
                Observation(observed_on=DATES[0], available_on=DATES[1], value=101.0),
            )
        },
        income={},
        fx={},
    )
    assert MarketView(revised, DATES[0]).prices("A") == ((DATES[0], 100.0),)
    assert MarketView(revised, DATES[1]).prices("A") == ((DATES[0], 101.0),)


@pytest.mark.parametrize(
    ("weights", "message"),
    [
        ({"A": D("0.7"), "B": D("0.4")}, "no leverage"),
        ({"A": D("-0.1")}, "no shorting"),
        ({"C": D("0.5")}, r"does not hold: \['C'\]"),
    ],
)
def test_weights_outside_the_mandate_are_refused(es, weights, message):
    with pytest.raises(SimulationError, match=message):
        simulator(es).run(FixedWeights("bad", weights), DATES[:2])


def test_a_leg_the_cost_model_cannot_price_stops_the_run(es):
    unpriced = Simulator(
        [
            SimInstrument(
                key="J",
                venue="XTKS",
                asset_class=AssetClass.ETF,
                currency="EUR",
                security=identity("J", "J"),
                issuer_country=None,
                market_cap_usd=None,
                income_country=None,
            )
        ],
        Panel(prices={"J": series((DATES[0], 100.0), (DATES[1], 100.0))}, income={}, fx={}),
        CostBook([], [], flat_costs()._friction),
        es,
        settings(),
    )
    with pytest.raises(
        SimulationError, match=r"cannot price a buy of J on 2024-01-31 \(no_schedule\)"
    ):
        unpriced.run(BuyAndHold("j", {"J": D(1)}), DATES[:2])


def test_a_foreign_price_without_a_fresh_rate_stops_the_run(es):
    sim = Simulator(
        [instrument("US", "US Issuer", currency="USD")],
        Panel(
            prices={"US": series((DATES[0], 100.0), (DATES[1], 100.0))},
            income={},
            fx={"USD": series((date(2023, 12, 1), 1.1))},
        ),
        flat_costs(),
        es,
        settings(),
    )
    with pytest.raises(SimulationError, match="no reference rate for USD on 2024-01-31"):
        sim.run(BuyAndHold("us", {"US": D(1)}), DATES[:2])


def test_a_run_needs_rising_dates_and_unique_instruments(es):
    with pytest.raises(SimulationError, match="strictly rising"):
        simulator(es).run(BuyAndHold("x", HALF), [DATES[0]])
    with pytest.raises(SimulationError, match="strictly rising"):
        simulator(es).run(BuyAndHold("x", HALF), [DATES[1], DATES[0]])
    with pytest.raises(ValueError, match="unique"):
        Simulator(
            [instrument("A", "X"), instrument("A", "X")],
            Panel({}, {}, {}),
            flat_costs(),
            es,
            settings(),
        )
    with pytest.raises(ValueError, match="carries the tax identity of B"):
        SimInstrument(
            key="A", venue="XETR", asset_class=AssetClass.ETF, currency="EUR",
            security=identity("B", "X"), issuer_country=None, market_cap_usd=None,
            income_country=None,
        )  # fmt: skip


def test_a_minimum_commission_with_fractional_units_still_buys(es):
    commission = flat_costs()._commissions[0].model_copy(update={"minimum": D("50")})
    costs = CostBook([commission], [], flat_costs()._friction)
    result = simulator(es, costs=costs, whole_units=False).run(
        BuyAndHold("hold", {"A": D(1)}), [DATES[0], DATES[-1]]
    )
    # 10,000 buys about 99.5 units: the 50 minimum leaves 9,950 of notional.
    assert result.nav[0] == pytest.approx(9950, abs=0.01)


def test_raising_cash_sells_in_proportion_then_whole_positions(es):
    from obliskiq.research.simulate import _State
    from obliskiq.tax.lots import LotEngine

    sim = simulator(es)
    engine = LotEngine(es, [identity("A", "Issuer A"), identity("B", "Issuer B")])
    state = _State(cash=D(0), next_year=2024, units={"A": D(10), "B": D(10)})
    for key in ("A", "B"):
        engine.acquire(key, on=DATES[0], quantity=D(10), cost=D(1000))
    # 400 to raise at A 150 and B 100: 240 from A (2 units) and 160 from B (2 units).
    sim._raise_cash(D(400), DATES[1], engine, state)
    assert state.units == {"A": D(8), "B": D(8)}
    assert state.cash == D(300) - D("0.3") + D(200) - D("0.2")
    # More than the account holds: everything is sold before the run gives up.
    with pytest.raises(SimulationError, match="cannot pay"):
        sim._raise_cash(D(100000), DATES[1], engine, state)
    assert state.units == {}
    empty = _State(cash=D(0), next_year=2024)
    with pytest.raises(SimulationError, match="cannot pay"):
        sim._raise_cash(D(1), DATES[1], engine, empty)


def test_the_proportional_pass_falls_back_to_whole_positions_when_costs_leave_it_short(es):
    from obliskiq.research.simulate import _State
    from obliskiq.tax.lots import LotEngine

    sim = simulator(es)
    engine = LotEngine(es, [identity("A", "Issuer A"), identity("B", "Issuer B")])
    state = _State(cash=D(0), next_year=2024, units={"B": D(3)})
    engine.acquire("B", on=DATES[0], quantity=D(3), cost=D(300))
    # 200 to raise at 100 a unit: 2 units raise 199.80 after the commission, so the whole
    # position goes.
    sim._raise_cash(D(200), DATES[1], engine, state)
    assert state.units == {}
    assert state.cash == D(300) - D("0.2") - D("0.1")


def test_a_sale_that_costs_more_than_it_raises_stops_the_run(es):
    expensive = CommissionSchedule(
        id="dear", venues=None, asset_classes=[AssetClass.ETF], currency="EUR", kind="per_share",
        rate=D(200), minimum=None, maximum_fraction_of_value=None, up_to_face=None,
        scope_note="test", source=SOURCE,
    )  # fmt: skip
    sim = simulator(es, costs=CostBook([expensive], [], flat_costs()._friction))
    from obliskiq.research.simulate import _State
    from obliskiq.tax.lots import LotEngine

    engine = LotEngine(es, [identity("A", "Issuer A")])
    engine.acquire("A", on=DATES[0], quantity=D(1), cost=D(100))
    with pytest.raises(SimulationError, match="costs more than it raises"):
        sim._sell("A", D(1), DATES[0], engine, _State(cash=D(0), next_year=2024, units={"A": D(1)}))


def test_a_target_without_any_price_yet_stops_the_run(es):
    late = Panel(prices={"A": A_PRICES, "B": series((DATES[1], 100.0))}, income={}, fx={})
    with pytest.raises(SimulationError, match="B has no price on 2024-01-31"):
        simulator(es, late).run(FixedWeights("50/50", HALF), DATES[:2])


def test_a_stale_price_does_not_mark_a_position(es):
    stale = Panel(
        prices={"A": series((DATES[0], 100.0), (date(2024, 7, 20), 140.0)), "B": B_PRICES},
        income={},
        fx={},
    )
    with pytest.raises(
        SimulationError, match="A has no price on 2024-07-31 within the allowed age"
    ):
        simulator(es, stale).run(FixedWeights("A", {"A": D(1)}), DATES[:2])
    assert simulator(es, stale, max_price_age_days=11).price("A", DATES[1]) == D(140)


def test_a_view_keeps_the_latest_vintage_whatever_the_order_of_the_rows():
    rows = (
        Observation(observed_on=DATES[0], available_on=DATES[1], value=101.0),
        Observation(observed_on=DATES[0], available_on=DATES[0], value=100.0),
    )
    view = MarketView(Panel(prices={"A": rows}, income={}, fx={}), DATES[1])
    assert view.prices("A") == ((DATES[0], 101.0),)


def test_the_result_reports_period_returns_and_the_tax_paid(es):
    result = simulator(es).run(FixedWeights("50/50", HALF), DATES)
    assert len(result.period_log_returns) == len(DATES) - 1
    assert result.period_log_returns[0] == pytest.approx(math.log(result.nav[1] / result.nav[0]))
    assert result.tax_paid == D("75.62") + D("385.1965")
    assert result.cost_labels == frozenset({"tax_not_modelled"})
