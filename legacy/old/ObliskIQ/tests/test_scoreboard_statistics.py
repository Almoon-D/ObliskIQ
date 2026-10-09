"""
Scoreboard statistics (v6.16.0): independent observations, regional USD benchmark, the pre-registered
cohort verdict, the weekly forecast panel and the calibration rule that gates sizing.

All prices come from a deterministic fake fetcher — no network.
"""

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules.analytics.ai_scoreboard import (
    MIN_OBS_FOR_VERDICT,
    MIN_WEEKS_FOR_VERDICT,
    AIScoreboard,
    dedupe_observations,
    iso_week_key,
)
from modules.storage.database_manager import DatabaseManager

pytestmark = pytest.mark.unit

BASE = date(2026, 1, 5)  # a Monday (ISO week 2)


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    manager = DatabaseManager(db_path=path, pool_size=2)
    yield manager
    manager.close_pool()
    for suffix in ("", "-wal", "-shm"):
        Path(path + suffix).unlink(missing_ok=True)


class CohortFetcher:
    """Entry price 100 on a ticker's proposal date, 100*(1+ret%) on any other date.

    Unknown symbols (benchmarks) are flat at 100 unless ``flat`` overrides them.
    """

    def __init__(self, spec, fx=None):
        self.spec = spec  # ticker -> (proposal date, return %)
        self.fx = fx or {}  # symbol -> (quote at proposal date, quote later)
        self.calls = 0
        self.proposal_dates = {tk: d for tk, (d, _) in spec.items()}

    def fetch_price_on_date(self, ticker, when):
        self.calls += 1
        if ticker in self.fx:
            start, end = self.fx[ticker]
            return start if when.date() in self.proposal_dates.values() else end
        if ticker in self.spec:
            pdate, ret = self.spec[ticker]
            return 100.0 if when.date() == pdate else 100.0 * (1.0 + ret / 100.0)
        return 100.0


def week_date(week_index):
    return BASE + timedelta(days=7 * week_index)


def stamp(d):
    return datetime(d.year, d.month, d.day, 13, 0, tzinfo=timezone.utc).isoformat()


def seed(sb, ticker, d, *, phase="phase4", source="masterprompt", direction="LONG",
         conviction=7.0, win_probability=None, expected_excess=None, baseline_signal=None,
         run=None, **extra):
    row = sb._build_row(
        proposal_id=f"{run or stamp(d)}_{phase}_{ticker}", run_id=run or stamp(d), phase=phase,
        proposal_date=stamp(d), ticker=ticker, direction=direction,
        action="BUY" if direction == "LONG" else "SELL", conviction=conviction,
        regime_call="bull", model_name="gemini:test", prompt_hash="h", source=source,
        screener_selected=1, win_probability=win_probability, expected_excess=expected_excess,
        baseline_signal=baseline_signal, **extra,
    )
    sb._insert_rows([row])


def make_sb(db, fetcher=None):
    return AIScoreboard(db, price_fetcher=fetcher, reports_dir=Path(tempfile.mkdtemp()))


def end_of(weeks):
    return datetime.combine(week_date(weeks), datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=90)


def seed_weekly_phase4(sb, returns_by_week, per_week=3):
    """``returns_by_week[w]`` is a list of per-ticker returns (%) for week w's picks."""
    spec = {}
    for w, rets in enumerate(returns_by_week):
        d = week_date(w)
        for i, ret in enumerate(rets[:per_week]):
            tk = f"W{w}T{i}"
            seed(sb, tk, d)
            spec[tk] = (d, ret)
    return spec


# ---------------------------------------------------------------------------
# Independent observations
# ---------------------------------------------------------------------------
def test_iso_week_key_and_column_populated(db):
    sb = make_sb(db)
    seed(sb, "AAA", BASE)
    assert iso_week_key(stamp(BASE)) == "2026-W02"
    assert sb._query_rows()[0]["iso_week"] == "2026-W02"


def test_dedupe_collapses_repeats_within_a_week_but_not_across_weeks_directions_or_layers(db):
    sb = make_sb(db)
    # same name, same direction, three runs in ONE ISO week -> one bet
    for offset in (0, 1, 2):
        seed(sb, "AAA", BASE + timedelta(days=offset), run=f"run{offset}")
    seed(sb, "AAA", BASE + timedelta(days=7), run="next-week")            # new week
    seed(sb, "AAA", BASE, direction="SHORT", run="short")                  # other direction
    seed(sb, "AAA", BASE, phase="asymmetry", source="asymmetry_hunter", run="soros")  # other layer
    rows = sb._query_rows()
    assert len(rows) == 6
    kept = dedupe_observations(rows)
    assert len(kept) == 4
    earliest = [r for r in kept if r["ticker"] == "AAA" and r["iso_week"] == "2026-W02"
                and r["direction"] == "LONG" and r["source"] == "masterprompt"]
    assert earliest[0]["proposal_date"].startswith("2026-01-05")  # the first sighting wins


def test_scoreboard_total_is_raw_but_metrics_use_independent_rows(db):
    sb = make_sb(db)
    for offset in range(3):
        seed(sb, "AAA", BASE + timedelta(days=offset), run=f"r{offset}")
    fetcher = CohortFetcher({"AAA": (BASE, 5.0)})
    sb._price_fetcher = fetcher
    # all three rows share the BASE proposal date for pricing purposes
    fetcher.proposal_dates["AAA"] = BASE
    snap = sb.compute_scoreboard(phase="phase4")
    assert snap["total_proposals"] == 3
    assert snap["independent_observations"] == 1


# ---------------------------------------------------------------------------
# Regional USD benchmark
# ---------------------------------------------------------------------------
def test_foreign_listing_gets_regional_benchmark_and_fx_leg_at_log_time(db):
    sb = make_sb(db)
    seed(sb, "7203.T", BASE)
    seed(sb, "AAPL", BASE)
    rows = {r["ticker"]: r for r in sb._query_rows()}
    assert rows["7203.T"]["benchmark_ticker"] == "EWJ"
    assert rows["7203.T"]["fx_symbol"] == "USDJPY=X"
    assert rows["AAPL"]["benchmark_ticker"] == "SPY"
    assert rows["AAPL"]["fx_symbol"] is None


def test_foreign_return_is_measured_in_usd_not_local_currency(db):
    sb = make_sb(db)
    seed(sb, "7203.T", BASE)
    # Local price +10%, but JPY per USD goes 100 -> 110 (yen weakens ~9.09%): in USD it is flat.
    fetcher = CohortFetcher({"7203.T": (BASE, 10.0)}, fx={"USDJPY=X": (100.0, 110.0)})
    sb._price_fetcher = fetcher
    sb.score_due_proposals(now=end_of(1))
    row = sb._query_rows()[0]
    assert row["pos_ret_4w"] == pytest.approx(0.0, abs=0.01)
    assert row["excess_4w"] == pytest.approx(0.0, abs=0.01)  # EWJ flat in the fake
    assert row["fx_price_at_proposal"] == pytest.approx(0.01)


def test_missing_fx_quote_leaves_horizon_unscored_until_stale(db):
    sb = make_sb(db)
    seed(sb, "7203.T", BASE)

    class NoFx(CohortFetcher):
        def fetch_price_on_date(self, ticker, when):
            return None if ticker == "USDJPY=X" else super().fetch_price_on_date(ticker, when)

    sb._price_fetcher = NoFx({"7203.T": (BASE, 10.0)})
    # matured 4w but not stale -> stays unscored (retry later); never a fake USD return
    sb.score_due_proposals(now=datetime.combine(BASE, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=31))
    assert sb._query_rows()[0]["pos_ret_4w"] is None


def test_batch_prefetch_replaces_per_ticker_calls_and_falls_back_for_misses(db):
    sb = make_sb(db)
    for i in range(5):
        seed(sb, f"B{i}", BASE)
    spec = {f"B{i}": (BASE, 2.0) for i in range(5)}

    class Batcher(CohortFetcher):
        batch_calls = 0

        def fetch_prices_on_date_batch(self, symbols, when):
            Batcher.batch_calls += 1
            out = {}
            for s in symbols:
                if s == "B4":
                    continue  # the batch misses one name -> per-ticker fallback must cover it
                out[s] = 100.0 if when.date() == BASE else (102.0 if s.startswith("B") else 100.0)
            return out

    fetcher = Batcher(spec)
    sb._price_fetcher = fetcher
    sb.score_due_proposals(now=end_of(1))
    rows = sb._query_rows()
    assert all(r["pos_ret_4w"] == pytest.approx(2.0, abs=0.01) for r in rows)
    assert Batcher.batch_calls >= 1
    assert fetcher.calls < 5 * 3 * 4  # far fewer than one request per (ticker, date, horizon)


# ---------------------------------------------------------------------------
# Pre-registered cohort verdict
# ---------------------------------------------------------------------------
def _verdict_for(sb, weekly_returns, per_week=3):
    spec = seed_weekly_phase4(sb, weekly_returns, per_week=per_week)
    sb._price_fetcher = CohortFetcher(spec)
    sb.score_due_proposals(now=end_of(len(weekly_returns)))
    return sb.compute_scoreboard(phase="phase4")["verdict"]


def test_verdict_needs_weeks_and_observations_not_just_rows(db):
    sb = make_sb(db)
    # 60 observations but only 5 weekly cohorts -> still insufficient
    v = _verdict_for(sb, [[5.0] * 12] * 5, per_week=12)
    assert v["status"] == "insufficient_data"
    assert v["n_weeks"] == 5 and v["n"] == 60


def test_verdict_adds_value_when_weekly_excess_is_reliably_positive(db):
    sb = make_sb(db)
    weeks = [[5.0 + 0.1 * ((w + i) % 3) for i in range(3)] for w in range(MIN_WEEKS_FOR_VERDICT + 4)]
    v = _verdict_for(sb, weeks)
    assert v["status"] == "adds_value"
    assert v["ci95_pp"][0] > 0
    assert v["n_weeks"] == MIN_WEEKS_FOR_VERDICT + 4
    assert v["n"] >= MIN_OBS_FOR_VERDICT


def test_verdict_is_anti_predictive_when_reliably_negative(db):
    sb = make_sb(db)
    weeks = [[-5.0 - 0.1 * ((w + i) % 3) for i in range(3)] for w in range(MIN_WEEKS_FOR_VERDICT + 4)]
    assert _verdict_for(sb, weeks)["status"] == "anti_predictive"


def test_verdict_decoration_only_when_interval_rules_out_a_meaningful_edge(db):
    sb = make_sb(db)
    # tight, centred on zero: the interval is far below +0.5pp
    weeks = [[0.1, -0.1, 0.0] if w % 2 == 0 else [-0.1, 0.1, 0.0] for w in range(MIN_WEEKS_FOR_VERDICT + 4)]
    v = _verdict_for(sb, weeks)
    assert v["status"] == "decoration"
    assert v["ci95_pp"][1] < 0.5
    assert v["indistinguishable"] is True


def test_noisy_zero_mean_result_is_inconclusive_not_decoration(db):
    sb = make_sb(db)
    pattern = [8.0, -8.0, 6.0, -6.0, 9.0, -9.0, 5.0, -5.0, 7.0, -7.0, 8.0, -8.0, 6.0, -6.0, 9.0, -9.0] * 2
    weeks = [[r, r, r] for r in pattern]
    v = _verdict_for(sb, weeks)
    assert v["status"] == "inconclusive"
    assert v["ci95_pp"][0] < 0 < v["ci95_pp"][1]
    assert "cannot rule out" in v["headline"]


def test_persistent_regime_swings_do_not_pass_as_a_significant_edge(db):
    """Slow-moving weekly means (overlapping 4w windows) fool an iid test; Newey-West must not be fooled."""
    from modules.analytics.stat_utils import newey_west_mean

    blocks = ([1.9] * 8 + [-0.7] * 8) * 2          # mean +0.6pp, 8-week regimes: 32 weekly cohorts
    sb = make_sb(db)
    verdict = _verdict_for(sb, [[r, r, r] for r in blocks])
    assert newey_west_mean(blocks, 0)["ci95"][0] > 0          # premise: an iid test WOULD call this an edge
    assert verdict["status"] == "inconclusive"
    assert verdict["ci95_pp"][0] < 0 < verdict["ci95_pp"][1]


def test_repeating_the_same_name_does_not_manufacture_significance(db):
    """Fifty sightings of one winning name in one week are ONE observation."""
    sb = make_sb(db)
    for k in range(50):
        seed(sb, "AAA", BASE + timedelta(hours=k % 2), run=f"dup{k}", conviction=7.0)
    sb._price_fetcher = CohortFetcher({"AAA": (BASE, 20.0)})
    sb.score_due_proposals(now=end_of(1))
    snap = sb.compute_scoreboard(phase="phase4")
    assert snap["independent_observations"] == 1
    assert snap["verdict"]["status"] == "insufficient_data"


def test_layers_are_judged_separately(db):
    sb = make_sb(db)
    layers = sb.compute_layer_scoreboards()
    assert set(layers) == {"phase2", "phase4", "asymmetry_hunter"}
    assert "Phase 4" in layers["phase4"]["verdict"]["headline"]
    assert "Asymmetry Hunter" in layers["asymmetry_hunter"]["verdict"]["headline"]


# ---------------------------------------------------------------------------
# Weekly forecast panel
# ---------------------------------------------------------------------------
def _seed_panel(sb, weeks, names, *, skilled, base_signal_skilled=False):
    """``skilled``: the AI's expected_excess equals the realised excess ranking."""
    spec = {}
    for w in range(weeks):
        d = week_date(w)
        for i in range(names):
            tk = f"P{w}N{i}"
            realised = (i - names / 2.0) / 5.0                  # -4 .. +4 spread, cross-sectionally
            signal = realised if skilled else ((i * 7919) % names - names / 2.0) / 5.0
            seed(sb, tk, d, phase="panel", source="forecast_panel",
                 win_probability=0.5 + 0.1 * (1 if realised > 0 else -1) if skilled else 0.5,
                 expected_excess=signal,
                 baseline_signal=(realised if base_signal_skilled else -realised) * 2.0)
            spec[tk] = (d, realised)
    return spec


def test_panel_rows_are_graded_only_at_the_reduced_horizons(db):
    sb = make_sb(db)
    spec = _seed_panel(sb, 1, 40, skilled=True)
    sb._price_fetcher = CohortFetcher(spec)
    sb.score_due_proposals(now=end_of(1))
    row = sb._query_rows(source="forecast_panel")[0]
    assert row["pos_ret_4w"] is not None and row["pos_ret_12w"] is not None
    assert row["pos_ret_2w"] is None and row["pos_ret_8w"] is None


def test_panel_cohort_exists_is_idempotent_guard(db):
    sb = make_sb(db)
    assert sb.panel_cohort_exists("2026-W02") is False
    sb.log_forecast_panel(
        [{"ticker": "SPY", "p_outperform": 0.55, "expected_excess": 0.3, "baseline_signal": 8.0,
          "entry_price": 500.0}],
        universe_hash="u", as_of=datetime(2026, 1, 5, 13, tzinfo=timezone.utc), run_id="r1",
    )
    assert sb.panel_cohort_exists("2026-W02") is True
    row = sb._query_rows(source="forecast_panel")[0]
    assert row["benchmark_ticker"] == "ACWI" and row["fx_symbol"] is None
    assert row["win_probability"] == 0.55 and row["ticker_price_at_proposal"] == 500.0
    assert row["universe_hash"] == "u"


def test_panel_with_real_skill_is_detected(db):
    sb = make_sb(db)
    weeks = MIN_WEEKS_FOR_VERDICT + 2
    spec = _seed_panel(sb, weeks, 40, skilled=True)
    sb._price_fetcher = CohortFetcher(spec)
    sb.score_due_proposals(now=end_of(weeks))
    panel = sb.compute_panel_scoreboard()
    assert panel["n_weeks"] == weeks
    assert panel["mean_ic"] > 0.9
    assert panel["verdict"]["status"] == "adds_value"
    assert panel["calibration"]["skill"] is not None and panel["calibration"]["skill"] == pytest.approx(0.36, abs=0.02)  # 1 - 0.16/0.25
    assert panel["spread_pp"] > 0


def test_panel_without_skill_is_not_called_skilled(db):
    sb = make_sb(db)
    weeks = MIN_WEEKS_FOR_VERDICT + 2
    spec = _seed_panel(sb, weeks, 40, skilled=False)
    sb._price_fetcher = CohortFetcher(spec)
    sb.score_due_proposals(now=end_of(weeks))
    panel = sb.compute_panel_scoreboard()
    assert panel["verdict"]["status"] != "adds_value"


def test_panel_reports_incremental_skill_over_the_momentum_baseline(db):
    sb = make_sb(db)
    weeks = MIN_WEEKS_FOR_VERDICT + 2
    spec = _seed_panel(sb, weeks, 40, skilled=True, base_signal_skilled=False)
    sb._price_fetcher = CohortFetcher(spec)
    sb.score_due_proposals(now=end_of(weeks))
    panel = sb.compute_panel_scoreboard()
    assert panel["baseline_mean_ic"] < 0           # the baseline signal here anti-predicts
    assert panel["incremental_ic"] > 1.5           # AI IC (~1) minus baseline IC (~-1)


def test_panel_insufficient_until_enough_weekly_cohorts(db):
    sb = make_sb(db)
    spec = _seed_panel(sb, 3, 40, skilled=True)
    sb._price_fetcher = CohortFetcher(spec)
    sb.score_due_proposals(now=end_of(3))
    assert sb.compute_panel_scoreboard()["verdict"]["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# Calibration gate for sizing
# ---------------------------------------------------------------------------
def test_calibration_defaults_to_shrinking_stated_probabilities(db):
    state = make_sb(db).calibration_state()
    assert state["shrink"] == 0.5 and state["n_graded"] == 0


def _graded_panel(db, *, informative, weeks=8, names=15):
    sb = make_sb(db)
    spec = {}
    for w in range(weeks):
        d = week_date(w)
        for i in range(names):
            tk = f"C{w}N{i}"
            up = (i % 2 == 0)
            # informative: p tracks the realised direction; uninformative: p is confidently wrong
            p = (0.8 if up else 0.2) if informative else (0.2 if up else 0.8)
            seed(sb, tk, d, phase="panel", source="forecast_panel", win_probability=p, expected_excess=p)
            spec[tk] = (d, 3.0 if up else -3.0)
    sb._price_fetcher = CohortFetcher(spec)
    sb.score_due_proposals(now=end_of(weeks))
    return sb


def test_calibration_lifts_the_shrink_only_with_enough_graded_positive_skill(db):
    state = _graded_panel(db, informative=True, weeks=8, names=15).calibration_state()  # 120 graded, 8 cohorts
    assert state["n_graded"] == 120 and state["n_cohorts"] == 8
    assert state["brier_skill"] > 0
    assert state["shrink"] == 1.0


def test_calibration_keeps_shrinking_when_skill_is_not_positive(db):
    state = _graded_panel(db, informative=False, weeks=8, names=15).calibration_state()
    assert state["n_graded"] == 120
    assert state["brier_skill"] < 0
    assert state["shrink"] == 0.5


def test_calibration_keeps_shrinking_below_the_minimum_graded(db):
    state = _graded_panel(db, informative=True, weeks=1, names=40).calibration_state()  # 40 graded
    assert state["n_graded"] == 40 and state["shrink"] == 0.5


def test_one_big_cohort_is_not_enough_even_with_perfect_skill(db):
    """188 names in a single week share one market regime: 100+ graded forecasts, but only 1 cohort."""
    state = _graded_panel(db, informative=True, weeks=1, names=150).calibration_state()
    assert state["n_graded"] == 150 and state["n_cohorts"] == 1
    assert state["brier_skill"] > 0 and state["shrink"] == 0.5


# ---------------------------------------------------------------------------
# Pairing against the universe baseline + report
# ---------------------------------------------------------------------------
def test_ai_picks_are_paired_against_the_panel_universe_baseline(db):
    sb = make_sb(db)
    weeks = MIN_WEEKS_FOR_VERDICT + 2
    spec = _seed_panel(sb, weeks, 30, skilled=False)            # universe mean return ~ 0
    spec.update(seed_weekly_phase4(sb, [[3.0, 3.0, 3.0]] * weeks))
    sb._price_fetcher = CohortFetcher(spec)
    sb.score_due_proposals(now=end_of(weeks))
    verdict = sb.compute_scoreboard(phase="phase4")["verdict"]
    paired = verdict["vs_universe_baseline"]
    assert paired["n_weeks"] == weeks
    assert paired["mean_pp"] == pytest.approx(3.0, abs=0.6)     # +3% picks vs a ~flat universe


def test_report_has_per_layer_verdicts_panel_and_calibration_sections(db):
    sb = make_sb(db)
    md = sb.render_report_markdown()
    for needle in ("## 🏁 Verdicts", "Weekly forecast panel", "Sizing calibration",
                   "Phase 2 — ticker proposals", "Phase 4 — trade recommendations",
                   "Asymmetry Hunter (Soros layer)", "pre-registered", "Per-prompt-version and per-model"):
        assert needle in md, needle
    assert "AI vs the deterministic screener" not in md
