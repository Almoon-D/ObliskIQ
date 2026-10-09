"""
Tests for MonitoringSuite (v5.22.0)

Tests the consolidated monitoring interface that encapsulates:
- Signal decay monitoring
- Correlation monitoring
- Drawdown monitoring
- Health monitoring
- IC decay monitoring

Ray Dalio: "Test the machine so it works when you need it."
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock
from dataclasses import dataclass
from enum import Enum

# Add parent directory to path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.execution.monitoring_suite import MonitoringSuite, MonitoringStatus


# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


# =============================================================================
# MOCK CLASSES
# =============================================================================


class MockCorrelationRegime(Enum):
    """Mock correlation regime for testing."""
    LOW_CORRELATION = "low"
    MODERATE_CORRELATION = "moderate"
    HIGH_CORRELATION = "high"
    CRISIS_CORRELATION = "crisis"


@dataclass
class MockSignalHealthReport:
    """Mock signal health report."""
    signal_name: str = "AI_Recommendations"
    recent_sharpe: float = 1.5
    historical_sharpe: float = 1.8
    sharpe_decline_pct: float = 16.7
    is_decaying: bool = False
    health_status: str = "HEALTHY"
    recommended_action: str = "Continue normal operation"


@dataclass
class MockCorrelationAnalysis:
    """Mock correlation analysis."""
    timestamp: datetime = None
    avg_correlation: float = 0.45
    max_correlation: float = 0.72
    min_correlation: float = 0.15
    regime: MockCorrelationRegime = MockCorrelationRegime.MODERATE_CORRELATION
    regime_confidence: float = 0.85
    diversification_ratio: float = 1.35
    effective_n_assets: float = 5.2
    high_correlation_pairs: list = None
    alerts: list = None
    timeframe_divergence: float = 0.1  # v5.24.0: Multi-timeframe divergence

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)
        if self.high_correlation_pairs is None:
            self.high_correlation_pairs = []
        if self.alerts is None:
            self.alerts = []


@dataclass
class MockDrawdownStatus:
    """Mock drawdown status."""
    timestamp: str = "2024-01-15T10:00:00Z"
    current_nav: float = 95000.0
    peak_nav: float = 100000.0
    peak_date: str = "2024-01-01T00:00:00Z"
    drawdown_pct: float = -5.0
    drawdown_tier: str = "NORMAL"
    recommended_action: str = "Normal operations"
    position_size_adjustment: str = "Standard sizing"
    cash_target_pct: float = 15.0
    max_sector_pct: float = 40.0
    should_halt_reflexivity: bool = False


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def mock_signal_monitor():
    """Create mock signal monitor."""
    mock = Mock()
    mock.check_signal_health.return_value = MockSignalHealthReport()
    return mock


@pytest.fixture
def mock_correlation_monitor():
    """Create mock correlation monitor."""
    mock = Mock()
    mock.correlation_history = [MockCorrelationAnalysis()]
    return mock


@pytest.fixture
def mock_drawdown_monitor():
    """Create mock drawdown monitor."""
    mock = Mock()
    mock.get_current_status.return_value = MockDrawdownStatus()
    return mock


@pytest.fixture
def mock_health_monitor():
    """Create mock health monitor."""
    mock = Mock()
    mock.get_health_status.return_value = {
        "status": "healthy",
        "api_error_rate": 0.01,
        "memory_usage_mb": 512.0,
        "recent_critical_alerts": 0,
        "recent_warning_alerts": 0,
    }
    return mock


@pytest.fixture
def mock_ic_decay_monitor():
    """Create mock IC decay monitor."""
    mock = Mock()
    mock.get_decay_report.return_value = {
        "last_full_check": datetime.now(timezone.utc).isoformat(),
        "config": {
            "alert_threshold": 0.20,
            "reduce_weight_threshold": 0.30,
            "disable_threshold": 0.50,
        },
        "summary": {
            "patterns_disabled": 0,
            "patterns_on_alert": 1,
            "patterns_recovered": 0,
        },
        "recent_actions": [],
    }
    return mock


@pytest.fixture
def monitoring_suite(
    mock_signal_monitor,
    mock_correlation_monitor,
    mock_drawdown_monitor,
    mock_health_monitor,
    mock_ic_decay_monitor,
):
    """Create MonitoringSuite with mocked dependencies."""
    return MonitoringSuite(
        signal_monitor=mock_signal_monitor,
        correlation_monitor=mock_correlation_monitor,
        drawdown_monitor=mock_drawdown_monitor,
        health_monitor=mock_health_monitor,
        ic_decay_monitor=mock_ic_decay_monitor,
    )


# =============================================================================
# INITIALIZATION TESTS
# =============================================================================


def test_monitoring_suite_initialization(monitoring_suite):
    """Test MonitoringSuite initializes with all components."""
    assert monitoring_suite.signal is not None
    assert monitoring_suite.correlation is not None
    assert monitoring_suite.drawdown is not None
    assert monitoring_suite.health is not None
    assert monitoring_suite.ic_decay is not None


def test_monitoring_suite_stores_dependencies(
    mock_signal_monitor,
    mock_correlation_monitor,
    mock_drawdown_monitor,
    mock_health_monitor,
    mock_ic_decay_monitor,
):
    """Test that MonitoringSuite stores the correct dependencies."""
    suite = MonitoringSuite(
        signal_monitor=mock_signal_monitor,
        correlation_monitor=mock_correlation_monitor,
        drawdown_monitor=mock_drawdown_monitor,
        health_monitor=mock_health_monitor,
        ic_decay_monitor=mock_ic_decay_monitor,
    )

    assert suite.signal == mock_signal_monitor
    assert suite.correlation == mock_correlation_monitor
    assert suite.drawdown == mock_drawdown_monitor
    assert suite.health == mock_health_monitor
    assert suite.ic_decay == mock_ic_decay_monitor


# =============================================================================
# GET FULL STATUS TESTS
# =============================================================================


def test_get_full_status_returns_monitoring_status(monitoring_suite):
    """Test get_full_status returns MonitoringStatus dataclass."""
    status = monitoring_suite.get_full_status()

    assert isinstance(status, MonitoringStatus)
    assert status.overall_status in ("healthy", "warning", "critical")


def test_get_full_status_contains_signal_health(monitoring_suite):
    """Test that status contains signal health information."""
    status = monitoring_suite.get_full_status()

    assert "status" in status.signal_health
    assert status.signal_health["status"] == "HEALTHY"
    assert "recent_sharpe" in status.signal_health


def test_get_full_status_contains_correlation_regime(monitoring_suite):
    """Test that status contains correlation regime."""
    status = monitoring_suite.get_full_status()

    assert status.correlation_regime == "moderate"
    assert status.correlation_details is not None
    assert "avg_correlation" in status.correlation_details


def test_get_full_status_contains_drawdown_info(monitoring_suite):
    """Test that status contains drawdown information."""
    status = monitoring_suite.get_full_status()

    assert status.drawdown_tier == "NORMAL"
    assert status.drawdown_pct == -5.0


def test_get_full_status_contains_system_health(monitoring_suite):
    """Test that status contains system health."""
    status = monitoring_suite.get_full_status()

    assert "status" in status.system_health
    assert status.system_health["status"] == "healthy"


def test_get_full_status_contains_ic_decay_status(monitoring_suite):
    """Test that status contains IC decay information."""
    status = monitoring_suite.get_full_status()

    assert "summary" in status.ic_decay_status
    assert status.ic_decay_status["summary"]["patterns_disabled"] == 0


def test_get_full_status_healthy_scenario(monitoring_suite):
    """Test overall status is healthy when all monitors report healthy."""
    status = monitoring_suite.get_full_status()

    assert status.overall_status == "healthy"


# =============================================================================
# OVERALL STATUS CALCULATION TESTS
# =============================================================================


def test_overall_status_critical_on_drawdown_risk_off(monitoring_suite):
    """Test critical status when drawdown is in RISK_OFF tier."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="RISK_OFF",
        drawdown_pct=-22.0,
    )

    status = monitoring_suite.get_full_status()
    assert status.overall_status == "critical"


def test_overall_status_critical_on_drawdown_full_defensive(monitoring_suite):
    """Test critical status when drawdown is FULL_DEFENSIVE."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="FULL_DEFENSIVE",
        drawdown_pct=-35.0,
    )

    status = monitoring_suite.get_full_status()
    assert status.overall_status == "critical"


def test_overall_status_critical_on_crisis_correlation(monitoring_suite):
    """Test critical status when correlation is in crisis regime."""
    monitoring_suite.correlation.correlation_history = [
        MockCorrelationAnalysis(regime=MockCorrelationRegime.CRISIS_CORRELATION)
    ]

    status = monitoring_suite.get_full_status()
    assert status.overall_status == "critical"


def test_overall_status_critical_on_system_unhealthy(monitoring_suite):
    """Test critical status when system health is unhealthy."""
    monitoring_suite.health.get_health_status.return_value = {
        "status": "unhealthy",
        "api_error_rate": 0.15,
    }

    status = monitoring_suite.get_full_status()
    assert status.overall_status == "critical"


def test_overall_status_critical_on_signal_critical(monitoring_suite):
    """Test critical status when signal health is critical."""
    monitoring_suite.signal.check_signal_health.return_value = MockSignalHealthReport(
        health_status="CRITICAL",
        is_decaying=True,
    )

    status = monitoring_suite.get_full_status()
    assert status.overall_status == "critical"


def test_overall_status_warning_on_drawdown_caution(monitoring_suite):
    """Test warning status when drawdown is in CAUTION tier."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="CAUTION",
        drawdown_pct=-12.0,
    )

    status = monitoring_suite.get_full_status()
    assert status.overall_status == "warning"


def test_overall_status_warning_on_high_correlation(monitoring_suite):
    """Test warning status when correlation is high."""
    monitoring_suite.correlation.correlation_history = [
        MockCorrelationAnalysis(regime=MockCorrelationRegime.HIGH_CORRELATION)
    ]

    status = monitoring_suite.get_full_status()
    assert status.overall_status == "warning"


def test_overall_status_critical_on_many_patterns_disabled(monitoring_suite):
    """Test critical status when 3+ patterns are disabled."""
    monitoring_suite.ic_decay.get_decay_report.return_value = {
        "summary": {
            "patterns_disabled": 3,
            "patterns_on_alert": 2,
        },
    }

    status = monitoring_suite.get_full_status()
    assert status.overall_status == "critical"


# =============================================================================
# CHECK TRADING ALLOWED TESTS
# =============================================================================


def test_check_trading_allowed_returns_tuple(monitoring_suite):
    """Test check_trading_allowed returns (bool, list) tuple."""
    allowed, reasons = monitoring_suite.check_trading_allowed()

    assert isinstance(allowed, bool)
    assert isinstance(reasons, list)


def test_trading_allowed_when_healthy(monitoring_suite):
    """Test trading is allowed when all systems healthy."""
    allowed, reasons = monitoring_suite.check_trading_allowed()

    assert allowed is True
    assert len(reasons) == 0


def test_trading_blocked_on_full_defensive(monitoring_suite):
    """Test trading blocked when drawdown is FULL_DEFENSIVE."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="FULL_DEFENSIVE",
        drawdown_pct=-35.0,
    )

    allowed, reasons = monitoring_suite.check_trading_allowed()

    assert allowed is False
    assert len(reasons) > 0
    assert any("FULL_DEFENSIVE" in r for r in reasons)


def test_trading_blocked_on_risk_off(monitoring_suite):
    """Test trading blocked when drawdown is RISK_OFF."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="RISK_OFF",
        drawdown_pct=-25.0,
    )

    allowed, reasons = monitoring_suite.check_trading_allowed()

    assert allowed is False
    assert any("RISK_OFF" in r for r in reasons)


def test_trading_blocked_on_crisis_correlation(monitoring_suite):
    """Test trading blocked when correlation is crisis."""
    monitoring_suite.correlation.correlation_history = [
        MockCorrelationAnalysis(regime=MockCorrelationRegime.CRISIS_CORRELATION)
    ]

    allowed, reasons = monitoring_suite.check_trading_allowed()

    assert allowed is False
    assert any("CRISIS" in r for r in reasons)


def test_trading_blocked_on_critical_system_health(monitoring_suite):
    """Test trading blocked when system health is critical."""
    monitoring_suite.health.get_health_status.return_value = {
        "status": "critical",
    }

    allowed, reasons = monitoring_suite.check_trading_allowed()

    assert allowed is False
    assert any("CRITICAL" in r for r in reasons)


# =============================================================================
# REFLEXIVITY HALT TESTS
# =============================================================================


def test_should_halt_reflexivity_false_in_normal(monitoring_suite):
    """Test reflexivity not halted in normal conditions."""
    result = monitoring_suite.should_halt_reflexivity()
    assert result is False


def test_should_halt_reflexivity_true_when_flagged(monitoring_suite):
    """Test reflexivity halted when drawdown monitor says so."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="DEFENSIVE",
        should_halt_reflexivity=True,
    )

    result = monitoring_suite.should_halt_reflexivity()
    assert result is True


def test_should_halt_reflexivity_handles_no_status(monitoring_suite):
    """Test reflexivity check handles no status gracefully."""
    monitoring_suite.drawdown.get_current_status.return_value = None

    result = monitoring_suite.should_halt_reflexivity()
    assert result is False


# =============================================================================
# POSITION SIZE ADJUSTMENT TESTS
# =============================================================================


def test_get_position_size_adjustment_normal(monitoring_suite):
    """Test position size adjustment is 1.0 in normal conditions."""
    adjustment = monitoring_suite.get_position_size_adjustment()
    assert adjustment == 1.0


def test_get_position_size_adjustment_caution(monitoring_suite):
    """Test position size reduced in CAUTION tier."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="CAUTION",
    )

    adjustment = monitoring_suite.get_position_size_adjustment()
    assert adjustment < 1.0
    assert adjustment == 0.7  # 30% reduction per CLAUDE.md


def test_get_position_size_adjustment_defensive(monitoring_suite):
    """Test position size reduced in DEFENSIVE tier."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="DEFENSIVE",
    )

    adjustment = monitoring_suite.get_position_size_adjustment()
    assert adjustment == 0.5  # 50% reduction


def test_get_position_size_adjustment_risk_off(monitoring_suite):
    """Test position size reduced in RISK_OFF tier."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="RISK_OFF",
    )

    adjustment = monitoring_suite.get_position_size_adjustment()
    assert adjustment == 0.3  # 70% reduction


def test_get_position_size_adjustment_full_defensive(monitoring_suite):
    """Test position size reduced in FULL_DEFENSIVE tier."""
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="FULL_DEFENSIVE",
    )

    adjustment = monitoring_suite.get_position_size_adjustment()
    assert adjustment == 0.1  # 90% reduction


def test_get_position_size_adjustment_crisis_correlation(monitoring_suite):
    """Test position size further reduced in crisis correlation."""
    # CAUTION tier (0.7) * crisis correlation (0.5) = 0.35
    monitoring_suite.drawdown.get_current_status.return_value = MockDrawdownStatus(
        drawdown_tier="CAUTION",
    )
    monitoring_suite.correlation.correlation_history = [
        MockCorrelationAnalysis(regime=MockCorrelationRegime.CRISIS_CORRELATION)
    ]

    adjustment = monitoring_suite.get_position_size_adjustment()
    assert adjustment == 0.35


def test_get_position_size_adjustment_high_correlation(monitoring_suite):
    """Test position size reduced for high correlation."""
    # NORMAL (1.0) * high correlation (0.8) = 0.8
    monitoring_suite.correlation.correlation_history = [
        MockCorrelationAnalysis(regime=MockCorrelationRegime.HIGH_CORRELATION)
    ]

    adjustment = monitoring_suite.get_position_size_adjustment()
    assert adjustment == 0.8


# =============================================================================
# TO_DICT TESTS
# =============================================================================


def test_monitoring_status_to_dict(monitoring_suite):
    """Test MonitoringStatus.to_dict() serialization."""
    status = monitoring_suite.get_full_status()
    result = status.to_dict()

    assert isinstance(result, dict)
    assert "overall_status" in result
    assert "signal_health" in result
    assert "correlation_regime" in result
    assert "drawdown_tier" in result
    assert "timestamp" in result


# =============================================================================
# ERROR HANDLING TESTS
# =============================================================================


def test_get_full_status_handles_signal_error(monitoring_suite):
    """Test get_full_status handles signal monitor errors."""
    monitoring_suite.signal.check_signal_health.side_effect = Exception("Signal error")

    status = monitoring_suite.get_full_status()

    assert status.signal_health["status"] == "UNKNOWN"
    assert "error" in status.signal_health


def test_get_full_status_handles_correlation_error(monitoring_suite):
    """Test get_full_status handles correlation monitor errors."""
    monitoring_suite.correlation.correlation_history = None

    status = monitoring_suite.get_full_status()

    # Should still return status, just with unknown correlation
    assert status is not None
    assert status.correlation_regime == "unknown"


def test_get_full_status_handles_drawdown_error(monitoring_suite):
    """Test get_full_status handles drawdown monitor errors."""
    monitoring_suite.drawdown.get_current_status.side_effect = Exception("Drawdown error")

    status = monitoring_suite.get_full_status()

    # Should default to NORMAL when error occurs
    assert status.drawdown_tier in ("NORMAL", "UNKNOWN")


def test_get_full_status_handles_health_error(monitoring_suite):
    """Test get_full_status handles health monitor errors."""
    monitoring_suite.health.get_health_status.side_effect = Exception("Health error")

    status = monitoring_suite.get_full_status()

    assert "error" in status.system_health or status.system_health.get("status") == "unknown"


def test_get_full_status_handles_ic_decay_error(monitoring_suite):
    """Test get_full_status handles IC decay monitor errors."""
    monitoring_suite.ic_decay.get_decay_report.side_effect = Exception("IC decay error")

    status = monitoring_suite.get_full_status()

    assert "error" in status.ic_decay_status


# =============================================================================
# EMPTY HISTORY TESTS
# =============================================================================


def test_correlation_with_empty_history(monitoring_suite):
    """Test correlation handling with empty history."""
    monitoring_suite.correlation.correlation_history = []

    status = monitoring_suite.get_full_status()

    assert status.correlation_regime == "unknown"
    assert status.correlation_details is None


def test_drawdown_with_no_status(monitoring_suite):
    """Test drawdown handling with no status available."""
    monitoring_suite.drawdown.get_current_status.return_value = None

    status = monitoring_suite.get_full_status()

    assert status.drawdown_tier == "NORMAL"
    assert status.drawdown_pct == 0.0


# =============================================================================
# PRINT DASHBOARD TESTS
# =============================================================================


def test_print_dashboard_does_not_crash(monitoring_suite, capsys):
    """Test print_dashboard executes without errors."""
    monitoring_suite.print_dashboard()

    captured = capsys.readouterr()
    assert "OBLISKIQ MONITORING DASHBOARD" in captured.out
    assert "Overall Status" in captured.out


def test_print_dashboard_shows_all_sections(monitoring_suite, capsys):
    """Test print_dashboard shows all monitoring sections."""
    monitoring_suite.print_dashboard()

    captured = capsys.readouterr()
    assert "DRAWDOWN MONITOR" in captured.out
    assert "CORRELATION MONITOR" in captured.out
    assert "SIGNAL HEALTH" in captured.out
    assert "IC DECAY MONITOR" in captured.out
    assert "SYSTEM HEALTH" in captured.out
