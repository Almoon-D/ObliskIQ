"""
Unit tests for Transaction Cost Model
"""

import pytest
import sys
import os

# Add parent directory to path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules.analytics.transaction_costs import TransactionCostModel

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


def test_liquid_stock_costs():
    """Test costs for liquid large-cap stock"""

    model = TransactionCostModel()

    # Example: $10,000 position in AAPL (market cap ~$3T)
    costs = model.calculate_total_cost(
        symbol="AAPL",
        position_size_dollars=10000,
        market_cap=3_000_000_000_000,  # $3 trillion
        is_option=False,
    )

    print("\n=== LIQUID STOCK TEST (AAPL) ===")
    print(f"Position Size: $10,000")
    print(f"Spread Cost: ${costs['spread_cost']:.2f}")
    print(f"Slippage Cost: ${costs['slippage_cost']:.2f}")
    print(f"Commission: ${costs['commission']:.2f}")
    print(f"Total Cost: ${costs['total_cost']:.2f} ({costs['total_cost_pct']:.3f}%)")
    print(f"Liquidity Tier: {costs['liquidity_tier']}")

    # For liquid stocks, costs should be low (0.1% spread + 0.1% slippage = 0.2%)
    assert (
        costs["total_cost_pct"] < 0.25
    ), f"Costs too high for liquid stock: {costs['total_cost_pct']}%"
    assert costs["liquidity_tier"] == "liquid"

    # Calculate breakeven
    entry_price = 180.0
    breakeven_price, breakeven_move = model.calculate_breakeven_move(
        entry_price, costs["total_cost_pct"]
    )
    print(f"\nBreakeven Analysis:")
    print(f"Entry Price: ${entry_price:.2f}")
    print(f"Breakeven Price: ${breakeven_price:.2f}")
    print(f"Breakeven Move: {breakeven_move:.3f}%")

    print("\n✅ Liquid stock test PASSED")


def test_illiquid_stock_costs():
    """Test costs for illiquid small-cap stock"""

    model = TransactionCostModel()

    # Example: $10,000 position in microcap (market cap $500M)
    costs = model.calculate_total_cost(
        symbol="SMALLCAP",
        position_size_dollars=10000,
        market_cap=500_000_000,  # $500 million
        is_option=False,
    )

    print("\n=== ILLIQUID STOCK TEST (Small Cap) ===")
    print(f"Position Size: $10,000")
    print(f"Spread Cost: ${costs['spread_cost']:.2f}")
    print(f"Slippage Cost: ${costs['slippage_cost']:.2f}")
    print(f"Commission: ${costs['commission']:.2f}")
    print(f"Total Cost: ${costs['total_cost']:.2f} ({costs['total_cost_pct']:.3f}%)")
    print(f"Liquidity Tier: {costs['liquidity_tier']}")

    # For illiquid stocks, costs should be higher (0.5% spread + 0.5% slippage = 1%)
    assert (
        costs["total_cost_pct"] > 0.9
    ), f"Costs too low for illiquid stock: {costs['total_cost_pct']}%"
    assert costs["liquidity_tier"] == "illiquid"

    # Calculate breakeven
    entry_price = 25.0
    breakeven_price, breakeven_move = model.calculate_breakeven_move(
        entry_price, costs["total_cost_pct"]
    )
    print(f"\nBreakeven Analysis:")
    print(f"Entry Price: ${entry_price:.2f}")
    print(f"Breakeven Price: ${breakeven_price:.2f}")
    print(f"Breakeven Move: {breakeven_move:.3f}%")

    print("\n✅ Illiquid stock test PASSED")


def test_options_costs():
    """Test costs for options trade"""

    model = TransactionCostModel()

    # Example: 10 contracts ($1000 per contract = $10,000 total)
    costs = model.calculate_total_cost(
        symbol="AAPL",
        position_size_dollars=10000,
        market_cap=3_000_000_000_000,
        is_option=True,
        num_contracts=10,
    )

    print("\n=== OPTIONS TRADE TEST ===")
    print(f"Position Size: $10,000 (10 contracts)")
    print(f"Spread Cost: ${costs['spread_cost']:.2f}")
    print(f"Slippage Cost: ${costs['slippage_cost']:.2f}")
    print(f"Commission: ${costs['commission']:.2f} (${costs['commission']/10:.2f} per contract)")
    print(f"Total Cost: ${costs['total_cost']:.2f} ({costs['total_cost_pct']:.3f}%)")

    # Options should have $0.65 per contract
    expected_commission = 10 * 0.65
    assert abs(costs["commission"] - expected_commission) < 0.01

    print("\n✅ Options test PASSED")


def test_position_adjustment():
    """Test adjusting position size for costs"""

    model = TransactionCostModel()

    target_position = 10000

    costs = model.calculate_total_cost(
        symbol="AAPL",
        position_size_dollars=target_position,
        market_cap=3_000_000_000_000,
        is_option=False,
    )

    adjusted_position = model.adjust_position_for_costs(target_position, costs)

    print("\n=== POSITION ADJUSTMENT TEST ===")
    print(f"Target Position: ${target_position:.2f}")
    print(f"Transaction Costs: ${costs['total_cost']:.2f}")
    print(f"Adjusted Position: ${adjusted_position:.2f}")
    print(f"Reduction: ${target_position - adjusted_position:.2f}")

    assert adjusted_position < target_position
    assert adjusted_position > 0

    print("\n✅ Position adjustment test PASSED")


if __name__ == "__main__":
    test_liquid_stock_costs()
    test_illiquid_stock_costs()
    test_options_costs()
    test_position_adjustment()
    print("\n🎉 ALL TRANSACTION COST TESTS PASSED!")
