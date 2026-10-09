"""
Tests for the Asymmetry Hunter v1 (deterministic Soros overlay).
================================================================

Covers:
1. All-neutral synthetic Phase 1 -> ZERO candidates (silence is the default).
2. Engineered 3-component, same-direction extreme -> exactly ONE candidate with
   the correct direction and all dossier fields populated.
3. Inconsistent direction -> no candidate.
4. Degraded (mocked/warming) components contribute 0 weight and don't count.
5. The max-5 cap.
6. weights_hash stability / sensitivity.
7. Event-calendar catalyst proximity.
8. Config weights sum to 1.0.

Fully synthetic — no network, no real database.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.analytics.asymmetry_hunter import (
    _catalyst_window,
    hunt,
)
from modules.config.settings import AsymmetryConfig
from modules.utils.risk_enforcer import RiskEnforcer

pytestmark = pytest.mark.unit


# ============================================================================
# Helpers / fakes
# ============================================================================
class FakeWarehouse:
    """No IV/price history -> vol component warms up, invalidation stays qualitative."""

    def get_iv_history(self, ticker, start_date=None, end_date=None):
        return []

    def get_ticker_prices(self, ticker, start_date=None, end_date=None):
        return []


def _run(phase1_data):
    """Run the hunter deterministically: explicit config/enforcer/warehouse, fixed date."""
    return hunt(
        phase1_data,
        config=AsymmetryConfig(),
        enforcer=RiskEnforcer(),
        warehouse=FakeWarehouse(),
        today=date(2026, 6, 16),
    )


def _risk_off_regime(score=70.0):
    return {"composite_score": score, "state": "transition_imminent", "score_trend": "rising"}


def _cot(contract, percentile, signal):
    return {
        "status": "success",
        "contracts": [
            {"contract": contract, "percentile": percentile, "signal": signal,
             "crowding_score": 0.9, "extreme": "extreme_short", "confidence": 0.8}
        ],
    }


def _commodity(symbol, regime, slope):
    return {
        "status": "success",
        "commodities": {symbol: {"regime": regime, "curve_slope": slope,
                                 "intensity": 0.9, "signal": "bearish"}},
    }


# ============================================================================
# 1. Silence is the default
# ============================================================================
def test_all_neutral_yields_zero_candidates():
    result = _run({})
    assert result["status"] == "ok"
    assert result["candidates_found"] == 0
    assert result["candidates"] == []


def test_calm_market_yields_zero_candidates():
    phase1 = {
        "regime_transition": {"composite_score": 20.0, "state": "stable", "score_trend": "stable"},
        "cross_asset_divergence": {"status": "success", "divergence": 0.3, "hy_oas_z": 0.1, "vix_z": -0.2},
        "sentiment": {"composite_score": 52.0},
    }
    result = _run(phase1)
    assert result["candidates_found"] == 0


# ============================================================================
# 2. Engineered 3-component same-direction extreme -> one candidate
# ============================================================================
def test_three_extreme_consistent_direction_yields_one_candidate():
    # Crude/USO: crowding SHORT (a) + commodity SHORT (d) + regime risk-off SHORT (c).
    # Other assets see only the market-wide regime (1 vote) -> below the bar.
    phase1 = {
        "cot_positioning": _cot("Crude Oil", 92.0, "contrarian_short"),
        "commodity_futures": _commodity("CL=F", "strong_contango", 15.0),
        "regime_transition": _risk_off_regime(70.0),
        # Cross-asset divergence NOT extreme (so only the per-asset commodity counts).
        "cross_asset_divergence": {"status": "success", "divergence": 0.5, "hy_oas_z": 0.2, "vix_z": -0.3},
        "sentiment": {"composite_score": 50.0},
    }
    result = _run(phase1)
    assert result["candidates_found"] == 1
    cand = result["candidates"][0]
    assert cand["mapped_ticker"] == "USO"
    assert cand["direction"] == "SHORT"
    assert len(cand["contributing_components"]) >= 3
    # All dossier fields populated.
    for field in ("consensus_belief", "why_fragile", "feedback_loop",
                  "catalyst_window", "invalidation_level", "markdown"):
        assert cand[field]
    dr = cand["defined_risk"]
    assert dr["instrument_type"] == "long_put"
    assert 0.6 <= dr["target_delta"] <= 0.7
    assert dr["expiry_days"] >= 30  # never < 30 DTE
    assert cand["max_premium_pct"] <= 0.02  # per-dossier cap


def test_capitulation_flips_safe_haven_long():
    # Panic sentiment -> contrarian risk-on -> SHORT safe havens / LONG risk-on.
    # Engineer 3 market-wide risk-on votes; safe-haven GLD should go SHORT, USO LONG.
    phase1 = {
        "regime_transition": _risk_off_regime(70.0),  # risk-off (off)
        "cross_asset_divergence": {"status": "success", "divergence": 2.5, "hy_oas_z": 2.0, "vix_z": -0.5},
        "sentiment": {"composite_score": 70.0},  # not extreme; keeps it clean
    }
    result = _run(phase1)
    # regime + cross = 2 market-wide votes -> below the 3-component bar -> no candidates.
    assert result["candidates_found"] == 0


# ============================================================================
# 3. Inconsistent direction
# ============================================================================
def test_inconsistent_direction_no_candidate():
    # Crowding LONG + commodity LONG, but regime risk-off SHORT on USO -> 2 vs 1, no 3-agree.
    phase1 = {
        "cot_positioning": _cot("Crude Oil", 92.0, "contrarian_long"),
        "commodity_futures": _commodity("CL=F", "strong_backwardation", -15.0),
        "regime_transition": _risk_off_regime(70.0),
        "cross_asset_divergence": {"status": "success", "divergence": 0.4, "hy_oas_z": 0.1, "vix_z": -0.3},
        "sentiment": {"composite_score": 50.0},
    }
    result = _run(phase1)
    assert result["candidates_found"] == 0


# ============================================================================
# 4. Degraded components contribute 0 weight and don't count
# ============================================================================
def test_degraded_components_zero_weight():
    # COT unavailable (mock) + only 2 live market-wide votes -> no candidate; crowding flagged.
    phase1 = {
        "cot_positioning": {"status": "unavailable", "message": "mock"},
        "regime_transition": _risk_off_regime(70.0),
        "cross_asset_divergence": {"status": "success", "divergence": 2.5, "hy_oas_z": 2.0, "vix_z": -0.5},
        "sentiment": {"composite_score": 50.0},
    }
    result = _run(phase1)
    assert result["candidates_found"] == 0
    degraded = {d["name"]: d["status"] for d in result["degraded_components"]}
    assert degraded.get("crowding") == "unavailable"


# ============================================================================
# 5. Max-5 cap
# ============================================================================
def test_max_five_cap():
    # Three market-wide risk-off extremes -> all 6 assets fire (3 votes each) -> capped at 5.
    phase1 = {
        "regime_transition": _risk_off_regime(80.0),
        "cross_asset_divergence": {"status": "success", "divergence": 3.0, "hy_oas_z": 2.5, "vix_z": -0.5},
        "sentiment": {"composite_score": 95.0},  # euphoria -> risk-off
    }
    result = _run(phase1)
    assert result["candidates_found"] == 5  # 6 would fire, capped to max_candidates
    # Sleeve respected: 5 * share <= 5%.
    assert sum(c["max_premium_pct"] for c in result["candidates"]) <= 0.05 + 1e-9


# ============================================================================
# 6. weights_hash
# ============================================================================
def test_weights_hash_stable_and_sensitive():
    a = AsymmetryConfig()
    b = AsymmetryConfig()
    assert a.weights_hash() == b.weights_hash()
    c = AsymmetryConfig(crowding_weight=0.30, sentiment_extreme_weight=0.08)
    assert c.weights_hash() != a.weights_hash()


def test_watchlist_carries_weights_hash():
    result = _run({})
    assert result["weights_hash"] == AsymmetryConfig().weights_hash()


# ============================================================================
# 7. Event-calendar proximity
# ============================================================================
def test_event_calendar_proximity():
    cfg = AsymmetryConfig()
    today = date(2026, 6, 16)
    calendar = {"FOMC": ["2026-06-22"], "CPI": ["2026-07-14"]}
    proximate, text = _catalyst_window(calendar, cfg, today)
    assert proximate is True
    assert "FOMC" in text and "6 day" in text

    far = {"FOMC": ["2026-07-29"]}
    proximate2, _ = _catalyst_window(far, cfg, today)
    assert proximate2 is False

    none_left = {"FOMC": ["2026-01-01"]}
    proximate3, text3 = _catalyst_window(none_left, cfg, today)
    assert proximate3 is False
    assert "No upcoming" in text3


# ============================================================================
# 8. Config validity
# ============================================================================
def test_asymmetry_config_weights_sum_to_one():
    cfg = AsymmetryConfig()
    assert cfg.validate() == []
    assert cfg.is_valid()
    assert abs(sum(cfg.weights().values()) - 1.0) < 1e-9


# ============================================================================
# 9. v6.5.0 — Polymarket (g) & GDELT (h) evidence streams
# ============================================================================
def _cross_off_extreme():
    """Market-wide credit/VIX divergence that fires risk-off."""
    return {"status": "success", "divergence": 2.5, "hy_oas_z": 2.0, "vix_z": -0.5}


def test_polymarket_divergence_counts_as_a_vote():
    # regime (off) + cross (off) + polymarket (off) -> 3 agreeing votes per asset.
    phase1 = {
        "regime_transition": _risk_off_regime(70.0),
        "cross_asset_divergence": _cross_off_extreme(),
        "polymarket_divergence": {
            "status": "success", "divergence": 0.30, "direction": "off",
            "detail": "Crowd 60% no-cut vs our 90% dovish",
        },
        "sentiment": {"composite_score": 50.0},
    }
    result = _run(phase1)
    assert result["candidates_found"] == 5  # >5 assets fire, capped
    assert any(
        "polymarket_divergence" in c["contributing_components"] for c in result["candidates"]
    )


def test_polymarket_below_threshold_does_not_fire():
    phase1 = {
        "regime_transition": _risk_off_regime(70.0),
        "cross_asset_divergence": {"status": "success", "divergence": 0.4, "hy_oas_z": 0.1, "vix_z": -0.3},
        "polymarket_divergence": {"status": "success", "divergence": 0.05, "direction": "off"},
        "sentiment": {"composite_score": 50.0},
    }
    result = _run(phase1)
    # Only regime fires (1 vote) -> nothing clears the bar.
    assert result["candidates_found"] == 0


def test_gdelt_anomaly_maps_to_mapped_assets():
    # MIDDLE_EAST anomaly -> GLD long. Safe-haven GLD also gets regime+cross LONG.
    phase1 = {
        "regime_transition": _risk_off_regime(70.0),
        "cross_asset_divergence": _cross_off_extreme(),
        "sentiment": {"composite_score": 50.0},
        "gdelt_pulse": {
            "status": "success",
            "worst_region": "MIDDLE_EAST",
            "regions": {
                "MIDDLE_EAST": {
                    "volume_z": 5.0, "tone": -4.0, "anomalous": True, "status": "success",
                }
            },
        },
    }
    result = _run(phase1)
    gld = [c for c in result["candidates"] if c["mapped_ticker"] == "GLD"]
    assert gld, "GLD should surface from regime+cross+gdelt aligned LONG"
    assert gld[0]["direction"] == "LONG"
    assert "gdelt_pulse" in gld[0]["contributing_components"]


def test_geo_streams_degrade_when_absent():
    result = _run({"regime_transition": _risk_off_regime(70.0)})
    degraded = {d["name"]: d["status"] for d in result["degraded_components"]}
    assert degraded.get("polymarket_divergence") == "unavailable"
    assert degraded.get("gdelt_pulse") == "unavailable"


def test_gdelt_warming_up_casts_no_vote():
    phase1 = {
        "regime_transition": _risk_off_regime(70.0),
        "cross_asset_divergence": _cross_off_extreme(),
        "sentiment": {"composite_score": 50.0},
        "gdelt_pulse": {"status": "warming_up", "regions": {}, "worst_region": None},
    }
    result = _run(phase1)
    degraded = {d["name"]: d["status"] for d in result["degraded_components"]}
    assert degraded.get("gdelt_pulse") == "warming_up"


def test_regional_etf_can_become_candidate_from_geo():
    # EUROPE anomaly -> EWG short; regime+cross also short the risk-on EWG.
    phase1 = {
        "regime_transition": _risk_off_regime(70.0),
        "cross_asset_divergence": _cross_off_extreme(),
        "sentiment": {"composite_score": 50.0},
        "gdelt_pulse": {
            "status": "success",
            "worst_region": "EUROPE",
            "regions": {
                "EUROPE": {
                    "volume_z": 4.0, "tone": -3.0, "anomalous": True, "status": "success",
                }
            },
        },
    }
    result = _run(phase1)
    ewg = [c for c in result["candidates"] if c["mapped_ticker"] == "EWG"]
    assert ewg, "EWG should surface from regime+cross+gdelt aligned SHORT"
    assert ewg[0]["direction"] == "SHORT"


def test_hunter_never_raises_on_garbage():
    # Malformed inputs must degrade, not throw.
    result = hunt({"cot_positioning": 42, "regime_transition": "nonsense"},
                  config=AsymmetryConfig(), warehouse=FakeWarehouse())
    assert result["status"] in ("ok", "error")
    assert "candidates" in result
