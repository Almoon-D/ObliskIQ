"""
Tests for the REAL CFTC COT history feed (v6.10.0).
====================================================

Fully OFFLINE — recorded CSV payloads + an in-memory warehouse, no live CFTC
calls (runs under ``pytest -m "not live"``). Covers:

  * ``parse_cot_annual_zip`` field extraction + the corrected 30Y contract code
  * warehouse percentile-from-history + the 52-week warm-up fallback
  * point-in-time (``as_of``) ranking (no look-ahead)
  * the contrarian direction mapping (extreme long -> short, extreme short -> long)
  * the symmetric Hunter crowding gate (BOTH tails fire)
"""

import io
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.data.warehouse import DataWarehouse
from modules.market.cftc_cot_analyzer import (
    CFTCCOTAnalyzer,
    PositioningExtreme,
    parse_cot_annual_zip,
)
from modules.analytics.asymmetry_hunter import (
    ASSET_SPECS,
    ComponentDirection,
    _crowding_component,
)
from modules.config.settings import AsymmetryConfig

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "cot_annual_sample.csv"


def _zip_csv(csv_text: str) -> bytes:
    """Wrap CSV text as a deacot-style ZIP (member annual.txt)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("annual.txt", csv_text)
    return buf.getvalue()


def _warehouse(tmp_path) -> DataWarehouse:
    return DataWarehouse(db_path=str(tmp_path / "cot.db"))


def _seed_weeks(w: DataWarehouse, code: str, name: str, nets, start=date(2024, 1, 2), oi=100_000):
    """Store one weekly row per value in ``nets`` (net spec contracts)."""
    rows = []
    for i, net in enumerate(nets):
        rows.append({
            "contract_code": code,
            "contract_name": name,
            "report_date": (start + timedelta(weeks=i)).isoformat(),
            "large_spec_net": int(net),
            "commercial_net": 0,
            "small_spec_net": 0,
            "open_interest": oi,
        })
    w.store_cot_snapshot(rows)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def test_parser_extracts_tracked_contracts_from_recorded_payload():
    raw = _zip_csv(_FIXTURE.read_text())
    df = parse_cot_annual_zip(raw)
    assert not df.empty
    names = set(df["contract_name"])
    # All six Hunter-mapped contracts are present in the recorded slice.
    assert {"Crude Oil", "Gold", "S&P 500", "Euro",
            "10-Year Treasury", "30-Year Treasury"} <= names
    # The corrected 30Y code (020601 "UST BOND", not the old 043607 "Ultra 10Y").
    bond = df[df["contract_name"] == "30-Year Treasury"].iloc[0]
    assert bond["contract_code"] == "020601"
    # Report dates parse to real dates; OI and position legs are ints.
    assert isinstance(bond["report_date"], date)
    assert int(bond["open_interest"]) > 0
    assert int(bond["large_spec_long"]) >= 0


def test_parser_ignores_untracked_and_bad_rows():
    csv_text = (
        "Market and Exchange Names,As of Date in Form YYYY-MM-DD,CFTC Contract Market Code,"
        "Open Interest (All),Noncommercial Positions-Long (All),Noncommercial Positions-Short (All),"
        "Commercial Positions-Long (All),Commercial Positions-Short (All),"
        "Nonreportable Positions-Long (All),Nonreportable Positions-Short (All)\n"
        "GOLD - COMMODITY EXCHANGE INC.,2024-03-05,088691,100000,60000,20000,30000,40000,10000,5000\n"
        "SOME RANDOM MARKET - NOWHERE,2024-03-05,999999,500,1,1,1,1,1,1\n"   # untracked -> skipped
        "GOLD - COMMODITY EXCHANGE INC.,2024-03-12,088691,,1,1,1,1,1,1\n"     # blank OI -> skipped
    )
    df = parse_cot_annual_zip(_zip_csv(csv_text))
    assert list(df["contract_code"].unique()) == ["088691"]
    assert len(df) == 1  # only the well-formed Gold row survives


# ---------------------------------------------------------------------------
# Warehouse percentile + warm-up
# ---------------------------------------------------------------------------
def test_percentile_warmup_below_52_weeks(tmp_path):
    w = _warehouse(tmp_path)
    _seed_weeks(w, "067651", "Crude Oil", [1000 + i for i in range(20)])
    pctl, n, status = w.cot_percentile_with_status("067651", 50.0, as_of=date(2024, 12, 31))
    assert pctl is None
    assert n == 20
    assert status == "COT percentile warming up: 20/52 weeks"


def test_percentile_real_rank_when_warm(tmp_path):
    w = _warehouse(tmp_path)
    # 60 increasing nets -> the latest is the max (-> 100th pctl), the smallest -> ~1.7.
    nets = [1000 + i * 100 for i in range(60)]
    _seed_weeks(w, "067651", "Crude Oil", nets)
    as_of = date(2024, 1, 2) + timedelta(weeks=80)
    hi_pct = (nets[-1] / 100_000) * 100
    lo_pct = (nets[0] / 100_000) * 100
    p_hi, n, status = w.cot_percentile_with_status("067651", hi_pct, as_of=as_of)
    p_lo, _, _ = w.cot_percentile_with_status("067651", lo_pct, as_of=as_of)
    assert n == 60 and "weekly observations" in status
    assert p_hi == 100.0
    assert p_lo < 5.0


def test_percentile_point_in_time_as_of(tmp_path):
    w = _warehouse(tmp_path)
    _seed_weeks(w, "088691", "Gold", [1000 + i * 10 for i in range(60)])
    # as_of after week 30 -> only ~31 rows visible -> still warming up (< 52).
    early = date(2024, 1, 2) + timedelta(weeks=30)
    pctl, n, status = w.cot_percentile_with_status("088691", 1.0, as_of=early)
    assert pctl is None and n == 31 and "warming up" in status


# ---------------------------------------------------------------------------
# Contrarian direction mapping via analyze_positioning
# ---------------------------------------------------------------------------
def _latest_df(code, name, net, oi=100_000, rdate="2025-03-05"):
    import pandas as pd
    return pd.DataFrame([{
        "contract_code": code, "contract_name": name, "report_date": date.fromisoformat(rdate),
        "commercial_long": 0, "commercial_short": 0,
        "large_spec_long": net, "large_spec_short": 0,
        "small_spec_long": 0, "small_spec_short": 0,
        "open_interest": oi,
    }])


def test_extreme_long_maps_to_contrarian_short(tmp_path):
    w = _warehouse(tmp_path)
    _seed_weeks(w, "067651", "Crude Oil", [1000 + i * 100 for i in range(60)])
    extreme_high = 1000 + 70 * 100  # above the whole history -> ~100th pctl
    w.store_cot_snapshot([{"contract_code": "067651", "contract_name": "Crude Oil",
                           "report_date": "2025-03-05", "large_spec_net": extreme_high,
                           "open_interest": 100_000}])
    df = _latest_df("067651", "Crude Oil", extreme_high)
    analysis = CFTCCOTAnalyzer().analyze_positioning(df, warehouse=w, as_of=date(2025, 3, 6))
    c = analysis.contracts[0]
    assert c.positioning_extreme == PositioningExtreme.EXTREME_LONG
    assert c.signal == "contrarian_short"
    assert c.large_spec_percentile >= 90


def test_extreme_short_maps_to_contrarian_long(tmp_path):
    w = _warehouse(tmp_path)
    _seed_weeks(w, "067651", "Crude Oil", [1000 + i * 100 for i in range(60)])
    extreme_low = 1000 - 5000  # below the whole history -> ~0th pctl
    w.store_cot_snapshot([{"contract_code": "067651", "contract_name": "Crude Oil",
                           "report_date": "2025-03-05", "large_spec_net": extreme_low,
                           "open_interest": 100_000}])
    df = _latest_df("067651", "Crude Oil", extreme_low)
    analysis = CFTCCOTAnalyzer().analyze_positioning(df, warehouse=w, as_of=date(2025, 3, 6))
    c = analysis.contracts[0]
    assert c.positioning_extreme == PositioningExtreme.EXTREME_SHORT
    assert c.signal == "contrarian_long"
    assert c.large_spec_percentile <= 10


def test_no_warehouse_degrades_to_neutral():
    df = _latest_df("067651", "Crude Oil", 40_000)
    analysis = CFTCCOTAnalyzer().analyze_positioning(df, warehouse=None)
    c = analysis.contracts[0]
    assert c.large_spec_percentile is None
    assert c.signal == "neutral"
    assert "no warehouse" in (c.percentile_status or "")


# ---------------------------------------------------------------------------
# Symmetric Hunter crowding gate (BOTH tails fire on real extremes)
# ---------------------------------------------------------------------------
def _spec(etf):
    return next(s for s in ASSET_SPECS if s.etf == etf)


def _cot_payload(contract, percentile, signal):
    return {"cot_positioning": {"status": "success", "contracts": [
        {"contract": contract, "percentile": percentile, "signal": signal}]}}


def test_hunter_gate_fires_high_tail_short():
    cfg = AsymmetryConfig()
    cs = _crowding_component(_spec("USO"), _cot_payload("Crude Oil", 95.0, "contrarian_short"), cfg)
    assert cs.available and cs.is_extreme
    assert cs.direction is ComponentDirection.SHORT


def test_hunter_gate_fires_low_tail_long():
    cfg = AsymmetryConfig()
    # Low percentile (crowded short) must now ALSO fire -> contrarian long.
    cs = _crowding_component(_spec("USO"), _cot_payload("Crude Oil", 5.0, "contrarian_long"), cfg)
    assert cs.available and cs.is_extreme
    assert cs.direction is ComponentDirection.LONG


def test_hunter_gate_silent_in_the_middle():
    cfg = AsymmetryConfig()
    cs = _crowding_component(_spec("USO"), _cot_payload("Crude Oil", 50.0, "neutral"), cfg)
    assert not cs.is_extreme
    assert cs.direction is ComponentDirection.NEUTRAL


def test_hunter_gate_warming_up_self_zeros():
    cfg = AsymmetryConfig()
    cs = _crowding_component(_spec("USO"), _cot_payload("Crude Oil", None, "neutral"), cfg)
    assert not cs.is_extreme  # percentile None (warming up) -> no vote
