"""
Regression tests for the CANONICAL walk-forward backtester (v5.34.0).

modules/testing/walk_forward_backtester.py is the single canonical backtest
path (see docs/BACKTESTER_CONSOLIDATION.md). These tests pin its behavior:

1. Perfect-foresight regression: a known synthetic signal on synthetic
   prices must produce expected metrics within tolerance.
2. No-lookahead guarantees: assert signal date < return date, train/test
   windows never overlap.
3. Transaction costs follow PaperTrader constants (IBKR schedule).
4. IC walk-forward validation: perfect signal → IC ≈ 1.0, OOS validated.
5. Off-by-one regression: the final full window is included (the deprecated
   implementations dropped it).

Jim Simons: "If it's not statistically significant, it doesn't exist."
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd
from datetime import date, datetime, timedelta
from scipy import stats

from modules.execution.paper_trader import PaperTrader
from modules.models.data_models import TradeSignal, Action
from modules.testing.walk_forward_backtester import (
    BacktestConfig,
    LookaheadBiasError,
    WalkForwardBacktester,
    assert_no_lookahead,
    calculate_commission,
    generate_window_indices,
    interpret_walk_forward_ic,
    walk_forward_ic_validation,
)

pytestmark = pytest.mark.unit


# =============================================================================
# Synthetic price model (deterministic — no API calls, no randomness)
# =============================================================================

EPOCH = date(2019, 1, 1)

# Deterministic geometric daily growth per ticker
PRICE_MODEL = {
    "UP": (100.0, 0.004),     # +0.4%/day, always rises
    "DOWN": (100.0, -0.002),  # -0.2%/day, always falls
    "SPY": (300.0, 0.001),    # benchmark
    "FLAT1": (100.0, 0.0),    # constant price (delta-mode zero-cost regression)
    "FLAT2": (50.0, 0.0),     # constant price (delta-mode zero-cost regression)
}


def model_price(ticker: str, on_date: date) -> float:
    """Closed-form synthetic price: base * (1 + daily)^days_since_epoch."""
    base, daily = PRICE_MODEL[ticker]
    return base * (1.0 + daily) ** (on_date - EPOCH).days


class FakeWarehouse:
    """Deterministic in-memory stand-in for DataWarehouse.get_ticker_prices."""

    def get_ticker_prices(self, ticker, start_date=None, end_date=None):
        if ticker not in PRICE_MODEL:
            return pd.DataFrame()
        start_date = start_date or EPOCH
        end_date = end_date or start_date
        rows = []
        d = start_date
        while d <= end_date:
            rows.append({"date": d, "close": model_price(ticker, d)})
            d += timedelta(days=1)
        return pd.DataFrame(rows)


def pearson_ic(signals, returns, signal_name=None):
    """Hermetic IC function with InformationCoefficientCalculator semantics."""
    ic, p_value = stats.pearsonr(list(signals), list(returns))
    return {"ic": float(ic), "p_value": float(p_value), "sample_size": len(signals)}


def zero_cost_config() -> BacktestConfig:
    return BacktestConfig(commission_pct=0.0, slippage_pct=0.0)


# =============================================================================
# Transaction costs: PaperTrader constants are the single source of truth
# =============================================================================


class TestTransactionCosts:
    def test_per_share_commission(self):
        # 10,000 shares @ $50: 10000 * $0.005 = $50, under the 1% cap ($5,000)
        assert calculate_commission(10_000, 50.0) == pytest.approx(
            10_000 * PaperTrader.COMMISSION_PER_SHARE
        )

    def test_minimum_commission_floor(self):
        # 100 shares @ $50: $0.50 per-share → floored at $1.00 minimum
        assert calculate_commission(100, 50.0) == pytest.approx(PaperTrader.MIN_COMMISSION)

    def test_maximum_commission_cap(self):
        # 1,000 shares @ $0.10: $5 per-share but capped at 1% of $100 value
        assert calculate_commission(1_000, 0.10) == pytest.approx(
            1_000 * 0.10 * PaperTrader.MAX_COMMISSION_PCT
        )

    def test_degenerate_inputs(self):
        assert calculate_commission(0, 100.0) == 0.0
        assert calculate_commission(-5, 100.0) == 0.0
        assert calculate_commission(100, 0.0) == 0.0

    def test_default_slippage_matches_paper_trader(self):
        assert BacktestConfig().slippage_pct == PaperTrader.SLIPPAGE_LIQUID


# =============================================================================
# No-lookahead guarantees
# =============================================================================


class TestLookaheadProtection:
    def test_signal_before_return_passes(self):
        assert_no_lookahead(date(2024, 1, 1), date(2024, 1, 2))

    def test_equal_dates_raise(self):
        with pytest.raises(LookaheadBiasError):
            assert_no_lookahead(date(2024, 1, 1), date(2024, 1, 1))

    def test_signal_after_return_raises(self):
        with pytest.raises(LookaheadBiasError):
            assert_no_lookahead(date(2024, 1, 2), date(2024, 1, 1))

    def test_execute_period_rejects_inverted_dates(self):
        backtester = WalkForwardBacktester(
            warehouse=FakeWarehouse(), config=zero_cost_config()
        )
        with pytest.raises(LookaheadBiasError):
            backtester._execute_period(
                positions=[{"ticker": "UP", "weight": 1.0}],
                test_start=date(2024, 1, 2),
                test_end=date(2024, 1, 2),
                portfolio_value=100_000.0,
            )

    def test_generated_periods_never_overlap(self):
        backtester = WalkForwardBacktester(
            warehouse=FakeWarehouse(), config=zero_cost_config()
        )
        periods = backtester._generate_periods(date(2020, 1, 1), date(2023, 12, 31))
        assert len(periods) > 10
        for p in periods:
            assert p["train_end"] < p["test_start"]
            assert p["test_start"] < p["test_end"]


class TestWindowGeneration:
    def test_window_count_and_boundaries(self):
        windows = generate_window_indices(100, train_size=60, test_size=20, step_size=20)
        assert len(windows) == 2
        assert windows[0] == {
            "train_start": 0, "train_end": 60, "test_start": 60, "test_end": 80
        }
        assert windows[1] == {
            "train_start": 20, "train_end": 80, "test_start": 80, "test_end": 100
        }

    def test_exact_boundary_window_included(self):
        # Off-by-one regression: PatternBacktest's old range() loop dropped
        # the final full window when n == train + test
        windows = generate_window_indices(80, train_size=60, test_size=20)
        assert len(windows) == 1

    def test_train_test_never_overlap(self):
        for w in generate_window_indices(1000, 252, 63):
            assert w["train_end"] == w["test_start"]
            assert w["train_end"] - 1 < w["test_start"]

    def test_invalid_sizes_raise(self):
        with pytest.raises(ValueError):
            generate_window_indices(100, 0, 20)
        with pytest.raises(ValueError):
            generate_window_indices(100, 60, -1)
        with pytest.raises(ValueError):
            generate_window_indices(100, 60, 20, step_size=0)


# =============================================================================
# REGRESSION: perfect-foresight signal on synthetic prices → expected metrics
# =============================================================================


class TestPerfectForesightRegression:
    """The canonical regression required by docs/BACKTESTER_CONSOLIDATION.md."""

    @pytest.fixture
    def frictionless_result(self):
        backtester = WalkForwardBacktester(
            warehouse=FakeWarehouse(), config=zero_cost_config()
        )

        def all_in_up(train_start, train_end, test_start, test_end, portfolio_value):
            # Perfect foresight: UP rises deterministically every day
            return [{"ticker": "UP", "weight": 1.0}]

        return backtester.run_backtest(
            start_date="2020-01-01",
            end_date="2023-12-31",
            initial_capital=100_000.0,
            strategy_func=all_in_up,
        )

    def test_total_return_matches_price_model(self, frictionless_result):
        # Independent recomputation from the closed-form price model:
        # equity compounds by price(test_end)/price(test_start) each period
        expected_value = 100_000.0
        for p in frictionless_result.periods:
            expected_value *= model_price("UP", p.test_end) / model_price("UP", p.test_start)

        final_value = frictionless_result.equity_curve.iloc[-1]["value"]
        assert final_value == pytest.approx(expected_value, rel=1e-9)

        expected_return_pct = (expected_value / 100_000.0 - 1) * 100
        assert frictionless_result.total_return_pct == pytest.approx(
            expected_return_pct, rel=1e-6
        )

    def test_all_periods_win(self, frictionless_result):
        assert frictionless_result.total_trades >= 30
        assert frictionless_result.win_rate_pct == pytest.approx(100.0)
        assert frictionless_result.losing_trades == 0
        assert frictionless_result.profitable_trades == frictionless_result.total_trades

    def test_profit_factor_infinite_without_losses(self, frictionless_result):
        assert frictionless_result.profit_factor == float("inf")

    def test_no_drawdown_on_monotonic_equity(self, frictionless_result):
        assert frictionless_result.max_drawdown_pct == pytest.approx(0.0, abs=1e-12)
        assert frictionless_result.sortino_ratio > 100  # No downside periods

    def test_sharpe_reflects_steady_gains(self, frictionless_result):
        # ~12.8% per month with only month-length jitter → very high Sharpe
        assert frictionless_result.sharpe_ratio > 3.0

    def test_annualized_return_positive_and_consistent(self, frictionless_result):
        assert frictionless_result.annualized_return_pct > 0
        # (1+total)^(1/years)-1 cross-check within loose tolerance
        years = (frictionless_result.end_date - frictionless_result.start_date).days / 365.25
        expected_annualized = (
            (1 + frictionless_result.total_return_pct / 100) ** (1 / years) - 1
        ) * 100
        assert frictionless_result.annualized_return_pct == pytest.approx(
            expected_annualized, rel=1e-9
        )

    def test_ic_hook_perfect_signal_gives_ic_of_one(self):
        backtester = WalkForwardBacktester(
            warehouse=FakeWarehouse(), config=zero_cost_config()
        )

        def perfect_foresight(train_start, train_end, test_start, test_end, portfolio_value):
            # Signal IS the (deterministic) forward return — perfect foresight
            positions = []
            for ticker, weight in (("UP", 0.5), ("DOWN", 0.5)):
                fwd = model_price(ticker, test_end) / model_price(ticker, test_start) - 1
                positions.append({"ticker": ticker, "weight": weight, "signal": fwd})
            return positions

        result = backtester.run_backtest(
            start_date="2020-01-01",
            end_date="2022-12-31",
            initial_capital=100_000.0,
            strategy_func=perfect_foresight,
            ic_hook=pearson_ic,
        )

        assert result.signal_ic is not None
        assert result.signal_ic["ic"] == pytest.approx(1.0, abs=1e-9)
        assert result.signal_ic["p_value"] < 0.001
        assert result.signal_ic["sample_size"] == result.total_trades

    def test_transaction_costs_reduce_returns(self, frictionless_result):
        costly = WalkForwardBacktester(
            warehouse=FakeWarehouse(), config=BacktestConfig()  # PaperTrader costs
        )

        def all_in_up(train_start, train_end, test_start, test_end, portfolio_value):
            return [{"ticker": "UP", "weight": 1.0}]

        costly_result = costly.run_backtest(
            start_date="2020-01-01",
            end_date="2023-12-31",
            initial_capital=100_000.0,
            strategy_func=all_in_up,
        )

        assert costly_result.total_return_pct < frictionless_result.total_return_pct
        assert costly_result.total_trades == frictionless_result.total_trades

    def test_trade_costs_match_paper_trader_schedule(self):
        backtester = WalkForwardBacktester(
            warehouse=FakeWarehouse(), config=BacktestConfig()
        )

        def all_in_up(train_start, train_end, test_start, test_end, portfolio_value):
            return [{"ticker": "UP", "weight": 1.0}]

        result = backtester.run_backtest(
            start_date="2020-01-01",
            end_date="2021-06-30",
            initial_capital=100_000.0,
            strategy_func=all_in_up,
        )

        trade = result.periods[0].trades[0]
        shares = trade["value_start"] / trade["entry_price"]
        expected_costs = (
            calculate_commission(shares, trade["entry_price"])
            + trade["value_start"] * PaperTrader.SLIPPAGE_LIQUID
            + calculate_commission(shares, trade["exit_price"])
            + shares * trade["exit_price"] * PaperTrader.SLIPPAGE_LIQUID
        )
        assert trade["transaction_costs"] == pytest.approx(expected_costs, rel=1e-9)

    def test_sharpe_annualized_by_period_frequency(self, frictionless_result):
        """
        v5.35.1 regression: monthly period returns must be annualized with the
        actual period frequency (~12/year), NOT the daily sqrt(252) factor
        that inflated Sharpe/Sortino ~4.6x. Independent recomputation from the
        equity curve pins the exact value.
        """
        equity = frictionless_result.equity_curve
        returns = equity["return"].values

        spacing_days = np.diff(pd.to_datetime(equity["date"]).values).astype(
            "timedelta64[D]"
        ).astype(float)
        periods_per_year = 365.25 / float(np.median(spacing_days))
        # Monthly walk-forward windows → ~12 periods/year, nowhere near daily
        assert 11.0 < periods_per_year < 13.5

        excess = returns - 0.02 / periods_per_year
        expected_sharpe = np.sqrt(periods_per_year) * (excess.mean() / excess.std())
        assert frictionless_result.sharpe_ratio == pytest.approx(
            expected_sharpe, rel=1e-9
        )

        # The old bug produced sqrt(252/12)≈4.6x this value
        inflated = np.sqrt(252) * ((returns - 0.02 / 252).mean() / (returns - 0.02 / 252).std())
        assert frictionless_result.sharpe_ratio < inflated


# =============================================================================
# IC walk-forward validation (the Simons gate)
# =============================================================================


class TestWalkForwardICValidation:
    def test_perfect_signal_validates_out_of_sample(self):
        rng = np.random.default_rng(42)
        signals = rng.standard_normal(700).tolist()
        returns = [0.01 * s for s in signals]  # Perfectly linear relation

        result = walk_forward_ic_validation(
            signal_values=signals,
            forward_returns=returns,
            train_size=300,
            test_size=100,
            ic_func=pearson_ic,
            pattern_name="synthetic_perfect",
        )

        assert result["pattern_name"] == "synthetic_perfect"
        assert result["n_windows"] == 4  # starts at 0, 100, 200, 300
        assert result["avg_in_sample_ic"] == pytest.approx(1.0, abs=1e-12)
        assert result["avg_out_of_sample_ic"] == pytest.approx(1.0, abs=1e-12)
        assert result["avg_degradation"] == pytest.approx(0.0, abs=1e-12)
        assert "✅" in result["interpretation"]
        for w in result["walk_forward_results"]:
            assert w["test_p_value"] < 0.001
            assert w["train_end"] <= w["test_start"]  # No-lookahead split

    def test_exact_boundary_produces_one_window(self):
        # Regression: old PatternBacktest loop returned no_results here
        signals = list(np.linspace(-1, 1, 400))
        returns = [0.02 * s for s in signals]
        result = walk_forward_ic_validation(
            signals, returns, train_size=300, test_size=100, ic_func=pearson_ic
        )
        assert result["n_windows"] == 1

    def test_insufficient_data_status(self):
        result = walk_forward_ic_validation(
            [0.1] * 50, [0.01] * 50, train_size=300, test_size=100, ic_func=pearson_ic
        )
        assert result["status"] == "insufficient_data"

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError):
            walk_forward_ic_validation(
                [0.1] * 10, [0.01] * 9, train_size=5, test_size=2, ic_func=pearson_ic
            )

    def test_interpretation_thresholds(self):
        assert "fails" in interpret_walk_forward_ic(0.01, 0.0)
        assert "overfitting" in interpret_walk_forward_ic(0.05, 0.10)
        assert "Strong" in interpret_walk_forward_ic(0.10, 0.0)
        assert "Moderate" in interpret_walk_forward_ic(0.05, 0.0)


# =============================================================================
# Day-window strategy walk-forward (ported from WalkForwardAnalyzer)
# =============================================================================


class TestStrategyWalkForward:
    @staticmethod
    def _rising_price_frame(num_days: int) -> pd.DataFrame:
        start = datetime(2022, 1, 3)
        rows = []
        for i in range(num_days):
            price = 100.0 * (1.003 ** i)  # Deterministic +0.3%/day
            rows.append(
                {
                    "date": start + timedelta(days=i),
                    "ticker": "TEST",
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1_000_000,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _buy_hold(current_date, prices, portfolio):
        if not portfolio["positions"]:
            return [
                TradeSignal(
                    ticker="TEST",
                    action=Action.BUY,
                    confidence=1.0,
                    position_size_pct=0.95,
                )
            ]
        return []

    def test_rising_market_buy_hold_is_consistent(self):
        backtester = WalkForwardBacktester(warehouse=FakeWarehouse())
        results = backtester.run_strategy_walk_forward(
            data=self._rising_price_frame(300),
            strategy=self._buy_hold,
            in_sample_days=100,
            out_of_sample_days=50,
        )

        # Windows: starts at 0, 50, 100, 150 (150 + 150 <= 300)
        assert results.windows_tested == 4
        assert results.windows_profitable == 4
        assert results.consistency_score == pytest.approx(100.0)
        assert results.total_oos_return_pct > 0
        assert results.avg_is_return_pct > 0
        assert results.oos_max_drawdown_pct >= -1.0  # Monotonic rise, tiny DD at most

    def test_insufficient_data_raises(self):
        from modules.exceptions import InsufficientDataError

        backtester = WalkForwardBacktester(warehouse=FakeWarehouse())
        with pytest.raises(InsufficientDataError):
            backtester.run_strategy_walk_forward(
                data=self._rising_price_frame(50),
                strategy=self._buy_hold,
                in_sample_days=100,
                out_of_sample_days=50,
            )


# =============================================================================
# v6.2.0 (Strategy v1.1 H1): delta-rebalance execution mode
# =============================================================================


def _static_5050(train_start, train_end, test_start, test_end, portfolio_value):
    """Static target portfolio: identical weights every window."""
    return [
        {"ticker": "FLAT1", "weight": 0.5, "signal": 1.0},
        {"ticker": "FLAT2", "weight": 0.5, "signal": 1.0},
    ]


class TestDeltaRebalanceMode:
    """H1: trade only the delta between consecutive target portfolios."""

    @staticmethod
    def delta_config(**overrides) -> BacktestConfig:
        # Short train window so several monthly periods fit quickly; DEFAULT
        # PaperTrader cost constants stay ON (the regression must hold with
        # real costs, not zero costs).
        params = dict(training_months=1, testing_months=1, rebalance_mode="delta")
        params.update(overrides)
        return BacktestConfig(**params)

    def _run(self, strategy, start="2020-01-01", end="2020-07-02", **overrides):
        backtester = WalkForwardBacktester(
            warehouse=FakeWarehouse(), config=self.delta_config(**overrides)
        )
        result = backtester.run_backtest(start, end, strategy_func=strategy)
        return backtester, result

    @pytest.mark.critical
    def test_delta_mode_static_targets_zero_rebalance_cost(self):
        """THE pre-registered H1 regression: a static target portfolio across
        consecutive windows incurs ZERO rebalance cost (window 1 pays the
        initial entry; the final window pays terminal liquidation only)."""
        _, result = self._run(_static_5050)
        periods = result.periods

        assert len(periods) >= 4
        assert periods[0].period_costs > 0  # initial entry legs
        for p in periods[1:-1]:
            assert p.period_costs == 0.0
            assert p.period_traded_notional == 0.0
            assert all(t["transaction_costs"] == 0.0 for t in p.trades)
        assert periods[-1].period_costs > 0  # terminal liquidation legs

    def test_default_mode_unchanged_full_liquidation(self):
        """Backward compatibility: the default config still liquidates and
        re-enters every window (costs charged in EVERY period), and the
        counterfactual equals the actual charge."""
        assert BacktestConfig().rebalance_mode == "full_liquidation"
        backtester = WalkForwardBacktester(
            warehouse=FakeWarehouse(),
            config=BacktestConfig(training_months=1, testing_months=1),
        )
        result = backtester.run_backtest(
            "2020-01-01", "2020-07-02", strategy_func=_static_5050
        )
        for p in result.periods:
            assert p.period_costs > 0  # round-trip legs every month
            assert p.counterfactual_full_liquidation_costs == pytest.approx(
                p.period_costs
            )

    def test_delta_mode_dropped_ticker_sold_with_cost(self):
        """A ticker dropped from the targets is sold in full; its sale cost is
        period-level accounting (no trade record — it isn't held that window)."""

        def switching(train_start, train_end, test_start, test_end, portfolio_value):
            if test_start < date(2020, 4, 1):
                return [{"ticker": "FLAT1", "weight": 0.85, "signal": 1.0}]
            return [{"ticker": "FLAT2", "weight": 0.85, "signal": 1.0}]

        backtester, result = self._run(switching)
        switch = next(
            p for p in result.periods if p.test_start >= date(2020, 4, 1)
        )
        tickers_traded = {t["ticker"] for t in switch.trades}
        assert tickers_traded == {"FLAT2"}
        # Period costs cover BOTH the FLAT1 sale and the FLAT2 buy — more than
        # the per-trade records (FLAT2 only) can account for.
        assert switch.period_costs > sum(
            t["transaction_costs"] for t in switch.trades
        ) > 0
        assert "FLAT1" not in backtester._holdings

    def test_delta_mode_final_period_liquidates(self):
        """Terminal window exits every holding (cash-only end state)."""
        backtester, result = self._run(_static_5050)
        assert backtester._holdings == {}
        # Final equity equals settled cash; with flat prices the only loss is
        # entry legs + terminal exit legs.
        final = result.periods[-1]
        total_costs = sum(p.period_costs for p in result.periods)
        assert final.portfolio_value_end == pytest.approx(
            100_000.0 - total_costs, abs=0.01
        )

    def test_delta_mode_counterfactual_geq_actual(self):
        """The full-liquidation counterfactual can never be cheaper than the
        delta execution on identical targets."""
        _, result = self._run(_static_5050)
        for p in result.periods:
            assert (
                p.counterfactual_full_liquidation_costs >= p.period_costs - 1e-9
            )
        # And it is STRICTLY greater whenever holdings persist.
        middle = result.periods[1:-1]
        assert all(
            p.counterfactual_full_liquidation_costs > 0 and p.period_costs == 0.0
            for p in middle
        )

    def test_delta_mode_cost_drag_identity(self):
        """Σ period_costs must equal the canonical cost-drag total (two
        accumulators, one set of trades) and the gross/net identity holds."""
        _, result = self._run(_static_5050)
        assert result.cost_drag is not None
        assert result.cost_drag.total_costs == pytest.approx(
            sum(p.period_costs for p in result.periods), abs=0.01
        )
        # Flat prices: gross curve must end exactly at initial capital.
        assert result.cost_drag.gross_return_pct == pytest.approx(0.0, abs=1e-6)

    def test_delta_band_does_not_swallow_signal_change(self):
        """A real 1-percentage-point weight shift trades (the dust band only
        suppresses cost-feedback noise, ~0.1% of a position)."""

        def shifting(train_start, train_end, test_start, test_end, portfolio_value):
            w = 0.10 if test_start < date(2020, 4, 1) else 0.09
            return [
                {"ticker": "FLAT1", "weight": w, "signal": 1.0},
                {"ticker": "FLAT2", "weight": 0.50, "signal": 1.0},
            ]

        _, result = self._run(shifting)
        shift = next(p for p in result.periods if p.test_start >= date(2020, 4, 1))
        assert shift.period_costs > 0
        # Traded ≈ 1% of portfolio (the FLAT1 delta), nowhere near a round-trip.
        assert 0.5 < shift.period_traded_notional / shift.portfolio_value_start * 100 < 2.0

    def test_delta_mode_portfolio_chain_continuity(self):
        """Period N's end value is period N+1's start value — the invariant
        the verdict runner's metrics integrity check relies on."""
        _, result = self._run(_static_5050)
        for prev, curr in zip(result.periods, result.periods[1:]):
            assert curr.portfolio_value_start == pytest.approx(
                prev.portfolio_value_end, rel=1e-9
            )

    def test_delta_mode_missing_held_price_raises(self):
        """A live position that can no longer be priced must fail LOUD."""

        class VanishingWarehouse(FakeWarehouse):
            def get_ticker_prices(self, ticker, start_date=None, end_date=None):
                if ticker == "FLAT1" and start_date and start_date >= date(2020, 3, 20):
                    return pd.DataFrame()
                return super().get_ticker_prices(ticker, start_date, end_date)

        def flat1_only(train_start, train_end, test_start, test_end, portfolio_value):
            return [{"ticker": "FLAT1", "weight": 0.85, "signal": 1.0}]

        backtester = WalkForwardBacktester(
            warehouse=VanishingWarehouse(), config=self.delta_config()
        )
        with pytest.raises(RuntimeError, match="held ticker FLAT1"):
            backtester.run_backtest("2020-01-01", "2020-07-02", strategy_func=flat1_only)

    def test_delta_mode_bil_style_sleeve_excluded_from_ic(self):
        """A signal=None position (the H2 cash sleeve) trades and earns but
        stays OUT of the signal-IC sample."""

        def with_sleeve(train_start, train_end, test_start, test_end, portfolio_value):
            return [
                {"ticker": "UP", "weight": 0.85, "signal": 0.9},
                {"ticker": "FLAT1", "weight": 0.15, "signal": None},
            ]

        backtester = WalkForwardBacktester(
            warehouse=FakeWarehouse(), config=self.delta_config()
        )
        result = backtester.run_backtest(
            "2020-01-01", "2020-07-02", strategy_func=with_sleeve, ic_hook=pearson_ic
        )
        sleeve_trades = [
            t for p in result.periods for t in p.trades if t["ticker"] == "FLAT1"
        ]
        assert sleeve_trades  # the sleeve is held and recorded
        assert result.signal_ic is not None
        # Only UP's signals entered the IC sample: one pair per period.
        assert result.signal_ic["sample_size"] == len(result.periods)
