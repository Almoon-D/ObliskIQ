"""
Continuity Guard — Phase-4 Decision Contract (v6.12.0)

Deterministic engine-side continuity enforcement, not prompt vibes:

- The default verdict for every open position is HOLD. Open positions the AI
  omits from its response get a HOLD decision appended.
- The AI may only flip (SELL) a position opened within the SAME trading day
  if its response cites either (a) a fired invalidation trigger from the
  thesis ledger (``cites_trigger_id``) or (b) materially new information
  absent from the prior run's payload (``new_information``). A flip without a
  valid citation is rewritten to HOLD and logged — never executed
  (paper_runner.decision_to_signal skips HOLD).

Verification semantics (deterministic, honestly degraded):
- price_below / price_above triggers are verified against the current price;
  a citation of an UNFIRED price trigger is rejected. If no price is
  available the citation is rejected conservatively — HOLD is the safe
  verdict, and the StopLossMonitor (orchestrator STEP 8, which runs AFTER
  this guard) independently converts genuinely triggered stops into SELLs.
- date_after triggers are verified against today's date.
- event triggers cannot be engine-verified: an existing event-trigger
  citation is accepted but logged as ``unverified_event_citation``.
- ``new_information`` is a deterministic PROXY for "materially new": it must
  be non-trivial (>= MIN_NEW_INFO_CHARS) and must not appear verbatim
  (whitespace/case-normalized) in the payload of the run that opened the
  position. Semantic truth is not engine-checkable; this is stated plainly
  in docs/AI_GOVERNANCE.md.

Flips of positions opened on EARLIER days pass the guard untouched — the
contract scopes the citation rule to same-day churn. Trading day is compared
on UTC dates: both scheduled ET runs (morning/afternoon) fall on the same
UTC date, so UTC-date equality == ET-date equality for scheduled runs.

Scope note: this guard reads the thesis ledger (recommendation commitments),
not broker positions — the system is advisory and the paper/IBKR book remains
execution truth.
"""

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Minimum length for a new_information citation to count as non-trivial.
MIN_NEW_INFO_CHARS = 40

GUARD_DEFAULT_SOURCE = "continuity_guard_default"


def _normalize_text(text: str) -> str:
    """Lowercase + collapse whitespace, for verbatim-containment checks"""
    return re.sub(r"\s+", " ", text.strip().lower())


def _entry_day(entry: Dict[str, Any]) -> Optional[date]:
    """UTC calendar day a ledger entry was opened, or None if unparseable"""
    raw = str(entry.get("entry_ts", ""))
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            logger.warning(f"ContinuityGuard: unparseable entry_ts '{raw}'")
            return None


class ContinuityGuard:
    """
    Enforce portfolio continuity on parsed Phase-4 decisions.

    Args:
        now_fn: Injectable UTC clock (tests freeze it). Defaults to
            ``datetime.now(timezone.utc)``.
    """

    def __init__(self, now_fn: Optional[Callable[[], datetime]] = None) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def enforce(
        self,
        portfolio_decisions: List[Dict[str, Any]],
        open_theses: List[Dict[str, Any]],
        current_prices: Optional[Dict[str, float]] = None,
        prior_payload_loader: Optional[Callable[[str], Optional[str]]] = None,
    ) -> Dict[str, Any]:
        """
        Apply the continuity rules to a run's decisions.

        Args:
            portfolio_decisions: Parsed Phase-4 decision dicts. MUTATED in
                place (orchestrator convention): uncited same-day flips are
                rewritten to HOLD, and default HOLDs are appended for open
                positions the AI omitted.
            open_theses: Open thesis-ledger entries
                (DatabaseManager.get_open_theses()).
            current_prices: {ticker: price} for price-trigger verification.
            prior_payload_loader: run_id -> serialized payload of that run
                (the orchestrator wraps checkpoint.load_checkpoint). Used to
                verify new_information citations; None/miss degrades to
                'unverifiable' (accepted, logged).

        Returns:
            Summary dict for reports/audit:
            {open_positions, defaults_applied, flips_allowed, flips_rejected}.
        """
        prices = current_prices or {}
        today = self._now_fn().date()
        summary: Dict[str, Any] = {
            "open_positions": [str(e.get("ticker", "")).upper() for e in open_theses],
            "defaults_applied": [],
            "flips_allowed": [],
            "flips_rejected": [],
        }

        # Last decision per ticker wins (mirrors downstream consumers that
        # scan the list; contract emits one decision per ticker).
        by_ticker: Dict[str, Dict[str, Any]] = {}
        for decision in portfolio_decisions:
            ticker = str(decision.get("ticker", decision.get("symbol", ""))).upper()
            if ticker:
                by_ticker[ticker] = decision

        for entry in open_theses:
            ticker = str(entry.get("ticker", "")).upper()
            if not ticker:
                continue
            verdict: Optional[Dict[str, Any]] = by_ticker.get(ticker)

            # Rule 1: default verdict for every open position is HOLD.
            if verdict is None:
                portfolio_decisions.append(
                    {
                        "ticker": ticker,
                        "action": "HOLD",
                        "source": GUARD_DEFAULT_SOURCE,
                        "reasoning": (
                            "No AI verdict for this open position — "
                            "default HOLD per continuity contract"
                        ),
                    }
                )
                summary["defaults_applied"].append(ticker)
                logger.info(f"  🛡️ {ticker}: no verdict from AI — default HOLD appended")
                continue

            # Rule 2: same-day flips require a valid citation.
            action = str(verdict.get("action", "")).upper()
            if action != "SELL":
                continue
            opened = _entry_day(entry)
            if opened is None or opened != today:
                continue  # earlier-day positions may be flipped freely

            allowed, basis = self._validate_flip_citation(
                verdict, entry, prices.get(ticker), today, prior_payload_loader
            )
            if allowed:
                verdict["flip_citation"] = basis
                summary["flips_allowed"].append({"ticker": ticker, "basis": basis})
                logger.info(f"  🛡️ {ticker}: same-day flip ALLOWED ({basis})")
            else:
                verdict["rejected_flip"] = {"original_action": action, "reason": basis}
                verdict["action"] = "HOLD"
                summary["flips_rejected"].append({"ticker": ticker, "reason": basis})
                logger.warning(
                    f"  🛡️ {ticker}: uncited same-day flip REJECTED → HOLD ({basis})"
                )

        return summary

    # ------------------------------------------------------------------ #
    # Citation verification
    # ------------------------------------------------------------------ #
    def _validate_flip_citation(
        self,
        decision: Dict[str, Any],
        entry: Dict[str, Any],
        current_price: Optional[float],
        today: date,
        prior_payload_loader: Optional[Callable[[str], Optional[str]]],
    ) -> Tuple[bool, str]:
        """Return (allowed, basis/reason) for a same-day flip attempt"""
        triggers = {
            str(t.get("trigger_id")): t
            for t in (entry.get("invalidation_triggers") or [])
            if isinstance(t, dict)
        }

        cited = decision.get("cites_trigger_id")
        if cited:
            trigger = triggers.get(str(cited))
            if trigger is None:
                return False, f"cited trigger '{cited}' not found in the ledger entry"
            fired, note = self._trigger_fired(trigger, current_price, today)
            if fired is True:
                return True, f"trigger:{cited} fired ({note})"
            if fired is None:
                return True, f"trigger:{cited} (unverified_event_citation)"
            return False, f"cited trigger '{cited}' has NOT fired ({note})"

        new_info = str(decision.get("new_information") or "").strip()
        if not new_info:
            return False, "same-day flip cites no trigger and provides no new_information"
        if len(new_info) < MIN_NEW_INFO_CHARS:
            return False, (
                f"new_information too thin ({len(new_info)} chars < "
                f"{MIN_NEW_INFO_CHARS} minimum)"
            )

        prior_payload: Optional[str] = None
        run_id = str(entry.get("run_id", "")) or None
        if prior_payload_loader is not None and run_id:
            try:
                prior_payload = prior_payload_loader(run_id)
            except Exception as exc:  # defensive: loader failure ≠ guard failure
                logger.warning(f"ContinuityGuard: prior payload load failed: {exc}")
        if prior_payload:
            if _normalize_text(new_info) in _normalize_text(prior_payload):
                return False, (
                    "new_information appears verbatim in the prior run's payload — "
                    "not materially new"
                )
            return True, "new_information (absent from prior payload)"
        return True, "new_information (prior payload unavailable — unverifiable)"

    @staticmethod
    def _trigger_fired(
        trigger: Dict[str, Any], current_price: Optional[float], today: date
    ) -> Tuple[Optional[bool], str]:
        """
        Deterministically evaluate whether a ledger trigger has fired.

        Returns (True/False, note) for machine-checkable triggers, or
        (None, note) for event triggers the engine cannot verify.
        """
        trig_type = str(trigger.get("type", "")).lower()

        if trig_type in ("price_below", "price_above"):
            threshold = trigger.get("threshold")
            if threshold is None:
                return False, "price trigger has no threshold"
            if current_price is None:
                # Conservative: unverifiable price citation does not authorize
                # a flip; triggered stops are handled by the StopLossMonitor.
                return False, "no current price available to verify"
            if trig_type == "price_below":
                fired = float(current_price) < float(threshold)
            else:
                fired = float(current_price) > float(threshold)
            return fired, f"price {current_price} vs threshold {threshold}"

        if trig_type == "date_after":
            raw = trigger.get("trigger_date")
            if not raw:
                return False, "date_after trigger has no trigger_date"
            try:
                bound = date.fromisoformat(str(raw)[:10])
            except ValueError:
                return False, f"unparseable trigger_date '{raw}'"
            return today > bound, f"today {today} vs bound {bound}"

        if trig_type == "event":
            return None, "event trigger — engine cannot verify"

        return False, f"unknown trigger type '{trig_type}'"
