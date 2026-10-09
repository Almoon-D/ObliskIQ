"""
Tests for the trade-sheet formatter (modules/notifications/run_summary.py) and the
scripts/notify_run_summary.py delivery wrapper. Pure formatting + a fake notifier:
no network.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

from modules.notifications import run_summary as rs

pytestmark = pytest.mark.unit

_SPEC = importlib.util.spec_from_file_location(
    "notify_run_summary", Path(__file__).parent.parent / "scripts" / "notify_run_summary.py"
)
notify = importlib.util.module_from_spec(_SPEC)
sys.modules["notify_run_summary"] = notify
_SPEC.loader.exec_module(notify)


def buy(ticker="EWJ", **over):
    base = {
        "ticker": ticker, "action": "BUY", "position_size_pct": 8.0, "entry_price": 70.0,
        "stop_loss_pct": -0.114, "target_price": 91.0, "conviction_contract": 0.7,
        "thesis": "Governance reform keeps re-rating Japanese equities.",
    }
    base.update(over)
    return base


def sheet(**over):
    base = {
        "market_regime": "bull",
        "portfolio_decisions": [buy(), {"ticker": "INDA", "action": "SELL", "position_size_pct": 5.0,
                                        "entry_price": 50.0, "stop_loss_pct": -0.08, "conviction": 6},
                                {"ticker": "EWZ", "action": "HOLD"}],
        "rejected_decisions": [{"ticker": "NVDA", "action": "BUY", "rejection_reason": "Stop loss -30.0% is wider than limit -15.0%"}],
        "ai_provider": {"label": "gemini:gemini-3.8-flash", "degraded": False, "attempts": [{"provider": "gemini", "outcome": "ok"}]},
        "continuity_guard": {"flips_rejected": [{"ticker": "AAPL", "reason": "uncited"}]},
    }
    base.update(over)
    return base


class TestFormatting:
    def test_a_sell_line_shows_no_stop_or_target(self):
        """The Phase-4 contract needs positive numbers, so the AI wrote stop 0.01 / target 0.01 for a SELL (Paper
        Run #189) and the sheet printed "stop 0.01 (-100.0%)" — a stop that does not exist on an exit order."""
        sell = {"ticker": "GE", "action": "SELL", "position_size": 0.059, "entry_price": 309.56,
                "stop_loss": 0.01, "stop_loss_price": 0.01, "target_price": 0.01, "conviction": 6}
        _, body = rs.format_run_summary(sheet(portfolio_decisions=[sell]))
        line = next(ln for ln in body.splitlines() if "SELL GE" in ln)
        assert "entry 309.56" in line
        assert "stop" not in line and "target" not in line and "100.0%" not in line

    def test_regime_falls_back_to_the_optimizers_when_the_sheet_has_no_top_level_one(self):
        """The decisions file has no top-level market_regime, so every real sheet said "regime: unknown"."""
        data = sheet()
        del data["market_regime"]
        data["portfolio_optimization"] = {"market_regime": "normal"}
        _, body = rs.format_run_summary(data)
        assert "regime: normal" in body and "regime: unknown" not in body

    def test_subject_counts_buys_and_sells(self):
        subject, _ = rs.format_run_summary(sheet(), run_label="pre-open")
        assert subject == "ObliskIQ pre-open: 1 BUY / 1 SELL"

    def test_buy_line_has_everything_needed_to_execute(self):
        _, body = rs.format_run_summary(sheet())
        line = next(ln for ln in body.splitlines() if "BUY EWJ" in ln)
        assert "8.0% of portfolio" in line and "entry 70.00" in line
        assert "target 91.00" in line and "conviction 7/10" in line
        assert "stop 62.02 (-11.4%)" in line  # price derived from entry + fraction
        assert "Governance reform" in body

    def test_explicit_stop_price_wins_over_derived(self):
        _, body = rs.format_run_summary(sheet(portfolio_decisions=[buy(stop_loss=60.0)]))
        assert "stop 60.00 (-11.4%)" in body

    def test_percent_point_stop_is_displayed_correctly_too(self):
        _, body = rs.format_run_summary(sheet(portfolio_decisions=[buy(stop_loss_pct=-12.0)]))
        assert "(-12.0%)" in body

    def test_holds_and_blocked_are_separated_from_actions(self):
        _, body = rs.format_run_summary(sheet())
        assert "HOLD: EWZ" in body
        assert "BLOCKED by risk gates (1) — not for execution:" in body
        assert "NVDA BUY: Stop loss -30.0% is wider" in body

    def test_provider_and_continuity_guard_are_reported(self):
        _, body = rs.format_run_summary(sheet())
        assert "AI: gemini:gemini-3.8-flash · regime: bull" in body
        assert "Continuity guard rejected 1 uncited same-day flip" in body

    def test_degraded_provider_is_loudly_flagged(self):
        data = sheet(ai_provider={"label": "groq:x", "degraded": True}, degraded_provider_policy={"blocked_buys": ["EWJ"]})
        _, body = rs.format_run_summary(data)
        assert "DEGRADED AI PROVIDER" in body

    def test_failover_is_visible(self):
        provider = {"label": "nvidia:m", "degraded": False,
                    "attempts": [{"provider": "gemini", "outcome": "failed"}, {"provider": "nvidia", "outcome": "ok"}]}
        _, body = rs.format_run_summary(sheet(ai_provider=provider))
        assert "failover: gemini failed" in body

    def test_no_actionable_trades(self):
        subject, body = rs.format_run_summary(sheet(portfolio_decisions=[{"ticker": "EWZ", "action": "HOLD"}]))
        assert "no trades to execute" in subject
        assert "Nothing to execute" in body

    def test_missing_fields_degrade_to_na_not_a_crash(self):
        _, body = rs.format_run_summary({"portfolio_decisions": [{"ticker": "X", "action": "BUY"}]})
        assert "BUY X — n/a of portfolio · entry n/a · stop n/a · target n/a" in body

    def test_long_thesis_is_truncated(self):
        _, body = rs.format_run_summary(sheet(portfolio_decisions=[buy(thesis="word " * 200)]))
        assert "…" in body and len(max(body.splitlines(), key=len)) < 400

    def test_more_than_five_rejections_are_summarised(self):
        rejected = [{"ticker": f"T{i}", "action": "BUY", "rejection_reason": "r"} for i in range(8)]
        _, body = rs.format_run_summary(sheet(rejected_decisions=rejected))
        assert "and 3 more" in body

    def test_always_says_paper_only(self):
        assert "Paper-trading only" in rs.format_run_summary(sheet())[1]


class TestTelegram:
    def test_html_is_escaped_and_headline_bold(self):
        chunks = rs.to_telegram_html("A & B", "x < y > z & <script>")
        assert chunks[0].startswith("<b>A &amp; B</b>")
        assert "&lt;script&gt;" in chunks[0] and "<script>" not in chunks[0]

    def test_long_messages_are_split_under_the_cap(self):
        body = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        chunks = rs.to_telegram_html("s", body)
        assert len(chunks) > 1 and all(len(c) <= rs.TELEGRAM_CHUNK + 200 for c in chunks)
        assert "".join(chunks).count("line 199") == 1  # nothing lost


class TestFindingTheSheet:
    def test_picks_the_newest_and_respects_since(self, tmp_path):
        old = tmp_path / "phase4_decisions_1.json"
        new = tmp_path / "phase4_decisions_2.json"
        old.write_text("{}")
        new.write_text("{}")
        past = time.time() - 3600
        os.utime(old, (past, past))
        assert rs.find_latest_trade_sheet(tmp_path) == new
        assert rs.find_latest_trade_sheet(tmp_path, since_epoch=time.time() - 60) == new
        assert rs.find_latest_trade_sheet(tmp_path, since_epoch=time.time() + 60) is None

    def test_empty_directory(self, tmp_path):
        assert rs.find_latest_trade_sheet(tmp_path) is None

    def test_non_object_payload_is_rejected(self, tmp_path):
        bad = tmp_path / "phase4_decisions_x.json"
        bad.write_text("[1]")
        with pytest.raises(ValueError):
            rs.load_trade_sheet(bad)


class TestDeliveryScript:
    def test_message_from_a_real_file(self, tmp_path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "phase4_decisions_1.json").write_text(json.dumps(sheet()))
        subject, body, found = notify.build_message(out, tmp_path / "no_digest.md", "pre-close", None)
        assert found and "1 BUY / 1 SELL" in subject and "BUY EWJ" in body

    def test_no_sheet_this_run_says_so_instead_of_staying_silent(self, tmp_path):
        out = tmp_path / "outputs"
        out.mkdir()
        subject, body, found = notify.build_message(out, tmp_path / "d.md", "pre-open", None)
        assert not found and "no trade sheet produced" in subject

    def test_a_stale_sheet_from_an_earlier_run_is_not_resent(self, tmp_path):
        out = tmp_path / "outputs"
        out.mkdir()
        stale = out / "phase4_decisions_old.json"
        stale.write_text(json.dumps(sheet()))
        past = time.time() - 7200
        os.utime(stale, (past, past))
        _, _, found = notify.build_message(out, tmp_path / "d.md", "x", since=time.time() - 60)
        assert not found

    def test_delivery_never_raises_and_reports_unconfigured(self, monkeypatch, capsys):
        for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECIPIENT"):
            monkeypatch.delenv(var, raising=False)
        assert notify.deliver("subject", "body") is False
        assert "No notification channel is configured" in capsys.readouterr().err
