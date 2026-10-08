"""
Tests for the AI Judgment Scoreboard (v5.40.0)
==============================================

Covers:
1. Schema creation
2. Logging Phase 2 / Phase 4 proposals (fields, direction mapping, idempotency)
3. Retrospective forward-return scoring (vs SPY) + idempotency
4. Conviction → realized-return IC
5. Regime-call accuracy
6. The statistical verdict (see tests/test_scoreboard_statistics.py for the cohort statuses)
7. Report rendering + monthly gating
8. Stable prompt-version hash

All price data is supplied by a deterministic fake fetcher — no network.
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.storage.database_manager import DatabaseManager
from modules.analytics.ai_scoreboard import AIScoreboard, HORIZONS
from modules.config.settings import DEFAULT_GEMINI_MODEL

pytestmark = pytest.mark.unit


# ============================================================================
# Fixtures & helpers
# ============================================================================
@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name
    db = DatabaseManager(db_path=temp_path, pool_size=2)
    yield db
    db.close_pool()
    Path(temp_path).unlink(missing_ok=True)
    Path(temp_path + "-wal").unlink(missing_ok=True)
    Path(temp_path + "-shm").unlink(missing_ok=True)


class FakePriceFetcher:
    """Deterministic price source. ``funcs`` maps ticker -> callable(datetime)->price."""

    def __init__(self, funcs):
        self.funcs = funcs

    def fetch_price_on_date(self, ticker, when):
        fn = self.funcs.get(ticker)
        return fn(when) if fn else None


def make_fetcher(proposal_date, winners, losers, spy_start=100.0, spy_end=100.0):
    """Build a fetcher: winners +10%, losers -10%, SPY moves spy_start->spy_end."""
    pdate = proposal_date.date()

    def const_then(start, end):
        return lambda when: start if when.date() == pdate else end

    funcs = {"SPY": const_then(spy_start, spy_end)}
    for tk in winners:
        funcs[tk] = const_then(100.0, 110.0)
    for tk in losers:
        funcs[tk] = const_then(100.0, 90.0)
    return FakePriceFetcher(funcs)


def backdate_proposals(db, days):
    """Move every proposal's proposal_date into the past so horizons mature."""
    new_dt = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE ai_proposals SET proposal_date = ?, scored_horizons = '[]'",
            (new_dt,),
        )
        conn.commit()
    return datetime.fromisoformat(new_dt)


def make_scoreboard(temp_db, fetcher=None, reports_dir=None):
    return AIScoreboard(
        temp_db,
        price_fetcher=fetcher,
        reports_dir=reports_dir or Path(tempfile.mkdtemp()),
    )


# ============================================================================
# Schema
# ============================================================================
def test_tables_created(temp_db):
    make_scoreboard(temp_db)
    with temp_db.get_connection() as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("ai_proposals",),
        )
        assert cursor.fetchone() is not None
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_proposals)").fetchall()}
    for h in HORIZONS:
        assert f"pos_ret_{h}" in cols
        assert f"excess_{h}" in cols
    assert {
        "conviction",
        "regime_call",
        "reflexivity_intensity",
        "prompt_hash",
        "model_name",
        "screener_selected",
    } <= cols


# ============================================================================
# Logging
# ============================================================================
def test_log_phase2_proposals(temp_db):
    sb = make_scoreboard(temp_db)
    analysis = {
        "ai_proposed_tickers": ["AAPL", "TLT", "XLE"],
        "suggested_tickers": ["AAPL", "TLT"],
        "market_regime": "bull",
        "regime_confidence": 7.0,
        "reflexivity_intensity": 0.4,
        "ticker_rationale": {"AAPL": "momentum", "TLT": "rate pivot"},
    }
    n = sb.log_phase2_proposals(
        analysis,
        run_id="RUN1",
        model_name=DEFAULT_GEMINI_MODEL,
        validated_tickers=["AAPL", "TLT"],
    )
    assert n == 3
    rows = sb._query_rows(phase="phase2")
    assert len(rows) == 3
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["AAPL"]["direction"] == "LONG"
    assert by_ticker["AAPL"]["conviction"] == 7.0
    assert by_ticker["AAPL"]["regime_call"] == "bull"
    assert by_ticker["AAPL"]["reflexivity_intensity"] == 0.4
    assert by_ticker["AAPL"]["screener_selected"] == 1
    assert by_ticker["XLE"]["screener_selected"] == 0  # proposed but not validated
    assert by_ticker["AAPL"]["model_name"] == DEFAULT_GEMINI_MODEL
    assert len(by_ticker["AAPL"]["prompt_hash"]) == 16


def test_log_phase4_proposals_direction_mapping(temp_db):
    sb = make_scoreboard(temp_db)
    decisions = {
        "decisions": [
            {
                "ticker": "AAPL",
                "action": "BUY",
                "conviction": 8,
                "position_size_pct": 7.5,
                "reasoning": "strong",
            },
            {"ticker": "TLT", "action": "SELL", "conviction": 6, "position_size_pct": 5.0},
            {"ticker": "GLD", "action": "HOLD", "conviction": 4},
        ]
    }
    n = sb.log_phase4_proposals(
        decisions,
        regime_call="bear",
        reflexivity_intensity=0.7,
        run_id="RUN2",
        model_name=DEFAULT_GEMINI_MODEL,
    )
    assert n == 3
    rows = {r["ticker"]: r for r in sb._query_rows(phase="phase4")}
    assert rows["AAPL"]["direction"] == "LONG"
    assert rows["AAPL"]["position_size_pct"] == 7.5
    assert rows["TLT"]["direction"] == "SHORT"
    assert rows["GLD"]["direction"] == "HOLD"
    assert rows["AAPL"]["regime_call"] == "bear"
    assert rows["AAPL"]["reflexivity_intensity"] == 0.7


def test_logging_is_idempotent(temp_db):
    sb = make_scoreboard(temp_db)
    analysis = {"ai_proposed_tickers": ["AAPL"], "market_regime": "bull", "regime_confidence": 5.0}
    assert sb.log_phase2_proposals(analysis, run_id="RUN1") == 1
    # Same run + ticker => same proposal_id => ignored on re-log.
    assert sb.log_phase2_proposals(analysis, run_id="RUN1") == 0
    assert len(sb._query_rows(phase="phase2")) == 1


def test_logging_never_raises_on_garbage(temp_db):
    sb = make_scoreboard(temp_db)
    assert sb.log_phase2_proposals({}) == 0
    assert sb.log_phase4_proposals({}) == 0
    assert sb.log_phase4_proposals({"decisions": [{}]}) == 0  # no ticker


# ============================================================================
# Asymmetry Hunter source logging (v6.4.0)
# ============================================================================
def test_source_column_migration(temp_db):
    make_scoreboard(temp_db)
    with temp_db.get_connection() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_proposals)").fetchall()}
    assert "source" in cols


def test_default_source_is_masterprompt(temp_db):
    sb = make_scoreboard(temp_db)
    sb.log_phase2_proposals(
        {"ai_proposed_tickers": ["AAPL"], "market_regime": "bull", "regime_confidence": 5.0},
        run_id="RUN1",
    )
    row = sb._query_rows(phase="phase2")[0]
    assert row["source"] == "masterprompt"


def test_log_asymmetry_dossiers_sets_source(temp_db):
    sb = make_scoreboard(temp_db)
    dossiers = [
        {
            "mapped_ticker": "USO",
            "direction": "SHORT",
            "composite_score": 82.0,
            "consensus_belief": "crowd long crude",
            "max_premium_pct": 0.02,
            "regime_call": "early_warning",
        },
        {
            "mapped_ticker": "GLD",
            "direction": "LONG",
            "composite_score": 75.0,
            "consensus_belief": "haven bid building",
            "max_premium_pct": 0.02,
        },
    ]
    n = sb.log_asymmetry_dossiers(dossiers, weights_hash="abc123def4567890", run_id="RUNX")
    assert n == 2
    rows = {r["ticker"]: r for r in sb._query_rows(phase="asymmetry")}
    assert rows["USO"]["source"] == "asymmetry_hunter"
    assert rows["USO"]["direction"] == "SHORT"
    assert rows["USO"]["prompt_hash"] == "abc123def4567890"
    assert rows["USO"]["conviction"] == pytest.approx(8.2)  # composite/10
    # Graded the same way: source filter isolates the Soros layer.
    assert len(sb._query_rows(source="asymmetry_hunter")) == 2


def test_log_asymmetry_dossiers_idempotent_and_safe(temp_db):
    sb = make_scoreboard(temp_db)
    dossiers = [
        {
            "mapped_ticker": "USO",
            "direction": "SHORT",
            "composite_score": 80.0,
            "max_premium_pct": 0.02,
        }
    ]
    assert sb.log_asymmetry_dossiers(dossiers, weights_hash="h", run_id="R1") == 1
    assert sb.log_asymmetry_dossiers(dossiers, weights_hash="h", run_id="R1") == 0  # idempotent
    # Garbage / no-direction dossiers are skipped, never raise.
    assert (
        sb.log_asymmetry_dossiers([{"mapped_ticker": "", "direction": "SHORT"}], weights_hash="h")
        == 0
    )
    assert sb.log_asymmetry_dossiers([], weights_hash="h") == 0


# ============================================================================
# Scoring
# ============================================================================
def test_forward_return_scoring_vs_spy(temp_db):
    sb = make_scoreboard(temp_db)
    sb.log_phase4_proposals(
        {
            "decisions": [
                {"ticker": "WIN", "action": "BUY", "conviction": 9},
                {"ticker": "LOSE", "action": "BUY", "conviction": 8},
            ]
        },
        regime_call="bull",
        run_id="R",
    )
    pdate = backdate_proposals(temp_db, days=100)
    sb._price_fetcher = make_fetcher(pdate, winners=["WIN"], losers=["LOSE"])

    result = sb.score_due_proposals()
    assert result["horizons_scored"] == 2 * len(HORIZONS)

    rows = {r["ticker"]: r for r in sb._query_rows(phase="phase4")}
    # SPY flat => excess == position return.
    assert rows["WIN"]["pos_ret_8w"] == pytest.approx(10.0, abs=0.01)
    assert rows["WIN"]["excess_8w"] == pytest.approx(10.0, abs=0.01)
    assert rows["LOSE"]["pos_ret_8w"] == pytest.approx(-10.0, abs=0.01)
    assert json.loads(rows["WIN"]["scored_horizons"]) == list(HORIZONS.keys())


def test_short_direction_scoring(temp_db):
    sb = make_scoreboard(temp_db)
    sb.log_phase4_proposals(
        {"decisions": [{"ticker": "DROP", "action": "SELL", "conviction": 7}]},
        run_id="R",
    )
    pdate = backdate_proposals(temp_db, days=100)
    # DROP falls 10% -> a SHORT profits +10%.
    sb._price_fetcher = make_fetcher(pdate, winners=[], losers=["DROP"])
    sb.score_due_proposals()
    row = sb._query_rows(phase="phase4")[0]
    assert row["pos_ret_8w"] == pytest.approx(10.0, abs=0.01)


def test_scoring_is_idempotent(temp_db):
    sb = make_scoreboard(temp_db)
    sb.log_phase4_proposals(
        {"decisions": [{"ticker": "WIN", "action": "BUY", "conviction": 9}]}, run_id="R"
    )
    pdate = backdate_proposals(temp_db, days=100)
    sb._price_fetcher = make_fetcher(pdate, winners=["WIN"], losers=[])
    first = sb.score_due_proposals()
    assert first["horizons_scored"] == len(HORIZONS)
    second = sb.score_due_proposals()
    assert second["horizons_scored"] == 0  # nothing new to score


def test_immature_proposals_not_scored(temp_db):
    sb = make_scoreboard(temp_db)
    sb.log_phase4_proposals(
        {"decisions": [{"ticker": "WIN", "action": "BUY", "conviction": 9}]}, run_id="R"
    )
    pdate = backdate_proposals(temp_db, days=20)  # only 2w has matured
    sb._price_fetcher = make_fetcher(pdate, winners=["WIN"], losers=[])
    sb.score_due_proposals()
    row = sb._query_rows(phase="phase4")[0]
    assert row["pos_ret_2w"] is not None
    assert row["pos_ret_8w"] is None
    assert json.loads(row["scored_horizons"]) == ["2w"]


# ============================================================================
# Regime accuracy
# ============================================================================
def test_regime_accuracy_correct_and_incorrect(temp_db):
    sb = make_scoreboard(temp_db)
    sb.log_phase4_proposals(
        {
            "decisions": [
                {"ticker": "A", "action": "BUY", "conviction": 8},
                {"ticker": "B", "action": "BUY", "conviction": 8},
            ]
        },
        regime_call="bull",
        run_id="R",
    )
    pdate = backdate_proposals(temp_db, days=100)
    # SPY rallies +5% over the window => realized regime "bull" => AI bull == correct.
    sb._price_fetcher = make_fetcher(
        pdate, winners=["A", "B"], losers=[], spy_start=100.0, spy_end=105.0
    )
    sb.score_due_proposals()
    acc = sb.compute_regime_accuracy(sb._query_rows())
    assert acc["n"] == 2
    assert acc["accuracy"] == 1.0

    # Now a bear call while SPY rallied => incorrect.
    with temp_db.get_connection() as conn:
        conn.execute(
            "UPDATE ai_proposals SET regime_call='bear', realized_regime=NULL,"
            " regime_correct=NULL, scored_horizons='[]'"
        )
        conn.commit()
    sb.score_due_proposals()
    acc2 = sb.compute_regime_accuracy(sb._query_rows())
    assert acc2["accuracy"] == 0.0


# ============================================================================
# Verdict
# ============================================================================
def _seed_phase4(sb, n, winners, conviction=8):
    decisions = []
    for i in range(n):
        tk = f"T{i}"
        decisions.append({"ticker": tk, "action": "BUY", "conviction": conviction})
    sb.log_phase4_proposals({"decisions": decisions}, regime_call="bull", run_id="BIG")
    return [d["ticker"] for d in decisions[:winners]], [d["ticker"] for d in decisions[winners:]]


def test_verdict_insufficient_data(temp_db):
    sb = make_scoreboard(temp_db)
    win, lose = _seed_phase4(sb, 10, winners=5)
    pdate = backdate_proposals(temp_db, days=100)
    sb._price_fetcher = make_fetcher(pdate, winners=win, losers=lose)
    sb.score_due_proposals()
    verdict = sb.compute_scoreboard()["verdict"]
    assert verdict["status"] == "insufficient_data"
    assert "INSUFFICIENT DATA" in verdict["headline"]


def test_verdict_refuses_to_conclude_from_a_single_cohort(temp_db):
    """64 proposals logged on ONE date are one weekly cohort: far too little to call skill or no skill.

    (v5.40 ran a proportion test on exactly this and declared "decoration" at 50% / "adds value" at 100%.
    The pre-registered cohort rules need >= 12 weekly cohorts; see tests/test_scoreboard_statistics.py
    for the verdict statuses themselves.)
    """
    sb = make_scoreboard(temp_db)
    win, lose = _seed_phase4(sb, 64, winners=64)  # even a perfect 64/64 is one cohort
    pdate = backdate_proposals(temp_db, days=100)
    sb._price_fetcher = make_fetcher(pdate, winners=win, losers=lose)
    sb.score_due_proposals()
    verdict = sb.compute_scoreboard()["verdict"]
    assert verdict["status"] == "insufficient_data"
    assert verdict["n"] == 64 and verdict["n_weeks"] == 1
    assert "INSUFFICIENT DATA" in verdict["headline"]


def test_conviction_ic_is_computed(temp_db):
    sb = make_scoreboard(temp_db)
    # High-conviction names win, low-conviction names lose => positive IC.
    decisions = []
    for i in range(20):
        decisions.append({"ticker": f"W{i}", "action": "BUY", "conviction": 9})
    for i in range(20):
        decisions.append({"ticker": f"L{i}", "action": "BUY", "conviction": 2})
    sb.log_phase4_proposals({"decisions": decisions}, regime_call="bull", run_id="IC")
    pdate = backdate_proposals(temp_db, days=100)
    sb._price_fetcher = make_fetcher(
        pdate, winners=[f"W{i}" for i in range(20)], losers=[f"L{i}" for i in range(20)]
    )
    sb.score_due_proposals()
    metrics = sb.compute_metrics(sb._query_rows(phase="phase4"), "8w")
    assert metrics["n"] == 40
    assert metrics["ic"] is not None
    assert metrics["ic"] > 0.5  # conviction strongly predicts realized return here


# ============================================================================
# Reporting
# ============================================================================
def test_report_rendering_and_monthly_gating(temp_db):
    reports_dir = Path(tempfile.mkdtemp())
    sb = make_scoreboard(temp_db, reports_dir=reports_dir)
    win, lose = _seed_phase4(sb, 64, winners=40)
    pdate = backdate_proposals(temp_db, days=100)
    sb._price_fetcher = make_fetcher(pdate, winners=win, losers=lose)
    sb.score_due_proposals()

    md = sb.render_report_markdown()
    assert "AI Value-Added Report" in md
    assert "## 🏁 Verdicts" in md
    assert "Per-prompt-version and per-model breakdown" in md
    assert "Regime-call accuracy" in md

    # First write is due; the next is gated until 28 days pass.
    path = sb.generate_value_added_report()
    assert path is not None
    assert Path(path).exists()
    assert sb.is_monthly_report_due() is False
    assert sb.generate_value_added_report() is None
    # force overrides the gate.
    assert sb.generate_value_added_report(force=True) is not None


def test_prompt_hash_stable(temp_db):
    sb = make_scoreboard(temp_db)
    h1 = sb.prompt_hash
    h2 = sb.prompt_hash
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 16


# ============================================================================
# Asymmetry Hunter (Soros layer) — separate verdict + grading clock (Prompt 3)
# ============================================================================
def _seed_asymmetry(sb, n, winners, conviction=80.0):
    dossiers = [
        {
            "mapped_ticker": f"A{i}",
            "direction": "LONG",
            "composite_score": conviction,
            "max_premium_pct": 0.01,
            "regime_call": "bull",
        }
        for i in range(n)
    ]
    sb.log_asymmetry_dossiers(dossiers, weights_hash="hash0000hash0000", run_id="ASYM")
    return [f"A{i}" for i in range(winners)], [f"A{i}" for i in range(winners, n)]


def test_asymmetry_scoreboard_is_isolated_from_masterprompt(temp_db):
    sb = make_scoreboard(temp_db)
    # MasterPrompt rows must NOT leak into the Soros verdict.
    sb.log_phase4_proposals(
        {"decisions": [{"ticker": "MP", "action": "BUY", "conviction": 8}]}, run_id="MP"
    )
    _seed_asymmetry(sb, 4, winners=4)
    asym = sb.compute_asymmetry_scoreboard()
    assert asym["total_proposals"] == 4  # only asymmetry_hunter rows
    overall = sb.compute_scoreboard()
    assert overall["total_proposals"] == 5  # MP + 4 asym


def test_asymmetry_verdict_uses_soros_wording(temp_db):
    sb = make_scoreboard(temp_db)
    win, lose = _seed_asymmetry(sb, 64, winners=64)
    pdate = backdate_proposals(temp_db, days=100)
    sb._price_fetcher = make_fetcher(pdate, winners=win, losers=lose)
    sb.score_due_proposals()
    asym = sb.compute_asymmetry_scoreboard()
    assert asym["verdict"]["status"] == "insufficient_data"   # one cohort is not a verdict
    assert "Asymmetry Hunter" in asym["verdict"]["headline"]
    # MasterPrompt wording must NOT appear in the Soros verdict.
    assert "MasterPrompt" not in asym["verdict"]["headline"]


def test_grading_clock_fields(temp_db):
    sb = make_scoreboard(temp_db)
    win, lose = _seed_asymmetry(sb, 10, winners=5)
    pdate = backdate_proposals(temp_db, days=100)
    sb._price_fetcher = make_fetcher(pdate, winners=win, losers=lose)
    sb.score_due_proposals()
    clock = sb.asymmetry_grading_clock()
    assert clock["dossiers_logged"] == 10
    assert clock["dossiers_scored"] == 10  # independent dossiers matured at the 4w primary horizon
    assert clock["weeks_matured"] == 1
    assert clock["required"] == 30 and clock["required_weeks"] == 26
    # Live-component count comes from the readiness layer (3 deterministic at cold start).
    assert clock["live_components"] is not None
    assert clock["min_components"] == 3


def test_report_includes_clock_and_soros_section(temp_db):
    reports_dir = Path(tempfile.mkdtemp())
    sb = make_scoreboard(temp_db, reports_dir=reports_dir)
    _seed_asymmetry(sb, 3, winners=2)
    md = sb.render_report_markdown()
    assert "Asymmetry Hunter — Grading Clock" in md
    assert "Asymmetry Hunter (Soros layer)" in md
    assert "dossiers logged" in md
