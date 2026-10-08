"""
Tests for the Asymmetry Hunter convexity-sleeve risk rails (RiskEnforcer).
=========================================================================

The rails are HARD and enforced in code, not prose:
* per-dossier premium cap (2%)
* total convexity sleeve cap (5% across all open expressions)
* options never recommended < 30 DTE
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.utils.risk_enforcer import RiskEnforcer

pytestmark = pytest.mark.unit


def _dossier(ticker, premium, instrument="long_call", expiry_days=120, composite=80.0):
    return {
        "mapped_ticker": ticker,
        "asset": ticker,
        "max_premium_pct": premium,
        "instrument_type": instrument,
        "expiry_days": expiry_days,
        "composite_score": composite,
    }


def test_per_dossier_cap_rejects_over_2pct():
    enf = RiskEnforcer()
    ok, violations = enf.validate_convexity_dossier(_dossier("USO", 0.03), open_dossiers=[])
    assert ok is False
    rules = {v.rule for v in violations}
    assert "CONVEXITY_PER_DOSSIER_CAP" in rules
    assert all(v.action == "REJECT" for v in violations)


def test_within_per_dossier_cap_passes():
    enf = RiskEnforcer()
    ok, violations = enf.validate_convexity_dossier(_dossier("USO", 0.02), open_dossiers=[])
    assert ok is True
    assert violations == []


def test_option_min_dte_rejects_under_30():
    enf = RiskEnforcer()
    ok, violations = enf.validate_convexity_dossier(
        _dossier("SPY", 0.02, instrument="long_put", expiry_days=21), open_dossiers=[]
    )
    assert ok is False
    assert "OPTION_MIN_DTE" in {v.rule for v in violations}


def test_etf_expression_ignores_dte():
    enf = RiskEnforcer()
    ok, _ = enf.validate_convexity_dossier(
        _dossier("FXE", 0.02, instrument="etf", expiry_days=0), open_dossiers=[]
    )
    assert ok is True


def test_sleeve_total_caps_at_5pct():
    enf = RiskEnforcer()
    # Four dossiers at 2% each would be 8% > 5% sleeve.
    dossiers = [
        _dossier("USO", 0.02, composite=90),
        _dossier("GLD", 0.02, composite=80),
        _dossier("TLT", 0.02, composite=70),
        _dossier("SPY", 0.02, composite=60),
    ]
    approved, rejected = enf.validate_convexity_sleeve(dossiers)
    # Highest-conviction dossiers win the limited sleeve; total stays <= 5%.
    assert sum(d["max_premium_pct"] for d in approved) <= 0.05 + 1e-9
    assert len(rejected) >= 1
    # The top-conviction dossier (USO) is admitted; the lowest (SPY) is dropped.
    assert "USO" in {d["mapped_ticker"] for d in approved}
    assert "SPY" in {d["mapped_ticker"] for d in rejected}
    for r in rejected:
        assert "CONVEXITY_SLEEVE_TOTAL" in {v["rule"] for v in r["violations"]}


def test_sleeve_admits_all_when_within_cap():
    enf = RiskEnforcer()
    dossiers = [_dossier("USO", 0.01), _dossier("GLD", 0.01), _dossier("TLT", 0.01)]
    approved, rejected = enf.validate_convexity_sleeve(dossiers)
    assert len(approved) == 3
    assert rejected == []
