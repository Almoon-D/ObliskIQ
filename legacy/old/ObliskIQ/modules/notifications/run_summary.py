"""
Run summary — the trade sheet a human actually reads.

ObliskIQ never places live orders; its output is a recommendation list the operator
executes at the broker. The pipeline wrote that list to ``outputs/phase4_decisions_*.json``
(gitignored, discarded when the Actions runner exits) and never sent it anywhere, so a
healthy run produced nothing a person could act on. This module turns the Phase-4
decisions dict into a short message for Telegram / email.

Pure formatting: no network, no filesystem except ``find_latest_trade_sheet``.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TELEGRAM_CHUNK = 3800  # Telegram caps a message at 4096 chars; leave headroom for markup
THESIS_CHARS = 160


def find_latest_trade_sheet(outputs_dir: Path, since_epoch: Optional[float] = None) -> Optional[Path]:
    """Newest ``phase4_decisions_*.json``; with ``since_epoch``, only one written by THIS run."""
    candidates = sorted(outputs_dir.glob("phase4_decisions_*.json"), key=lambda p: p.stat().st_mtime)
    if since_epoch is not None:
        candidates = [p for p in candidates if p.stat().st_mtime >= since_epoch]
    return candidates[-1] if candidates else None


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _fmt_price(value: Any) -> str:
    number = _num(value)
    return "n/a" if number is None else f"{number:,.2f}"


def _stop_text(decision: Dict[str, Any]) -> str:
    """'62.00 (-11.4%)' from whichever of stop price / stop fraction is available."""
    stop_price = _num(decision.get("stop_loss", decision.get("stop_loss_price")))
    fraction = _num(decision.get("stop_loss_pct"))
    entry = _num(decision.get("entry_price"))
    if stop_price is None and fraction is not None and entry:
        stop_price = entry * (1 + (-abs(fraction) if abs(fraction) < 1 else -abs(fraction) / 100))
    parts = []
    if stop_price is not None:
        parts.append(f"{stop_price:,.2f}")
    if fraction is not None:
        pct = abs(fraction) * (100 if abs(fraction) < 1 else 1)
        parts.append(f"(-{pct:.1f}%)")
    return " ".join(parts) if parts else "n/a"


def _size_text(decision: Dict[str, Any]) -> str:
    pct = _num(decision.get("position_size_pct"))
    if pct is None:
        fraction = _num(decision.get("position_size"))
        pct = None if fraction is None else fraction * 100
    return "n/a" if pct is None else f"{pct:.1f}%"


def _conviction_text(decision: Dict[str, Any]) -> str:
    contract = _num(decision.get("conviction_contract"))
    if contract is not None:
        return f"{contract * 10:.0f}/10"
    internal = _num(decision.get("conviction"))
    return "n/a" if internal is None else f"{internal:.0f}/10"


def _thesis(decision: Dict[str, Any]) -> str:
    text = str(decision.get("thesis") or decision.get("reasoning") or "").strip().replace("\n", " ")
    return text if len(text) <= THESIS_CHARS else text[: THESIS_CHARS - 1].rstrip() + "…"


def summarize(decisions: Dict[str, Any]) -> Dict[str, Any]:
    """Structured view of a Phase-4 decisions dict (also what the tests assert on)."""
    approved: List[Dict[str, Any]] = list(decisions.get("portfolio_decisions") or decisions.get("decisions") or [])
    rejected: List[Dict[str, Any]] = list(decisions.get("rejected_decisions") or [])
    actionable = [d for d in approved if str(d.get("action", "")).upper() in ("BUY", "SELL")]
    holds = [d for d in approved if str(d.get("action", "")).upper() == "HOLD"]
    provider = decisions.get("ai_provider") or {}
    guard = decisions.get("continuity_guard") or {}
    degraded = decisions.get("degraded_provider_policy") or {}
    return {
        "actionable": actionable,
        "holds": holds,
        "rejected": rejected,
        "provider": provider.get("label") or "unknown",
        "degraded": bool(provider.get("degraded")),
        "blocked_buys": list(degraded.get("blocked_buys") or []),
        "regime": str(
            decisions.get("market_regime")
            or (decisions.get("portfolio_optimization") or {}).get("market_regime")
            or "unknown"
        ),
        "guard_rewrites": len(guard.get("flips_rejected") or []),
        "failover": [a for a in (provider.get("attempts") or []) if a.get("outcome") != "ok"],
    }


def format_run_summary(
    decisions: Dict[str, Any], *, run_label: str = "", digest_text: Optional[str] = None
) -> Tuple[str, str]:
    """Return ``(subject, plain_text_body)``."""
    s = summarize(decisions)
    buys = sum(1 for d in s["actionable"] if str(d.get("action")).upper() == "BUY")
    sells = len(s["actionable"]) - buys
    if s["actionable"]:
        headline = f"{buys} BUY / {sells} SELL"
    else:
        headline = "no trades to execute"
    subject = f"ObliskIQ {run_label}: {headline}".replace("  ", " ").strip()

    lines = [f"ObliskIQ trade sheet — {run_label or datetime.now().strftime('%Y-%m-%d')}".replace("  ", " ")]
    lines.append(f"AI: {s['provider']} · regime: {s['regime']}")
    if s["degraded"]:
        lines.append("⚠️ DEGRADED AI PROVIDER: new BUYs were blocked this run (holds/exits only).")
    if s["failover"]:
        lines.append(
            "ℹ️ failover: " + "; ".join(f"{a.get('provider')} {a.get('outcome')}" for a in s["failover"])
        )

    lines.append("")
    if s["actionable"]:
        lines.append("ACTIONS (passed every risk gate — execute manually):")
        for d in s["actionable"]:
            action = str(d.get("action")).upper()
            icon = "🟢" if action == "BUY" else "🔴"
            ticker = d.get("ticker", d.get("symbol", "?"))
            # An exit has no stop or target: the contract forces positive numbers, so the AI writes 0.01
            # placeholders that would print as "stop 0.01 (-100.0%)".
            levels = (
                f" · stop {_stop_text(d)} · target {_fmt_price(d.get('target_price'))}" if action == "BUY" else ""
            )
            lines.append(
                f"{icon} {action} {ticker} — {_size_text(d)} of portfolio · entry {_fmt_price(d.get('entry_price'))}"
                f"{levels} · conviction {_conviction_text(d)}"
            )
            thesis = _thesis(d)
            if thesis:
                lines.append(f"    {thesis}")
    else:
        lines.append("No BUY or SELL passed the risk gates this run. Nothing to execute.")

    if s["holds"]:
        lines.append("")
        lines.append("HOLD: " + ", ".join(str(d.get("ticker", d.get("symbol", "?"))) for d in s["holds"]))

    if s["rejected"]:
        lines.append("")
        lines.append(f"BLOCKED by risk gates ({len(s['rejected'])}) — not for execution:")
        for d in s["rejected"][:5]:
            lines.append(
                f"  - {d.get('ticker', d.get('symbol', '?'))} {d.get('action', '')}: "
                f"{str(d.get('rejection_reason', 'rejected'))[:140]}"
            )
        if len(s["rejected"]) > 5:
            lines.append(f"  … and {len(s['rejected']) - 5} more")

    if s["guard_rewrites"]:
        lines.append("")
        lines.append(f"Continuity guard rejected {s['guard_rewrites']} uncited same-day flip(s); those positions stay on HOLD.")

    if digest_text:
        gate = [ln.strip() for ln in digest_text.splitlines() if "gate" in ln.lower() or "day" in ln.lower()][:3]
        if gate:
            lines.append("")
            lines.extend(gate)

    lines.append("")
    lines.append("Paper-trading only. Recommendations, not advice; verify before you trade.")
    return subject, "\n".join(lines)


def format_no_sheet_message(run_label: str = "") -> Tuple[str, str]:
    """Sent when a run finished without producing a Phase-4 trade sheet."""
    subject = f"ObliskIQ {run_label}: no trade sheet produced".replace("  ", " ").strip()
    body = (
        "The run finished but produced no Phase-4 trade sheet. Typical causes: the kill switch halted the "
        "run, the market-data feeds (Yahoo) were down so the run stopped before any AI call, the AI "
        "proposed no tickers the structural gate accepted, or an earlier phase stopped. "
        "Check the run log; nothing to execute."
    )
    return subject, body


def to_telegram_html(subject: str, body: str) -> List[str]:
    """Escape for Telegram HTML mode, bold the headline, and split under the message cap."""
    escaped = html.escape(body)
    text = f"<b>{html.escape(subject)}</b>\n\n{escaped}"
    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > TELEGRAM_CHUNK and current:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks


def load_trade_sheet(path: Path) -> Dict[str, Any]:
    """Read a decisions JSON file (raises ValueError on a non-object payload)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} does not contain a JSON object")
    return payload
