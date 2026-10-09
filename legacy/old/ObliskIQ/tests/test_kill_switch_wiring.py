"""
Kill-switch wiring tests (v6.1.0).

The MonitoringSuite is now the SINGLE wired kill switch: a CRITICAL halt blocks
the run in ``run_pre_execution_checks`` and a snapshot is written to
``reports/health/`` every run. The kill-switch DECISION logic
(``check_trading_allowed``) is covered exhaustively by test_monitoring_suite.py;
here we cover the orchestrator-side wiring (the health snapshot).

The orchestrator pulls in the full Phase-1 data-source chain (feedparser, etc.),
so this skips cleanly where those optional deps are absent and runs in CI.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

orch_mod = pytest.importorskip("modules.execution.orchestrator")
ObliskOrchestrator = orch_mod.ObliskOrchestrator


def _fake_status(**over):
    base = dict(
        overall_status="critical",
        drawdown_tier="RISK_OFF",
        drawdown_pct=-22.0,
        correlation_regime="high",
        signal_health={"status": "WARNING"},
        ic_decay_status={"summary": {}},
        system_health={"status": "degraded"},
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_write_health_snapshot_records_halt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = MagicMock()
    fake.logger = logging.getLogger("test")
    fake.monitoring_suite.get_full_status.return_value = _fake_status()

    path = ObliskOrchestrator._write_health_snapshot(
        fake, False, ["Drawdown in RISK_OFF tier (-22.0%)"]
    )

    assert path is not None
    data = json.loads(Path(path).read_text())
    assert data["trading_allowed"] is False
    assert data["drawdown_tier"] == "RISK_OFF"
    assert data["halt_reasons"] == ["Drawdown in RISK_OFF tier (-22.0%)"]

    # Stable 'latest' pointer is refreshed every run.
    latest = Path("reports/health/latest.json")
    assert latest.exists()
    assert json.loads(latest.read_text())["trading_allowed"] is False


def test_write_health_snapshot_records_allow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = MagicMock()
    fake.logger = logging.getLogger("test")
    fake.monitoring_suite.get_full_status.return_value = _fake_status(
        overall_status="healthy",
        drawdown_tier="NORMAL",
        drawdown_pct=0.0,
        correlation_regime="low",
        signal_health={"status": "HEALTHY"},
        system_health={"status": "healthy"},
    )

    path = ObliskOrchestrator._write_health_snapshot(fake, True, [])
    data = json.loads(Path(path).read_text())
    assert data["trading_allowed"] is True
    assert data["drawdown_tier"] == "NORMAL"
    assert data["halt_reasons"] == []


def test_write_health_snapshot_never_raises(tmp_path, monkeypatch):
    """A snapshot failure must not block the run — it returns None, not raises."""
    monkeypatch.chdir(tmp_path)
    fake = MagicMock()
    fake.logger = logging.getLogger("test")
    fake.monitoring_suite.get_full_status.side_effect = RuntimeError("boom")
    assert ObliskOrchestrator._write_health_snapshot(fake, True, []) is None


# ---------------------------------------------------------------------------
# v8 demotion: the pattern-library revalidation GATE and the provenance check
# were REMOVED from run_pre_execution_checks. These tests assert they do NOT run
# on the live path and that the MonitoringSuite kill switch is still the gate.
# ---------------------------------------------------------------------------
def _healthy_preexec_mock():
    """A MagicMock orchestrator whose pre-execution inputs are all green."""
    fake = MagicMock()
    fake.logger = logging.getLogger("test")
    # Step 1: IC decay — empty list => no auto-disables.
    fake.ic_decay_monitor.check_all_patterns.return_value = []
    # Step 2: health — not critical.
    fake.health_monitor.get_health_status.return_value = {"overall_status": "healthy"}
    # Step 3: stop-loss sync.
    fake.stop_loss_monitor.sync_from_reflexivity_tracker.return_value = 0
    # Step 4: kill switch clear.
    fake.monitoring_suite.check_trading_allowed.return_value = (True, [])
    return fake


def test_pre_execution_does_not_run_pattern_gate():
    """The pattern revalidation gate + provenance check must NOT be invoked."""
    fake = _healthy_preexec_mock()

    result = ObliskOrchestrator.run_pre_execution_checks(fake)

    assert result is True
    # The demoted pattern subsystem entry points are never called on the live path.
    fake.validate_all_patterns_before_run.assert_not_called()
    fake._check_pattern_provenance.assert_not_called()
    # The kill switch IS the gate, and the IC-decay refresh (kill-switch feed) stays.
    fake.monitoring_suite.check_trading_allowed.assert_called_once()
    fake.ic_decay_monitor.check_all_patterns.assert_called_once()


def test_pre_execution_blocks_on_kill_switch_halt():
    """A CRITICAL kill-switch halt still aborts the run (gate preserved)."""
    fake = _healthy_preexec_mock()
    fake.monitoring_suite.check_trading_allowed.return_value = (
        False,
        ["Drawdown in RISK_OFF tier (-22.0%)"],
    )

    result = ObliskOrchestrator.run_pre_execution_checks(fake)

    assert result is False
    fake.validate_all_patterns_before_run.assert_not_called()


def test_pre_execution_blocks_on_critical_health():
    """Critical system health still aborts the run (non-pattern gate preserved)."""
    fake = _healthy_preexec_mock()
    fake.health_monitor.get_health_status.return_value = {"overall_status": "critical"}

    result = ObliskOrchestrator.run_pre_execution_checks(fake)

    assert result is False
    fake._check_pattern_provenance.assert_not_called()
