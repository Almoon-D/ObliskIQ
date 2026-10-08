"""
Risk Limit Enforcer
===================
Hard enforcement of risk limits on all trading decisions.
NO decision gets through without passing these checks.

Ray Dalio: "He who lives by the crystal ball will eat shattered glass."
Jim Simons: "We don't predict, we measure and control risk."
"""

from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

# =============================================================================
# CONSOLIDATED IMPORTS: Use modules.config.risk_limits as source of truth
# =============================================================================
# The RiskLimits class is now imported from the consolidated module.
# This provides backward compatibility while using the canonical source.
# =============================================================================

# Re-export with compatibility: schemas.RiskLimits uses Pydantic, but
# the consolidated module uses a frozen dataclass. For this module,
# we use the consolidated version but maintain the same interface.
from modules.models.schemas import RiskLimits  # Keep for Pydantic validation

from modules.utils.audit_logger import get_audit_logger


@dataclass
class RiskViolation:
    """A single risk limit violation"""

    rule: str
    severity: str  # "CRITICAL", "HIGH", "MEDIUM"
    message: str
    current_value: float
    limit_value: float
    action: str  # "REJECT", "REDUCE", "WARN"


class RiskEnforcer:
    """
    Enforces hard risk limits on all trading decisions.

    Rules enforced:
    1. Position size limits (max % of portfolio)
    2. Sector concentration limits
    3. Correlation limits with existing holdings (new BUYs)
    4. Leverage limit (no margin) + cash-reserve floor
    5. Stop loss requirements
    6. Risk/reward ratio minimums
    7. Liquidity floor

    v6.1.0: cash-reserve, leverage, correlation and liquidity checks were
    ported here from the archived ``modules/risk/risk_manager.py`` so this is
    the SINGLE risk authority (see docs/DEPRECATED_TRIAGE.md). Their limits
    (``min_cash_reserve_pct``, ``max_leverage``, ``max_correlation``,
    ``min_liquidity_score``) already existed on ``RiskLimits`` but were not
    enforced anywhere — now they are.
    """

    def __init__(self, risk_limits: Optional[RiskLimits] = None):
        """
        Initialize risk enforcer.

        Args:
            risk_limits: Risk limits configuration (uses default if None)
        """
        if risk_limits is None:
            # Use default risk limits
            risk_limits = RiskLimits()

        self.limits = risk_limits
        self.audit = get_audit_logger()

    def validate_decision(
        self,
        ticker: str,
        action: str,
        position_size_pct: float,
        sector: Optional[str] = None,
        stop_loss_pct: Optional[float] = None,
        entry_price: Optional[float] = None,
        target_price: Optional[float] = None,
        current_portfolio: Optional[Dict[str, Any]] = None,
        reflexivity_intensity: Optional[float] = None,
        correlations: Optional[Dict[str, float]] = None,
        liquidity_score: Optional[float] = None,
    ) -> Tuple[bool, List[RiskViolation]]:
        """
        Validate a single trading decision against risk limits.

        Args:
            ticker: Stock ticker symbol
            action: BUY, SELL, or HOLD
            position_size_pct: Proposed position size as % of portfolio
            sector: Stock's sector
            stop_loss_pct: Stop loss as % (negative)
            entry_price: Proposed entry price
            target_price: Target price
            current_portfolio: Current portfolio holdings
            reflexivity_intensity: Reflexivity intensity from Phase 2 (0.0-1.0).
                                   If >= 0.6, stricter R:R requirements apply.
            correlations: Optional {ticker: corr} of this name vs existing
                          holdings. Checked for new BUYs only (v6.1.0).
            liquidity_score: Optional 0-100 liquidity score; rejected below the
                             ``min_liquidity_score`` floor for BUYs (v6.1.0).

        Returns:
            Tuple of (is_valid, list_of_violations)
        """
        violations = []

        # Only validate BUY decisions
        if action != "BUY":
            return True, []

        # 1. Position Size Limit
        if position_size_pct > self.limits.max_position_pct:
            violations.append(
                RiskViolation(
                    rule="MAX_POSITION_SIZE",
                    severity="CRITICAL",
                    message=f"Position size {position_size_pct:.1%} exceeds limit {self.limits.max_position_pct:.1%}",
                    current_value=position_size_pct,
                    limit_value=self.limits.max_position_pct,
                    action="REJECT",
                )
            )

        # 2. Minimum Position Size
        if position_size_pct < self.limits.min_position_pct:
            violations.append(
                RiskViolation(
                    rule="MIN_POSITION_SIZE",
                    severity="HIGH",
                    message=f"Position size {position_size_pct:.1%} below minimum {self.limits.min_position_pct:.1%}",
                    current_value=position_size_pct,
                    limit_value=self.limits.min_position_pct,
                    action="REJECT",
                )
            )

        # 3. Stop Loss Requirement
        if self.limits.require_stop_loss and (stop_loss_pct is None or stop_loss_pct >= 0):
            violations.append(
                RiskViolation(
                    rule="STOP_LOSS_REQUIRED",
                    severity="CRITICAL",
                    message=f"Stop loss is required but not set or invalid (got: {stop_loss_pct})",
                    current_value=stop_loss_pct if stop_loss_pct else 0,
                    limit_value=self.limits.max_stop_loss_pct,
                    action="REJECT",
                )
            )

        # 4. Stop Loss Too Wide
        if stop_loss_pct is not None and stop_loss_pct < self.limits.max_stop_loss_pct:
            violations.append(
                RiskViolation(
                    rule="STOP_LOSS_TOO_WIDE",
                    severity="HIGH",
                    message=f"Stop loss {stop_loss_pct:.1%} is wider than limit {self.limits.max_stop_loss_pct:.1%}",
                    current_value=stop_loss_pct,
                    limit_value=self.limits.max_stop_loss_pct,
                    action="REJECT",
                )
            )

        # 5. Risk/Reward Ratio
        if entry_price and target_price and stop_loss_pct:
            stop_loss_price = entry_price * (1 + stop_loss_pct)
            risk = entry_price - stop_loss_price
            reward = target_price - entry_price

            if risk > 0:  # Avoid division by zero
                risk_reward_ratio = reward / risk

                if risk_reward_ratio < self.limits.min_risk_reward_ratio:
                    violations.append(
                        RiskViolation(
                            rule="MIN_RISK_REWARD",
                            severity="HIGH",
                            message=f"Risk/reward ratio {risk_reward_ratio:.2f} below minimum {self.limits.min_risk_reward_ratio:.2f}",
                            current_value=risk_reward_ratio,
                            limit_value=self.limits.min_risk_reward_ratio,
                            action="REJECT",
                        )
                    )

                # 5b. Reflexivity Trade R:R Requirement (Soros: R:R >= 3:1)
                # High-intensity reflexivity trades require stricter risk/reward
                if reflexivity_intensity is not None and reflexivity_intensity >= 0.6:
                    if risk_reward_ratio < self.limits.min_reflexivity_risk_reward:
                        violations.append(
                            RiskViolation(
                                rule="REFLEXIVITY_MIN_RR",
                                severity="CRITICAL",
                                message=f"Reflexivity trade (intensity={reflexivity_intensity:.2f}) requires R:R >= {self.limits.min_reflexivity_risk_reward:.1f}, got {risk_reward_ratio:.2f}",
                                current_value=risk_reward_ratio,
                                limit_value=self.limits.min_reflexivity_risk_reward,
                                action="REJECT",
                            )
                        )

        # 6. Sector Concentration (if portfolio provided)
        if sector and current_portfolio:
            sector_exposure = self._calculate_sector_exposure(sector, current_portfolio)
            new_exposure = sector_exposure + position_size_pct

            if new_exposure > self.limits.max_sector_pct:
                violations.append(
                    RiskViolation(
                        rule="MAX_SECTOR_CONCENTRATION",
                        severity="CRITICAL",
                        message=f"Sector {sector} exposure would be {new_exposure:.1%}, exceeds limit {self.limits.max_sector_pct:.1%}",
                        current_value=new_exposure,
                        limit_value=self.limits.max_sector_pct,
                        action="REJECT",
                    )
                )

        # 7. Single Stock Concentration
        if current_portfolio:
            current_ticker_exposure = self._get_ticker_exposure(ticker, current_portfolio)
            new_ticker_exposure = current_ticker_exposure + position_size_pct

            if new_ticker_exposure > self.limits.max_position_pct:
                violations.append(
                    RiskViolation(
                        rule="DUPLICATE_POSITION",
                        severity="CRITICAL",
                        message=f"Already hold {current_ticker_exposure:.1%} of {ticker}, new total would be {new_ticker_exposure:.1%}",
                        current_value=new_ticker_exposure,
                        limit_value=self.limits.max_position_pct,
                        action="REJECT",
                    )
                )

        # 8. Portfolio Allocation Limit
        if current_portfolio:
            # Calculate current total allocation
            positions = current_portfolio.get("positions", {})
            total_value = current_portfolio.get("total_value", 1.0)
            current_allocated = sum(pos.get("value", 0) for pos in positions.values()) / total_value if total_value > 0 else 0
            new_allocation = current_allocated + position_size_pct

            if new_allocation > self.limits.max_portfolio_allocation:
                violations.append(
                    RiskViolation(
                        rule="MAX_PORTFOLIO_ALLOCATION",
                        severity="CRITICAL",
                        message=f"Total portfolio allocation would be {new_allocation:.1%}, exceeds limit {self.limits.max_portfolio_allocation:.1%}",
                        current_value=new_allocation,
                        limit_value=self.limits.max_portfolio_allocation,
                        action="REJECT",
                    )
                )

            # 9. Cash Reserve Floor (Dalio: keep dry powder).
            # Ported from risk_manager (v6.1.0). The stricter of this floor and
            # MAX_PORTFOLIO_ALLOCATION binds; with defaults that is a 10% cash
            # minimum (looser than CLAUDE.md's 15% NORMAL target, so consistent).
            cash_pct = 1.0 - new_allocation
            # 1e-9 tolerance so landing exactly at the floor (e.g. 90% deployed
            # -> 10.0% cash) passes despite binary-float rounding.
            if cash_pct < self.limits.min_cash_reserve_pct - 1e-9:
                violations.append(
                    RiskViolation(
                        rule="MIN_CASH_RESERVE",
                        severity="HIGH",
                        message=f"Cash reserve would fall to {cash_pct:.1%}, below minimum {self.limits.min_cash_reserve_pct:.1%}",
                        current_value=cash_pct,
                        limit_value=self.limits.min_cash_reserve_pct,
                        action="REJECT",
                    )
                )

            # 10. Leverage limit (no margin: gross exposure <= max_leverage).
            # Ported from risk_manager (v6.1.0). In a long-only book the 95%/90%
            # caps already prevent leverage; this is an explicit guard against a
            # malformed >100% sized decision.
            if new_allocation > self.limits.max_leverage:
                violations.append(
                    RiskViolation(
                        rule="MAX_LEVERAGE",
                        severity="CRITICAL",
                        message=f"Gross exposure would be {new_allocation:.2f}x, exceeds leverage limit {self.limits.max_leverage:.2f}x",
                        current_value=new_allocation,
                        limit_value=self.limits.max_leverage,
                        action="REJECT",
                    )
                )

        # 11. Correlation with existing holdings (new BUYs only).
        # Ported from risk_manager (v6.1.0). Fills the documented-but-unenforced
        # max_correlation gap. Activates only when correlations are provided.
        if correlations:
            already_held = bool(
                current_portfolio and ticker in current_portfolio.get("positions", {})
            )
            if not already_held:
                usable = [abs(c) for c in correlations.values() if c is not None]
                if usable:
                    max_corr = max(usable)
                    if max_corr > self.limits.max_correlation:
                        violations.append(
                            RiskViolation(
                                rule="MAX_CORRELATION",
                                severity="HIGH",
                                message=f"Max correlation with holdings {max_corr:.2f} exceeds limit {self.limits.max_correlation:.2f}",
                                current_value=max_corr,
                                limit_value=self.limits.max_correlation,
                                action="REJECT",
                            )
                        )

        # 12. Liquidity floor.
        # Ported from risk_manager (v6.1.0). Activates only when a liquidity
        # score is supplied (avoids slippage/exit risk on illiquid names).
        if liquidity_score is not None and liquidity_score < self.limits.min_liquidity_score:
            violations.append(
                RiskViolation(
                    rule="MIN_LIQUIDITY",
                    severity="HIGH",
                    message=f"Liquidity score {liquidity_score:.0f} below minimum {self.limits.min_liquidity_score:.0f}",
                    current_value=liquidity_score,
                    limit_value=self.limits.min_liquidity_score,
                    action="REJECT",
                )
            )

        # Log violations to audit trail
        for violation in violations:
            self.audit.log_risk_violation(
                violation_type=violation.rule,
                details={
                    "ticker": ticker,
                    "action": action,
                    "position_size_pct": position_size_pct,
                    "violation": violation.message,
                    "severity": violation.severity,
                },
            )

        # Decision is valid only if no violations
        is_valid = len(violations) == 0

        return is_valid, violations

    def validate_portfolio_decisions(
        self, decisions: List[Dict[str, Any]], current_portfolio: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Validate multiple trading decisions at once.

        Args:
            decisions: List of decision dicts with keys: ticker, action, position_size_pct, etc.
            current_portfolio: Current portfolio holdings

        Returns:
            Tuple of (approved_decisions, rejected_decisions)
        """
        approved = []
        rejected = []

        total_new_allocation = 0.0

        for decision in decisions:
            ticker = decision.get("ticker", decision.get("symbol"))
            action = decision.get("action", "HOLD")
            position_size = decision.get("position_size_pct", decision.get("position_size", 0))

            # Skip decisions without ticker
            if not ticker:
                rejected.append({
                    **decision,
                    "rejection_reason": "Missing ticker/symbol",
                    "violations": ["MISSING_TICKER"]
                })
                continue

            # Skip non-BUY actions
            if action != "BUY":
                approved.append(decision)
                continue

            # Validate this decision
            is_valid, violations = self.validate_decision(
                ticker=ticker,
                action=action,
                position_size_pct=position_size,
                sector=decision.get("sector"),
                stop_loss_pct=decision.get("stop_loss_pct", decision.get("stop_loss")),
                entry_price=decision.get("entry_price"),
                target_price=decision.get("target_price"),
                current_portfolio=current_portfolio,
                reflexivity_intensity=decision.get("reflexivity_intensity"),
                correlations=decision.get("correlations"),
                liquidity_score=decision.get("liquidity_score"),
            )

            if is_valid:
                # Check portfolio-wide limits
                total_new_allocation += position_size

                if total_new_allocation > self.limits.max_portfolio_allocation:
                    # Reject - would over-allocate portfolio
                    decision["rejection_reason"] = (
                        f"Total allocation {total_new_allocation:.1%} exceeds limit {self.limits.max_portfolio_allocation:.1%}"
                    )
                    decision["violations"] = [
                        {
                            "rule": "MAX_PORTFOLIO_ALLOCATION",
                            "message": decision["rejection_reason"],
                        }
                    ]
                    rejected.append(decision)

                    self.audit.log_decision_rejected(
                        ticker=ticker,
                        reason=decision["rejection_reason"],
                        original_decision=decision,
                    )
                else:
                    approved.append(decision)
            else:
                # Rejected due to violations
                decision["rejection_reason"] = "; ".join([v.message for v in violations])
                decision["violations"] = [
                    {"rule": v.rule, "message": v.message, "severity": v.severity}
                    for v in violations
                ]
                rejected.append(decision)

                self.audit.log_decision_rejected(
                    ticker=ticker, reason=decision["rejection_reason"], original_decision=decision
                )

        return approved, rejected

    def validate_convexity_dossier(
        self,
        dossier: Dict[str, Any],
        open_dossiers: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, List[RiskViolation]]:
        """
        Enforce convexity-sleeve rails on ONE Asymmetry Hunter dossier.

        HARD rails, SEPARATE from Phase 4 position sizing (the hunter never sizes up
        a Phase 4 position; this is its own sleeve):
          - per-dossier premium cap (per_dossier_convexity_cap, default 2%)
          - option expressions must have DTE >= options_min_dte (default 30 days)
          - adding this dossier must keep total open sleeve <=
            total_convexity_sleeve_pct (default 5%)

        Args:
            dossier: dict with keys max_premium_pct, instrument_type, and either
                expiry_days or expiry_months, plus asset/mapped_ticker for labelling.
            open_dossiers: already-approved asymmetry expressions (each carrying
                max_premium_pct) counted against the running sleeve.

        Returns:
            Tuple of (is_valid, list_of_violations).
        """
        violations: List[RiskViolation] = []
        open_dossiers = open_dossiers or []

        premium = float(dossier.get("max_premium_pct", 0.0) or 0.0)
        instrument = str(dossier.get("instrument_type", "etf"))
        label = str(dossier.get("mapped_ticker") or dossier.get("asset") or "?")

        # 1. Per-dossier premium cap
        if premium > self.limits.per_dossier_convexity_cap:
            violations.append(
                RiskViolation(
                    rule="CONVEXITY_PER_DOSSIER_CAP",
                    severity="CRITICAL",
                    message=(
                        f"Dossier {label} premium {premium:.1%} exceeds per-dossier cap "
                        f"{self.limits.per_dossier_convexity_cap:.1%}"
                    ),
                    current_value=premium,
                    limit_value=self.limits.per_dossier_convexity_cap,
                    action="REJECT",
                )
            )

        # 2. Minimum DTE for option expressions (never recommend < options_min_dte)
        if instrument in ("long_call", "long_put"):
            dte = dossier.get("expiry_days")
            if dte is None and dossier.get("expiry_months") is not None:
                dte = int(round(float(dossier["expiry_months"]) * 30))
            if dte is not None and int(dte) < self.limits.options_min_dte:
                violations.append(
                    RiskViolation(
                        rule="OPTION_MIN_DTE",
                        severity="CRITICAL",
                        message=(
                            f"Dossier {label} option DTE {int(dte)} below minimum "
                            f"{self.limits.options_min_dte}"
                        ),
                        current_value=float(dte),
                        limit_value=float(self.limits.options_min_dte),
                        action="REJECT",
                    )
                )

        # 3. Total sleeve would be breached by adding this dossier
        open_total = sum(float(d.get("max_premium_pct", 0.0) or 0.0) for d in open_dossiers)
        if open_total + premium > self.limits.total_convexity_sleeve_pct + 1e-9:
            violations.append(
                RiskViolation(
                    rule="CONVEXITY_SLEEVE_TOTAL",
                    severity="CRITICAL",
                    message=(
                        f"Adding dossier {label} ({premium:.1%}) pushes sleeve to "
                        f"{open_total + premium:.1%}, over limit "
                        f"{self.limits.total_convexity_sleeve_pct:.1%}"
                    ),
                    current_value=open_total + premium,
                    limit_value=self.limits.total_convexity_sleeve_pct,
                    action="REJECT",
                )
            )

        for violation in violations:
            self.audit.log_risk_violation(
                violation_type=violation.rule,
                details={
                    "asset": label,
                    "instrument_type": instrument,
                    "max_premium_pct": premium,
                    "violation": violation.message,
                    "severity": violation.severity,
                },
            )

        return len(violations) == 0, violations

    def validate_convexity_sleeve(
        self, dossiers: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Enforce the total convexity sleeve across ALL candidate dossiers at once.

        Greedily admits dossiers in descending composite_score order, applying the
        per-dossier + DTE rails and the running sleeve cap. The highest-conviction
        asymmetries win the limited sleeve; the rest are rejected (NOT resized).

        Returns:
            Tuple of (approved_dossiers, rejected_dossiers). Rejected dossiers carry a
            'rejection_reason' and 'violations' list, mirroring
            validate_portfolio_decisions.
        """
        approved: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []

        ranked = sorted(
            dossiers,
            key=lambda d: float(d.get("composite_score", 0.0) or 0.0),
            reverse=True,
        )

        for dossier in ranked:
            is_valid, violations = self.validate_convexity_dossier(
                dossier, open_dossiers=approved
            )
            if is_valid:
                approved.append(dossier)
            else:
                rejected.append(
                    {
                        **dossier,
                        "rejection_reason": "; ".join(v.message for v in violations),
                        "violations": [
                            {"rule": v.rule, "message": v.message, "severity": v.severity}
                            for v in violations
                        ],
                    }
                )

        return approved, rejected

    def _calculate_sector_exposure(self, sector: str, portfolio: Dict) -> float:
        """Calculate current exposure to a sector"""
        if not portfolio or "positions" not in portfolio:
            return 0.0

        sector_value = 0.0
        total_value = portfolio.get("total_value", 1.0)

        for ticker, position in portfolio.get("positions", {}).items():
            if position.get("sector") == sector:
                sector_value += position.get("value", 0.0)

        return sector_value / total_value if total_value > 0 else 0.0

    def _get_ticker_exposure(self, ticker: str, portfolio: Dict) -> float:
        """Get current exposure to a specific ticker"""
        if not portfolio or "positions" not in portfolio:
            return 0.0

        total_value = portfolio.get("total_value", 1.0)

        for pos_ticker, position in portfolio.get("positions", {}).items():
            if pos_ticker == ticker:
                return position.get("value", 0.0) / total_value if total_value > 0 else 0.0

        return 0.0

    def get_limits_summary(self) -> str:
        """Get summary of current risk limits as formatted string"""
        lines = [
            "Risk Limits Summary:",
            f"  Max Position Size: {self.limits.max_position_pct:.1%}",
            f"  Min Position Size: {self.limits.min_position_pct:.1%}",
            f"  Max Sector Concentration: {self.limits.max_sector_pct:.1%}",
            f"  Max Portfolio Allocation: {self.limits.max_portfolio_allocation:.1%}",
            f"  Min Cash Reserve: {self.limits.min_cash_reserve_pct:.1%}",
            f"  Max Leverage: {self.limits.max_leverage:.2f}x",
            f"  Max Stop Loss: {self.limits.max_stop_loss_pct:.1%}",
            f"  Min Risk/Reward Ratio: {self.limits.min_risk_reward_ratio:.2f}",
            f"  Min Reflexivity R:R: {self.limits.min_reflexivity_risk_reward:.2f}",
            f"  Require Stop Loss: {self.limits.require_stop_loss}",
            f"  Max Correlation: {self.limits.max_correlation:.2f}",
            f"  Min Liquidity Score: {self.limits.min_liquidity_score:.0f}",
        ]
        return "\n".join(lines)
