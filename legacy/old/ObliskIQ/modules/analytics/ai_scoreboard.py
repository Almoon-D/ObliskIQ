"""
AI Judgment Scoreboard (v5.40.0)
================================

Measure whether the AI layer (Phase 2 ticker proposals + Phase 4 trade
recommendations) has any predictive value — empirically, against reality.

Why this exists
---------------
ObliskIQ's MasterPrompt is the most expensive, least-tested component in the
system. Phases 2 and 4 are "where the AI thinks", yet nothing has ever measured
whether that thinking beats the deterministic screener it sits on top of. This
module closes that loop:

1. **Log** every AI proposal at proposal time (ticker, direction, conviction,
   regime call, reflexivity claim, timestamp, model, prompt-version hash).
2. **Score** proposals retrospectively once they have aged 2 / 4 / 8 / 12 weeks:
   forward return vs SPY, and vs the deterministic screener's own selection.
   Metrics: hit rate, average excess return, IC of conviction vs realized return.
3. **Grade** the AI's regime call against the realized regime.
4. **Report** monthly — an "AI value-added report" with a per-MasterPrompt-version
   breakdown, so prompt edits become A/B-testable.
5. **Render a verdict.** Once 60+ proposals have been scored at a horizon, run a
   proportion test of the AI's hit rate against the no-skill baseline. If the AI
   is statistically indistinguishable from the baseline, the report says so
   plainly — that would mean the MasterPrompt is decoration and Phases 2/4 should
   be simplified.

Design notes (Dalio: graceful degradation)
------------------------------------------
- Logging is a pure SQLite write with no network — it never slows the pipeline.
- Forward prices are fetched lazily at *scoring* time (both endpoints are in the
  past by then), so a logging call never blocks on yfinance.
- Every public entry point swallows its own exceptions and logs them: the
  scoreboard can never break the trading pipeline.
- Scoring is idempotent — each (proposal, horizon) is scored at most once.

"Realized regime" is a market-outcome proxy: the sign and size of SPY's forward
return over the 8-week horizon (bull / bear / choppy). This is deliberately
honest about what we can reconstruct retroactively without a full historical
macro snapshot; the methodology is stated in the generated report.

v6.16.0 — statistics that can tell skill from luck
--------------------------------------------------
The v5.40 verdict (one proportion test of "beat SPY" on every logged row) could not discriminate:
proposals of the same name on consecutive runs are not independent, the null ("beats SPY half the
time") is not the no-skill null for a long-biased book, and 60 rows only detect a hit rate >= ~68%.
The verdict logic is now PRE-REGISTERED (constants below; do not tune them against data):

* one observation per (layer, ticker, direction, ISO week)  — overlapping repeats are dropped;
* each layer (Phase 2, Phase 4, Asymmetry Hunter, weekly forecast panel) is judged on its own;
* the primary horizon is fixed at 4 weeks — no picking the horizon that looks best;
* excess return is against the proposal's OWN regional benchmark, in USD (a Tokyo listing is not
  "beating SPY" by being long the yen);
* the test statistic is the mean of weekly-cohort mean excess returns with a Newey-West standard
  error (consecutive 4-week cohorts share 3 weeks of returns), reported as a 95% interval;
* "decoration" is claimed only when the interval RULES OUT a meaningful edge; an interval that
  merely includes zero is "inconclusive" — absence of evidence is not evidence of absence.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modules.analytics.stat_utils import (
    brier_skill,
    newey_west_mean,
    spearman_ic,
)
from modules.config.global_vehicles import (
    benchmark_for_symbol,
    fx_pair_for_currency,
    trading_currency_for,
)

logger = logging.getLogger(__name__)


# Forward-return horizons, in calendar days. Keys are the human labels used in
# the schema columns and the report.
HORIZONS: Dict[str, int] = {
    "2w": 14,
    "4w": 28,
    "8w": 56,
    "12w": 84,
}

# Horizon at which we grade the AI's regime call (the most mature medium-term
# window). Must be a key of HORIZONS.
REGIME_HORIZON = "8w"

# SPY forward-return thresholds (percent) used to label the realized regime.
REGIME_BULL_THRESHOLD = 3.0
REGIME_BEAR_THRESHOLD = -3.0

# Minimum scored proposals (at a single horizon) before a statistical verdict is
# rendered. Below this, the report says "insufficient data" rather than guessing.
# (Kept for the grading-clock display; the verdict itself now uses the pre-registered
# cohort rules below.)
MIN_PROPOSALS_FOR_VERDICT = 60

# --- PRE-REGISTERED verdict rules (v6.16.0). Do not tune against observed results. ---------
#: The one horizon every verdict is judged on (no data-dependent horizon pick).
PRIMARY_HORIZON = "4w"
#: Newey-West lags: consecutive weekly cohorts overlap for (horizon_weeks - 1) weeks.
NW_LAGS = HORIZONS[PRIMARY_HORIZON] // 7 - 1
#: Distinct weekly cohorts, and independent observations, needed before any verdict. 26 weeks because a
#: Newey-West standard error is anti-conservative on a short series (it under-states the variance at n~12),
#: and consecutive 4-week cohorts overlap, so 26 weekly cohorts carry roughly 6-7 independent ones.
MIN_WEEKS_FOR_VERDICT = 26
MIN_OBS_FOR_VERDICT = 30
#: Smallest mean 4-week excess return (percentage points) worth acting on. "Decoration" is only
#: declared when the 95% interval's upper bound is below this.
MEANINGFUL_EXCESS_PP = 0.5
#: Forecast panel: smallest cross-sectional rank IC worth acting on (Simons' bar is 0.05; the panel
#: is a calibration instrument, so its bar is lower).
MEANINGFUL_PANEL_IC = 0.03
#: Calibration evidence needed before stated probabilities stop being shrunk toward 0.5.
CALIBRATION_MIN_GRADED = 100
#: ...across at least this many weekly cohorts (one cohort is one market regime: its outcomes are correlated).
CALIBRATION_MIN_COHORTS = 8
CALIBRATION_SHRINK = 0.5

#: The weekly forecast panel is graded at fewer horizons (it is ~185 names a week: every extra
#: horizon is ~185 more price lookups).
PANEL_SOURCE = "forecast_panel"
PANEL_HORIZONS: Tuple[str, ...] = ("4w", "12w")
#: A weekly cohort needs at least this many graded names for its rank IC to be meaningful.
MIN_PANEL_COHORT = 30

# Map the AI's free-form regime label onto a directional bucket so we can compare
# it to the realized (SPY-derived) regime.
_REGIME_BUCKET: Dict[str, str] = {
    "bull": "bull",
    "goldilocks": "bull",
    "reflation": "bull",
    "recovery": "bull",
    "bear": "bear",
    "recession": "bear",
    "deflation": "bear",
    "risk-off": "bear",
    "crisis": "bear",
    "defensive": "bear",
    "choppy": "choppy",
    "stagflation": "choppy",
    "uncertain": "choppy",
    "reflexivity": "choppy",
    "normal": "choppy",
    "sideways": "choppy",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _direction_sign(direction: str) -> Optional[int]:
    """+1 for long bets, -1 for short bets, None for non-directional (HOLD)."""
    d = (direction or "").upper()
    if d in ("LONG", "BUY"):
        return 1
    if d in ("SHORT", "SELL"):
        return -1
    return None


class AIScoreboard:
    """
    Persist and grade AI proposals against realized market outcomes.

    Args:
        db_manager: A ``DatabaseManager`` (provides pooled SQLite connections).
        price_fetcher: Optional object exposing
            ``fetch_price_on_date(ticker, datetime) -> Optional[float]``.
            Defaults to a lazily-constructed ``PriceHistoryFetcher``. Inject a
            mock in tests to avoid network.
        ic_calculator: Optional ``InformationCoefficientCalculator``. Lazily
            constructed if omitted.
        prompt_composer: Optional ``PromptComposer`` used only to derive the
            default prompt-version hash. Lazily constructed if omitted.
        reports_dir: Where the monthly markdown report is written
            (default ``reports/scoreboard``).
    """

    def __init__(
        self,
        db_manager: Any,
        price_fetcher: Optional[Any] = None,
        ic_calculator: Optional[Any] = None,
        prompt_composer: Optional[Any] = None,
        reports_dir: Optional[Path] = None,
    ) -> None:
        self.db = db_manager
        self._price_fetcher = price_fetcher
        self._ic_calculator = ic_calculator
        self._prompt_composer = prompt_composer
        self.reports_dir = reports_dir or (
            Path(__file__).resolve().parent.parent.parent / "reports" / "scoreboard"
        )
        self._prompt_hash_cache: Optional[str] = None
        self._ensure_tables()
        logger.info("🧮 AIScoreboard initialized (AI Judgment Scoreboard)")

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def _ensure_tables(self) -> None:
        """Create the proposal + meta tables if they do not yet exist."""
        # Per-horizon scoring columns are generated so adding a horizon is a
        # one-line change to HORIZONS.
        horizon_cols = []
        for h in HORIZONS:
            horizon_cols.append(f"pos_ret_{h} REAL")
            horizon_cols.append(f"spy_ret_{h} REAL")
            horizon_cols.append(f"excess_{h} REAL")
        horizon_sql = ",\n            ".join(horizon_cols)

        create_sql = f"""
        CREATE TABLE IF NOT EXISTS ai_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT UNIQUE NOT NULL,
            run_id TEXT,
            phase TEXT NOT NULL,
            proposal_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            action TEXT,
            conviction REAL,
            regime_call TEXT,
            regime_confidence REAL,
            reflexivity_intensity REAL,
            position_size_pct REAL,
            win_probability REAL,
            payoff_ratio REAL,
            model_name TEXT,
            prompt_version TEXT,
            prompt_hash TEXT,
            source TEXT DEFAULT 'masterprompt',
            system_version TEXT,
            screener_selected INTEGER,
            reasoning TEXT,
            ticker_price_at_proposal REAL,
            spy_price_at_proposal REAL,
            benchmark_ticker TEXT,
            fx_symbol TEXT,
            fx_price_at_proposal REAL,
            iso_week TEXT,
            expected_excess REAL,
            baseline_signal REAL,
            universe_hash TEXT,
            {horizon_sql},
            realized_regime TEXT,
            regime_correct INTEGER,
            scored_horizons TEXT DEFAULT '[]',
            last_scored_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_ai_prop_date ON ai_proposals(proposal_date);
        CREATE INDEX IF NOT EXISTS idx_ai_prop_ticker ON ai_proposals(ticker);
        CREATE INDEX IF NOT EXISTS idx_ai_prop_phase ON ai_proposals(phase);
        CREATE INDEX IF NOT EXISTS idx_ai_prop_hash ON ai_proposals(prompt_hash);

        CREATE TABLE IF NOT EXISTS ai_scoreboard_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
        with self.db.get_connection() as conn:
            conn.executescript(create_sql)
            conn.commit()
        self._migrate_add_missing_columns()

    def _migrate_add_missing_columns(self) -> None:
        """Add any columns missing from an older ai_proposals table (defensive)."""
        expected: Dict[str, str] = {
            "source": "TEXT DEFAULT 'masterprompt'",
            # v6.12.0 decision contract: AI-stated Kelly inputs, logged at
            # proposal time so win_probability becomes Brier-scoreable by the
            # grading loop once outcomes mature.
            "win_probability": "REAL",
            "payoff_ratio": "REAL",
            # v6.16.0: regional USD benchmark (spy_ret_* / spy_price_at_proposal now hold the
            # BENCHMARK's return/price — column names kept to avoid a destructive migration),
            # FX leg for non-USD listings, cohort key, and the forecast-panel fields.
            "benchmark_ticker": "TEXT",
            "fx_symbol": "TEXT",
            "fx_price_at_proposal": "REAL",
            "iso_week": "TEXT",
            "expected_excess": "REAL",
            "baseline_signal": "REAL",
            "universe_hash": "TEXT",
        }
        for h in HORIZONS:
            expected[f"pos_ret_{h}"] = "REAL"
            expected[f"spy_ret_{h}"] = "REAL"
            expected[f"excess_{h}"] = "REAL"
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("PRAGMA table_info(ai_proposals)")
                existing = {row[1] for row in cursor.fetchall()}
                for col, col_type in expected.items():
                    if col not in existing:
                        conn.execute(
                            f"ALTER TABLE ai_proposals ADD COLUMN {col} {col_type}"
                        )
                        logger.info(f"🔄 AIScoreboard migration: added column {col}")
                conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"AIScoreboard migration check skipped: {exc}")

    # ------------------------------------------------------------------ #
    # Attribution helpers
    # ------------------------------------------------------------------ #
    @property
    def prompt_hash(self) -> str:
        """Hash of the current MasterPrompt template (cached per process)."""
        if self._prompt_hash_cache is None:
            try:
                if self._prompt_composer is None:
                    from modules.utils.prompt_composer import PromptComposer

                    self._prompt_composer = PromptComposer()
                self._prompt_hash_cache = self._prompt_composer.prompt_version_hash()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"prompt_hash unavailable: {exc}")
                self._prompt_hash_cache = "unknown"
        return self._prompt_hash_cache

    @property
    def price_fetcher(self) -> Any:
        if self._price_fetcher is None:
            from modules.market.price_history import PriceHistoryFetcher

            self._price_fetcher = PriceHistoryFetcher()
        return self._price_fetcher

    @property
    def ic_calculator(self) -> Any:
        if self._ic_calculator is None:
            from modules.analytics.ic_calculator import (
                InformationCoefficientCalculator,
            )

            self._ic_calculator = InformationCoefficientCalculator()
        return self._ic_calculator

    @staticmethod
    def _system_version() -> str:
        try:
            from modules.utils.version import get_version

            return get_version()
        except Exception:  # pragma: no cover - defensive
            return "unknown"

    # ------------------------------------------------------------------ #
    # Logging (called at proposal time)
    # ------------------------------------------------------------------ #
    def log_phase2_proposals(
        self,
        analysis: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
        model_name: Optional[str] = None,
        prompt_hash: Optional[str] = None,
        validated_tickers: Optional[Sequence[str]] = None,
    ) -> int:
        """
        Log Phase 2 AI ticker proposals (one row per suggested ticker).

        Phase 2 proposes long opportunities; conviction is taken from the AI's
        regime confidence (Phase 2 has no per-ticker conviction). ``screener_selected``
        records whether the deterministic screener subsequently validated the
        ticker — the basis for the "AI vs deterministic screener" comparison.

        Returns the number of rows written. Never raises.
        """
        try:
            tickers = (
                analysis.get("ai_proposed_tickers")
                or analysis.get("suggested_tickers")
                or []
            )
            if not tickers:
                return 0
            regime = analysis.get("market_regime", "unknown")
            regime_conf = _as_float(analysis.get("regime_confidence"))
            reflex = _as_float(analysis.get("reflexivity_intensity"))
            rationale = analysis.get("ticker_rationale", {}) or {}
            validated = {t.upper() for t in (validated_tickers or [])}
            ts = _utcnow().isoformat()
            run = run_id or _run_id_from_ts(ts)

            rows = []
            for ticker in tickers:
                tk = str(ticker).upper()
                rows.append(
                    self._build_row(
                        proposal_id=f"{run}_phase2_{tk}",
                        run_id=run,
                        phase="phase2",
                        proposal_date=ts,
                        ticker=tk,
                        direction="LONG",
                        action="SUGGEST",
                        conviction=regime_conf,
                        regime_call=regime,
                        regime_confidence=regime_conf,
                        reflexivity_intensity=reflex,
                        position_size_pct=None,
                        model_name=model_name,
                        prompt_hash=prompt_hash or self.prompt_hash,
                        screener_selected=1 if tk in validated else 0,
                        reasoning=str(rationale.get(ticker, rationale.get(tk, "")))[:1000],
                    )
                )
            written = self._insert_rows(rows)
            logger.info(f"🧮 Scoreboard: logged {written} Phase 2 proposals (run {run})")
            return written
        except Exception as exc:
            logger.warning(f"AIScoreboard.log_phase2_proposals failed (non-fatal): {exc}")
            return 0

    def log_phase4_proposals(
        self,
        ai_decisions: Dict[str, Any],
        *,
        regime_call: Optional[str] = None,
        reflexivity_intensity: Optional[float] = None,
        run_id: Optional[str] = None,
        model_name: Optional[str] = None,
        prompt_hash: Optional[str] = None,
    ) -> int:
        """
        Log Phase 4 AI trade recommendations (one row per decision).

        Pass the AI's *original* decisions, before deterministic conviction or
        position-size overrides, so we grade the AI's own judgment. Regime and
        reflexivity context are inherited from Phase 2.

        Returns the number of rows written. Never raises.
        """
        try:
            decisions = ai_decisions.get("portfolio_decisions") or ai_decisions.get(
                "decisions"
            ) or []
            if not decisions:
                return 0
            ts = _utcnow().isoformat()
            run = run_id or _run_id_from_ts(ts)
            reflex = _as_float(reflexivity_intensity)

            rows = []
            for dec in decisions:
                tk = str(dec.get("ticker", "")).upper()
                if not tk:
                    continue
                action = str(dec.get("action", "")).upper() or "HOLD"
                direction = {"BUY": "LONG", "SELL": "SHORT"}.get(action, "HOLD")
                rows.append(
                    self._build_row(
                        proposal_id=f"{run}_phase4_{tk}",
                        run_id=run,
                        phase="phase4",
                        proposal_date=ts,
                        ticker=tk,
                        direction=direction,
                        action=action,
                        conviction=_as_float(dec.get("conviction")),
                        regime_call=regime_call or "unknown",
                        regime_confidence=None,
                        reflexivity_intensity=reflex,
                        position_size_pct=_as_float(dec.get("position_size_pct")),
                        # v6.12.0 contract: AI-stated Kelly inputs (Brier-scoreable)
                        win_probability=_as_float(dec.get("win_probability")),
                        payoff_ratio=_as_float(dec.get("payoff_ratio")),
                        model_name=model_name,
                        prompt_hash=prompt_hash or self.prompt_hash,
                        screener_selected=1,  # Phase 4 acts on screener-validated tickers
                        reasoning=str(dec.get("reasoning", ""))[:1000],
                    )
                )
            written = self._insert_rows(rows)
            logger.info(f"🧮 Scoreboard: logged {written} Phase 4 proposals (run {run})")
            return written
        except Exception as exc:
            logger.warning(f"AIScoreboard.log_phase4_proposals failed (non-fatal): {exc}")
            return 0

    def log_asymmetry_dossiers(
        self,
        dossiers: Sequence[Dict[str, Any]],
        *,
        weights_hash: str,
        run_id: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> int:
        """
        Log Asymmetry Hunter dossiers (one row per candidate) for grading.

        The Soros overlay is graded by the SAME machinery as the MasterPrompt:
        each dossier carries ``source='asymmetry_hunter'`` and
        ``prompt_hash=weights_hash`` (a hash of the component-weights config), so
        ``score_due_proposals`` scores it against SPY at 2/4/8/12w and the verdict
        logic tells us whether the layer adds value, is decoration, or is
        anti-predictive — same standard, isolated by source/hash.

        Returns the number of rows written. Never raises.
        """
        try:
            if not dossiers:
                return 0
            ts = _utcnow().isoformat()
            run = run_id or _run_id_from_ts(ts)

            rows = []
            for dossier in dossiers:
                tk = str(dossier.get("mapped_ticker", "")).upper()
                if not tk:
                    continue
                direction = str(dossier.get("direction", "")).upper()
                if direction not in ("LONG", "SHORT"):
                    continue
                composite = _as_float(dossier.get("composite_score"))
                # Composite is 0-100; conviction is logged on the 0-10 scale used
                # elsewhere in the scoreboard.
                conviction = composite / 10.0 if composite is not None else None
                rows.append(
                    self._build_row(
                        proposal_id=f"{run}_asym_{tk}",
                        run_id=run,
                        phase="asymmetry",
                        proposal_date=ts,
                        ticker=tk,
                        direction=direction,
                        action="WATCH",
                        conviction=conviction,
                        regime_call=str(dossier.get("regime_call", "unknown")),
                        regime_confidence=None,
                        reflexivity_intensity=None,
                        position_size_pct=_as_float(dossier.get("max_premium_pct")),
                        model_name=model_name,
                        prompt_hash=weights_hash,
                        source="asymmetry_hunter",
                        screener_selected=1,
                        reasoning=str(dossier.get("consensus_belief", ""))[:1000],
                    )
                )
            written = self._insert_rows(rows)
            logger.info(
                f"🧮 Scoreboard: logged {written} Asymmetry dossiers (run {run})"
            )
            return written
        except Exception as exc:
            logger.warning(
                f"AIScoreboard.log_asymmetry_dossiers failed (non-fatal): {exc}"
            )
            return 0

    # ------------------------------------------------------------------ #
    # Weekly forecast panel (v6.16.0)
    # ------------------------------------------------------------------ #
    def panel_cohort_exists(self, iso_week: str) -> bool:
        """True when a forecast-panel cohort was already logged for this ISO week."""
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM ai_proposals WHERE source = ? AND iso_week = ? LIMIT 1",
                (PANEL_SOURCE, iso_week),
            ).fetchone()
        return row is not None

    def log_forecast_panel(
        self,
        forecasts: Sequence[Dict[str, Any]],
        *,
        universe_hash: str,
        run_id: Optional[str] = None,
        model_name: Optional[str] = None,
        prompt_hash: Optional[str] = None,
        as_of: Optional[datetime] = None,
        benchmark_ticker: str = "ACWI",
        benchmark_price: Optional[float] = None,
    ) -> int:
        """
        Log one cohort of the weekly forecast panel (one row per forecast name).

        Each forecast is ``{"ticker", "p_outperform", "expected_excess" (%), "baseline_signal"
        (12-1 momentum, %), "entry_price"}``. Rows carry ``source='forecast_panel'`` and the hash of the
        pre-registered universe; all names are USD listings graded against ONE common benchmark so the
        cross-sectional ranks are untouched by it. Idempotent per (run, ticker). Never raises.
        """
        try:
            if not forecasts:
                return 0
            when = as_of or _utcnow()
            ts = when.isoformat()
            run = run_id or _run_id_from_ts(ts)
            rows = []
            for fc in forecasts:
                tk = str(fc.get("ticker", "")).upper()
                p = _as_float(fc.get("p_outperform"))
                if not tk or p is None:
                    continue
                rows.append(
                    self._build_row(
                        proposal_id=f"{run}_panel_{tk}",
                        run_id=run,
                        phase="panel",
                        proposal_date=ts,
                        ticker=tk,
                        direction="LONG",
                        action="FORECAST",
                        conviction=p * 10.0,
                        regime_call="unknown",
                        win_probability=p,
                        expected_excess=_as_float(fc.get("expected_excess")),
                        baseline_signal=_as_float(fc.get("baseline_signal")),
                        model_name=fc.get("model_name") or model_name,
                        prompt_hash=prompt_hash or self.prompt_hash,
                        source=PANEL_SOURCE,
                        screener_selected=1,
                        universe_hash=universe_hash,
                        benchmark_ticker=benchmark_ticker,
                        fx_symbol=None,
                        fx_price_at_proposal=1.0,
                        ticker_price_at_proposal=_as_float(fc.get("entry_price")),
                        spy_price_at_proposal=benchmark_price,
                        reasoning="",
                    )
                )
            written = self._insert_rows(rows)
            logger.info(f"🧮 Scoreboard: logged {written} forecast-panel rows (run {run})")
            return written
        except Exception as exc:
            logger.warning(f"AIScoreboard.log_forecast_panel failed (non-fatal): {exc}")
            return 0

    def _build_row(self, **kwargs: Any) -> Dict[str, Any]:
        kwargs.setdefault("prompt_version", "v3.3")
        kwargs.setdefault("source", "masterprompt")
        kwargs["system_version"] = self._system_version()
        ticker = str(kwargs.get("ticker", ""))
        # Grade a proposal against ITS region's benchmark, in USD (v6.16.0). The panel overrides the
        # benchmark with one common index so its cross-sectional ranks are unaffected by it.
        kwargs.setdefault("benchmark_ticker", benchmark_for_symbol(ticker))
        fx = fx_pair_for_currency(trading_currency_for(ticker))
        kwargs.setdefault("fx_symbol", fx[0] if fx else None)
        kwargs["iso_week"] = iso_week_key(kwargs.get("proposal_date"))
        return kwargs

    def _insert_rows(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        columns = [
            "proposal_id", "run_id", "phase", "proposal_date", "ticker",
            "direction", "action", "conviction", "regime_call", "regime_confidence",
            "reflexivity_intensity", "position_size_pct",
            "win_probability", "payoff_ratio", "model_name",
            "prompt_version", "prompt_hash", "source", "system_version",
            "screener_selected", "reasoning",
            "benchmark_ticker", "fx_symbol", "iso_week", "expected_excess",
            "baseline_signal", "universe_hash",
            "ticker_price_at_proposal", "spy_price_at_proposal", "fx_price_at_proposal",
        ]
        placeholders = ", ".join("?" for _ in columns)
        col_list = ", ".join(columns)
        sql = (
            f"INSERT OR IGNORE INTO ai_proposals ({col_list}) VALUES ({placeholders})"
        )
        written = 0
        with self.db.get_connection() as conn:
            for row in rows:
                cursor = conn.execute(sql, tuple(row.get(c) for c in columns))
                written += cursor.rowcount if cursor.rowcount > 0 else 0
            conn.commit()
        return written

    # ------------------------------------------------------------------ #
    # Retrospective scoring (called in Phase 1)
    # ------------------------------------------------------------------ #
    def score_due_proposals(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Score every proposal/horizon that has matured and is not yet scored.

        For each due (proposal, horizon): fetch the ticker and SPY price at the
        proposal date and at proposal_date + horizon, then record the AI bet's
        signed return, SPY's return, and the excess. At the 8-week horizon, also
        grade the regime call.

        Returns a summary ``{"proposals_examined", "horizons_scored", "errors"}``.
        Idempotent and non-fatal.
        """
        now = now or _utcnow()
        summary = {"proposals_examined": 0, "horizons_scored": 0, "errors": 0}
        price_memo: Dict[str, Optional[float]] = {}

        try:
            rows = self._fetch_unscored_rows()
        except Exception as exc:
            logger.warning(f"AIScoreboard.score_due_proposals query failed: {exc}")
            return summary

        self._prefetch_prices(rows, now, price_memo)

        for row in rows:
            summary["proposals_examined"] += 1
            try:
                updated = self._score_row(row, now, price_memo)
                summary["horizons_scored"] += updated
            except Exception as exc:
                summary["errors"] += 1
                logger.debug(f"Scoring proposal {row.get('proposal_id')} failed: {exc}")

        if summary["horizons_scored"]:
            logger.info(
                f"🧮 Scoreboard: scored {summary['horizons_scored']} proposal-horizons "
                f"across {summary['proposals_examined']} due proposals"
            )
        return summary

    def _fetch_unscored_rows(self) -> List[Dict[str, Any]]:
        """Return proposals that still have at least one unscored horizon."""
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM ai_proposals "
                "WHERE scored_horizons IS NULL "
                "   OR scored_horizons NOT LIKE '%12w%' "
                "   OR scored_horizons = '[]'"
            )
            return [dict(r) for r in cursor.fetchall()]

    def _price_on(
        self, ticker: str, when: datetime, memo: Dict[str, Optional[float]]
    ) -> Optional[float]:
        key = f"{ticker}|{when.date().isoformat()}"
        if key not in memo:
            memo[key] = self.price_fetcher.fetch_price_on_date(ticker, when)
        return memo[key]

    def _usd_rate_on(
        self, fx_symbol: Optional[str], when: datetime, memo: Dict[str, Optional[float]]
    ) -> Optional[float]:
        """USD per one unit of the listing currency on ``when`` (1.0 for USD listings)."""
        if not fx_symbol:
            return 1.0
        quote = self._price_on(fx_symbol, when, memo)
        if quote is None or quote <= 0:
            return None
        # "USDJPY=X" quotes JPY per USD -> USD per JPY is the reciprocal.
        return 1.0 / quote if fx_symbol.upper().startswith("USD") else quote

    def _prefetch_prices(
        self, rows: Sequence[Dict[str, Any]], now: datetime, memo: Dict[str, Optional[float]]
    ) -> None:
        """Batch the due price lookups per date when the fetcher supports it.

        One request per DATE instead of one per (ticker, date): a weekly panel of ~185 names would
        otherwise issue ~185 sequential Yahoo requests per horizon. Fetchers without the batch method
        (and any ticker the batch misses) fall back to the per-ticker path, so this only ever saves
        requests — it never changes a result.
        """
        batch = getattr(self._price_fetcher or self.price_fetcher, "fetch_prices_on_date_batch", None)
        if not callable(batch):
            return
        wanted: Dict[str, set] = {}
        for row in rows:
            proposal_date = _parse_dt(row.get("proposal_date"))
            if proposal_date is None:
                continue
            symbols = {row["ticker"], row.get("benchmark_ticker") or "SPY"}
            if row.get("fx_symbol"):
                symbols.add(row["fx_symbol"])
            already = set(json.loads(row.get("scored_horizons") or "[]"))
            for label, days in _horizons_for(row).items():
                if label in already:
                    continue
                target = proposal_date + timedelta(days=days)
                if now < target + timedelta(days=2):
                    continue
                wanted.setdefault(target.date().isoformat(), set()).update(symbols)
        for day, symbols in wanted.items():
            when = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            try:
                found = batch(sorted(symbols), when)
            except Exception as exc:  # never let an optimisation break scoring
                logger.debug(f"batch price prefetch failed for {day}: {exc}")
                continue
            for symbol, price in (found or {}).items():
                if price is not None:
                    memo[f"{symbol}|{day}"] = price

    def _score_row(
        self, row: Dict[str, Any], now: datetime, memo: Dict[str, Optional[float]]
    ) -> int:
        proposal_date = _parse_dt(row["proposal_date"])
        if proposal_date is None:
            return 0
        sign = _direction_sign(row.get("direction", ""))
        already = set(json.loads(row.get("scored_horizons") or "[]"))
        ticker = row["ticker"]
        bench = row.get("benchmark_ticker") or "SPY"
        fx_symbol = row.get("fx_symbol") or None

        updates: Dict[str, Any] = {}
        newly_scored: List[str] = []
        horizons = _horizons_for(row)

        # Resolve entry prices once (shared across horizons). ``ticker_price_at_proposal`` is the
        # LOCAL price; the FX leg is stored separately so the return can be computed in USD.
        start_ticker = row.get("ticker_price_at_proposal")
        start_bench = row.get("spy_price_at_proposal")
        start_fx = row.get("fx_price_at_proposal")
        if start_ticker is None:
            start_ticker = self._price_on(ticker, proposal_date, memo)
            if start_ticker is not None:
                updates["ticker_price_at_proposal"] = start_ticker
        if start_bench is None:
            start_bench = self._price_on(bench, proposal_date, memo)
            if start_bench is not None:
                updates["spy_price_at_proposal"] = start_bench
        if start_fx is None:
            start_fx = self._usd_rate_on(fx_symbol, proposal_date, memo)
            if start_fx is not None:
                updates["fx_price_at_proposal"] = start_fx

        for label, days in horizons.items():
            if label in already:
                continue
            target = proposal_date + timedelta(days=days)
            # Only score once the window has fully matured (plus a small buffer
            # so the target trading day reliably exists in price history).
            if now < target + timedelta(days=2):
                continue

            end_ticker = self._price_on(ticker, target, memo)
            end_bench = self._price_on(bench, target, memo)
            end_fx = self._usd_rate_on(fx_symbol, target, memo)

            # If prices are still unavailable well after maturity, mark the
            # horizon scored with NULLs to stop retrying a delisted/missing name.
            stale = now > target + timedelta(days=30)
            if (
                start_ticker is None
                or end_ticker is None
                or start_bench is None
                or end_bench is None
                or start_fx is None
                or end_fx is None
            ):
                if stale:
                    newly_scored.append(label)
                continue

            # Return in USD: local price change compounded with the currency move.
            ticker_ret = ((end_ticker * end_fx) / (start_ticker * start_fx) - 1.0) * 100.0
            bench_ret = (end_bench / start_bench - 1.0) * 100.0
            if sign is None:
                # HOLD: no directional bet. Record the benchmark move and a zero
                # position return so the horizon is closed out, but it is
                # excluded from hit-rate / IC stats downstream.
                pos_ret = 0.0
            else:
                pos_ret = sign * ticker_ret
            excess = pos_ret - bench_ret

            updates[f"pos_ret_{label}"] = round(pos_ret, 4)
            updates[f"spy_ret_{label}"] = round(bench_ret, 4)
            updates[f"excess_{label}"] = round(excess, 4)
            newly_scored.append(label)

            # Grade the regime call at the medium-term horizon (SPY is the regime proxy whatever the
            # proposal's own benchmark is).
            if label == REGIME_HORIZON and row.get("realized_regime") is None:
                spy_start = self._price_on("SPY", proposal_date, memo)
                spy_end = self._price_on("SPY", target, memo)
                if spy_start and spy_end:
                    realized = _classify_realized_regime((spy_end / spy_start - 1.0) * 100.0)
                    updates["realized_regime"] = realized
                    ai_bucket = _REGIME_BUCKET.get(str(row.get("regime_call", "")).lower())
                    if ai_bucket is not None:
                        updates["regime_correct"] = 1 if ai_bucket == realized else 0

        if not newly_scored and not updates:
            return 0

        all_scored = sorted(already.union(newly_scored), key=lambda x: HORIZONS.get(x, 0))
        updates["scored_horizons"] = json.dumps(all_scored)
        updates["last_scored_at"] = now.isoformat()
        self._apply_updates(row["proposal_id"], updates)
        return len(newly_scored)

    def _apply_updates(self, proposal_id: str, updates: Dict[str, Any]) -> None:
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [proposal_id]
        with self.db.get_connection() as conn:
            conn.execute(
                f"UPDATE ai_proposals SET {set_clause} WHERE proposal_id = ?", params
            )
            conn.commit()

    # ------------------------------------------------------------------ #
    # Aggregation
    # ------------------------------------------------------------------ #
    def _query_rows(
        self,
        *,
        phase: Optional[str] = None,
        prompt_hash: Optional[str] = None,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if phase:
            clauses.append("phase = ?")
            params.append(phase)
        if prompt_hash:
            clauses.append("prompt_hash = ?")
            params.append(prompt_hash)
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.db.get_connection() as conn:
            cursor = conn.execute(f"SELECT * FROM ai_proposals{where}", params)
            return [dict(r) for r in cursor.fetchall()]

    def compute_metrics(
        self, rows: Sequence[Dict[str, Any]], horizon: str
    ) -> Dict[str, Any]:
        """
        Aggregate scored, directional proposals at one horizon.

        Returns hit rate (beat SPY), profitable rate, average excess return,
        average position return, and the IC of conviction vs realized return.
        HOLD proposals are excluded (no directional bet).
        """
        excess_col = f"excess_{horizon}"
        pos_col = f"pos_ret_{horizon}"
        convictions: List[float] = []
        pos_returns: List[float] = []
        excesses: List[float] = []
        beat_spy = 0
        profitable = 0

        for r in rows:
            if _direction_sign(r.get("direction", "")) is None:
                continue  # HOLD
            excess = r.get(excess_col)
            pos = r.get(pos_col)
            if excess is None or pos is None:
                continue
            excesses.append(float(excess))
            pos_returns.append(float(pos))
            if float(excess) > 0:
                beat_spy += 1
            if float(pos) > 0:
                profitable += 1
            conv = r.get("conviction")
            convictions.append(float(conv) if conv is not None else math.nan)

        n = len(excesses)
        result: Dict[str, Any] = {
            "horizon": horizon,
            "n": n,
            "beat_spy_rate": (beat_spy / n) if n else None,
            "profitable_rate": (profitable / n) if n else None,
            "avg_excess_return": (sum(excesses) / n) if n else None,
            "avg_position_return": (sum(pos_returns) / n) if n else None,
            "ic": None,
            "ic_p_value": None,
            "ic_significance": None,
        }
        distinct_conv = {c for c in convictions if not math.isnan(c)}
        if n >= 10 and len(distinct_conv) >= 2:
            # IC is undefined when conviction has no variance (e.g. Phase 2,
            # where every proposal inherits the same regime confidence).
            try:
                ic_res = self.ic_calculator.calculate_ic(
                    convictions, pos_returns, signal_name=f"conviction_{horizon}"
                )
                result["ic"] = ic_res.get("ic")
                result["ic_p_value"] = ic_res.get("p_value")
                result["ic_significance"] = ic_res.get("significance")
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"IC computation failed for {horizon}: {exc}")
        return result

    def compute_regime_accuracy(
        self, rows: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        graded = [r for r in rows if r.get("regime_correct") is not None]
        n = len(graded)
        correct = sum(1 for r in graded if int(r["regime_correct"]) == 1)
        return {
            "n": n,
            "correct": correct,
            "accuracy": (correct / n) if n else None,
        }

    def compute_scoreboard(
        self,
        *,
        phase: Optional[str] = None,
        prompt_hash: Optional[str] = None,
        source: Optional[str] = None,
        layer_label: str = "MasterPrompt",
        simplify_hint: str = "Phases 2/4 should be simplified to the deterministic screener",
    ) -> Dict[str, Any]:
        """
        Full aggregate snapshot used by the report and the CLI.

        ``source`` isolates a layer (e.g. ``'asymmetry_hunter'`` for the Soros
        overlay) and ``phase`` isolates Phase 2 / Phase 4, so each is graded as its
        OWN verdict. Metrics and the verdict are computed on INDEPENDENT observations
        (one per ticker/direction/ISO week); ``total_proposals`` stays the raw logged
        count so the operator can see how many repeats were collapsed.
        """
        rows = self._query_rows(phase=phase, prompt_hash=prompt_hash, source=source)
        independent = dedupe_observations(rows)
        per_horizon = {h: self.compute_metrics(independent, h) for h in HORIZONS}
        return {
            "total_proposals": len(rows),
            "independent_observations": len(independent),
            "per_horizon": per_horizon,
            "regime_accuracy": self.compute_regime_accuracy(rows),
            "verdict": self._verdict(
                independent, per_horizon, layer_label=layer_label, simplify_hint=simplify_hint
            ),
        }

    def compute_layer_scoreboards(self) -> Dict[str, Dict[str, Any]]:
        """One scoreboard per graded layer — never pooled (a pooled verdict hides which layer works)."""
        return {
            "phase2": self.compute_scoreboard(phase="phase2", layer_label="Phase 2 (ticker proposals)",
                                              simplify_hint="Phase 2 should be replaced by the deterministic screen"),
            "phase4": self.compute_scoreboard(phase="phase4", layer_label="Phase 4 (trade recommendations)",
                                              simplify_hint="Phase 4 should defer to the deterministic sizing"),
            "asymmetry_hunter": self.compute_asymmetry_scoreboard(),
        }

    def _universe_baseline_by_week(self, horizon: str) -> Dict[str, float]:
        """Equal-weight mean RAW return (%) of the forecast-panel universe per cohort week.

        The deterministic comparator for the AI's picks: "what did a random draw from the same fixed
        liquid universe return that week?" Empty until the first panel cohort matures.
        """
        col = f"pos_ret_{horizon}"
        by_week: Dict[str, List[float]] = {}
        for r in self._query_rows(source=PANEL_SOURCE):
            value = r.get(col)
            if value is not None and r.get("iso_week"):
                by_week.setdefault(r["iso_week"], []).append(float(value))
        return {wk: sum(v) / len(v) for wk, v in by_week.items() if v}

    # ------------------------------------------------------------------ #
    # Forecast panel — the statistically powered instrument
    # ------------------------------------------------------------------ #
    def compute_panel_scoreboard(self, horizon: str = PRIMARY_HORIZON) -> Dict[str, Any]:
        """
        Cross-sectional skill of the weekly forecast panel.

        Per weekly cohort (~185 names): rank IC of the AI's ``expected_excess`` against the realised
        excess return, the same for the 12-1 momentum baseline logged alongside it, their difference
        (incremental skill), and the top-decile-minus-bottom-decile spread. The verdict is the
        Newey-West mean of the weekly IC series. Brier skill pools every graded name
        (outcome: excess return > 0). Power: a cohort's IC has SE ~ 1/sqrt(N) ~ 0.07, so ~12 weekly
        cohorts resolve an IC of ~0.04 — years faster than grading a handful of trades.
        """
        excess_col = f"excess_{horizon}"
        rows = [r for r in self._query_rows(source=PANEL_SOURCE) if r.get(excess_col) is not None]
        by_week: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_week.setdefault(r.get("iso_week") or iso_week_key(r.get("proposal_date")), []).append(r)

        ic_ai: List[float] = []
        ic_base: List[float] = []
        ic_diff: List[float] = []
        spreads: List[float] = []
        for week in sorted(by_week):
            cohort = by_week[week]
            if len(cohort) < MIN_PANEL_COHORT:
                continue
            outcome = [float(r[excess_col]) for r in cohort]
            signal = [
                _as_float(r.get("expected_excess")) if r.get("expected_excess") is not None
                else _as_float(r.get("win_probability"))
                for r in cohort
            ]
            ai = spearman_ic([x if x is not None else math.nan for x in signal], outcome)
            if ai is None:
                continue
            ic_ai.append(ai)
            base = spearman_ic(
                [_as_float(r.get("baseline_signal")) if r.get("baseline_signal") is not None else math.nan
                 for r in cohort],
                outcome,
            )
            if base is not None:
                ic_base.append(base)
                ic_diff.append(ai - base)
            ranked = sorted(
                (s_, o) for s_, o in zip(signal, outcome) if s_ is not None
            )
            decile = max(1, len(ranked) // 10)
            if len(ranked) >= 2 * decile:
                spreads.append(
                    sum(o for _, o in ranked[-decile:]) / decile - sum(o for _, o in ranked[:decile]) / decile
                )

        probs = [float(r["win_probability"]) for r in rows if r.get("win_probability") is not None]
        outs = [1 if float(r[excess_col]) > 0 else 0 for r in rows if r.get("win_probability") is not None]
        calibration = brier_skill(probs, outs)

        n_weeks = len(ic_ai)
        out: Dict[str, Any] = {
            "horizon": horizon,
            "n_names": len(rows),
            "n_cohorts_graded": len(by_week),
            "n_weeks": n_weeks,
            "required_weeks": MIN_WEEKS_FOR_VERDICT,
            "mean_ic": None,
            "ic_ci95": None,
            "baseline_mean_ic": None,
            "incremental_ic": None,
            "incremental_ci95": None,
            "spread_pp": None,
            "calibration": calibration,
            "reliability": _reliability_bins(probs, outs),
        }
        if ic_base:
            out["baseline_mean_ic"] = sum(ic_base) / len(ic_base)
        if n_weeks < MIN_WEEKS_FOR_VERDICT:
            out["verdict"] = {
                "status": "insufficient_data",
                "headline": (
                    f"INSUFFICIENT DATA — forecast panel: need {MIN_WEEKS_FOR_VERDICT}+ weekly cohorts matured at "
                    f"{horizon} (have {n_weeks}). Nothing is concluded from fewer."
                ),
            }
            return out

        nw = newey_west_mean(ic_ai, NW_LAGS)
        lo, hi = nw["ci95"]
        out["mean_ic"], out["ic_ci95"] = nw["mean"], (lo, hi)
        if len(ic_diff) >= MIN_WEEKS_FOR_VERDICT:
            inc = newey_west_mean(ic_diff, NW_LAGS)
            out["incremental_ic"], out["incremental_ci95"] = inc["mean"], inc["ci95"]
        if len(spreads) >= MIN_WEEKS_FOR_VERDICT:
            out["spread_pp"] = newey_west_mean(spreads, NW_LAGS)["mean"]

        evidence = (
            f"mean weekly rank IC {nw['mean']:+.3f} at {horizon} (95% CI {lo:+.3f} to {hi:+.3f}; "
            f"{n_weeks} weekly cohorts, {len(rows)} graded forecasts)"
        )
        if lo > 0:
            status, headline = "adds_value", f"✅ Forecast panel shows POSITIVE skill: {evidence}."
        elif hi < 0:
            status, headline = "anti_predictive", f"🛑 Forecast panel is ANTI-PREDICTIVE: {evidence}."
        elif hi < MEANINGFUL_PANEL_IC:
            status, headline = (
                "decoration",
                f"⚠️ Forecast panel shows NO MEANINGFUL SKILL: {evidence} — the interval rules out an IC of "
                f"{MEANINGFUL_PANEL_IC:.2f}.",
            )
        else:
            status, headline = (
                "inconclusive",
                f"❔ Forecast panel is INCONCLUSIVE: {evidence}; cannot rule out an IC of {MEANINGFUL_PANEL_IC:.2f}.",
            )
        out["verdict"] = {"status": status, "headline": headline, "mean_ic": nw["mean"], "ci95": (lo, hi),
                          "n_weeks": n_weeks}
        return out

    def calibration_state(self) -> Dict[str, Any]:
        """
        How far the AI's stated win probabilities may be trusted for sizing (pre-registered rule).

        Until the forecast panel has ``CALIBRATION_MIN_GRADED`` graded forecasts over at least
        ``CALIBRATION_MIN_COHORTS`` weekly cohorts with a POSITIVE Brier skill, every stated probability is shrunk ``CALIBRATION_SHRINK`` of the way toward 0.5 before it
        reaches the Kelly clamp (``shrink`` is the share of the stated edge that is kept: 0.5 keeps half).
        Fail-safe: any error returns the conservative shrink.
        """
        state: Dict[str, Any] = {
            "shrink": CALIBRATION_SHRINK, "n_graded": 0, "n_cohorts": 0, "brier_skill": None,
            "reason": f"no graded forecasts yet — stated probabilities keep {CALIBRATION_SHRINK:.0%} of their edge",
        }
        try:
            panel = self.compute_panel_scoreboard()
            cal = panel["calibration"]
            n, skill, cohorts = int(cal["n"]), cal["skill"], int(panel["n_cohorts_graded"])
            state["n_graded"], state["brier_skill"], state["n_cohorts"] = n, skill, cohorts
            if n >= CALIBRATION_MIN_GRADED and cohorts >= CALIBRATION_MIN_COHORTS and skill is not None and skill > 0:
                state["shrink"] = 1.0
                state["reason"] = (
                    f"panel Brier skill {skill:+.3f} on {n} graded forecasts over {cohorts} weekly cohorts — "
                    "stated probabilities used as is"
                )
            elif n >= CALIBRATION_MIN_GRADED and cohorts >= CALIBRATION_MIN_COHORTS:
                state["reason"] = (
                    f"panel Brier skill {skill if skill is None else format(skill, '+.3f')} on {n} graded forecasts "
                    f"is not positive — stated probabilities keep {CALIBRATION_SHRINK:.0%} of their edge"
                )
            else:
                state["reason"] = (
                    f"only {n}/{CALIBRATION_MIN_GRADED} graded forecasts over {cohorts}/{CALIBRATION_MIN_COHORTS} "
                    f"weekly cohorts — stated probabilities keep {CALIBRATION_SHRINK:.0%} of their edge"
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"calibration_state fell back to conservative shrink: {exc}")
        return state

    # ------------------------------------------------------------------ #
    # Asymmetry Hunter (Soros layer) — separate verdict + grading clock
    # ------------------------------------------------------------------ #
    def compute_asymmetry_scoreboard(self) -> Dict[str, Any]:
        """
        Soros-layer scoreboard: identical machinery, isolated to
        ``source='asymmetry_hunter'`` with a verdict worded for the overlay.
        """
        return self.compute_scoreboard(
            source="asymmetry_hunter",
            layer_label="Asymmetry Hunter (Soros layer)",
            simplify_hint="retire the Soros overlay",
        )

    def asymmetry_grading_clock(
        self, *, warehouse: Any = None, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        The operator's "is the Soros layer being graded yet, and when do we get a
        verdict?" clock. Factual, never raises.

        Reports: how many components are LIVE right now (delegated to
        ``compute_hunter_readiness`` — DRY), how many independent dossiers have
        matured at the primary horizon and across how many weekly cohorts, and a
        best-effort weeks-to-first-verdict estimate. The verdict needs the same
        pre-registered cohort rules as every other layer (``MIN_WEEKS_FOR_VERDICT`` /
        ``MIN_OBS_FOR_VERDICT``).
        """
        now = now or _utcnow()
        clock: Dict[str, Any] = {
            "required": MIN_OBS_FOR_VERDICT,
            "required_weeks": MIN_WEEKS_FOR_VERDICT,
            "weeks_matured": 0,
            "live_components": None,
            "min_components": None,
            "dossiers_logged": 0,
            "dossiers_scored": 0,
            "weeks_to_first_verdict": None,
            "note": "",
        }
        # Live component count (no run, no network).
        try:
            from modules.analytics.asymmetry_hunter import compute_hunter_readiness

            readiness = compute_hunter_readiness(warehouse=warehouse)
            clock["live_components"] = readiness.get("live_independent_count")
            clock["min_components"] = readiness.get("min_components")
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"asymmetry_grading_clock readiness skipped: {exc}")

        # Accrual: independent dossiers matured at the PRIMARY horizon, and the weekly cohorts they span.
        try:
            rows = self._query_rows(source="asymmetry_hunter")
            clock["dossiers_logged"] = len(rows)
            col = f"excess_{PRIMARY_HORIZON}"
            scored = [r for r in dedupe_observations(rows) if r.get(col) is not None]
            weeks = {r.get("iso_week") or iso_week_key(r.get("proposal_date")) for r in scored}
            clock["dossiers_scored"] = len(scored)
            clock["weeks_matured"] = len(weeks)
            remaining_obs = max(0, MIN_OBS_FOR_VERDICT - len(scored))
            remaining_weeks = max(0, MIN_WEEKS_FOR_VERDICT - len(weeks))

            if remaining_obs == 0 and remaining_weeks == 0:
                clock["weeks_to_first_verdict"] = 0
                clock["note"] = "Verdict available now — the Soros layer has been graded."
            else:
                # Estimate the logging rate from elapsed wall-clock since the first dossier. Dossiers
                # are rare by design, so this is genuinely an estimate; the floor is the maturation of
                # the primary horizon.
                dates = sorted(
                    d for d in (_parse_dt(r.get("proposal_date")) for r in rows) if d
                )
                maturation_weeks = HORIZONS[PRIMARY_HORIZON] / 7.0
                if len(dates) >= 2:
                    span_weeks = max((now - dates[0]).days / 7.0, 1e-6)
                    rate = len(dates) / span_weeks  # dossiers per week
                    if rate > 0:
                        weeks_needed = max(remaining_weeks, remaining_obs / rate) + maturation_weeks
                        clock["weeks_to_first_verdict"] = round(weeks_needed, 1)
                        clock["note"] = (
                            f"~{round(weeks_needed)} wk estimate at the current "
                            f"{rate:.2f} dossiers/wk cadence (dossiers are rare by design)."
                        )
                else:
                    clock["note"] = (
                        "Indeterminate — too few dossiers logged to estimate a cadence. "
                        "Dossiers are rare by design; the clock is data-limited, not "
                        "time-limited."
                    )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"asymmetry_grading_clock accrual skipped: {exc}")
        return clock

    def _render_asymmetry_clock_md(self, clock: Dict[str, Any]) -> List[str]:
        """Markdown lines for the grading-clock header block."""
        live = clock.get("live_components")
        minc = clock.get("min_components")
        lines = ["## ⏱️ Asymmetry Hunter — Grading Clock", ""]
        if live is not None:
            lines.append(
                f"- Components currently **LIVE: {live}**"
                + (f" / {minc} required to fire" if minc is not None else "")
            )
        lines.append(
            f"- Soros-layer dossiers logged: **{clock.get('dossiers_logged', 0)}**; independent, matured at "
            f"{PRIMARY_HORIZON}: **{clock.get('dossiers_scored', 0)} / {clock.get('required', MIN_OBS_FOR_VERDICT)}** "
            f"across **{clock.get('weeks_matured', 0)} / {clock.get('required_weeks', MIN_WEEKS_FOR_VERDICT)}** "
            f"weekly cohorts required for a verdict"
        )
        weeks = clock.get("weeks_to_first_verdict")
        if weeks == 0:
            lines.append("- **Verdict available now.**")
        elif weeks is not None:
            lines.append(f"- Estimated weeks to first verdict: **~{weeks}**")
        if clock.get("note"):
            lines.append(f"- _{clock['note']}_")
        lines.append("")
        return lines

    # ------------------------------------------------------------------ #
    # Verdict
    # ------------------------------------------------------------------ #
    def _verdict(
        self,
        rows: Sequence[Dict[str, Any]],
        per_horizon: Dict[str, Dict[str, Any]],
        *,
        layer_label: str = "MasterPrompt",
        simplify_hint: str = "Phases 2/4 should be simplified to the deterministic screener",
    ) -> Dict[str, Any]:
        """
        Pre-registered verdict for ONE layer (see the module docstring for the rules).

        Unit of analysis: the weekly cohort. Each independent observation's excess return over its own
        benchmark at the PRIMARY horizon (4w) is averaged within its ISO week; the verdict is the
        Newey-West mean of those weekly cohort means with a 95% interval. ``rows`` must already be
        de-duplicated (``dedupe_observations``). Statuses: ``insufficient_data``, ``adds_value``,
        ``anti_predictive``, ``decoration`` (interval rules out a meaningful edge), ``inconclusive``
        (interval includes both zero and a meaningful edge).
        """
        excess_col = f"excess_{PRIMARY_HORIZON}"
        pos_col = f"pos_ret_{PRIMARY_HORIZON}"
        by_week: Dict[str, List[float]] = {}
        paired_by_week: Dict[str, List[float]] = {}
        baseline = self._universe_baseline_by_week(PRIMARY_HORIZON)
        n_obs = 0
        beats = 0
        for r in rows:
            sign = _direction_sign(r.get("direction", ""))
            excess = r.get(excess_col)
            if sign is None or excess is None:
                continue
            week = r.get("iso_week") or iso_week_key(r.get("proposal_date"))
            if not week:
                continue
            by_week.setdefault(week, []).append(float(excess))
            n_obs += 1
            beats += 1 if float(excess) > 0 else 0
            pos, universe = r.get(pos_col), baseline.get(week)
            if pos is not None and universe is not None:
                paired_by_week.setdefault(week, []).append(float(pos) - sign * universe)

        n_weeks = len(by_week)
        base: Dict[str, Any] = {
            "primary_horizon": PRIMARY_HORIZON,
            "n": n_obs,
            "n_weeks": n_weeks,
            "required_weeks": MIN_WEEKS_FOR_VERDICT,
            "required_obs": MIN_OBS_FOR_VERDICT,
            "required": MIN_OBS_FOR_VERDICT,
            "beat_rate": (beats / n_obs) if n_obs else None,
        }
        if n_weeks < MIN_WEEKS_FOR_VERDICT or n_obs < MIN_OBS_FOR_VERDICT:
            return {
                **base,
                "status": "insufficient_data",
                "scored_max": n_obs,
                "headline": (
                    f"INSUFFICIENT DATA — {layer_label}: need {MIN_WEEKS_FOR_VERDICT}+ weekly cohorts and "
                    f"{MIN_OBS_FOR_VERDICT}+ independent observations matured at {PRIMARY_HORIZON} "
                    f"(have {n_weeks} weeks / {n_obs} observations). The scoreboard keeps accruing; "
                    "no verdict is drawn from a sample this small."
                ),
            }

        weekly = [sum(v) / len(v) for _, v in sorted(by_week.items())]
        nw = newey_west_mean(weekly, NW_LAGS)
        mean, (lo, hi) = nw["mean"], nw["ci95"]
        evidence = (
            f"mean weekly-cohort excess {mean:+.2f}pp over its regional benchmark at {PRIMARY_HORIZON} "
            f"(95% CI {lo:+.2f} to {hi:+.2f}pp; {n_weeks} weeks, {n_obs} independent observations)"
        )
        if lo > 0:
            status = "adds_value"
            headline = f"✅ {layer_label} shows a POSITIVE edge: {evidence}."
        elif hi < 0:
            status = "anti_predictive"
            headline = (
                f"🛑 {layer_label} is ANTI-PREDICTIVE: {evidence}. {simplify_hint[0].upper()}{simplify_hint[1:]}."
            )
        elif hi < MEANINGFUL_EXCESS_PP:
            status = "decoration"
            headline = (
                f"⚠️ {layer_label} shows NO MEANINGFUL EDGE: {evidence} — the interval rules out a "
                f"+{MEANINGFUL_EXCESS_PP:.1f}pp edge. On this evidence it is decoration: {simplify_hint}."
            )
        else:
            status = "inconclusive"
            headline = (
                f"❔ {layer_label} is INCONCLUSIVE: {evidence}. The interval includes zero but cannot "
                f"rule out a meaningful (+{MEANINGFUL_EXCESS_PP:.1f}pp) edge — keep accruing."
            )

        result = {
            **base,
            "status": status,
            "mean_excess_pp": round(mean, 4),
            "se_pp": round(nw["se"], 4),
            "ci95_pp": (round(lo, 4), round(hi, 4)),
            "t_stat": None if nw["t"] is None else round(nw["t"], 4),
            "p_value": None if nw["p"] is None else round(nw["p"], 4),
            "indistinguishable": status in ("decoration", "inconclusive"),
            "headline": headline,
        }
        paired_weekly = [sum(v) / len(v) for _, v in sorted(paired_by_week.items())]
        if len(paired_weekly) >= MIN_WEEKS_FOR_VERDICT:
            pnw = newey_west_mean(paired_weekly, NW_LAGS)
            result["vs_universe_baseline"] = {
                "mean_pp": round(pnw["mean"], 4),
                "ci95_pp": (round(pnw["ci95"][0], 4), round(pnw["ci95"][1], 4)),
                "n_weeks": len(paired_weekly),
            }
        return result

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def _meta_get(self, key: str) -> Optional[str]:
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT value FROM ai_scoreboard_meta WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ai_scoreboard_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def is_monthly_report_due(self, *, now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        last = self._meta_get("last_report_date")
        if not last:
            return True
        last_dt = _parse_dt(last)
        if last_dt is None:
            return True
        return (now - last_dt) >= timedelta(days=28)

    def generate_value_added_report(
        self, *, force: bool = False, now: Optional[datetime] = None
    ) -> Optional[str]:
        """
        Write the monthly "AI value-added report".

        Unless ``force`` is set, the report is written at most once every 28 days.
        Returns the report path, or ``None`` if not due / on failure.
        """
        now = now or _utcnow()
        try:
            if not force and not self.is_monthly_report_due(now=now):
                return None
            markdown = self.render_report_markdown(now=now)
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            path = self.reports_dir / f"ai_value_added_{now.strftime('%Y%m')}.md"
            path.write_text(markdown, encoding="utf-8")
            self._meta_set("last_report_date", now.isoformat())
            logger.info(f"🧮 Scoreboard: AI value-added report written → {path}")
            return str(path)
        except Exception as exc:
            logger.warning(f"AIScoreboard.generate_value_added_report failed: {exc}")
            return None

    def render_report_markdown(self, *, now: Optional[datetime] = None) -> str:
        """Render the AI value-added report as markdown (no side effects)."""
        now = now or _utcnow()
        layers = self.compute_layer_scoreboards()
        panel = self.compute_panel_scoreboard()
        calibration = self.calibration_state()
        all_rows = self._query_rows()
        lines: List[str] = []
        a = lines.append

        a("# AI Value-Added Report — ObliskIQ Judgment Scoreboard")
        a("")
        a(f"_Generated: {now.strftime('%Y-%m-%d %H:%M UTC')} · "
          f"System v{self._system_version()}_")
        a("")
        a("> **Question this report answers:** does any AI layer carry information about forward "
          "returns beyond luck? Each layer is judged separately, on independent observations, at one "
          f"pre-registered horizon ({PRIMARY_HORIZON}), against its own regional benchmark in USD.")
        a("")
        a(f"> **How to read a verdict.** Pre-registered, not tunable: ≥{MIN_WEEKS_FOR_VERDICT} weekly "
          f"cohorts and ≥{MIN_OBS_FOR_VERDICT} independent observations; Newey-West 95% interval on the "
          f"mean weekly excess return. *Decoration* only when the interval rules out +{MEANINGFUL_EXCESS_PP:.1f}pp; "
          "an interval that merely includes zero is *inconclusive*, never \"no skill\".")
        a("")

        # Grading clock — so the operator sees the Soros-layer accrual at a glance.
        try:
            for line in self._render_asymmetry_clock_md(self.asymmetry_grading_clock(now=now)):
                a(line)
        except Exception as exc:  # never let the clock break the report
            logger.debug(f"grading-clock header skipped: {exc}")

        # Verdicts first — they are the headline. One per layer; never pooled.
        a("## 🏁 Verdicts")
        a("")
        a(f"- **Forecast panel** (weekly, ~185 fixed names — the statistically powered test): "
          f"{panel['verdict']['headline']}")
        for key, label in (("phase2", "Phase 2"), ("phase4", "Phase 4"), ("asymmetry_hunter", "Asymmetry Hunter")):
            a(f"- **{label}**: {layers[key]['verdict']['headline']}")
        a("")

        # Forecast panel detail.
        a("## 🔭 Weekly forecast panel")
        a("")
        a(f"_{panel['n_names']} graded forecasts across {panel['n_weeks']} weekly cohorts at {panel['horizon']}._")
        a("")
        a(self._panel_table(panel))
        a("")
        a(self._reliability_table(panel.get("reliability") or []))
        a("")
        a("**Sizing calibration:** " + calibration["reason"] + ".")
        a("")

        # Per-layer horizon tables (independent observations only).
        for key, label in (("phase2", "Phase 2 — ticker proposals"),
                           ("phase4", "Phase 4 — trade recommendations"),
                           ("asymmetry_hunter", "🦅 Asymmetry Hunter (Soros layer)")):
            sub = layers[key]
            a(f"## {label}")
            a("")
            a(f"_{sub['total_proposals']} logged → {sub['independent_observations']} independent "
              f"(one per ticker/direction/ISO week)._")
            a("")
            a(self._horizon_table(sub["per_horizon"]))
            vs = sub["verdict"].get("vs_universe_baseline")
            if vs:
                a("")
                a(f"vs the equal-weight forecast-panel universe over the same weeks: mean "
                  f"{vs['mean_pp']:+.2f}pp (95% CI {vs['ci95_pp'][0]:+.2f} to {vs['ci95_pp'][1]:+.2f}, "
                  f"{vs['n_weeks']} weeks).")
            a("")
        a("- **Beat-benchmark** = share of independent directional observations whose realised return "
          "(in the proposed direction, in USD) exceeded their regional benchmark over the same window. "
          "Descriptive only — the verdict uses the cohort test above.")
        a("- **Avg excess** = mean (position return − benchmark return), percentage points.")
        a("- **IC(conviction)** = pooled rank correlation of conviction with realised return. Descriptive: "
          "observations overlap in time, so its p-value is optimistic.")
        a("")

        # Regime accuracy.
        reg = layers["phase2"]["regime_accuracy"]
        a("## Regime-call accuracy")
        a("")
        if reg["n"]:
            a(f"- AI regime calls graded: **{reg['n']}**")
            a(f"- Correct (matched realized {REGIME_HORIZON} SPY regime): "
              f"**{reg['correct']}** ({reg['accuracy']:.1%})")
        else:
            a("- No regime calls have matured to the grading horizon yet.")
        a("")
        a(f"_Realized regime is the market's verdict: SPY's {REGIME_HORIZON} forward "
          f"return labelled bull (> +{REGIME_BULL_THRESHOLD:.0f}%), "
          f"bear (< {REGIME_BEAR_THRESHOLD:.0f}%), or choppy (between). A full "
          f"historical macro snapshot would let us substitute the deterministic "
          f"regime detector here._")
        a("")

        # Per-model / per-prompt breakdown (A/B, includes which fallback provider answered).
        a("## Per-prompt-version and per-model breakdown (A/B)")
        a("")
        a(self._prompt_version_table(dedupe_observations([r for r in all_rows if r.get("source") != PANEL_SOURCE])))
        a("")
        a("_Each prompt edit changes the hash, and `model` is the provider:model that actually answered "
          "(fallbacks included). Compare rows only once each has a verdict-sized sample._")
        a("")

        a("---")
        a(f"_Total rows logged: {len(all_rows)}. Pre-registered rules: primary horizon {PRIMARY_HORIZON}, "
          f"≥{MIN_WEEKS_FOR_VERDICT} weeks, ≥{MIN_OBS_FOR_VERDICT} observations, "
          f"meaningful excess +{MEANINGFUL_EXCESS_PP:.1f}pp, meaningful panel IC {MEANINGFUL_PANEL_IC:.2f}._")
        a("")
        return "\n".join(lines)

    # --- table renderers ------------------------------------------------ #
    def _horizon_table(self, per_horizon: Dict[str, Dict[str, Any]]) -> str:
        head = (
            "| Horizon | n | Beat-benchmark | Profitable | Avg excess | "
            "Avg pos. ret | IC(conviction) | IC p |"
        )
        sep = "|---|---|---|---|---|---|---|---|"
        rows = [head, sep]
        for h in HORIZONS:
            m = per_horizon[h]
            rows.append(
                f"| {h} | {m['n']} | {_pct(m['beat_spy_rate'])} | "
                f"{_pct(m['profitable_rate'])} | {_pp(m['avg_excess_return'])} | "
                f"{_pp(m['avg_position_return'])} | {_num(m['ic'])} | "
                f"{_num(m['ic_p_value'])} |"
            )
        return "\n".join(rows)

    def _panel_table(self, panel: Dict[str, Any]) -> str:
        def ci(value: Any) -> str:
            return f"{value[0]:+.3f} to {value[1]:+.3f}" if value else "—"

        cal = panel["calibration"]
        out = ["| Metric | Value |", "|---|---|"]
        out.append(f"| Mean weekly rank IC (AI) | {_num(panel['mean_ic'])} (95% CI {ci(panel['ic_ci95'])}) |")
        out.append(f"| Mean weekly rank IC (12-1 momentum baseline) | {_num(panel['baseline_mean_ic'])} |")
        out.append(f"| Incremental IC over momentum | {_num(panel['incremental_ic'])} (95% CI {ci(panel['incremental_ci95'])}) |")
        out.append(f"| Top-decile minus bottom-decile spread | {_pp(panel['spread_pp'])} |")
        out.append(f"| Brier skill vs base rate | {_num(cal['skill'])} (n={cal['n']}, base rate {_pct(cal['base_rate'])}, "
                   f"mean stated {_pct(cal['mean_stated'])}) |")
        return "\n".join(out)

    def _reliability_table(self, bins: Sequence[Dict[str, Any]]) -> str:
        if not bins:
            return "_No graded forecast probabilities yet._"
        out = ["| Stated probability | n | Mean stated | Observed frequency |", "|---|---|---|---|"]
        for b in bins:
            out.append(f"| {b['bin']} | {b['n']} | {_pct(b['mean_stated'])} | {_pct(b['observed'])} |")
        return "\n".join(out)

    def _prompt_version_table(self, rows: Sequence[Dict[str, Any]]) -> str:
        by_hash: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_hash.setdefault(r.get("prompt_hash") or "unknown", []).append(r)
        head = (
            "| Prompt hash | Model | Observations | Beat-benchmark (8w) | "
            "Avg excess (8w) | IC(conviction, 8w) |"
        )
        sep = "|---|---|---|---|---|---|"
        out = [head, sep]
        for phash, group in sorted(by_hash.items()):
            m = self.compute_metrics(group, REGIME_HORIZON)
            models = sorted({str(r.get("model_name") or "?") for r in group})
            model_str = ", ".join(models)[:40]
            out.append(
                f"| `{phash}` | {model_str} | {len(group)} | "
                f"{_pct(m['beat_spy_rate'])} | {_pp(m['avg_excess_return'])} | "
                f"{_num(m['ic'])} |"
            )
        return "\n".join(out)


# ---------------------------------------------------------------------- #
# Module-level helpers
# ---------------------------------------------------------------------- #
def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _reliability_bins(probs: Sequence[float], outcomes: Sequence[int], bins: int = 5) -> List[Dict[str, Any]]:
    """Reliability table: stated-probability bucket -> (n, mean stated, observed frequency)."""
    out: List[Dict[str, Any]] = []
    if not probs:
        return out
    edges = [i / bins for i in range(bins + 1)]
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, p in enumerate(probs) if (lo <= p < hi) or (hi == 1.0 and p == 1.0)]
        if idx:
            out.append({
                "bin": f"{lo:.1f}-{hi:.1f}",
                "n": len(idx),
                "mean_stated": sum(probs[i] for i in idx) / len(idx),
                "observed": sum(outcomes[i] for i in idx) / len(idx),
            })
    return out


def dedupe_observations(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One INDEPENDENT observation per (layer, ticker, direction, ISO week), earliest wins.

    The same name proposed on Monday's pre-open, Monday's pre-close and Tuesday's run is ONE bet seen
    three times: its forward returns overlap almost completely, so counting it three times triples n
    without adding information. The layer key is ``(source, phase)`` so a name proposed by both the
    AI and the Hunter is kept once in each layer's own verdict.
    """
    seen: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for r in sorted(rows, key=lambda x: str(x.get("proposal_date") or "")):
        week = r.get("iso_week") or iso_week_key(r.get("proposal_date"))
        key = (r.get("source"), r.get("phase"), r.get("ticker"), r.get("direction"), week)
        seen.setdefault(key, r)
    return list(seen.values())


def _horizons_for(row: Dict[str, Any]) -> Dict[str, int]:
    """Horizons a row is graded at: the forecast panel uses a reduced set (price-lookup budget)."""
    if row.get("source") == PANEL_SOURCE:
        return {h: HORIZONS[h] for h in PANEL_HORIZONS}
    return HORIZONS


def iso_week_key(value: Any) -> str:
    """``YYYY-Www`` ISO-week cohort key for a timestamp (UTC); '' if unparseable."""
    dt = _parse_dt(value)
    if dt is None:
        return ""
    iso = dt.astimezone(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _run_id_from_ts(ts: str) -> str:
    dt = _parse_dt(ts) or _utcnow()
    return dt.strftime("%Y%m%d_%H%M%S")


def _classify_realized_regime(spy_forward_return_pct: float) -> str:
    if spy_forward_return_pct > REGIME_BULL_THRESHOLD:
        return "bull"
    if spy_forward_return_pct < REGIME_BEAR_THRESHOLD:
        return "bear"
    return "choppy"


def _pct(value: Optional[float]) -> str:
    return f"{value:.1%}" if isinstance(value, (int, float)) else "—"


def _pp(value: Optional[float]) -> str:
    return f"{value:+.2f}pp" if isinstance(value, (int, float)) else "—"


def _num(value: Optional[float]) -> str:
    return f"{value:.3f}" if isinstance(value, (int, float)) else "—"
