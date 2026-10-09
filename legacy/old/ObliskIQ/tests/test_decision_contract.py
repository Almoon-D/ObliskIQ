"""
Tests for the Phase-4 Decision Contract (v6.12.0)

Covers:
- Strict versioned JSON schema validation (valid / malformed / violations)
- The dict adapter's scale bridges (conviction 0-1 -> 0-10, size dual keys)
- GeminiAnalyst retry-once-then-ModelError protocol (fixture-simulated)
- Quarter-Kelly sizing clamp vs hand-computed Kelly cases
- Scoreboard logging of AI-stated win_probability/payoff_ratio (Brier inputs)
- Thesis-ledger CRUD on the DatabaseManager
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modules.execution.phase4_decision_engine import Phase4DecisionEngine, PositionLimits
from modules.models.schemas import (
    PHASE4_SCHEMA_VERSION,
    contract_decision_to_dict,
    parse_phase4_response,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "phase4_responses"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ============================================================================
# SCHEMA VALIDATION
# ============================================================================


class TestContractSchema:
    """Strict versioned JSON action schema"""

    def test_valid_response_parses(self):
        response = parse_phase4_response(load_fixture("valid_response.json"))
        assert response.schema_version == PHASE4_SCHEMA_VERSION
        assert len(response.portfolio_decisions) == 3
        buy = response.portfolio_decisions[0]
        assert buy.ticker == "005930.KS"  # global symbol allowed
        assert buy.action.value == "BUY"
        assert len(buy.invalidation_triggers) == 3
        assert buy.invalidation_triggers[1].trigger_date is not None

    def test_prose_envelope_keys_pass_through(self):
        response = parse_phase4_response(load_fixture("valid_response.json"))
        dumped = response.model_dump()
        assert "hedging_strategy" in dumped
        assert "monitoring_checklist" in dumped

    def test_malformed_prose_rejected(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_phase4_response(load_fixture("malformed_not_json.txt"))

    def test_schema_violations_rejected_with_compact_errors(self):
        with pytest.raises(ValueError) as exc_info:
            parse_phase4_response(load_fixture("schema_violation.json"))
        message = str(exc_info.value)
        # conviction 8 > 1 and size_pct 45 > 20 both surface
        assert "conviction" in message
        assert "size_pct" in message

    def test_wrong_schema_version_rejected(self):
        payload = json.loads(load_fixture("valid_response.json"))
        payload["schema_version"] = "0.9"
        with pytest.raises(ValueError, match="schema_version"):
            parse_phase4_response(json.dumps(payload))

    def test_buy_requires_thesis_triggers_and_kelly_inputs(self):
        payload = {
            "schema_version": "1.0",
            "portfolio_decisions": [
                {"ticker": "AAPL", "action": "BUY", "conviction": 0.8, "size_pct": 5.0}
            ],
        }
        with pytest.raises(ValueError) as exc_info:
            parse_phase4_response(json.dumps(payload))
        message = str(exc_info.value)
        assert "thesis" in message and "win_probability" in message

    def test_price_trigger_requires_threshold(self):
        payload = {
            "schema_version": "1.0",
            "portfolio_decisions": [
                {
                    "ticker": "AAPL",
                    "action": "BUY",
                    "conviction": 0.8,
                    "size_pct": 5.0,
                    "thesis": "A perfectly reasonable one-paragraph thesis.",
                    "win_probability": 0.6,
                    "payoff_ratio": 2.0,
                    "invalidation_triggers": [
                        {"trigger_id": "t1", "type": "price_below", "description": "support"}
                    ],
                }
            ],
        }
        with pytest.raises(ValueError, match="threshold"):
            parse_phase4_response(json.dumps(payload))

    def test_hold_needs_only_core_fields(self):
        payload = {
            "schema_version": "1.0",
            "portfolio_decisions": [
                {"ticker": "asml.as", "action": "HOLD", "conviction": 0.5}
            ],
        }
        response = parse_phase4_response(json.dumps(payload))
        assert response.portfolio_decisions[0].ticker == "ASML.AS"  # normalized

    def test_empty_decisions_rejected(self):
        payload = {"schema_version": "1.0", "portfolio_decisions": []}
        with pytest.raises(ValueError, match="at least 1"):
            parse_phase4_response(json.dumps(payload))


class TestContractAdapter:
    """contract_decision_to_dict bridges to internal conventions"""

    def test_scale_bridges(self):
        response = parse_phase4_response(load_fixture("valid_response.json"))
        decision = contract_decision_to_dict(response.portfolio_decisions[0])
        assert decision["conviction"] == 8.0          # 0.8 -> internal 0-10
        assert decision["conviction_contract"] == 0.8  # raw contract value kept
        assert decision["position_size_pct"] == 12.0   # percent form
        assert decision["position_size"] == 0.12       # fraction form
        assert decision["stop_loss"] == 61000          # TradeSignal-compatible key
        assert decision["win_probability"] == 0.6
        assert decision["payoff_ratio"] == 2.0

    def test_thesis_backfills_reasoning(self):
        payload = {
            "schema_version": "1.0",
            "portfolio_decisions": [
                {
                    "ticker": "AAPL",
                    "action": "SELL",
                    "conviction": 0.7,
                    "size_pct": 0.0,
                    "thesis": "Exit thesis paragraph used as the log line.",
                    "win_probability": 0.55,
                    "payoff_ratio": 1.5,
                }
            ],
        }
        response = parse_phase4_response(json.dumps(payload))
        decision = contract_decision_to_dict(response.portfolio_decisions[0])
        assert decision["reasoning"] == "Exit thesis paragraph used as the log line."


# ============================================================================
# RETRY PROTOCOL (LLMAnalyst — provider-independent)
# ============================================================================


class TestRetryProtocol:
    """Malformed responses: rejected, retried once with the error appended,
    then logged as a failed run (ModelError). Provider-independent: the same
    protocol protects every model in the failover chain."""

    @staticmethod
    def _make_analyst(responses):
        from llm_fakes import ScriptedProvider
        from modules.analysis.llm_analyst import LLMAnalyst

        provider = ScriptedProvider(responses)
        composer = MagicMock()
        composer.compose_phase4_prompt.return_value = "PROMPT"
        analyst = LLMAnalyst(provider, use_v33_prompts=True, prompt_composer=composer)
        return analyst, provider

    def test_malformed_then_valid_succeeds_on_retry(self):
        analyst, provider = self._make_analyst(
            [load_fixture("malformed_not_json.txt"), load_fixture("valid_response.json")]
        )
        result = analyst.analyze_ticker_details({"005930.KS": {"price": 70000}})
        assert provider.calls == 2
        # The retry prompt carries the rejection notice + validation error.
        assert "PREVIOUS RESPONSE REJECTED" in provider.prompts[1]
        assert result["portfolio_decisions"][0]["ticker"] == "005930.KS"

    def test_malformed_twice_raises_model_error(self):
        from modules.exceptions import ModelError

        analyst, provider = self._make_analyst(
            [load_fixture("malformed_not_json.txt"), load_fixture("schema_violation.json")]
        )
        with pytest.raises(ModelError, match="decision-contract"):
            analyst.analyze_ticker_details({"AAPL": {"price": 180}})
        assert provider.calls == 2

    def test_valid_first_try_makes_single_call(self):
        analyst, provider = self._make_analyst([load_fixture("valid_response.json")])
        result = analyst.analyze_ticker_details({"005930.KS": {"price": 70000}})
        assert provider.calls == 1
        assert len(result["portfolio_decisions"]) == 3


# ============================================================================
# QUARTER-KELLY SIZING CLAMP
# ============================================================================


class TestQuarterKellyClamp:
    """Deterministic post-processor vs hand-computed Kelly cases"""

    def setup_method(self):
        self.engine = Phase4DecisionEngine(total_capital=100_000.0)

    @staticmethod
    def _buy(ticker, size_pct, p, b):
        return {
            "ticker": ticker,
            "action": "BUY",
            "position_size_pct": size_pct,
            "win_probability": p,
            "payoff_ratio": b,
        }

    def test_oversized_buy_clamped_to_quarter_kelly(self):
        # Hand-computed: f* = (b·p − q)/b = (2·0.6 − 0.4)/2 = 0.40
        # quarter-Kelly = 0.10 → a 12% request clamps to 10%.
        decisions = [self._buy("AAPL", 12.0, 0.6, 2.0)]
        summary = self.engine.apply_quarter_kelly_clamp(decisions, regime="normal")
        assert decisions[0]["position_size_pct"] == pytest.approx(10.0)
        assert decisions[0]["position_size"] == pytest.approx(0.10)
        assert decisions[0]["kelly_full"] == pytest.approx(0.40)
        assert decisions[0]["size_pct_before_clamp"] == 12.0
        assert summary["clamped"][0]["ticker"] == "AAPL"

    def test_request_below_clamp_untouched(self):
        # f* = (1.5·0.55 − 0.45)/1.5 = 0.25 → quarter = 0.0625 (6.25%).
        # A 5% request sits below the clamp and passes through.
        decisions = [self._buy("MSFT", 5.0, 0.55, 1.5)]
        summary = self.engine.apply_quarter_kelly_clamp(decisions, regime="normal")
        assert decisions[0]["position_size_pct"] == 5.0
        assert decisions[0]["kelly_quarter_pct"] == pytest.approx(6.25)
        assert summary["clamped"] == []

    def test_negative_edge_rewritten_to_hold(self):
        # f* = (1·0.4 − 0.6)/1 = −0.20 → stated edge is negative → HOLD.
        decisions = [self._buy("XOM", 5.0, 0.4, 1.0)]
        summary = self.engine.apply_quarter_kelly_clamp(decisions, regime="normal")
        assert decisions[0]["action"] == "HOLD"
        assert decisions[0]["position_size_pct"] == 0.0
        assert summary["negative_edge_holds"] == ["XOM"]

    def test_reflexivity_regime_cap_applies(self):
        # f* = (3·0.9 − 0.1)/3 = 0.8667 → quarter = 0.2167, capped at the
        # reflexivity regime limit 0.20 → a 25% request clamps to 20%.
        decisions = [self._buy("NVDA", 25.0, 0.9, 3.0)]
        self.engine.apply_quarter_kelly_clamp(decisions, regime="reflexivity")
        assert decisions[0]["position_size_pct"] == pytest.approx(20.0)

    def test_crisis_regime_cap_applies(self):
        # Same stated edge, crisis regime cap 5%.
        decisions = [self._buy("NVDA", 12.0, 0.9, 3.0)]
        self.engine.apply_quarter_kelly_clamp(decisions, regime="crisis")
        assert decisions[0]["position_size_pct"] == pytest.approx(5.0)

    def test_missing_kelly_inputs_flagged_not_silent(self):
        decisions = [{"ticker": "LEGACY", "action": "BUY", "position_size_pct": 8.0}]
        summary = self.engine.apply_quarter_kelly_clamp(decisions, regime="normal")
        assert summary["missing_inputs"] == ["LEGACY"]
        assert decisions[0]["position_size_pct"] == 8.0  # passes through, flagged

    def test_stated_probability_is_shrunk_toward_one_half_before_kelly(self):
        # stated p=0.6, b=2; shrink 0.5 -> effective p = 0.5 + 0.5*(0.6-0.5) = 0.55
        # f* = (2*0.55 - 0.45)/2 = 0.325 -> quarter-Kelly 8.125% (vs 10% unshrunk)
        decisions = [self._buy("AAPL", 12.0, 0.6, 2.0)]
        summary = self.engine.apply_quarter_kelly_clamp(decisions, regime="normal", probability_shrink=0.5)
        d = decisions[0]
        assert d["win_probability_stated"] == 0.6
        assert d["win_probability_effective"] == pytest.approx(0.55)
        assert d["kelly_full"] == pytest.approx(0.325)
        assert d["position_size_pct"] == pytest.approx(8.125, abs=0.01)  # engine rounds the fraction to 4 dp
        assert summary["probability_shrink"] == 0.5

    def test_an_edge_that_exists_only_through_overconfidence_becomes_a_hold(self):
        # stated p=0.7, b=0.5 -> f* = (0.5*0.7 - 0.3)/0.5 = +0.10 (a thin "edge");
        # shrunk to p=0.6 -> f* = (0.5*0.6 - 0.4)/0.5 = -0.20 -> HOLD
        stated = [self._buy("THIN", 5.0, 0.7, 0.5)]
        self.engine.apply_quarter_kelly_clamp(stated, regime="normal")
        assert stated[0]["action"] == "BUY"
        shrunk = [self._buy("THIN", 5.0, 0.7, 0.5)]
        summary = self.engine.apply_quarter_kelly_clamp(shrunk, regime="normal", probability_shrink=0.5)
        assert shrunk[0]["action"] == "HOLD" and summary["negative_edge_holds"] == ["THIN"]
        assert "after shrinking" in shrunk[0]["clamp_reason"]

    def test_shrink_is_bounded_and_default_changes_nothing(self):
        default = [self._buy("AAPL", 12.0, 0.6, 2.0)]
        self.engine.apply_quarter_kelly_clamp(default, regime="normal")
        assert default[0]["position_size_pct"] == pytest.approx(10.0)
        assert default[0]["win_probability_effective"] == pytest.approx(0.6)
        wild = [self._buy("AAPL", 12.0, 0.6, 2.0)]
        summary = self.engine.apply_quarter_kelly_clamp(wild, regime="normal", probability_shrink=7.0)
        assert summary["probability_shrink"] == 1.0              # clamped to [0, 1]
        none = [self._buy("AAPL", 12.0, 0.6, 2.0)]
        self.engine.apply_quarter_kelly_clamp(none, regime="normal", probability_shrink=0.0)
        assert none[0]["win_probability_effective"] == pytest.approx(0.5)   # full shrink: no stated edge believed

    def test_sell_and_hold_untouched(self):
        decisions = [
            {"ticker": "A", "action": "SELL", "win_probability": 0.6, "payoff_ratio": 2.0},
            {"ticker": "B", "action": "HOLD"},
        ]
        summary = self.engine.apply_quarter_kelly_clamp(decisions, regime="normal")
        assert decisions[0]["action"] == "SELL"
        assert "kelly_full" not in decisions[1]
        assert summary["clamped"] == []

    def test_custom_position_limits_respected(self):
        engine = Phase4DecisionEngine(
            total_capital=100_000.0,
            position_limits=PositionLimits(normal_max=0.08),
        )
        # quarter-Kelly 10% > custom regime cap 8% → recommended_size caps at 8%.
        decisions = [self._buy("AAPL", 12.0, 0.6, 2.0)]
        engine.apply_quarter_kelly_clamp(decisions, regime="normal")
        assert decisions[0]["position_size_pct"] == pytest.approx(8.0)


# ============================================================================
# BRIER-SCOREABLE LOGGING (AI Scoreboard)
# ============================================================================


class TestScoreboardBrierInputs:
    """win_probability/payoff_ratio land in ai_proposals at proposal time"""

    def test_win_probability_logged(self, tmp_path):
        from modules.analytics.ai_scoreboard import AIScoreboard
        from modules.storage.database_manager import DatabaseManager

        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        scoreboard = AIScoreboard(db, prompt_composer=MagicMock(prompt_version_hash=lambda: "h"))
        written = scoreboard.log_phase4_proposals(
            {
                "portfolio_decisions": [
                    {
                        "ticker": "AAPL",
                        "action": "BUY",
                        "conviction": 8.0,
                        "position_size_pct": 10.0,
                        "win_probability": 0.6,
                        "payoff_ratio": 2.0,
                        "reasoning": "test",
                    }
                ]
            },
            regime_call="bull",
            run_id="20260703_124500",
            model_name="gemini-3.5-pro-thinking",
        )
        assert written == 1
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT win_probability, payoff_ratio, model_name FROM ai_proposals"
            ).fetchone()
        assert row["win_probability"] == 0.6
        assert row["payoff_ratio"] == 2.0
        assert row["model_name"] == "gemini-3.5-pro-thinking"
        db.close_pool()

    def test_migration_adds_columns_to_legacy_table(self, tmp_path):
        """An older ai_proposals table without the new columns gets migrated"""
        import sqlite3

        from modules.analytics.ai_scoreboard import AIScoreboard
        from modules.storage.database_manager import DatabaseManager

        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        # Realistic pre-v6.12.0 table shape: everything except the two new
        # Kelly-input columns (the index targets must exist).
        conn.execute(
            "CREATE TABLE ai_proposals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT UNIQUE NOT NULL, "
            "run_id TEXT, phase TEXT NOT NULL, proposal_date TEXT NOT NULL, "
            "ticker TEXT NOT NULL, direction TEXT NOT NULL, action TEXT, "
            "conviction REAL, regime_call TEXT, regime_confidence REAL, "
            "reflexivity_intensity REAL, position_size_pct REAL, model_name TEXT, "
            "prompt_version TEXT, prompt_hash TEXT, source TEXT, system_version TEXT, "
            "screener_selected INTEGER, reasoning TEXT, "
            "ticker_price_at_proposal REAL, spy_price_at_proposal REAL, "
            "realized_regime TEXT, regime_correct INTEGER, "
            "scored_horizons TEXT DEFAULT '[]', last_scored_at TEXT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.commit()
        conn.close()

        db = DatabaseManager(db_path=db_path)
        AIScoreboard(db, prompt_composer=MagicMock(prompt_version_hash=lambda: "h"))
        with db.get_connection() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(ai_proposals)")}
        assert "win_probability" in columns
        assert "payoff_ratio" in columns
        db.close_pool()


# ============================================================================
# THESIS LEDGER (DatabaseManager)
# ============================================================================


class TestThesisLedger:
    """thesis_ledger schema migration + open/get/close round-trip"""

    def test_table_created_on_init(self, tmp_path):
        from modules.storage.database_manager import DatabaseManager

        db = DatabaseManager(db_path=str(tmp_path / "ledger.db"))
        with db.get_connection() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "thesis_ledger" in tables
        db.close_pool()

    def test_open_get_close_round_trip(self, tmp_path):
        from modules.storage.database_manager import DatabaseManager

        db = DatabaseManager(db_path=str(tmp_path / "ledger.db"))
        triggers = [
            {
                "trigger_id": "t1",
                "type": "price_below",
                "threshold": 61000,
                "description": "support break",
            }
        ]
        row_id = db.open_thesis_entry(
            ticker="005930.ks",
            entry_thesis="Memory upcycle underpriced.",
            invalidation_triggers=triggers,
            run_id="20260703_124500",
            model_name="gemini-3.5-pro-thinking",
            entry_price=70000.0,
            size_pct=10.0,
        )
        assert row_id

        open_theses = db.get_open_theses()
        assert len(open_theses) == 1
        entry = open_theses[0]
        assert entry["ticker"] == "005930.KS"  # uppercased
        assert entry["run_id"] == "20260703_124500"
        assert entry["model_name"] == "gemini-3.5-pro-thinking"
        assert entry["invalidation_triggers"] == triggers  # JSON round-trip
        assert entry["status"] == "open"

        closed = db.close_thesis_entry(
            "005930.KS", close_run_id="20260703_190000", close_reason="trigger:t1 fired"
        )
        assert closed == 1
        assert db.get_open_theses() == []

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT status, close_run_id, close_reason FROM thesis_ledger"
            ).fetchone()
        assert row["status"] == "closed"
        assert row["close_run_id"] == "20260703_190000"
        assert row["close_reason"] == "trigger:t1 fired"
        db.close_pool()

    def test_close_without_open_entry_is_noop(self, tmp_path):
        from modules.storage.database_manager import DatabaseManager

        db = DatabaseManager(db_path=str(tmp_path / "ledger.db"))
        assert db.close_thesis_entry("NOPE", close_run_id="r", close_reason="x") == 0
        db.close_pool()
