"""
Tests for the ContinuityGuard (v6.12.0, Phase-4 Decision Contract)

The guard is deterministic engine-side logic: default HOLD for every open
position; a same-day flip (SELL on a position opened the same UTC trading
day) executes only with a valid citation — a FIRED ledger trigger or
materially new information absent from the prior run's payload.

Fixtures under tests/fixtures/phase4_responses simulate the AI responses
(the same files exercise the contract parser end-to-end).
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from modules.execution.continuity_guard import (
    MIN_NEW_INFO_CHARS,
    ContinuityGuard,
)
from modules.models.schemas import contract_decision_to_dict, parse_phase4_response

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "phase4_responses"

NOW = datetime(2026, 7, 3, 19, 0, tzinfo=timezone.utc)  # afternoon run
SAME_DAY_TS = "2026-07-03T12:45:00+00:00"  # morning run, same trading day
PRIOR_DAY_TS = "2026-07-01T12:45:00+00:00"


@pytest.fixture
def guard():
    return ContinuityGuard(now_fn=lambda: NOW)


@pytest.fixture
def open_entry():
    """Ledger entry opened by the same-day morning run"""
    return {
        "ticker": "005930.KS",
        "entry_ts": SAME_DAY_TS,
        "run_id": "20260703_124500",
        "entry_thesis": "Memory upcycle underpriced.",
        "invalidation_triggers": [
            {
                "trigger_id": "smsn-t1",
                "type": "price_below",
                "threshold": 61000,
                "description": "Breaks accumulation support",
            },
            {
                "trigger_id": "smsn-t2",
                "type": "date_after",
                "trigger_date": "2026-09-30",
                "description": "No catalyst by end of Q3",
            },
            {
                "trigger_id": "smsn-t3",
                "type": "event",
                "description": "Key customer dual-sources away",
            },
        ],
        "status": "open",
    }


def decisions_from_fixture(name: str):
    """Parse a fixture through the real contract layer into decision dicts"""
    response = parse_phase4_response((FIXTURES / name).read_text())
    return [contract_decision_to_dict(d) for d in response.portfolio_decisions]


# ============================================================================
# DEFAULT HOLD
# ============================================================================


class TestDefaultHold:
    def test_omitted_open_position_gets_default_hold(self, guard, open_entry):
        decisions = []
        summary = guard.enforce(decisions, [open_entry])
        assert summary["defaults_applied"] == ["005930.KS"]
        assert len(decisions) == 1
        assert decisions[0]["action"] == "HOLD"
        assert decisions[0]["source"] == "continuity_guard_default"

    def test_position_with_verdict_gets_no_default(self, guard, open_entry):
        decisions = [{"ticker": "005930.KS", "action": "HOLD"}]
        summary = guard.enforce(decisions, [open_entry])
        assert summary["defaults_applied"] == []
        assert len(decisions) == 1

    def test_no_open_positions_is_noop(self, guard):
        decisions = [{"ticker": "AAPL", "action": "BUY"}]
        summary = guard.enforce(decisions, [])
        assert summary["open_positions"] == []
        assert summary["defaults_applied"] == []
        assert decisions[0]["action"] == "BUY"


# ============================================================================
# SAME-DAY FLIP RULE
# ============================================================================


class TestSameDayFlips:
    def test_uncited_same_day_flip_rejected(self, guard, open_entry):
        decisions = decisions_from_fixture("flip_without_citation.json")
        summary = guard.enforce(decisions, [open_entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "HOLD"
        assert decisions[0]["rejected_flip"]["original_action"] == "SELL"
        assert summary["flips_rejected"][0]["ticker"] == "005930.KS"

    def test_fired_price_trigger_citation_allows_flip(self, guard, open_entry):
        decisions = decisions_from_fixture("flip_with_trigger_citation.json")
        summary = guard.enforce(decisions, [open_entry], {"005930.KS": 59000.0})
        assert decisions[0]["action"] == "SELL"
        assert "trigger:smsn-t1" in decisions[0]["flip_citation"]
        assert summary["flips_allowed"][0]["ticker"] == "005930.KS"

    def test_unfired_price_trigger_citation_rejected(self, guard, open_entry):
        decisions = decisions_from_fixture("flip_with_trigger_citation.json")
        summary = guard.enforce(decisions, [open_entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "HOLD"
        assert "NOT fired" in summary["flips_rejected"][0]["reason"]

    def test_unknown_cited_trigger_rejected(self, guard, open_entry):
        decisions = [
            {"ticker": "005930.KS", "action": "SELL", "cites_trigger_id": "ghost-id"}
        ]
        summary = guard.enforce(decisions, [open_entry], {"005930.KS": 59000.0})
        assert decisions[0]["action"] == "HOLD"
        assert "not found" in summary["flips_rejected"][0]["reason"]

    def test_missing_price_makes_citation_conservative(self, guard, open_entry):
        """No price available → price citation cannot authorize the flip"""
        decisions = decisions_from_fixture("flip_with_trigger_citation.json")
        summary = guard.enforce(decisions, [open_entry], current_prices={})
        assert decisions[0]["action"] == "HOLD"
        assert "no current price" in summary["flips_rejected"][0]["reason"]

    def test_event_trigger_citation_accepted_unverified(self, guard, open_entry):
        decisions = [
            {"ticker": "005930.KS", "action": "SELL", "cites_trigger_id": "smsn-t3"}
        ]
        summary = guard.enforce(decisions, [open_entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "SELL"
        assert "unverified_event_citation" in summary["flips_allowed"][0]["basis"]

    def test_date_trigger_fires_only_after_bound(self, guard, open_entry):
        decisions = [
            {"ticker": "005930.KS", "action": "SELL", "cites_trigger_id": "smsn-t2"}
        ]
        # Bound is 2026-09-30; today is 2026-07-03 → not fired.
        summary = guard.enforce(decisions, [open_entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "HOLD"
        assert "NOT fired" in summary["flips_rejected"][0]["reason"]

        late_guard = ContinuityGuard(
            now_fn=lambda: datetime(2026, 10, 1, 19, 0, tzinfo=timezone.utc)
        )
        # A position opened 2026-10-01 with the same triggers, flipped same day.
        entry = dict(open_entry, entry_ts="2026-10-01T12:45:00+00:00")
        decisions = [
            {"ticker": "005930.KS", "action": "SELL", "cites_trigger_id": "smsn-t2"}
        ]
        summary = late_guard.enforce(decisions, [entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "SELL"

    def test_prior_day_position_flips_freely(self, guard, open_entry):
        entry = dict(open_entry, entry_ts=PRIOR_DAY_TS)
        decisions = decisions_from_fixture("flip_without_citation.json")
        summary = guard.enforce(decisions, [entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "SELL"
        assert summary["flips_rejected"] == []

    def test_buy_and_reduce_are_not_flips(self, guard, open_entry):
        decisions = [{"ticker": "005930.KS", "action": "BUY"}]
        summary = guard.enforce(decisions, [open_entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "BUY"
        assert summary["flips_rejected"] == []


# ============================================================================
# NEW-INFORMATION CITATIONS
# ============================================================================


class TestNewInformationCitations:
    def test_new_info_accepted_when_absent_from_prior_payload(self, guard, open_entry):
        decisions = decisions_from_fixture("flip_with_new_info.json")
        prior_payload = json.dumps({"005930.KS": {"fundamentals": "old morning data"}})
        summary = guard.enforce(
            decisions, [open_entry], {"005930.KS": 70500.0}, lambda run_id: prior_payload
        )
        assert decisions[0]["action"] == "SELL"
        assert "absent from prior payload" in summary["flips_allowed"][0]["basis"]

    def test_new_info_rejected_when_verbatim_in_prior_payload(self, guard, open_entry):
        decisions = decisions_from_fixture("flip_with_new_info.json")
        cited = decisions[0]["new_information"]
        # The prior payload already contained the exact text (case/whitespace vary).
        prior_payload = "MORNING PAYLOAD ...  " + cited.upper() + "  ... more data"
        summary = guard.enforce(
            decisions, [open_entry], {"005930.KS": 70500.0}, lambda run_id: prior_payload
        )
        assert decisions[0]["action"] == "HOLD"
        assert "verbatim" in summary["flips_rejected"][0]["reason"]

    def test_new_info_accepted_unverifiable_without_prior_payload(self, guard, open_entry):
        decisions = decisions_from_fixture("flip_with_new_info.json")
        summary = guard.enforce(
            decisions, [open_entry], {"005930.KS": 70500.0}, lambda run_id: None
        )
        assert decisions[0]["action"] == "SELL"
        assert "unverifiable" in summary["flips_allowed"][0]["basis"]

    def test_thin_new_info_rejected(self, guard, open_entry):
        decisions = [
            {"ticker": "005930.KS", "action": "SELL", "new_information": "vibes"}
        ]
        summary = guard.enforce(decisions, [open_entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "HOLD"
        assert "too thin" in summary["flips_rejected"][0]["reason"]
        assert str(MIN_NEW_INFO_CHARS) in summary["flips_rejected"][0]["reason"]

    def test_loader_exception_degrades_to_unverifiable(self, guard, open_entry):
        def exploding_loader(run_id):
            raise OSError("checkpoint corrupted")

        decisions = decisions_from_fixture("flip_with_new_info.json")
        summary = guard.enforce(
            decisions, [open_entry], {"005930.KS": 70500.0}, exploding_loader
        )
        assert decisions[0]["action"] == "SELL"
        assert "unverifiable" in summary["flips_allowed"][0]["basis"]


# ============================================================================
# EDGE CASES
# ============================================================================


class TestGuardEdgeCases:
    def test_unparseable_entry_ts_skips_flip_rule(self, guard, open_entry):
        """An entry with a broken timestamp cannot be proven same-day → free flip"""
        entry = dict(open_entry, entry_ts="not-a-timestamp")
        decisions = decisions_from_fixture("flip_without_citation.json")
        summary = guard.enforce(decisions, [entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "SELL"
        assert summary["flips_rejected"] == []

    def test_date_only_entry_ts_parses(self, guard, open_entry):
        entry = dict(open_entry, entry_ts="2026-07-03")
        decisions = decisions_from_fixture("flip_without_citation.json")
        summary = guard.enforce(decisions, [entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "HOLD"
        assert summary["flips_rejected"]

    def test_summary_lists_open_positions(self, guard, open_entry):
        summary = guard.enforce([], [open_entry])
        assert summary["open_positions"] == ["005930.KS"]

    def test_default_clock_is_utc_today(self, open_entry):
        """Without an injected clock the guard uses the real UTC date"""
        real_guard = ContinuityGuard()
        entry = dict(
            open_entry,
            entry_ts=datetime.now(timezone.utc).isoformat(),
        )
        decisions = [{"ticker": "005930.KS", "action": "SELL"}]
        summary = real_guard.enforce(decisions, [entry], {"005930.KS": 70500.0})
        assert decisions[0]["action"] == "HOLD"
        assert summary["flips_rejected"]
        assert date.today() is not None  # sanity: no frozen clock in this test
