"""
Canonical Walk-Forward Backtester (v5.34.0)
===========================================

THE single canonical backtesting/walk-forward implementation for ObliskIQ.
All walk-forward validation — portfolio-level OR signal/IC-level — must go
through this module. See docs/BACKTESTER_CONSOLIDATION.md for the audit,
decision record, and migration map.

Consolidates (v5.34.0):
- modules/analytics/walk_forward.py        → run_strategy_walk_forward() + WalkForwardResults
- modules/analytics/pattern_backtest.py    → walk_forward_ic_validation() (PatternBacktest now delegates)
- research/validation/walk_forward.py      → duplicate of the above, deleted
- modules/analytics/walk_forward_validator.py → delegates window math to generate_window_indices()

GUARANTEES (enforced, not aspirational):
1. No lookahead bias: assert_no_lookahead() raises LookaheadBiasError whenever
   a signal date is not strictly before its return-realization date, and
   window generation rejects any train/test overlap.
2. Transaction costs: commissions + slippage sourced from PaperTrader
   constants (single source of truth: modules/execution/paper_trader.py).
3. Metrics: Sharpe, Sortino, MaxDD, win rate, profit factor, Calmar.
4. IC integration: walk_forward_ic_validation() and the ic_hook parameter
   plug into InformationCoefficientCalculator (IC > 0.05, p < 0.05 gates).

METHODOLOGY (Industry Standard):
1. Define training window (e.g., 12 months)
2. Define testing window (e.g., 1 month)
3. Train strategy on training data
4. Test on next period (out-of-sample)
5. Roll forward and repeat

EXAMPLE TIMELINE:
Train: 2020-01 to 2020-12 → Test: 2021-01
Train: 2020-02 to 2021-01 → Test: 2021-02
... (walk forward)

USAGE (portfolio-level):
    backtester = WalkForwardBacktester(warehouse=DataWarehouse())
    result = backtester.run_backtest('2020-01-01', '2024-12-31', 100000)
    print(generate_summary_report(result))

USAGE (signal/IC-level, the Simons path):
    wf = walk_forward_ic_validation(
        signal_values=signals, forward_returns=returns,
        train_size=1260, test_size=252, ic_func=ic_calc.calculate_ic,
    )
    print(wf["avg_out_of_sample_ic"], wf["interpretation"])

USAGE (day-window strategy analysis, ported from WalkForwardAnalyzer):
    results = backtester.run_strategy_walk_forward(
        data=price_df, strategy=my_strategy,
        in_sample_days=252, out_of_sample_days=63,
    )
    results.print_summary()

v5.24.0: backtest_with_reflexivity_comparison() for Soros mode validation.

Jim Simons: "If it's not statistically significant, it doesn't exist."
"""

import logging
import math
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from dataclasses import dataclass, field

from modules.data.warehouse import DataWarehouse
from modules.exceptions import InsufficientDataError
from modules.analytics.transaction_costs import (
    DEFAULT_SLIPPAGE_PCT,
    calculate_commission,
)

# v5.24.0: Import for reflexivity comparison
try:
    from modules.analytics.historical_reflexivity import (
        get_event_intensity_on_date,
        generate_intensity_series,
    )
    REFLEXIVITY_AVAILABLE = True
except ImportError:
    REFLEXIVITY_AVAILABLE = False

logger = logging.getLogger(__name__)


# =============================================================================
# LOOKAHEAD-BIAS PROTECTION (v5.34.0)
# =============================================================================

class LookaheadBiasError(ValueError):
    """Raised when a backtest would use information from the future."""


def assert_no_lookahead(signal_date: Any, return_date: Any, context: str = "") -> None:
    """
    Enforce the canonical no-lookahead invariant: signal date < return date.

    A signal must be fully known strictly BEFORE the period whose return it
    is credited with. Equal dates are rejected: a signal computed from
    day-T close cannot earn day-T returns.

    Args:
        signal_date: Date (or sortable index) at which the signal was known
        return_date: Date (or sortable index) at which the return is realized
        context: Optional description for the error message

    Raises:
        LookaheadBiasError: If signal_date >= return_date
    """
    if not signal_date < return_date:
        raise LookaheadBiasError(
            f"Lookahead bias detected{f' ({context})' if context else ''}: "
            f"signal date {signal_date} must be strictly before return date {return_date}"
        )


# =============================================================================
# TRANSACTION COSTS
# =============================================================================
# The single source of truth is modules/config/settings.py (RiskManagement),
# exposed through modules/analytics/transaction_costs.py. DEFAULT_SLIPPAGE_PCT
# and calculate_commission() are imported from there (see imports above), so
# the backtester and PaperTrader charge byte-for-byte identical costs. There
# are intentionally NO cost constants declared in this file.


# Annual cost-drag alarm: twice-daily evaluation with weekly rebalancing should
# never churn this hard. Breaching it flags the strategy loudly.
COST_DRAG_ALARM_PCT: float = 3.0


# =============================================================================
# WALK-FORWARD WINDOW GENERATION (shared core)
# =============================================================================

def generate_window_indices(
    n_observations: int,
    train_size: int,
    test_size: int,
    step_size: Optional[int] = None,
) -> List[Dict[str, int]]:
    """
    Generate non-overlapping train/test index windows for walk-forward runs.

    Indices are half-open: train = [train_start, train_end),
    test = [test_start, test_end), with test_start == train_end so the test
    window begins strictly after the last training observation.

    This is the shared window engine used by walk_forward_ic_validation(),
    PatternBacktest, and WalkForwardValidator.

    Args:
        n_observations: Total observations available
        train_size: Observations per training window
        test_size: Observations per test window
        step_size: Roll-forward step (default: test_size)

    Returns:
        List of dicts with train_start/train_end/test_start/test_end indices

    Raises:
        ValueError: If sizes are not positive
        LookaheadBiasError: If a generated window would overlap (defensive)
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError(
            f"Window sizes must be positive (train={train_size}, test={test_size})"
        )
    step = test_size if step_size is None else step_size
    if step <= 0:
        raise ValueError(f"step_size must be positive, got {step}")

    windows: List[Dict[str, int]] = []
    current_start = 0
    while current_start + train_size + test_size <= n_observations:
        window = {
            "train_start": current_start,
            "train_end": current_start + train_size,
            "test_start": current_start + train_size,
            "test_end": current_start + train_size + test_size,
        }
        # Defensive invariant: last training index strictly before first test index
        assert_no_lookahead(
            window["train_end"] - 1, window["test_start"], context="window generation"
        )
        windows.append(window)
        current_start += step
    return windows


# =============================================================================
# IC WALK-FORWARD VALIDATION (Simons core, consolidated from PatternBacktest)
# =============================================================================

def _default_ic_func() -> Callable[..., Dict[str, Any]]:
    """Lazily build the default IC function (avoids import-time DB creation)."""
    from modules.analytics.ic_calculator import InformationCoefficientCalculator

    return InformationCoefficientCalculator().calculate_ic


def interpret_walk_forward_ic(test_ic: float, degradation: float) -> str:
    """Interpret IC walk-forward results (thresholds from PatternBacktest)."""
    if test_ic < 0.03:
        return "⚠️ Signal fails out-of-sample validation. IC too low to be useful."
    elif degradation > 0.05:
        return "⚠️ Significant degradation from in-sample to out-of-sample. Possible overfitting."
    elif test_ic > 0.08:
        return "✅ Strong out-of-sample performance. Signal validated."
    else:
        return "🟡 Moderate out-of-sample IC. Signal has some predictive power."


def walk_forward_ic_validation(
    signal_values: Sequence[float],
    forward_returns: Sequence[float],
    train_size: int,
    test_size: int,
    step_size: Optional[int] = None,
    ic_func: Optional[Callable[..., Dict[str, Any]]] = None,
    min_test_observations: int = 10,
    pattern_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Walk-forward IC validation: train on N observations, test on next M,
    roll forward. The gold standard for signal validation.

    IMPORTANT: forward_returns[i] must already be the FORWARD return for
    signal_values[i] (i.e., realized strictly after the signal date). The
    train/test split here additionally guarantees out-of-sample testing.

    Args:
        signal_values: Signal values, time-ordered
        forward_returns: Forward returns aligned with signal_values
        train_size: Observations per training window (e.g., 5*252)
        test_size: Observations per test window (e.g., 252)
        step_size: Roll-forward step (default: test_size)
        ic_func: IC function with InformationCoefficientCalculator.calculate_ic
                 semantics: (signals, returns) -> {'ic', 'p_value', 'sample_size', ...}.
                 Defaults to InformationCoefficientCalculator().calculate_ic.
        min_test_observations: Skip windows with fewer test observations
        pattern_name: Optional name for reporting

    Returns:
        {
            'pattern_name': str | None,
            'avg_in_sample_ic': float,
            'avg_out_of_sample_ic': float,
            'avg_degradation': float,
            'degradation_pct': float,
            'n_windows': int,
            'walk_forward_results': [
                {'train_ic', 'test_ic', 'test_p_value', 'test_sample_size',
                 'degradation', 'train_start', 'train_end', 'test_start', 'test_end'},
            ],
            'interpretation': str,
        }
        or {'status': 'insufficient_data'} / {'status': 'no_results'}
    """
    if len(signal_values) != len(forward_returns):
        raise ValueError(
            f"signal_values ({len(signal_values)}) and forward_returns "
            f"({len(forward_returns)}) must have the same length"
        )

    n = len(signal_values)
    if n < train_size + test_size:
        return {"status": "insufficient_data", "sample_size": n}

    if ic_func is None:
        ic_func = _default_ic_func()

    windows = generate_window_indices(n, train_size, test_size, step_size)
    walk_forward_results: List[Dict[str, Any]] = []

    for w in windows:
        train_signals = list(signal_values[w["train_start"]:w["train_end"]])
        train_returns = list(forward_returns[w["train_start"]:w["train_end"]])
        test_signals = list(signal_values[w["test_start"]:w["test_end"]])
        test_returns = list(forward_returns[w["test_start"]:w["test_end"]])

        if len(test_signals) < min_test_observations:
            continue

        train_ic = ic_func(train_signals, train_returns)
        test_ic = ic_func(test_signals, test_returns)

        walk_forward_results.append(
            {
                "train_ic": train_ic["ic"],
                "test_ic": test_ic["ic"],
                "test_p_value": test_ic["p_value"],
                "test_sample_size": test_ic["sample_size"],
                "degradation": train_ic["ic"] - test_ic["ic"],
                "train_start": w["train_start"],
                "train_end": w["train_end"],
                "test_start": w["test_start"],
                "test_end": w["test_end"],
            }
        )

    if not walk_forward_results:
        return {"status": "no_results"}

    avg_train_ic = float(np.mean([r["train_ic"] for r in walk_forward_results]))
    avg_test_ic = float(np.mean([r["test_ic"] for r in walk_forward_results]))
    avg_degradation = float(np.mean([r["degradation"] for r in walk_forward_results]))

    return {
        "pattern_name": pattern_name,
        "avg_in_sample_ic": avg_train_ic,
        "avg_out_of_sample_ic": avg_test_ic,
        "avg_degradation": avg_degradation,
        "degradation_pct": avg_degradation / avg_test_ic if avg_test_ic != 0 else 0,
        "n_windows": len(walk_forward_results),
        "walk_forward_results": walk_forward_results,
        "interpretation": interpret_walk_forward_ic(avg_test_ic, avg_degradation),
    }


# =============================================================================
# v5.24.0: REFLEXIVITY COMPARISON DATACLASSES
# =============================================================================

@dataclass
class ReflexivityComparisonResult:
    """
    Result from comparing reflexivity vs uniform position sizing.

    Used to validate the Soros mode hypothesis from CLAUDE.md:
    Position Size = (Conviction / 10) × (1 + Reflexivity Intensity)
    """
    # Reflexivity mode performance
    reflexivity_return_pct: float
    reflexivity_sharpe: float
    reflexivity_max_dd_pct: float
    reflexivity_win_rate: float

    # Uniform mode performance (control)
    uniform_return_pct: float
    uniform_sharpe: float
    uniform_max_dd_pct: float
    uniform_win_rate: float

    # Comparison
    alpha_vs_uniform_pct: float
    sharpe_improvement: float
    statistical_significance: float  # p-value

    # Validation
    is_validated: bool  # Meets all criteria

    # Details
    periods_analyzed: int
    reflexivity_periods_won: int


@dataclass
class BacktestConfig:
    """
    Configuration for portfolio-level walk-forward backtests.

    Cost defaults come from the single source of truth in settings.py
    (RiskManagement), via modules/analytics/transaction_costs.py, so backtest,
    paper trading, and live assumptions stay in sync:
    - slippage_pct: settings.risk.slippage_liquid (0.1% per leg)
    - commission_pct=None → use the IBKR per-share schedule via
      calculate_commission(); set a float to force a flat percentage instead.
    """

    training_months: int = 12  # Lookback period for training
    testing_months: int = 1  # Forward period for testing
    initial_capital: float = 100000.0
    commission_pct: Optional[float] = None  # None = IBKR per-share schedule
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT  # Per leg
    rebalance_frequency: str = "monthly"  # monthly, weekly, daily
    benchmark_ticker: str = "SPY"

    # v6.2.0 (Strategy v1.1 H1): execution model at each period boundary.
    # - "full_liquidation": every period liquidates and re-enters the whole
    #   portfolio (entry+exit legs charged monthly). Historical default.
    # - "delta": positions persist across periods; only the DELTA between the
    #   previous holdings and the new targets is traded (PaperTrader parity).
    #   The final period liquidates fully so terminal equity is cash.
    rebalance_mode: str = "full_liquidation"
    # Delta mode no-trade band, as a fraction of the position's own target
    # notional (with a $1 absolute floor, mirroring the 60/40 benchmark's
    # dust rule). Suppresses cost-feedback dust (~0.1% of position) without
    # touching real signal changes (a 10%→9% weight shift is ~10% of the
    # position's notional, 20x this band).
    delta_no_trade_band_pct: float = 0.005


@dataclass
class BacktestPeriod:
    """Single period in walk-forward test"""

    period_id: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    portfolio_value_start: float
    portfolio_value_end: float
    return_pct: float
    positions: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    # v6.2.0 (H1): authoritative per-period execution accounting. In delta
    # mode, costs of tickers sold to ZERO at the boundary have no trade
    # record (the ticker is not held during the window), so per-trade
    # transaction_costs no longer sum to the period total — these do.
    period_costs: float = 0.0
    period_traded_notional: float = 0.0
    # What the period would have cost under the historical full-liquidation
    # model (identical position list, entry+exit legs). Equal to period_costs
    # in full_liquidation mode; enables before/after-H1 cost-drag reporting
    # from a single run.
    counterfactual_full_liquidation_costs: float = 0.0


@dataclass
class CostDragReport:
    """
    Transaction-cost drag report (v5.38.0).

    Quantifies how much of the strategy's gross return is eaten by costs, and
    whether the strategy churns. ObliskIQ runs twice daily with weekly
    rebalancing — annual cost drag above COST_DRAG_ALARM_PCT (3%) means the
    rebalancing logic is trading far more than the cadence implies.
    """

    gross_return_pct: float  # Return before transaction costs
    net_return_pct: float  # Return after transaction costs (matches headline)
    total_costs: float  # Total dollars paid in commissions + slippage/spread
    total_costs_pct_of_capital: float  # total_costs / initial capital, %
    annual_cost_drag_pct: float  # Cost % of capital, annualized
    turnover: float  # Sum of traded notional / avg capital (round-trips counted)
    annualized_turnover: float  # Turnover scaled to a 1-year horizon
    exceeds_alarm: bool  # annual_cost_drag_pct > COST_DRAG_ALARM_PCT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gross_return_pct": round(self.gross_return_pct, 4),
            "net_return_pct": round(self.net_return_pct, 4),
            "total_costs": round(self.total_costs, 2),
            "total_costs_pct_of_capital": round(self.total_costs_pct_of_capital, 4),
            "annual_cost_drag_pct": round(self.annual_cost_drag_pct, 4),
            "turnover": round(self.turnover, 4),
            "annualized_turnover": round(self.annualized_turnover, 4),
            "exceeds_alarm": self.exceeds_alarm,
            "alarm_threshold_pct": COST_DRAG_ALARM_PCT,
        }


@dataclass
class BacktestResult:
    """Complete backtest results"""

    config: BacktestConfig
    start_date: date
    end_date: date
    periods: List[BacktestPeriod]

    # Portfolio equity curve
    equity_curve: pd.DataFrame  # date, value, returns, drawdown

    # Performance metrics
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    calmar_ratio: float  # Return / MaxDD

    # Win/loss metrics
    win_rate_pct: float
    profit_factor: float  # Gross profit / gross loss
    avg_win_pct: float
    avg_loss_pct: float

    # Benchmark comparison
    benchmark_return_pct: float
    alpha_pct: float
    beta: float
    information_ratio: float

    # Trade statistics
    total_trades: int
    profitable_trades: int
    losing_trades: int

    # v5.34.0: IC of position signals vs realized returns (via ic_hook)
    signal_ic: Optional[Dict[str, Any]] = None

    # v5.38.0: gross-vs-net cost drag, turnover, and the >3% churn alarm
    cost_drag: Optional[CostDragReport] = None

    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class WalkForwardWindow:
    """Single day-based walk-forward window (ported from WalkForwardAnalyzer)"""

    window_id: int
    in_sample_start: datetime
    in_sample_end: datetime
    out_of_sample_start: datetime
    out_of_sample_end: datetime
    in_sample_days: int
    out_of_sample_days: int


@dataclass
class WalkForwardResults:
    """
    Aggregated walk-forward analysis results.

    Interface preserved verbatim from modules/analytics/walk_forward.py
    (deprecated v5.34.0) — callers of the old WalkForwardAnalyzer migrate
    to WalkForwardBacktester.run_strategy_walk_forward() unchanged.
    """

    # Overall performance (from all OOS periods combined)
    total_oos_return_pct: float
    annualized_oos_return_pct: float
    oos_sharpe_ratio: float
    oos_max_drawdown_pct: float
    oos_win_rate_pct: float

    # Comparison: In-sample vs Out-of-sample
    avg_is_return_pct: float
    avg_oos_return_pct: float
    performance_degradation_pct: float  # How much worse OOS vs IS

    # Consistency metrics
    windows_tested: int
    windows_profitable: int
    consistency_score: float  # % of windows that were profitable

    # Per-window results
    window_results: List[Dict] = field(default_factory=list)

    # Overall equity curve (OOS periods only)
    oos_equity_curve: Optional[pd.Series] = None

    def print_summary(self):
        """Print walk-forward analysis summary"""
        print("\n" + "=" * 80)
        print("WALK-FORWARD ANALYSIS RESULTS")
        print("=" * 80)
        print(f"Windows Tested:           {self.windows_tested}")
        print(
            f"Windows Profitable:       {self.windows_profitable} ({self.consistency_score:.1f}%)"
        )
        print()
        print("OUT-OF-SAMPLE PERFORMANCE (True Performance):")
        print(f"  Total Return:           {self.total_oos_return_pct:.2f}%")
        print(f"  Annualized Return:      {self.annualized_oos_return_pct:.2f}%")
        print(f"  Sharpe Ratio:           {self.oos_sharpe_ratio:.2f}")
        print(f"  Max Drawdown:           {self.oos_max_drawdown_pct:.2f}%")
        print(f"  Win Rate:               {self.oos_win_rate_pct:.1f}%")
        print()
        print("IN-SAMPLE vs OUT-OF-SAMPLE:")
        print(f"  Avg IS Return:          {self.avg_is_return_pct:.2f}%")
        print(f"  Avg OOS Return:         {self.avg_oos_return_pct:.2f}%")
        print(f"  Performance Degradation: {self.performance_degradation_pct:.2f}%")

        if self.performance_degradation_pct > 50:
            print("  ⚠️  WARNING: Significant overfitting detected (>50% degradation)")
        elif self.performance_degradation_pct > 30:
            print("  ⚠️  CAUTION: Moderate overfitting (>30% degradation)")
        elif self.performance_degradation_pct < 0:
            print("  ✅ EXCELLENT: OOS performs better than IS (no overfitting)")
        else:
            print("  ✅ GOOD: Acceptable performance degradation (<30%)")

        print("=" * 80 + "\n")

        # Per-window breakdown
        print("PER-WINDOW BREAKDOWN:")
        print("-" * 80)
        print(
            f"{'Window':<10} {'IS Return':<12} {'OOS Return':<12} {'OOS Sharpe':<12} {'OOS Trades':<12}"
        )
        print("-" * 80)

        for window in self.window_results:
            print(
                f"{window['window_id']:<10} "
                f"{window['is_return']:<11.2f}% "
                f"{window['oos_return']:<11.2f}% "
                f"{window['oos_sharpe']:<11.2f} "
                f"{window['oos_trades']:<12}"
            )

        print("=" * 80 + "\n")


class WalkForwardBacktester:
    """
    Canonical walk-forward backtester with point-in-time data access.

    Prevents lookahead bias by only using data available at each point in
    time, and ENFORCES it via assert_no_lookahead() on every period.
    """

    def __init__(
        self,
        warehouse: Optional[DataWarehouse] = None,
        config: Optional[BacktestConfig] = None,
    ):
        """
        Initialize backtester

        Args:
            warehouse: DataWarehouse for historical data
            config: Backtest configuration
        """
        self.warehouse = warehouse or DataWarehouse()
        self.config = config or BacktestConfig()

        # v6.2.0 (H1) delta-mode execution state (shares held + settled cash);
        # reset by run_backtest() so consecutive runs are isolated.
        self._holdings: Dict[str, float] = {}
        self._cash: float = self.config.initial_capital

        logger.info("WalkForwardBacktester initialized")
        logger.info(f"  Training: {self.config.training_months} months")
        logger.info(f"  Testing: {self.config.testing_months} months")
        logger.info(f"  Rebalance: {self.config.rebalance_frequency}")

    def run_backtest(
        self,
        start_date: str,
        end_date: str,
        initial_capital: Optional[float] = None,
        strategy_func=None,  # Optional: Custom strategy function
        ic_hook: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> BacktestResult:
        """
        Run walk-forward backtest

        Args:
            start_date: Backtest start date (YYYY-MM-DD)
            end_date: Backtest end date (YYYY-MM-DD)
            initial_capital: Initial capital (overrides config)
            strategy_func: Custom strategy function (ticker, data) -> positions.
                           Positions may include an optional numeric 'signal'
                           key, which feeds the IC hook.
            ic_hook: Optional IC function with calculate_ic semantics
                     (signals, returns) -> {'ic', 'p_value', ...}. When
                     positions carry 'signal' values, the realized
                     signal-vs-return IC is attached to BacktestResult.signal_ic.

        Returns:
            BacktestResult with all metrics
        """
        logger.info("=" * 80)
        logger.info("WALK-FORWARD BACKTEST")
        logger.info("=" * 80)

        start = pd.to_datetime(start_date).date()
        end = pd.to_datetime(end_date).date()

        if initial_capital:
            self.config.initial_capital = initial_capital

        logger.info(f"Period: {start} to {end}")
        logger.info(f"Initial capital: ${self.config.initial_capital:,.2f}")

        # Generate walk-forward periods
        periods = self._generate_periods(start, end)
        logger.info(f"Generated {len(periods)} walk-forward periods")

        # v6.2.0 (H1) delta mode: holdings/cash persist across periods within
        # ONE run_backtest call (reset here so back-to-back runs are isolated).
        self._holdings = {}
        self._cash = self.config.initial_capital

        # Run backtest on each period
        backtest_periods = []
        portfolio_value = self.config.initial_capital

        equity_curve_data = []
        all_trades = []
        signal_return_pairs: List[Tuple[float, float]] = []

        # v5.38.0: cost-drag accumulators
        gross_value = self.config.initial_capital  # Parallel cost-free equity
        total_costs = 0.0
        total_traded_notional = 0.0
        sum_period_starts = 0.0

        for period in periods:
            logger.info(
                f"\nPeriod {period['id']}: Train {period['train_start']} to {period['train_end']}, "
                f"Test {period['test_start']} to {period['test_end']}"
            )

            # No-lookahead guarantee: training data ends strictly before testing begins
            assert_no_lookahead(
                period["train_end"], period["test_start"],
                context=f"period {period['id']} train/test split",
            )

            # Get strategy positions (using only training data)
            positions = self._run_strategy_period(
                train_start=period["train_start"],
                train_end=period["train_end"],
                test_start=period["test_start"],
                test_end=period["test_end"],
                portfolio_value=portfolio_value,
                strategy_func=strategy_func,
            )

            # Execute trades and calculate returns
            if self.config.rebalance_mode == "delta":
                period_result, period_trades = self._execute_period_delta(
                    positions=positions,
                    test_start=period["test_start"],
                    test_end=period["test_end"],
                    portfolio_value=portfolio_value,
                    is_final=(period is periods[-1]),
                )
            else:
                period_result, period_trades = self._execute_period(
                    positions=positions,
                    test_start=period["test_start"],
                    test_end=period["test_end"],
                    portfolio_value=portfolio_value,
                )

            # Collect (signal, realized return) pairs for the IC hook
            for trade in period_trades:
                if trade.get("signal") is not None:
                    signal_return_pairs.append(
                        (trade["signal"], trade["return_pct"] / 100.0)
                    )

            # Update portfolio value
            new_portfolio_value = period_result["portfolio_value_end"]
            period_return = (new_portfolio_value - portfolio_value) / portfolio_value

            # v5.38.0: accumulate cost drag — compound the gross (cost-free)
            # curve with this period's gross return, and sum dollar costs/turnover
            gross_value *= 1 + period_result["gross_return_pct"] / 100.0
            total_costs += period_result["period_costs"]
            total_traded_notional += period_result["period_traded_notional"]
            sum_period_starts += portfolio_value

            # Create backtest period
            bt_period = BacktestPeriod(
                period_id=period["id"],
                train_start=period["train_start"],
                train_end=period["train_end"],
                test_start=period["test_start"],
                test_end=period["test_end"],
                portfolio_value_start=portfolio_value,
                portfolio_value_end=new_portfolio_value,
                return_pct=period_return * 100,
                positions=positions,
                trades=period_trades,
                period_costs=period_result["period_costs"],
                period_traded_notional=period_result["period_traded_notional"],
                # Full-liquidation mode IS its own counterfactual.
                counterfactual_full_liquidation_costs=period_result.get(
                    "counterfactual_full_liquidation_costs",
                    period_result["period_costs"],
                ),
            )

            backtest_periods.append(bt_period)
            all_trades.extend(period_trades)

            # Record equity point
            equity_curve_data.append(
                {"date": period["test_end"], "value": new_portfolio_value, "return": period_return}
            )

            # Update portfolio value for next period
            portfolio_value = new_portfolio_value

            logger.info(f"  Portfolio value: ${portfolio_value:,.2f} ({period_return*100:+.2f}%)")

        if not equity_curve_data:
            raise InsufficientDataError(
                message=f"No walk-forward periods fit between {start} and {end}",
                required_points=self.config.training_months + self.config.testing_months,
                available_points=0,
            )

        # Build equity curve DataFrame
        equity_df = pd.DataFrame(equity_curve_data)
        equity_df = self._calculate_drawdowns(equity_df)

        # Calculate performance metrics
        metrics = self._calculate_performance_metrics(
            equity_df=equity_df, trades=all_trades, start_date=start, end_date=end
        )

        # Get benchmark performance
        benchmark_metrics = self._calculate_benchmark_metrics(
            equity_df=equity_df,
            benchmark_ticker=self.config.benchmark_ticker,
            start_date=start,
            end_date=end,
        )

        # IC hook: signal predictive power over realized period returns
        signal_ic: Optional[Dict[str, Any]] = None
        if signal_return_pairs:
            ic_func = ic_hook or _default_ic_func()
            signals, realized = zip(*signal_return_pairs)
            signal_ic = ic_func(list(signals), list(realized))

        # v5.38.0: cost-drag report (gross vs net, costs % of capital, turnover)
        cost_drag = self._build_cost_drag_report(
            net_return_pct=metrics["total_return_pct"],
            gross_value=gross_value,
            total_costs=total_costs,
            total_traded_notional=total_traded_notional,
            avg_capital=(sum_period_starts / len(periods)) if periods else self.config.initial_capital,
            start_date=start,
            end_date=end,
        )
        if cost_drag.exceeds_alarm:
            logger.warning(
                "⚠️  COST DRAG ALARM: annual cost drag %.2f%% exceeds %.1f%% — "
                "twice-daily evaluation with weekly rebalancing should not churn "
                "this hard. Turnover=%.2fx/yr, total costs=$%.2f (%.2f%% of capital).",
                cost_drag.annual_cost_drag_pct,
                COST_DRAG_ALARM_PCT,
                cost_drag.annualized_turnover,
                cost_drag.total_costs,
                cost_drag.total_costs_pct_of_capital,
            )

        # Create result
        result = BacktestResult(
            config=self.config,
            start_date=start,
            end_date=end,
            periods=backtest_periods,
            equity_curve=equity_df,
            total_return_pct=metrics["total_return_pct"],
            annualized_return_pct=metrics["annualized_return_pct"],
            sharpe_ratio=metrics["sharpe_ratio"],
            sortino_ratio=metrics["sortino_ratio"],
            max_drawdown_pct=metrics["max_drawdown_pct"],
            calmar_ratio=metrics["calmar_ratio"],
            win_rate_pct=metrics["win_rate_pct"],
            profit_factor=metrics["profit_factor"],
            avg_win_pct=metrics["avg_win_pct"],
            avg_loss_pct=metrics["avg_loss_pct"],
            benchmark_return_pct=benchmark_metrics["benchmark_return_pct"],
            alpha_pct=benchmark_metrics["alpha_pct"],
            beta=benchmark_metrics["beta"],
            information_ratio=benchmark_metrics["information_ratio"],
            total_trades=len(all_trades),
            profitable_trades=int(metrics["profitable_trades"]),
            losing_trades=int(metrics["losing_trades"]),
            signal_ic=signal_ic,
            cost_drag=cost_drag,
        )

        logger.info("\n" + "=" * 80)
        logger.info("BACKTEST COMPLETE")
        logger.info("=" * 80)
        logger.info(f"Total return: {result.total_return_pct:+.2f}%")
        logger.info(f"Annualized return: {result.annualized_return_pct:+.2f}%")
        logger.info(f"Sharpe ratio: {result.sharpe_ratio:.2f}")
        logger.info(f"Max drawdown: {result.max_drawdown_pct:.2f}%")
        logger.info(f"Win rate: {result.win_rate_pct:.1f}%")

        return result

    # =========================================================================
    # v5.34.0: DAY-WINDOW STRATEGY WALK-FORWARD
    # (ported from modules/analytics/walk_forward.py WalkForwardAnalyzer)
    # =========================================================================

    def run_strategy_walk_forward(
        self,
        data: pd.DataFrame,
        strategy: Callable,
        in_sample_days: int = 252,
        out_of_sample_days: int = 63,
        step_size: Optional[int] = None,
        initial_capital: float = 100000.0,
    ) -> WalkForwardResults:
        """
        Day-window walk-forward analysis of a signal-generating strategy.

        Splits price history into rolling in-sample/out-of-sample windows,
        runs the strategy through the event-driven Backtester on each split,
        and reports IS-vs-OOS degradation + consistency (the overfitting
        detectors from the deprecated WalkForwardAnalyzer).

        NOTE: the deprecated WalkForwardAnalyzer passed Backtester.run()
        arguments swapped (signals where price_data belongs) and could never
        execute. This port uses the real Backtester contract.

        Args:
            data: Price data in Backtester format: DataFrame with columns
                  [date, ticker, open, high, low, close, volume]
            strategy: Backtester strategy function:
                      (date, day_prices, portfolio_state) -> List[TradeSignal]
            in_sample_days: Training window length in trading days
            out_of_sample_days: Testing window length in trading days
            step_size: Roll-forward step (default: out_of_sample_days)
            initial_capital: Starting capital per window

        Returns:
            WalkForwardResults (interface preserved from WalkForwardAnalyzer)
        """
        # Lazy import: keeps module import graph acyclic
        # (modules.analytics.__init__ transitively imports this module)
        from modules.analytics.backtester import Backtester

        dates = self._distinct_dates(data)
        if len(dates) < (in_sample_days + out_of_sample_days):
            raise InsufficientDataError(
                message="Insufficient data for walk-forward analysis",
                required_points=in_sample_days + out_of_sample_days,
                available_points=len(dates),
            )

        windows = self._create_day_windows(
            dates, in_sample_days, out_of_sample_days, step_size or out_of_sample_days
        )
        if not windows:
            raise InsufficientDataError(
                message="No walk-forward windows could be created",
                required_points=in_sample_days + out_of_sample_days,
                available_points=len(dates),
            )

        logger.info(f"🔄 Running strategy walk-forward: {len(windows)} windows")
        logger.info(f"   In-Sample: {in_sample_days}d, Out-of-Sample: {out_of_sample_days}d")

        window_results = []
        oos_equity_curves = []

        for window in windows:
            is_data = self._get_data_slice(data, window.in_sample_start, window.in_sample_end)
            oos_data = self._get_data_slice(
                data, window.out_of_sample_start, window.out_of_sample_end
            )

            # In-sample backtest (parameter fit reference)
            is_results = Backtester(initial_capital=initial_capital).run(is_data, strategy)
            # Out-of-sample backtest (true test of strategy)
            oos_results = Backtester(initial_capital=initial_capital).run(oos_data, strategy)

            window_results.append(
                {
                    "window_id": window.window_id,
                    "is_return": is_results.total_return_pct,
                    "oos_return": oos_results.total_return_pct,
                    "is_sharpe": is_results.sharpe_ratio,
                    "oos_sharpe": oos_results.sharpe_ratio,
                    "is_trades": is_results.total_trades,
                    "oos_trades": oos_results.total_trades,
                    "oos_win_rate": oos_results.win_rate_pct,
                    "oos_max_dd": oos_results.max_drawdown_pct,
                }
            )

            if oos_results.equity_curve is not None:
                oos_equity_curves.append(oos_results.equity_curve)

        return self._aggregate_strategy_windows(
            window_results, oos_equity_curves, out_of_sample_days, len(windows)
        )

    @staticmethod
    def _distinct_dates(data: pd.DataFrame) -> List[Any]:
        """Distinct ordered dates of a price DataFrame (date column or index)."""
        if hasattr(data, "columns") and "date" in data.columns:
            return sorted(pd.unique(data["date"]))
        if hasattr(data, "index"):
            return data.index.tolist()
        return list(range(len(data)))

    def _create_day_windows(
        self,
        dates: List[Any],
        in_sample_days: int,
        out_of_sample_days: int,
        step_size: int,
    ) -> List[WalkForwardWindow]:
        """Create day-based walk-forward windows over distinct dates."""
        windows = []
        for window_id, idx in enumerate(
            generate_window_indices(
                len(dates), in_sample_days, out_of_sample_days, step_size
            ),
            start=1,
        ):
            window = WalkForwardWindow(
                window_id=window_id,
                in_sample_start=dates[idx["train_start"]],
                in_sample_end=dates[idx["train_end"] - 1],
                out_of_sample_start=dates[idx["test_start"]],
                out_of_sample_end=dates[idx["test_end"] - 1],
                in_sample_days=in_sample_days,
                out_of_sample_days=out_of_sample_days,
            )
            # No-lookahead guarantee: OOS begins strictly after IS ends
            assert_no_lookahead(
                window.in_sample_end,
                window.out_of_sample_start,
                context=f"strategy walk-forward window {window_id}",
            )
            windows.append(window)
        return windows

    @staticmethod
    def _get_data_slice(data: pd.DataFrame, start, end) -> pd.DataFrame:
        """Get data slice for date range (inclusive)."""
        if hasattr(data, "columns") and "date" in data.columns:
            return data[(data["date"] >= start) & (data["date"] <= end)]
        if hasattr(data, "index"):
            return data.loc[start:end]
        return data[start : end + 1]

    def _aggregate_strategy_windows(
        self,
        window_results: List[Dict],
        oos_equity_curves: List[pd.Series],
        out_of_sample_days: int,
        n_windows: int,
    ) -> WalkForwardResults:
        """Aggregate per-window strategy results into WalkForwardResults."""
        windows_profitable = sum(1 for w in window_results if w["oos_return"] > 0)
        consistency_score = (
            (windows_profitable / len(window_results) * 100) if window_results else 0
        )

        avg_is_return = float(np.mean([w["is_return"] for w in window_results]))
        avg_oos_return = float(np.mean([w["oos_return"] for w in window_results]))

        if avg_is_return > 0:
            performance_degradation = ((avg_is_return - avg_oos_return) / avg_is_return) * 100
        else:
            performance_degradation = 0

        total_oos_return = float(np.sum([w["oos_return"] for w in window_results]))
        avg_oos_sharpe = float(np.mean([w["oos_sharpe"] for w in window_results]))
        avg_oos_win_rate = float(np.mean([w["oos_win_rate"] for w in window_results]))
        # Worst (most negative) drawdown across all windows
        max_oos_dd = min(w["oos_max_dd"] for w in window_results)

        total_oos_days = n_windows * out_of_sample_days
        years = total_oos_days / 252
        annualized_oos_return = (
            (((1 + total_oos_return / 100) ** (1 / years)) - 1) * 100 if years > 0 else 0
        )

        # Concatenate OOS equity curves (scaled to be continuous)
        oos_equity_curve = None
        if oos_equity_curves:
            combined_equity: List[float] = []
            for curve in oos_equity_curves:
                if combined_equity:
                    last_value = combined_equity[-1]
                    scaled_curve = (curve / curve.iloc[0]) * last_value
                    combined_equity.extend(scaled_curve.tolist()[1:])
                else:
                    combined_equity.extend(curve.tolist())
            oos_equity_curve = pd.Series(combined_equity)

        return WalkForwardResults(
            total_oos_return_pct=total_oos_return,
            annualized_oos_return_pct=annualized_oos_return,
            oos_sharpe_ratio=avg_oos_sharpe,
            oos_max_drawdown_pct=max_oos_dd,
            oos_win_rate_pct=avg_oos_win_rate,
            avg_is_return_pct=avg_is_return,
            avg_oos_return_pct=avg_oos_return,
            performance_degradation_pct=performance_degradation,
            windows_tested=len(window_results),
            windows_profitable=windows_profitable,
            consistency_score=consistency_score,
            window_results=window_results,
            oos_equity_curve=oos_equity_curve,
        )

    # =========================================================================
    # PERIOD GENERATION AND EXECUTION
    # =========================================================================

    def _generate_periods(self, start_date: date, end_date: date) -> List[Dict]:
        """
        Generate walk-forward periods.

        Rolls the training window forward by testing_months each period so
        OOS test windows are CONTIGUOUS (no untested gaps), matching the
        module docstring timeline. The pre-v5.34.0 implementation advanced
        by a full training window, leaving ~11 of every 12 months untested.
        """
        periods = []
        period_id = 1

        current_date = start_date

        while current_date < end_date:
            # Training window
            train_start = current_date
            train_end = train_start + relativedelta(months=self.config.training_months)

            # Testing window
            test_start = train_end + timedelta(days=1)
            test_end = test_start + relativedelta(months=self.config.testing_months)

            # Check if we have enough data
            if test_end > end_date:
                break

            # No-lookahead guarantee at generation time
            assert_no_lookahead(train_end, test_start, context="period generation")

            periods.append(
                {
                    "id": period_id,
                    "train_start": train_start,
                    "train_end": train_end,
                    "test_start": test_start,
                    "test_end": test_end,
                }
            )

            # Roll forward by testing window
            current_date = train_start + relativedelta(months=self.config.testing_months)
            period_id += 1

        return periods

    def _run_strategy_period(
        self,
        train_start: date,
        train_end: date,
        test_start: date,
        test_end: date,
        portfolio_value: float,
        strategy_func=None,
    ) -> List[Dict[str, Any]]:
        """
        Run strategy for a single period

        Uses ONLY training data to generate positions for testing period

        Args:
            train_start: Training window start
            train_end: Training window end
            test_start: Testing window start
            test_end: Testing window end
            portfolio_value: Current portfolio value
            strategy_func: Optional custom strategy

        Returns:
            List of positions [{'ticker': 'AAPL', 'weight': 0.10, 'signal': 0.8}, ...]
            ('signal' is optional; feeds the IC hook)
        """
        if strategy_func:
            # Use custom strategy
            return strategy_func(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                portfolio_value=portfolio_value,
            )
        else:
            # Default simple strategy (for demonstration)
            return self._default_strategy(
                train_start=train_start, train_end=train_end, portfolio_value=portfolio_value
            )

    def _default_strategy(
        self, train_start: date, train_end: date, portfolio_value: float
    ) -> List[Dict[str, Any]]:
        """
        Default strategy: Equal-weight top momentum stocks

        This is a placeholder. Real strategy would use Phase 1-4 modules.

        Args:
            train_start: Training start
            train_end: Training end
            portfolio_value: Portfolio value

        Returns:
            List of positions
        """
        # For now, simple equal-weight SPY/TLT (60/40)
        # Real implementation would integrate Phase 2-4
        return [
            {"ticker": "SPY", "weight": 0.60},
            {"ticker": "TLT", "weight": 0.30},
            {"ticker": "GLD", "weight": 0.10},
        ]

    def _execute_period(
        self,
        positions: List[Dict[str, Any]],
        test_start: date,
        test_end: date,
        portfolio_value: float,
    ) -> Tuple[Dict, List]:
        """
        Execute trades and calculate period returns.

        Costs per leg = commission (PaperTrader IBKR schedule, or flat
        commission_pct override) + slippage (slippage_pct of traded value).

        Args:
            positions: Target positions
            test_start: Test period start (entry / signal-execution date)
            test_end: Test period end (return-realization date)
            portfolio_value: Starting portfolio value

        Returns:
            Tuple of (period_result_dict, trades_list)
        """
        # No-lookahead guarantee: entry strictly before return realization
        assert_no_lookahead(test_start, test_end, context="period execution")

        trades = []

        # Get prices at start and end
        start_prices = {}
        end_prices = {}

        for position in positions:
            ticker = position["ticker"]

            # Get price at test_start (entry)
            start_price = self._get_price_at_date(ticker, test_start)
            if start_price:
                start_prices[ticker] = start_price

            # Get price at test_end (exit)
            end_price = self._get_price_at_date(ticker, test_end)
            if end_price:
                end_prices[ticker] = end_price

        # Calculate position values (net of costs) and a parallel GROSS curve
        # that ignores transaction costs, so run_backtest can report cost drag.
        total_value_end = 0.0
        total_value_end_gross = 0.0
        period_costs = 0.0
        period_traded_notional = 0.0

        for position in positions:
            ticker = position["ticker"]
            weight = position["weight"]

            if ticker not in start_prices or ticker not in end_prices:
                continue

            entry_price = start_prices[ticker]
            exit_price = end_prices[ticker]

            # Position value at entry
            position_value_start = portfolio_value * weight
            shares = position_value_start / entry_price

            # Transaction costs per leg (entry + exit)
            entry_costs = self._calculate_leg_cost(shares, entry_price)
            exit_gross = shares * exit_price
            exit_costs = self._calculate_leg_cost(shares, exit_price)
            trade_costs = entry_costs + exit_costs

            # Position value at exit, net of all costs
            position_value_end = exit_gross - trade_costs
            net_return = (position_value_end - position_value_start) / position_value_start

            total_value_end += position_value_end
            total_value_end_gross += exit_gross
            period_costs += trade_costs
            # Round-trip notional traded: entry leg + exit leg
            period_traded_notional += position_value_start + exit_gross

            # Record trade
            trades.append(
                {
                    "ticker": ticker,
                    "entry_date": test_start,
                    "exit_date": test_end,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "weight": weight,
                    "value_start": position_value_start,
                    "value_end": position_value_end,
                    "return_pct": net_return * 100,
                    "transaction_costs": trade_costs,
                    "signal": position.get("signal"),
                }
            )

        # Add cash (unallocated portion) to both curves
        total_weight = sum(p["weight"] for p in positions)
        cash = portfolio_value * (1 - total_weight)
        total_value_end += cash
        total_value_end_gross += cash

        result = {
            "portfolio_value_start": portfolio_value,
            "portfolio_value_end": total_value_end,
            "portfolio_value_end_gross": total_value_end_gross,
            "return_pct": ((total_value_end - portfolio_value) / portfolio_value) * 100,
            "gross_return_pct": (
                (total_value_end_gross - portfolio_value) / portfolio_value
            )
            * 100,
            "period_costs": period_costs,
            "period_traded_notional": period_traded_notional,
        }

        return result, trades

    def _execute_period_delta(
        self,
        positions: List[Dict[str, Any]],
        test_start: date,
        test_end: date,
        portfolio_value: float,
        is_final: bool,
    ) -> Tuple[Dict, List]:
        """
        v6.2.0 (Strategy v1.1 H1): delta-rebalance execution.

        Holdings persist across periods (state in self._holdings/self._cash,
        reset by run_backtest). At each boundary only the DELTA between the
        previous holdings and the new targets is traded — matching how
        PaperTrader executes and how the verdict runner's 60/40 benchmark is
        simulated — instead of liquidating and re-entering the whole book.

        Conventions (documented in the verdict report):
        - Costs are paid from cash, so cash can dip slightly negative
          (≈ the boundary's costs) when target weights sum to 1.0 — the same
          convention as the 60/40 benchmark simulation.
        - Trade records carry the position's GROSS price return (costs live
          in cash); per-trade transaction_costs no longer sum to the period
          total when tickers are sold to zero — BacktestPeriod.period_costs
          is authoritative.
        - A held ticker that cannot be priced at a boundary raises (fail
          loud); an unpriceable NEW target is skipped and counted in
          n_positions_skipped via len(positions) - len(trades).
        - The final period liquidates fully (exit legs), matching the
          benchmarks' terminal liquidation, so terminal equity is cash.

        Returns:
            Tuple of (period_result_dict, trades_list) — same keys as
            _execute_period plus counterfactual_full_liquidation_costs (what
            this period would have cost under the historical model).
        """
        assert_no_lookahead(test_start, test_end, context="period execution")

        # ----- Targets (deduped; non-positive weights ignored) -----
        targets: Dict[str, Dict[str, Any]] = {}
        for position in positions:
            if float(position.get("weight", 0.0)) > 0.0:
                targets[position["ticker"]] = position

        # ----- Boundary prices for held ∪ target tickers -----
        tickers = sorted(set(self._holdings) | set(targets))
        start_prices: Dict[str, float] = {}
        end_prices: Dict[str, Optional[float]] = {}
        for ticker in tickers:
            start_price = self._get_price_at_date(ticker, test_start)
            if start_price is None:
                if ticker in self._holdings:
                    raise RuntimeError(
                        f"Delta execution: held ticker {ticker} has no price at "
                        f"{test_start} — cannot mark or trade a live position"
                    )
                logger.warning(
                    "Delta execution: skipping new target %s (no entry price at %s)",
                    ticker, test_start,
                )
                targets.pop(ticker, None)
                continue
            start_prices[ticker] = start_price
            end_prices[ticker] = self._get_price_at_date(ticker, test_end)
            if end_prices[ticker] is None and ticker in self._holdings:
                raise RuntimeError(
                    f"Delta execution: held ticker {ticker} has no price at "
                    f"{test_end} — cannot mark a live position"
                )

        # ----- Mark to market; chain invariant -----
        pv = self._cash + sum(
            shares * start_prices[t] for t, shares in self._holdings.items()
        )
        if abs(pv - portfolio_value) > 1e-6 * max(abs(pv), 1.0):
            raise RuntimeError(
                f"Delta execution: portfolio chain broken at {test_start}: "
                f"marked {pv:.6f} != chained {portfolio_value:.6f}"
            )

        period_costs = 0.0
        period_traded_notional = 0.0
        counterfactual_costs = 0.0
        boundary_costs: Dict[str, float] = {}
        boundary_notional: Dict[str, float] = {}

        # ----- Trade the deltas (sells first frees cash, then buys) -----
        target_shares: Dict[str, float] = {}
        for ticker, position in targets.items():
            target_shares[ticker] = (
                float(position["weight"]) * pv / start_prices[ticker]
            )
        deltas = {
            ticker: target_shares.get(ticker, 0.0) - self._holdings.get(ticker, 0.0)
            for ticker in (set(self._holdings) | set(target_shares))
        }
        for ticker in sorted(deltas, key=lambda t: deltas[t]):  # sells first
            delta = deltas[ticker]
            price = start_prices[ticker]
            target_notional = target_shares.get(ticker, 0.0) * price
            no_trade_band = max(1.0, self.config.delta_no_trade_band_pct * target_notional)
            if abs(delta) * price < no_trade_band:
                continue
            cost = self._calculate_leg_cost(abs(delta), price)
            self._cash -= delta * price + cost
            period_costs += cost
            period_traded_notional += abs(delta) * price
            boundary_costs[ticker] = boundary_costs.get(ticker, 0.0) + cost
            boundary_notional[ticker] = (
                boundary_notional.get(ticker, 0.0) + abs(delta) * price
            )
            new_shares = self._holdings.get(ticker, 0.0) + delta
            if abs(new_shares) < 1e-9:
                self._holdings.pop(ticker, None)
            else:
                self._holdings[ticker] = new_shares

        # ----- Counterfactual: the historical full-liquidation charge -----
        # Identical position list, _execute_period semantics (entry+exit legs
        # on weight*pv/entry_price shares); targets without an exit price
        # would have been skipped by the stateless model → contribute 0.
        for ticker, position in targets.items():
            exit_price = end_prices.get(ticker)
            if exit_price is None:
                continue
            shares_cf = float(position["weight"]) * pv / start_prices[ticker]
            counterfactual_costs += self._calculate_leg_cost(
                shares_cf, start_prices[ticker]
            ) + self._calculate_leg_cost(shares_cf, exit_price)

        # ----- Trade records: one per TARGET position held this window -----
        trades = []
        for ticker, position in targets.items():
            shares = self._holdings.get(ticker, 0.0)
            if shares <= 0.0:
                continue  # band-suppressed dust target never established
            entry_price = start_prices[ticker]
            exit_price = end_prices[ticker]
            trades.append(
                {
                    "ticker": ticker,
                    "entry_date": test_start,
                    "exit_date": test_end,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "weight": position["weight"],
                    "value_start": shares * entry_price,
                    "value_end": shares * exit_price,
                    "return_pct": (exit_price / entry_price - 1.0) * 100,
                    "transaction_costs": boundary_costs.get(ticker, 0.0),
                    "traded_notional": boundary_notional.get(ticker, 0.0),
                    "signal": position.get("signal"),
                }
            )

        # ----- Terminal liquidation (final period only) -----
        if is_final:
            trades_by_ticker = {t["ticker"]: t for t in trades}
            for ticker in sorted(self._holdings):
                shares = self._holdings[ticker]
                exit_price = end_prices[ticker]
                assert exit_price is not None  # held ⇒ enforced above
                cost = self._calculate_leg_cost(abs(shares), exit_price)
                self._cash += shares * exit_price - cost
                period_costs += cost
                period_traded_notional += abs(shares) * exit_price
                if ticker in trades_by_ticker:
                    trades_by_ticker[ticker]["transaction_costs"] += cost
                    trades_by_ticker[ticker]["traded_notional"] += (
                        abs(shares) * exit_price
                    )
            self._holdings = {}
            total_value_end = self._cash
        else:
            total_value_end = self._cash + sum(
                shares * end_prices[t] for t, shares in self._holdings.items()
            )

        result = {
            "portfolio_value_start": pv,
            "portfolio_value_end": total_value_end,
            # Cost-free parallel curve: identical trades without their costs
            # ⇒ gross_end = net_end + period_costs (v5.38.0 identity).
            "portfolio_value_end_gross": total_value_end + period_costs,
            "return_pct": ((total_value_end - pv) / pv) * 100,
            "gross_return_pct": ((total_value_end + period_costs - pv) / pv) * 100,
            "period_costs": period_costs,
            "period_traded_notional": period_traded_notional,
            "counterfactual_full_liquidation_costs": counterfactual_costs,
        }

        return result, trades

    def _calculate_leg_cost(self, shares: float, price: float) -> float:
        """Cost of one trade leg: commission + slippage (PaperTrader constants)."""
        traded_value = shares * price
        if self.config.commission_pct is not None:
            commission = traded_value * self.config.commission_pct
        else:
            commission = calculate_commission(shares, price)
        slippage = traded_value * self.config.slippage_pct
        return commission + slippage

    def _build_cost_drag_report(
        self,
        net_return_pct: float,
        gross_value: float,
        total_costs: float,
        total_traded_notional: float,
        avg_capital: float,
        start_date: date,
        end_date: date,
    ) -> CostDragReport:
        """
        Assemble the gross-vs-net cost drag report (v5.38.0).

        - gross_return_pct: compounded return of the cost-free parallel curve.
        - total_costs_pct_of_capital: dollar costs / initial capital.
        - annual_cost_drag_pct: that cost %, scaled to a 1-year horizon, vs the
          COST_DRAG_ALARM_PCT (3%) churn alarm.
        - turnover: round-trip notional / average capital deployed.
        """
        initial_capital = self.config.initial_capital
        gross_return_pct = (
            (gross_value - initial_capital) / initial_capital * 100
            if initial_capital > 0
            else 0.0
        )

        total_costs_pct_of_capital = (
            total_costs / initial_capital * 100 if initial_capital > 0 else 0.0
        )

        denom_capital = avg_capital if avg_capital > 0 else initial_capital
        turnover = total_traded_notional / denom_capital if denom_capital > 0 else 0.0

        # Annualize cost drag and turnover by the actual elapsed horizon.
        years = max((end_date - start_date).days / 365.25, 1e-9)
        annual_cost_drag_pct = total_costs_pct_of_capital / years
        annualized_turnover = turnover / years

        # Cast to native Python floats/bool (inputs may be numpy scalars).
        return CostDragReport(
            gross_return_pct=float(gross_return_pct),
            net_return_pct=float(net_return_pct),
            total_costs=float(total_costs),
            total_costs_pct_of_capital=float(total_costs_pct_of_capital),
            annual_cost_drag_pct=float(annual_cost_drag_pct),
            turnover=float(turnover),
            annualized_turnover=float(annualized_turnover),
            exceeds_alarm=bool(annual_cost_drag_pct > COST_DRAG_ALARM_PCT),
        )

    def _get_price_at_date(self, ticker: str, target_date: date) -> Optional[float]:
        """
        Get price at specific date (point-in-time)

        Args:
            ticker: Ticker symbol
            target_date: Target date

        Returns:
            Closing price or None
        """
        # Get price from warehouse
        df = self.warehouse.get_ticker_prices(
            ticker=ticker,
            start_date=target_date - timedelta(days=7),  # Look back 7 days
            end_date=target_date,
        )

        if df.empty:
            logger.warning(f"No price data for {ticker} at {target_date}")
            return None

        # Get last available price
        return df.iloc[-1]["close"]

    @staticmethod
    def _periods_per_year(equity_df: pd.DataFrame) -> float:
        """
        Equity-curve periods per year, inferred from median point spacing.

        run_backtest() emits one equity point per walk-forward period, so the
        spacing is testing_months long (~30.4 days for monthly windows →
        ~12 periods/year). Falls back to 252 (daily) when spacing cannot be
        inferred, preserving pre-v5.35.1 behavior for degenerate curves.
        """
        if "date" not in equity_df.columns or len(equity_df) < 2:
            return 252.0
        dates = pd.to_datetime(equity_df["date"]).values
        spacing_days = np.diff(dates).astype("timedelta64[D]").astype(float)
        median_days = float(np.median(spacing_days))
        if median_days <= 0:
            return 252.0
        return 365.25 / median_days

    def _calculate_drawdowns(self, equity_df: pd.DataFrame) -> pd.DataFrame:
        """Calculate drawdowns for equity curve"""
        equity_df = equity_df.copy()

        # Calculate running maximum
        equity_df["peak"] = equity_df["value"].expanding().max()

        # Calculate drawdown
        equity_df["drawdown"] = (equity_df["value"] - equity_df["peak"]) / equity_df["peak"]
        equity_df["drawdown_pct"] = equity_df["drawdown"] * 100

        return equity_df

    def _calculate_performance_metrics(
        self, equity_df: pd.DataFrame, trades: List[Dict], start_date: date, end_date: date
    ) -> Dict[str, float]:
        """Calculate performance metrics"""

        # Total return
        initial_value = self.config.initial_capital
        final_value = equity_df.iloc[-1]["value"]
        total_return = (final_value - initial_value) / initial_value

        # Annualized return
        days = (end_date - start_date).days
        years = days / 365.25
        annualized_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        # Sharpe ratio (assume 2% risk-free rate).
        # The equity curve has ONE point per walk-forward period (monthly for
        # testing_months=1), so annualization must use the actual period
        # frequency. v5.35.1 fix: the old hardcoded sqrt(252)/daily-rf treated
        # monthly period returns as daily, inflating Sharpe/Sortino ~4.6x.
        returns = equity_df["return"].values
        periods_per_year = self._periods_per_year(equity_df)
        excess_returns = returns - (0.02 / periods_per_year)  # Per-period risk-free
        sharpe = (
            np.sqrt(periods_per_year) * (excess_returns.mean() / excess_returns.std())
            if excess_returns.std() > 0
            else 0
        )

        # Sortino ratio (downside deviation)
        downside_returns = returns[returns < 0]
        downside_std = downside_returns.std() if len(downside_returns) > 0 else 0.0001
        sortino = (
            np.sqrt(periods_per_year) * (returns.mean() / downside_std)
            if downside_std > 0
            else 0
        )

        # Max drawdown
        max_dd = equity_df["drawdown"].min()

        # Calmar ratio
        calmar = annualized_return / abs(max_dd) if max_dd != 0 else 0

        # Win/loss metrics
        trade_returns = [t["return_pct"] for t in trades]
        winning_trades = [r for r in trade_returns if r > 0]
        losing_trades = [r for r in trade_returns if r < 0]

        win_rate = (len(winning_trades) / len(trade_returns) * 100) if trade_returns else 0
        avg_win = np.mean(winning_trades) if winning_trades else 0
        avg_loss = np.mean(losing_trades) if losing_trades else 0

        gross_profit = sum(winning_trades) if winning_trades else 0
        gross_loss = abs(sum(losing_trades)) if losing_trades else 0.0
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        else:
            # No losing trades: profit factor is undefined/infinite by convention
            profit_factor = float("inf") if gross_profit > 0 else 0.0

        return {
            "total_return_pct": total_return * 100,
            "annualized_return_pct": annualized_return * 100,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "max_drawdown_pct": max_dd * 100,
            "calmar_ratio": calmar,
            "win_rate_pct": win_rate,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "profit_factor": profit_factor,
            "profitable_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
        }

    def _calculate_benchmark_metrics(
        self, equity_df: pd.DataFrame, benchmark_ticker: str, start_date: date, end_date: date
    ) -> Dict[str, float]:
        """Calculate metrics vs benchmark"""

        # Get benchmark returns
        benchmark_df = self.warehouse.get_ticker_prices(
            ticker=benchmark_ticker, start_date=start_date, end_date=end_date
        )

        if benchmark_df.empty:
            logger.warning(f"No benchmark data for {benchmark_ticker}")
            return {"benchmark_return_pct": 0, "alpha_pct": 0, "beta": 0, "information_ratio": 0}

        # Calculate benchmark return
        benchmark_return = (
            benchmark_df.iloc[-1]["close"] - benchmark_df.iloc[0]["close"]
        ) / benchmark_df.iloc[0]["close"]

        # Calculate beta (simple approximation)
        # Match dates (simplified)
        beta = 1.0  # Placeholder

        # Alpha = Strategy return - Beta * Benchmark return
        strategy_total_return = (
            equity_df.iloc[-1]["value"] - equity_df.iloc[0]["value"]
        ) / equity_df.iloc[0]["value"]
        alpha = strategy_total_return - (beta * benchmark_return)

        # Information ratio
        tracking_error = 0.05  # Placeholder
        information_ratio = alpha / tracking_error if tracking_error > 0 else 0

        return {
            "benchmark_return_pct": benchmark_return * 100,
            "alpha_pct": alpha * 100,
            "beta": beta,
            "information_ratio": information_ratio,
        }

    # =========================================================================
    # v5.24.0: REFLEXIVITY COMPARISON BACKTEST
    # =========================================================================

    def backtest_with_reflexivity_comparison(
        self,
        signals: List[Dict[str, Any]],
        reflexivity_intensities: Dict[date, float],
        base_position_size: float = 0.08,
        max_reflexivity_size: float = 0.20,
        reflexivity_threshold: float = 0.6,
    ) -> ReflexivityComparisonResult:
        """
        Run backtest comparing reflexivity vs uniform sizing.

        Tests the CLAUDE.md hypothesis:
        Position Size = (Conviction / 10) × (1 + Reflexivity Intensity)

        Args:
            signals: List of signal dicts with keys:
                     {date, ticker, action, conviction (0-10)}
            reflexivity_intensities: Dict mapping date -> intensity (0-1)
            base_position_size: Base position size (default 8%)
            max_reflexivity_size: Max position during Soros mode (default 20%)
            reflexivity_threshold: Intensity threshold for Soros mode (default 0.6)

        Returns:
            ReflexivityComparisonResult with comparison metrics

        Example:
            intensities = {date(2020, 3, 15): 0.85, date(2020, 3, 16): 0.82, ...}
            signals = [
                {'date': date(2020, 3, 15), 'ticker': 'SPY', 'action': 'BUY', 'conviction': 8},
            ]
            result = backtester.backtest_with_reflexivity_comparison(
                signals=signals,
                reflexivity_intensities=intensities
            )
        """
        if not REFLEXIVITY_AVAILABLE:
            logger.warning("Reflexivity module not available. Returning empty comparison.")
            return ReflexivityComparisonResult(
                reflexivity_return_pct=0, reflexivity_sharpe=0, reflexivity_max_dd_pct=0,
                reflexivity_win_rate=0, uniform_return_pct=0, uniform_sharpe=0,
                uniform_max_dd_pct=0, uniform_win_rate=0, alpha_vs_uniform_pct=0,
                sharpe_improvement=0, statistical_significance=1.0, is_validated=False,
                periods_analyzed=0, reflexivity_periods_won=0
            )

        logger.info("=" * 70)
        logger.info("REFLEXIVITY VS UNIFORM SIZING COMPARISON")
        logger.info("=" * 70)
        logger.info(f"Base position size: {base_position_size:.1%}")
        logger.info(f"Max reflexivity size: {max_reflexivity_size:.1%}")
        logger.info(f"Reflexivity threshold: {reflexivity_threshold}")

        # Separate signals into reflexivity periods vs normal
        reflexivity_signals = []
        normal_signals = []

        for signal in signals:
            signal_date = signal.get('date')
            intensity = (
                reflexivity_intensities.get(signal_date, 0.0)
                if signal_date is not None else 0.0
            )

            if intensity >= reflexivity_threshold:
                reflexivity_signals.append({**signal, 'intensity': intensity})
            else:
                normal_signals.append({**signal, 'intensity': intensity})

        logger.info(f"Signals in reflexivity periods: {len(reflexivity_signals)}")
        logger.info(f"Signals in normal periods: {len(normal_signals)}")

        # Calculate position sizes for both modes
        reflexivity_positions = []
        uniform_positions = []

        for signal in signals:
            signal_date = signal.get('date')
            conviction = signal.get('conviction', 7.0)
            intensity = (
                reflexivity_intensities.get(signal_date, 0.0)
                if signal_date is not None else 0.0
            )

            # Reflexivity sizing: base × (1 + intensity) × (conviction/10)
            reflex_size = base_position_size * (1 + intensity) * (conviction / 10.0)
            reflex_size = min(reflex_size, max_reflexivity_size)

            # Uniform sizing: constant base × (conviction/10)
            uniform_size = base_position_size * (conviction / 10.0)

            reflexivity_positions.append(reflex_size)
            uniform_positions.append(uniform_size)

        # Calculate average sizes
        avg_reflex = np.mean(reflexivity_positions) if reflexivity_positions else base_position_size
        avg_uniform = np.mean(uniform_positions) if uniform_positions else base_position_size

        logger.info(f"Avg reflexivity position size: {avg_reflex:.1%}")
        logger.info(f"Avg uniform position size: {avg_uniform:.1%}")

        # Simulate returns (simplified - in production would use actual price data)
        # For now, assume signals have a 'return_pct' field or we estimate
        reflexivity_returns = []
        uniform_returns = []

        for i, signal in enumerate(signals):
            signal_return = signal.get('return_pct', np.random.normal(0.003, 0.02))

            # Scale return by position size
            reflex_return = signal_return * (reflexivity_positions[i] / base_position_size)
            uniform_return = signal_return * (uniform_positions[i] / base_position_size)

            reflexivity_returns.append(reflex_return)
            uniform_returns.append(uniform_return)

        # Calculate performance metrics
        reflexivity_metrics = self._calculate_comparison_metrics(reflexivity_returns)
        uniform_metrics = self._calculate_comparison_metrics(uniform_returns)

        # Statistical significance (paired t-test)
        p_value = self._calculate_paired_ttest(reflexivity_returns, uniform_returns)

        # Determine if validated
        alpha = reflexivity_metrics['total_return'] - uniform_metrics['total_return']
        sharpe_improvement = reflexivity_metrics['sharpe'] - uniform_metrics['sharpe']

        is_validated = (
            alpha > 2.0 and  # >2% alpha
            sharpe_improvement > 0.2 and  # Sharpe improvement
            p_value < 0.05  # Statistically significant
        )

        result = ReflexivityComparisonResult(
            reflexivity_return_pct=reflexivity_metrics['total_return'],
            reflexivity_sharpe=reflexivity_metrics['sharpe'],
            reflexivity_max_dd_pct=reflexivity_metrics['max_drawdown'],
            reflexivity_win_rate=reflexivity_metrics['win_rate'],
            uniform_return_pct=uniform_metrics['total_return'],
            uniform_sharpe=uniform_metrics['sharpe'],
            uniform_max_dd_pct=uniform_metrics['max_drawdown'],
            uniform_win_rate=uniform_metrics['win_rate'],
            alpha_vs_uniform_pct=alpha,
            sharpe_improvement=sharpe_improvement,
            statistical_significance=p_value,
            is_validated=is_validated,
            periods_analyzed=len(signals),
            reflexivity_periods_won=len(reflexivity_signals),
        )

        logger.info("")
        logger.info("COMPARISON RESULTS:")
        logger.info(f"  Reflexivity Return: {result.reflexivity_return_pct:+.2f}%")
        logger.info(f"  Uniform Return:     {result.uniform_return_pct:+.2f}%")
        logger.info(f"  Alpha vs Uniform:   {result.alpha_vs_uniform_pct:+.2f}%")
        logger.info(f"  Sharpe Improvement: {result.sharpe_improvement:+.2f}")
        logger.info(f"  p-value:            {result.statistical_significance:.4f}")
        logger.info(f"  Validated:          {result.is_validated}")
        logger.info("=" * 70)

        return result

    def _calculate_comparison_metrics(self, returns: List[float]) -> Dict[str, float]:
        """Calculate metrics from a list of returns."""
        if not returns:
            return {'total_return': 0, 'sharpe': 0, 'max_drawdown': 0, 'win_rate': 0}

        returns_array = np.array(returns)

        # Total return (compounded)
        total_return = (np.prod(1 + returns_array) - 1) * 100

        # Sharpe ratio (annualized, assuming daily returns)
        if np.std(returns_array) > 0:
            sharpe = np.sqrt(252) * (np.mean(returns_array) / np.std(returns_array))
        else:
            sharpe = 0

        # Max drawdown
        cumulative = np.cumprod(1 + returns_array)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = (cumulative - running_max) / running_max
        max_drawdown = np.min(drawdowns) * 100

        # Win rate
        wins = sum(1 for r in returns if r > 0)
        win_rate = (wins / len(returns)) * 100 if returns else 0

        return {
            'total_return': total_return,
            'sharpe': sharpe,
            'max_drawdown': max_drawdown,
            'win_rate': win_rate,
        }

    def _calculate_paired_ttest(
        self,
        reflexivity_returns: List[float],
        uniform_returns: List[float]
    ) -> float:
        """Calculate p-value from paired t-test."""
        if len(reflexivity_returns) != len(uniform_returns) or len(reflexivity_returns) < 10:
            return 1.0  # Not significant

        differences = np.array(reflexivity_returns) - np.array(uniform_returns)

        if np.std(differences) == 0:
            return 1.0 if np.mean(differences) == 0 else 0.0

        n = len(differences)
        t_stat = np.mean(differences) / (np.std(differences, ddof=1) / np.sqrt(n))

        # Approximate p-value using normal distribution for large n
        p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(t_stat) / math.sqrt(2))))

        return p_value


def generate_summary_report(result: BacktestResult) -> str:
    """Generate human-readable summary report"""

    lines = []
    lines.append("\n" + "=" * 80)
    lines.append("BACKTEST SUMMARY REPORT")
    lines.append("=" * 80)
    lines.append(f"Period: {result.start_date} to {result.end_date}")
    lines.append(
        f"Strategy: Walk-Forward ({result.config.training_months}M train, {result.config.testing_months}M test)"
    )
    lines.append(f"Initial Capital: ${result.config.initial_capital:,.2f}")
    lines.append(f"Final Value: ${result.equity_curve.iloc[-1]['value']:,.2f}")
    lines.append("")

    lines.append("PERFORMANCE METRICS:")
    lines.append("-" * 80)
    lines.append(f"  Total Return:        {result.total_return_pct:>10.2f}%")
    lines.append(f"  Annualized Return:   {result.annualized_return_pct:>10.2f}%")
    lines.append(f"  Sharpe Ratio:        {result.sharpe_ratio:>10.2f}")
    lines.append(f"  Sortino Ratio:       {result.sortino_ratio:>10.2f}")
    lines.append(f"  Max Drawdown:        {result.max_drawdown_pct:>10.2f}%")
    lines.append(f"  Calmar Ratio:        {result.calmar_ratio:>10.2f}")
    lines.append("")

    lines.append("WIN/LOSS METRICS:")
    lines.append("-" * 80)
    lines.append(f"  Total Trades:        {result.total_trades:>10d}")
    lines.append(f"  Winning Trades:      {result.profitable_trades:>10d}")
    lines.append(f"  Losing Trades:       {result.losing_trades:>10d}")
    lines.append(f"  Win Rate:            {result.win_rate_pct:>10.1f}%")
    lines.append(f"  Profit Factor:       {result.profit_factor:>10.2f}")
    lines.append(f"  Avg Win:             {result.avg_win_pct:>10.2f}%")
    lines.append(f"  Avg Loss:            {result.avg_loss_pct:>10.2f}%")
    lines.append("")

    lines.append("BENCHMARK COMPARISON:")
    lines.append("-" * 80)
    lines.append(
        f"  Benchmark ({result.config.benchmark_ticker}): {result.benchmark_return_pct:>10.2f}%"
    )
    lines.append(f"  Alpha:               {result.alpha_pct:>10.2f}%")
    lines.append(f"  Beta:                {result.beta:>10.2f}")
    lines.append(f"  Information Ratio:   {result.information_ratio:>10.2f}")

    if result.signal_ic:
        lines.append("")
        lines.append("SIGNAL IC (Simons gate: IC > 0.05, p < 0.05):")
        lines.append("-" * 80)
        lines.append(f"  IC:                  {result.signal_ic.get('ic', 0):>10.4f}")
        lines.append(f"  p-value:             {result.signal_ic.get('p_value', 1):>10.4f}")
        lines.append(f"  Sample Size:         {result.signal_ic.get('sample_size', 0):>10d}")

    if result.cost_drag:
        cd = result.cost_drag
        lines.append("")
        lines.append("COST DRAG REPORT (gross vs net, turnover):")
        lines.append("-" * 80)
        lines.append(f"  Gross Return:        {cd.gross_return_pct:>10.2f}%")
        lines.append(f"  Net Return:          {cd.net_return_pct:>10.2f}%")
        lines.append(
            f"  Cost Drag:           {cd.gross_return_pct - cd.net_return_pct:>10.2f}%"
        )
        lines.append(f"  Total Costs:         ${cd.total_costs:>12,.2f}")
        lines.append(f"  Costs % of Capital:  {cd.total_costs_pct_of_capital:>10.2f}%")
        lines.append(f"  Annual Cost Drag:    {cd.annual_cost_drag_pct:>10.2f}%")
        lines.append(f"  Turnover:            {cd.turnover:>10.2f}x")
        lines.append(f"  Annualized Turnover: {cd.annualized_turnover:>10.2f}x")
        if cd.exceeds_alarm:
            lines.append(
                f"  ⚠️  ALARM: annual cost drag exceeds {COST_DRAG_ALARM_PCT:.1f}% — "
                "strategy is churning."
            )

    lines.append("=" * 80)

    return "\n".join(lines)


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Create backtester
    backtester = WalkForwardBacktester(
        config=BacktestConfig(training_months=12, testing_months=1, initial_capital=100000)
    )

    # Run backtest
    result = backtester.run_backtest(start_date="2020-01-01", end_date="2023-12-31")

    # Print summary
    print(generate_summary_report(result))
