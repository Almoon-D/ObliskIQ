"""
Asymmetry Hunter v1 — the deterministic Soros overlay.
=====================================================

Finds asymmetric, episodic opportunities across markets WITHOUT letting an LLM
hallucinate reflexivity twice a day. The hunter is DETERMINISTIC: it fuses
signals the pipeline ALREADY computes into a scored watchlist. The AI may later
narrate the candidates the math surfaced — it never invents candidates.

George Soros: "Markets are driven by perception. Find where perception will break."

Design contract
---------------
* ``hunt(phase1_data)`` is PURE, SYNCHRONOUS and NEVER raises (it wraps its body
  and degrades to ``{"status": "error"}``). It reads already-computed Phase 1 dict
  keys, so it is fully testable with synthetic fixtures and adds no network I/O.
* Six component scores (a-e weighted, f is a window/gate). A component backed by
  mocked / approximate / immature data DEGRADES to 0 weight with an honest status
  flag — never a fabricated number (CLAUDE.md anti-fabrication rule).
* An asset becomes a candidate only when >= ``min_components`` INDEPENDENT
  components agree on the SAME direction. Most runs emit ZERO — silence is a
  feature, not a bug.
* Risk rails (5% convexity sleeve, 2% per dossier, options never < 30 DTE) are
  enforced in code via ``RiskEnforcer``, never in prose. The hunter NEVER sizes
  up a Phase 4 position; it is a separate sleeve.
* Every dossier is graded by the AI Scoreboard (``source='asymmetry_hunter'``,
  prompt-hash = ``weights_hash``) — the same standard as the MasterPrompt.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Enums & dataclasses
# =============================================================================
class ComponentDirection(Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class ComponentScore:
    """One component's read on one asset."""

    name: str  # crowding | vol_mispricing | regime_fragility | cross_asset | sentiment_extreme
    score: float  # 0-100 magnitude of the extreme
    direction: ComponentDirection
    is_extreme: bool  # crossed the component's extreme threshold
    weight: float  # effective weight used (0.0 when degraded)
    available: bool  # False => degraded to 0 weight
    status: str  # ok | unavailable | warming_up | approximation | placeholder | error
    detail: str  # human one-liner for the dossier / audit

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 2),
            "direction": self.direction.value,
            "is_extreme": self.is_extreme,
            "weight": round(self.weight, 4),
            "available": self.available,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DefinedRiskExpression:
    """How to express a dossier with DEFINED risk, consistent with our constraints."""

    instrument_type: str  # long_call | long_put | etf
    underlying: str
    target_delta: Optional[float]  # 0.6-0.7 for options, None for ETF
    expiry_months: int  # 3-6
    expiry_days: int  # used by the RiskEnforcer DTE rail
    min_open_interest: int  # OI gate (config.risk.options_min_open_interest)
    is_liquid_underlying: bool  # tighter half-spread tier
    max_premium_pct: float  # this dossier's share of the convexity sleeve
    note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_type": self.instrument_type,
            "underlying": self.underlying,
            "target_delta": self.target_delta,
            "expiry_months": self.expiry_months,
            "expiry_days": self.expiry_days,
            "min_open_interest": self.min_open_interest,
            "is_liquid_underlying": self.is_liquid_underlying,
            "max_premium_pct": round(self.max_premium_pct, 4),
            "note": self.note,
        }


@dataclass(frozen=True)
class AsymmetryDossier:
    """A single asymmetric setup the math surfaced."""

    asset: str  # mapped ETF ticker (also the tradable)
    mapped_ticker: str
    asset_class: str
    direction: ComponentDirection
    composite_score: float  # 0-100
    contributing_components: Tuple[str, ...]
    consensus_belief: str
    why_fragile: str
    feedback_loop: str
    catalyst_window: str
    invalidation_level: str
    regime_call: str
    defined_risk: DefinedRiskExpression
    component_scores: Tuple[ComponentScore, ...]
    markdown: str

    def to_dict(self) -> Dict[str, Any]:
        """Serializable mirror used by the report, the Phase 2 prompt and the scoreboard."""
        return {
            "asset": self.asset,
            "mapped_ticker": self.mapped_ticker,
            "asset_class": self.asset_class,
            "direction": self.direction.value.upper(),
            "composite_score": round(self.composite_score, 2),
            "contributing_components": list(self.contributing_components),
            "consensus_belief": self.consensus_belief,
            "why_fragile": self.why_fragile,
            "feedback_loop": self.feedback_loop,
            "catalyst_window": self.catalyst_window,
            "invalidation_level": self.invalidation_level,
            "regime_call": self.regime_call,
            "defined_risk": self.defined_risk.to_dict(),
            # Promoted to top-level for the AI Scoreboard logger.
            "max_premium_pct": round(self.defined_risk.max_premium_pct, 4),
            "component_scores": [c.to_dict() for c in self.component_scores],
            "markdown": self.markdown,
        }


# =============================================================================
# Asset universe (contract -> liquid ETF, with risk polarity)
# =============================================================================
@dataclass(frozen=True)
class AssetSpec:
    """A tradable ETF and how the math maps signals onto it."""

    etf: str
    asset_class: str
    polarity: int  # +1 risk-on, -1 safe-haven
    cot_aliases: Tuple[str, ...]  # COT contract codes & human names we accept
    commodity_symbols: Tuple[str, ...]  # phase1 commodity_futures keys (e.g. "CL=F")


# Primary mappings named in the prompt (secondary ETFs documented in
# docs/ASYMMETRY_HUNTER.md). Polarity drives how market-wide (risk-on/off)
# signals become a per-asset LONG/SHORT vote.
ASSET_SPECS: Tuple[AssetSpec, ...] = (
    AssetSpec("USO", "crude", +1, ("CL", "Crude Oil", "WTI Crude Oil"), ("CL=F",)),
    AssetSpec("GLD", "gold", -1, ("GC", "Gold"), ("GC=F",)),
    AssetSpec("IEF", "rates", -1, ("US10Y", "10-Year Treasury", "10-Year T-Note"), ()),
    AssetSpec("TLT", "rates", -1, ("US30Y", "30-Year Treasury", "30-Year T-Bond", "Ultra T-Bond"), ()),
    AssetSpec("FXE", "fx", +1, ("EUR", "Euro", "Euro FX"), ()),
    AssetSpec("SPY", "equity", +1, ("SP500", "S&P 500", "E-mini S&P 500"), ()),
    # v6.5.0: GDELT-mapping universe. Defense + regional equities carry no COT /
    # commodity coverage, so crowding (a) and commodity (d) self-degrade for them
    # — they earn candidacy from market-wide signals (regime/cross/sentiment) plus
    # the geo (h) and prediction-market (g) evidence streams.
    AssetSpec("ITA", "defense", +1, (), ()),
    AssetSpec("EWG", "regional_equity", +1, (), ()),
    AssetSpec("EWJ", "regional_equity", +1, (), ()),
    AssetSpec("EWZ", "regional_equity", +1, (), ()),
)

# Mapped ETFs with liquid LISTED options (get an option expression; others get
# the ETF itself). ITA/EWZ carry liquid options; EWG/EWJ option chains are thin,
# so those degrade to a direct-ETF expression.
OPTIONABLE_ETFS: frozenset = frozenset(
    {"USO", "GLD", "IEF", "TLT", "FXE", "SPY", "ITA", "EWZ"}
)


# =============================================================================
# Helpers
# =============================================================================
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _risk_off_direction(polarity: int) -> ComponentDirection:
    """Risk-off: short risk-on assets, long safe havens."""
    return ComponentDirection.SHORT if polarity > 0 else ComponentDirection.LONG


def _risk_on_direction(polarity: int) -> ComponentDirection:
    """Risk-on: long risk-on assets, short safe havens."""
    return ComponentDirection.LONG if polarity > 0 else ComponentDirection.SHORT


def _neutral_component(name: str, status: str, detail: str) -> ComponentScore:
    """A degraded / non-firing component that casts no directional vote."""
    return ComponentScore(
        name=name,
        score=0.0,
        direction=ComponentDirection.NEUTRAL,
        is_extreme=False,
        weight=0.0,
        available=status == "ok",
        status=status,
        detail=detail,
    )


def load_event_calendar(path: Optional[Path] = None) -> Dict[str, List[str]]:
    """
    Load the hand-editable static macro event calendar.

    Returns ``{event_type: [ISO dates]}`` or ``{}`` if the file is missing /
    malformed (the catalyst component then degrades honestly).
    """
    if path is None:
        path = Path(__file__).resolve().parents[2] / "config" / "event_calendar.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        events = payload.get("events", {})
        if isinstance(events, dict):
            return {str(k): list(v) for k, v in events.items()}
        return {}
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug(f"event_calendar load skipped: {exc}")
        return {}


# =============================================================================
# Market-wide signals (computed once per run)
# =============================================================================
@dataclass(frozen=True)
class _MarketSignal:
    """A market-wide read shared across assets, mapped to per-asset direction later."""

    name: str
    score: float  # 0-100 magnitude
    is_extreme: bool
    risk_direction: Optional[str]  # "off" | "on" | None
    available: bool
    status: str
    detail: str

    def for_asset(self, polarity: int) -> ComponentScore:
        if not self.is_extreme or self.risk_direction is None:
            return _neutral_component(self.name, self.status, self.detail)
        direction = (
            _risk_off_direction(polarity)
            if self.risk_direction == "off"
            else _risk_on_direction(polarity)
        )
        return ComponentScore(
            name=self.name,
            score=self.score,
            direction=direction,
            is_extreme=True,
            weight=0.0,  # set later during composite renormalization
            available=True,
            status=self.status,
            detail=self.detail,
        )


def _regime_fragility_signal(phase1_data: Dict[str, Any], cfg: Any) -> _MarketSignal:
    """(c) Regime fragility from the leading-indicator transition detector."""
    name = "regime_fragility"
    rt = phase1_data.get("regime_transition")
    if not isinstance(rt, dict) or rt.get("status") == "error":
        return _MarketSignal(name, 0.0, False, None, False, "error",
                             "regime_transition unavailable")
    score = _as_float(rt.get("composite_score"))
    trend = str(rt.get("score_trend", "stable"))
    state = str(rt.get("state", "unknown"))
    if score is None:
        return _MarketSignal(name, 0.0, False, None, False, "error",
                             "regime_transition has no composite_score")
    is_extreme = score >= cfg.regime_fragility_extreme and trend == "rising"
    detail = (
        f"Regime transition score {score:.0f}/100 ({state}, trend {trend})"
        + (" — fragile & rising" if is_extreme else "")
    )
    return _MarketSignal(name, score, is_extreme, "off" if is_extreme else None,
                        True, "ok", detail)


def _cross_asset_signal(phase1_data: Dict[str, Any], cfg: Any) -> _MarketSignal:
    """(d, market-wide) Credit (HY OAS) vs VIX z-score divergence."""
    name = "cross_asset"
    div = phase1_data.get("cross_asset_divergence")
    if not isinstance(div, dict) or div.get("status") not in ("success", "ok"):
        return _MarketSignal(name, 0.0, False, None, False, "error",
                             "cross_asset_divergence unavailable")
    divergence = _as_float(div.get("divergence"))
    hy_z = _as_float(div.get("hy_oas_z"))
    vix_z = _as_float(div.get("vix_z"))
    if divergence is None:
        return _MarketSignal(name, 0.0, False, None, False, "error",
                             "cross_asset_divergence has no divergence")
    is_extreme = abs(divergence) >= cfg.cross_asset_z_div
    # |divergence| in [threshold, 2*threshold] -> 60-100 magnitude.
    score = min(100.0, 50.0 + (abs(divergence) / max(cfg.cross_asset_z_div, 1e-9)) * 25.0)
    detail = (
        f"Credit/VIX divergence {divergence:+.2f}σ "
        f"(HY z={hy_z:+.2f}, VIX z={vix_z:+.2f})"
        if hy_z is not None and vix_z is not None
        else f"Credit/VIX divergence {divergence:+.2f}σ"
    )
    return _MarketSignal(name, score, is_extreme, "off" if is_extreme else None,
                        True, "ok", detail)


def _extract_overall_sentiment(phase1_data: Dict[str, Any]) -> Optional[float]:
    """
    Best-effort overall sentiment on a 0-100 scale from REAL sources only.

    Avoids the hardcoded Fear&Greed (=50) and put/call (=0.85) placeholders in
    Phase 1's ``sentiment`` dict. Prefers the SentimentMatrix overall cell, then
    a composite social score. Returns None if no trustworthy reading exists.
    """
    regional = phase1_data.get("regional_sentiment")
    if isinstance(regional, dict):
        for key in ("overall_sentiment", "global_sentiment", "average_sentiment"):
            val = _as_float(regional.get(key))
            if val is not None:
                return val if val > 1.5 else (val + 1.0) * 50.0  # accept 0-100 or -1..1
        overall = regional.get("Overall") or regional.get("overall")
        if isinstance(overall, dict):
            val = _as_float(overall.get("sentiment_score"))
            if val is not None:
                return val

    sentiment = phase1_data.get("sentiment")
    if isinstance(sentiment, dict):
        for key in ("composite_score", "overall_sentiment", "composite_sentiment"):
            val = _as_float(sentiment.get(key))
            if val is not None:
                return val if val > 1.5 else (val + 1.0) * 50.0
    return None


def _sentiment_signal(phase1_data: Dict[str, Any], cfg: Any) -> _MarketSignal:
    """(e) Sentiment extreme from real matrix / social readings (placeholders excluded)."""
    name = "sentiment_extreme"
    score_0_100 = _extract_overall_sentiment(phase1_data)
    if score_0_100 is None:
        return _MarketSignal(name, 0.0, False, None, False, "placeholder",
                             "No trustworthy sentiment reading (F&G/put-call are placeholders)")
    if score_0_100 >= cfg.sentiment_high_pctl:
        # Euphoria -> contrarian risk-off.
        magnitude = min(100.0, 50.0 + (score_0_100 - 50.0))
        return _MarketSignal(name, magnitude, True, "off", True, "ok",
                            f"Sentiment {score_0_100:.0f}/100 — euphoric (contrarian risk-off)")
    if score_0_100 <= cfg.sentiment_low_pctl:
        # Panic / capitulation -> contrarian risk-on.
        magnitude = min(100.0, 50.0 + (50.0 - score_0_100))
        return _MarketSignal(name, magnitude, True, "on", True, "ok",
                            f"Sentiment {score_0_100:.0f}/100 — panic (contrarian risk-on)")
    return _MarketSignal(name, score_0_100, False, None, True, "ok",
                        f"Sentiment {score_0_100:.0f}/100 — not extreme")


def _polymarket_divergence_signal(phase1_data: Dict[str, Any], cfg: Any) -> _MarketSignal:
    """
    (g) Polymarket divergence — the crowd's prediction-market probability vs our
    own data-implied stance. Phase 1 precomputes ``polymarket_divergence`` (signed
    ``divergence = our_prob - market_prob`` with a risk ``direction``), exactly as
    ``cross_asset_divergence`` is precomputed; here we just read it. Evidence
    stream, not a standalone trigger — it still needs >= min_components agreement.
    """
    name = "polymarket_divergence"
    pm = phase1_data.get("polymarket_divergence")
    if not isinstance(pm, dict) or pm.get("status") != "success":
        return _MarketSignal(name, 0.0, False, None, False, "unavailable",
                             "Polymarket divergence unavailable")
    divergence = _as_float(pm.get("divergence"))
    direction = pm.get("direction")  # "off" | "on" | None
    if divergence is None or direction not in ("off", "on"):
        return _MarketSignal(name, 0.0, False, None, True, "ok",
                             "Polymarket has no data-anchored divergence")
    is_extreme = abs(divergence) >= cfg.polymarket_divergence_extreme
    # |divergence| in [threshold, 2*threshold] -> 60-100 magnitude.
    score = min(100.0, 50.0 + (abs(divergence) / max(cfg.polymarket_divergence_extreme, 1e-9)) * 25.0)
    detail = pm.get("detail") or f"Polymarket vs our data diverge {divergence:+.0%}"
    return _MarketSignal(name, score, is_extreme, direction if is_extreme else None,
                        True, "ok", detail)


def load_gdelt_mapping(path: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """
    Load the static region -> asset -> stance map for the GDELT component.

    Returns ``{REGION: {ETF: "long"|"short"}}`` or ``{}`` if the file is missing /
    malformed (the geo component then finds no mapping and casts no vote).
    """
    if path is None:
        path = Path(__file__).resolve().parents[2] / "config" / "gdelt_regions.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        asset_map = payload.get("asset_map", {})
        if isinstance(asset_map, dict):
            return {
                str(region): {str(etf): str(stance) for etf, stance in (mapping or {}).items()}
                for region, mapping in asset_map.items()
                if isinstance(mapping, dict)
            }
        return {}
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug(f"gdelt_regions mapping load skipped: {exc}")
        return {}


# =============================================================================
# Per-asset components
# =============================================================================
def _crowding_component(spec: AssetSpec, phase1_data: Dict[str, Any], cfg: Any) -> ComponentScore:
    """(a) CFTC COT crowding percentile -> contrarian direction (v6.10.0 — REAL feed).

    The percentile is the real trailing-3y large-spec rank from ``cot_history``.
    BOTH tails fire (symmetric): high pctl (crowded long) → contrarian short, low
    pctl (crowded short) → contrarian long. A degraded / warming-up feed (percentile
    None or a non-extreme reading) self-zeros for that asset.
    """
    name = "crowding"
    cot = phase1_data.get("cot_positioning")
    if not isinstance(cot, dict) or cot.get("status") != "success":
        return _neutral_component(name, "unavailable",
                                  "COT feed unavailable/degraded — 0 weight")
    contracts = cot.get("contracts") or []
    match = None
    for c in contracts:
        if str(c.get("contract", "")) in spec.cot_aliases:
            match = c
            break
    if match is None:
        return _neutral_component(name, "ok", f"No COT contract mapped to {spec.etf}")
    pctl = _as_float(match.get("percentile"))
    signal = str(match.get("signal", "neutral"))
    # Symmetric extreme gate: high tail (>= crowding_extreme_pctl, crowded long) OR
    # low tail (<= 100 - crowding_extreme_pctl, crowded short). None => warming up.
    low_tail = 100.0 - cfg.crowding_extreme_pctl
    if pctl is None or (pctl < cfg.crowding_extreme_pctl and pctl > low_tail):
        return _neutral_component(name, "ok",
                                  f"COT {match.get('contract')} percentile "
                                  f"{pctl if pctl is None else round(pctl)} — not crowded")
    direction = ComponentDirection.NEUTRAL
    if signal == "contrarian_long":
        direction = ComponentDirection.LONG
    elif signal == "contrarian_short":
        direction = ComponentDirection.SHORT
    if direction is ComponentDirection.NEUTRAL:
        return _neutral_component(name, "ok", "COT crowded but no contrarian direction")
    # Score = depth into the tail (0-100) so the deeper extreme ranks higher in the
    # composite, symmetric across both tails.
    tail_score = pctl if pctl >= cfg.crowding_extreme_pctl else (100.0 - pctl)
    return ComponentScore(name, tail_score, direction, True, 0.0, True, "ok",
                          f"COT {match.get('contract')} crowded at {pctl:.0f}th pctl "
                          f"({signal})")


def _vol_component(
    spec: AssetSpec, phase1_data: Dict[str, Any], cfg: Any, warehouse: Any,
    as_of: Optional[date] = None,
) -> ComponentScore:
    """(b) IV-rank / skew extreme from warehouse snapshots. Warms up for 60 days."""
    name = "vol_mispricing"
    if warehouse is None:
        return _neutral_component(name, "warming_up", "IV warehouse unavailable — 0 weight")
    try:
        hist = warehouse.get_iv_history(spec.etf, end_date=as_of) if as_of else warehouse.get_iv_history(spec.etf)
    except Exception as exc:  # pragma: no cover - defensive
        return _neutral_component(name, "error", f"IV history read failed: {exc}")
    if hist is None or len(hist) == 0:
        return _neutral_component(name, "warming_up", f"No IV snapshots for {spec.etf} yet")
    last = hist.iloc[-1]
    iv_rank = _as_float(last.get("iv_rank"))
    skew = _as_float(last.get("skew_25d"))
    if iv_rank is None:
        return _neutral_component(name, "warming_up",
                                  f"{spec.etf} IV rank warming up (< {cfg.min_iv_history_days} snapshots)")
    skew_extreme = skew is not None and abs(skew) >= cfg.skew_extreme
    if iv_rank >= cfg.iv_rank_high:
        # Rich vol = stress -> risk-off vote on this asset.
        return ComponentScore(name, iv_rank, _risk_off_direction(spec.polarity), True, 0.0, True,
                              "ok", f"{spec.etf} IV rank {iv_rank:.0f} — rich vol / stress"
                              + (f", 25Δ skew {skew:+.1f}" if skew_extreme else ""))
    if iv_rank <= cfg.iv_rank_low:
        # Cheap vol enables a convex expression but casts no directional vote.
        return ComponentScore(name, 50.0, ComponentDirection.NEUTRAL, False, 0.0, True, "ok",
                              f"{spec.etf} IV rank {iv_rank:.0f} — cheap convexity (expression enabler)")
    return _neutral_component(name, "ok", f"{spec.etf} IV rank {iv_rank:.0f} — not extreme")


def _commodity_component(spec: AssetSpec, phase1_data: Dict[str, Any], cfg: Any) -> ComponentScore:
    """(d, per-asset) Commodity term-structure flip for commodity-mapped ETFs (approximation)."""
    name = "cross_asset"
    if not spec.commodity_symbols:
        return _neutral_component(name, "ok", "No commodity term structure for this asset")
    cf = phase1_data.get("commodity_futures")
    if not isinstance(cf, dict) or cf.get("status") != "success":
        return _neutral_component(name, "approximation", "Commodity futures unavailable")
    commodities = cf.get("commodities") or {}
    match = None
    for sym in spec.commodity_symbols:
        if sym in commodities:
            match = commodities[sym]
            break
    if match is None:
        return _neutral_component(name, "ok", "No commodity curve matched")
    regime = str(match.get("regime", "flat"))
    slope = _as_float(match.get("curve_slope")) or 0.0
    # Backwardation (negative slope) is bullish the commodity; contango bearish.
    if "backwardation" in regime:
        direction = ComponentDirection.LONG
    elif "contango" in regime:
        direction = ComponentDirection.SHORT
    else:
        return _neutral_component(name, "approximation",
                                  f"{spec.etf} term structure {regime} — flat")
    is_extreme = "strong" in regime
    magnitude = min(100.0, 50.0 + abs(slope) * 2.5)
    detail = f"{spec.etf} term structure {regime} (slope {slope:+.1f}%, MA-approximation)"
    return ComponentScore(name, magnitude, direction, is_extreme, 0.0, True, "approximation", detail)


def _gdelt_component(
    spec: AssetSpec, phase1_data: Dict[str, Any], cfg: Any, mapping: Dict[str, Dict[str, str]]
) -> ComponentScore:
    """
    (h) GDELT geopolitical anomaly -> per-asset stance via the static region map.

    For each anomalous region (volume z >= ``gdelt_anomaly_z`` and tone negative),
    look up this asset's stance in ``config/gdelt_regions.json``. Assets the map
    doesn't touch get a neutral (no-vote) component; conflicting region stances
    also neutralize. Honest degradation: missing feed -> 0 weight; warming-up
    accumulator -> warming_up.
    """
    name = "gdelt_pulse"
    gp = phase1_data.get("gdelt_pulse")
    if not isinstance(gp, dict) or gp.get("status") not in ("success", "warming_up"):
        return _neutral_component(name, "unavailable", "GDELT pulse unavailable — 0 weight")
    if gp.get("status") == "warming_up":
        return _neutral_component(name, "warming_up", "GDELT anomaly stats warming up")
    regions = gp.get("regions") or {}
    votes: List[Tuple[ComponentDirection, float]] = []
    detail_bits: List[str] = []
    for region, rdata in regions.items():
        if not isinstance(rdata, dict) or not rdata.get("anomalous"):
            continue
        vz = _as_float(rdata.get("volume_z"))
        tone = _as_float(rdata.get("tone"))
        if vz is None or vz < cfg.gdelt_anomaly_z or tone is None or tone > cfg.gdelt_tone_max:
            continue
        stance = (mapping.get(region) or {}).get(spec.etf)
        if stance not in ("long", "short"):
            continue
        direction = ComponentDirection.LONG if stance == "long" else ComponentDirection.SHORT
        votes.append((direction, vz))
        detail_bits.append(f"{region} anomaly z={vz:.1f}, tone {tone:+.0f} → {stance}")
    if not votes:
        return _neutral_component(name, "ok", f"No mapped GDELT anomaly for {spec.etf}")
    directions = {d for d, _ in votes}
    if len(directions) > 1:
        return _neutral_component(name, "ok", f"Conflicting GDELT anomalies for {spec.etf}")
    direction = votes[0][0]
    z = max(z for _, z in votes)
    score = min(100.0, 50.0 + (z / max(cfg.gdelt_anomaly_z, 1e-9)) * 25.0)
    return ComponentScore(name, score, direction, True, 0.0, True, "ok", "; ".join(detail_bits))


def _catalyst_window(calendar: Dict[str, List[str]], cfg: Any, today: date) -> Tuple[bool, str]:
    """(f) Days to the next FOMC/CPI/NFP — a window/gate, not a directional vote."""
    nearest_name = None
    nearest_days: Optional[int] = None
    for event_type, dates in calendar.items():
        for ds in dates:
            try:
                d = date.fromisoformat(str(ds))
            except ValueError:
                continue
            delta = (d - today).days
            if delta < 0:
                continue
            if nearest_days is None or delta < nearest_days:
                nearest_days = delta
                nearest_name = event_type
    if nearest_days is None:
        return False, "No upcoming catalyst on the static calendar"
    proximate = nearest_days <= cfg.catalyst_window_days
    return proximate, f"{nearest_name} in {nearest_days} day(s)"


# =============================================================================
# Dossier construction
# =============================================================================
def _build_defined_risk(
    spec: AssetSpec, direction: ComponentDirection, max_premium_pct: float
) -> DefinedRiskExpression:
    """Reuse our option constraints: 0.6-0.7 delta, 3-6mo, OI gate, sleeve-share premium."""
    try:
        from modules.config.settings import config as _cfg

        min_oi = int(_cfg.risk.options_min_open_interest)
    except Exception:  # pragma: no cover - defensive
        min_oi = 1000
    try:
        from modules.analytics.transaction_costs import LIQUID_OPTIONS_UNDERLYINGS

        is_liquid = spec.etf in LIQUID_OPTIONS_UNDERLYINGS
    except Exception:  # pragma: no cover - defensive
        is_liquid = spec.etf == "SPY"

    if spec.etf in OPTIONABLE_ETFS and direction in (ComponentDirection.LONG, ComponentDirection.SHORT):
        instrument = "long_call" if direction is ComponentDirection.LONG else "long_put"
        return DefinedRiskExpression(
            instrument_type=instrument,
            underlying=spec.etf,
            target_delta=0.65,  # mid of the 0.6-0.7 ITM band
            expiry_months=4,  # within the 3-6 month band, comfortably > 30 DTE
            expiry_days=120,
            min_open_interest=min_oi,
            is_liquid_underlying=is_liquid,
            max_premium_pct=max_premium_pct,
            note=(
                f"ITM {instrument.split('_')[1]} ~0.65Δ, ~4mo, OI > {min_oi}; "
                f"select strike with bs_delta in [0.60, 0.70]"
            ),
        )
    return DefinedRiskExpression(
        instrument_type="etf",
        underlying=spec.etf,
        target_delta=None,
        expiry_months=0,
        expiry_days=0,
        min_open_interest=0,
        is_liquid_underlying=is_liquid,
        max_premium_pct=max_premium_pct,
        note=f"Hold {spec.etf} directly (no listed-option expression)",
    )


def _invalidation_level(
    spec: AssetSpec, direction: ComponentDirection, warehouse: Any,
    as_of: Optional[date] = None,
) -> str:
    """A price/level that kills the thesis — recent swing extreme if priced, else qualitative."""
    if warehouse is not None:
        try:
            df = warehouse.get_ticker_prices(spec.etf, end_date=as_of) if as_of else warehouse.get_ticker_prices(spec.etf)
            if df is not None and len(df) >= 20:
                window = df.tail(60)
                if direction is ComponentDirection.SHORT:
                    level = float(window["high"].max())
                    return f"Thesis invalid above {spec.etf} {level:.2f} (60-day swing high)"
                level = float(window["low"].min())
                return f"Thesis invalid below {spec.etf} {level:.2f} (60-day swing low)"
        except Exception:  # pragma: no cover - defensive
            pass
    if direction is ComponentDirection.SHORT:
        return f"Thesis invalid if {spec.etf} breaks to new highs / positioning normalises"
    return f"Thesis invalid if {spec.etf} breaks to new lows / positioning normalises"


def _render_markdown(dossier_fields: Dict[str, Any]) -> str:
    dr = dossier_fields["defined_risk"]
    comps = "; ".join(
        f"{c['name']}({c['direction']}, {c['score']:.0f})"
        for c in dossier_fields["component_scores"]
        if c["is_extreme"] and c["weight"] > 0
    )
    return (
        f"### {dossier_fields['mapped_ticker']} — {dossier_fields['direction']} "
        f"(score {dossier_fields['composite_score']:.0f}/100)\n"
        f"- **Consensus belief:** {dossier_fields['consensus_belief']}\n"
        f"- **Why fragile:** {dossier_fields['why_fragile']}\n"
        f"- **Feedback loop:** {dossier_fields['feedback_loop']}\n"
        f"- **Catalyst:** {dossier_fields['catalyst_window']}\n"
        f"- **Invalidation:** {dossier_fields['invalidation_level']}\n"
        f"- **Expression:** {dr['instrument_type']} on {dr['underlying']} "
        f"(max premium {dr['max_premium_pct']:.1%} of portfolio) — {dr['note']}\n"
        f"- **Contributing components:** {comps}\n"
    )


def _build_dossier(
    spec: AssetSpec,
    direction: ComponentDirection,
    contributors: List[ComponentScore],
    composite: float,
    catalyst_text: str,
    regime_call: str,
    max_premium_pct: float,
    warehouse: Any,
    as_of: Optional[date] = None,
) -> AsymmetryDossier:
    dir_word = "downside" if direction is ComponentDirection.SHORT else "upside"
    consensus_belief = (
        f"The crowd is leaning the other way on {spec.etf} "
        f"({', '.join(c.name for c in contributors)} aligned {direction.value})."
    )
    why_fragile = " ".join(c.detail for c in contributors)
    feedback_loop = (
        f"If {spec.etf} starts to move {dir_word}, the same crowding/positioning that "
        f"built the consensus unwinds and accelerates the move."
    )
    defined_risk = _build_defined_risk(spec, direction, max_premium_pct)
    invalidation = _invalidation_level(spec, direction, warehouse, as_of)

    dossier = AsymmetryDossier(
        asset=spec.etf,
        mapped_ticker=spec.etf,
        asset_class=spec.asset_class,
        direction=direction,
        composite_score=composite,
        contributing_components=tuple(c.name for c in contributors),
        consensus_belief=consensus_belief,
        why_fragile=why_fragile,
        feedback_loop=feedback_loop,
        catalyst_window=catalyst_text,
        invalidation_level=invalidation,
        regime_call=regime_call,
        defined_risk=defined_risk,
        component_scores=tuple(contributors),
        markdown="",
    )
    fields = dossier.to_dict()
    markdown = _render_markdown(fields)
    return AsymmetryDossier(**{**dossier.__dict__, "markdown": markdown})


# =============================================================================
# Entry point
# =============================================================================
def hunt(
    phase1_data: Dict[str, Any],
    *,
    config: Any = None,
    enforcer: Any = None,
    warehouse: Any = None,
    today: Optional[date] = None,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Run the deterministic Asymmetry Hunter over a Phase 1 intelligence dict.

    Args:
        phase1_data: the Phase 1 results dict (read in place; never mutated).
        config: an AsymmetryConfig (defaults to ``settings.config.asymmetry``).
        enforcer: a RiskEnforcer (defaults to a fresh one) for the convexity rails.
        warehouse: a DataWarehouse for IV/price reads (lazily constructed if None).
        today: override for the catalyst calendar (defaults to UTC today).

    Returns:
        ``AsymmetryWatchlist`` as a serializable dict. Never raises.
    """
    try:
        cfg = config
        if cfg is None:
            from modules.config.settings import config as _settings_config

            cfg = _settings_config.asymmetry

        if enforcer is None:
            try:
                from modules.utils.risk_enforcer import RiskEnforcer

                enforcer = RiskEnforcer()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"RiskEnforcer unavailable, rails skipped: {exc}")

        if warehouse is None:
            try:
                from modules.data.warehouse import DataWarehouse

                warehouse = DataWarehouse()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"DataWarehouse unavailable, IV/price reads degrade: {exc}")

        run_today = today or datetime.now(timezone.utc).date()

        # --- Market-wide signals (computed once) ---
        regime_sig = _regime_fragility_signal(phase1_data, cfg)
        cross_sig = _cross_asset_signal(phase1_data, cfg)
        sentiment_sig = _sentiment_signal(phase1_data, cfg)
        polymarket_sig = _polymarket_divergence_signal(phase1_data, cfg)  # (g)
        gdelt_map = load_gdelt_mapping()  # (h) static region -> asset stance
        calendar = load_event_calendar()
        catalyst_proximate, catalyst_text = _catalyst_window(calendar, cfg, run_today)
        regime_call = "unknown"
        rt = phase1_data.get("regime_transition")
        if isinstance(rt, dict) and rt.get("state"):
            regime_call = str(rt.get("state"))

        weights = cfg.weights()
        degraded: Dict[str, str] = {}
        raw_candidates: List[Tuple[AssetSpec, ComponentDirection, List[ComponentScore], float]] = []

        for spec in ASSET_SPECS:
            # Per-asset components.
            crowding = _crowding_component(spec, phase1_data, cfg)
            vol = _vol_component(spec, phase1_data, cfg, warehouse, as_of)
            commodity = _commodity_component(spec, phase1_data, cfg)
            regime = regime_sig.for_asset(spec.polarity)
            sentiment = sentiment_sig.for_asset(spec.polarity)

            # cross_asset = stronger of (per-asset commodity flip, market-wide divergence).
            market_cross = cross_sig.for_asset(spec.polarity)
            cross = commodity if (commodity.is_extreme and not market_cross.is_extreme) else market_cross
            if market_cross.is_extreme and commodity.is_extreme and commodity.score > market_cross.score:
                cross = commodity

            # (g) prediction-market divergence (polarity-mapped) and
            # (h) GDELT geo anomaly (static region->asset stance).
            polymarket = polymarket_sig.for_asset(spec.polarity)
            gdelt = _gdelt_component(spec, phase1_data, cfg, gdelt_map)

            components = [crowding, vol, regime, cross, sentiment, polymarket, gdelt]
            for c in components:
                if not c.available:
                    degraded.setdefault(c.name, c.status)

            # Keep only firing components that cast a directional vote.
            firing = [
                c for c in components
                if c.available and c.is_extreme and c.direction is not ComponentDirection.NEUTRAL
            ]
            if not firing:
                continue

            # Bucket by direction; need >= min_components agreeing.
            by_dir: Dict[ComponentDirection, List[ComponentScore]] = {}
            for c in firing:
                by_dir.setdefault(c.direction, []).append(c)
            best_dir = max(by_dir, key=lambda d: len(by_dir[d]))
            agreeing = by_dir[best_dir]
            # Ties (LONG count == SHORT count) fail the consistency rule.
            counts = sorted((len(v) for v in by_dir.values()), reverse=True)
            if len(counts) >= 2 and counts[0] == counts[1]:
                continue
            # De-duplicate by component name (independence): keep the strongest per name.
            unique: Dict[str, ComponentScore] = {}
            for c in agreeing:
                if c.name not in unique or c.score > unique[c.name].score:
                    unique[c.name] = c
            contributors = list(unique.values())
            if len(contributors) < cfg.min_components:
                continue

            # Composite = weighted mean over contributors, weights renormalised.
            total_w = sum(weights.get(c.name, 0.0) for c in contributors)
            if total_w <= 0:
                continue
            contributors = [
                ComponentScore(**{**c.__dict__, "weight": weights.get(c.name, 0.0) / total_w})
                for c in contributors
            ]
            composite = sum(c.score * c.weight for c in contributors)
            raw_candidates.append((spec, best_dir, contributors, composite))

        # Rank, cap, size against the convexity sleeve.
        raw_candidates.sort(key=lambda t: t[3], reverse=True)
        capped = raw_candidates[: cfg.max_candidates]
        n = len(capped)
        dossiers: List[AsymmetryDossier] = []
        if n > 0:
            sleeve = getattr(getattr(enforcer, "limits", None), "total_convexity_sleeve_pct", 0.05)
            per_cap = getattr(getattr(enforcer, "limits", None), "per_dossier_convexity_cap", 0.02)
            # Floor the per-dossier share to the precision the dossier persists
            # (4 dp) so the summed, rounded premiums never tip over the sleeve and
            # spuriously reject the last (equally-ranked) candidate at the rail.
            share = math.floor(min(per_cap, sleeve / n) * 10000) / 10000
            for spec, direction, contributors, composite in capped:
                dossiers.append(
                    _build_dossier(spec, direction, contributors, composite, catalyst_text,
                                   regime_call, share, warehouse, as_of)
                )

        # Enforce the rails in code (defense in depth).
        approved_dicts: List[Dict[str, Any]] = [d.to_dict() for d in dossiers]
        if enforcer is not None and approved_dicts:
            approved, rejected = enforcer.validate_convexity_sleeve(approved_dicts)
            if rejected:
                for r in rejected:
                    logger.info(
                        f"Asymmetry dossier {r.get('mapped_ticker')} rejected by rails: "
                        f"{r.get('rejection_reason')}"
                    )
            approved_dicts = approved

        watchlist = {
            "status": "ok",
            "generated_at": _now_iso(),
            "weights_hash": cfg.weights_hash(),
            "assets_evaluated": len(ASSET_SPECS),
            "candidates_found": len(approved_dicts),
            "catalyst_window": catalyst_text,
            "catalyst_proximate": catalyst_proximate,
            "degraded_components": [{"name": k, "status": v} for k, v in sorted(degraded.items())],
            "candidates": approved_dicts,
        }
        return watchlist

    except Exception as exc:  # never abort Phase 1
        logger.warning(f"AsymmetryHunter.hunt failed (non-fatal): {exc}")
        return {
            "status": "error",
            "error": str(exc),
            "generated_at": _now_iso(),
            "candidates": [],
            "candidates_found": 0,
        }


# =============================================================================
# Operator-facing readout
# =============================================================================
def format_hunter_status(watchlist: Dict[str, Any]) -> str:
    """Render an Asymmetry Hunter watchlist as a human-readable terminal block.

    Mirrors ``paper_gate.format_gate_status``: this is what ``main.py
    hunter-status`` prints for a non-coder operator. It reads the serialized
    watchlist dict (the same one persisted to
    ``reports/dossiers/latest_watchlist.json`` each run), so it never runs the
    pipeline and never makes a network call. Tolerant of partial/old dicts.
    """
    w = watchlist or {}
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("ASYMMETRY HUNTER — READINESS")
    lines.append("=" * 60)

    status = w.get("status", "unknown")
    if status == "error":
        lines.append("Status: ⚠️  ERROR (degraded to empty watchlist — non-fatal)")
        if w.get("error"):
            lines.append(f"Reason: {w['error']}")
        lines.append(f"Generated: {w.get('generated_at', '—')}")
        lines.append("=" * 60)
        return "\n".join(lines)

    degraded = w.get("degraded_components", []) or []
    n_degraded = len(degraded)
    n_candidates = int(w.get("candidates_found", 0) or 0)

    # Readiness headline: warming-up vs fully armed.
    if n_degraded == 0:
        lines.append("Readiness: ✅ FULLY ARMED — all components nominal (full weight)")
    else:
        lines.append(
            f"Readiness: ⏳ WARMING UP — {n_degraded} component(s) degraded to 0 weight"
        )

    lines.append(f"Generated: {w.get('generated_at', '—')}")
    lines.append(f"Weights hash: {w.get('weights_hash', '—')}")
    lines.append(f"Assets evaluated: {w.get('assets_evaluated', '—')}")
    lines.append(f"Candidates found: {n_candidates}")

    catalyst = w.get("catalyst_window")
    if catalyst:
        proximate = " [PROXIMATE]" if w.get("catalyst_proximate") else ""
        lines.append(f"Catalyst window: {catalyst}{proximate}")

    lines.append("")
    lines.append("Component readiness:")
    if n_degraded == 0:
        lines.append("  ✅ All weighted components nominal.")
    else:
        for comp in degraded:
            name = comp.get("name", "?")
            cstatus = comp.get("status", "?")
            lines.append(f"  ⏳ {name:<18} {cstatus}")
        lines.append("  (components not listed above are nominal / full weight)")

    lines.append("")
    lines.append("Candidates:")
    candidates = w.get("candidates", []) or []
    if not candidates:
        lines.append("  (none — silence is a feature; most runs emit zero)")
    else:
        for c in candidates:
            asset = c.get("mapped_ticker") or c.get("asset", "?")
            direction = str(c.get("direction", "?")).upper()
            score = c.get("composite_score", 0)
            contribs = ", ".join(c.get("contributing_components", []) or [])
            lines.append(
                f"  • {asset:<6} {direction:<5} score {float(score):.0f}/100  [{contribs}]"
            )

    lines.append("=" * 60)
    return "\n".join(lines)


# =============================================================================
# Live readiness (pure status query — `main.py hunter-status`)
# =============================================================================
# These functions inspect the CURRENT warm-up state of every component directly
# (warehouse counts, config gates, file presence) WITHOUT running the pipeline
# and WITHOUT any network I/O. This is the operator's honest "can the Hunter
# fire?" readout — distinct from ``format_hunter_status`` above, which renders the
# LAST run's persisted watchlist.

READY_LIVE = "LIVE"
READY_WARMING = "WARMING-UP"
READY_MOCKED = "MOCKED-OFF"
READY_APPROX = "APPROXIMATION"
READY_PLACEHOLDER = "PLACEHOLDER"

_STATE_ICON = {
    READY_LIVE: "✅",
    READY_WARMING: "⏳",
    READY_MOCKED: "🚫",
    READY_APPROX: "≈",
    READY_PLACEHOLDER: "◌",
}

#: How recent a geo/prediction-market snapshot must be to count as "fresh" — a
#: few days covers weekends and a single missed run.
POLY_FRESH_DAYS = 4
GDELT_FRESH_DAYS = 4

#: Documented convexity-sleeve cap (fallback if RiskEnforcer is unavailable).
DEFAULT_CONVEXITY_SLEEVE_PCT = 0.05


@dataclass(frozen=True)
class ComponentReadiness:
    """One component's CURRENT readiness for the operator readout."""

    name: str  # matches the weight key / component name
    label: str  # human label, e.g. "(b) Vol mispricing (IV rank/skew)"
    state: str  # LIVE | WARMING-UP | MOCKED-OFF | APPROXIMATION | PLACEHOLDER
    gate: str  # the exact gate that makes this component fire
    progress: str  # e.g. "SPY 12/60, TLT 9/60" or "0/52 wk"
    detail: str  # honest one-liner
    counts_as_live: bool  # can it contribute to candidacy right now?
    independent: bool  # does it count toward the >= min_components rule?

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "state": self.state,
            "gate": self.gate,
            "progress": self.progress,
            "detail": self.detail,
            "counts_as_live": self.counts_as_live,
            "independent": self.independent,
        }


def _age_days(date_str: Optional[str], today: date) -> Optional[int]:
    """Whole days between an ISO date string and ``today``; None if unparseable."""
    if not date_str:
        return None
    try:
        d = date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return None
    return (today - d).days


def _hunter_iv_assets() -> Tuple[List[str], List[str]]:
    """
    Split the Hunter's mapped ETFs into (IV-collected, NOT-collected).

    The Phase-1 IV snapshot collector only persists the
    ``strategy_universe.iv_snapshot_universe()`` set, so the vol component can
    ONLY ever warm up for the Hunter assets that fall inside it (SPY/TLT/GLD).
    The rest never accrue IV history — an honest structural 0-weight we surface.
    """
    hunter = [spec.etf for spec in ASSET_SPECS]
    try:
        from modules.config.strategy_universe import iv_snapshot_universe

        collected_set = set(iv_snapshot_universe())
    except Exception:  # pragma: no cover - defensive
        collected_set = {"SPY", "QQQ", "IWM", "TLT", "GLD"}
    collected = [t for t in hunter if t in collected_set]
    not_collected = [t for t in hunter if t not in collected_set]
    return collected, not_collected


def _count_curated_polymarket_markets() -> int:
    """Count hand-curated Polymarket markets in config (coverage denominator)."""
    try:
        path = Path(__file__).resolve().parents[2] / "config" / "polymarket_markets.json"
        payload = json.loads(path.read_text())
        markets = payload.get("markets", [])
        return len([m for m in markets if isinstance(m, dict) and m.get("slug")])
    except Exception:
        return 0


def _hunter_cot_contracts() -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """
    Split the Hunter's mapped ETFs by COT coverage.

    Returns ``(covered, uncovered)`` where ``covered`` is a list of
    ``(etf, contract_name, cftc_code)`` for Hunter assets that map to a tracked
    CFTC contract, and ``uncovered`` is the ETFs with no COT contract (ITA / EWG /
    EWJ / EWZ — an honest structural 0-weight for the crowding component).
    """
    try:
        from modules.market.cftc_cot_analyzer import CFTCCOTAnalyzer

        name_to_code = {
            m["name"]: m["cftc_code"] for m in CFTCCOTAnalyzer.TRACKED_CONTRACTS.values()
        }
    except Exception:  # pragma: no cover - defensive
        name_to_code = {}
    covered: List[Tuple[str, str, str]] = []
    uncovered: List[str] = []
    for spec in ASSET_SPECS:
        match = next(((a, name_to_code[a]) for a in spec.cot_aliases if a in name_to_code), None)
        if match is not None:
            covered.append((spec.etf, match[0], match[1]))
        else:
            uncovered.append(spec.etf)
    return covered, uncovered


def _readiness_crowding(cfg: Any, warehouse: Any = None) -> ComponentReadiness:
    """(a) COT crowding — REAL CFTC feed + cot_history; per-contract 52-week warm-up."""
    label = "(a) Crowding (CFTC COT)"
    gate = (
        f"Real CFTC COT feed + ≥52-week history → large-spec percentile "
        f"≥ {cfg.crowding_extreme_pctl:.0f} casts a contrarian vote"
    )
    covered, uncovered = _hunter_cot_contracts()
    if warehouse is None:
        return ComponentReadiness(
            "crowding", label, READY_WARMING, gate, "warehouse unavailable",
            "COT warehouse unavailable — cannot read cot_history counts.", False, True)

    threshold = int(getattr(warehouse, "MIN_COT_HISTORY_WEEKS", 52))
    counts: Dict[str, int] = {}
    for _etf, _cname, code in covered:
        try:
            counts[code] = int(warehouse.count_cot_snapshots(code))
        except Exception:
            counts[code] = 0
    warm = [(etf, cname, code) for (etf, cname, code) in covered
            if counts.get(code, 0) >= threshold]
    progress = (", ".join(f"{cname} {counts.get(code, 0)}/{threshold}"
                          for _etf, cname, code in covered)
                or "no COT-mapped Hunter assets")
    state = READY_LIVE if warm else READY_WARMING
    detail_bits: List[str] = []
    detail_bits.append(
        f"LIVE for: {', '.join(c for _e, c, _co in warm)}." if warm
        else "All COT-mapped Hunter assets still warming up (<52 wk).")
    if uncovered:
        detail_bits.append(
            f"No COT coverage (structural 0-weight): {', '.join(uncovered)}.")
    try:
        latest = warehouse.latest_cot_date()
    except Exception:
        latest = None
    detail_bits.append(f"Latest COT report: {latest or 'none'}.")
    return ComponentReadiness(
        "crowding", label, state, gate, progress, " ".join(detail_bits),
        counts_as_live=bool(warm), independent=True)


def _readiness_vol(cfg: Any, warehouse: Any) -> ComponentReadiness:
    """(b) Vol mispricing — IV snapshot counts per Hunter asset vs the 60-day gate."""
    threshold = int(getattr(cfg, "min_iv_history_days", 60))
    gate = (
        f"≥ {threshold} daily IV snapshots per asset → IV rank ≥ {cfg.iv_rank_high:.0f} "
        f"(rich/stress) or ≤ {cfg.iv_rank_low:.0f} (cheap convexity)"
    )
    collected, not_collected = _hunter_iv_assets()
    if warehouse is None:
        return ComponentReadiness(
            "vol_mispricing", "(b) Vol mispricing (IV rank/skew)", READY_WARMING,
            gate, "warehouse unavailable",
            "IV warehouse unavailable — cannot read snapshot counts.", False, True)
    counts: Dict[str, int] = {}
    for tk in collected:
        try:
            counts[tk] = int(warehouse.count_iv_snapshots(tk))
        except Exception:
            counts[tk] = 0
    warm = [tk for tk in collected if counts.get(tk, 0) >= threshold]
    progress = (", ".join(f"{tk} {counts.get(tk, 0)}/{threshold}" for tk in collected)
                or "no IV-collected Hunter assets")
    state = READY_LIVE if warm else READY_WARMING
    detail_bits: List[str] = []
    detail_bits.append(f"LIVE for: {', '.join(warm)}." if warm
                       else "All IV-collected Hunter assets still warming up.")
    if not_collected:
        detail_bits.append(
            f"NOT collected (structural 0-weight): {', '.join(not_collected)} "
            f"— outside the Phase-1 IV snapshot universe.")
    return ComponentReadiness(
        "vol_mispricing", "(b) Vol mispricing (IV rank/skew)", state, gate,
        progress, " ".join(detail_bits), counts_as_live=bool(warm), independent=True)


def _readiness_regime(cfg: Any) -> ComponentReadiness:
    """(c) Regime fragility — deterministic, always on."""
    gate = (
        f"Regime-transition composite ≥ {cfg.regime_fragility_extreme:.0f}/100 "
        f"AND score trend rising → risk-off vote")
    return ComponentReadiness(
        "regime_fragility", "(c) Regime fragility", READY_LIVE, gate,
        "n/a (computed each run)",
        "Deterministic from the leading-indicator transition detector; real and always on.",
        counts_as_live=True, independent=True)


def _readiness_cross_asset(cfg: Any) -> ComponentReadiness:
    """(d) Cross-asset — credit/VIX divergence is live; commodity sub-signal is approx."""
    gate = (
        f"|HY-OAS z − VIX z| ≥ {cfg.cross_asset_z_div:.1f}σ (credit/VIX divergence) "
        f"→ risk-off vote")
    detail = (
        "Credit/VIX z-divergence is real and live. The per-asset commodity "
        "term-structure sub-signal (USO/GLD) is an MA-based APPROXIMATION (flagged "
        "in every dossier), not a true futures-curve build.")
    return ComponentReadiness(
        "cross_asset", "(d) Cross-asset divergence", READY_LIVE, gate,
        "n/a (computed each run)", detail, counts_as_live=True, independent=True)


def _readiness_sentiment(cfg: Any) -> ComponentReadiness:
    """(e) Sentiment extreme — real matrix/social feeds; placeholders excluded."""
    gate = (
        f"Composite sentiment ≥ {cfg.sentiment_high_pctl:.0f}th (euphoria → contrarian "
        f"risk-off) or ≤ {cfg.sentiment_low_pctl:.0f}th (panic → contrarian risk-on)")
    detail = (
        "Reads the regional sentiment matrix + social composite. Sub-feeds: "
        "Reddit (real, circuit-broken), Twitter/X (real, circuit-broken), "
        "CNN Fear & Greed (real since v6.7.0), CBOE put/call (real since v6.7.0). "
        "The Hunter EXCLUDES any feed that degrades to a placeholder.")
    return ComponentReadiness(
        "sentiment_extreme", "(e) Sentiment extreme", READY_LIVE, gate,
        "n/a (computed each run)", detail, counts_as_live=True, independent=True)


def _readiness_polymarket(cfg: Any, warehouse: Any, today: date) -> ComponentReadiness:
    """(g) Polymarket divergence — reachability via last snapshot age + market coverage."""
    gate = (
        f"Curated markets snapshotted + |our_prob − market_prob| "
        f"≥ {cfg.polymarket_divergence_extreme:.0%}")
    n_markets = _count_curated_polymarket_markets()
    if warehouse is None:
        return ComponentReadiness(
            "polymarket_divergence", "(g) Polymarket divergence", READY_WARMING, gate,
            f"{n_markets} markets configured; warehouse unavailable",
            "Warehouse unavailable — cannot read snapshot freshness.", False, True)
    last = warehouse.latest_polymarket_date()
    if last is None:
        return ComponentReadiness(
            "polymarket_divergence", "(g) Polymarket divergence", READY_WARMING, gate,
            f"0 snapshots ({n_markets} markets configured)",
            "No Polymarket snapshots stored yet — populated once Phase 1 reaches the Gamma API.",
            False, True)
    age = _age_days(last, today)
    snapped = warehouse.count_distinct_polymarket_markets()
    fresh = age is not None and age <= POLY_FRESH_DAYS
    state = READY_LIVE if fresh else READY_WARMING
    progress = f"last snapshot {age}d ago, {snapped}/{n_markets} markets"
    detail = ("Reachable — recent snapshot present (evidence stream, small weight)."
              if fresh else
              f"Stale: last snapshot {age}d ago (> {POLY_FRESH_DAYS}d); casts no vote until refreshed.")
    return ComponentReadiness(
        "polymarket_divergence", "(g) Polymarket divergence", state, gate,
        progress, detail, counts_as_live=fresh, independent=True)


def _readiness_gdelt(cfg: Any, warehouse: Any, today: date) -> ComponentReadiness:
    """(h) GDELT pulse — Welford warm-up count vs gate + last-pulse freshness."""
    min_hist = int(getattr(cfg, "gdelt_min_history", 20))
    gate = (
        f"Per-region Welford count ≥ {min_hist} AND volume z ≥ {cfg.gdelt_anomaly_z:.1f} "
        f"with negative tone → geo stance vote")
    if warehouse is None:
        return ComponentReadiness(
            "gdelt_pulse", "(h) GDELT geopolitical pulse", READY_WARMING, gate,
            "warehouse unavailable", "Warehouse unavailable.", False, True)
    last = warehouse.latest_gdelt_date()
    max_count = warehouse.max_gdelt_history("volume")
    if last is None or max_count == 0:
        return ComponentReadiness(
            "gdelt_pulse", "(h) GDELT geopolitical pulse", READY_WARMING, gate,
            f"0/{min_hist} obs",
            "No GDELT pulse stored yet — anomaly accumulator empty.", False, True)
    age = _age_days(last, today)
    warm = max_count >= min_hist
    fresh = age is not None and age <= GDELT_FRESH_DAYS
    state = READY_LIVE if (warm and fresh) else READY_WARMING
    progress = f"{max_count}/{min_hist} obs (best region), last pulse {age}d ago"
    if not warm:
        detail = f"Welford accumulator warming up ({max_count}/{min_hist}); z-scores not yet trusted."
    elif not fresh:
        detail = f"Warm but stale (last pulse {age}d ago)."
    else:
        detail = "Warm and fresh — anomaly z-scores trusted (evidence stream, small weight)."
    return ComponentReadiness(
        "gdelt_pulse", "(h) GDELT geopolitical pulse", state, gate,
        progress, detail, counts_as_live=(warm and fresh), independent=True)


def _readiness_catalyst(cfg: Any, today: date) -> ComponentReadiness:
    """(f) Catalyst window — a window/gate (no weight, not independent)."""
    calendar = load_event_calendar()
    base_gate = f"Next FOMC/CPI/NFP within {cfg.catalyst_window_days}d → proximity window flag"
    if not calendar:
        return ComponentReadiness(
            "catalyst", "(f) Catalyst window (gate)", READY_WARMING, base_gate,
            "no calendar",
            "config/event_calendar.json missing or empty — edit it to enable the window.",
            counts_as_live=False, independent=False)
    proximate, text = _catalyst_window(calendar, cfg, today)
    progress = text + (" [PROXIMATE]" if proximate else "")
    return ComponentReadiness(
        "catalyst", "(f) Catalyst window (gate)", READY_LIVE, base_gate, progress,
        "Hand-maintained static calendar; a window flag, not a weighted vote.",
        counts_as_live=False, independent=False)


def _summarize_last_run(watchlist: Dict[str, Any]) -> Dict[str, Any]:
    """Compact summary of the last persisted watchlist for the readiness footer."""
    cands = watchlist.get("candidates", []) or []
    sleeve_used = sum((_as_float(c.get("max_premium_pct")) or 0.0) for c in cands)
    return {
        "generated_at": watchlist.get("generated_at"),
        "status": watchlist.get("status"),
        "candidates_found": int(watchlist.get("candidates_found", len(cands)) or 0),
        "weights_hash": watchlist.get("weights_hash"),
        "degraded_components": watchlist.get("degraded_components", []) or [],
        "sleeve_used_pct": sleeve_used,
        "candidates": [
            {
                "ticker": c.get("mapped_ticker") or c.get("asset"),
                "direction": str(c.get("direction", "")).upper(),
                "composite_score": _as_float(c.get("composite_score")) or 0.0,
                "contributing_components": c.get("contributing_components", []) or [],
            }
            for c in cands
        ],
    }


def _convexity_sleeve_cap() -> float:
    """Documented convexity-sleeve cap from RiskEnforcer (DRY), with a safe fallback."""
    try:
        from modules.utils.risk_enforcer import RiskEnforcer

        cap = getattr(RiskEnforcer().limits, "total_convexity_sleeve_pct", None)
        if cap is not None:
            return float(cap)
    except Exception:  # pragma: no cover - defensive
        pass
    return DEFAULT_CONVEXITY_SLEEVE_PCT


def compute_hunter_readiness(
    *,
    warehouse: Any = None,
    config: Any = None,
    watchlist: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Inspect the CURRENT readiness of every Hunter component — a pure status query.

    No pipeline run, no network: reads warehouse counts (IV snapshots, geo
    snapshot freshness), config gates, and the static calendar. Returns a
    serializable dict the operator readout (``format_hunter_readiness``), the
    scoreboard grading clock and the daily digest all share (DRY). Never raises.

    Args:
        warehouse: a DataWarehouse for snapshot counts (lazily built if None).
        config: an AsymmetryConfig (defaults to ``settings.config.asymmetry``).
        watchlist: the last persisted watchlist (optional) for a last-run footer.
        today: override for freshness/catalyst math (defaults to UTC today).
    """
    try:
        cfg = config
        if cfg is None:
            try:
                from modules.config.settings import config as _settings_config

                cfg = _settings_config.asymmetry
            except Exception:  # pragma: no cover - defensive
                from modules.config.settings import AsymmetryConfig

                cfg = AsymmetryConfig()

        if warehouse is None:
            try:
                from modules.data.warehouse import DataWarehouse

                warehouse = DataWarehouse()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"readiness: warehouse unavailable: {exc}")
                warehouse = None

        run_today = today or datetime.now(timezone.utc).date()

        components = [
            _readiness_crowding(cfg, warehouse),
            _readiness_vol(cfg, warehouse),
            _readiness_regime(cfg),
            _readiness_cross_asset(cfg),
            _readiness_sentiment(cfg),
            _readiness_polymarket(cfg, warehouse, run_today),
            _readiness_gdelt(cfg, warehouse, run_today),
        ]
        catalyst = _readiness_catalyst(cfg, run_today)

        live_independent = [c for c in components if c.independent and c.counts_as_live]
        live_names = [c.name for c in live_independent]
        live_count = len(live_independent)
        min_comp = int(getattr(cfg, "min_components", 3))
        n_weighted = len(components)

        if live_count >= min_comp:
            posture = (
                f"RARE — {live_count} of {n_weighted} weighted components are LIVE "
                f"({', '.join(live_names)}); the candidate rule needs ≥{min_comp} "
                f"INDEPENDENT components agreeing at extremes, so firing stays rare by "
                f"design. It stays rare until more per-asset components warm up "
                f"(e.g. IV ≥{getattr(cfg, 'min_iv_history_days', 60)} snapshots)."
            )
        else:
            posture = (
                f"DORMANT — only {live_count} of {n_weighted} weighted components are LIVE "
                f"({', '.join(live_names) or 'none'}); the candidate rule needs ≥{min_comp} "
                f"independent. The Hunter cannot fire until more components come online "
                f"(IV warm-up + real COT feed)."
            )

        return {
            "generated_at": _now_iso(),
            "weights_hash": cfg.weights_hash(),
            "min_components": min_comp,
            "weighted_component_count": n_weighted,
            "live_independent_count": live_count,
            "live_component_names": live_names,
            "components": [c.to_dict() for c in components],
            "catalyst": catalyst.to_dict(),
            "firing_posture": posture,
            "convexity_sleeve_cap_pct": _convexity_sleeve_cap(),
            "last_run": _summarize_last_run(watchlist) if watchlist else None,
        }
    except Exception as exc:  # readiness must never raise
        logger.warning(f"compute_hunter_readiness failed (non-fatal): {exc}")
        return {
            "generated_at": _now_iso(),
            "error": str(exc),
            "components": [],
            "live_independent_count": 0,
            "min_components": 3,
            "firing_posture": "UNKNOWN — readiness computation failed.",
            "last_run": None,
        }


def format_hunter_readiness(readiness: Dict[str, Any]) -> str:
    """
    Render ``compute_hunter_readiness`` as the operator's terminal block.

    This is what ``main.py hunter-status`` prints: per-component
    LIVE / WARMING-UP / MOCKED-OFF with the exact gate, the live-count vs the
    candidate rule, the expected firing posture, and a last-run footer. Tolerant
    of partial dicts.
    """
    r = readiness or {}
    lines: List[str] = []
    lines.append("=" * 66)
    lines.append("ASYMMETRY HUNTER — READINESS  (pure status query, no run)")
    lines.append("=" * 66)

    if r.get("error"):
        lines.append(f"⚠️  Readiness computation failed: {r['error']}")
        lines.append("=" * 66)
        return "\n".join(lines)

    min_comp = r.get("min_components", 3)
    live_count = r.get("live_independent_count", 0)
    n_weighted = r.get("weighted_component_count", 7)
    live_names = r.get("live_component_names", []) or []

    lines.append(f"Generated:    {r.get('generated_at', '—')}")
    lines.append(f"Weights hash: {r.get('weights_hash', '—')}")
    lines.append(f"Candidate rule: ≥ {min_comp} INDEPENDENT components must agree at extremes")
    lines.append("")
    lines.append(
        f"Components currently LIVE: {live_count} of {n_weighted}"
        + (f"  ({', '.join(live_names)})" if live_names else "")
    )
    lines.append("")
    lines.append("Per-component readiness:")
    for comp in r.get("components", []) or []:
        state = comp.get("state", "?")
        icon = _STATE_ICON.get(state, "•")
        progress = comp.get("progress", "")
        head = f"  {icon} [{state:<11}] {comp.get('label', comp.get('name', '?'))}"
        if progress and progress not in ("n/a (computed each run)",):
            head += f"   {progress}"
        lines.append(head)
        if comp.get("gate"):
            lines.append(f"        gate: {comp['gate']}")
        if comp.get("detail"):
            lines.append(f"        {comp['detail']}")

    catalyst = r.get("catalyst") or {}
    if catalyst:
        icon = _STATE_ICON.get(catalyst.get("state", "?"), "•")
        lines.append(
            f"  {icon} [{catalyst.get('state', '?'):<11}] {catalyst.get('label', '(f) Catalyst')}"
            f"   {catalyst.get('progress', '')}"
        )

    lines.append("")
    lines.append("Expected firing posture:")
    for chunk in _wrap(r.get("firing_posture", "—"), width=62):
        lines.append(f"  {chunk}")

    lines.append("")
    last = r.get("last_run")
    if not last:
        lines.append("Last pipeline run: none yet — run `python main.py auto --paper` to generate one.")
    else:
        cap = r.get("convexity_sleeve_cap_pct", DEFAULT_CONVEXITY_SLEEVE_PCT)
        used = last.get("sleeve_used_pct", 0.0)
        lines.append("Last pipeline run:")
        lines.append(f"  Generated: {last.get('generated_at', '—')}   "
                     f"Status: {last.get('status', '—')}")
        lines.append(f"  Candidates: {last.get('candidates_found', 0)}   "
                     f"Convexity sleeve used: {used:.1%} / {cap:.1%} cap")
        degraded = last.get("degraded_components", []) or []
        if degraded:
            joined = ", ".join(f"{d.get('name')}({d.get('status')})" for d in degraded)
            lines.append(f"  Degraded last run: {joined}")
        cands = last.get("candidates", []) or []
        if not cands:
            lines.append("  (no candidates — silence is a feature; most runs emit zero)")
        else:
            for c in cands:
                contribs = ", ".join(c.get("contributing_components", []) or [])
                lines.append(
                    f"  • {str(c.get('ticker', '?')):<6} {c.get('direction', '?'):<5} "
                    f"score {float(c.get('composite_score', 0)):.0f}/100  [{contribs}]"
                )

    lines.append("=" * 66)
    return "\n".join(lines)


def _wrap(text: str, width: int = 62) -> List[str]:
    """Tiny greedy word-wrap (stdlib textwrap avoided to keep this import-light)."""
    words = str(text).split()
    out: List[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out or [""]
