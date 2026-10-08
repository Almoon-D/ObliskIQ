"""
Phase 4: Deterministic Decision Engine

CRITICAL ARCHITECTURE DECISION:
- Position sizing is DETERMINISTIC (Kelly Criterion, not AI)
- Buy/sell signals are RULES-BASED (not AI)
- Risk limits are HARD-CODED (not AI suggestions)
- AI is DEMOTED to explanation/commentary role only

PROBLEM WITH OLD DESIGN:
- Phase 4 asked Gemini for position sizes
- One AI hallucination: "75%" instead of "7.5%" = BLOWN ACCOUNT
- Cannot backtest AI decisions
- Cannot audit reasoning

NEW DESIGN:
1. Calculate Kelly Criterion position sizes (deterministic)
2. Apply scenario-based stop-loss/take-profit rules
3. Validate against RiskEnforcer hard limits
4. Generate executable order specifications
5. Optional: Ask AI to explain reasoning (not decide)

This module is fully testable and auditable.
"""

import logging
from typing import Dict, List, Tuple, Any, Optional, TYPE_CHECKING
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

if TYPE_CHECKING:
    from modules.analytics.reflexivity_learner import ReflexivityLearner
    from modules.execution.tranche_planner import TranchePlanner, TranchePlan

logger = logging.getLogger(__name__)


# Default reflexivity component weights from CLAUDE.md
DEFAULT_REFLEXIVITY_WEIGHTS: Dict[str, float] = {
    "sentiment": 0.30,
    "policy": 0.30,
    "crowding": 0.20,
    "narrative": 0.20,
}


class ActionType(Enum):
    """Possible actions for a ticker"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    REDUCE = "reduce"  # Partial sell
    ADD = "add"  # Add to existing position


@dataclass
class OptionsGreeksThresholds:
    """
    Thresholds for options Greeks validation (v5.24.0).

    Based on MasterPrompt options playbook:
    - Delta: 0.6-0.7 for directional ITM calls/puts
    - Theta: > -0.02/day (avoid excessive theta decay)
    - Open Interest: > 1,000 contracts (liquidity requirement)
    """

    # Delta thresholds for directional trades
    delta_min: float = 0.60  # Minimum delta for ITM directional plays
    delta_max: float = 0.70  # Maximum delta for ITM directional plays

    # Theta threshold (daily decay rate)
    theta_min: float = -0.02  # Maximum theta decay per day (more negative = worse)

    # Liquidity threshold
    min_open_interest: int = 1000  # Minimum open interest for liquidity


@dataclass
class OptionsGreeksValidation:
    """
    Result of options Greeks validation (v5.24.0).

    Simons: "Validate the Greeks before sizing the position."

    This validation ensures option recommendations meet quality thresholds
    for delta (directional exposure), theta (time decay), and liquidity.
    """

    is_valid: bool  # True if all validations pass
    validation_passed: List[str]  # List of passed validations
    validation_failed: List[str]  # List of failed validations with reasons

    # Individual Greek values
    delta: Optional[float]
    theta: Optional[float]
    open_interest: Optional[int]

    # Position adjustment factor (1.0 = full size, 0.5 = half size, 0 = reject)
    position_size_adjustment: float

    # Recommendation
    recommendation: str


# =============================================================================
# DEPRECATED: Import PositionLimits from modules.config.risk_limits instead
# =============================================================================
# This class is maintained for backward compatibility. New code should import
# from modules.config.risk_limits which is the SINGLE SOURCE OF TRUTH.
#
# To migrate, change:
#   from modules.execution.phase4_decision_engine import PositionLimits
# To:
#   from modules.config.risk_limits import PositionLimitsConfig as PositionLimits
# =============================================================================


@dataclass
class PositionLimits:
    """
    Position limits by market regime.

    DEPRECATED: Import from modules.config.risk_limits.PositionLimitsConfig instead.

    CLAUDE.md Reference: Position Sizing section
    Source of Truth: modules.config.risk_limits.PositionLimitsConfig

    | Regime      | Max Position | Description                              |
    |-------------|--------------|------------------------------------------|
    | Normal      | 10%          | Standard market conditions               |
    | Reflexivity | 20%          | Soros mode - asymmetric opportunities    |
    | Crisis      | 5%           | Defensive posture during market stress   |
    """

    normal_max: float = 0.10  # Normal regime: 10% max per position
    reflexivity_max: float = 0.20  # Reflexivity regime: 20% max (Soros mode)
    crisis_max: float = 0.05  # Crisis regime: 5% max (defensive)

    def get_max_for_regime(self, regime: str) -> float:
        """
        Get maximum position size for a given regime.

        Args:
            regime: Market regime ("normal", "reflexivity", "crisis", or legacy names)

        Returns:
            Maximum position size as fraction (0.0 to 1.0)
        """
        regime_lower = regime.lower()

        # Handle reflexivity regime
        if regime_lower in ("reflexivity", "reflation", "soros"):
            return self.reflexivity_max

        # Handle crisis regime
        if regime_lower in ("crisis", "risk-off", "risk_off", "defensive", "stagflation"):
            return self.crisis_max

        # Default to normal regime (goldilocks, deflation, bull, normal, unknown)
        return self.normal_max

    def to_dict(self) -> Dict[str, float]:
        """Return limits as dictionary for auditing."""
        return {
            "normal_max": self.normal_max,
            "reflexivity_max": self.reflexivity_max,
            "crisis_max": self.crisis_max,
        }


@dataclass
class PositionDecision:
    """Single position decision with all parameters"""
    ticker: str
    action: ActionType
    target_weight: float  # Target portfolio weight (0.0 to 1.0)
    position_size_usd: float  # Dollar amount
    shares: int  # Number of shares
    confidence: float  # Confidence score (0-100)

    # Risk management
    stop_loss_pct: float  # Stop loss percentage (e.g., -15.0)
    take_profit_pct: float  # Take profit percentage (e.g., 25.0)
    max_hold_days: int  # Maximum hold period

    # Kelly parameters
    kelly_fraction: float  # Full Kelly fraction
    fractional_kelly: float  # Conservative Kelly (typically 25% of full)
    win_probability: float  # Estimated win probability

    # Reasoning
    rationale: str
    risk_factors: List[str]

    timestamp: datetime

    # v5.25.0: Tranche entry plan for staged position building
    # Only populated for BUY actions with target_weight >= 5%
    tranche_plan: Optional["TranchePlan"] = None


@dataclass
class PortfolioDecisions:
    """Complete set of portfolio decisions"""
    decisions: List[PositionDecision]
    total_capital: float
    allocated_capital: float
    cash_reserve_pct: float
    regime: str
    timestamp: datetime
    summary: str


@dataclass
class PreDecisionValidation:
    """
    Result of pre-decision validation gates (v5.25.0).

    Dalio: "Risk constraints always enforced first."

    Validation gates check liquidity, correlation, and cash reserve
    BEFORE allowing decision generation. Any violation rejects the ticker.
    """

    can_proceed: bool  # True if all gates pass
    violations: List[str]  # List of violations if any
    ticker: str  # Ticker being validated
    regime: str  # Current market regime

    # Individual gate results
    liquidity_passed: bool = True
    liquidity_score: Optional[float] = None

    correlation_passed: bool = True
    high_correlation_count: int = 0

    cash_reserve_passed: bool = True
    required_cash_pct: float = 0.0
    current_cash_pct: float = 0.0


class Phase4DecisionEngine:
    """
    Deterministic decision engine for position sizing and execution

    Uses:
    - Kelly Criterion for position sizing (Jim Simons)
    - Regime-based stop-loss/take-profit rules (Ray Dalio)
    - Statistical confidence from quant scores
    - Pre-decision validation gates (v5.25.0)

    NO AI IN DECISION LOOP
    """

    def __init__(
        self,
        total_capital: float,
        fractional_kelly: float = 0.25,  # Use 25% of Kelly (conservative)
        min_position_pct: float = 0.02,  # Min 2% position
        max_position_pct: float = 0.10,  # Max 10% position (legacy, use position_limits)
        min_cash_reserve_pct: float = 0.10,  # Keep 10% cash
        position_limits: Optional[PositionLimits] = None,  # Regime-aware limits
        reflexivity_learner: Optional["ReflexivityLearner"] = None,  # v5.19.0
        tranche_planner: Optional["TranchePlanner"] = None,  # v5.25.0
    ):
        """
        Initialize decision engine

        Args:
            total_capital: Total portfolio value (USD)
            fractional_kelly: Fraction of Kelly to use (0.25 = 25% of Kelly)
            min_position_pct: Minimum position size as % of portfolio
            max_position_pct: Maximum position size as % of portfolio (legacy param)
            min_cash_reserve_pct: Minimum cash reserve %
            position_limits: Regime-aware position limits (overrides max_position_pct)
            reflexivity_learner: Optional ReflexivityLearner for Bayesian-updated
                reflexivity weights (v5.19.0). If provided and has sufficient
                observations, learned weights will be used for intensity calculation.
            tranche_planner: Optional TranchePlanner for staged entry (v5.25.0).
                If provided, BUY decisions for positions >= 5% will include
                a tranche plan with staged entry orders per MasterPrompt rules.

        Note:
            If position_limits is provided, max_position_pct is ignored.
            The regime is passed to generate_decisions() to determine the active limit.
        """
        self.total_capital = total_capital
        self.fractional_kelly = fractional_kelly
        self.min_position_pct = min_position_pct
        self.min_cash_reserve_pct = min_cash_reserve_pct

        # Regime-aware position limits (v5.17.0)
        # If position_limits is provided, it takes precedence over max_position_pct
        self.position_limits = position_limits or PositionLimits(
            normal_max=max_position_pct  # Use legacy param as normal max
        )

        # Keep max_position_pct for backward compatibility (defaults to normal max)
        self.max_position_pct = self.position_limits.normal_max

        # v5.19.0: Optional ReflexivityLearner for Bayesian weight updates
        self.reflexivity_learner = reflexivity_learner

        # v5.25.0: Optional TranchePlanner for staged entry
        self.tranche_planner = tranche_planner

    # =========================================================================
    # PRE-DECISION VALIDATION GATES (v5.25.0)
    # =========================================================================

    def _validate_pre_decision(
        self,
        ticker: str,
        market_regime: str,
        existing_portfolio: Dict[str, Any],
    ) -> PreDecisionValidation:
        """
        Gate checks BEFORE allowing decision generation (v5.25.0).

        SAFETY RAIL: This method enforces hard rejection gates:
        1. Liquidity Gate: Score must be >= 60
        2. Correlation Gate: Max 3 positions with >0.7 correlation
        3. Cash Reserve Gate: Must meet regime-specific cash requirement

        Dalio: "Risk constraints always enforced."
        Simons: "If it doesn't pass validation, it doesn't trade."

        Args:
            ticker: Ticker symbol to validate
            market_regime: Current market regime (bull, bear, crisis, etc.)
            existing_portfolio: Current portfolio state including:
                - positions: Dict[str, Dict] with position data
                - cash_pct: Current cash as percentage (0-1)

        Returns:
            PreDecisionValidation with can_proceed flag and violations list

        Example:
            validation = engine._validate_pre_decision(
                ticker="AAPL",
                market_regime="bear",
                existing_portfolio={
                    "positions": {"MSFT": {"correlation_with_AAPL": 0.75}},
                    "cash_pct": 0.20
                }
            )
            if not validation.can_proceed:
                logger.warning(f"Rejected: {validation.violations}")
        """
        violations: List[str] = []
        liquidity_passed = True
        liquidity_score = None
        correlation_passed = True
        high_corr_count = 0
        cash_reserve_passed = True

        # 1. Liquidity Gate
        try:
            from modules.analytics.liquidity_analyzer import LiquidityAnalyzer
            liquidity = LiquidityAnalyzer()
            liq_result = liquidity.analyze_liquidity(ticker)

            if liq_result.get('status') == 'success':
                # Extract score from nested structure
                score_data = liq_result.get('liquidity_score', {})
                liquidity_score = score_data.get('score', 0) if isinstance(score_data, dict) else 0

                if liquidity_score < 60:
                    violations.append(
                        f"Liquidity {liquidity_score:.0f} < 60 minimum"
                    )
                    liquidity_passed = False
                    logger.debug(f"{ticker}: Liquidity gate failed (score={liquidity_score:.0f})")
            else:
                # Error fetching liquidity - log but don't reject
                error_msg = liq_result.get('error', 'Unknown error')
                logger.warning(f"{ticker}: Liquidity check error: {error_msg}")
        except ImportError:
            logger.warning("LiquidityAnalyzer not available, skipping liquidity gate")
        except Exception as e:
            logger.warning(f"{ticker}: Liquidity check exception: {e}")

        # 2. Correlation Count Gate (max 3 positions >0.7 correlated)
        high_corr_count = self._count_high_correlations(ticker, existing_portfolio)
        if high_corr_count >= 3:
            violations.append(
                f"Would create {high_corr_count + 1} positions with >0.7 correlation"
            )
            correlation_passed = False
            logger.debug(f"{ticker}: Correlation gate failed (count={high_corr_count})")

        # 3. Cash Reserve Gate (regime-linked)
        required_cash = self._get_required_cash_reserve(market_regime)
        current_cash = existing_portfolio.get('cash_pct', 0.0)

        if current_cash < required_cash:
            violations.append(
                f"Cash {current_cash:.1%} < {required_cash:.1%} required for {market_regime}"
            )
            cash_reserve_passed = False
            logger.debug(
                f"{ticker}: Cash gate failed "
                f"(current={current_cash:.1%}, required={required_cash:.1%})"
            )

        can_proceed = len(violations) == 0

        return PreDecisionValidation(
            can_proceed=can_proceed,
            violations=violations,
            ticker=ticker,
            regime=market_regime,
            liquidity_passed=liquidity_passed,
            liquidity_score=liquidity_score,
            correlation_passed=correlation_passed,
            high_correlation_count=high_corr_count,
            cash_reserve_passed=cash_reserve_passed,
            required_cash_pct=required_cash,
            current_cash_pct=current_cash,
        )

    def _get_required_cash_reserve(self, regime: str) -> float:
        """
        Get dynamic cash requirement by regime (v5.25.0).

        Dalio: "Cash requirements scale with uncertainty."

        Args:
            regime: Current market regime

        Returns:
            Required cash reserve as fraction (0.0 to 1.0)

        Regime Cash Requirements (from CLAUDE.md Drawdown Protocol):
        - bull/normal: 10% - standard conditions
        - uncertain: 25% - elevated caution
        - bear: 25% - defensive posture
        - crisis: 50% - capital preservation mode
        - reflexivity: 15% - Soros mode needs powder for asymmetric bets
        """
        REGIME_CASH: Dict[str, float] = {
            "bull": 0.10,
            "normal": 0.10,
            "goldilocks": 0.10,
            "uncertain": 0.25,
            "bear": 0.25,
            "crisis": 0.50,
            "risk-off": 0.50,
            "defensive": 0.50,
            "reflexivity": 0.15,
            "reflation": 0.15,
            "stagflation": 0.35,  # Between uncertain and crisis
            "deflation": 0.20,
        }
        return REGIME_CASH.get(regime.lower(), 0.10)

    def _count_high_correlations(
        self,
        ticker: str,
        portfolio: Dict[str, Any],
    ) -> int:
        """
        Count existing positions with >0.7 correlation to ticker (v5.25.0).

        Dalio: "Diversification is the only free lunch."

        Args:
            ticker: Ticker to check correlations against
            portfolio: Portfolio dict with 'positions' key containing position data.
                      Each position may have 'correlation_with_<ticker>' keys.

        Returns:
            Count of positions with absolute correlation > 0.7

        Example:
            count = engine._count_high_correlations("AAPL", {
                "positions": {
                    "MSFT": {"correlation_with_AAPL": 0.75},  # Counts
                    "XOM": {"correlation_with_AAPL": 0.30},   # Doesn't count
                    "GOOGL": {"correlation_with_AAPL": -0.80} # Counts (abs)
                }
            })
            # Returns 2
        """
        count = 0
        positions = portfolio.get('positions', {})

        for pos_ticker, pos_data in positions.items():
            if pos_ticker == ticker:
                continue  # Don't count self-correlation

            # Look for correlation with the target ticker
            corr_key = f'correlation_with_{ticker}'
            corr = pos_data.get(corr_key, 0.0)

            if abs(corr) > 0.7:
                count += 1

        return count

    def generate_decisions(
        self,
        screened_tickers: List[str],
        ticker_scores: Dict[str, Dict[str, Any]],
        ticker_prices: Dict[str, float],
        current_holdings: Optional[Dict[str, float]] = None,  # {ticker: shares}
        regime: str = 'goldilocks',
        existing_portfolio: Optional[Dict[str, Any]] = None,  # v5.25.0: Portfolio state for validation
    ) -> PortfolioDecisions:
        """
        Generate deterministic position sizing and execution decisions

        Args:
            screened_tickers: List of tickers from Phase 2 screener
            ticker_scores: Composite scores and metrics from Phase 2
            ticker_prices: Current prices {ticker: price}
            current_holdings: Existing positions {ticker: shares}
            regime: Current macro regime
            existing_portfolio: (v5.25.0) Full portfolio state for validation gates.
                               Should include 'positions' dict with correlation data
                               and 'cash_pct' for cash reserve checks.

        Returns:
            PortfolioDecisions with all position decisions
        """
        logger.info("=" * 80)
        logger.info("PHASE 4: DETERMINISTIC DECISION ENGINE")
        logger.info("=" * 80)
        logger.info(f"Total capital: ${self.total_capital:,.2f}")
        logger.info(f"Screened tickers: {len(screened_tickers)}")

        # Get regime-aware position limit (v5.17.0)
        regime_max_position = self.position_limits.get_max_for_regime(regime)
        logger.info(f"Regime: {regime} -> Max position: {regime_max_position:.1%}")

        current_holdings = current_holdings or {}
        decisions = []
        rejected_tickers: List[Tuple[str, List[str]]] = []  # v5.25.0: Track rejections

        # Calculate available capital (after cash reserve)
        max_allocatable = self.total_capital * (1 - self.min_cash_reserve_pct)

        # v5.25.0: Build portfolio state for validation if not provided
        if existing_portfolio is None:
            # Estimate current cash percentage based on holdings
            total_holdings_value = sum(
                current_holdings.get(t, 0) * ticker_prices.get(t, 0)
                for t in current_holdings
            )
            current_cash_pct = max(0, 1 - (total_holdings_value / self.total_capital)) if self.total_capital > 0 else 1.0
            existing_portfolio = {
                'positions': {
                    t: ticker_scores.get(t, {}) for t in current_holdings
                },
                'cash_pct': current_cash_pct,
            }

        # STEP 1: Calculate Kelly position sizes (regime-aware)
        logger.info("\nSTEP 1: Calculating Kelly Criterion position sizes...")
        position_weights = self._calculate_kelly_weights(
            screened_tickers,
            ticker_scores,
            max_allocatable,
            regime=regime  # Pass regime for regime-aware limits
        )

        # STEP 2: Generate decisions for each ticker
        logger.info("\nSTEP 2: Generating position decisions...")
        total_allocated = 0.0

        for ticker in screened_tickers:
            if ticker not in ticker_prices:
                logger.warning(f"No price available for {ticker}, skipping")
                continue

            # v5.25.0: PRE-DECISION VALIDATION GATES
            validation = self._validate_pre_decision(
                ticker=ticker,
                market_regime=regime,
                existing_portfolio=existing_portfolio,
            )

            if not validation.can_proceed:
                logger.warning(f"❌ {ticker} REJECTED: {validation.violations}")
                rejected_tickers.append((ticker, validation.violations))
                continue  # Skip this ticker - failed validation gates

            score_data = ticker_scores.get(ticker, {})
            target_weight = position_weights.get(ticker, 0.0)
            current_price = ticker_prices[ticker]

            # Skip if weight too small
            if target_weight < self.min_position_pct:
                logger.debug(f"{ticker}: Weight {target_weight:.2%} below minimum, skipping")
                continue

            # Generate decision
            decision = self._generate_position_decision(
                ticker=ticker,
                target_weight=target_weight,
                current_price=current_price,
                score_data=score_data,
                current_shares=int(current_holdings.get(ticker, 0)),
                regime=regime
            )

            if decision:
                decisions.append(decision)
                total_allocated += decision.position_size_usd

        # STEP 3: Normalize if over-allocated
        if total_allocated > max_allocatable:
            logger.warning(f"Over-allocated: ${total_allocated:,.2f} > ${max_allocatable:,.2f}")
            logger.info("Normalizing position sizes...")
            scale_factor = max_allocatable / total_allocated

            for decision in decisions:
                decision.target_weight *= scale_factor
                decision.position_size_usd *= scale_factor
                decision.shares = int(decision.position_size_usd / ticker_prices[decision.ticker])

            total_allocated = max_allocatable

        # Calculate cash reserve
        cash_reserve_pct = (self.total_capital - total_allocated) / self.total_capital

        # Generate summary
        summary = self._generate_summary(decisions, total_allocated, cash_reserve_pct, regime)

        portfolio_decisions = PortfolioDecisions(
            decisions=decisions,
            total_capital=self.total_capital,
            allocated_capital=total_allocated,
            cash_reserve_pct=cash_reserve_pct,
            regime=regime,
            timestamp=datetime.now(),
            summary=summary
        )

        logger.info("\n" + "=" * 80)
        logger.info("DECISION GENERATION COMPLETE")
        logger.info("=" * 80)
        logger.info(summary)

        # v5.25.0: Log rejected tickers for audit trail
        if rejected_tickers:
            logger.info("\n" + "-" * 40)
            logger.info(f"VALIDATION GATE REJECTIONS: {len(rejected_tickers)} tickers")
            logger.info("-" * 40)
            for ticker, violations in rejected_tickers:
                logger.info(f"  ❌ {ticker}: {', '.join(violations)}")

        return portfolio_decisions

    def calculate_reflexivity_intensity(
        self,
        component_scores: Dict[str, float],
    ) -> Tuple[float, Dict[str, float]]:
        """
        Calculate reflexivity intensity using learned weights if available.

        This method applies the reflexivity formula from CLAUDE.md:
            Intensity = Sentiment×w1 + Policy×w2 + Crowding×w3 + Narrative×w4

        If a ReflexivityLearner is attached and has sufficient observations
        (≥10 trades per component), it uses Bayesian-updated weights.
        Otherwise, it falls back to the static DEFAULT_REFLEXIVITY_WEIGHTS.

        Args:
            component_scores: Dict mapping component name to score (0-1)
                Expected keys: sentiment, policy, crowding, narrative

        Returns:
            Tuple of (intensity, weights_used):
                - intensity: Float 0-1 representing reflexivity intensity
                - weights_used: Dict of weights that were applied

        Example:
            intensity, weights = engine.calculate_reflexivity_intensity({
                "sentiment": 0.8,
                "policy": 0.6,
                "crowding": 0.5,
                "narrative": 0.4
            })
            # intensity = 0.8*0.30 + 0.6*0.30 + 0.5*0.20 + 0.4*0.20 = 0.60
        """
        # Determine which weights to use
        if self.reflexivity_learner:
            # Check if learner has sufficient observations
            total_predictions = sum(
                p.total_predictions
                for p in self.reflexivity_learner.performance.values()
            )
            min_required = self.reflexivity_learner.min_observations * len(
                self.reflexivity_learner.performance
            )

            if total_predictions >= min_required:
                weights = self.reflexivity_learner.get_updated_weights()
                logger.info("🧠 Using learned reflexivity weights from past trades")
            else:
                weights = DEFAULT_REFLEXIVITY_WEIGHTS.copy()
                logger.debug(
                    f"Insufficient observations ({total_predictions}/{min_required}), "
                    "using default weights"
                )
        else:
            weights = DEFAULT_REFLEXIVITY_WEIGHTS.copy()

        # Calculate intensity as weighted sum
        intensity = sum(
            component_scores.get(component, 0.0) * weight
            for component, weight in weights.items()
        )

        # Clamp to [0, 1]
        intensity = max(0.0, min(1.0, intensity))

        return intensity, weights

    def _calculate_kelly_weights(
        self,
        tickers: List[str],
        ticker_scores: Dict[str, Dict[str, Any]],
        max_allocatable: float,
        regime: str = "normal",
    ) -> Dict[str, float]:
        """
        Calculate position weights using Kelly Criterion with regime-aware limits.

        Kelly formula: f = (bp - q) / b
        where:
        - f = fraction of capital to bet
        - b = odds (avg_win / avg_loss)
        - p = probability of win
        - q = probability of loss (1 - p)

        We estimate:
        - p from composite score percentile
        - b from Sharpe ratio and historical returns

        Position Limits by Regime (v5.17.0):
        - Normal: 10% max (standard market conditions)
        - Reflexivity: 20% max (Soros mode - asymmetric opportunities)
        - Crisis: 5% max (defensive posture)

        Args:
            tickers: List of tickers
            ticker_scores: Score data for each ticker
            max_allocatable: Maximum allocatable capital
            regime: Current market regime for position limit selection

        Returns:
            Dict {ticker: weight} where weight is fraction of total capital
        """
        kelly_fractions = {}

        # Get regime-specific max position (v5.17.0)
        max_position_for_regime = self.position_limits.get_max_for_regime(regime)

        for ticker in tickers:
            score_data = ticker_scores.get(ticker, {})

            # Estimate win probability from percentile (higher percentile = higher win prob)
            percentile = score_data.get('percentile', 50) / 100.0  # 0.0 to 1.0

            # Transform percentile to win probability (sigmoid-like)
            # Percentile 90 -> ~70% win prob
            # Percentile 50 -> ~50% win prob
            # Percentile 10 -> ~30% win prob
            win_prob = 0.3 + (percentile * 0.4)  # Range: 0.3 to 0.7

            # Estimate odds ratio from composite score
            # Higher composite score = better risk/reward
            composite = score_data.get('composite_score', 50)

            # Normalize composite to odds ratio
            # Score 80+ -> 2.0 odds (2:1 reward:risk)
            # Score 60 -> 1.5 odds
            # Score 40 -> 1.0 odds
            if composite >= 80:
                odds_ratio = 2.0
            elif composite >= 70:
                odds_ratio = 1.8
            elif composite >= 60:
                odds_ratio = 1.5
            else:
                odds_ratio = 1.2

            # Calculate Kelly fraction
            # f = (bp - q) / b
            b = odds_ratio
            p = win_prob
            q = 1 - p

            kelly_f = (b * p - q) / b

            # Apply fractional Kelly (safety factor)
            conservative_kelly = kelly_f * self.fractional_kelly

            # Cap at regime-specific max position (v5.17.0)
            final_weight = max(0, min(conservative_kelly, max_position_for_regime))

            kelly_fractions[ticker] = final_weight

            logger.debug(f"{ticker}: p={p:.2f}, b={b:.2f}, Kelly={kelly_f:.3f}, "
                        f"Fractional={conservative_kelly:.3f}, Final={final_weight:.3f} "
                        f"(max={max_position_for_regime:.1%})")

        return kelly_fractions

    def apply_quarter_kelly_clamp(
        self,
        portfolio_decisions: List[Dict[str, Any]],
        regime: str = "normal",
        kelly_fraction: float = 0.25,
        probability_shrink: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Deterministic sizing post-processor (v6.12.0, Phase-4 Decision Contract).

        Caps every BUY's AI-requested position size at ``kelly_fraction`` ×
        Kelly, where Kelly is computed from the AI's OWN stated
        ``win_probability`` and ``payoff_ratio`` (decision-contract fields) —
        not from synthesized composite scores. A non-positive Kelly edge
        rewrites the decision to HOLD. The regime position limit is applied
        inside the Kelly calculation; RiskEnforcer hard limits still run after
        this (the clamp only ever shrinks, never grows, a size).

        ``probability_shrink`` (v6.16.0) is the share of the stated edge that is believed: the Kelly input
        is ``0.5 + shrink * (stated - 0.5)``. A language model's stated probabilities are uncalibrated until
        the forecast panel shows otherwise, and Kelly is brutally sensitive to an overstated edge, so the
        orchestrator passes ``AIScoreboard.calibration_state()["shrink"]`` (0.5 until Brier skill > 0 on
        >= 100 graded forecasts). The default 1.0 keeps the stated value, for callers without evidence.

        Must run AFTER portfolio optimization, which overwrites
        ``position_size_pct`` — clamping earlier would be undone.

        Mutates the decision dicts in place (orchestrator convention) and
        returns a summary for reports/audit.
        """
        # Lazy import (file idiom, avoids analytics<->execution import cycles).
        # ScreeningEngine.calculate_kelly_position_size is the canonical
        # stated-probability Kelly; avg_loss=1.0 makes avg_win the odds ratio.
        from modules.analytics.screening_engine import ScreeningEngine

        engine = ScreeningEngine()
        regime_cap = self.position_limits.get_max_for_regime(regime)
        shrink = min(1.0, max(0.0, float(probability_shrink)))
        summary: Dict[str, Any] = {
            "kelly_fraction": kelly_fraction,
            "probability_shrink": shrink,
            "regime_cap_pct": regime_cap * 100.0,
            "clamped": [],
            "negative_edge_holds": [],
            "missing_inputs": [],
        }

        for decision in portfolio_decisions:
            if str(decision.get("action", "")).upper() != "BUY":
                continue
            ticker = decision.get("ticker", decision.get("symbol", "?"))

            win_prob = decision.get("win_probability")
            payoff = decision.get("payoff_ratio")
            if win_prob is None or payoff is None:
                # Contract v1.0 requires both for BUY; legacy decision dicts
                # (tests, strategy-engine baselines) pass through unclamped
                # but are flagged so the gap is visible, never silent.
                summary["missing_inputs"].append(ticker)
                logger.warning(
                    f"     ⚠️ {ticker}: no stated win_probability/payoff_ratio — "
                    "quarter-Kelly clamp skipped"
                )
                continue

            effective_prob = 0.5 + shrink * (float(win_prob) - 0.5)
            decision["win_probability_stated"] = float(win_prob)
            decision["win_probability_effective"] = round(effective_prob, 6)
            kelly = engine.calculate_kelly_position_size(
                win_prob=effective_prob,
                avg_win=float(payoff),
                avg_loss=1.0,
                max_position=regime_cap,
                kelly_fraction=kelly_fraction,
            )
            ai_size_pct = float(
                decision.get("position_size_pct", decision.get("position_size", 0.0)) or 0.0
            )
            if 0 < ai_size_pct <= 1 and "position_size_pct" not in decision:
                ai_size_pct *= 100.0  # fraction-only legacy dict
            clamp_pct = kelly["recommended_size"] * 100.0

            decision["kelly_full"] = kelly["optimal_fraction"]
            decision["kelly_quarter_pct"] = round(clamp_pct, 4)
            decision["size_pct_before_clamp"] = ai_size_pct

            if kelly["optimal_fraction"] <= 0:
                decision["action"] = "HOLD"
                decision["position_size_pct"] = 0.0
                decision["position_size"] = 0.0
                decision["clamp_reason"] = (
                    "negative Kelly edge from stated win_probability/payoff_ratio"
                    + (f" (after shrinking the stated probability to {effective_prob:.3f})" if shrink < 1.0 else "")
                )
                summary["negative_edge_holds"].append(ticker)
                logger.warning(
                    f"     🛑 {ticker}: stated edge is non-positive "
                    f"(p={win_prob}, effective p={effective_prob:.3f}, b={payoff}) — BUY rewritten to HOLD"
                )
            elif ai_size_pct > clamp_pct:
                decision["position_size_pct"] = round(clamp_pct, 4)
                decision["position_size"] = round(kelly["recommended_size"], 6)
                decision["clamp_reason"] = "quarter_kelly_cap"
                summary["clamped"].append(
                    {"ticker": ticker, "from_pct": ai_size_pct, "to_pct": round(clamp_pct, 4)}
                )
                logger.info(
                    f"     ✂️ {ticker}: size clamped {ai_size_pct:.2f}% → {clamp_pct:.2f}% "
                    f"(quarter-Kelly, p={win_prob}→{effective_prob:.3f}, b={payoff})"
                )

        return summary

    def _generate_position_decision(
        self,
        ticker: str,
        target_weight: float,
        current_price: float,
        score_data: Dict[str, Any],
        current_shares: int,
        regime: str
    ) -> Optional[PositionDecision]:
        """
        Generate detailed position decision for a single ticker

        Args:
            ticker: Ticker symbol
            target_weight: Target portfolio weight (0.0 to 1.0)
            current_price: Current price per share
            score_data: Score data from Phase 2
            current_shares: Current shares owned
            regime: Current macro regime

        Returns:
            PositionDecision or None if no action
        """
        # Calculate position size
        position_size_usd = target_weight * self.total_capital

        # Handle zero or invalid price
        if current_price <= 0:
            logger.warning(f"{ticker}: Invalid price {current_price}, skipping")
            return None

        target_shares = int(position_size_usd / current_price)

        # Determine action
        if current_shares == 0:
            action = ActionType.BUY
        elif target_shares > current_shares * 1.1:  # 10% increase threshold
            action = ActionType.ADD
        elif target_shares < current_shares * 0.9:  # 10% decrease threshold
            action = ActionType.REDUCE
        elif abs(target_shares - current_shares) / max(current_shares, 1) < 0.05:
            action = ActionType.HOLD
            return None  # No action needed
        else:
            action = ActionType.HOLD
            return None

        # Determine stop-loss and take-profit based on regime and volatility
        stop_loss_pct, take_profit_pct, max_hold_days = self._calculate_risk_parameters(
            ticker=ticker,
            score_data=score_data,
            regime=regime
        )

        # Calculate confidence from composite score
        composite = score_data.get('composite_score', 50)
        confidence = min(100, max(0, composite))  # Already 0-100

        # Extract Kelly parameters
        percentile = score_data.get('percentile', 50) / 100.0
        win_prob = 0.3 + (percentile * 0.4)

        # Calculate odds from composite
        if composite >= 80:
            odds_ratio = 2.0
        elif composite >= 70:
            odds_ratio = 1.8
        elif composite >= 60:
            odds_ratio = 1.5
        else:
            odds_ratio = 1.2

        kelly_full = ((odds_ratio * win_prob) - (1 - win_prob)) / odds_ratio
        kelly_fractional = kelly_full * self.fractional_kelly

        # Generate rationale
        rationale = self._generate_rationale(ticker, action, score_data, regime)

        # Identify risk factors
        risk_factors = self._identify_risk_factors(ticker, score_data)

        # v5.25.0: Generate tranche plan for BUY actions
        tranche_plan = None
        if action == ActionType.BUY and self.tranche_planner is not None:
            tranche_plan = self._generate_tranche_plan(
                ticker=ticker,
                target_weight=target_weight,
                current_price=current_price,
                score_data=score_data,
                regime=regime,
            )

        decision = PositionDecision(
            ticker=ticker,
            action=action,
            target_weight=target_weight,
            position_size_usd=position_size_usd,
            shares=target_shares,
            confidence=confidence,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            max_hold_days=max_hold_days,
            kelly_fraction=kelly_full,
            fractional_kelly=kelly_fractional,
            win_probability=win_prob,
            rationale=rationale,
            risk_factors=risk_factors,
            timestamp=datetime.now(),
            tranche_plan=tranche_plan,
        )

        return decision

    def _calculate_risk_parameters(
        self,
        ticker: str,
        score_data: Dict[str, Any],
        regime: str
    ) -> Tuple[float, float, int]:
        """
        Calculate stop-loss, take-profit, and max hold period

        Regime-based rules (Dalio):
        - Goldilocks: Tighter stops (market efficient), longer holds
        - Reflation: Wider stops (volatile), medium holds
        - Stagflation: Tight stops (defensive), shorter holds
        - Deflation: Medium stops (flight to quality), longer holds

        Args:
            ticker: Ticker symbol
            score_data: Score data
            regime: Current regime

        Returns:
            Tuple of (stop_loss_pct, take_profit_pct, max_hold_days)
        """
        # Base parameters by regime
        regime_params = {
            'goldilocks': (-12, 20, 90),   # Tight stop, moderate profit, long hold
            'reflation': (-18, 30, 60),     # Wide stop, high profit, medium hold
            'stagflation': (-10, 15, 45),   # Very tight stop, modest profit, short hold
            'deflation': (-15, 18, 75)      # Medium stop, modest profit, medium-long hold
        }

        base_stop, base_profit, base_days = regime_params.get(
            regime.lower(),
            (-15, 20, 60)  # Default
        )

        # Adjust based on quality score (higher quality = wider stops)
        quality_score = score_data.get('quality_score', 50)
        if quality_score > 80:
            stop_loss_pct = base_stop * 1.2  # 20% wider for high quality
        elif quality_score < 40:
            stop_loss_pct = base_stop * 0.8  # 20% tighter for low quality
        else:
            stop_loss_pct = base_stop

        # Adjust take-profit based on momentum score
        momentum_score = score_data.get('momentum_score', 50)
        if momentum_score > 80:
            take_profit_pct = base_profit * 1.3  # Higher profit target
        elif momentum_score < 40:
            take_profit_pct = base_profit * 0.8  # Lower profit target
        else:
            take_profit_pct = base_profit

        # Adjust hold period based on volatility (if available)
        # For now, use base
        max_hold_days = base_days

        return (stop_loss_pct, take_profit_pct, max_hold_days)

    def _generate_rationale(
        self,
        ticker: str,
        action: ActionType,
        score_data: Dict[str, Any],
        regime: str
    ) -> str:
        """Generate human-readable rationale for decision"""
        composite = score_data.get('composite_score', 0)
        value = score_data.get('value_score', 0)
        momentum = score_data.get('momentum_score', 0)
        quality = score_data.get('quality_score', 0)
        percentile = score_data.get('percentile', 0)

        rationale = f"{action.value.upper()} {ticker}: Composite score {composite:.1f} "
        rationale += f"(percentile {percentile:.0f}). "

        # Highlight strongest factor
        scores = {
            'Value': value,
            'Momentum': momentum,
            'Quality': quality
        }
        strongest = max(scores.items(), key=lambda x: x[1])
        rationale += f"Strongest factor: {strongest[0]} ({strongest[1]:.1f}). "

        # Regime fit
        rationale += f"Regime: {regime}. "

        return rationale

    def _identify_risk_factors(
        self,
        ticker: str,
        score_data: Dict[str, Any]
    ) -> List[str]:
        """Identify key risk factors for position"""
        risks = []

        # Check liquidity
        liquidity_score = score_data.get('liquidity_score')
        if liquidity_score and liquidity_score < 40:
            risks.append(f"Low liquidity (score: {liquidity_score:.0f})")

        # Check volatility (if available in factor data)
        # For now, check composite score
        composite = score_data.get('composite_score', 50)
        if composite < 65:
            risks.append("Below-average composite score")

        # Check value metrics
        value_score = score_data.get('value_score', 50)
        if value_score < 40:
            risks.append("Expensive valuation")

        # Check quality
        quality_score = score_data.get('quality_score', 50)
        if quality_score < 40:
            risks.append("Weak financial quality")

        if not risks:
            risks.append("No major red flags identified")

        return risks

    def _generate_tranche_plan(
        self,
        ticker: str,
        target_weight: float,
        current_price: float,
        score_data: Dict[str, Any],
        regime: str,
    ) -> Optional["TranchePlan"]:
        """
        Generate tranche entry plan for BUY decisions (v5.25.0).

        Creates a staged entry plan per MasterPrompt_v3.3_PortfolioRules.md:
        - <5% position: Single entry (market order)
        - 5-10%: 2 tranches (50%/50%)
        - >10%: 3 tranches (40%/30%/30%)

        Args:
            ticker: Stock symbol
            target_weight: Target position weight (0.0 to 1.0)
            current_price: Current stock price
            score_data: Ticker score data (may contain support/resistance levels)
            regime: Current market regime

        Returns:
            TranchePlan if tranche_planner is available, None otherwise
        """
        if self.tranche_planner is None:
            return None

        # Extract support/resistance from score_data if available
        support_level = score_data.get('support_level')
        resistance_level = score_data.get('resistance_level')

        # If no support in score_data, estimate from stop loss
        # Typical stop loss is 10-15% below entry
        if support_level is None:
            support_level = current_price * 0.92  # Estimate 8% below

        # Generate base tranche plan
        plan = self.tranche_planner.plan_entry(
            ticker=ticker,
            total_size_pct=target_weight,
            current_price=current_price,
            support_level=support_level,
            resistance_level=resistance_level,
        )

        # Adjust for regime if needed
        reflexivity_intensity = score_data.get('reflexivity_intensity', 0.0)
        adjusted_plan = self.tranche_planner.adjust_for_regime(
            plan=plan,
            regime=regime,
            reflexivity_intensity=reflexivity_intensity,
        )

        logger.info(
            f"{ticker}: Tranche plan generated - "
            f"{adjusted_plan.total_tranches} tranches, "
            f"immediate: {adjusted_plan.immediate_size_pct*100:.1f}%"
        )

        return adjusted_plan

    def _validate_options_greeks(
        self,
        delta: Optional[float],
        theta: Optional[float],
        open_interest: Optional[int],
        thresholds: Optional[OptionsGreeksThresholds] = None,
    ) -> OptionsGreeksValidation:
        """
        Validate options Greeks for any option recommendation (v5.24.0).

        SAFETY RAIL: This method ensures option recommendations meet quality
        thresholds before position sizing. Following MasterPrompt guidelines:
        - Delta: 0.6-0.7 for directional (ITM calls/puts)
        - Theta: > -0.02/day (avoid excessive theta decay)
        - Open Interest: > 1,000 (liquidity requirement)

        If validation fails:
        - Single failure: Downgrade position size by 50%
        - Multiple failures: Reject the position entirely

        Args:
            delta: Option delta (0 to 1 for calls, -1 to 0 for puts)
            theta: Daily theta decay (typically negative)
            open_interest: Number of open contracts
            thresholds: Optional custom thresholds (defaults to standard)

        Returns:
            OptionsGreeksValidation with validation results and size adjustment

        Example:
            validation = engine._validate_options_greeks(
                delta=0.65,
                theta=-0.015,
                open_interest=2500
            )
            if validation.is_valid:
                position_size = base_size
            else:
                position_size = base_size * validation.position_size_adjustment
        """
        if thresholds is None:
            thresholds = OptionsGreeksThresholds()

        validation_passed: List[str] = []
        validation_failed: List[str] = []

        # Validate Delta (for directional trades)
        if delta is not None:
            # Use absolute value for puts (which have negative delta)
            abs_delta = abs(delta)
            if thresholds.delta_min <= abs_delta <= thresholds.delta_max:
                validation_passed.append(
                    f"Delta ({abs_delta:.2f}) within target range "
                    f"[{thresholds.delta_min:.2f}, {thresholds.delta_max:.2f}]"
                )
            else:
                if abs_delta < thresholds.delta_min:
                    validation_failed.append(
                        f"Delta ({abs_delta:.2f}) below minimum {thresholds.delta_min:.2f} - "
                        "option too far OTM for directional play"
                    )
                else:
                    validation_failed.append(
                        f"Delta ({abs_delta:.2f}) above maximum {thresholds.delta_max:.2f} - "
                        "option too deep ITM, consider stock instead"
                    )
        else:
            validation_failed.append("Delta not provided - cannot validate directional exposure")

        # Validate Theta (time decay)
        if theta is not None:
            if theta >= thresholds.theta_min:
                validation_passed.append(
                    f"Theta ({theta:.4f}/day) acceptable - "
                    f"above minimum {thresholds.theta_min:.4f}"
                )
            else:
                validation_failed.append(
                    f"Theta ({theta:.4f}/day) excessive decay - "
                    f"below minimum {thresholds.theta_min:.4f}. "
                    "Consider longer-dated options."
                )
        else:
            validation_failed.append("Theta not provided - cannot validate time decay risk")

        # Validate Open Interest (liquidity)
        if open_interest is not None:
            if open_interest >= thresholds.min_open_interest:
                validation_passed.append(
                    f"Open Interest ({open_interest:,}) adequate liquidity - "
                    f"above minimum {thresholds.min_open_interest:,}"
                )
            else:
                validation_failed.append(
                    f"Open Interest ({open_interest:,}) insufficient liquidity - "
                    f"below minimum {thresholds.min_open_interest:,}. "
                    "Risk of slippage and wide bid-ask spreads."
                )
        else:
            validation_failed.append("Open Interest not provided - cannot validate liquidity")

        # Calculate position size adjustment based on failures
        num_failures = len(validation_failed)

        if num_failures == 0:
            position_size_adjustment = 1.0
            recommendation = "APPROVED: All Greeks validations passed. Full position size allowed."
            is_valid = True
        elif num_failures == 1:
            position_size_adjustment = 0.5
            recommendation = (
                "CAUTION: One validation failed. "
                "Position size reduced by 50%. Review before execution."
            )
            is_valid = False
        elif num_failures == 2:
            position_size_adjustment = 0.25
            recommendation = (
                "WARNING: Two validations failed. "
                "Position size reduced by 75%. High risk - consider alternative."
            )
            is_valid = False
        else:
            position_size_adjustment = 0.0
            recommendation = (
                "REJECTED: Multiple validations failed. "
                "Do not execute this option position."
            )
            is_valid = False

        logger.info(f"Options Greeks validation: {len(validation_passed)} passed, {num_failures} failed")
        if num_failures > 0:
            logger.warning(f"Failed validations: {validation_failed}")

        return OptionsGreeksValidation(
            is_valid=is_valid,
            validation_passed=validation_passed,
            validation_failed=validation_failed,
            delta=delta,
            theta=theta,
            open_interest=open_interest,
            position_size_adjustment=position_size_adjustment,
            recommendation=recommendation,
        )

    def _generate_summary(
        self,
        decisions: List[PositionDecision],
        total_allocated: float,
        cash_reserve_pct: float,
        regime: str
    ) -> str:
        """Generate summary of all decisions"""
        lines = []
        lines.append(f"\nDECISION ENGINE SUMMARY - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Regime: {regime.upper()}")
        lines.append(f"Total Capital: ${self.total_capital:,.2f}")
        lines.append(f"Allocated: ${total_allocated:,.2f} ({total_allocated/self.total_capital*100:.1f}%)")
        lines.append(f"Cash Reserve: {cash_reserve_pct*100:.1f}%")
        lines.append(f"\n{len(decisions)} POSITION DECISIONS:")
        lines.append("-" * 120)
        lines.append(f"{'Ticker':<8} {'Action':<8} {'Weight':<8} {'Shares':<8} {'Value':<12} "
                    f"{'Confidence':<11} {'Stop Loss':<11} {'Take Profit':<12} {'Max Days':<9}")
        lines.append("-" * 120)

        for decision in decisions:
            lines.append(
                f"{decision.ticker:<8} {decision.action.value:<8} {decision.target_weight*100:>6.2f}% "
                f"{decision.shares:>7d} ${decision.position_size_usd:>10,.0f} "
                f"{decision.confidence:>10.1f} {decision.stop_loss_pct:>10.1f}% "
                f"{decision.take_profit_pct:>11.1f}% {decision.max_hold_days:>8d}"
            )

        lines.append("-" * 120)

        # Risk summary
        lines.append("\nRISK SUMMARY:")
        all_risks: Dict[str, int] = {}
        for decision in decisions:
            for risk in decision.risk_factors:
                all_risks[risk] = all_risks.get(risk, 0) + 1

        for risk, count in sorted(all_risks.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {count}x {risk}")

        # v5.25.0: Tranche entry summary
        tranche_decisions = [d for d in decisions if d.tranche_plan is not None]
        if tranche_decisions:
            lines.append("\n" + "=" * 80)
            lines.append("TRANCHE ENTRY PLANS (v5.25.0)")
            lines.append("=" * 80)
            for decision in tranche_decisions:
                plan = decision.tranche_plan
                if plan is None:
                    continue  # Type guard for mypy
                lines.append(f"\n{decision.ticker}: {plan.total_tranches} tranches for {plan.total_size_pct*100:.1f}% position")
                lines.append(f"  Current Price: ${plan.current_price:.2f}")
                if plan.support_level:
                    lines.append(f"  Support Level: ${plan.support_level:.2f}")
                for tranche in plan.tranches:
                    price_str = f"@ ${tranche.limit_price:.2f}" if tranche.limit_price else "(market)"
                    alloc_pct = tranche.size_pct * plan.total_size_pct * 100
                    lines.append(
                        f"  T{tranche.tranche_num}: {tranche.size_pct*100:.0f}% "
                        f"({alloc_pct:.1f}% of portfolio) - {tranche.order_type.value} {price_str}"
                    )
                    lines.append(f"      Trigger: {tranche.trigger_description}")

        return "\n".join(lines)


# Example usage
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # Mock data
    screened = ['AAPL', 'MSFT', 'GOOGL']
    scores = {
        'AAPL': {'composite_score': 75, 'value_score': 60, 'momentum_score': 80,
                'quality_score': 85, 'percentile': 85, 'liquidity_score': 90},
        'MSFT': {'composite_score': 78, 'value_score': 65, 'momentum_score': 82,
                'quality_score': 88, 'percentile': 88, 'liquidity_score': 95},
        'GOOGL': {'composite_score': 72, 'value_score': 70, 'momentum_score': 75,
                 'quality_score': 80, 'percentile': 80, 'liquidity_score': 85}
    }
    prices = {'AAPL': 180.0, 'MSFT': 380.0, 'GOOGL': 140.0}

    engine = Phase4DecisionEngine(total_capital=100000.0)
    decisions = engine.generate_decisions(
        screened_tickers=screened,
        ticker_scores=scores,
        ticker_prices=prices,
        regime='goldilocks'
    )

    print(decisions.summary)
