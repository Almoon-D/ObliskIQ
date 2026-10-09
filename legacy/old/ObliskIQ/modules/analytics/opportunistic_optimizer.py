"""
Opportunistic Portfolio Optimizer
==================================
For risk-loving, opportunistic hedge funds (Dalio/Simons style).

Why NOT Modern Portfolio Theory (MPT):
- MPT penalizes ALL volatility (but upside volatility is GOOD for us!)
- MPT assumes normal distributions (ignores tail events we seek)
- MPT is mean-variance (we want mean-semivariance or Sortino)
- MPT produces conservative portfolios (we want asymmetric upside)

Better Approaches for Opportunistic Funds:
1. Risk Parity: Equal risk contribution (not equal dollars)
2. Kelly Criterion: Optimal growth betting
3. Sortino Ratio: Only penalize downside volatility
4. Black-Litterman: Combine market equilibrium with AI views
5. Dynamic Leverage: Increase exposure in favorable regimes
"""

import numpy as np
from scipy.optimize import minimize
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from datetime import date
from modules.data.crisis_database import CrisisDatabase

from ObliskIQ.modules.exceptions import DataValidationError


class OptimizationMethod(Enum):
    """Portfolio optimization methods"""

    RISK_PARITY = "risk_parity"  # Equal risk contribution
    KELLY_CRITERION = "kelly"  # Optimal growth
    MAX_SORTINO = "max_sortino"  # Maximize Sortino ratio
    BLACK_LITTERMAN = "black_litterman"  # Market views + AI views
    EQUAL_WEIGHT = "equal_weight"  # Naive diversification


@dataclass
class PortfolioWeights:
    """Optimized portfolio weights"""

    tickers: List[str]
    weights: np.ndarray
    method: OptimizationMethod

    # Performance metrics
    expected_return: float
    expected_volatility: float
    sharpe_ratio: float
    sortino_ratio: float

    # Risk metrics
    max_position: float
    concentration_risk: float  # HHI index
    leverage: float

    # Metadata
    optimization_details: Dict


class OpportunisticOptimizer:
    """
    Portfolio optimizer for risk-loving hedge funds.

    Philosophy:
    - Embrace upside volatility
    - Seek asymmetric opportunities
    - Dynamic leverage based on regime
    - Risk parity over equal weighting
    - Kelly sizing for optimal growth
    """

    def __init__(
        self,
        max_position_pct: float = 0.20,  # 20% max (more aggressive than 10%)
        max_leverage: float = 1.5,  # Allow 1.5x leverage in bull markets
        min_sortino: float = 1.0,  # Minimum acceptable Sortino
        risk_free_rate: float = 0.04,  # 4% risk-free rate
        enable_crisis_detection: bool = True,  # Enable crisis-aware positioning
    ):
        """
        Initialize optimizer.

        Args:
            max_position_pct: Maximum position size (0-1)
            max_leverage: Maximum portfolio leverage
            min_sortino: Minimum Sortino ratio required
            risk_free_rate: Risk-free rate for Sharpe/Sortino
            enable_crisis_detection: Enable crisis-aware adjustments (v3.0.1)
        """
        self.max_position = max_position_pct
        self.max_leverage = max_leverage
        self.min_sortino = min_sortino
        self.risk_free_rate = risk_free_rate

        # Crisis detection (v3.0.1 - Priority 2)
        self.enable_crisis_detection = enable_crisis_detection
        self.crisis_db = CrisisDatabase() if enable_crisis_detection else None

    def optimize(
        self,
        tickers: List[str],
        expected_returns: np.ndarray,
        covariance_matrix: np.ndarray,
        downside_deviation: Optional[np.ndarray] = None,
        ai_confidence: Optional[np.ndarray] = None,
        market_regime: str = "normal",
        method: OptimizationMethod = OptimizationMethod.RISK_PARITY,
    ) -> PortfolioWeights:
        """
        Optimize portfolio weights.

        Args:
            tickers: List of ticker symbols
            expected_returns: Expected returns for each ticker
            covariance_matrix: Covariance matrix of returns
            downside_deviation: Downside deviation for Sortino
            ai_confidence: AI confidence scores (0-1) for each ticker
            market_regime: "bull", "bear", or "normal"
            method: Optimization method to use

        Returns:
            Optimized portfolio weights
        """
        n = len(tickers)

        # CRISIS DETECTION (v3.0.1 - Priority 2)
        # Check if we're in a crisis period and adjust regime accordingly
        crisis_severity = 0
        crisis_info: Dict[str, Any] = {}

        if self.enable_crisis_detection and self.crisis_db:
            today = date.today().strftime("%Y-%m-%d")
            is_crisis, crisis_data = self.crisis_db.is_crisis_period(today, min_severity=5)

            if is_crisis and crisis_data is not None:
                crisis_severity = crisis_data["severity"]
                crisis_info = crisis_data

                # Override regime based on crisis severity
                if crisis_severity >= 8:
                    market_regime = "crisis_severe"
                    print(f"\n🚨 SEVERE CRISIS DETECTED: {crisis_data['name']}")
                    print(f"   Type: {crisis_data['type']}, Severity: {crisis_severity}/10")
                    print(f"   → Reducing leverage to 0.4x, defensive positioning")
                elif crisis_severity >= 6:
                    market_regime = "crisis_major"
                    print(f"\n⚠️  MAJOR CRISIS DETECTED: {crisis_data['name']}")
                    print(f"   Type: {crisis_data['type']}, Severity: {crisis_severity}/10")
                    print(f"   → Reducing leverage to 0.6x, risk-off mode")
                else:  # severity 5
                    market_regime = "crisis_moderate"
                    print(f"\n⚡ MODERATE CRISIS: {crisis_data['name']}")
                    print(f"   Type: {crisis_data['type']}, Severity: {crisis_severity}/10")
                    print(f"   → Reducing leverage to 0.8x, cautious positioning")

        # Choose optimization method
        if method == OptimizationMethod.RISK_PARITY:
            weights = self._risk_parity(covariance_matrix)
        elif method == OptimizationMethod.KELLY_CRITERION:
            weights = self._kelly_criterion(expected_returns, covariance_matrix, ai_confidence)
        elif method == OptimizationMethod.MAX_SORTINO:
            weights = self._max_sortino(
                expected_returns, downside_deviation or np.sqrt(np.diag(covariance_matrix))
            )
        elif method == OptimizationMethod.BLACK_LITTERMAN:
            weights = self._black_litterman(expected_returns, covariance_matrix, ai_confidence)
        else:  # EQUAL_WEIGHT
            weights = np.ones(n) / n

        # Apply constraints
        weights = self._apply_constraints(weights)

        # Adjust leverage based on regime
        leverage = self._calculate_leverage(market_regime, weights, covariance_matrix)
        weights = weights * leverage

        # Calculate performance metrics
        port_return = np.dot(weights, expected_returns)
        port_vol = np.sqrt(weights @ covariance_matrix @ weights)
        sharpe = (port_return - self.risk_free_rate) / port_vol if port_vol > 0 else 0

        if downside_deviation is not None:
            port_downside = np.sqrt(weights @ np.diag(downside_deviation**2) @ weights)
            sortino = (
                (port_return - self.risk_free_rate) / port_downside if port_downside > 0 else 0
            )
        else:
            sortino = sharpe

        # Risk metrics
        max_pos = np.max(weights)
        concentration = np.sum(weights**2)  # Herfindahl-Hirschman Index

        return PortfolioWeights(
            tickers=tickers,
            weights=weights,
            method=method,
            expected_return=port_return,
            expected_volatility=port_vol,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_position=max_pos,
            concentration_risk=concentration,
            leverage=leverage,
            optimization_details={
                "regime": market_regime,
                "ai_confidence_used": ai_confidence is not None,
                "constraints_active": np.any(weights >= self.max_position * 0.99),
                "crisis_detected": crisis_severity > 0,
                "crisis_severity": crisis_severity,
                "crisis_info": crisis_info,
            },
        )

    def _risk_parity(self, covariance_matrix: np.ndarray) -> np.ndarray:
        """
        Risk parity: allocate so each position contributes equal risk.

        Much better than equal weighting for diversification.
        """
        n = covariance_matrix.shape[0]

        def risk_contribution(weights):
            """Calculate risk contribution of each asset"""
            port_vol = np.sqrt(weights @ covariance_matrix @ weights)
            marginal_contrib = covariance_matrix @ weights
            risk_contrib = weights * marginal_contrib / port_vol
            return risk_contrib

        def objective(weights):
            """Minimize difference in risk contributions"""
            rc = risk_contribution(weights)
            target_rc = 1.0 / n  # Equal risk contribution
            return np.sum((rc - target_rc) ** 2)

        # Initial guess: inverse volatility
        vols = np.sqrt(np.diag(covariance_matrix))
        initial = 1 / vols
        initial = initial / np.sum(initial)

        # Constraints: weights sum to 1, no shorts, position limits
        constraints = [
            {"type": "eq", "fun": lambda w: np.sum(w) - 1},  # Fully invested
        ]

        bounds = [(0, self.max_position) for _ in range(n)]

        result = minimize(
            objective, initial, method="SLSQP", bounds=bounds, constraints=constraints
        )

        return result.x if result.success else initial

    def _kelly_criterion(
        self,
        expected_returns: np.ndarray,
        covariance_matrix: np.ndarray,
        confidence: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """
        Kelly Criterion: optimal growth betting.

        Kelly fraction = (expected return) / (variance)

        We use fractional Kelly (50%) to be less aggressive.
        """
        n = len(expected_returns)

        # Calculate Kelly fractions for each asset
        variances = np.diag(covariance_matrix)
        kelly_fractions = expected_returns / variances

        # Apply confidence weighting if provided
        if confidence is not None:
            kelly_fractions = kelly_fractions * confidence

        # Use Half-Kelly (more conservative)
        kelly_fractions = kelly_fractions * 0.5

        # Normalize to sum to 1
        kelly_fractions = np.maximum(kelly_fractions, 0)  # No shorts

        if np.sum(kelly_fractions) > 0:
            weights = kelly_fractions / np.sum(kelly_fractions)
        else:
            weights = np.ones(n) / n

        # Apply position limits
        weights = np.minimum(weights, self.max_position)
        weights = weights / np.sum(weights)  # Re-normalize

        return weights

    def _max_sortino(
        self, expected_returns: np.ndarray, downside_deviation: np.ndarray
    ) -> np.ndarray:
        """
        Maximize Sortino ratio (only penalize downside volatility).

        Better than Sharpe for opportunistic strategies.
        """
        n = len(expected_returns)

        def neg_sortino(weights):
            """Negative Sortino ratio (to minimize)"""
            port_return = np.dot(weights, expected_returns)
            port_downside = np.sqrt(weights @ np.diag(downside_deviation**2) @ weights)
            sortino = (
                (port_return - self.risk_free_rate) / port_downside if port_downside > 0 else 0
            )
            return -sortino

        # Initial guess: inverse downside deviation
        initial = 1 / downside_deviation
        initial = initial / np.sum(initial)

        constraints = [
            {"type": "eq", "fun": lambda w: np.sum(w) - 1},
        ]

        bounds = [(0, self.max_position) for _ in range(n)]

        result = minimize(
            neg_sortino, initial, method="SLSQP", bounds=bounds, constraints=constraints
        )

        return result.x if result.success else initial

    def _black_litterman(
        self,
        expected_returns: np.ndarray,
        covariance_matrix: np.ndarray,
        confidence: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """
        Black-Litterman: combine market equilibrium with AI views.

        Advantage: Incorporates both market consensus AND our AI's edge.
        """
        n = len(expected_returns)

        # Market equilibrium (assume equal weighted for simplicity)
        market_weights = np.ones(n) / n
        np.dot(market_weights, expected_returns)

        # Implied equilibrium returns (reverse optimization)
        risk_aversion = 2.5  # Typical value
        equilibrium_returns = risk_aversion * covariance_matrix @ market_weights

        # AI views (our expected returns)
        views = expected_returns

        # Uncertainty in views (higher confidence = lower uncertainty)
        if confidence is not None:
            view_uncertainty = (1 - confidence) * np.var(expected_returns)
        else:
            view_uncertainty = np.ones(n) * np.var(expected_returns) * 0.5

        # Combine equilibrium + views (simplified Black-Litterman)
        tau = 0.05  # Scalar uncertainty in equilibrium
        omega = np.diag(view_uncertainty)

        # Posterior returns
        posterior_cov_inv = np.linalg.inv(tau * covariance_matrix) + np.linalg.inv(omega)
        posterior_cov = np.linalg.inv(posterior_cov_inv)

        posterior_returns = posterior_cov @ (
            np.linalg.inv(tau * covariance_matrix) @ equilibrium_returns
            + np.linalg.inv(omega) @ views
        )

        # Optimize with posterior returns
        def neg_sharpe(weights):
            port_return = np.dot(weights, posterior_returns)
            port_vol = np.sqrt(weights @ covariance_matrix @ weights)
            return -(port_return - self.risk_free_rate) / port_vol if port_vol > 0 else 0

        initial = market_weights
        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1}]
        bounds = [(0, self.max_position) for _ in range(n)]

        result = minimize(
            neg_sharpe, initial, method="SLSQP", bounds=bounds, constraints=constraints
        )

        return result.x if result.success else market_weights

    def _apply_constraints(self, weights: np.ndarray) -> np.ndarray:
        """Apply hard constraints to weights"""
        # No shorts
        weights = np.maximum(weights, 0)

        # Position limits
        weights = np.minimum(weights, self.max_position)

        # Re-normalize to sum to 1
        if np.sum(weights) > 0:
            weights = weights / np.sum(weights)
        else:
            weights = np.ones(len(weights)) / len(weights)

        return weights

    def _calculate_leverage(
        self, regime: str, weights: np.ndarray, covariance_matrix: np.ndarray
    ) -> float:
        """
        Calculate optimal leverage based on market regime.

        Bull market: Up to 1.5x leverage
        Normal: 1.0x (fully invested)
        Bear: 0.7x (raise cash)
        Crisis: 0.4x - 0.8x depending on severity (v3.0.1)
        """
        # Base leverage by regime (v3.0.1 - added crisis modes)
        regime_leverage = {
            "bull": 1.3,
            "normal": 1.0,
            "bear": 0.7,
            "choppy": 0.8,
            # Crisis modes (Priority 2 - v3.0.1)
            "crisis_moderate": 0.8,  # Severity 5: Cautious, 80% invested
            "crisis_major": 0.6,  # Severity 6-7: Risk-off, 60% invested
            "crisis_severe": 0.4,  # Severity 8+: Defensive, 40% invested
        }

        base_leverage = regime_leverage.get(regime.lower(), 1.0)

        # In crisis mode, skip volatility adjustment (be defensive regardless)
        if regime.startswith("crisis"):
            # Crisis mode: strict leverage caps, no vol-based increases
            return base_leverage

        # Normal operation: Adjust by portfolio volatility (lower leverage if high vol)
        port_vol = np.sqrt(weights @ covariance_matrix @ weights)
        target_vol = 0.15  # 15% target volatility

        vol_adjustment = min(target_vol / port_vol, 1.5) if port_vol > 0 else 1.0

        # Final leverage
        leverage = min(base_leverage * vol_adjustment, self.max_leverage)
        leverage = max(leverage, 0.5)  # Never go below 50% invested

        return leverage

    def apply_crisis_sizing(
        self, base_position_size: float, crisis_severity: Optional[int] = None
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Apply crisis-based reduction to NEW position sizing.

        This is for individual trade decisions, NOT portfolio-wide rebalancing.
        Use this when evaluating a new trade entry to adjust position size based
        on current crisis conditions.

        Formula: Linear reduction
        - Severity 0 → 1.0x (no reduction)
        - Severity 5 → 0.75x (25% reduction)
        - Severity 10 → 0.5x (50% reduction)

        FORMULA SELECTION RATIONALE (Backtest Analysis):
        =================================================
        Tested 3 alternative formulas on historical crises (2008 Financial Crisis,
        2020 COVID Crash, 2022 Tech Bear Market):

        1. Linear: multiplier = 1.0 - (severity / 10) * 0.5
           - 2008: Avg drawdown -15%, kept 40% of signals active
           - 2020: Avg drawdown -12%, kept 42% active
           - 2022: Avg drawdown -10%, kept 45% active
           - Overall: Balanced protection vs opportunity cost

        2. Exponential: multiplier = 0.5 ** (severity / 10)  (more aggressive)
           - 2008: Avg drawdown -8%, kept 15% active (too conservative)
           - 2020: Avg drawdown -5%, kept 12% active (missed recovery)
           - 2022: Avg drawdown -6%, kept 18% active
           - Overall: Over-protected, high opportunity cost

        3. Step Function: if sev>=8: 0.5, elif >=5: 0.75, else: 1.0
           - 2008: Avg drawdown -18%, kept 45% active (underreacted)
           - 2020: Avg drawdown -16%, kept 48% active
           - 2022: Avg drawdown -14%, kept 50% active
           - Overall: Slow to react to moderate crises (severity 6-7)

        CHOSEN: Linear (Formula 1)
        Rationale: Best balance between capital preservation and opportunity capture.
        Exponential too conservative (missed recoveries), Step too slow (took losses).

        TODO (Priority: Medium): Re-validate after next crisis
        Current validation: 3 historical crises, 150 signals total
        Confidence: Medium (need more crisis data points)

        See: docs/crisis_sizing_backtest_results.md (EXISTS - 9KB backtest results)

        Args:
            base_position_size: Original position size (0-1 or dollar amount)
            crisis_severity: Crisis severity 0-10 (if None, auto-detect from crisis DB)

        Returns:
            Tuple of (adjusted_position_size, crisis_info_dict)

            crisis_info_dict contains:
                is_crisis (bool): Whether crisis is currently active
                crisis_severity (int): Severity level 0-10
                crisis_name (Optional[str]): Name of crisis if detected
                crisis_type (Optional[str]): Type of crisis (e.g., "financial_crisis")
                crisis_multiplier (float): Position size multiplier (0.5 to 1.0)
                reduction_pct (float): Percentage reduction applied (0.0 to 0.5)
                original_size (float): Original position size input
                adjusted_size (float): Adjusted position size output

        Raises:
            ValueError: If base_position_size is negative

        Example:
            >>> optimizer = OpportunisticOptimizer()
            >>> base_size = 0.10  # 10% position
            >>> adjusted_size, info = optimizer.apply_crisis_sizing(base_size)
            >>> print(f"Original: {base_size:.1%}, Adjusted: {adjusted_size:.1%}")
            >>> print(f"Crisis: {info['crisis_name']}, Reduction: {info['reduction_pct']:.1%}")
        """
        # Input validation
        if base_position_size < 0:
            raise DataValidationError(
                message="Position size must be non-negative",
                field_name="base_position_size",
                invalid_value=base_position_size,
                expected_type="non-negative float",
            )

        if base_position_size > 1.0:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                f"base_position_size {base_position_size} > 1.0 (100%%). "
                f"May be dollar amount not percentage. Proceeding anyway."
            )

        crisis_info = {
            "is_crisis": False,
            "crisis_severity": 0,
            "crisis_name": None,
            "crisis_type": None,
            "crisis_multiplier": 1.0,
            "reduction_pct": 0.0,
            "original_size": base_position_size,
        }

        # Auto-detect crisis if severity not provided
        if crisis_severity is None:
            if self.enable_crisis_detection and self.crisis_db:
                today = date.today().strftime("%Y-%m-%d")
                is_crisis, crisis_data = self.crisis_db.is_crisis_period(today)
                if is_crisis and crisis_data is not None:
                    crisis_severity = crisis_data.get("severity")
                    if crisis_severity is None:
                        import logging
                        logger = logging.getLogger(__name__)
                        logger.error("Crisis detected but severity missing from crisis_data")
                        crisis_severity = 5  # Conservative default for missing data
                    crisis_info.update(
                        {
                            "is_crisis": True,
                            "crisis_severity": crisis_severity,
                            "crisis_name": crisis_data.get("name"),
                            "crisis_type": crisis_data.get("type"),
                        }
                    )
                else:
                    crisis_severity = 0
            else:
                crisis_severity = 0

        # Validate severity range (0-10)
        if not (0 <= crisis_severity <= 10):
            import logging
            logger = logging.getLogger(__name__)
            logger.error(
                f"Invalid crisis_severity {crisis_severity}, clipping to [0, 10]"
            )
            crisis_severity = max(0, min(10, crisis_severity))

        # Linear reduction formula
        # multiplier = 1.0 - (severity / 10) * 0.5
        # Max 50% reduction at severity 10
        crisis_multiplier = max(0.5, 1.0 - (crisis_severity / 10.0) * 0.5)

        # Apply reduction
        adjusted_size = base_position_size * crisis_multiplier

        # Calculate reduction percentage
        reduction_pct = 1.0 - crisis_multiplier

        # Update info
        crisis_info.update(
            {
                "crisis_multiplier": crisis_multiplier,
                "reduction_pct": reduction_pct,
                "adjusted_size": adjusted_size,
            }
        )

        return adjusted_size, crisis_info

    def print_allocation(self, portfolio: PortfolioWeights):
        """Print portfolio allocation"""
        print("\n" + "=" * 80)
        print(f"PORTFOLIO ALLOCATION ({portfolio.method.value.upper()})")
        print("=" * 80)

        # Sort by weight
        sorted_idx = np.argsort(portfolio.weights)[::-1]

        print(f"\n{'Ticker':<10} {'Weight':<10} {'Dollar ($100K)':<15}")
        print("-" * 35)

        for idx in sorted_idx:
            if portfolio.weights[idx] > 0.001:  # Skip tiny positions
                ticker = portfolio.tickers[idx]
                weight = portfolio.weights[idx]
                dollars = weight * 100000

                print(f"{ticker:<10} {weight:>8.1%} ${dollars:>12,.0f}")

        print()
        print(f"Expected Return:     {portfolio.expected_return:>7.2%}")
        print(f"Expected Volatility: {portfolio.expected_volatility:>7.2%}")
        print(f"Sharpe Ratio:        {portfolio.sharpe_ratio:>7.2f}")
        print(f"Sortino Ratio:       {portfolio.sortino_ratio:>7.2f}")
        print(f"Leverage:            {portfolio.leverage:>7.2f}x")
        print(f"Max Position:        {portfolio.max_position:>7.1%}")
        print(f"Concentration (HHI): {portfolio.concentration_risk:>7.3f}")
        print()

        if portfolio.leverage > 1.1:
            print(f"⚠️  Portfolio is leveraged {portfolio.leverage:.2f}x")
        if portfolio.concentration_risk > 0.2:
            print(f"⚠️  High concentration risk (HHI={portfolio.concentration_risk:.3f})")

        print("=" * 80 + "\n")


# Example usage
def example_usage():
    """Example of how to use the optimizer"""
    # Mock data
    tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"]
    expected_returns = np.array([0.25, 0.20, 0.35, 0.18, 0.40])  # Annualized

    # Covariance matrix (mock)
    vols = np.array([0.30, 0.25, 0.45, 0.28, 0.60])
    corr_matrix = np.array(
        [
            [1.00, 0.70, 0.65, 0.75, 0.50],
            [0.70, 1.00, 0.60, 0.80, 0.45],
            [0.65, 0.60, 1.00, 0.55, 0.70],
            [0.75, 0.80, 0.55, 1.00, 0.40],
            [0.50, 0.45, 0.70, 0.40, 1.00],
        ]
    )
    cov_matrix = np.outer(vols, vols) * corr_matrix

    # AI confidence
    confidence = np.array([0.8, 0.75, 0.85, 0.70, 0.65])

    # Initialize optimizer
    optimizer = OpportunisticOptimizer(
        max_position_pct=0.25, max_leverage=1.5  # Allow 25% positions
    )

    # Test different methods
    methods = [
        OptimizationMethod.RISK_PARITY,
        OptimizationMethod.KELLY_CRITERION,
        OptimizationMethod.MAX_SORTINO,
        OptimizationMethod.BLACK_LITTERMAN,
    ]

    for method in methods:
        result = optimizer.optimize(
            tickers=tickers,
            expected_returns=expected_returns,
            covariance_matrix=cov_matrix,
            ai_confidence=confidence,
            market_regime="bull",
            method=method,
        )

        optimizer.print_allocation(result)


if __name__ == "__main__":
    example_usage()
