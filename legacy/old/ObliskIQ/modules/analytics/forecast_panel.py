"""
Weekly Forecast Panel (v6.16.0) — the statistically powered test of the AI
==========================================================================

Why this exists
---------------
The scoreboard grades the AI's *trade proposals*: a handful a week, overlapping, long-biased. Even
done correctly, ~3,000 independent proposals would be needed to resolve an information coefficient
of 0.05 — about a decade at the rate the pipeline produces them. That is not a feedback loop.

The panel replaces "wait for trades" with "ask the same model the same question about the SAME ~185
liquid names every week" and grade the cross-section:

* a FIXED, hashed, pre-registered universe (``build_panel_universe`` — the strategy mega-caps, the
  regional ETFs/ADRs and the core/sector ETFs the system already tracks);
* per name the AI states ``p_outperform_4w`` (probability its 4-week USD return beats ACWI) and
  ``expected_excess_4w_pct``;
* alongside, the deterministic 12-1 momentum value is logged, so the question "does the AI add
  anything over the oldest factor in the book?" is answered by incremental IC, not by assertion;
* outcomes arrive 28 days later and are scored by ``AIScoreboard`` (``source='forecast_panel'``).

Power (honest): one cohort's rank IC has standard error ~ 1/sqrt(N) = 0.07 for N=188. Consecutive weekly
cohorts share 3 of their 4 weeks of returns, so ~4 of them carry the information of one: a year is ~13
independent cohorts (SE ~ 0.02, resolving a true IC of ~0.06 at 80% power) and the 26-week minimum ~6-7
(resolving ~0.08). That is the difference between "months" and "a decade" for finding out whether the AI is
useful at all; an IC below ~0.05 stays invisible at this scale (and is not worth trading on anyway).

Honesty rules (Dalio: log everything; Simons: no p-hacking)
-----------------------------------------------------------
* The AI sees only trailing price statistics and a one-line market context — never realised outcomes.
* A name without enough price history is NOT forecast (never filled in); a stale feed skips the whole
  week instead of forecasting from old prices.
* A cohort is logged only when >= ``MIN_COHORT_COVERAGE`` of the universe was forecast; a partial run
  retries on the next scheduled run of the same week.
* Probabilities are clamped to [0.02, 0.98] and validated by the same retry-once contract protocol as
  Phase 2/4, across the operator's provider chain.
* Everything is non-fatal: the panel can never break the trading pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from modules.analytics.ai_scoreboard import AIScoreboard, iso_week_key
from modules.config.global_vehicles import GLOBAL_BENCHMARK, REGION_ADRS, REGION_ETFS
from modules.config.strategy_universe import CORE_ETFS, SECTOR_ETFS, STRATEGY_UNIVERSE

logger = logging.getLogger(__name__)

PANEL_SCHEMA_VERSION = "1.0"

#: Names per AI call. ~40 compact rows is ~2.5K prompt tokens: fits every provider's window.
BATCH_SIZE = 40
#: Share of a batch's tickers that must come back valid, else the batch is rejected (retry-once).
MIN_BATCH_COVERAGE = 0.8
#: Share of the universe that must be forecast before the cohort is logged.
MIN_COHORT_COVERAGE = 0.6
#: Pause between batches: the panel shares per-minute free-tier limits with the trading phases.
BATCH_PAUSE_S = 4.0
#: Two failed batches in a row means the chain is exhausted (quota / outage): stop spending calls.
MAX_CONSECUTIVE_FAILURES = 2
#: Trading days of history needed to compute a name's features / the 12-1 momentum value.
MIN_HISTORY_DAYS = 64
MOMENTUM_HISTORY_DAYS = 253
#: Latest price bar may be at most this many calendar days old (a long weekend + a holiday).
MAX_FEED_AGE_DAYS = 5

P_MIN, P_MAX = 0.02, 0.98
EXCESS_LIMIT_PCT = 50.0

_TICKER = re.compile(r"^[A-Z0-9.\-]{1,12}$")


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
def build_panel_universe() -> Tuple[str, ...]:
    """The fixed, sorted panel universe (ACWI is the yardstick, so it is excluded)."""
    names = set(STRATEGY_UNIVERSE) | set(CORE_ETFS) | set(SECTOR_ETFS)
    for etfs in REGION_ETFS.values():
        names.update(etfs)
    for adrs in REGION_ADRS.values():
        names.update(adrs)
    names.discard(GLOBAL_BENCHMARK)
    bad = sorted(n for n in names if not _TICKER.match(n))
    if bad:  # a typo in a seed list must fail loudly, not silently shrink the universe
        raise ValueError(f"invalid tickers in the panel universe: {bad}")
    return tuple(sorted(names))


def universe_hash(universe: Sequence[str]) -> str:
    """Stable id of a universe + schema version (stored on every row; a change is visible in reports)."""
    payload = f"{PANEL_SCHEMA_VERSION}|" + "\n".join(sorted(universe))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Features (deterministic, trailing only)
# --------------------------------------------------------------------------- #
@dataclass
class FeatureLoad:
    """Result of loading panel features: either rows, or a reason the week is skipped."""

    status: str  # "ok" | "stale" | "unavailable"
    reason: str = ""
    asof: Optional[date] = None
    rows: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    benchmark_price: Optional[float] = None


def compute_panel_features(close: Any) -> Tuple[Dict[str, Dict[str, Any]], Optional[date]]:
    """
    Per-ticker trailing statistics from a frame of daily closes (columns = tickers).

    Returns ``({ticker: features}, latest_bar_date)``. A ticker with fewer than ``MIN_HISTORY_DAYS``
    bars is omitted (not forecast). ``mom_12_1`` is None when fewer than ``MOMENTUM_HISTORY_DAYS`` bars
    exist; that name is still forecast, without a baseline value.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    latest: Optional[date] = None
    for ticker in close.columns:
        series = close[ticker].dropna()
        n = len(series)
        if n < MIN_HISTORY_DAYS:
            continue
        last = float(series.iloc[-1])
        if not math.isfinite(last) or last <= 0:
            continue
        daily = series.pct_change().dropna()
        vol = float(daily.iloc[-60:].std() * math.sqrt(252.0) * 100.0) if len(daily) >= 20 else None
        mom = None
        if n >= MOMENTUM_HISTORY_DAYS:
            mom = (float(series.iloc[-22]) / float(series.iloc[-253]) - 1.0) * 100.0
        bar = series.index[-1]
        bar_date = bar.date() if hasattr(bar, "date") else bar
        latest = bar_date if latest is None or bar_date > latest else latest
        rows[str(ticker)] = {
            "entry_price": last,
            "ret_1m": round((last / float(series.iloc[-22]) - 1.0) * 100.0, 1),
            "ret_3m": round((last / float(series.iloc[-64]) - 1.0) * 100.0, 1),
            "mom_12_1": None if mom is None else round(mom, 1),
            "vol_60d": None if vol is None or not math.isfinite(vol) else round(vol, 1),
            "off_52w_high": round((last / float(series.iloc[-252:].max()) - 1.0) * 100.0, 1),
        }
    return rows, latest


def load_panel_features(universe: Sequence[str], now: datetime) -> FeatureLoad:
    """Download ~15 months of closes for the universe + ACWI in ONE request and compute features."""
    try:
        import yfinance as yf

        symbols = list(universe) + [GLOBAL_BENCHMARK]
        frame = yf.download(
            symbols, period="15mo", interval="1d", auto_adjust=True, progress=False,
            group_by="column", threads=True,
        )
        if frame is None or frame.empty:
            return FeatureLoad("unavailable", "price download returned no data")
        close = frame["Close"]
    except Exception as exc:
        return FeatureLoad("unavailable", f"price download failed: {type(exc).__name__}: {exc}"[:300])

    rows, latest = compute_panel_features(close.drop(columns=[GLOBAL_BENCHMARK], errors="ignore"))
    if not rows or latest is None:
        return FeatureLoad("unavailable", "no instrument had enough price history")
    age = (now.date() - latest).days
    if age > MAX_FEED_AGE_DAYS:
        return FeatureLoad("stale", f"latest price bar is {age} days old (limit {MAX_FEED_AGE_DAYS})", asof=latest)
    benchmark = None
    if GLOBAL_BENCHMARK in close.columns and not close[GLOBAL_BENCHMARK].dropna().empty:
        benchmark = float(close[GLOBAL_BENCHMARK].dropna().iloc[-1])
    return FeatureLoad("ok", asof=latest, rows=rows, benchmark_price=benchmark)


# --------------------------------------------------------------------------- #
# Prompt + contract
# --------------------------------------------------------------------------- #
def build_panel_prompt(
    batch: Dict[str, Dict[str, Any]], *, as_of: date, context: Optional[Dict[str, Any]] = None
) -> str:
    """The self-contained forecasting prompt for one batch of names."""
    instruments = [
        {
            "ticker": ticker,
            "ret_1m": feat["ret_1m"],
            "ret_3m": feat["ret_3m"],
            "mom_12_1": feat["mom_12_1"],
            "vol_60d": feat["vol_60d"],
            "off_52w_high": feat["off_52w_high"],
        }
        for ticker, feat in batch.items()
    ]
    return (
        f"You are a systematic equity/ETF forecaster. Today is {as_of.isoformat()}.\n\n"
        "TASK: for EACH instrument below estimate (a) the probability that its total return over the "
        f"next 4 weeks (28 calendar days), in USD, will EXCEED that of {GLOBAL_BENCHMARK} (global equities), "
        f"and (b) its expected 4-week excess return over {GLOBAL_BENCHMARK} in percent.\n\n"
        "RULES:\n"
        "- Use ONLY the data given and your general knowledge as of the date above. Do not invent prices, "
        "earnings or news.\n"
        "- In a typical 4-week window roughly 45-50% of instruments beat the benchmark. Stay near 0.50 unless "
        "you have a specific reason; reserve probabilities outside 0.35-0.65 for strong, specific evidence.\n"
        f"- Probabilities must lie in [{P_MIN}, {P_MAX}]; expected excess in [-25, 25].\n"
        "- If you have no view on an instrument, answer p_outperform_4w 0.5 and expected_excess_4w_pct 0.\n"
        "- Answer EVERY ticker listed, exactly once, spelled as given.\n\n"
        f"MARKET CONTEXT: {json.dumps(context or {}, separators=(',', ':'), sort_keys=True)}\n\n"
        "INSTRUMENTS (ret_1m, ret_3m, mom_12_1 [return from 12 to 1 months ago], off_52w_high in %; "
        "vol_60d = annualised volatility %; null = unavailable):\n"
        f"{json.dumps(instruments, separators=(',', ':'))}\n\n"
        "OUTPUT (JSON only, no prose, no markdown fences):\n"
        f'{{"schema_version":"{PANEL_SCHEMA_VERSION}","forecasts":['
        '{"ticker":"AAA","p_outperform_4w":0.50,"expected_excess_4w_pct":0.0}]}'
    )


def parse_panel_response(text: str, expected: Sequence[str]) -> Dict[str, Dict[str, float]]:
    """
    Validate one batch response. Raises ``ValueError`` (the retry-once protocol's signal) on a malformed
    document or when fewer than ``MIN_BATCH_COVERAGE`` of the expected tickers come back valid.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not valid JSON: {exc}") from exc
    items = data.get("forecasts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError('top-level object with a "forecasts" array is required')

    wanted = {t.upper() for t in expected}
    out: Dict[str, Dict[str, float]] = {}
    problems: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            problems.append("non-object forecast entry")
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        if ticker not in wanted or ticker in out:
            continue  # unknown or duplicate: ignored, not an error
        p, ex = item.get("p_outperform_4w"), item.get("expected_excess_4w_pct")
        if (
            isinstance(p, bool) or isinstance(ex, bool)
            or not isinstance(p, (int, float)) or not isinstance(ex, (int, float))
            or not math.isfinite(p) or not math.isfinite(ex)
            or not 0.0 <= p <= 1.0 or abs(ex) > EXCESS_LIMIT_PCT
        ):
            problems.append(f"{ticker}: p_outperform_4w must be in [0,1] and expected_excess_4w_pct within ±{EXCESS_LIMIT_PCT:g}")
            continue
        out[ticker] = {"p_outperform": min(P_MAX, max(P_MIN, float(p))), "expected_excess": float(ex)}

    if len(out) < MIN_BATCH_COVERAGE * len(wanted):
        missing = sorted(wanted - set(out))[:8]
        detail = f"; e.g. {problems[0]}" if problems else ""
        raise ValueError(
            f"only {len(out)} of {len(wanted)} tickers have a valid forecast (missing {missing}){detail}"
        )
    return out


# --------------------------------------------------------------------------- #
# Weekly run
# --------------------------------------------------------------------------- #
def _chunks(items: Sequence[str], size: int) -> List[List[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def run_weekly_forecast_panel(
    scoreboard: AIScoreboard,
    analyst: Any,
    *,
    now: Optional[datetime] = None,
    feature_loader: Optional[Callable[[Sequence[str], datetime], FeatureLoad]] = None,
    context: Optional[Dict[str, Any]] = None,
    batch_size: int = BATCH_SIZE,
    pause_s: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    run_id: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Log this ISO week's forecast cohort, at most once. Never raises; returns a status dict.

    ``analyst`` needs ``run_contract(what, prompt, parse) -> (parsed, provenance)`` (``AnalystRouter``).
    Statuses: ``logged``, ``skipped`` (already done this week / no analyst / no features / stale feed),
    ``incomplete`` (too few names forecast — nothing logged, retried on the next run of the week),
    ``failed`` (unexpected error).
    """
    now = now or datetime.now(timezone.utc)
    week = iso_week_key(now)
    result: Dict[str, Any] = {"status": "skipped", "week": week, "reason": "", "n_forecast": 0}
    try:
        if not force and scoreboard.panel_cohort_exists(week):
            result["reason"] = "cohort for this ISO week is already logged"
            return result
        runner = getattr(analyst, "run_contract", None)
        if not callable(runner):
            result["reason"] = "analyst has no run_contract()"
            return result

        universe = build_panel_universe()
        features = (feature_loader or load_panel_features)(universe, now)
        result["n_universe"] = len(universe)
        if features.status != "ok":
            result["reason"] = f"features {features.status}: {features.reason}"
            logger.warning("Forecast panel skipped for %s: %s", week, result["reason"])
            return result

        tickers = sorted(features.rows)
        as_of = features.asof or now.date()
        forecasts: List[Dict[str, Any]] = []
        failures = 0
        consecutive = 0
        for index, batch in enumerate(_chunks(tickers, batch_size)):
            pause = BATCH_PAUSE_S if pause_s is None else pause_s
            if index and pause > 0:
                sleep(pause)  # stay under free-tier per-minute limits shared with the trading run
            prompt = build_panel_prompt({t: features.rows[t] for t in batch}, as_of=as_of, context=context)
            try:
                parsed, provenance = runner(
                    "Forecast panel", prompt, lambda text, wanted=tuple(batch): parse_panel_response(text, wanted)
                )
            except Exception as exc:  # AllProvidersFailedError, ModelError, anything: keep going, count it
                failures += 1
                consecutive += 1
                logger.warning("Forecast panel batch failed (%d/%d): %s", consecutive, MAX_CONSECUTIVE_FAILURES, str(exc)[:200])
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    break
                continue
            consecutive = 0
            label = (provenance or {}).get("label")
            for ticker, fc in parsed.items():
                row = features.rows[ticker]
                forecasts.append({
                    "ticker": ticker,
                    "p_outperform": fc["p_outperform"],
                    "expected_excess": fc["expected_excess"],
                    "baseline_signal": row.get("mom_12_1"),
                    "entry_price": row["entry_price"],
                    "model_name": label,
                })

        result["n_forecast"] = len(forecasts)
        result["failed_batches"] = failures
        coverage = len(forecasts) / len(universe)
        result["coverage"] = round(coverage, 3)
        if coverage < MIN_COHORT_COVERAGE:
            result["status"] = "incomplete"
            result["reason"] = (
                f"only {len(forecasts)}/{len(universe)} names forecast (need {MIN_COHORT_COVERAGE:.0%}); nothing logged"
            )
            logger.warning("Forecast panel incomplete for %s: %s", week, result["reason"])
            return result

        written = scoreboard.log_forecast_panel(
            forecasts,
            universe_hash=universe_hash(universe),
            run_id=run_id or f"panel_{week}",
            as_of=now,
            benchmark_ticker=GLOBAL_BENCHMARK,
            benchmark_price=features.benchmark_price,
        )
        result["status"] = "logged"
        result["rows_written"] = written
        logger.info("🔭 Forecast panel %s: logged %d/%d forecasts (%d failed batches)", week, written, len(universe), failures)
        return result
    except Exception as exc:  # pragma: no cover - defensive: the panel must never break a run
        logger.warning("Forecast panel failed (non-fatal): %s", exc)
        result["status"] = "failed"
        result["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        return result
