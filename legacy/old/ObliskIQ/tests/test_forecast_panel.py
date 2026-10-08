"""Weekly forecast panel (v6.16.0): universe, features, contract, and the weekly run through the REAL
AnalystRouter with scripted providers (no network)."""

import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from llm_fakes import ScriptedProvider
from modules.analysis.analyst_router import AnalystRouter
from modules.analysis.providers import ProviderCallError
from modules.analytics.ai_scoreboard import AIScoreboard
from modules.analytics.forecast_panel import (
    FeatureLoad,
    build_panel_prompt,
    build_panel_universe,
    compute_panel_features,
    parse_panel_response,
    run_weekly_forecast_panel,
    universe_hash,
)
from modules.storage.database_manager import DatabaseManager

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)  # Wednesday, ISO week 2026-W02


@pytest.fixture(autouse=True)
def no_real_pacing(monkeypatch):
    monkeypatch.setattr("modules.analytics.forecast_panel.BATCH_PAUSE_S", 0.0)


@pytest.fixture
def board():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    db = DatabaseManager(db_path=path, pool_size=2)
    yield AIScoreboard(db, reports_dir=Path(tempfile.mkdtemp()))
    db.close_pool()
    for suffix in ("", "-wal", "-shm"):
        Path(path + suffix).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
#: (size, hash) of the pre-registered universe measured when the panel was introduced (v6.16.0).
PINNED = (188, "de02ea2b4858ef0b")


def test_universe_is_a_fixed_sorted_liquid_global_set():
    universe = build_panel_universe()
    assert list(universe) == sorted(set(universe))
    assert 150 <= len(universe) <= 250
    assert "ACWI" not in universe                      # the yardstick is not a forecast name
    assert {"SPY", "AAPL", "EWJ", "TM", "XLK"} <= set(universe)
    assert not any("." in t and t != "BRK-B" for t in universe)  # USD listings only


def test_universe_hash_is_pinned_so_a_change_is_a_conscious_decision():
    """Pre-registration: if this fails, the panel universe changed — bump the hash deliberately and note it in
    docs/AI_SCOREBOARD.md (cohorts before and after are separate populations)."""
    universe = build_panel_universe()
    assert universe_hash(universe) == universe_hash(tuple(reversed(universe)))   # order-independent
    assert universe_hash(universe) != universe_hash(universe[:-1])               # membership-sensitive
    assert (len(universe), universe_hash(universe)) == PINNED



# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------
def _growth_frame(days=300, daily=0.001, names=("AAA",)):
    index = pd.bdate_range(end="2026-01-06", periods=days)
    return pd.DataFrame({n: [100.0 * (1.0 + daily) ** i for i in range(days)] for n in names}, index=index)


def test_features_hand_computed_for_a_constant_growth_series():
    rows, latest = compute_panel_features(_growth_frame())
    f = rows["AAA"]
    assert latest == date(2026, 1, 6)
    assert f["ret_1m"] == pytest.approx((1.001 ** 21 - 1) * 100, abs=0.06)       # 21 bars back
    assert f["ret_3m"] == pytest.approx((1.001 ** 63 - 1) * 100, abs=0.06)
    assert f["mom_12_1"] == pytest.approx((1.001 ** 231 - 1) * 100, abs=0.06)    # 12m ago -> 1m ago
    assert f["vol_60d"] == pytest.approx(0.0, abs=0.01)                          # constant daily return
    assert f["off_52w_high"] == 0.0                                              # last bar is the high
    assert f["entry_price"] == pytest.approx(100.0 * 1.001 ** 299)


def test_short_history_name_is_dropped_and_mid_history_has_no_baseline():
    index = pd.bdate_range(end="2026-01-06", periods=300)
    frame = pd.DataFrame({"LONG": [100.0 + i for i in range(300)]}, index=index)
    frame["NEW"] = [None] * 250 + [100.0 + i for i in range(50)]      # 50 bars: below the minimum
    frame["MID"] = [None] * 180 + [100.0 + i for i in range(120)]     # 120 bars: forecastable, no 12-1
    rows, _ = compute_panel_features(frame)
    assert "NEW" not in rows
    assert rows["MID"]["mom_12_1"] is None and rows["MID"]["ret_3m"] is not None
    assert rows["LONG"]["mom_12_1"] is not None


# ---------------------------------------------------------------------------
# Prompt + contract
# ---------------------------------------------------------------------------
def _feat(ticker="AAA"):
    return {ticker: {"entry_price": 10.0, "ret_1m": 1.0, "ret_3m": 2.0, "mom_12_1": 3.0, "vol_60d": 20.0,
                     "off_52w_high": -4.0}}


def test_prompt_lists_every_name_states_the_base_rate_and_leaks_no_outcomes():
    batch = {**_feat("AAA"), **_feat("BBB")}
    prompt = build_panel_prompt(batch, as_of=date(2026, 1, 6), context={"vix": 18.2})
    assert '"ticker":"AAA"' in prompt and '"ticker":"BBB"' in prompt
    assert "ACWI" in prompt and "2026-01-06" in prompt and '"vix":18.2' in prompt
    assert "45-50%" in prompt                           # base-rate anchor against overconfidence
    assert "entry_price" not in prompt                  # the model sees trailing statistics only


def _resp(tickers, p=0.55, ex=0.4):
    return json.dumps({"schema_version": "1.0", "forecasts": [
        {"ticker": t, "p_outperform_4w": p, "expected_excess_4w_pct": ex} for t in tickers]})


def test_parser_accepts_valid_clamps_probabilities_and_ignores_unknown_and_duplicates():
    text = json.dumps({"forecasts": [
        {"ticker": "aaa", "p_outperform_4w": 0.999, "expected_excess_4w_pct": 3.0},
        {"ticker": "AAA", "p_outperform_4w": 0.1, "expected_excess_4w_pct": -1.0},   # duplicate: first wins
        {"ticker": "ZZZ", "p_outperform_4w": 0.5, "expected_excess_4w_pct": 0.0},    # not asked for
        {"ticker": "BBB", "p_outperform_4w": 0.0, "expected_excess_4w_pct": 0.0},
    ]})
    out = parse_panel_response(text, ["AAA", "BBB"])
    assert set(out) == {"AAA", "BBB"}
    assert out["AAA"] == {"p_outperform": 0.98, "expected_excess": 3.0}               # clamped to [0.02, 0.98]
    assert out["BBB"]["p_outperform"] == 0.02


@pytest.mark.parametrize("text, message", [
    ("not json", "not valid JSON"),
    ("[]", "forecasts"),
    (json.dumps({"forecasts": "x"}), "forecasts"),
    (_resp(["A"]), "only 1 of 5"),                                                   # coverage below 80%
    (json.dumps({"forecasts": [{"ticker": t, "p_outperform_4w": 1.7, "expected_excess_4w_pct": 0}
                               for t in "ABCDE"]}), "only 0 of 5"),                    # p outside [0,1]
    (json.dumps({"forecasts": [{"ticker": t, "p_outperform_4w": 0.5, "expected_excess_4w_pct": 400}
                               for t in "ABCDE"]}), "only 0 of 5"),                    # absurd excess
    (json.dumps({"forecasts": [{"ticker": t, "p_outperform_4w": True, "expected_excess_4w_pct": 0}
                               for t in "ABCDE"]}), "only 0 of 5"),                    # bool is not a number
])
def test_parser_rejects_bad_documents(text, message):
    with pytest.raises(ValueError, match=message):
        parse_panel_response(text, list("ABCDE"))


# ---------------------------------------------------------------------------
# The weekly run, through the real router
# ---------------------------------------------------------------------------
def test_batches_are_paced_to_respect_shared_free_tier_limits(board, monkeypatch):
    naps = []
    gemini = ScriptedProvider([_resp(b) for b in _batches()], name="gemini")
    run_weekly_forecast_panel(board, _router(gemini), now=NOW, feature_loader=lambda u, n: _features_for(u),
                              pause_s=4.0, sleep=naps.append)
    assert naps == [4.0] * (len(_batches()) - 1)          # between batches, not before the first


def _features_for(universe):
    rows = {t: {"entry_price": 100.0, "ret_1m": 1.0, "ret_3m": 2.0, "mom_12_1": (i % 7) - 3.0, "vol_60d": 20.0,
                "off_52w_high": -5.0} for i, t in enumerate(universe)}
    return FeatureLoad("ok", asof=date(2026, 1, 6), rows=rows, benchmark_price=150.0)


def _batches(size=40):
    names = sorted(build_panel_universe())
    return [names[i:i + size] for i in range(0, len(names), size)]


def _router(*providers):
    return AnalystRouter(order=[p.name for p in providers], providers={p.name: p for p in providers})


def test_weekly_run_logs_one_cohort_with_baseline_prices_and_the_answering_model(board):
    gemini = ScriptedProvider([_resp(b) for b in _batches()], name="gemini", model="flash-test")
    out = run_weekly_forecast_panel(board, _router(gemini), now=NOW, feature_loader=lambda u, n: _features_for(u),
                                    context={"vix": 17.0})
    universe = build_panel_universe()
    assert out["status"] == "logged" and out["n_forecast"] == len(universe) and out["failed_batches"] == 0
    rows = board._query_rows(source="forecast_panel")
    assert len(rows) == len(universe)
    r = {x["ticker"]: x for x in rows}["SPY"]
    assert r["iso_week"] == "2026-W02" and r["benchmark_ticker"] == "ACWI"
    assert r["win_probability"] == 0.55 and r["expected_excess"] == 0.4
    assert r["ticker_price_at_proposal"] == 100.0 and r["spy_price_at_proposal"] == 150.0
    assert r["model_name"] == "gemini:flash-test" and r["universe_hash"] == universe_hash(universe)
    assert r["baseline_signal"] is not None
    assert gemini.calls == len(_batches())


def test_second_run_in_the_same_week_makes_no_ai_calls(board):
    gemini = ScriptedProvider([_resp(b) for b in _batches()], name="gemini")
    loader = lambda u, n: _features_for(u)  # noqa: E731
    run_weekly_forecast_panel(board, _router(gemini), now=NOW, feature_loader=loader)
    calls = gemini.calls
    again = run_weekly_forecast_panel(board, _router(gemini), now=NOW, feature_loader=loader)
    assert again["status"] == "skipped" and "already logged" in again["reason"]
    assert gemini.calls == calls
    # next ISO week is a new cohort
    nxt = run_weekly_forecast_panel(board, _router(ScriptedProvider([_resp(b) for b in _batches()], name="gemini")),
                                    now=datetime(2026, 1, 14, 12, tzinfo=timezone.utc), feature_loader=loader)
    assert nxt["status"] == "logged"


def test_a_malformed_batch_is_retried_once_with_the_error(board):
    batches = _batches()
    script = ["{broken", _resp(batches[0])] + [_resp(b) for b in batches[1:]]
    gemini = ScriptedProvider(script, name="gemini")
    out = run_weekly_forecast_panel(board, _router(gemini), now=NOW, feature_loader=lambda u, n: _features_for(u))
    assert out["status"] == "logged" and out["failed_batches"] == 0
    assert "PREVIOUS RESPONSE REJECTED" in gemini.prompts[1]


def test_failover_to_the_next_provider_and_each_row_names_who_answered(board):
    batches = _batches()
    failing = ScriptedProvider([ProviderCallError("gemini", "429", retryable=False, status=429)] * 2, name="gemini")
    backup = ScriptedProvider([_resp(b) for b in batches], name="openrouter", model="nemotron-test")
    out = run_weekly_forecast_panel(board, _router(failing, backup), now=NOW,
                                    feature_loader=lambda u, n: _features_for(u))
    assert out["status"] == "logged"
    models = {r["model_name"] for r in board._query_rows(source="forecast_panel")}
    assert models == {"openrouter:nemotron-test"}


def test_exhausted_chain_stops_spending_calls_and_logs_nothing_so_the_week_can_retry(board):
    dead = ScriptedProvider([ProviderCallError("gemini", "quota", retryable=False, status=429)] * 10, name="gemini")
    out = run_weekly_forecast_panel(board, _router(dead), now=NOW, feature_loader=lambda u, n: _features_for(u))
    assert out["status"] == "incomplete" and out["n_forecast"] == 0
    assert dead.calls <= 2                                   # stopped after 2 consecutive failed batches
    assert board.panel_cohort_exists("2026-W02") is False    # a later run this week may still succeed


def test_stale_or_missing_prices_skip_the_week_without_any_ai_call(board):
    gemini = ScriptedProvider([], name="gemini")
    stale = run_weekly_forecast_panel(board, _router(gemini), now=NOW,
                                      feature_loader=lambda u, n: FeatureLoad("stale", "latest price bar is 9 days old"))
    down = run_weekly_forecast_panel(board, _router(gemini), now=NOW,
                                     feature_loader=lambda u, n: FeatureLoad("unavailable", "429"))
    assert stale["status"] == down["status"] == "skipped"
    assert "stale" in stale["reason"] and "unavailable" in down["reason"]
    assert gemini.calls == 0


def test_an_analyst_without_run_contract_is_skipped_not_crashed(board):
    out = run_weekly_forecast_panel(board, object(), now=NOW, feature_loader=lambda u, n: _features_for(u))
    assert out["status"] == "skipped" and "run_contract" in out["reason"]
