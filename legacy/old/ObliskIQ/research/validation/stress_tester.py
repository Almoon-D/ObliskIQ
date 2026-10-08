"""
Stress Testing for Portfolio Resilience
========================================
Tests portfolio performance under extreme market conditions.

Ray Dalio: "The biggest mistake investors make is to believe that what happened in
the recent past is likely to persist. They assume that something that was a good
investment will continue to be a good investment."

Nassim Taleb: "The Black Swan problem applies to all domains: financial markets,
war, epidemics, and the spread of ideas."
"""

from typing import Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from ObliskIQ.modules.exceptions import DataValidationError


class StressScenario(Enum):
    """Pre-defined stress scenarios"""

    MARKET_CRASH_1987 = "1987_crash"  # -20% in 1 day
    FLASH_CRASH_2010 = "2010_flash"  # -9% intraday
    COVID_CRASH_2020 = "2020_covid"  # -34% over 1 month
    TECH_BUBBLE_2000 = "2000_tech"  # -78% over 2.5 years (NASDAQ)
    FINANCIAL_CRISIS_2008 = "2008_gfc"  # -57% over 17 months
    INFLATION_SPIKE_1970s = "1970s_inflation"  # Stagflation
    SECTOR_ROTATION = "sector_rotation"  # Growth→Value or vice versa
    LIQUIDITY_CRISIS = "liquidity_crisis"  # Massive spreads, slippage
    CUSTOM = "custom"  # User-defined scenario


@dataclass
class StressTestConfig:
    """Configuration for stress test scenario"""

    scenario: StressScenario
    market_shock_pct: float  # Overall market decline (%)
    sector_shocks: Dict[str, float] = field(default_factory=dict)  # Sector-specific shocks
    volatility_multiplier: float = 3.0  # VIX spike multiplier
    liquidity_impact_pct: float = 2.0  # Additional slippage (%)
    correlation_spike: float = 0.95  # All correlations spike to this level
    duration_days: int = 1  # How long shock persists


# Pre-configured stress scenarios
STRESS_SCENARIOS: Dict[StressScenario, StressTestConfig] = {
    StressScenario.MARKET_CRASH_1987: StressTestConfig(
        scenario=StressScenario.MARKET_CRASH_1987,
        market_shock_pct=-20.0,
        sector_shocks={"TECH": -15.0, "FINANCE": -25.0, "CONSUMER": -18.0},
        volatility_multiplier=5.0,
        liquidity_impact_pct=3.0,
        correlation_spike=0.98,
        duration_days=1,
    ),
    StressScenario.FLASH_CRASH_2010: StressTestConfig(
        scenario=StressScenario.FLASH_CRASH_2010,
        market_shock_pct=-9.0,
        sector_shocks={"TECH": -12.0, "FINANCE": -10.0},
        volatility_multiplier=4.0,
        liquidity_impact_pct=5.0,  # Liquidity evaporates
        correlation_spike=0.99,
        duration_days=1,
    ),
    StressScenario.COVID_CRASH_2020: StressTestConfig(
        scenario=StressScenario.COVID_CRASH_2020,
        market_shock_pct=-34.0,
        sector_shocks={"TRAVEL": -60.0, "HOSPITALITY": -55.0, "TECH": -25.0, "HEALTHCARE": -15.0},
        volatility_multiplier=4.5,
        liquidity_impact_pct=2.5,
        correlation_spike=0.90,
        duration_days=30,
    ),
    StressScenario.TECH_BUBBLE_2000: StressTestConfig(
        scenario=StressScenario.TECH_BUBBLE_2000,
        market_shock_pct=-45.0,
        sector_shocks={"TECH": -78.0, "TELECOM": -80.0, "FINANCE": -30.0},
        volatility_multiplier=3.5,
        liquidity_impact_pct=2.0,
        correlation_spike=0.85,
        duration_days=900,  # ~2.5 years
    ),
    StressScenario.FINANCIAL_CRISIS_2008: StressTestConfig(
        scenario=StressScenario.FINANCIAL_CRISIS_2008,
        market_shock_pct=-57.0,
        sector_shocks={"FINANCE": -80.0, "REAL_ESTATE": -75.0, "CONSUMER": -50.0, "TECH": -45.0},
        volatility_multiplier=4.0,
        liquidity_impact_pct=4.0,
        correlation_spike=0.95,
        duration_days=510,  # 17 months
    ),
    StressScenario.INFLATION_SPIKE_1970s: StressTestConfig(
        scenario=StressScenario.INFLATION_SPIKE_1970s,
        market_shock_pct=-25.0,
        sector_shocks={
            "TECH": -35.0,
            "CONSUMER_DISCRETIONARY": -40.0,
            "UTILITIES": -10.0,
            "COMMODITIES": +50.0,  # Commodities benefit
        },
        volatility_multiplier=2.5,
        liquidity_impact_pct=1.5,
        correlation_spike=0.70,
        duration_days=1825,  # 5 years
    ),
    StressScenario.SECTOR_ROTATION: StressTestConfig(
        scenario=StressScenario.SECTOR_ROTATION,
        market_shock_pct=-5.0,
        sector_shocks={
            "TECH": -20.0,  # Growth out of favor
            "FINANCE": +10.0,  # Value in favor
            "UTILITIES": +8.0,
            "CONSUMER_STAPLES": +5.0,
        },
        volatility_multiplier=1.5,
        liquidity_impact_pct=1.0,
        correlation_spike=0.60,
        duration_days=90,
    ),
    StressScenario.LIQUIDITY_CRISIS: StressTestConfig(
        scenario=StressScenario.LIQUIDITY_CRISIS,
        market_shock_pct=-15.0,
        sector_shocks={"FINANCE": -30.0, "REAL_ESTATE": -25.0},
        volatility_multiplier=5.0,
        liquidity_impact_pct=10.0,  # Cannot exit positions easily
        correlation_spike=0.99,
        duration_days=7,
    ),
}


@dataclass
class StressTestResult:
    """Results from stress test"""

    scenario: StressScenario
    scenario_name: str

    # Portfolio impact
    starting_value: float
    ending_value: float
    portfolio_loss_pct: float
    portfolio_loss_dollars: float

    # Position-level impacts
    position_impacts: List[Dict]  # Per-position breakdown
    worst_position: Optional[Dict] = None
    best_position: Optional[Dict] = None

    # Risk metrics
    max_drawdown_pct: float = 0.0
    volatility_during_stress: float = 0.0
    correlation_impact: float = 0.0

    # Risk limit violations
    violations: List[str] = field(default_factory=list)
    margin_call: bool = False
    forced_liquidation: bool = False

    # Liquidity impact
    total_slippage_cost: float = 0.0
    exit_impossible_positions: List[str] = field(default_factory=list)


class PortfolioStressTester:
    """
    Tests portfolio resilience under extreme market conditions.

    Features:
    - Historical stress scenarios (1987, 2008, 2020 crashes)
    - Custom scenario definition
    - Position-level impact analysis
    - Correlation shock modeling
    - Liquidity stress testing
    """

    def __init__(
        self, portfolio: Dict, risk_limits: Optional[Dict] = None  # Current portfolio holdings
    ):
        """
        Initialize stress tester.

        Args:
            portfolio: Dict with holdings (ticker → quantity, entry_price, sector)
            risk_limits: Optional risk limits to validate against
        """
        self.portfolio = portfolio
        self.risk_limits = risk_limits or {}

    def run_stress_test(
        self, scenario: StressScenario, custom_config: Optional[StressTestConfig] = None
    ) -> StressTestResult:
        """
        Run stress test on portfolio.

        Args:
            scenario: Pre-defined scenario to test
            custom_config: Optional custom scenario configuration

        Returns:
            Stress test results
        """
        # Get scenario config
        config = custom_config if custom_config else STRESS_SCENARIOS.get(scenario)
        if not config:
            valid_scenarios = [s.value for s in StressScenario]
            raise DataValidationError(
                message=f"Unknown stress scenario",
                field_name="scenario",
                invalid_value=scenario.value if isinstance(scenario, StressScenario) else str(scenario),
                expected_type=f"one of {valid_scenarios}",
            )

        # Calculate starting portfolio value
        starting_value = sum(
            pos["quantity"] * pos["current_price"] for pos in self.portfolio.values()
        )

        # Apply stress to each position
        position_impacts = []
        total_loss = 0.0
        total_slippage = 0.0
        violations = []

        for ticker, position in self.portfolio.items():
            impact = self._calculate_position_impact(
                ticker=ticker, position=position, config=config
            )
            position_impacts.append(impact)
            total_loss += impact["loss_dollars"]
            total_slippage += impact["slippage_cost"]

            # Check for violations
            if impact["loss_pct"] < -50.0:
                violations.append(f"{ticker}: {impact['loss_pct']:.1f}% loss (>50% threshold)")

        # Calculate ending value
        ending_value = starting_value + total_loss
        portfolio_loss_pct = (total_loss / starting_value) * 100 if starting_value > 0 else 0

        # Find worst/best positions
        worst_position = min(position_impacts, key=lambda x: x["loss_pct"])
        best_position = max(position_impacts, key=lambda x: x["loss_pct"])

        # Check for margin call (portfolio drops >30%)
        margin_call = portfolio_loss_pct < -30.0
        forced_liquidation = portfolio_loss_pct < -50.0

        if margin_call:
            violations.append(
                f"MARGIN CALL: Portfolio loss {portfolio_loss_pct:.1f}% exceeds -30% threshold"
            )
        if forced_liquidation:
            violations.append(
                f"FORCED LIQUIDATION: Portfolio loss {portfolio_loss_pct:.1f}% exceeds -50% threshold"
            )

        # Check liquidity - positions that cannot be exited
        exit_impossible = [
            pos["ticker"]
            for pos in position_impacts
            if pos["slippage_cost"] > pos["position_value"] * 0.1  # 10%+ slippage
        ]

        return StressTestResult(
            scenario=scenario,
            scenario_name=config.scenario.value,
            starting_value=starting_value,
            ending_value=ending_value,
            portfolio_loss_pct=portfolio_loss_pct,
            portfolio_loss_dollars=total_loss,
            position_impacts=position_impacts,
            worst_position=worst_position,
            best_position=best_position,
            max_drawdown_pct=portfolio_loss_pct,  # Simplified
            violations=violations,
            margin_call=margin_call,
            forced_liquidation=forced_liquidation,
            total_slippage_cost=total_slippage,
            exit_impossible_positions=exit_impossible,
        )

    def _calculate_position_impact(
        self, ticker: str, position: Dict, config: StressTestConfig
    ) -> Dict:
        """Calculate impact of stress scenario on single position"""
        current_price = position["current_price"]
        quantity = position["quantity"]
        sector = position.get("sector", "UNKNOWN")
        position_value = current_price * quantity

        # Apply market shock
        shock_pct = config.market_shock_pct

        # Apply sector-specific shock (overrides market if defined)
        if sector in config.sector_shocks:
            shock_pct = config.sector_shocks[sector]

        # Calculate stressed price
        stressed_price = current_price * (1 + shock_pct / 100)
        stressed_value = stressed_price * quantity

        # Calculate loss
        loss_dollars = stressed_value - position_value
        loss_pct = (loss_dollars / position_value) * 100 if position_value > 0 else 0

        # Calculate liquidity impact (additional slippage when trying to exit)
        slippage_cost = abs(stressed_value) * (config.liquidity_impact_pct / 100)

        return {
            "ticker": ticker,
            "sector": sector,
            "position_value": position_value,
            "shock_pct": shock_pct,
            "stressed_price": stressed_price,
            "stressed_value": stressed_value,
            "loss_dollars": loss_dollars,
            "loss_pct": loss_pct,
            "slippage_cost": slippage_cost,
            "total_impact": loss_dollars - slippage_cost,
        }

    def run_all_scenarios(self) -> Dict[str, StressTestResult]:
        """Run all pre-defined stress scenarios"""
        results = {}

        for scenario in [
            StressScenario.MARKET_CRASH_1987,
            StressScenario.FLASH_CRASH_2010,
            StressScenario.COVID_CRASH_2020,
            StressScenario.FINANCIAL_CRISIS_2008,
            StressScenario.SECTOR_ROTATION,
            StressScenario.LIQUIDITY_CRISIS,
        ]:
            result = self.run_stress_test(scenario)
            results[scenario.value] = result

        return results

    def print_stress_report(self, result: StressTestResult):
        """Print formatted stress test report"""
        print(f"\n{'='*80}")
        print(f"STRESS TEST: {result.scenario_name.upper()}")
        print(f"{'='*80}")
        print(f"Starting Portfolio Value: ${result.starting_value:,.2f}")
        print(f"Ending Portfolio Value:   ${result.ending_value:,.2f}")
        print(
            f"Portfolio Loss:           ${result.portfolio_loss_dollars:,.2f} ({result.portfolio_loss_pct:.2f}%)"
        )
        print(f"Total Slippage Cost:      ${result.total_slippage_cost:,.2f}")

        print(f"\n{'─'*80}")
        print("POSITION-LEVEL IMPACTS:")
        print(f"{'─'*80}")
        print(f"{'Ticker':<10} {'Sector':<15} {'Value':<12} {'Loss %':<10} {'Loss $':<12}")
        print(f"{'─'*80}")

        for pos in sorted(result.position_impacts, key=lambda x: x["loss_pct"]):
            print(
                f"{pos['ticker']:<10} {pos['sector']:<15} "
                f"${pos['position_value']:>10,.0f} "
                f"{pos['loss_pct']:>8.1f}% "
                f"${pos['loss_dollars']:>10,.0f}"
            )

        if result.worst_position:
            print(
                f"\n🔻 Worst Position: {result.worst_position['ticker']} "
                f"({result.worst_position['loss_pct']:.1f}% loss)"
            )

        if result.violations:
            print(f"\n{'─'*80}")
            print("⚠️  RISK VIOLATIONS:")
            print(f"{'─'*80}")
            for violation in result.violations:
                print(f"  ❌ {violation}")

        if result.exit_impossible_positions:
            print(f"\n🚫 LIQUIDITY CRISIS - Cannot exit positions (>10% slippage):")
            for ticker in result.exit_impossible_positions:
                print(f"  • {ticker}")

        if result.margin_call:
            print(f"\n🚨 MARGIN CALL TRIGGERED")
        if result.forced_liquidation:
            print(f"\n💀 FORCED LIQUIDATION REQUIRED")

        print(f"{'='*80}\n")

    def print_summary_report(self, all_results: Dict[str, StressTestResult]):
        """Print summary of all stress scenarios"""
        print(f"\n{'='*80}")
        print("COMPREHENSIVE STRESS TEST SUMMARY")
        print(f"{'='*80}")
        print(f"{'Scenario':<30} {'Loss %':<12} {'Loss $':<15} {'Violations':<12}")
        print(f"{'─'*80}")

        for scenario_name, result in all_results.items():
            violations_count = len(result.violations)
            emoji = "✅" if violations_count == 0 else "⚠️" if violations_count < 3 else "🚨"

            print(
                f"{emoji} {scenario_name:<27} "
                f"{result.portfolio_loss_pct:>9.1f}% "
                f"${result.portfolio_loss_dollars:>12,.0f} "
                f"{violations_count} issues"
            )

        print(f"{'='*80}\n")
