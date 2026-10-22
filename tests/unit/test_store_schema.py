"""The shape of the migrations (``001_core.sql`` through ``008_capital.sql``) and the constraints
that keep a bad row out.

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
        {"release_date": "2026-06-30"},  # published before the period it describes
        {"reference_date": "2026-07"},
        {"release_date": "12 August 2026"},
        {"series_id": ""},
        {"source": " "},
        {"collected_at": "2026-10-09"},
    ],
)
def test_a_bad_macro_row_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    refuses(store, "macro", **overrides)


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
