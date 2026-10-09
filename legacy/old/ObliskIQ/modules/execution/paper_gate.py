"""
Paper-Trading Validation Gate (the 90-day gate)
================================================

The HARD gate between SIMULATED (paper) and REAL (IBKR write path) execution.

Live order placement is BLOCKED until paper trading has PASSED every validation criterion:

    1. Sharpe ratio       > 0.5   (annualised, risk-free rate MEASURED from FRED, not assumed)
    2. Max drawdown       < 20%
    3. Win rate           > 50%   (of CLOSED positions — round trips, not legs)
    4. Calendar days      >= 90   (clock starts at the FIRST paper trade)
    5. Trading days       >= 126  (daily snapshots; v6.16.0)
    6. Closed positions   >= 30   (v6.16.0)
    7. Excess vs ACWI     not demonstrably negative: the 95% block-bootstrap interval of the
                          annualised mean daily excess return has an upper bound above zero (v6.16.0)

WHAT A PASS MEANS. The gate protects live capital from a book that is unstable, deep in drawdown, or
demonstrably worse than the world index. It does NOT prove the AI has skill: a 90-126 day Sharpe has a
standard error of ~2 and a coin flip clears "win rate > 50%" 34-44% of the time at n=30. Whether the AI
adds information is decided by the AI scoreboard's weekly forecast panel (docs/AI_SCOREBOARD.md).
WHAT IS VALIDATED. The paper book runs the AI pipeline (Phase 2/4 decisions through the deterministic
gauntlet); ``strategy_engine_version`` pins the configured deterministic baseline engine and resets the
clock when it changes — it is not the thing being traded.

This is an ENFORCED check, not documentation. ``assert_live_trading_allowed()``
raises ``LiveTradingBlockedError`` unless the gate reports PASS, and the IBKR
write path (``IBKRClient.place_order`` / ``assert_write_allowed``) calls it
before any order can leave the building.

The four thresholds are the single source of truth on ``PaperTrader``
(``MIN_SHARPE_RATIO``, ``MAX_DRAWDOWN_PCT``, ``MIN_WIN_RATE``,
``MIN_VALIDATION_DAYS``) so the gate and the performance report never diverge.

George Soros: "90 days minimum with real-time data, fake execution before real capital."
Ray Dalio:    "Risk constraints always enforced."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional

from modules.execution.paper_trader import (
    BenchmarkComparison,
    PaperTrader,
    PerformanceReport,
)

logger = logging.getLogger(__name__)


class LiveTradingBlockedError(RuntimeError):
    """Raised when the IBKR write path is attempted before the gate PASSes."""


class GateStatus(str, Enum):
    """Status of an individual criterion or the gate as a whole."""

    PASS = "PASS"
    FAIL = "FAIL"
    IN_PROGRESS = "IN_PROGRESS"


_STATUS_ICON = {
    GateStatus.PASS: "✅",
    GateStatus.FAIL: "❌",
    GateStatus.IN_PROGRESS: "⏳",
}


@dataclass
class GateCriterion:
    """One of the four gate criteria, with its current value and verdict."""

    name: str
    current_value: float
    threshold: float
    comparison: str  # ">", "<", ">="
    status: GateStatus
    unit: str = ""
    detail: str = ""  # replaces the numeric value in the display when the number alone would mislead

    @property
    def passed(self) -> bool:
        return self.status == GateStatus.PASS

    def format_value(self) -> str:
        """Human-readable current value with unit."""
        if self.detail:
            return self.detail
        if self.unit == "d":
            return f"{int(round(self.current_value))}d"
        if self.unit == "%":
            return f"{self.current_value:.2f}%"
        return f"{self.current_value:.2f}"

    def format_threshold(self) -> str:
        """Human-readable rule, e.g. '> 0.5' or '>= 90d'."""
        if self.unit == "d":
            return f"{self.comparison} {int(round(self.threshold))}d"
        if self.unit == "n":
            return f"{self.comparison} {int(round(self.threshold))}"
        if self.unit == "%":
            return f"{self.comparison} {self.threshold:.0f}%"
        return f"{self.comparison} {self.threshold}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "current_value": round(self.current_value, 4),
            "threshold": self.threshold,
            "comparison": self.comparison,
            "unit": self.unit,
            "detail": self.detail,
            "status": self.status.value,
            "passed": self.passed,
        }


@dataclass
class PaperGateStatus:
    """Full snapshot of where paper trading stands against the 90-day gate."""

    overall_status: GateStatus
    days_elapsed: int
    days_required: int
    clock_started_at: Optional[date]
    strategy_engine_version: Optional[str]
    criteria: List[GateCriterion]
    total_trades: int
    benchmark: Optional[BenchmarkComparison] = None
    note: str = ""
    performance: Optional[PerformanceReport] = field(default=None, repr=False)
    excess_vs_acwi: Optional[Dict[str, Any]] = None
    trading_days: int = 0
    closed_positions: int = 0

    @property
    def is_pass(self) -> bool:
        return self.overall_status == GateStatus.PASS

    @property
    def days_remaining(self) -> int:
        return max(0, self.days_required - self.days_elapsed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_status": self.overall_status.value,
            "is_pass": self.is_pass,
            "days_elapsed": self.days_elapsed,
            "days_required": self.days_required,
            "days_remaining": self.days_remaining,
            "clock_started_at": (
                self.clock_started_at.isoformat() if self.clock_started_at else None
            ),
            "strategy_engine_version": self.strategy_engine_version,
            "total_trades": self.total_trades,
            "criteria": [c.to_dict() for c in self.criteria],
            "benchmark": self.benchmark.to_dict() if self.benchmark else None,
            "excess_vs_acwi": self.excess_vs_acwi,
            "trading_days": self.trading_days,
            "closed_positions": self.closed_positions,
            "note": self.note,
        }


class PaperTradingGate:
    """
    Evaluates the paper-trading portfolio against the 90-day live-trading gate.

    Wraps a :class:`PaperTrader` and reads its persisted state (performance,
    clock, strategy version) to produce a :class:`PaperGateStatus`.
    """

    def __init__(self, trader: PaperTrader):
        self.trader = trader

    def evaluate(self, benchmark: str = "SPY") -> PaperGateStatus:
        """
        Compute the current gate status.

        Args:
            benchmark: Benchmark ticker for the same-window total-return comparison shown in the status
                (SPY, for continuity). The gate's own excess-return criterion always uses ACWI.

        Returns:
            PaperGateStatus with every criterion, the overall verdict, and the benchmark comparisons.
        """
        report = self.trader.generate_performance_report()
        days_elapsed = self.trader.calendar_days_elapsed()
        days_required = PaperTrader.MIN_VALIDATION_DAYS
        clock_started = self.trader.get_clock_start_date()
        version = self.trader.get_recorded_strategy_version()
        excess = self._safe_excess("ACWI")

        trading_days = report.trading_days if self.trader.daily_snapshots else 0
        closed = report.total_trades

        # "Readiness": is there enough data to judge a criterion yet? Sharpe/drawdown need >= 2
        # snapshots; win rate needs the minimum number of CLOSED positions (a win rate over 3 trades is a
        # coin flip, not a verdict). Before that the criterion is IN_PROGRESS, whatever the number says.
        sharpe_ready = trading_days >= 2
        win_rate_ready = closed >= PaperTrader.MIN_CLOSED_POSITIONS

        def perf_status(passed: bool, ready: bool) -> GateStatus:
            if not ready:
                return GateStatus.IN_PROGRESS
            return GateStatus.PASS if passed else GateStatus.FAIL

        def threshold_status(value: float, minimum: float) -> GateStatus:
            return GateStatus.PASS if value >= minimum else GateStatus.IN_PROGRESS

        excess_status, excess_detail = self._excess_criterion(excess)
        sharpe_detail = ""
        if report.sharpe_ci95 is not None:
            sharpe_detail = (
                f"{report.sharpe_ratio:.2f} (95% CI {report.sharpe_ci95[0]:.1f} to {report.sharpe_ci95[1]:.1f}; "
                f"rf {report.risk_free_rate:.1%} {report.risk_free_source})"
            )

        criteria = [
            GateCriterion(
                name="Sharpe Ratio",
                current_value=report.sharpe_ratio,
                threshold=PaperTrader.MIN_SHARPE_RATIO,
                comparison=">",
                status=perf_status(report.passes_sharpe, sharpe_ready),
                detail=sharpe_detail,
            ),
            GateCriterion(
                name="Max Drawdown",
                current_value=abs(report.max_drawdown_pct),
                threshold=PaperTrader.MAX_DRAWDOWN_PCT,
                comparison="<",
                status=perf_status(report.passes_drawdown, sharpe_ready),
                unit="%",
            ),
            GateCriterion(
                name="Win Rate",
                current_value=report.win_rate,
                threshold=PaperTrader.MIN_WIN_RATE,
                comparison=">",
                status=perf_status(report.passes_win_rate, win_rate_ready),
                unit="%",
            ),
            GateCriterion(
                name="Closed Positions",
                current_value=float(closed),
                threshold=float(PaperTrader.MIN_CLOSED_POSITIONS),
                comparison=">=",
                status=threshold_status(closed, PaperTrader.MIN_CLOSED_POSITIONS),
                unit="n",
            ),
            GateCriterion(
                name="Calendar Days",
                current_value=float(days_elapsed),
                threshold=float(days_required),
                comparison=">=",
                status=threshold_status(days_elapsed, days_required),
                unit="d",
            ),
            GateCriterion(
                name="Trading Days",
                current_value=float(trading_days),
                threshold=float(PaperTrader.MIN_TRADING_DAYS),
                comparison=">=",
                status=threshold_status(trading_days, PaperTrader.MIN_TRADING_DAYS),
                unit="d",
            ),
            GateCriterion(
                name="Excess vs ACWI",
                current_value=float(excess.get("annualized_excess_pct") or 0.0),
                threshold=0.0,
                comparison="CI upper >",
                status=excess_status,
                unit="%",
                detail=excess_detail,
            ),
        ]

        overall = self._overall_status(criteria)
        benchmark_cmp = self._safe_benchmark(benchmark)
        note = self._build_note(overall, days_elapsed, days_required, criteria, trading_days, closed)

        return PaperGateStatus(
            overall_status=overall,
            days_elapsed=days_elapsed,
            days_required=days_required,
            clock_started_at=clock_started,
            strategy_engine_version=version,
            criteria=criteria,
            total_trades=report.total_trades,
            benchmark=benchmark_cmp,
            note=note,
            performance=report,
            excess_vs_acwi=excess,
            trading_days=trading_days,
            closed_positions=closed,
        )

    @staticmethod
    def _excess_criterion(excess: Dict[str, Any]) -> "tuple[GateStatus, str]":
        """Status + display of the excess-vs-ACWI criterion.

        "Not demonstrably worse than the index": PASS while the 95% interval's UPPER bound is above zero,
        FAIL when even the optimistic end is negative. Unavailable data is IN_PROGRESS — absence of a
        number is not a pass and not a fail.
        """
        if not excess.get("available") or not excess.get("ci95_pct") or excess.get("n", 0) < 20:
            reason = excess.get("reason") or f"only {excess.get('n', 0)} aligned days (need 20)"
            return GateStatus.IN_PROGRESS, f"not yet measurable ({reason})"
        lo, hi = excess["ci95_pct"]
        detail = (
            f"{excess['annualized_excess_pct']:+.1f}%/yr (95% CI {lo:+.1f} to {hi:+.1f}, n={excess['n']})"
        )
        return (GateStatus.PASS if hi > 0 else GateStatus.FAIL), detail

    @staticmethod
    def _overall_status(criteria: List[GateCriterion]) -> GateStatus:
        """
        Overall verdict.

        - IN_PROGRESS while ANY evidence requirement is unmet (the 90-day clock, 126 trading days, 30
          closed positions): you cannot pass early, however good the numbers look, and a performance
          criterion that is already FAIL cannot end the window early either.
        - Once every evidence requirement is met: FAIL if any criterion failed, PASS if all passed, and
          IN_PROGRESS if something could not be measured (never a pass on missing data).
        """
        evidence = {"Calendar Days", "Trading Days", "Closed Positions"}
        if any(c.name in evidence and c.status != GateStatus.PASS for c in criteria):
            return GateStatus.IN_PROGRESS
        if any(c.status == GateStatus.FAIL for c in criteria):
            return GateStatus.FAIL
        if all(c.status == GateStatus.PASS for c in criteria):
            return GateStatus.PASS
        return GateStatus.IN_PROGRESS

    def _safe_excess(self, benchmark: str) -> Dict[str, Any]:
        """Excess-return block; never raises (degrades to ``available: False``)."""
        try:
            return self.trader.excess_return_vs(benchmark)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Excess-return computation failed for {benchmark}: {exc}")
            return {"benchmark": benchmark, "available": False, "n": 0, "reason": str(exc)[:120],
                    "annualized_excess_pct": None, "ci95_pct": None}

    def _safe_benchmark(self, benchmark: str) -> Optional[BenchmarkComparison]:
        """Benchmark comparison over the window; never raises (degrades to None)."""
        try:
            return self.trader.compare_to_benchmark(benchmark)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Benchmark comparison failed for {benchmark}: {exc}")
            return None

    @staticmethod
    def _build_note(
        overall: GateStatus,
        days_elapsed: int,
        days_required: int,
        criteria: List[GateCriterion],
        trading_days: int = 0,
        closed: int = 0,
    ) -> str:
        if overall == GateStatus.PASS:
            return (
                "Gate PASSED — the paper book met every criterion with the required evidence. "
                "This is capital protection, not proof of skill (see the AI scoreboard's forecast panel)."
            )
        if overall == GateStatus.IN_PROGRESS:
            waiting = []
            if days_elapsed < days_required:
                waiting.append(f"{days_elapsed}/{days_required} calendar days")
            if trading_days < PaperTrader.MIN_TRADING_DAYS:
                waiting.append(f"{trading_days}/{PaperTrader.MIN_TRADING_DAYS} trading days")
            if closed < PaperTrader.MIN_CLOSED_POSITIONS:
                waiting.append(f"{closed}/{PaperTrader.MIN_CLOSED_POSITIONS} closed positions")
            unmeasured = [c.name for c in criteria if c.status == GateStatus.IN_PROGRESS
                          and c.name not in ("Calendar Days", "Trading Days", "Closed Positions")]
            parts = []
            if waiting:
                parts.append("waiting on " + ", ".join(waiting))
            if unmeasured:
                parts.append("not yet measurable: " + ", ".join(unmeasured))
            return "In progress — " + "; ".join(parts) + "." if parts else "In progress."
        failed = [c.name for c in criteria if c.status == GateStatus.FAIL]
        return "Gate FAILED — unmet after the full window: " + ", ".join(failed or ["unknown"]) + "."


def format_gate_status(status: PaperGateStatus) -> str:
    """Render a PaperGateStatus as a human-readable terminal block."""
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("PAPER TRADING GATE STATUS")
    lines.append("=" * 60)
    lines.append(f"Overall: {status.overall_status.value}")

    if status.clock_started_at:
        lines.append(
            f"Clock started: {status.clock_started_at.isoformat()} "
            f"(strategy_engine_version={status.strategy_engine_version})"
        )
    else:
        lines.append("Clock started: — (no paper trade executed yet)")

    lines.append(
        f"Calendar days: {status.days_elapsed} / {status.days_required} "
        f"({status.days_remaining} remaining); trading days {status.trading_days} / "
        f"{PaperTrader.MIN_TRADING_DAYS}; closed positions {status.closed_positions} / "
        f"{PaperTrader.MIN_CLOSED_POSITIONS}"
    )
    lines.append(f"Validates: the AI pipeline's decisions (baseline engine pinned: {status.strategy_engine_version})")
    lines.append("")
    lines.append("Criteria:")
    for c in status.criteria:
        icon = _STATUS_ICON[c.status]
        lines.append(
            f"  [{icon} {c.status.value:<11}] {c.name:<17}: "
            f"{c.format_value()} (need {c.format_threshold()})"
        )

    if status.benchmark is not None:
        b = status.benchmark
        lines.append("")
        lines.append(
            f"Benchmark ({b.benchmark_ticker}) over window: "
            f"{b.benchmark_ticker} {b.benchmark_return_pct:+.2f}% | "
            f"strategy {b.strategy_return_pct:+.2f}% | "
            f"alpha {b.alpha:+.2f}%"
        )

    lines.append(f"Total paper trades: {status.total_trades}")
    lines.append("")
    if status.is_pass:
        lines.append("🔓 Gate PASSED — IBKR write path may be enabled (operator opt-in). Capital protection, not proof of skill.")
    else:
        lines.append("🔒 Live trading (IBKR write path) is BLOCKED until status = PASS.")
    lines.append("=" * 60)
    return "\n".join(lines)


def assert_live_trading_allowed(
    *,
    trader: Optional[PaperTrader] = None,
    db_path: Optional[str] = None,
    initial_capital: float = 100000.0,
    benchmark: str = "SPY",
) -> PaperGateStatus:
    """
    🔒 SAFETY RAIL: raise unless the paper gate reports PASS.

    This is the ENFORCED check the IBKR write path (Prompt 9) must call before
    placing any live order. It is intentionally a function (not a comment): the
    write path cannot be wired up without it raising until paper trading has
    PASSED over >= 90 calendar days.

    Args:
        trader: An existing PaperTrader (its state is used as-is). If omitted, a
            PaperTrader is constructed and its persisted state loaded.
        db_path: SQLite path when constructing the trader (default location used
            if omitted).
        initial_capital: Starting capital when constructing the trader.
        benchmark: Benchmark ticker for the status report.

    Returns:
        The passing PaperGateStatus.

    Raises:
        LiveTradingBlockedError: if the gate is not PASS.
    """
    if trader is None:
        trader = PaperTrader(
            initial_capital=initial_capital,
            db_path=db_path,
            simulate_delays=False,
        )
        trader.load_state()

    status = PaperTradingGate(trader).evaluate(benchmark=benchmark)
    if not status.is_pass:
        raise LiveTradingBlockedError(
            f"Live trading BLOCKED: paper gate status={status.overall_status.value}. "
            f"{status.note} "
            f"The IBKR write path requires PASS: >= {status.days_required} calendar days, "
            f">= {PaperTrader.MIN_TRADING_DAYS} trading days and >= {PaperTrader.MIN_CLOSED_POSITIONS} closed positions."
        )
    return status
