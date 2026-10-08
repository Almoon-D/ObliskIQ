"""
Configuration Management for ObliskIQ
Centralizes all settings, API keys, and constants
"""

import os
from dataclasses import dataclass, field
from typing import List, Tuple
from pathlib import Path

# ============================================================================
# AI MODEL DEFAULTS — SINGLE SOURCE OF TRUTH
# ============================================================================
# Rotate the default Gemini model HERE (and nowhere else) when Google ships a
# new FREE-tier model. Everything that needs the default — the provider
# registry (modules/config/ai_providers.py) and the test suite — reads it from
# this constant, so there is exactly one place to change.
# The GEMINI_MODEL environment variable (or [ai] gemini_model in the operator
# panel) still overrides this at runtime.
#
# History: this was "gemini-3.5-pro-thinking", a model ID that does not exist
# (Google's 3.5 generation is Flash-only) and, even as the real Pro model
# (gemini-3.1-pro-preview), is paid-only since Google removed Pro from the free
# tier in April 2026 — so the first real run would have failed with a 404.
# gemini-3.8-flash is stable, free-tier and supports thinking_level.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


@dataclass
class APIConfig:
    """API keys and rate limits"""

    # Alpha Vantage
    alphavantage_keys: List[str] = field(
        default_factory=lambda: [
            k
            for k in [
                os.getenv("ALPHAVANTAGE_API_KEY"),
                os.getenv("ALPHAVANTAGE_API_KEY_2"),
                os.getenv("ALPHAVANTAGE_API_KEY_3"),
                os.getenv("ALPHAVANTAGE_API_KEY_4"),
                os.getenv("ALPHAVANTAGE_API_KEY_5"),
            ]
            if k
        ]
    )
    alphavantage_rate_limit_seconds: int = 12  # 5 calls/min
    rate_limit_delay_sec: int = 50  # Wait time between tickers with multiple AV calls

    # Finnhub
    finnhub_keys: List[str] = field(
        default_factory=lambda: [
            k
            for k in [
                os.getenv("FINNHUB_API_KEY"),
                os.getenv("FINNHUB_API_KEY_2"),
                os.getenv("FINNHUB_API_KEY_3"),
            ]
            if k
        ]
    )
    finnhub_rate_limit_seconds: int = 1  # 60 calls/min

    # FRED
    fred_keys: List[str] = field(
        default_factory=lambda: [
            k
            for k in [
                os.getenv("FRED_API_KEY"),
                os.getenv("FRED_API_KEY_2"),
                os.getenv("FRED_API_KEY_3"),
            ]
            if k
        ]
    )

    # FMP
    fmp_key: str = os.getenv("FMP_API_KEY", "")

    # Other APIs
    newsapi_key: str = os.getenv("NEWSAPI_KEY", "")
    reddit_client_id: str = os.getenv("REDDIT_CLIENT_ID", "")
    reddit_client_secret: str = os.getenv("REDDIT_CLIENT_SECRET", "")
    binance_api_key: str = os.getenv("BINANCE_API_KEY", "")
    binance_secret: str = os.getenv("BINANCE_SECRET_KEY", "")


@dataclass
class MacroThresholds:
    """Thresholds for macro regime detection (Dalio-style)"""

    # Growth indicators
    gdp_strong_threshold: float = 3.0  # % YoY
    gdp_weak_threshold: float = 1.0
    unemployment_low_threshold: float = 4.0  # %
    unemployment_high_threshold: float = 6.0

    # Inflation indicators
    cpi_target_min: float = 1.5  # %
    cpi_target_max: float = 2.5
    cpi_elevated_threshold: float = 4.0
    cpi_low_threshold: float = 1.0

    # Liquidity indicators
    m2_growth_strong: float = 8.0  # % YoY
    m2_growth_weak: float = 2.0
    real_yield_loose: float = 0.0  # Negative = loose policy
    real_yield_tight: float = 2.0

    # Credit indicators
    bbb_spread_tight: float = 1.5  # %
    bbb_spread_stress: float = 2.5
    hy_spread_tight: float = 3.0
    hy_spread_stress: float = 5.0
    yield_curve_inverted: float = 0.0  # Negative = inverted
    yield_curve_steep: float = 1.5


@dataclass
class QuantitativeThresholds:
    """Thresholds for quantitative screening (Simons-style)"""

    # Valuation
    pe_excellent: float = 10.0
    pe_good: float = 15.0
    pe_expensive: float = 25.0
    pe_extreme: float = 30.0

    pb_excellent: float = 1.5
    pb_expensive: float = 5.0

    peg_excellent: float = 1.0
    peg_expensive: float = 2.0

    # Profitability
    roe_excellent: float = 0.20  # 20%
    roe_good: float = 0.15
    roe_weak: float = 0.05

    operating_margin_excellent: float = 0.20
    operating_margin_weak: float = 0.05

    profit_margin_excellent: float = 0.15
    profit_margin_weak: float = 0.03

    # Growth
    revenue_growth_strong: float = 0.20  # 20% YoY
    revenue_growth_good: float = 0.10

    earnings_growth_strong: float = 0.25
    earnings_growth_good: float = 0.15

    # Financial Health
    debt_to_equity_excellent: float = 0.3
    debt_to_equity_good: float = 0.5
    debt_to_equity_risky: float = 1.5
    debt_to_equity_extreme: float = 2.0

    current_ratio_excellent: float = 2.0
    current_ratio_good: float = 1.5
    current_ratio_weak: float = 1.0

    # Momentum
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # FCF Yield
    fcf_yield_excellent: float = 0.08  # 8%
    fcf_yield_good: float = 0.05
    fcf_yield_weak: float = 0.03


@dataclass
class DrawdownThresholds:
    """Drawdown monitoring thresholds (v3.3)"""

    # Drawdown tiers (as negative percentages)
    caution_threshold: float = -10.0  # -10%: Reduce positions, raise cash to 25%
    defensive_threshold: float = -15.0  # -15%: Halt reflexivity, raise cash to 35%
    risk_off_threshold: float = -20.0  # -20%: Exit speculative, raise cash to 50%
    full_defensive_threshold: float = -30.0  # -30%: Pause all trading for 30 days

    # Cash targets for each tier
    cash_target_normal: float = 15.0  # 15% minimum cash reserve
    cash_target_caution: float = 25.0  # 25% cash at CAUTION
    cash_target_defensive: float = 35.0  # 35% cash at DEFENSIVE
    cash_target_risk_off: float = 50.0  # 50% cash at RISK_OFF
    cash_target_full_defensive: float = 70.0  # 70% cash at FULL_DEFENSIVE

    # Sector concentration limits for each tier
    max_sector_normal: float = 40.0  # 40% max sector allocation (normal)
    max_sector_caution: float = 30.0  # 30% max at CAUTION
    max_sector_defensive: float = 25.0  # 25% max at DEFENSIVE
    max_sector_risk_off: float = 20.0  # 20% max at RISK_OFF
    max_sector_full_defensive: float = 15.0  # 15% max at FULL_DEFENSIVE

    # Lookback period for peak tracking
    peak_lookback_days: int = 252  # 252 trading days = 1 year
    history_retention_days: int = 365  # Keep 1 year of history


@dataclass
class RiskManagement:
    """Risk management parameters"""

    # Position sizing
    max_position_size: float = 0.10  # 10% max per position
    max_sector_allocation: float = 0.30  # 30% max per sector
    min_cash_reserve: float = 0.10  # 10% minimum cash

    # Kelly Criterion
    kelly_fraction: float = 0.5  # Use half-Kelly for safety
    max_kelly_position: float = 0.15  # Cap at 15% even if Kelly says more

    # Stop losses
    stop_loss_percentage: float = 0.08  # 8% stop loss
    trailing_stop_percentage: float = 0.05  # 5% trailing stop

    # =====================================================================
    # TRANSACTION COSTS — SINGLE SOURCE OF TRUTH (v5.38.0)
    # Imported by BOTH PaperTrader (modules/execution/paper_trader.py) and the
    # canonical backtester (modules/testing/walk_forward_backtester.py) via the
    # helpers in modules/analytics/transaction_costs.py. Do NOT redeclare these
    # constants anywhere else — keep one definition so paper trading, backtests,
    # and live assumptions can never silently diverge.
    # =====================================================================

    # --- Equity commissions (IBKR per-share schedule) ---
    commission_per_trade: float = 0.0  # Flat $/trade fallback (most stock brokers)
    commission_per_share: float = 0.005  # IBKR $0.005 per share
    min_commission: float = 1.00  # $1.00 minimum per order
    max_commission_pct: float = 0.01  # Commission capped at 1% of trade value

    # --- Equity bid/ask spread (per leg, fraction of notional) ---
    bid_ask_spread_liquid: float = 0.001  # 0.1% for liquid stocks
    bid_ask_spread_illiquid: float = 0.005  # 0.5% for illiquid

    # --- Equity slippage (per leg, fraction of notional) ---
    slippage_liquid: float = 0.001  # 0.1%
    slippage_illiquid: float = 0.005  # 0.5%

    # --- Square-root market impact (Almgren-style approximation) ---
    # Extra slippage when an order is large relative to ADV:
    #   impact_pct = market_impact_coefficient * sqrt(order_value / adv_dollars)
    # Calibrated so a full-ADV order costs ~10% and a 1%-of-ADV order ~1%.
    market_impact_coefficient: float = 0.10
    market_impact_cap: float = 0.05  # Cap impact at 5% of notional per leg

    # --- Options costs (the silent killer: spreads are 1-3% ROUND-TRIP) ---
    options_commission_per_contract: float = 0.65  # IBKR $0.65/contract
    # Half-spread (per leg) by liquidity tier. Round-trip = 2x these values.
    #   liquid (SPY/QQQ/IWM etc.): 0.5% half → 1.0% round-trip
    #   single-name ITM:           1.5% half → 3.0% round-trip
    options_half_spread_liquid: float = 0.005  # 0.5% half-spread, liquid ETF options
    options_half_spread_single_name: float = 0.015  # 1.5% half-spread, single-name ITM
    # Liquidity gate: reject option legs whose open interest is below this.
    options_min_open_interest: int = 1000


@dataclass
class PatternThresholds:
    """
    Thresholds for pattern detection and scoring

    Controls when patterns trigger and how they're weighted.
    """

    # Information Coefficient thresholds
    min_ic_for_pattern: float = 0.05  # Minimum IC to consider pattern valid
    good_ic_threshold: float = 0.10  # IC > 0.10 is good
    excellent_ic_threshold: float = 0.15  # IC > 0.15 is excellent

    # Pattern confidence thresholds
    min_confidence: float = 0.30  # 30% minimum confidence
    high_confidence: float = 0.70  # 70%+ is high confidence
    very_high_confidence: float = 0.85  # 85%+ is very high

    # Hit rate thresholds
    min_hit_rate: float = 0.55  # 55% minimum hit rate
    good_hit_rate: float = 0.65  # 65%+ is good
    excellent_hit_rate: float = 0.75  # 75%+ is excellent

    # Sample size requirements
    min_sample_size: int = 20  # Minimum observations for pattern
    preferred_sample_size: int = 50  # Preferred sample size

    # Correlation thresholds
    high_correlation: float = 0.70  # 70% correlation is high
    crisis_correlation: float = 0.85  # 85% correlation indicates crisis
    correlation_spike: float = 0.30  # 30% increase is a spike

    # Volatility thresholds
    vix_low: float = 12.0  # VIX < 12 is low volatility
    vix_normal: float = 20.0  # VIX 12-20 is normal
    vix_elevated: float = 30.0  # VIX 20-30 is elevated
    vix_crisis: float = 40.0  # VIX > 40 is crisis

    # Volume thresholds (relative to average)
    volume_surge: float = 2.0  # 2x average volume
    volume_extreme: float = 5.0  # 5x average volume

    # Price momentum thresholds
    strong_momentum: float = 0.20  # 20% gain in period
    weak_momentum: float = -0.10  # -10% in period

    # RSI thresholds
    rsi_oversold: float = 30.0  # RSI < 30 is oversold
    rsi_overbought: float = 70.0  # RSI > 70 is overbought
    rsi_extreme_oversold: float = 20.0  # RSI < 20 is extreme
    rsi_extreme_overbought: float = 80.0  # RSI > 80 is extreme


@dataclass
class SignalValidationThresholds:
    """
    Signal validation thresholds with documented derivation.

    See docs/THRESHOLD_DERIVATION.md for full derivation of each threshold.

    Jim Simons: "If it's not statistically significant, it doesn't exist."
    """

    # IC threshold
    # Derivation: Grinold & Kahn recommendation + empirical backtest 2010-2023
    # Sensitivity range: 0.03-0.07 (+-40% produces similar results)
    ic_threshold: float = 0.05

    # P-value threshold
    # Derivation: Standard 95% confidence level
    # Consideration: May need Bonferroni correction for multiple testing
    p_value_max: float = 0.05

    # Minimum sample size
    # Derivation: CLT requires n>=30 for normal approximation
    min_sample_size: int = 30

    # Minimum win rate
    # Derivation: 50% break-even + 2% transaction cost buffer
    win_rate_min: float = 0.52

    # IC decay thresholds
    # Action levels for signal degradation (see docs/THRESHOLD_DERIVATION.md)
    ic_decay_warning_pct: float = 0.30  # 30% decay = warning, reduce weight
    ic_decay_halt_pct: float = 0.50  # 50% decay = halt pattern usage

    def validate(self) -> List[str]:
        """
        Validate threshold values are within sensible ranges.

        Returns:
            List of error messages. Empty list means all thresholds are valid.

        Example:
            >>> thresholds = SignalValidationThresholds(ic_threshold=0.001)
            >>> errors = thresholds.validate()
            >>> if errors:
            ...     print(f"Invalid thresholds: {errors}")
        """
        errors = []

        # IC threshold validation
        if not 0.01 <= self.ic_threshold <= 0.20:
            errors.append(
                f"ic_threshold {self.ic_threshold} outside sensible range [0.01, 0.20]. "
                f"See docs/THRESHOLD_DERIVATION.md for guidance."
            )

        # P-value validation
        if not 0.001 <= self.p_value_max <= 0.10:
            errors.append(
                f"p_value_max {self.p_value_max} outside sensible range [0.001, 0.10]. "
                f"Standard is 0.05 for 95% confidence."
            )

        # Sample size validation
        if self.min_sample_size < 20:
            errors.append(
                f"min_sample_size {self.min_sample_size} too low (need >=20 for reliable statistics). "
                f"CLT recommends n>=30."
            )

        if self.min_sample_size > 500:
            errors.append(
                f"min_sample_size {self.min_sample_size} may be too restrictive. "
                f"Many valid patterns won't have 500+ observations."
            )

        # Win rate validation
        if not 0.50 <= self.win_rate_min <= 0.70:
            errors.append(
                f"win_rate_min {self.win_rate_min} outside sensible range [0.50, 0.70]. "
                f"Below 0.50 is worse than random; above 0.70 is unrealistic."
            )

        # IC decay thresholds validation
        if not 0.10 <= self.ic_decay_warning_pct <= 0.50:
            errors.append(
                f"ic_decay_warning_pct {self.ic_decay_warning_pct} outside sensible range [0.10, 0.50]. "
                f"Warning should trigger before halt."
            )

        if not 0.30 <= self.ic_decay_halt_pct <= 0.80:
            errors.append(
                f"ic_decay_halt_pct {self.ic_decay_halt_pct} outside sensible range [0.30, 0.80]. "
                f"Halt at >80% decay may be too lenient."
            )

        if self.ic_decay_warning_pct >= self.ic_decay_halt_pct:
            errors.append(
                f"ic_decay_warning_pct ({self.ic_decay_warning_pct}) must be less than "
                f"ic_decay_halt_pct ({self.ic_decay_halt_pct})."
            )

        return errors

    def is_valid(self) -> bool:
        """Check if all thresholds are valid."""
        return len(self.validate()) == 0


@dataclass
class WalkForwardValidationThresholds:
    """
    Walk-forward validation thresholds (Jim Simons standards).

    These thresholds determine whether a pattern is considered valid
    for trading decisions based on out-of-sample testing.
    """

    # Minimum out-of-sample IC for pattern to be valid
    min_oos_ic: float = 0.03

    # Maximum IC degradation allowed (IS to OOS)
    max_degradation_pct: float = 0.50  # 50%

    # Minimum percentage of windows that must be profitable
    min_consistency_score: float = 0.55  # 55%

    # Minimum observations per window
    min_sample_size: int = 30

    # Training window size (in-sample period)
    train_window_days: int = 252  # 1 year

    # Testing window size (out-of-sample period)
    test_window_days: int = 63  # 3 months (1 quarter)

    # Cache expiry for validation results
    cache_expiry_days: int = 7  # Re-validate after 7 days


@dataclass
class MonteCarloConfig:
    """
    Monte Carlo simulation configuration (Jim Simons standards).

    Controls iteration count, early termination, and progress logging
    for statistical significance testing.

    Jim Simons: "If it's not statistically significant, it doesn't exist."
    """

    # Default iterations: 10,000 for production-grade confidence
    # Can be overridden via MONTE_CARLO_ITERATIONS env var
    default_iterations: int = field(
        default_factory=lambda: int(os.getenv("MONTE_CARLO_ITERATIONS", "10000"))
    )

    # Minimum iterations before early termination is allowed
    min_iterations_before_early_stop: int = 5000

    # Early termination p-value thresholds
    # If p-value < early_stop_significant after min_iterations, stop early (clearly significant)
    early_stop_significant_threshold: float = 0.001

    # If p-value > early_stop_insignificant after min_iterations, stop early (clearly random)
    early_stop_insignificant_threshold: float = 0.5

    # Progress logging interval
    progress_log_interval: int = 1000  # Log every 1,000 iterations

    # Enable progress logging
    enable_progress_logging: bool = field(
        default_factory=lambda: os.getenv("MONTE_CARLO_PROGRESS_LOG", "true").lower() == "true"
    )

    # Strict validation mode (--strict-validation flag equivalent)
    strict_validation: bool = field(
        default_factory=lambda: os.getenv("STRICT_VALIDATION", "false").lower() == "true"
    )


@dataclass
class ModelThresholds:
    """
    Thresholds for ML model predictions and performance

    Controls model behavior, prediction filtering, and performance monitoring.
    """

    # Prediction confidence thresholds
    min_prediction_confidence: float = 0.60  # Don't trade on predictions < 60%
    high_prediction_confidence: float = 0.75  # 75%+ is high confidence
    very_high_prediction_confidence: float = 0.85  # 85%+ is very high

    # Model performance thresholds
    min_sharpe_ratio: float = 1.0  # Minimum Sharpe for production use
    good_sharpe_ratio: float = 1.5  # Sharpe > 1.5 is good
    excellent_sharpe_ratio: float = 2.0  # Sharpe > 2.0 is excellent

    min_accuracy: float = 0.55  # 55% minimum accuracy
    good_accuracy: float = 0.65  # 65%+ is good
    excellent_accuracy: float = 0.75  # 75%+ is excellent

    # Information Coefficient
    min_ic: float = 0.05  # IC < 0.05 means no predictive power
    good_ic: float = 0.10  # IC > 0.10 is good
    excellent_ic: float = 0.15  # IC > 0.15 is excellent

    # Backtest requirements
    min_backtest_days: int = 252  # 1 year minimum
    preferred_backtest_days: int = 756  # 3 years preferred
    min_trades_for_validation: int = 30  # Need 30+ trades to validate

    # Feature importance thresholds
    min_feature_importance: float = 0.01  # Drop features < 1% importance
    high_feature_importance: float = 0.10  # 10%+ importance is significant

    # Retraining triggers
    max_performance_degradation: float = 0.15  # Retrain if performance drops 15%
    max_days_without_retrain: int = 90  # Retrain every 90 days
    min_new_samples_for_retrain: int = 100  # Need 100 new samples

    # Ensemble thresholds
    min_models_for_ensemble: int = 3  # Need 3+ models
    max_model_correlation: float = 0.80  # Models with >80% correlation are redundant

    # Outlier detection
    z_score_outlier: float = 3.0  # |z-score| > 3.0 is outlier
    max_drawdown_alert: float = -0.20  # Alert if drawdown > 20%

    # Kelly Criterion
    kelly_fraction: float = 0.25  # Use quarter-Kelly (conservative)
    max_kelly_position: float = 0.15  # Cap Kelly at 15%

    # Signal aggregation
    min_signals_for_consensus: int = 3  # Need 3+ signals
    consensus_agreement_threshold: float = 0.70  # 70% agreement needed


@dataclass
class RegionalWeights:
    """Global market regional weights for macro analysis"""

    us: float = 0.325
    china: float = 0.20
    europe: float = 0.15
    japan: float = 0.10
    india: float = 0.10
    rest_asia: float = 0.075
    emerging: float = 0.05


@dataclass
class RegimeTransitionThresholds:
    """
    Thresholds for regime transition detection (George Soros-style).

    Detects regime transitions using LEADING indicators rather than lagging
    ones like drawdown. The goal is to detect transitions 5-10 days BEFORE
    they become apparent in price action.

    George Soros: "The biggest money is made at inflection points.
    I want to know BEFORE the crowd."
    """

    # =========================================================================
    # INDICATOR WEIGHTS (must sum to 1.0)
    # =========================================================================
    credit_spread_weight: float = 0.25  # Baa-10Y (BAA10Y) spread widening
    vix_term_structure_weight: float = 0.20  # VIX vs VIX3M inversion
    cftc_positioning_weight: float = 0.20  # Extreme positioning
    yield_curve_slope_weight: float = 0.15  # 10Y-2Y rate of change
    breadth_weight: float = 0.10  # % stocks above 200MA
    put_call_ratio_weight: float = 0.10  # CBOE put/call ratio

    # =========================================================================
    # INDICATOR THRESHOLDS
    # =========================================================================

    # Credit spread (Baa-10Y): Warning when > 2 std dev above mean
    credit_spread_std_threshold: float = 2.0  # Standard deviations
    credit_spread_lookback_days: int = 252  # 1 year for std calculation

    # VIX term structure: Inverted when VIX > VIX3M (backwardation)
    vix_inversion_threshold: float = 1.0  # VIX/VIX3M ratio > 1.0 = inverted

    # CFTC positioning: Extreme when > 80th percentile
    cftc_extreme_percentile: float = 80.0  # Above this = crowded trade

    # Yield curve slope (10Y-2Y): Warning when change > 0.5% in 5 days
    yield_curve_change_threshold: float = 0.50  # Percentage points
    yield_curve_change_days: int = 5  # Lookback for change calculation

    # Breadth deterioration: Warning when < 40% stocks above 200MA
    breadth_threshold: float = 40.0  # Percentage of stocks

    # Put/call ratio: Spike when > 1.2
    put_call_spike_threshold: float = 1.2  # Above this = fear

    # =========================================================================
    # TRANSITION STATE THRESHOLDS
    # =========================================================================
    stable_max_score: float = 30.0  # Score < 30 = STABLE
    early_warning_max_score: float = 50.0  # Score 30-50 = EARLY_WARNING
    imminent_max_score: float = 70.0  # Score 50-70 = TRANSITION_IMMINENT
    # Score > 70 = TRANSITION_ACTIVE

    # =========================================================================
    # OPERATIONAL SETTINGS
    # =========================================================================
    history_retention_days: int = 365  # Keep 1 year of history
    calculation_timeout_seconds: float = 10.0  # Max time for calculation

    # =========================================================================
    # BACKTEST TARGETS
    # =========================================================================
    target_lead_time_days: int = 5  # Minimum days of advance warning
    max_false_positive_rate: float = 0.30  # Maximum 30% false positives

    def validate(self) -> List[str]:
        """Validate threshold values are within sensible ranges."""
        errors = []

        # Weights must sum to 1.0
        total_weight = (
            self.credit_spread_weight
            + self.vix_term_structure_weight
            + self.cftc_positioning_weight
            + self.yield_curve_slope_weight
            + self.breadth_weight
            + self.put_call_ratio_weight
        )
        if not 0.99 <= total_weight <= 1.01:
            errors.append(f"Indicator weights sum to {total_weight:.2f}, must equal 1.0")

        # Thresholds validation
        if not 1.0 <= self.credit_spread_std_threshold <= 4.0:
            errors.append(
                f"credit_spread_std_threshold {self.credit_spread_std_threshold} "
                f"outside range [1.0, 4.0]"
            )

        if not 50.0 <= self.cftc_extreme_percentile <= 95.0:
            errors.append(
                f"cftc_extreme_percentile {self.cftc_extreme_percentile} "
                f"outside range [50.0, 95.0]"
            )

        if not 0.8 <= self.put_call_spike_threshold <= 2.0:
            errors.append(
                f"put_call_spike_threshold {self.put_call_spike_threshold} "
                f"outside range [0.8, 2.0]"
            )

        return errors

    def is_valid(self) -> bool:
        """Check if all thresholds are valid."""
        return len(self.validate()) == 0


@dataclass
class AsymmetryConfig:
    """
    Weights and thresholds for the deterministic Asymmetry Hunter (Soros overlay).

    The hunter fuses signals the pipeline ALREADY computes into a scored watchlist.
    Weights are fixed ex-ante (documented here) and hashed via ``weights_hash`` so the
    AI Scoreboard can grade the Soros layer the same way it grades the MasterPrompt.

    George Soros: "Find where perception will break." We only act when >= 3 independent
    components agree on the SAME direction; most runs emit ZERO candidates (silence is
    a feature, not a bug).
    """

    # =========================================================================
    # COMPONENT WEIGHTS (a-e + g/h; must sum to 1.0). Catalyst (f) is a
    # window/gate, not a weighted vote, so it carries no weight here.
    #
    # v6.5.0: the original five were renormalized down to make room for two
    # external EVIDENCE streams (Polymarket divergence g, GDELT pulse h). They
    # are deliberately small — evidence, not standalone triggers — and still
    # need >= min_components agreement to surface a candidate. Adding them
    # changes weights_hash by design (AI Scoreboard attribution rolls to the new
    # config; old grades stay tied to the old hash).
    # =========================================================================
    crowding_weight: float = 0.19  # CFTC COT crowding percentile
    vol_mispricing_weight: float = 0.19  # IV rank / skew extremes
    regime_fragility_weight: float = 0.17  # Regime transition score + trend
    cross_asset_weight: float = 0.17  # Credit/VIX z divergence + commodity flips
    sentiment_extreme_weight: float = 0.15  # Sentiment matrix / put-call extremes
    polymarket_divergence_weight: float = 0.07  # (g) prediction-market vs our data
    gdelt_pulse_weight: float = 0.06  # (h) geopolitical anomaly

    # =========================================================================
    # EXTREME THRESHOLDS (when a component is "firing")
    # =========================================================================
    crowding_extreme_pctl: float = 85.0  # Large-spec percentile >= this = extreme
    iv_rank_high: float = 90.0  # IV rank >= this = rich vol
    iv_rank_low: float = 10.0  # IV rank <= this = cheap vol
    skew_extreme: float = 3.0  # |25d skew| (vol pts) >= this = extreme
    regime_fragility_extreme: float = 60.0  # Transition composite score >= this
    cross_asset_z_div: float = 2.0  # |z_hy - z_vix| >= this = divergence
    sentiment_high_pctl: float = 90.0  # Sentiment percentile > this = euphoria
    sentiment_low_pctl: float = 10.0  # Sentiment percentile < this = panic
    catalyst_window_days: int = 10  # Days-to-event <= this = catalyst proximate
    # (g) Polymarket: |our_implied_prob - market_implied_prob| >= this = divergence
    polymarket_divergence_extreme: float = 0.20
    # (h) GDELT: per-region article-volume z-score >= this (with negative tone) = anomaly
    gdelt_anomaly_z: float = 2.5
    gdelt_tone_max: float = 0.0  # tone must be <= this (negative) to count as stress
    gdelt_min_history: int = 20  # Welford observations before a z-score is trusted

    # =========================================================================
    # CANDIDATE RULE
    # =========================================================================
    min_components: int = 3  # >= this many independent components must agree
    max_candidates: int = 5  # Emit at most this many candidates per run
    min_iv_history_days: int = 60  # Mirror IVSnapshotCollector.MIN_IV_HISTORY_DAYS

    def weights(self) -> "dict[str, float]":
        """Ordered component weights (used for hashing and composite means)."""
        return {
            "crowding": self.crowding_weight,
            "vol_mispricing": self.vol_mispricing_weight,
            "regime_fragility": self.regime_fragility_weight,
            "cross_asset": self.cross_asset_weight,
            "sentiment_extreme": self.sentiment_extreme_weight,
            "polymarket_divergence": self.polymarket_divergence_weight,
            "gdelt_pulse": self.gdelt_pulse_weight,
        }

    def weights_hash(self) -> str:
        """
        Stable 16-char hash of the component weights — the prompt-hash-equivalent
        used by the AI Scoreboard grading contract. Changing any weight changes the
        hash, so historical grades stay attributable to the config that produced them.
        """
        import hashlib
        import json

        payload = json.dumps(self.weights(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def validate(self) -> List[str]:
        """Validate weights sum to 1.0 and thresholds are in sensible ranges."""
        errors = []

        total_weight = sum(self.weights().values())
        if not 0.99 <= total_weight <= 1.01:
            errors.append(f"Asymmetry component weights sum to {total_weight:.2f}, must equal 1.0")

        if not 50.0 <= self.crowding_extreme_pctl <= 99.0:
            errors.append(f"crowding_extreme_pctl {self.crowding_extreme_pctl} outside [50, 99]")
        if not 0.0 <= self.iv_rank_low < self.iv_rank_high <= 100.0:
            errors.append(f"iv_rank_low/high {self.iv_rank_low}/{self.iv_rank_high} invalid")
        if not 0.0 <= self.sentiment_low_pctl < self.sentiment_high_pctl <= 100.0:
            errors.append(
                f"sentiment_low/high pctl {self.sentiment_low_pctl}/"
                f"{self.sentiment_high_pctl} invalid"
            )
        if self.min_components < 2:
            errors.append(f"min_components {self.min_components} must be >= 2")
        if self.max_candidates < 1:
            errors.append(f"max_candidates {self.max_candidates} must be >= 1")
        if not 0.0 < self.polymarket_divergence_extreme <= 1.0:
            errors.append(
                f"polymarket_divergence_extreme {self.polymarket_divergence_extreme} "
                "must be in (0, 1]"
            )
        if self.gdelt_anomaly_z <= 0.0:
            errors.append(f"gdelt_anomaly_z {self.gdelt_anomaly_z} must be > 0")
        if self.gdelt_min_history < 2:
            errors.append(f"gdelt_min_history {self.gdelt_min_history} must be >= 2")

        return errors

    def is_valid(self) -> bool:
        """Check if all thresholds are valid."""
        return len(self.validate()) == 0


@dataclass
class SystemConfig:
    """Overall system configuration"""

    # AI Model Settings (v5.26.0)
    # Default lives in DEFAULT_GEMINI_MODEL (top of this module) — change it there.
    gemini_model: str = field(
        default_factory=lambda: os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    )

    # Strategy engine version (v5.35.0)
    # "v1" = deterministic baseline (12-1 momentum + 200-day SMA trend filter
    #         + macro regime overlay) — see modules/strategy/engine.py
    # "v0" = legacy placeholder (hardcoded AAPL/HOLD), retained for comparison
    strategy_engine_version: str = field(
        default_factory=lambda: os.getenv("STRATEGY_ENGINE_VERSION", "v1")
    )

    # Directories
    output_dir: Path = field(default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "outputs")))
    log_dir: Path = field(default_factory=lambda: Path(os.getenv("LOG_DIR", "logs")))

    # Email settings
    email_enabled: bool = field(
        default_factory=lambda: os.getenv("EMAIL_ENABLED", "true").lower() == "true"
    )
    email_from: str = os.getenv("EMAIL_FROM", "")
    email_to: str = os.getenv("EMAIL_TO", "")
    email_password: str = os.getenv("EMAIL_PASSWORD", "")

    # IBKR settings
    ibkr_enabled: bool = field(
        default_factory=lambda: os.getenv("IBKR_CLIENT_PORTAL_ENABLED", "false").lower() == "true"
    )
    ibkr_base_url: str = os.getenv("IBKR_BASE_URL", "https://localhost:5000/v1/api")
    # SECURITY: SSL verification mode for IBKR connections
    # True (default): Full SSL verification - REQUIRED for production
    # False: Disable SSL verification - ONLY for localhost development with self-signed certs
    # WARNING: Setting to False creates a man-in-the-middle attack vulnerability
    ibkr_secure_mode: bool = field(
        default_factory=lambda: os.getenv("IBKR_SECURE_MODE", "true").lower() == "true"
    )

    # LIVE TRADING GATE (v5.39.0)
    # The IBKR write path (order placement, Prompt 9) is BLOCKED until paper
    # trading PASSES the 90-day validation gate (modules/execution/paper_gate.py).
    # This flag is the operator's explicit opt-in: it is NECESSARY but NOT
    # SUFFICIENT. Even when True, IBKRClient.assert_write_allowed() still requires
    # the paper gate to report PASS over >= 90 calendar days. Default False
    # (paper only). See docs/PAPER_TRADING_GATE.md.
    live_trading_enabled: bool = field(
        default_factory=lambda: os.getenv("IBKR_LIVE_TRADING_ENABLED", "false").lower() == "true"
    )

    # Google Drive
    gdrive_service_json: str = os.getenv("GDRIVE_SERVICE_JSON", "")
    gdrive_folder_id: str = os.getenv("GDRIVE_FOLDER_ID", "")

    # Data quality
    min_data_quality_score: int = 60  # Minimum acceptable quality score
    sanitize_reports: bool = field(
        default_factory=lambda: os.getenv("SANITIZE_REPORTS", "false").lower() == "true"
    )


@dataclass(frozen=True)
class HandoffBudgetConfig:
    """
    Hard per-call token ceilings + section-drop priority for the Phase 2 / Phase 4
    AI handoff (see docs/HANDOFF_BUDGET.md).

    The ceilings are BACKSTOPS, not targets: the dense curated payload built by the
    compressed HybridFormatter should land far below them. enforce_section_budget
    (modules/utils/token_budget.py) drops WHOLE lowest-priority top-level keys —
    never severing a JSON block — only if a payload somehow approaches a ceiling,
    which signals upstream looseness to investigate rather than a normal state.
    """

    phase2_ceiling_tokens: int = 250_000
    phase4_ceiling_tokens: int = 250_000
    # Reserve headroom for the model's RESPONSE (input + output share the window).
    response_headroom_tokens: int = 8_192

    # Phase 1 (Phase 2 prompt) — top-level intelligence sections, HIGH -> LOW.
    # NEWS (market_news) ranks HIGH on purpose: breaking catalysts are the
    # reflexivity triggers the AI most needs. Keys absent from this tuple rank
    # BELOW every listed key (dropped first by enforce_section_budget).
    phase1_section_priority: Tuple[str, ...] = field(
        default_factory=lambda: (
            "regime_analysis",
            "portfolio",
            "asymmetry_watchlist",
            "market_news",
            "macro_data",
            "sentiment",
            "sectors",
            "global_equity_indices",
            "volatility",
            "commodity_futures",
            "polymarket_divergence",
            "gdelt_pulse",
        )
    )

    # Phase 3 (Phase 4 prompt) — per-ticker sub-sections, HIGH -> LOW. Decision-
    # critical fundamentals/quant/options survive longest; the richest historical
    # context (regime_history, similar_periods) sheds first. Whole tickers are
    # NEVER dropped — the AI must decide on every ticker it is handed.
    phase3_section_priority: Tuple[str, ...] = field(
        default_factory=lambda: (
            "fundamentals",
            "quantitative_analysis",
            "options_intelligence",
            "factor_analysis",
            "liquidity_metrics",
            "profile",
            "analyst_recommendations",
            "regime_sensitivity",
            "portfolio_correlation",
            "fundamentals_summary",
            "ic_analysis",
            "price_summary",
            "pattern_performance",
            "similar_periods",
            "regime_history",
        )
    )


@dataclass
class Config:
    """Master configuration object"""

    api: APIConfig = field(default_factory=APIConfig)
    macro_thresholds: MacroThresholds = field(default_factory=MacroThresholds)
    quant_thresholds: QuantitativeThresholds = field(default_factory=QuantitativeThresholds)
    drawdown_thresholds: DrawdownThresholds = field(default_factory=DrawdownThresholds)
    pattern_thresholds: PatternThresholds = field(default_factory=PatternThresholds)
    model_thresholds: ModelThresholds = field(default_factory=ModelThresholds)
    signal_validation: SignalValidationThresholds = field(
        default_factory=SignalValidationThresholds
    )
    walk_forward_validation: WalkForwardValidationThresholds = field(
        default_factory=WalkForwardValidationThresholds
    )
    monte_carlo: MonteCarloConfig = field(default_factory=MonteCarloConfig)
    risk: RiskManagement = field(default_factory=RiskManagement)
    regional_weights: RegionalWeights = field(default_factory=RegionalWeights)
    regime_transition: RegimeTransitionThresholds = field(
        default_factory=RegimeTransitionThresholds
    )
    asymmetry: AsymmetryConfig = field(default_factory=AsymmetryConfig)
    handoff_budget: HandoffBudgetConfig = field(default_factory=HandoffBudgetConfig)
    system: SystemConfig = field(default_factory=SystemConfig)

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from environment variables"""
        return cls()


# Apply operator-panel overrides (config/operator_panel.toml, if present) as
# environment variables BEFORE building the global config instance below, so
# every os.getenv(...) default read throughout this module sees them. See
# modules/config/operator_panel.py for the whitelist + validation.
from modules.config.operator_panel import apply_operator_panel_overrides  # noqa: E402

apply_operator_panel_overrides()

# Global config instance
config = Config.load()
