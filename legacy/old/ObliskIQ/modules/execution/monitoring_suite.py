"""
Monitoring Suite - Extracted from ObliskOrchestrator (v5.22.0)
==============================================================

Encapsulates all monitoring-related functionality:
- Signal decay monitoring
- Correlation monitoring
- Drawdown monitoring
- Health monitoring
- IC decay monitoring

This module reduces orchestrator dependencies by consolidating
monitoring components into a single, cohesive interface.

Ray Dalio: "The machine must be observable. If you can't measure it, you can't manage it."
Jim Simons: "Kill signals that decay; no sunk cost fallacy."

Usage:
    from modules.execution.monitoring_suite import MonitoringSuite, MonitoringStatus

    # Create suite with dependencies
    suite = MonitoringSuite(
        signal_monitor=signal_monitor,
        correlation_monitor=correlation_monitor,
        drawdown_monitor=drawdown_monitor,
        health_monitor=health_monitor,
        ic_decay_monitor=ic_decay_monitor,
    )

    # Get unified status
    status = suite.get_full_status()
    print(f"Overall: {status.overall_status}")

    # Print dashboard
    suite.print_dashboard()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd
    from modules.analytics.signal_decay_monitor import SignalDecayMonitor, AutoDisableICMonitor
    from modules.analytics.correlation_monitor import CorrelationMonitor, CorrelationAnalysis
    from modules.analytics.drawdown_monitor import DrawdownMonitor, DrawdownStatus
    from modules.utils.health_monitor import HealthMonitor

logger = logging.getLogger(__name__)


@dataclass
class MonitoringStatus:
    """
    Combined status from all monitors.

    This dataclass provides a unified view of system health
    from all monitoring components.

    Attributes:
        signal_health: Signal decay monitoring status
        correlation_regime: Current correlation regime (LOW, MODERATE, HIGH, CRISIS)
        correlation_details: Detailed correlation analysis if available
        drawdown_tier: Current drawdown tier (NORMAL, CAUTION, DEFENSIVE, etc.)
        drawdown_pct: Current drawdown percentage
        system_health: Infrastructure health metrics
        ic_decay_status: IC decay monitoring results
        overall_status: Aggregate status ("healthy", "warning", "critical")
        timestamp: When this status was generated
    """

    signal_health: Dict[str, Any]
    correlation_regime: str
    correlation_details: Optional[Dict[str, Any]]
    drawdown_tier: str
    drawdown_pct: float
    system_health: Dict[str, Any]
    ic_decay_status: Dict[str, Any]
    overall_status: str  # "healthy", "warning", "critical"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "signal_health": self.signal_health,
            "correlation_regime": self.correlation_regime,
            "correlation_details": self.correlation_details,
            "drawdown_tier": self.drawdown_tier,
            "drawdown_pct": self.drawdown_pct,
            "system_health": self.system_health,
            "ic_decay_status": self.ic_decay_status,
            "overall_status": self.overall_status,
            "timestamp": self.timestamp.isoformat(),
        }


class MonitoringSuite:
    """
    Unified monitoring interface for ObliskIQ.

    This class encapsulates all monitoring-related components and provides
    a single point of access for health status across the system.

    The suite consolidates:
    - SignalDecayMonitor: Tracks AI signal health and decay
    - CorrelationMonitor: Portfolio correlation and regime detection
    - DrawdownMonitor: Portfolio drawdown tier tracking
    - HealthMonitor: System infrastructure health
    - AutoDisableICMonitor: IC decay with auto-disable

    Ray Dalio: "The machine must know its own health."

    Example:
        suite = MonitoringSuite(
            signal_monitor=signal_monitor,
            correlation_monitor=correlation_monitor,
            drawdown_monitor=drawdown_monitor,
            health_monitor=health_monitor,
            ic_decay_monitor=ic_decay_monitor,
        )

        # Get unified status
        status = suite.get_full_status()

        if status.overall_status == "critical":
            logger.error("System in critical state!")
            suite.print_dashboard()
    """

    def __init__(
        self,
        signal_monitor: "SignalDecayMonitor",
        correlation_monitor: "CorrelationMonitor",
        drawdown_monitor: "DrawdownMonitor",
        health_monitor: "HealthMonitor",
        ic_decay_monitor: "AutoDisableICMonitor",
    ):
        """
        Initialize MonitoringSuite with all monitoring components.

        Args:
            signal_monitor: SignalDecayMonitor for AI signal health
            correlation_monitor: CorrelationMonitor for portfolio correlation
            drawdown_monitor: DrawdownMonitor for portfolio drawdown
            health_monitor: HealthMonitor for system infrastructure
            ic_decay_monitor: AutoDisableICMonitor for IC decay tracking
        """
        self.signal = signal_monitor
        self.correlation = correlation_monitor
        self.drawdown = drawdown_monitor
        self.health = health_monitor
        self.ic_decay = ic_decay_monitor

        logger.info("MonitoringSuite initialized (v5.22.0)")

    def get_full_status(self) -> MonitoringStatus:
        """
        Get combined status from all monitors.

        This method aggregates status from all monitoring components
        and calculates an overall system status.

        Returns:
            MonitoringStatus with unified view of system health

        Example:
            status = suite.get_full_status()
            if status.drawdown_tier in ("RISK_OFF", "FULL_DEFENSIVE"):
                halt_trading()
        """
        # Get signal health
        signal_health = self._get_signal_health()

        # Get correlation regime from history
        correlation_regime, correlation_details = self._get_correlation_info()

        # Get drawdown status
        drawdown_tier, drawdown_pct = self._get_drawdown_info()

        # Get system health
        system_health = self._get_system_health()

        # Get IC decay status
        ic_decay = self._get_ic_decay_status()

        # Calculate overall status
        overall = self._calculate_overall_status(
            signal_health=signal_health,
            correlation_regime=correlation_regime,
            drawdown_tier=drawdown_tier,
            system_health=system_health,
            ic_decay=ic_decay,
        )

        return MonitoringStatus(
            signal_health=signal_health,
            correlation_regime=correlation_regime,
            correlation_details=correlation_details,
            drawdown_tier=drawdown_tier,
            drawdown_pct=drawdown_pct,
            system_health=system_health,
            ic_decay_status=ic_decay,
            overall_status=overall,
        )

    def _get_signal_health(self) -> Dict[str, Any]:
        """Get signal health from SignalDecayMonitor."""
        try:
            report = self.signal.check_signal_health()
            return {
                "signal_name": report.signal_name,
                "status": report.health_status,
                "recent_sharpe": report.recent_sharpe,
                "historical_sharpe": report.historical_sharpe,
                "sharpe_decline_pct": report.sharpe_decline_pct,
                "is_decaying": report.is_decaying,
                "recommended_action": report.recommended_action,
            }
        except Exception as e:
            logger.warning(f"Failed to get signal health: {e}")
            return {
                "status": "UNKNOWN",
                "error": str(e),
            }

    def _get_correlation_info(self) -> tuple[str, Optional[Dict[str, Any]]]:
        """Get correlation regime from CorrelationMonitor."""
        try:
            # Get latest analysis from history
            if self.correlation.correlation_history:
                latest: "CorrelationAnalysis" = self.correlation.correlation_history[-1]
                regime = latest.regime.value if hasattr(latest.regime, 'value') else str(latest.regime)
                details = {
                    "avg_correlation": latest.avg_correlation,
                    "max_correlation": latest.max_correlation,
                    "regime_confidence": latest.regime_confidence,
                    "effective_n_assets": latest.effective_n_assets,
                    "diversification_ratio": latest.diversification_ratio,
                    "high_correlation_pairs": len(latest.high_correlation_pairs),
                    "alerts": len(latest.alerts),
                    # v5.24.0: Multi-timeframe divergence
                    "timeframe_divergence": latest.timeframe_divergence,
                }
                return regime, details
            else:
                return "unknown", None
        except Exception as e:
            logger.warning(f"Failed to get correlation info: {e}")
            return "unknown", {"error": str(e)}

    def _get_drawdown_info(self) -> tuple[str, float]:
        """Get drawdown status from DrawdownMonitor."""
        try:
            status: Optional["DrawdownStatus"] = self.drawdown.get_current_status()
            if status:
                return status.drawdown_tier, status.drawdown_pct
            else:
                return "NORMAL", 0.0
        except Exception as e:
            logger.warning(f"Failed to get drawdown info: {e}")
            return "UNKNOWN", 0.0

    def _get_system_health(self) -> Dict[str, Any]:
        """Get system health from HealthMonitor."""
        try:
            return self.health.get_health_status()
        except Exception as e:
            logger.warning(f"Failed to get system health: {e}")
            return {"status": "unknown", "error": str(e)}

    def _get_ic_decay_status(self) -> Dict[str, Any]:
        """Get IC decay status from AutoDisableICMonitor."""
        try:
            return self.ic_decay.get_decay_report()
        except Exception as e:
            logger.warning(f"Failed to get IC decay status: {e}")
            return {"error": str(e)}

    def _calculate_overall_status(
        self,
        signal_health: Dict[str, Any],
        correlation_regime: str,
        drawdown_tier: str,
        system_health: Dict[str, Any],
        ic_decay: Dict[str, Any],
    ) -> str:
        """
        Calculate overall system status based on all monitoring components.

        Returns:
            "healthy", "warning", or "critical"
        """
        # Critical conditions
        if drawdown_tier in ("RISK_OFF", "FULL_DEFENSIVE"):
            return "critical"
        if system_health.get("status") in ("critical", "unhealthy"):
            return "critical"
        if signal_health.get("status") == "CRITICAL":
            return "critical"
        if correlation_regime == "crisis":
            return "critical"

        # Check IC decay - if many patterns disabled, that's critical
        summary = ic_decay.get("summary", {})
        if summary.get("patterns_disabled", 0) >= 3:
            return "critical"

        # Warning conditions
        if drawdown_tier in ("CAUTION", "DEFENSIVE"):
            return "warning"
        if system_health.get("status") == "degraded":
            return "warning"
        if signal_health.get("status") in ("WARNING", "INSUFFICIENT_DATA"):
            return "warning"
        if correlation_regime == "high":
            return "warning"
        if summary.get("patterns_on_alert", 0) >= 2:
            return "warning"

        return "healthy"

    def get_multi_timeframe_correlation(
        self, returns_data: "pd.DataFrame"
    ) -> Optional[Dict[str, Any]]:
        """
        Get multi-timeframe correlation analysis for Phase 3/4 decisions (v5.24.0).

        This method runs the multi-timeframe analysis and returns actionable
        insights for trading decisions.

        Args:
            returns_data: DataFrame with returns (columns = tickers, rows = dates)

        Returns:
            Dict with multi-timeframe analysis results, or None if analysis fails

        Example:
            mtf = suite.get_multi_timeframe_correlation(returns_df)
            if mtf and mtf["timeframe_divergence"] > 0.5:
                # Recent stress event - be cautious
                position_adjustment *= 0.8
        """
        try:
            from modules.analytics.correlation_monitor import MultiTimeframeCorrelation

            mtf: MultiTimeframeCorrelation = self.correlation.analyze_multi_timeframe(
                returns_data
            )

            return {
                "short_term_regime": mtf.regime_short.value,
                "medium_term_regime": mtf.regime_medium.value,
                "long_term_regime": mtf.regime_long.value,
                "short_term_avg_corr": mtf.short_term.avg_correlation,
                "medium_term_avg_corr": mtf.medium_term.avg_correlation,
                "long_term_avg_corr": mtf.long_term.avg_correlation,
                "timeframe_divergence": mtf.timeframe_divergence,
                "interpretation": mtf.interpretation,
                "timestamp": mtf.timestamp.isoformat(),
            }
        except Exception as e:
            logger.warning(f"Failed to get multi-timeframe correlation: {e}")
            return None

    def print_dashboard(self) -> None:
        """
        Print unified monitoring dashboard to console.

        This provides a quick overview of system health at a glance.
        Ray Dalio: "Radical transparency: show the numbers."
        """
        status = self.get_full_status()

        status_emoji = {
            "healthy": "\u2705",   # Green check
            "warning": "\u26a0\ufe0f",   # Warning
            "critical": "\U0001f6a8",  # Alert siren
        }

        print("\n" + "=" * 70)
        print("OBLISKIQ MONITORING DASHBOARD")
        print("=" * 70)
        print(f"Timestamp: {status.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print()

        emoji = status_emoji.get(status.overall_status, "\u2753")
        print(f"{emoji} Overall Status: {status.overall_status.upper()}")
        print()

        # Drawdown section
        print("-" * 70)
        print("DRAWDOWN MONITOR")
        print("-" * 70)
        tier_emoji = {
            "NORMAL": "\u2705",
            "CAUTION": "\u26a1",
            "DEFENSIVE": "\u26a0\ufe0f",
            "RISK_OFF": "\U0001f534",
            "FULL_DEFENSIVE": "\U0001f6a8",
        }
        dd_emoji = tier_emoji.get(status.drawdown_tier, "\u2753")
        print(f"  {dd_emoji} Tier: {status.drawdown_tier}")
        print(f"  Drawdown: {status.drawdown_pct:+.2f}%")
        print()

        # Correlation section
        print("-" * 70)
        print("CORRELATION MONITOR")
        print("-" * 70)
        regime_emoji = {
            "low": "\u2705",
            "moderate": "\U0001f4ca",
            "high": "\u26a0\ufe0f",
            "crisis": "\U0001f6a8",
        }
        corr_emoji = regime_emoji.get(status.correlation_regime, "\u2753")
        print(f"  {corr_emoji} Regime: {status.correlation_regime.upper()}")
        if status.correlation_details:
            print(f"  Avg Correlation: {status.correlation_details.get('avg_correlation', 0):.3f}")
            print(f"  Effective N Assets: {status.correlation_details.get('effective_n_assets', 0):.1f}")
        print()

        # Signal health section
        print("-" * 70)
        print("SIGNAL HEALTH")
        print("-" * 70)
        signal_status = status.signal_health.get("status", "UNKNOWN")
        signal_emoji = {
            "HEALTHY": "\u2705",
            "IMPROVING": "\U0001f4c8",
            "WARNING": "\u26a0\ufe0f",
            "CRITICAL": "\U0001f6a8",
            "INSUFFICIENT_DATA": "\u2753",
        }
        s_emoji = signal_emoji.get(signal_status, "\u2753")
        print(f"  {s_emoji} Status: {signal_status}")
        if "recent_sharpe" in status.signal_health:
            print(f"  Recent Sharpe (30d): {status.signal_health.get('recent_sharpe', 0):.2f}")
            print(f"  Historical Sharpe: {status.signal_health.get('historical_sharpe', 0):.2f}")
        print()

        # IC Decay section
        print("-" * 70)
        print("IC DECAY MONITOR (Jim Simons Auto-Disable)")
        print("-" * 70)
        summary = status.ic_decay_status.get("summary", {})
        disabled = summary.get("patterns_disabled", 0)
        on_alert = summary.get("patterns_on_alert", 0)
        recovered = summary.get("patterns_recovered", 0)

        if disabled > 0:
            print(f"  \U0001f6ab Patterns Disabled: {disabled}")
        if on_alert > 0:
            print(f"  \u26a0\ufe0f Patterns on Alert: {on_alert}")
        if recovered > 0:
            print(f"  \U0001f504 Patterns Recovered: {recovered}")
        if disabled == 0 and on_alert == 0:
            print("  \u2705 All patterns healthy")
        print()

        # System health section
        print("-" * 70)
        print("SYSTEM HEALTH")
        print("-" * 70)
        sys_status = status.system_health.get("status", "unknown")
        sys_emoji = {
            "healthy": "\u2705",
            "degraded": "\u26a0\ufe0f",
            "unhealthy": "\U0001f534",
            "critical": "\U0001f6a8",
        }
        sys_e = sys_emoji.get(sys_status, "\u2753")
        print(f"  {sys_e} Status: {sys_status.upper()}")
        print(f"  API Error Rate: {status.system_health.get('api_error_rate', 0):.1%}")
        print(f"  Memory Usage: {status.system_health.get('memory_usage_mb', 0):.0f} MB")
        print()

        print("=" * 70)

    def check_trading_allowed(self) -> tuple[bool, List[str]]:
        """
        Check if trading should be allowed based on monitoring status.

        Returns:
            Tuple of (allowed, list_of_reasons_if_blocked)

        Example:
            allowed, reasons = suite.check_trading_allowed()
            if not allowed:
                logger.warning(f"Trading blocked: {reasons}")
        """
        status = self.get_full_status()
        reasons: List[str] = []

        # Check drawdown tier
        if status.drawdown_tier == "FULL_DEFENSIVE":
            reasons.append(f"Drawdown in FULL_DEFENSIVE tier ({status.drawdown_pct:.1f}%)")
        elif status.drawdown_tier == "RISK_OFF":
            reasons.append(f"Drawdown in RISK_OFF tier ({status.drawdown_pct:.1f}%)")

        # Check correlation regime
        if status.correlation_regime == "crisis":
            reasons.append("Correlation in CRISIS regime")

        # Check system health
        if status.system_health.get("status") == "critical":
            reasons.append("System health is CRITICAL")

        # Check signal health
        if status.signal_health.get("status") == "CRITICAL":
            reasons.append("Signal health is CRITICAL")

        return len(reasons) == 0, reasons

    def should_halt_reflexivity(self) -> bool:
        """
        Check if reflexivity trading should be halted.

        Per CLAUDE.md: Reflexivity is halted in DEFENSIVE, RISK_OFF,
        or FULL_DEFENSIVE drawdown tiers.

        Returns:
            True if reflexivity should be halted
        """
        try:
            status = self.drawdown.get_current_status()
            if status:
                return status.should_halt_reflexivity
            return False
        except Exception as e:
            logger.warning(f"Failed to check reflexivity halt: {e}")
            return False  # Default to allowing if check fails

    def get_position_size_adjustment(self) -> float:
        """
        Get position size adjustment factor based on monitoring status.

        Returns:
            Multiplier for position sizes (1.0 = normal, <1.0 = reduced)
        """
        status = self.get_full_status()

        # Drawdown-based adjustments per CLAUDE.md
        tier_adjustments = {
            "NORMAL": 1.0,
            "CAUTION": 0.7,  # Reduce by 30%
            "DEFENSIVE": 0.5,  # Reduce by 50%
            "RISK_OFF": 0.3,  # Reduce by 70%
            "FULL_DEFENSIVE": 0.1,  # Reduce by 90%
        }

        base_adjustment = tier_adjustments.get(status.drawdown_tier, 1.0)

        # Further reduce if correlation is high
        if status.correlation_regime == "crisis":
            base_adjustment *= 0.5
        elif status.correlation_regime == "high":
            base_adjustment *= 0.8

        return base_adjustment
