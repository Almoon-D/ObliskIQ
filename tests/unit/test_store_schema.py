"""The shape of the migrations (``001_core.sql`` through ``012_engines.sql``) and the
constraints that keep a bad row out.

The shape is pinned column by column so that a migration cannot change a table without a test
saying so. Each CHECK has a row it must refuse.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from typing import Any

import pytest

from obliskiq.store import Store

#: ``name:TYPE`` with ``!`` for NOT NULL, ``*`` for part of the primary key, ``=x`` a default.
SHAPE = {
    "bars": (
        "conid:INTEGER!*, session_date:TEXT!*, open:REAL, high:REAL, low:REAL, "
        "close:REAL!, volume:REAL, source:TEXT!"
    ),
    "chain_snapshots": (
        "snapshot_date:TEXT!*, underlying_conid:INTEGER!*, expiry:TEXT!*, "
        "strike:REAL!*, right:TEXT!*, option_conid:INTEGER, option_symbol:TEXT, "
        "option_exchange:TEXT, bid:REAL, ask:REAL, last:REAL, iv:REAL, delta:REAL, "
        "open_interest:INTEGER, volume:INTEGER, sigma_used:REAL, sigma_source:TEXT, "
        "source:TEXT!, collected_at:TEXT!, market_data_status:TEXT, "
        "market_data_raw:TEXT, quote_delay_seconds:INTEGER, multiplier:REAL"
    ),
    "curves": (
        "snapshot_date:TEXT!*, root:TEXT!*, expiry:TEXT!*, price:REAL, "
        "annualised_roll:REAL, conid:INTEGER, exchange:TEXT, currency:TEXT, "
        "source:TEXT!, collected_at:TEXT!, market_data_status:TEXT, "
        "market_data_raw:TEXT, quote_delay_seconds:INTEGER"
    ),
    "instruments": (
        "conid:INTEGER*, symbol:TEXT!, listing_exchange:TEXT!, currency:TEXT!, "
        "asset_class:TEXT!, domicile:TEXT, ucits:INTEGER, kid_status:TEXT, "
        "share_class:TEXT, lot_size:INTEGER, option_exchange:TEXT, "
        "history_exchange:TEXT, roles:TEXT!, superseded_by:INTEGER, created_at:TEXT!, "
        "superseded_at:TEXT"
    ),
    "macro": (
        "series_id:TEXT!*, source:TEXT!*, reference_date:TEXT!*, release_date:TEXT, "
        "value:REAL!, frequency:TEXT, units:TEXT, collected_at:TEXT!"
    ),
    "run_steps": (
        "step_id:INTEGER*, run_id:TEXT, step_index:INTEGER, collector:TEXT!, "
        "started_at:TEXT!, finished_at:TEXT, status:TEXT!, rows_written:INTEGER!=0, "
        "targets_ok:INTEGER!=0, targets_failed:INTEGER!=0, detail:TEXT"
    ),
    "runs": (
        "run_id:TEXT!*, command:TEXT!, session_date:TEXT!, started_at:TEXT!, "
        "finished_at:TEXT, status:TEXT!, steps_planned:INTEGER!, "
        "steps_completed:INTEGER!=0, exit_code:INTEGER, detail:TEXT"
    ),
    "schema_migrations": ("name:TEXT*, checksum:TEXT!, applied_at:TEXT!"),
    # 002_orders
    "account_reads": "read_id:INTEGER*, kind:TEXT!, observed_at:TEXT!",
    "broker_orders": (
        "order_id:TEXT*, coid:TEXT, intent_id:TEXT, conid:INTEGER, side:TEXT, sec_type:TEXT, "
        "order_type:TEXT, status:TEXT!, total_quantity:TEXT, filled_quantity:TEXT, "
        "remaining_quantity:TEXT, average_price:TEXT, tif:TEXT, observed_at:TEXT!"
    ),
    "cash_snap": "read_id:INTEGER!*, currency:TEXT!*, cash:TEXT!, settled_cash:TEXT",
    "clearances": (
        "clearance_id:TEXT*, intent_id:TEXT!, action:TEXT!, order_id:TEXT, conid:INTEGER!, "
        "side:TEXT!, quantity:TEXT, terms_digest:TEXT!, effect:TEXT!, clearance:TEXT!, "
        "issued_at:TEXT!, expires_at:TEXT!, used_at:TEXT"
    ),
    "fills": (
        "execution_id:TEXT*, intent_id:TEXT, coid:TEXT, conid:INTEGER!, side:TEXT!, "
        "quantity:TEXT!, price:TEXT!, commission:TEXT, executed_at:TEXT!, simulated:INTEGER!"
    ),
    "holding_assignments": (
        "assignment_id:INTEGER*, conid:INTEGER!, owner:TEXT!, assigned_by:TEXT!, "
        "assigned_at:TEXT!, reason:TEXT!"
    ),
    "intent_events": (
        "intent_id:TEXT!*, seq:INTEGER!*, at:TEXT!, from_state:TEXT, to_state:TEXT!, detail:TEXT!"
    ),
    "intents": (
        "intent_id:TEXT*, coid:TEXT!, sleeve:TEXT!, thesis_id:TEXT, role:TEXT!, conid:INTEGER!, "
        "side:TEXT!, terms:TEXT!, reason:TEXT!, mode:TEXT!, approval_required:INTEGER!, "
        "created_at:TEXT!, expires_at:TEXT!, parent_id:TEXT, protective_id:TEXT, state:TEXT!, "
        "updated_at:TEXT!, protective_spec:TEXT"
    ),
    "killswitch": "change_id:INTEGER*, at:TEXT!, state:TEXT!, actor:TEXT!, reason:TEXT!",
    "mode_audit": (
        "change_id:INTEGER*, at:TEXT!, from_mode:TEXT!, to_mode:TEXT!, direction:TEXT!, "
        "actor:TEXT!, reason:TEXT!"
    ),
    "positions_snap": (
        "read_id:INTEGER!*, conid:INTEGER!*, quantity:TEXT!, average_cost:TEXT, currency:TEXT"
    ),
    # 003_pretrade
    "approvals": (
        "approval_id:INTEGER*, intent_id:TEXT!, decision:TEXT!, actor:TEXT!, reason:TEXT!, at:TEXT!"
    ),
    "stop_resizes": (
        "resize_id:INTEGER*, exit_intent_id:TEXT!, stop_intent_id:TEXT!, stop_order_id:TEXT!, "
        "original_terms:TEXT!, resized_quantity:TEXT!, state:TEXT!, created_at:TEXT!, "
        "updated_at:TEXT!, detail:TEXT!"
    ),
    # 007_service
    "nav_history": "as_of:TEXT!*, nav_base:TEXT!, currency:TEXT!, source:TEXT!, recorded_at:TEXT!",
    # 006_execution
    "reconciliations": (
        "run_id:INTEGER*, at:TEXT!, status:TEXT!, findings:TEXT!, positions_read:INTEGER, "
        "cash_read:INTEGER, anchor:INTEGER!"
    ),
    # 004_research_tax
    "research_series": (
        "series_id:TEXT!*, dataset_id:TEXT!, vendor:TEXT!, field:TEXT!, units:TEXT!, "
        "currency:TEXT, frequency:TEXT!, point_in_time:TEXT!, source_url:TEXT!, "
        "downloaded_at:TEXT!, content_sha256:TEXT!, bias_notes:TEXT!"
    ),
    "research_history": ("series_id:TEXT!*, observed_on:TEXT!*, available_on:TEXT!*, value:REAL!"),
    "research_trials": (
        "trial_id:TEXT!*, study_id:TEXT!, stage:TEXT!, recorded_at:TEXT!, spec_sha256:TEXT!, "
        "inputs_sha256:TEXT!, parameters:TEXT!, metric:TEXT!, estimate:REAL, sharpe:REAL, "
        "observations:INTEGER, outcome:TEXT"
    ),
    "tax_lots": (
        "lot_id:TEXT!*, security:TEXT!, acquired_on:TEXT!, quantity:TEXT!, cost:TEXT!, "
        "deferred_loss:TEXT!, replacement_units:TEXT!, saved_at:TEXT!"
    ),
    "snapshots": (
        "snapshot_date:TEXT!*, conid:INTEGER!*, last:REAL, bid:REAL, ask:REAL, "
        "volume:INTEGER, implied_vol:REAL, implied_vol_source:TEXT, "
        "historical_vol_30d:REAL, dividend_yield:REAL, dividend_yield_basis:TEXT, "
        "implied_vol_solved:REAL, implied_vol_solved_source:TEXT, source:TEXT!, "
        "collected_at:TEXT!, market_data_status:TEXT, market_data_raw:TEXT, "
        "quote_delay_seconds:INTEGER"
    ),
    # 008_capital
    "withdrawal_reservations": (
        "reservation_id:INTEGER*, amount:TEXT!, currency:TEXT!, due_date:TEXT!, reason:TEXT!, "
        "state:TEXT!, created_at:TEXT!, created_by:TEXT!, updated_at:TEXT!"
    ),
    "reservation_events": (
        "reservation_id:INTEGER!*, seq:INTEGER!*, at:TEXT!, actor:TEXT!, detail:TEXT!"
    ),
    "cash_events": (
        "event_id:INTEGER*, currency:TEXT!, amount:TEXT!, detected_at:TEXT!, updated_at:TEXT!, "
        "state:TEXT!, classified_at:TEXT, classified_by:TEXT"
    ),
    "cash_flows": (
        "flow_id:INTEGER*, kind:TEXT!, category:TEXT, currency:TEXT!, amount:TEXT!, "
        "on_date:TEXT!, recorded_at:TEXT!, recorded_by:TEXT!, matched_at:TEXT, "
        "cancelled_at:TEXT, in_book:INTEGER!, reservation_id:INTEGER, event_id:INTEGER, note:TEXT"
    ),
    "owner_questions": (
        "question_id:INTEGER*, kind:TEXT!, reservation_id:INTEGER!, context:TEXT!, "
        "options:TEXT!, default_option:TEXT!, deadline:TEXT!, state:TEXT!, answer:TEXT, "
        "answered_at:TEXT, answered_by:TEXT, created_at:TEXT!"
    ),
    "question_events": "question_id:INTEGER!*, seq:INTEGER!*, at:TEXT!, actor:TEXT!, detail:TEXT!",
    "external_positions": (
        "holding_id:INTEGER*, broker:TEXT!, symbol:TEXT!, isin:TEXT, quantity:TEXT!, "
        "currency:TEXT!, market_value_base:TEXT!, last_updated:TEXT!, notes:TEXT, sector:TEXT, "
        "country:TEXT, recorded_at:TEXT!"
    ),
    "tax_loss_windows": (
        "position:INTEGER*, security:TEXT!, sold_on:TEXT!, closes_on:TEXT!, "
        "loss_per_unit:TEXT!, units:TEXT!"
    ),
    "tax_ledger_years": (
        "tax_year:INTEGER*, income:TEXT!, gains:TEXT!, withheld:TEXT!, foreign_income:TEXT!"
    ),
    "tax_carry": "position:INTEGER*, component:TEXT!, arising_year:INTEGER!, amount:TEXT!",
    "tax_settled": "tax_year:INTEGER*, summary:TEXT!",
    "tax_engine": "singleton:INTEGER*, lot_sequence:INTEGER!, saved_at:TEXT!",
    "tax_fills": "execution_id:TEXT!*, outcome:TEXT!, detail:TEXT!, taken_at:TEXT!",
    "tax_realisations": (
        "realisation_id:INTEGER*, execution_id:TEXT!, security:TEXT!, lot_id:TEXT!, "
        "acquired_on:TEXT!, disposed_on:TEXT!, quantity:TEXT!, proceeds:TEXT!, cost:TEXT!, "
        "released:TEXT!, deferred:TEXT!"
    ),
    "fx_rates": (
        "currency:TEXT!*, on_date:TEXT!*, rate_to_base:TEXT!, source:TEXT!, observed_at:TEXT!"
    ),
    # 009_universe
    "universe_rejections": (
        "rejected_at:TEXT!*, symbol:TEXT!*, origin:TEXT!*, listing_exchange:TEXT, "
        "conid:INTEGER, description:TEXT, reason:TEXT!"
    ),
    "holdings_files": (
        "fund_symbol:TEXT!*, as_of:TEXT!*, issuer:TEXT!, url:TEXT!, downloaded_at:TEXT!, "
        "content_sha256:TEXT!, rows:INTEGER!"
    ),
    "holdings": (
        "fund_symbol:TEXT!*, as_of:TEXT!*, line:INTEGER!*, ticker:TEXT!, name:TEXT!, "
        "asset_class:TEXT!, issuer_sector:TEXT, sector:TEXT, location:TEXT, country:TEXT, "
        "exchange:TEXT, ibkr_exchange:TEXT, currency:TEXT, weight:REAL, candidate_key:TEXT"
    ),
    "scanner_rows": (
        "scanned_on:TEXT!*, location:TEXT!*, scan_type:TEXT!*, rank:INTEGER!*, market:TEXT!, "
        "conid:INTEGER!, symbol:TEXT, listing_exchange:TEXT, company_name:TEXT, "
        "candidate_key:TEXT!, scanned_at:TEXT!"
    ),
    "candidate_resolutions": (
        "candidate_key:TEXT!*, resolved_at:TEXT!, conid:INTEGER, symbol:TEXT, "
        "listing_exchange:TEXT, currency:TEXT, pinned:INTEGER!, pinned_for:TEXT, failure:TEXT"
    ),
    # 010_sources
    "cot_reports": (
        "contract_code:TEXT!*, report_date:TEXT!*, release_date:TEXT!, market_name:TEXT!, "
        "open_interest:INTEGER!, noncomm_long:INTEGER, noncomm_short:INTEGER, "
        "comm_long:INTEGER, comm_short:INTEGER, nonrept_long:INTEGER, nonrept_short:INTEGER, "
        "source:TEXT!, collected_at:TEXT!"
    ),
    "insider_filings": (
        "accession:TEXT!*, form:TEXT!, issuer_cik:INTEGER!, issuer_symbol:TEXT, filed_on:TEXT!, "
        "accepted_at:TEXT!, period_of_report:TEXT, collected_at:TEXT!"
    ),
    "insider_owners": (
        "accession:TEXT!*, owner_cik:INTEGER!*, owner_name:TEXT!, is_director:INTEGER!, "
        "is_officer:INTEGER!, is_ten_percent:INTEGER!, is_other:INTEGER!, officer_title:TEXT"
    ),
    "insider_transactions": (
        "accession:TEXT!*, line:INTEGER!*, derivative:INTEGER!, security_title:TEXT!, "
        "transaction_date:TEXT!, code:TEXT!, shares:REAL, price:REAL, acquired:INTEGER, "
        "shares_after:REAL, direct:INTEGER"
    ),
    "event_markets": (
        "venue:TEXT!*, market_id:TEXT!*, event_id:TEXT, question:TEXT!, outcome:TEXT!, "
        "closes_at:TEXT, curated:INTEGER!, tags:TEXT!, first_seen_at:TEXT!, last_seen_at:TEXT!"
    ),
    "event_snapshots": (
        "venue:TEXT!*, market_id:TEXT!*, observed_at:TEXT!*, probability:REAL!, bid:REAL, "
        "ask:REAL, last_price:REAL, volume:REAL, liquidity:REAL"
    ),
    "gdelt_days": (
        "query_id:TEXT!*, day:TEXT!*, articles:INTEGER!, monitored:INTEGER!, tone:REAL, "
        "collected_at:TEXT!"
    ),
    "news_items": (
        "item_id:TEXT!*, feed:TEXT!, kind:TEXT!, region:TEXT, symbol:TEXT, guid:TEXT, "
        "link:TEXT, title:TEXT!, summary:TEXT, published_at:TEXT, collected_at:TEXT!, "
        "raw:TEXT!, fallback_tone:REAL"
    ),
    "event_extractions": (
        "extraction_id:INTEGER*, item_id:TEXT!, produced_at:TEXT!, provider:TEXT!, "
        "model_id:TEXT!, prompt_version:INTEGER!, prompt_hash:TEXT!, status:TEXT!, "
        "attempts:INTEGER!, output:TEXT, error:TEXT, input_tokens:INTEGER!, "
        "output_tokens:INTEGER!, cost_usd:REAL!"
    ),
    "earnings_dates": (
        "symbol:TEXT!*, collected_on:TEXT!*, report_at:TEXT!*, source:TEXT!, grade:TEXT!"
    ),
    "halt_feed_state": "origin:TEXT!*, fetched_at:TEXT!, incidents:TEXT!",
    "source_health": (
        "source:TEXT!*, last_attempt_at:TEXT!, last_success_at:TEXT, last_status:TEXT!, "
        "last_rows:INTEGER!, consecutive_failures:INTEGER!, detail:TEXT"
    ),
    "source_targets": (
        "source:TEXT!*, target:TEXT!*, last_attempt_at:TEXT!, last_success_at:TEXT, "
        "last_ok:INTEGER!, last_rows:INTEGER!, detail:TEXT"
    ),
    # 011_measurements, rebuilt by 012_engines
    "measurements": (
        "entity_kind:TEXT!*, entity_id:TEXT!*, indicator:TEXT!, leaf:TEXT!*, as_of:TEXT!*, "
        "value:REAL, status:TEXT!, reason:TEXT, reason_code:TEXT, n_obs:INTEGER, window:TEXT, "
        "units:TEXT, source:TEXT, market_data_status:TEXT, inputs_hash:TEXT!, computed_at:TEXT!"
    ),
    # 012_engines
    "option_positions": (
        "position_id:INTEGER*, underlying_conid:INTEGER!, option_conid:INTEGER, expiry:TEXT!, "
        "strike:REAL!, right:TEXT!, contracts:INTEGER!, multiplier:REAL!, "
        "premium_per_unit:TEXT!, currency:TEXT!, premium_base:TEXT!, fx_to_base:TEXT, "
        "opened_on:TEXT!, close_by:TEXT!, target:REAL, prob_target_at_entry:REAL, "
        "catalyst_date:TEXT, intent_id:TEXT, closed_on:TEXT, exit_reason:TEXT"
    ),
    "option_ledger": (
        "event_id:INTEGER*, event_on:TEXT!, event_type:TEXT!, position_id:INTEGER, "
        "conid:INTEGER, amount:TEXT!, penalty_added:TEXT!, penalty_balance_after:TEXT!, "
        "exit_reason:TEXT, quote_currency:TEXT, quote_amount:TEXT, fx_to_base:TEXT, note:TEXT!, "
        "recorded_at:TEXT!"
    ),
    "zombie_observations": (
        "position_id:INTEGER!*, observed_on:TEXT!*, all_conditions_met:INTEGER!, delta:REAL, "
        "dte:INTEGER, prob_target:REAL, prob_ratio_vs_entry:REAL, "
        "recoverable_pct_of_premium:REAL, detail:TEXT!, recorded_at:TEXT!"
    ),
    "option_shadow_entries": (
        "as_of:TEXT!*, underlying_conid:INTEGER!*, expiry:TEXT!*, strike:REAL!*, right:TEXT!*, "
        "snapshot_date:TEXT!, option_conid:INTEGER, bid:REAL, ask:REAL, mid:REAL!, iv:REAL, "
        "delta:REAL, multiplier:REAL, currency:TEXT!, premium_base:TEXT, iv_percentile:REAL, "
        "market_data_status:TEXT, untested:TEXT!, activation:TEXT!, computed_at:TEXT!"
    ),
}

INDEXES = {
    "ix_chain_expiry",
    "ix_chain_underlying_date",
    "ix_curves_root_date",
    "ix_instruments_superseded_by",
    "ix_instruments_symbol",
    "ix_macro_release",
    "ix_run_steps_collector",
    "ix_runs_session",
    "ix_snapshots_conid_date",
    "ux_run_steps_position",
    # 002_orders
    "ix_account_reads_kind_time",
    "ix_broker_orders_coid",
    "ix_broker_orders_intent",
    "ix_clearances_intent",
    "ix_clearances_open",
    "ix_fills_conid_time",
    "ix_fills_intent",
    "ix_holding_assignments_conid",
    "ix_intents_conid_state",
    "ix_intents_state",
    # 003_pretrade
    "ix_approvals_intent",
    "ix_clearances_issued",
    "ix_stop_resizes_state",
    # 006_execution
    "ix_reconciliations_anchor",
    # 004_research_tax
    "ix_research_series_dataset",
    "ix_research_history_available",
    "ix_research_trials_study",
    "ix_tax_lots_security",
    # 008_capital
    "ix_withdrawal_reservations_state",
    "ux_cash_events_pending",
    "ix_cash_flows_on",
    "ux_owner_questions_open",
    "ux_external_positions_key",
    "ix_external_positions_updated",
    "ix_tax_realisations_disposed",
    # 009_universe
    "ix_holdings_candidate",
    # 010_sources
    "ix_cot_release",
    "ix_insider_filings_issuer",
    "ix_news_collected",
    "ix_extractions_item",
    "ix_extractions_produced",
    # 011_measurements, rebuilt by 012_engines
    "ix_measurements_session",
    # 012_engines
    "ix_option_positions_open",
    "ix_option_ledger_position",
}

STAMP = "2026-10-09T22:30:00Z"

ROWS: dict[str, dict[str, Any]] = {
    "instruments": {
        "conid": 756733,
        "symbol": "SPY",
        "listing_exchange": "ARCA",
        "currency": "USD",
        "asset_class": "etf",
        "roles": '["data"]',
        "created_at": STAMP,
    },
    "bars": {
        "conid": 756733,
        "session_date": "2026-10-08",
        "close": 100.5,
        "source": "cp_api",
    },
    "snapshots": {
        "snapshot_date": "2026-10-08",
        "conid": 756733,
        "source": "cp_api",
        "collected_at": STAMP,
    },
    "chain_snapshots": {
        "snapshot_date": "2026-10-08",
        "underlying_conid": 756733,
        "expiry": "2027-01-15",
        "strike": 100.0,
        "right": "C",
        "source": "cp_api",
        "collected_at": STAMP,
    },
    "curves": {
        "snapshot_date": "2026-10-08",
        "root": "CL",
        "expiry": "2026-12-21",
        "source": "cp_api",
        "collected_at": STAMP,
    },
    "macro": {
        "series_id": "CPI",
        "source": "fred",
        "reference_date": "2026-07-01",
        "release_date": "2026-08-12",
        "value": 1.5,
        "collected_at": STAMP,
    },
    "runs": {
        "run_id": "run-1",
        "command": "daily",
        "session_date": "2026-10-08",
        "started_at": STAMP,
        "status": "running",
        "steps_planned": 3,
    },
    "run_steps": {
        "collector": "bars",
        "started_at": STAMP,
        "status": "running",
    },
    "measurements": {
        "entity_kind": "equity_cell",
        "entity_id": "us/broad",
        "indicator": "returns",
        "leaf": "return_5d_local",
        "as_of": "2026-10-08",
        "value": 0.01,
        "status": "ok",
        "inputs_hash": "a" * 64,
        "computed_at": STAMP,
    },
    "option_positions": {
        "underlying_conid": 756733,
        "expiry": "2027-01-15",
        "strike": 100.0,
        "right": "C",
        "contracts": 1,
        "multiplier": 100.0,
        "premium_per_unit": "2.50",
        "currency": "USD",
        "premium_base": "214.00",
        "fx_to_base": "0.856",
        "opened_on": "2026-10-08",
        "close_by": "2026-12-31",
    },
    "option_ledger": {
        "event_on": "2026-10-08",
        "event_type": "premium_committed",
        "position_id": 1,
        "amount": "214.00",
        "penalty_added": "0",
        "penalty_balance_after": "0",
        "note": "committed",
        "recorded_at": STAMP,
    },
    "zombie_observations": {
        "position_id": 1,
        "observed_on": "2026-10-09",
        "all_conditions_met": 0,
        "detail": "{}",
        "recorded_at": STAMP,
    },
    "option_shadow_entries": {
        "as_of": "2026-10-09",
        "underlying_conid": 756733,
        "expiry": "2027-01-15",
        "strike": 100.0,
        "right": "C",
        "snapshot_date": "2026-10-08",
        "mid": 2.5,
        "currency": "USD",
        "untested": "[]",
        "activation": "shadow: evidence fails",
        "computed_at": STAMP,
    },
}


def put(store: Store, table: str, **overrides: Any) -> None:
    fields = {**ROWS[table], **overrides}
    columns = ", ".join(f'"{name}"' for name in fields)
    marks = ", ".join("?" for _ in fields)
    store.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(fields.values()))


@pytest.fixture
def seeded(store: Store) -> Store:
    put(store, "instruments")
    return store


def refuses(store: Store, table: str, **overrides: Any) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        put(store, table, **overrides)


# ── shape ──


def test_every_table_has_exactly_the_pinned_columns(store: Store) -> None:
    found: dict[str, str] = {}
    tables = [t for t in store.table_names() if not t.startswith("sqlite_")]
    for table in tables:
        parts = []
        for _, name, kind, notnull, default, pk in store.execute(f"PRAGMA table_info({table})"):
            flags = (
                ("!" if notnull else "")
                + ("*" if pk else "")
                + (f"={default}" if default is not None else "")
            )
            parts.append(f"{name}:{kind}{flags}")
        found[table] = ", ".join(parts)
    assert found == SHAPE


def test_the_indexes_are_the_pinned_ones(store: Store) -> None:
    rows = store.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
    )
    assert {row[0] for row in rows} == INDEXES


def test_the_market_tables_have_no_frequency_column(store: Store) -> None:
    """Daily bars only: a weekly bar would carry a period-start date and leak the days after."""
    columns = {row[1] for row in store.execute("PRAGMA table_info(bars)")}
    assert "freq" not in columns
    assert "ts" not in columns


def test_a_valid_row_goes_into_every_table(seeded: Store) -> None:
    for table in ("bars", "snapshots", "chain_snapshots", "curves", "macro", "runs"):
        put(seeded, table)
    put(seeded, "run_steps")
    assert seeded.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] == 1


# ── instruments ──


@pytest.mark.parametrize(
    "overrides",
    [
        {"conid": 0},
        {"symbol": "  "},
        {"listing_exchange": ""},
        {"currency": "usd"},
        {"currency": "US"},
        {"asset_class": ""},
        {"domicile": "usa"},
        {"domicile": "u"},
        {"ucits": 2},
        {"lot_size": 0},
        {"roles": "[]"},
        {"roles": "not json"},
        {"roles": '{"data": true}'},
        {"roles": '"data"'},
        {"superseded_by": 756733, "superseded_at": STAMP},
        {"superseded_by": 1},  # replacement named but no time
        {"superseded_at": STAMP},  # time named but no replacement
        {"created_at": "2026-10-09T22:30:00.123Z"},
        {"created_at": "2026-10-09 22:30:00"},
        {"created_at": "2026-10-09T22:30:00+02:00"},
        {"created_at": "2026-10-09"},
    ],
)
def test_a_bad_instrument_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "instruments", **overrides)


def test_a_replacement_must_exist_and_cannot_predate_the_original(seeded: Store) -> None:
    refuses(seeded, "instruments", conid=2, superseded_by=999, superseded_at=STAMP)
    put(seeded, "instruments", conid=3)
    refuses(
        seeded,
        "instruments",
        conid=4,
        superseded_by=3,
        superseded_at="2026-01-01T00:00:00Z",  # before created_at
    )


def test_an_instrument_cannot_be_deleted_while_something_refers_to_it(seeded: Store) -> None:
    put(seeded, "bars")
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute("DELETE FROM instruments WHERE conid = 756733")
    put(seeded, "instruments", conid=2, superseded_by=756733, superseded_at=STAMP)
    seeded.execute("DELETE FROM bars")
    with pytest.raises(sqlite3.IntegrityError):  # the replacement is referenced by instrument 2
        seeded.execute("DELETE FROM instruments WHERE conid = 756733")


def test_an_instrument_row_is_unique_by_conid(seeded: Store) -> None:
    refuses(seeded, "instruments")


# ── bars ──


@pytest.mark.parametrize(
    "overrides",
    [
        {"close": None},
        {"close": -1.0},
        {"open": -1.0},
        {"high": -0.01},
        {"low": -5.0},
        {"volume": -1.0},
        {"source": ""},
        {"session_date": "2026-10-8"},
        {"session_date": "2026-10-08T00:00:00Z"},
        {"session_date": "20261008"},
        {"conid": 424242},  # no such instrument
    ],
)
def test_a_bad_bar_is_refused(seeded: Store, overrides: dict[str, Any]) -> None:
    refuses(seeded, "bars", **overrides)


def test_bars_are_unique_per_instrument_and_session(seeded: Store) -> None:
    put(seeded, "bars")
    refuses(seeded, "bars")
    put(seeded, "bars", session_date="2026-10-09")


def test_a_bar_may_lack_everything_but_its_close(seeded: Store) -> None:
    put(seeded, "bars", open=None, high=None, low=None, volume=None)


# ── snapshots ──


@pytest.mark.parametrize(
    "overrides",
    [
        {"market_data_status": "REALTIME"},
        {"market_data_status": "frozen_delayed"},
        {"market_data_status": ""},
        {"quote_delay_seconds": -1},
        {"bid": -0.1},
        {"implied_vol": -0.1},
        {"implied_vol_solved": 0.2},  # a solved figure names its source
        {"implied_vol_solved_source": "mid"},  # and a source needs its figure
        {"dividend_yield": 0.01},
        {"dividend_yield_basis": "trailing"},
        {"collected_at": "yesterday"},
        {"snapshot_date": "2026-10-08T00:00:00Z"},
    ],
)
def test_a_bad_snapshot_is_refused(seeded: Store, overrides: dict[str, Any]) -> None:
    refuses(seeded, "snapshots", **overrides)


@pytest.mark.parametrize("status", ["realtime", "delayed", "frozen", "none", None])
def test_every_decoded_status_and_a_missing_one_is_accepted(
    seeded: Store, status: str | None
) -> None:
    put(seeded, "snapshots", market_data_status=status, market_data_raw=None)


def test_a_route_flag_the_decoder_does_not_know_is_kept_beside_a_missing_status(
    seeded: Store,
) -> None:
    put(seeded, "snapshots", market_data_status=None, market_data_raw="SOMETHING_NEW")
    row = seeded.execute("SELECT market_data_status, market_data_raw FROM snapshots").fetchone()
    assert tuple(row) == (None, "SOMETHING_NEW")


def test_a_solved_figure_and_its_source_travel_together(seeded: Store) -> None:
    put(seeded, "snapshots", implied_vol_solved=0.2, implied_vol_solved_source="mid")
    put(
        seeded,
        "snapshots",
        snapshot_date="2026-10-07",
        dividend_yield=0.01,
        dividend_yield_basis="ttm",
    )


# ── chains and curves ──


@pytest.mark.parametrize(
    "overrides",
    [
        {"right": "X"},
        {"right": "c"},
        {"strike": 0.0},
        {"strike": -5.0},
        {"expiry": "2026-10-07"},  # before the snapshot date
        {"delta": 1.5},
        {"delta": -1.01},
        {"iv": -0.2},
        {"open_interest": -1},
        {"volume": -1},
        {"option_conid": 0},
        {"sigma_used": 0.3},  # a sigma names its source
        {"sigma_source": "realised_30d"},
        {"market_data_status": "live"},
        {"underlying_conid": 424242},
    ],
)
def test_a_bad_chain_row_is_refused(seeded: Store, overrides: dict[str, Any]) -> None:
    refuses(seeded, "chain_snapshots", **overrides)


def test_a_chain_row_is_unique_per_day_underlying_expiry_strike_and_right(seeded: Store) -> None:
    put(seeded, "chain_snapshots")
    refuses(seeded, "chain_snapshots")
    put(seeded, "chain_snapshots", right="P")
    put(seeded, "chain_snapshots", strike=105.0)
    put(seeded, "chain_snapshots", expiry="2027-02-19")
    put(seeded, "chain_snapshots", snapshot_date="2026-10-07")


def test_a_chain_row_expiring_on_its_snapshot_day_is_allowed(seeded: Store) -> None:
    put(seeded, "chain_snapshots", expiry="2026-10-08")


@pytest.mark.parametrize(
    "overrides",
    [
        {"root": " "},
        {"expiry": "2026-10-07"},
        {"price": -1.0},
        {"currency": "usd"},
        {"conid": 0},
        {"market_data_status": "stale"},
    ],
)
def test_a_bad_curve_point_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "curves", **overrides)


def test_curve_points_are_unique_per_day_root_and_expiry(store: Store) -> None:
    put(store, "curves")
    refuses(store, "curves")
    put(store, "curves", expiry="2027-01-19")
    put(store, "curves", root="NG")


# ── macro ──


@pytest.mark.parametrize(
    "overrides",
    [
        {"value": None},
        {"reference_date": "2026-07"},
        {"release_date": "12 August 2026"},
        {"series_id": ""},
        {"source": " "},
        {"collected_at": "2026-10-09"},
    ],
)
def test_a_bad_macro_row_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "macro", **overrides)


def test_a_release_before_the_period_it_describes_is_held(store: Store) -> None:
    """010_sources dropped 001's refusal: a flash estimate dated at its period's end, or a policy
    rate announced before the day it applies, is public before its reference date."""
    put(store, "macro", release_date="2026-06-30")
    row = store.execute("SELECT reference_date, release_date FROM macro").fetchone()
    assert tuple(row) == ("2026-07-01", "2026-06-30")


def test_macro_holds_both_dates_and_may_lack_a_release_date(store: Store) -> None:
    put(store, "macro")
    put(store, "macro", reference_date="2026-08-01", release_date=None)
    rows = store.execute("SELECT reference_date, release_date FROM macro ORDER BY 1").fetchall()
    assert [tuple(row) for row in rows] == [
        ("2026-07-01", "2026-08-12"),
        ("2026-08-01", None),
    ]


def test_a_macro_period_is_unique_per_series_and_source(store: Store) -> None:
    put(store, "macro")
    refuses(store, "macro")
    put(store, "macro", source="oecd")


# ── runs and steps ──


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "finished"},
        {"status": "ok"},  # a verdict needs a finish time
        {"status": "running", "finished_at": "2026-10-09T22:00:00Z"},  # before it started
        {"steps_planned": -1},
        {"steps_completed": 4},  # more than planned
        {"run_id": ""},
        {"command": " "},
        {"session_date": "2026-10-08T00:00:00Z"},
    ],
)
def test_a_bad_run_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "runs", **overrides)


def test_a_finished_run_names_its_verdict(store: Store) -> None:
    for status in ("ok", "partial", "degraded", "skipped", "error", "interrupted"):
        put(store, "runs", run_id=status, status=status, finished_at="2026-10-09T23:00:00Z")


def test_a_step_is_standalone_or_has_a_position_in_a_run(store: Store) -> None:
    put(store, "runs")
    put(store, "run_steps")  # standalone: no run, no position
    put(store, "run_steps")  # any number of them
    put(store, "run_steps", run_id="run-1", step_index=0)
    put(store, "run_steps", run_id="run-1", step_index=1)
    refuses(store, "run_steps", run_id="run-1", step_index=1)  # a position is taken once
    refuses(store, "run_steps", run_id="run-1")  # a run's step has a position
    refuses(store, "run_steps", step_index=2)  # and a position needs a run
    refuses(store, "run_steps", run_id="no-such-run", step_index=0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "finished"},
        {"status": "error"},  # a verdict needs a finish time
        {"rows_written": -1},
        {"targets_ok": -1},
        {"targets_failed": -1},
        {"collector": ""},
        {"finished_at": "2026-10-09T22:00:00Z", "status": "ok"},  # before it started
    ],
)
def test_a_bad_step_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "run_steps", **overrides)


def test_a_run_with_steps_cannot_be_deleted(store: Store) -> None:
    put(store, "runs")
    put(store, "run_steps", run_id="run-1", step_index=0)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("DELETE FROM runs WHERE run_id = 'run-1'")


# ── measurements ──


@pytest.mark.parametrize(
    "overrides",
    [
        {"entity_kind": "region"},
        {"entity_id": " "},
        {"as_of": "2026-10"},
        {"status": "missing"},
        {"value": None},
        {"value": float("inf")},
        {"status": "n/a", "reason": "r", "reason_code": "c"},
        {"status": "n/a", "value": None, "reason": "r"},
        {"status": "inapplicable", "value": None, "reason_code": "c"},
        {"status": "stale", "value": None, "reason": "r", "reason_code": "c"},
        {"n_obs": -1},
        {"market_data_status": "REALTIME"},
        {"inputs_hash": "abc"},
        {"inputs_hash": "G" * 64},
        {"computed_at": "2026-10-09"},
    ],
)
def test_a_bad_measurement_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "measurements", **overrides)


def test_an_absent_leaf_says_why_and_carries_no_value(store: Store) -> None:
    put(store, "measurements", status="n/a", value=None, reason="no bars", reason_code="short")
    put(store, "measurements", leaf="x", status="stale", reason="old", reason_code="inputs_stale")
    assert store.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 2


def test_a_leaf_is_unique_per_entity_and_session(store: Store) -> None:
    put(store, "measurements")
    refuses(store, "measurements")
    put(store, "measurements", as_of="2026-10-09")
    put(store, "measurements", entity_kind="macro_cell")


def test_the_new_entity_kinds_are_accepted(store: Store) -> None:
    put(store, "measurements", entity_kind="market", entity_id="us")
    put(store, "measurements", entity_kind="commodity_root", entity_id="CL")
    assert store.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 2


# ── the options sleeve ──


@pytest.mark.parametrize(
    "overrides",
    [
        {"right": "X"},
        {"contracts": 0},
        {"strike": 0},
        {"premium_per_unit": "0"},
        {"premium_base": ""},
        {"currency": "usd"},
        {"close_by": "2026-10-07"},  # before it opened
        {"close_by": "2027-02-01"},  # after the expiry
        {"closed_on": "2026-10-09"},  # closed without a reason
        {"exit_reason": "target"},  # a reason without a close
        {"closed_on": "2026-10-07", "exit_reason": "target"},  # closed before it opened
        {"prob_target_at_entry": 1.5},
        {"fx_to_base": "0"},
    ],
)
def test_a_bad_option_position_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "option_positions", **overrides)


def test_an_option_position_closes_once_and_is_never_rewritten(store: Store) -> None:
    put(store, "option_positions")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE option_positions SET contracts = 2 WHERE position_id = 1")
    store.execute(
        "UPDATE option_positions SET closed_on = '2026-10-20', exit_reason = 'target' "
        "WHERE position_id = 1"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE option_positions SET exit_reason = 'zombie' WHERE position_id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("DELETE FROM option_positions")


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": "refund"},
        {"position_id": None},  # a commitment belongs to a position
        {"position_id": 99},  # to one that exists
        {"amount": "-1"},
        {"penalty_balance_after": "-0.01"},
        {"note": " "},
        {"quote_currency": "USD"},  # a conversion is stated whole
        {"event_type": "qualifying_gain", "position_id": None},  # names its instrument
        {"event_type": "qualifying_gain", "position_id": None, "conid": 5},  # and its reason
        {"event_type": "qualifying_gain", "conid": 5, "exit_reason": "target"},  # no position
    ],
)
def test_a_bad_ledger_event_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    put(store, "option_positions")
    refuses(store, "option_ledger", **overrides)


def test_the_ledger_is_append_only(store: Store) -> None:
    put(store, "option_positions")
    put(store, "option_ledger")
    put(
        store,
        "option_ledger",
        event_type="qualifying_gain",
        position_id=None,
        conid=5,
        exit_reason="target",
        amount="10",
        penalty_added="-10",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE option_ledger SET amount = '1'")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("DELETE FROM option_ledger")


@pytest.mark.parametrize(
    "overrides",
    [
        {"all_conditions_met": 2},
        {"position_id": 99},
        {"detail": "not json"},
        {"prob_target": 1.2},
    ],
)
def test_a_bad_zombie_observation_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    put(store, "option_positions")
    refuses(store, "zombie_observations", **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"snapshot_date": "2026-10-10"},  # priced off a chain from after its session
        {"mid": 0},
        {"right": "c"},
        {"untested": "{}"},
        {"activation": ""},
        {"delta": 1.5},
        {"market_data_status": "REALTIME"},
    ],
)
def test_a_bad_shadow_entry_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "option_shadow_entries", **overrides)
