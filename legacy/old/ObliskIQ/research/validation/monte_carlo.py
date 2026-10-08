"""
Monte Carlo Simulation for Backtesting
======================================
Tests statistical robustness of trading strategies through random resampling.

Nassim Taleb: "Monte Carlo is about seeing reality and the surrounding possible worlds."

Purpose:
- Understand range of possible outcomes
- Calculate confidence intervals
- Test sensitivity to trade sequence
- Identify luck vs skill
"""

from typing import List, Optional
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
from modules.analytics.backtester import BacktestResults

from ObliskIQ.modules.exceptions import InsufficientDataError


@dataclass
class MonteCarloResults:
    """Results from Monte Carlo simulation"""

    simulations_run: int

    # Return distribution
    mean_return_pct: float
    median_return_pct: float
    std_return_pct: float
    min_return_pct: float
    max_return_pct: float

    # Confidence intervals (return)
    return_5th_percentile: float  # 90% confidence lower bound
    return_95th_percentile: float  # 90% confidence upper bound
    return_25th_percentile: float  # 50% confidence lower bound
    return_75th_percentile: float  # 50% confidence upper bound

    # Sharpe ratio distribution
    mean_sharpe: float
    median_sharpe: float
    std_sharpe: float
    sharpe_5th_percentile: float
    sharpe_95th_percentile: float

    # Max drawdown distribution
    mean_max_dd_pct: float
    median_max_dd_pct: float
    worst_max_dd_pct: float  # Worst case
    max_dd_5th_percentile: float
    max_dd_95th_percentile: float

    # Risk metrics
    probability_profit_pct: float  # % of simulations that were profitable
    probability_ruin_pct: float  # % of simulations that lost >50%

    # Original backtest comparison
    original_return_pct: float
    original_percentile: float  # Where original falls in distribution

    # All simulation results (for plotting)
    all_returns: List[float] = field(default_factory=list)
    all_sharpes: List[float] = field(default_factory=list)
    all_max_dds: List[float] = field(default_factory=list)
    all_equity_curves: List[pd.Series] = field(default_factory=list)

    def print_summary(self):
        """Print Monte Carlo analysis summary"""
        print("\n" + "=" * 80)
        print("MONTE CARLO SIMULATION RESULTS")
        print("=" * 80)
        print(f"Simulations Run:           {self.simulations_run:,}")
        print()
        print("RETURN DISTRIBUTION:")
        print(f"  Mean:                    {self.mean_return_pct:.2f}%")
        print(f"  Median:                  {self.median_return_pct:.2f}%")
        print(f"  Std Dev:                 {self.std_return_pct:.2f}%")
        print(
            f"  Range:                   {self.min_return_pct:.2f}% to {self.max_return_pct:.2f}%"
        )
        print()
        print("CONFIDENCE INTERVALS (Return):")
        print(
            f"  90% CI:                  {self.return_5th_percentile:.2f}% to {self.return_95th_percentile:.2f}%"
        )
        print(
            f"  50% CI:                  {self.return_25th_percentile:.2f}% to {self.return_75th_percentile:.2f}%"
        )
        print()
        print("SHARPE RATIO DISTRIBUTION:")
        print(f"  Mean:                    {self.mean_sharpe:.2f}")
        print(f"  Median:                  {self.median_sharpe:.2f}")
        print(
            f"  90% CI:                  {self.sharpe_5th_percentile:.2f} to {self.sharpe_95th_percentile:.2f}"
        )
        print()
        print("MAX DRAWDOWN DISTRIBUTION:")
        print(f"  Mean:                    {self.mean_max_dd_pct:.2f}%")
        print(f"  Median:                  {self.median_max_dd_pct:.2f}%")
        print(f"  Worst Case (95th):       {self.worst_max_dd_pct:.2f}%")
        print()
        print("RISK ASSESSMENT:")
        print(f"  Probability of Profit:   {self.probability_profit_pct:.1f}%")
        print(f"  Probability of Ruin (>50% loss): {self.probability_ruin_pct:.1f}%")
        print()
        print("ORIGINAL BACKTEST COMPARISON:")
        print(f"  Original Return:         {self.original_return_pct:.2f}%")
        print(f"  Original Percentile:     {self.original_percentile:.1f}th percentile")

        # Interpretation
        if self.original_percentile > 90:
            print("  ⚠️  WARNING: Original result in top 10% - may be lucky outlier")
        elif self.original_percentile > 75:
            print("  ⚠️  CAUTION: Original result in top 25% - possible luck component")
        elif self.original_percentile >= 40 and self.original_percentile <= 60:
            print("  ✅ GOOD: Original result near median - representative performance")
        elif self.original_percentile < 25:
            print("  ⚠️  CONCERN: Original result in bottom 25% - unlucky or poor strategy")

        if self.probability_profit_pct < 50:
            print("  🚨 CRITICAL: <50% probability of profit - strategy not viable")
        elif self.probability_profit_pct < 70:
            print("  ⚠️  WARNING: <70% probability of profit - high risk")
        else:
            print("  ✅ GOOD: >70% probability of profit - robust strategy")

        if self.probability_ruin_pct > 5:
            print(
                f"  🚨 CRITICAL: {self.probability_ruin_pct:.1f}% risk of catastrophic loss (>50%)"
            )

        print("=" * 80 + "\n")


class MonteCarloSimulator:
    """
    Monte Carlo simulation for backtest validation.

    Randomly resamples trades to generate distribution of possible outcomes.

    Usage:
        simulator = MonteCarloSimulator(backtest_results)
        mc_results = simulator.run(n_simulations=10000)
        mc_results.print_summary()
    """

    def __init__(self, backtest_results: BacktestResults, initial_capital: float = 100000.0):
        """
        Initialize Monte Carlo simulator.

        Args:
            backtest_results: Original backtest results to resample from
            initial_capital: Starting capital for simulations
        """
        self.backtest_results = backtest_results
        self.initial_capital = initial_capital

        # Extract trade returns for resampling
        self.trade_returns_pct = [
            trade.pnl_pct for trade in backtest_results.trades if trade.pnl_pct is not None
        ]

        if len(self.trade_returns_pct) < 10:
            raise InsufficientDataError(
                message="Insufficient trades for Monte Carlo simulation",
                required_points=10,
                available_points=len(self.trade_returns_pct),
            )

    def run(
        self, n_simulations: int = 10000, n_trades_per_sim: Optional[int] = None
    ) -> MonteCarloResults:
        """
        Run Monte Carlo simulation.

        Args:
            n_simulations: Number of simulations to run
            n_trades_per_sim: Number of trades per simulation (default: same as original)

        Returns:
            Monte Carlo simulation results
        """
        if n_trades_per_sim is None:
            n_trades_per_sim = len(self.trade_returns_pct)

        print(f"\n🎲 Running Monte Carlo Simulation: {n_simulations:,} iterations")
        print(f"   Resampling from {len(self.trade_returns_pct)} original trades")
        print(f"   {n_trades_per_sim} trades per simulation\n")

        all_returns = []
        all_sharpes = []
        all_max_dds = []
        all_equity_curves = []

        for i in range(n_simulations):
            if (i + 1) % 1000 == 0:
                print(f"  Completed {i + 1:,}/{n_simulations:,} simulations...")

            # Randomly resample trades with replacement
            resampled_returns = np.random.choice(
                self.trade_returns_pct, size=n_trades_per_sim, replace=True
            )

            # Build equity curve from resampled trades
            equity_curve = self._build_equity_curve(resampled_returns)

            # Calculate metrics
            total_return = ((equity_curve[-1] / self.initial_capital) - 1) * 100
            sharpe = self._calculate_sharpe(resampled_returns)
            max_dd = self._calculate_max_drawdown(equity_curve)

            all_returns.append(total_return)
            all_sharpes.append(sharpe)
            all_max_dds.append(max_dd)
            all_equity_curves.append(pd.Series(equity_curve))

        print(f"  ✅ Completed {n_simulations:,} simulations\n")

        # Calculate statistics
        all_returns = np.array(all_returns)
        all_sharpes = np.array(all_sharpes)
        all_max_dds = np.array(all_max_dds)

        # Return statistics
        mean_return = np.mean(all_returns)
        median_return = np.median(all_returns)
        std_return = np.std(all_returns)
        min_return = np.min(all_returns)
        max_return = np.max(all_returns)

        # Confidence intervals
        return_5th = np.percentile(all_returns, 5)
        return_95th = np.percentile(all_returns, 95)
        return_25th = np.percentile(all_returns, 25)
        return_75th = np.percentile(all_returns, 75)

        # Sharpe statistics
        mean_sharpe = np.mean(all_sharpes)
        median_sharpe = np.median(all_sharpes)
        std_sharpe = np.std(all_sharpes)
        sharpe_5th = np.percentile(all_sharpes, 5)
        sharpe_95th = np.percentile(all_sharpes, 95)

        # Max drawdown statistics
        mean_max_dd = np.mean(all_max_dds)
        median_max_dd = np.median(all_max_dds)
        worst_max_dd = np.min(all_max_dds)  # Most negative
        max_dd_5th = np.percentile(all_max_dds, 5)  # 5th percentile = worst case
        max_dd_95th = np.percentile(all_max_dds, 95)  # Best case

        # Risk metrics
        probability_profit = (np.sum(all_returns > 0) / n_simulations) * 100
        probability_ruin = (np.sum(all_returns < -50) / n_simulations) * 100

        # Original backtest comparison
        original_return = self.backtest_results.total_return_pct
        original_percentile = (np.sum(all_returns <= original_return) / n_simulations) * 100

        return MonteCarloResults(
            simulations_run=n_simulations,
            mean_return_pct=mean_return,
            median_return_pct=median_return,
            std_return_pct=std_return,
            min_return_pct=min_return,
            max_return_pct=max_return,
            return_5th_percentile=return_5th,
            return_95th_percentile=return_95th,
            return_25th_percentile=return_25th,
            return_75th_percentile=return_75th,
            mean_sharpe=mean_sharpe,
            median_sharpe=median_sharpe,
            std_sharpe=std_sharpe,
            sharpe_5th_percentile=sharpe_5th,
            sharpe_95th_percentile=sharpe_95th,
            mean_max_dd_pct=mean_max_dd,
            median_max_dd_pct=median_max_dd,
            worst_max_dd_pct=worst_max_dd,
            max_dd_5th_percentile=max_dd_5th,
            max_dd_95th_percentile=max_dd_95th,
            probability_profit_pct=probability_profit,
            probability_ruin_pct=probability_ruin,
            original_return_pct=original_return,
            original_percentile=original_percentile,
            all_returns=all_returns.tolist(),
            all_sharpes=all_sharpes.tolist(),
            all_max_dds=all_max_dds.tolist(),
            all_equity_curves=all_equity_curves,
        )

    def _build_equity_curve(self, trade_returns_pct: np.ndarray) -> np.ndarray:
        """Build equity curve from sequence of trade returns"""
        equity = [self.initial_capital]

        for ret_pct in trade_returns_pct:
            # Apply return to current equity
            new_equity = equity[-1] * (1 + ret_pct / 100)
            equity.append(new_equity)

        return np.array(equity)

    def _calculate_sharpe(self, returns_pct: np.ndarray, risk_free_rate: float = 0.0) -> float:
        """Calculate Sharpe ratio from returns"""
        if len(returns_pct) == 0:
            return 0.0

        mean_return = np.mean(returns_pct)
        std_return = np.std(returns_pct)

        if std_return == 0:
            return 0.0

        # Annualize (assume daily returns)
        sharpe = ((mean_return - risk_free_rate) / std_return) * np.sqrt(252)
        return float(sharpe)

    def _calculate_max_drawdown(self, equity_curve: np.ndarray) -> float:
        """Calculate maximum drawdown from equity curve"""
        if len(equity_curve) == 0:
            return 0.0

        # Calculate running maximum
        running_max = np.maximum.accumulate(equity_curve)

        # Calculate drawdown at each point
        drawdowns = ((equity_curve - running_max) / running_max) * 100

        # Return maximum drawdown (most negative)
        max_dd = np.min(drawdowns)
        return float(max_dd)


def quick_monte_carlo(
    backtest_results: BacktestResults, n_simulations: int = 1000, print_results: bool = True
) -> MonteCarloResults:
    """
    Quick Monte Carlo analysis (convenience function).

    Args:
        backtest_results: Original backtest results
        n_simulations: Number of simulations
        print_results: Whether to print summary

    Returns:
        Monte Carlo results
    """
    simulator = MonteCarloSimulator(backtest_results)
    results = simulator.run(n_simulations=n_simulations)

    if print_results:
        results.print_summary()

    return results
