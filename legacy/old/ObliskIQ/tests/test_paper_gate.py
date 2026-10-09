"""
Unit tests for the Paper-Trading Validation Gate (the 90-day gate)
==================================================================

Covers:
- The 90-day clock: starts at first trade, persists across crash/resume, counts
  CALENDAR days, and resets on a strategy-engine version change.
- Gate verdicts: IN_PROGRESS while the clock runs, PASS after a clean window,
  FAIL after a window with an unmet criterion.
- The enforced live-trading block (assert_live_trading_allowed + IBKR write path).
- Decision → TradeSignal conversion used to feed Phase-4 output to the trader.
"""

import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules.execution.paper_trader import (
    DailySnapshot,
    PaperTrade,
    PaperTrader,
)
from modules.execution.paper_gate import (
    GateStatus,
    LiveTradingBlockedError,
    PaperTradingGate,
    assert_live_trading_allowed,
    format_gate_status,
)
from modules.execution import paper_runner
from modules.models.data_models import Action, TradeSignal

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def trader(temp_db):
    return PaperTrader(initial_capital=100000.0, db_path=temp_db, simulate_delays=False)


_MARKET = {"price": 180.0, "market_cap": 3_000_000_000_000, "avg_volume": 50_000_000}


def _seed_steady_snapshots(t: PaperTrader, days: int) -> None:
    """Steady upward equity curve → positive Sharpe, zero drawdown."""
    for i in range(days):
        value = 100000 * (1 + i * 0.001)
        t.daily_snapshots.append(
            DailySnapshot(
                snapshot_date=date.today() - timedelta(days=days - 1 - i),
                timestamp=datetime.now(timezone.utc),
                total_value=value,
                cash=50000.0,
                equity_value=value - 50000,
                daily_return=0.1,
                total_return=(value / 100000 - 1) * 100,
                positions={},
                trade_count=i,
            )
        )


def _add_closed_trade(t: PaperTrader, ticker: str, buy: float, sell: float) -> None:
    t.portfolio.trade_history.append(
        PaperTrade(trade_id=f"{ticker}-b", timestamp=datetime.now(timezone.utc),
                   ticker=ticker, action="BUY", quantity=10, price=buy)
    )
    t.portfolio.trade_history.append(
        PaperTrade(trade_id=f"{ticker}-s", timestamp=datetime.now(timezone.utc),
                   ticker=ticker, action="SELL", quantity=10, price=sell)
    )


def _add_closed_trades(t: PaperTrader, wins: int, losses: int) -> None:
    """``wins`` +20% round trips and ``losses`` -10% round trips on distinct tickers."""
    base = datetime.now(timezone.utc) - timedelta(days=60)
    for i in range(wins + losses):
        ticker = f"T{i:03d}"
        sell = 120.0 if i < wins else 90.0
        t.portfolio.trade_history.append(PaperTrade(
            trade_id=f"{ticker}-b", timestamp=base + timedelta(hours=2 * i), ticker=ticker,
            action="BUY", quantity=10, price=100.0))
        t.portfolio.trade_history.append(PaperTrade(
            trade_id=f"{ticker}-s", timestamp=base + timedelta(hours=2 * i + 1), ticker=ticker,
            action="SELL", quantity=10, price=sell))


def _benchmark(daily: float):
    """A benchmark-close loader growing ``daily`` per calendar day (0.0 = flat)."""
    def load(ticker, start, end):
        days = (end - start).days + 10
        origin = start - timedelta(days=8)
        return {origin + timedelta(days=i): 100.0 * (1.0 + daily) ** i for i in range(days + 10)}
    return load


def _evidence_book(t: PaperTrader, *, wins=24, losses=8, days=130, bench_daily=0.0) -> None:
    """A book with enough evidence for the gate to rule: >=126 snapshots, >=30 closed positions."""
    _seed_steady_snapshots(t, days)
    _add_closed_trades(t, wins, losses)
    _start_clock(t, days_ago=days)
    t.risk_free_override = 0.04
    t.benchmark_loader = _benchmark(bench_daily)


def _start_clock(t: PaperTrader, days_ago: int, version: str = "v1") -> None:
    t._set_meta(t.CLOCK_START_META_KEY, (date.today() - timedelta(days=days_ago)).isoformat())
    t._set_meta(t.STRATEGY_VERSION_META_KEY, version)


# ---------------------------------------------------------------------------
# Clock: start, persistence, calendar days, version reset
# ---------------------------------------------------------------------------

def test_clock_starts_at_first_trade(trader):
    assert trader.get_clock_start_date() is None

    with patch.object(trader, "_fetch_market_data", return_value=_MARKET):
        trader.execute_signal(
            TradeSignal(ticker="AAPL", action=Action.BUY, confidence=0.8, position_size_pct=0.10)
        )

    assert trader.get_clock_start_date() == date.today()
    assert trader.get_recorded_strategy_version() is not None
    print("Clock start test PASSED")


def test_clock_idempotent(trader):
    """A second trade must NOT move the clock start date."""
    with patch.object(trader, "_fetch_market_data", return_value=_MARKET):
        trader.execute_signal(
            TradeSignal(ticker="AAPL", action=Action.BUY, confidence=0.8, position_size_pct=0.05)
        )
        # Force a stale start date, then trade again — it must be preserved.
        trader._set_meta(trader.CLOCK_START_META_KEY, "2020-01-01")
        trader.execute_signal(
            TradeSignal(ticker="MSFT", action=Action.BUY, confidence=0.8, position_size_pct=0.05)
        )
    assert trader.get_clock_start_date() == date(2020, 1, 1)
    print("Clock idempotent test PASSED")


def test_clock_survives_crash_and_resume(temp_db):
    """Simulate a mid-day crash: a fresh trader on the same DB recovers clock + positions."""
    t1 = PaperTrader(initial_capital=100000.0, db_path=temp_db, simulate_delays=False)
    with patch.object(t1, "_fetch_market_data", return_value=_MARKET):
        t1.execute_signal(
            TradeSignal(ticker="AAPL", action=Action.BUY, confidence=0.8, position_size_pct=0.10)
        )
    clock = t1.get_clock_start_date()
    version = t1.get_recorded_strategy_version()
    positions = len(t1.portfolio.positions)
    assert clock is not None

    # "Crash": drop t1, build a new trader pointing at the same DB.
    t2 = PaperTrader(initial_capital=100000.0, db_path=temp_db, simulate_delays=False)
    loaded = t2.load_state()

    assert loaded
    assert t2.get_clock_start_date() == clock          # clock survived
    assert t2.get_recorded_strategy_version() == version
    assert len(t2.portfolio.positions) == positions     # positions survived
    print("Crash/resume test PASSED")


def test_calendar_days_elapsed(trader):
    _start_clock(trader, days_ago=37)
    assert trader.calendar_days_elapsed() == 37
    # No clock → zero
    trader.reset()
    assert trader.calendar_days_elapsed() == 0
    print("Calendar days test PASSED")


def test_version_change_resets_clock(trader):
    with patch.object(trader, "_fetch_market_data", return_value=_MARKET):
        trader.execute_signal(
            TradeSignal(ticker="AAPL", action=Action.BUY, confidence=0.8, position_size_pct=0.10)
        )
    assert trader.get_clock_start_date() is not None
    assert len(trader.portfolio.trade_history) == 1

    recorded = trader.get_recorded_strategy_version()
    changed = trader.check_and_handle_version_change(current_version=recorded + "-CHANGED")

    assert changed is True
    assert trader.get_clock_start_date() is None             # clock reset
    assert len(trader.portfolio.trade_history) == 0          # state wiped
    assert trader.get_recorded_strategy_version() == recorded + "-CHANGED"
    print("Version reset test PASSED")


def test_version_unchanged_no_reset(trader):
    with patch.object(trader, "_fetch_market_data", return_value=_MARKET):
        trader.execute_signal(
            TradeSignal(ticker="AAPL", action=Action.BUY, confidence=0.8, position_size_pct=0.10)
        )
    recorded = trader.get_recorded_strategy_version()
    assert trader.check_and_handle_version_change(current_version=recorded) is False
    assert trader.get_clock_start_date() is not None
    print("Version unchanged test PASSED")


# ---------------------------------------------------------------------------
# Gate verdicts
# ---------------------------------------------------------------------------

def test_gate_in_progress_before_90_days(trader):
    _seed_steady_snapshots(trader, 30)
    _add_closed_trades(trader, 1, 0)
    _start_clock(trader, days_ago=30)
    trader.risk_free_override, trader.benchmark_loader = 0.04, _benchmark(0.0)

    status = PaperTradingGate(trader).evaluate()

    assert status.overall_status == GateStatus.IN_PROGRESS
    assert not status.is_pass
    assert status.days_elapsed == 30
    assert status.days_remaining == 60
    print("Gate IN_PROGRESS test PASSED")


def test_perfect_numbers_cannot_pass_early_without_the_evidence(trader):
    """The old gate passed after 95 days and ONE closed trade. Neither 95 days nor 1 trade is evidence."""
    _seed_steady_snapshots(trader, 95)
    _add_closed_trades(trader, 1, 0)                 # 100% win rate on a single position
    _start_clock(trader, days_ago=95)
    trader.risk_free_override, trader.benchmark_loader = 0.04, _benchmark(0.0)

    status = PaperTradingGate(trader).evaluate()

    assert status.overall_status == GateStatus.IN_PROGRESS
    assert "126 trading days" in status.note and "30 closed positions" in status.note
    by_name = {c.name: c for c in status.criteria}
    assert by_name["Win Rate"].status == GateStatus.IN_PROGRESS      # 1 trade is not a win rate
    assert by_name["Calendar Days"].status == GateStatus.PASS        # the old clock criterion alone is met


def test_an_early_bad_stretch_cannot_fail_the_gate_before_the_window_is_complete(trader):
    """A criterion may already read FAIL mid-window, but the verdict waits for the evidence either way."""
    _curve(trader, [-0.004, 0.001] * 20)             # 41 snapshots, steadily losing
    _start_clock(trader, days_ago=40)
    trader.risk_free_override, trader.benchmark_loader = 0.04, _benchmark(0.0)

    status = PaperTradingGate(trader).evaluate()

    sharpe = next(c for c in status.criteria if c.name == "Sharpe Ratio")
    assert sharpe.status == GateStatus.FAIL           # the number is already bad ...
    assert status.overall_status == GateStatus.IN_PROGRESS   # ... but the window has not run its course


def test_gate_pass_with_the_required_evidence(trader):
    _evidence_book(trader, wins=24, losses=8)        # 32 closed positions, 75% win rate, 130 days
    status = PaperTradingGate(trader).evaluate()

    print(format_gate_status(status))
    assert {c.name for c in status.criteria} == {
        "Sharpe Ratio", "Max Drawdown", "Win Rate", "Closed Positions", "Calendar Days", "Trading Days",
        "Excess vs ACWI"}
    assert all(c.passed for c in status.criteria), [c.to_dict() for c in status.criteria]
    assert status.overall_status == GateStatus.PASS and status.is_pass
    assert "not proof of skill" in status.note


def test_gate_fail_after_window_with_bad_winrate(trader):
    _evidence_book(trader, wins=8, losses=24)        # 25% win rate over 32 closed positions
    status = PaperTradingGate(trader).evaluate()

    assert status.overall_status == GateStatus.FAIL
    win_rate = next(c for c in status.criteria if c.name == "Win Rate")
    assert win_rate.status == GateStatus.FAIL
    assert "Win Rate" in status.note


def test_gate_fails_when_the_book_is_demonstrably_worse_than_the_world_index(trader):
    _evidence_book(trader, bench_daily=0.01)         # ACWI +1%/day vs the book's ~+0.1%: far behind, with a tight CI
    status = PaperTradingGate(trader).evaluate()

    excess = next(c for c in status.criteria if c.name == "Excess vs ACWI")
    assert excess.status == GateStatus.FAIL
    assert status.overall_status == GateStatus.FAIL
    assert "Excess vs ACWI" in status.note


def test_missing_benchmark_data_is_never_a_pass_and_never_a_fail(trader):
    _evidence_book(trader)

    def offline(ticker, start, end):
        raise RuntimeError("Yahoo 429")

    trader.benchmark_loader = offline
    status = PaperTradingGate(trader).evaluate()

    excess = next(c for c in status.criteria if c.name == "Excess vs ACWI")
    assert excess.status == GateStatus.IN_PROGRESS and "not yet measurable" in excess.format_value()
    assert status.overall_status == GateStatus.IN_PROGRESS
    assert not status.is_pass


def test_gate_fresh_trader_in_progress(trader):
    """No trades at all → IN_PROGRESS, no clock, perf criteria not yet ready."""
    status = PaperTradingGate(trader).evaluate()
    assert status.overall_status == GateStatus.IN_PROGRESS
    assert status.clock_started_at is None
    assert status.days_elapsed == 0
    print("Fresh trader gate test PASSED")


# ---------------------------------------------------------------------------
# Enforced live-trading block
# ---------------------------------------------------------------------------

def test_assert_live_trading_allowed_raises_when_not_pass(trader):
    with pytest.raises(LiveTradingBlockedError):
        assert_live_trading_allowed(trader=trader)
    print("assert_live_trading_allowed raises test PASSED")


def test_assert_live_trading_allowed_returns_when_pass(trader):
    _evidence_book(trader)

    status = assert_live_trading_allowed(trader=trader)
    assert status.is_pass
    print("assert_live_trading_allowed pass test PASSED")


def test_ibkr_write_blocked_when_live_disabled(monkeypatch):
    from modules.execution.ibkr_client import IBKRClient
    from modules.config.settings import config

    monkeypatch.setattr(config.system, "live_trading_enabled", False)
    client = IBKRClient(secure_mode=True)
    with pytest.raises(LiveTradingBlockedError):
        client.assert_write_allowed()
    print("IBKR disabled-flag block test PASSED")


def test_ibkr_place_order_blocked(monkeypatch):
    import asyncio
    from modules.execution.ibkr_client import IBKRClient
    from modules.config.settings import config

    monkeypatch.setattr(config.system, "live_trading_enabled", False)
    client = IBKRClient(secure_mode=True)
    with pytest.raises(LiveTradingBlockedError):
        asyncio.run(client.place_order(session=None, order={"ticker": "AAPL"}))
    print("IBKR place_order block test PASSED")


# ---------------------------------------------------------------------------
# Decision extraction / conversion (paper_runner)
# ---------------------------------------------------------------------------

def test_extract_portfolio_decisions_from_pipeline_summary():
    summary = {
        "status": "complete",
        "phase4": {
            "decisions_count": 2,
            "decisions": {
                "portfolio_decisions": [
                    {"ticker": "AAPL", "action": "BUY", "position_size": 0.07},
                    {"ticker": "MSFT", "action": "HOLD"},
                ]
            },
        },
    }
    decisions = paper_runner.extract_portfolio_decisions(summary)
    assert len(decisions) == 2
    assert decisions[0]["ticker"] == "AAPL"
    print("Extract from summary test PASSED")


def test_extract_portfolio_decisions_handles_empty():
    assert paper_runner.extract_portfolio_decisions({"status": "aborted"}) == []
    assert paper_runner.extract_portfolio_decisions(None) == []
    print("Extract empty test PASSED")


def test_decision_to_signal_filters_hold():
    assert paper_runner.decision_to_signal({"ticker": "AAPL", "action": "HOLD"}) is None
    assert paper_runner.decision_to_signal({"action": "BUY"}) is None  # no ticker
    print("decision_to_signal HOLD filter test PASSED")


def test_decision_to_signal_normalizes_size_and_confidence():
    # position_size is a fraction; position_size_pct is a percent.
    sig_frac = paper_runner.decision_to_signal(
        {"ticker": "AAPL", "action": "BUY", "position_size": 0.08, "confidence": 8}
    )
    assert sig_frac is not None
    assert abs(sig_frac.position_size_pct - 0.08) < 1e-9
    assert abs(sig_frac.confidence - 0.8) < 1e-9  # 1-10 conviction → 0-1

    sig_pct = paper_runner.decision_to_signal(
        {"symbol": "MSFT", "action": "BUY", "position_size_pct": 12.0, "confidence": 0.9}
    )
    assert sig_pct is not None
    assert sig_pct.ticker == "MSFT"
    assert abs(sig_pct.position_size_pct - 0.12) < 1e-9
    assert abs(sig_pct.confidence - 0.9) < 1e-9
    print("decision_to_signal normalization test PASSED")


def test_feed_decisions_executes(trader):
    summary = {
        "phase4": {
            "decisions": {
                "portfolio_decisions": [
                    {"ticker": "AAPL", "action": "BUY", "position_size": 0.10},
                    {"ticker": "MSFT", "action": "HOLD"},
                ]
            }
        }
    }
    with patch.object(trader, "_fetch_market_data", return_value=_MARKET):
        executed = paper_runner.feed_decisions(trader, summary)

    assert len(executed) == 1            # HOLD filtered out
    assert executed[0].ticker == "AAPL"
    assert trader.get_clock_start_date() == date.today()  # feeding started the clock
    print("feed_decisions test PASSED")


# ---------------------------------------------------------------------------
# Weekly report
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Session orchestration (end-to-end wiring with a fake orchestrator)
# ---------------------------------------------------------------------------

class _FakeOrchestrator:
    """Minimal stand-in for ObliskOrchestrator.run_full_pipeline."""

    def __init__(self, decisions):
        self._decisions = decisions

    def run_full_pipeline(self, mock=False, resume_run_id=None):
        return {
            "status": "complete",
            "phase4": {
                "decisions_count": len(self._decisions),
                "decisions": {"portfolio_decisions": self._decisions},
            },
        }


def test_run_paper_session_end_to_end(trader, tmp_path):
    decisions = [
        {"ticker": "AAPL", "action": "BUY", "position_size": 0.10, "confidence": 8},
        {"ticker": "MSFT", "action": "HOLD"},
    ]
    orch = _FakeOrchestrator(decisions)

    with patch.object(trader, "_fetch_market_data", return_value=_MARKET):
        session = paper_runner.run_paper_session(
            orch, trader, mock=True, reports_dir=str(tmp_path / "paper")
        )

    assert len(session["executed_trades"]) == 1                  # HOLD filtered
    assert session["snapshot"].total_value > 0
    assert session["gate_status"].overall_status == GateStatus.IN_PROGRESS
    assert session["weekly_report_path"] is not None
    assert os.path.exists(session["weekly_report_path"])
    assert trader.get_clock_start_date() == date.today()         # clock started

    summary = paper_runner.format_session_summary(session)
    assert "PAPER TRADING GATE STATUS" in summary
    assert "Trades executed this run: 1" in summary
    print("run_paper_session end-to-end test PASSED")


def test_run_paper_session_resets_clock_on_version_change(trader, tmp_path):
    # Pre-seed an old clock under a stale strategy version.
    _start_clock(trader, days_ago=40, version="LEGACY-vX")
    assert trader.calendar_days_elapsed() == 40

    orch = _FakeOrchestrator([{"ticker": "AAPL", "action": "BUY", "position_size": 0.05}])
    with patch.object(trader, "_fetch_market_data", return_value=_MARKET):
        session = paper_runner.run_paper_session(
            orch, trader, mock=True, reports_dir=str(tmp_path / "paper")
        )

    assert session["version_reset"] is True
    # Clock restarted today at the trade fed this run (not the 40-day-old start).
    assert trader.calendar_days_elapsed() == 0
    assert trader.get_clock_start_date() == date.today()
    print("run_paper_session version-reset test PASSED")


def test_weekly_report_once_per_week(trader, tmp_path):
    _seed_steady_snapshots(trader, 10)
    _start_clock(trader, days_ago=10)
    status = PaperTradingGate(trader).evaluate()

    out = str(tmp_path / "paper")
    first = paper_runner.generate_weekly_report(trader, status, out_dir=out)
    second = paper_runner.generate_weekly_report(trader, status, out_dir=out)

    assert first is not None and os.path.exists(first)
    assert second is None  # same ISO week → not regenerated
    content = open(first).read()
    assert "Paper Trading — Weekly Summary" in content
    assert "90-Day Gate" in content
    print("Weekly report test PASSED")


# ---------------------------------------------------------------------------
# Honest statistics (v6.16.0): measured risk-free, Sharpe interval, excess vs ACWI
# ---------------------------------------------------------------------------

def _curve(t: PaperTrader, returns):
    value, start = 100_000.0, date.today() - timedelta(days=len(returns) + 1)
    snaps = [value]
    for r in returns:
        value *= 1.0 + r
        snaps.append(value)
    t.daily_snapshots = [
        DailySnapshot(snapshot_date=start + timedelta(days=i), timestamp=datetime.now(timezone.utc),
                      total_value=v, cash=0.0, equity_value=v, daily_return=0.0, total_return=0.0,
                      positions={}, trade_count=0)
        for i, v in enumerate(snaps)
    ]


def test_sharpe_is_hand_computed_from_the_measured_risk_free_rate(trader):
    # 100 daily returns alternating +0.2% / 0.0%: mean 0.001; sample sd = sqrt(100*1e-6/99) = 0.00100504
    _curve(trader, [0.002, 0.0] * 50)
    trader.risk_free_override = 0.04
    report = trader.generate_performance_report()
    sd = (100 * 1e-6 / 99) ** 0.5
    expected = (0.001 - 0.04 / 252) / sd * (252 ** 0.5)
    assert report.sharpe_ratio == pytest.approx(expected, rel=1e-6)
    assert report.risk_free_rate == 0.04 and report.risk_free_source == "override"
    lo, hi = report.sharpe_ci95
    assert lo < report.sharpe_ratio < hi and hi - lo > 5     # a ~100-day Sharpe is a WIDE estimate

    trader.risk_free_override = 0.10
    assert trader.generate_performance_report().sharpe_ratio < report.sharpe_ratio


def test_risk_free_rate_is_measured_then_falls_back_loudly(trader, monkeypatch):
    async def tbill():
        return 4.3

    monkeypatch.setattr(PaperTrader, "_fetch_tbill_yield_pct", staticmethod(tbill))
    assert trader.risk_free_rate() == (pytest.approx(0.043), "FRED:DGS3MO")

    async def down():
        raise RuntimeError("FRED unreachable")

    fresh = PaperTrader(db_path=trader.db_path, simulate_delays=False)
    monkeypatch.setattr(PaperTrader, "_fetch_tbill_yield_pct", staticmethod(down))
    assert fresh.risk_free_rate() == (PaperTrader.RISK_FREE_FALLBACK, "fallback_constant")


def test_excess_return_vs_benchmark_is_hand_computed(trader):
    _curve(trader, [0.002] * 60)                        # the book: +0.2% a day
    trader.benchmark_loader = _benchmark(0.001)         # the index: +0.1% a calendar day
    result = trader.excess_return_vs("ACWI")
    assert result["available"] and result["n"] == 60
    assert result["annualized_excess_pct"] == pytest.approx(0.001 * 252 * 100, rel=0.02)
    lo, hi = result["ci95_pct"]
    assert lo <= result["annualized_excess_pct"] <= hi


def test_excess_return_is_unavailable_not_zero_when_data_is_missing(trader):
    _curve(trader, [0.001] * 10)
    trader.benchmark_loader = lambda *a: (_ for _ in ()).throw(RuntimeError("no prices"))
    out = trader.excess_return_vs()
    assert out["available"] is False and "unavailable" in out["reason"] and out["annualized_excess_pct"] is None
    trader.daily_snapshots = trader.daily_snapshots[:2]
    assert trader.excess_return_vs()["reason"] == "fewer than 3 daily snapshots"


def test_validation_flag_needs_the_new_evidence(trader):
    _curve(trader, [0.001] * 60)
    _add_closed_trades(trader, 40, 0)
    trader.risk_free_override = 0.0
    assert trader.generate_performance_report().is_validated is False      # 61 snapshots < 126
    _curve(trader, [0.001, 0.0012] * 70)
    assert trader.generate_performance_report().is_validated is True
    trader.portfolio.trade_history = []
    _add_closed_trades(trader, 5, 0)
    assert trader.generate_performance_report().is_validated is False      # 5 closed positions < 30
