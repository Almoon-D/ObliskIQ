"""The simulator's trailing protective stop against a hand calculation: the stop trails the
highest close since entry by a multiple of the recent daily volatility, fills at the first close
at or below it (a gap is borne in full), and the cash waits for the next rebalance date. Costs
are a flat 0.1 % commission with no friction, so every figure can be worked out on paper.

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
from obliskiq.jurisdiction import load_profile
from obliskiq.jurisdiction.tax_es_irpf import EsIrpfTaxPolicy
from obliskiq.quant.costs import CostBook, FrictionInputs
from obliskiq.research.simulate import (
    IndexedSeries,
    MarketView,
    Panel,
    PanelIndex,
    Pricing,
    SimInstrument,
    SimulationSettings,
    Simulator,
    StopRule,
)
from obliskiq.research.strategies import FixedWeights

D = Decimal
SOURCE = Provenance(origin="test", verify_live=False)
DATES = [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 28)]
RULE = StopRule(volatility_sessions=4, horizon_sessions=1, multiple=1.0)


def flat_costs() -> CostBook:
    commission = CommissionSchedule(
        id="flat", venues=None, asset_classes=[AssetClass.ETF], currency="EUR",
        kind="fraction_of_value", rate=D("0.001"), minimum=None, maximum_fraction_of_value=None,
        up_to_face=None, scope_note="test", source=SOURCE,
    )  # fmt: skip
    friction = FrictionInputs(
        liquid_market_cap_usd=D(0), liquid_spread_fraction=D(0), illiquid_spread_fraction=D(0),
        liquid_slippage_fraction=D(0), illiquid_slippage_fraction=D(0), impact_coefficient=D(0),
        impact_cap_fraction=D(0), liquid_half_spread_fraction=D(0),
        single_name_half_spread_fraction=D(0), liquid_underlyings=frozenset(), unverified=False,
        origin="test",
    )  # fmt: skip
    return CostBook([commission], [], friction)


INSTRUMENT = SimInstrument(
    key="S", venue="XETR", asset_class=AssetClass.ETF, currency="EUR",
    security=SecurityIdentity(
        key="S", kind=SecurityKind.ETF, isin=None, issuer="Issuer", tracked_index="S index",
        listed=True,
    ),
    issuer_country=None, market_cap_usd=D(0), income_country=None,
)  # fmt: skip


def rows(*points: tuple[date, float], late: frozenset[date] = frozenset()) -> tuple:
    return tuple(
        Observation(
            observed_on=d,
            available_on=date.fromordinal(d.toordinal() + 1) if d in late else d,
            value=v,
        )
        for d, v in points
    )


#: January moves +-2 % a day, so the stop sits one standard deviation below the peak.
JANUARY = [
    (date(2024, 1, 25), 100.0),
    (date(2024, 1, 26), 102.0),
    (date(2024, 1, 29), 100.0),
    (date(2024, 1, 30), 102.0),
    (date(2024, 1, 31), 100.0),
]
REST = [
    (date(2024, 2, 26), 103.0),
    (date(2024, 2, 27), 104.0),
    (date(2024, 2, 28), 104.0),
    (date(2024, 2, 29), 105.0),
    (date(2024, 3, 28), 105.0),
]


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


def run(es, prices: tuple, *, stop: StopRule | None = RULE, **overrides):
    simulator = Simulator(
        [INSTRUMENT], Panel({"S": prices}, {}, {}), flat_costs(), es, settings(**overrides)
    )
    sales: list[tuple[date, Decimal]] = []
    original = simulator._sell

    def spy(key, quantity, on, engine, state):
        sales.append((on, simulator.price(key, on)))
        return original(key, quantity, on, engine, state)

    simulator._sell = spy
    return simulator.run(FixedWeights("all in", {"S": D(1)}), DATES, stop=stop), sales


# ── the hand calculation ──
#
# 31 Jan: 99 units at 100 cost 9,909.90 (100 would cost 10,010); cash 90.10. The last four daily
# log returns are +-ln(1.02): their sample standard deviation is ln(1.02) * sqrt(4 / 3), and the
# stop is that far below the peak since entry (100).
# 1 Feb, 104: above 100 e^-d (97.74); the peak becomes 104.
# 2 Feb, 101: at or below 104 e^-d (101.65): the 99 units are sold at 101, raising 9,999 - 9.999
# = 9,989.001 (a gain of 79.101). Cash 10,079.101.
# 29 Feb, 105: 95 units cost 9,984.975; cash 94.126. No close after it reaches the new stop.
# 28 Mar: the 95 units raise 9,975 - 9.975 = 9,965.025 (a loss of 19.95). 2024's gains are
# 59.151 and its tax at 19 % is 11.23869. Liquidation value: 94.126 + 9,965.025 - 11.23869.
DISTANCE = math.log(1.02) * math.sqrt(4 / 3)
AFTER_TAX = D("10047.91231")
BEFORE_TAX = D("10059.151")


def test_the_stop_trails_the_peak_and_sells_at_the_close_that_reaches_it(es):
    prices = rows(*JANUARY, (date(2024, 2, 1), 104.0), (date(2024, 2, 2), 101.0), *REST)
    result, sales = run(es, prices)
    assert result.stops == 1
    assert sales[0] == (date(2024, 2, 2), D("101.0"))
    assert result.trades == 4
    assert result.liquidation_value == AFTER_TAX
    assert RULE.distance([v for _, v in JANUARY]) == pytest.approx(DISTANCE)
    assert 101.0 <= 104.0 * math.exp(-DISTANCE) < 102.0


def test_before_tax_the_same_trades_leave_the_gain_untaxed(es):
    prices = rows(*JANUARY, (date(2024, 2, 1), 104.0), (date(2024, 2, 2), 101.0), *REST)
    result, _ = run(es, prices, taxed=False)
    assert result.liquidation_value == BEFORE_TAX


def test_a_gap_through_the_stop_is_borne_in_full(es):
    prices = rows(*JANUARY, (date(2024, 2, 1), 104.0), (date(2024, 2, 2), 90.0), *REST)
    result, sales = run(es, prices)
    assert result.stops == 1
    # Sold at the close of the gap, not at the stop (about 101.65).
    assert sales[0] == (date(2024, 2, 2), D("90.0"))


def test_without_a_stop_nothing_is_sold_before_the_horizon(es):
    prices = rows(*JANUARY, (date(2024, 2, 1), 104.0), (date(2024, 2, 2), 90.0), *REST)
    result, sales = run(es, prices, stop=None)
    assert result.stops == 0
    assert [day for day, _ in sales] == [date(2024, 3, 28)]


def test_a_close_published_after_its_day_cannot_trigger_the_stop(es):
    prices = rows(
        *JANUARY,
        (date(2024, 2, 1), 104.0),
        (date(2024, 2, 2), 90.0),
        *REST,
        late=frozenset({date(2024, 2, 2)}),
    )
    result, _ = run(es, prices)
    assert result.stops == 0


def test_no_stop_while_the_opening_close_is_not_yet_public(es):
    # Bought on 31 Jan at 30 Jan's close; 31 Jan's close is published a day late, so the peak
    # since entry is unknown during February.
    prices = rows(
        (date(2024, 1, 24), 102.0),
        *JANUARY,
        (date(2024, 2, 1), 104.0),
        (date(2024, 2, 2), 90.0),
        *REST,
        late=frozenset({date(2024, 1, 31)}),
    )
    result, _ = run(es, prices)
    assert result.stops == 0


def test_no_stop_while_the_history_is_shorter_than_its_window(es):
    prices = rows(*JANUARY[-2:], (date(2024, 2, 1), 104.0), (date(2024, 2, 2), 90.0), *REST)
    result, _ = run(es, prices)
    assert result.stops == 0


def test_a_sale_that_costs_more_than_it_raises_is_not_counted_as_a_stop(es):
    prices = rows(*JANUARY, (date(2024, 2, 1), 104.0), (date(2024, 2, 2), 101.0), *REST)
    simulator = Simulator([INSTRUMENT], Panel({"S": prices}, {}, {}), flat_costs(), es, settings())
    simulator._sell = lambda *_args: False
    result = simulator.run(FixedWeights("all in", {"S": D(1)}), DATES, stop=RULE)
    assert result.stops == 0


# ── the rule ──


def test_the_distance_scales_with_the_horizon_and_the_multiple():
    closes = [v for _, v in JANUARY]
    wide = StopRule(volatility_sessions=4, horizon_sessions=9, multiple=2.0)
    assert wide.distance(closes) == pytest.approx(2 * 3 * DISTANCE)


def test_the_distance_needs_enough_closes_that_move():
    assert RULE.distance([100.0, 101.0, 100.0, 101.0]) is None
    assert RULE.distance([100.0] * 5) is None


@pytest.mark.parametrize(
    ("sessions", "horizon", "multiple"), [(1, 1, 1.0), (4, 0, 1.0), (4, 1, 0.0)]
)
def test_a_stop_rule_refuses_nonsense(sessions, horizon, multiple):
    with pytest.raises(ValueError, match="stop needs"):
        StopRule(sessions, horizon, multiple)


# ── the series and the view ──


def test_an_indexed_series_counts_and_slices_by_day():
    series = IndexedSeries(rows(*JANUARY))
    assert series.through(date(2024, 1, 29)) == 3
    assert series.through(date(2024, 1, 1)) == 0
    assert [row.value for row in series.after(date(2024, 1, 26), date(2024, 1, 30))] == [
        100.0,
        102.0,
    ]


def test_the_view_gives_a_fresh_close_in_the_base_currency_or_none():
    usd = SimInstrument(
        key="U", venue="XETR", asset_class=AssetClass.ETF, currency="USD",
        security=SecurityIdentity(
            key="U", kind=SecurityKind.ETF, isin=None, issuer="I", tracked_index="U index",
            listed=True,
        ),
        issuer_country=None, market_cap_usd=D(0), income_country=None,
    )  # fmt: skip
    panel = Panel(
        {"S": rows(*JANUARY), "U": rows((date(2024, 1, 31), 110.0))},
        {},
        {"USD": rows((date(2024, 1, 31), 1.1))},
    )
    pricing = Pricing({"S": INSTRUMENT, "U": usd}, PanelIndex(panel), settings())
    assert MarketView(pricing, date(2024, 1, 31)).close("S") == D("100.0")
    assert MarketView(pricing, date(2024, 1, 31)).close("U") == D("100")
    assert MarketView(pricing, date(2024, 3, 1)).close("S") is None
