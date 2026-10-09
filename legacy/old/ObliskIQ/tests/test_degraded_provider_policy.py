"""
Degraded-provider policy: a run answered by a small-window provider (pruned briefing)
may hold or exit positions but never open new ones.
"""

import logging

import pytest

pytest.importorskip("yfinance")
pytest.importorskip("vaderSentiment")

from modules.execution.orchestrator import ObliskOrchestrator

pytestmark = pytest.mark.unit


@pytest.fixture
def orch():
    stub = ObliskOrchestrator.__new__(ObliskOrchestrator)
    stub.logger = logging.getLogger("test_degraded_provider_policy")
    return stub


def decisions(degraded):
    return {
        "ai_provider": {"provider": "groq", "label": "groq:openai/gpt-oss-120b", "degraded": degraded},
        "portfolio_decisions": [
            {"ticker": "EWJ", "action": "BUY"},
            {"ticker": "INDA", "action": "SELL"},
            {"ticker": "EWZ", "action": "HOLD"},
        ],
    }


class TestDegradedProviderPolicy:
    def test_full_context_provider_is_untouched(self, orch):
        data = decisions(degraded=False)
        summary = orch._apply_degraded_provider_policy(data)
        assert summary == {"degraded": False, "provider": "groq:openai/gpt-oss-120b", "blocked_buys": []}
        assert [d["action"] for d in data["portfolio_decisions"]] == ["BUY", "SELL", "HOLD"]

    def test_degraded_provider_blocks_new_buys_only(self, orch):
        data = decisions(degraded=True)
        summary = orch._apply_degraded_provider_policy(data)
        assert [d["action"] for d in data["portfolio_decisions"]] == ["HOLD", "SELL", "HOLD"]
        assert summary["blocked_buys"] == ["EWJ"]

    def test_original_action_is_kept_for_the_audit_trail(self, orch):
        data = decisions(degraded=True)
        orch._apply_degraded_provider_policy(data)
        blocked = data["portfolio_decisions"][0]
        assert blocked["original_action"] == "BUY" and blocked["degraded_provider_block"] is True
        assert "degraded_provider_block" not in data["portfolio_decisions"][1]

    def test_legacy_decisions_key_is_honoured(self, orch):
        data = {"ai_provider": {"degraded": True}, "decisions": [{"symbol": "AAPL", "action": "buy"}]}
        summary = orch._apply_degraded_provider_policy(data)
        assert data["decisions"][0]["action"] == "HOLD"
        assert summary["blocked_buys"] == ["AAPL"]

    def test_missing_provenance_means_not_degraded(self, orch):
        data = {"portfolio_decisions": [{"ticker": "EWJ", "action": "BUY"}]}
        assert orch._apply_degraded_provider_policy(data)["degraded"] is False
        assert data["portfolio_decisions"][0]["action"] == "BUY"
