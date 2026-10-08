"""The tool surface: what is registered, what it returns, and what it refuses.

Driven through an in-memory client session rather than by calling the handlers
directly, so the schemas, the annotations and the JSON round-trip are all
exercised. A handler that works but whose schema a client cannot call is not a
working tool.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from obliskiq.server.app import build_server

SESSION = date(2026, 1, 15)
NAV = 200_000.0

#: Every tool BUILD-BRIEF.md names. The list is asserted exactly: a tool renamed
#: without the brief being updated is a tool the skill will call and not find.
EXPECTED_TOOLS = {
    "get_sleeve_status",
    "get_grid_rankings",
    "get_grid_cell",
    "get_portfolio_review",
    "check_position_risk",
    "get_commodity_state",
    "get_fx_real_returns",
    "screen_options",
    "get_catalyst_calendar",
    "log_entry",
    "log_exit",
    "log_reunderwrite",
    "upsert_external_position",
    "record_iv_percentile",
    "run_collectors",
    "get_system_health",
}

#: The tools that only read. Everything else writes, and says so.
READ_ONLY_TOOLS = {
    "get_sleeve_status",
    "get_grid_rankings",
    "get_grid_cell",
    "get_portfolio_review",
    "get_commodity_state",
    "get_fx_real_returns",
    "screen_options",
    "get_catalyst_calendar",
    "get_system_health",
}


@pytest.fixture()
def server(server_context):
    return build_server(server_context)


async def _call(server, name: str, **arguments):
    """Call one tool and return its structured payload."""
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(name, arguments)
        text = "".join(block.text for block in result.content if block.type == "text")
        return result, (json.loads(text) if text and not result.isError else text)


def _seed_nav(store, *, as_of: date = SESSION, nav: float = NAV) -> None:
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO nav_history (as_of, nav_base, source, recorded_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (as_of) DO UPDATE SET nav_base = excluded.nav_base",
            (as_of.isoformat(), nav, "ibkr", "2026-01-15T00:00:00Z"),
        )


# ── registration ──


async def test_every_tool_in_the_brief_is_registered(server):
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_read_tools_are_annotated_read_only_and_writers_are_not(server):
    for tool in await server.list_tools():
        read_only = bool(tool.annotations and tool.annotations.readOnlyHint)
        assert read_only is (tool.name in READ_ONLY_TOOLS), tool.name


async def test_check_position_risk_is_not_read_only(server):
    """It records the review so the two-session zombie confirmation can accumulate.

    Annotating it read-only would be a lie a client could act on by caching or
    deduplicating the call, and the recording is the mechanism.
    """
    tool = next(t for t in await server.list_tools() if t.name == "check_position_risk")
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False
    assert "WRITES" in (tool.description or "")


async def test_every_tool_carries_a_description(server):
    for tool in await server.list_tools():
        assert tool.description, tool.name


async def test_no_tool_can_edit_a_thesis(server):
    """thesis_text is written once at entry. Only log_entry may name it at all."""
    for tool in await server.list_tools():
        properties = (tool.inputSchema or {}).get("properties", {})
        if tool.name == "log_entry":
            assert "thesis_text" in properties
            continue
        assert "thesis_text" not in properties, tool.name
        assert "invalidation_trigger" not in properties, tool.name


# ── the sleeve ──


async def test_sleeve_status_refuses_without_a_nav(server):
    result, payload = await _call(server, "get_sleeve_status")
    assert result.isError
    assert "budget defines the universe" in payload


async def test_sleeve_status_reports_the_budget_and_the_ceiling(server, store):
    _seed_nav(store)
    result, payload = await _call(server, "get_sleeve_status", as_of=SESSION.isoformat())
    assert not result.isError
    assert payload["sleeve"]["nav"] == NAV
    assert payload["sleeve"]["available_budget"] > 0
    assert payload["affordability"]["max_premium_per_contract"] > 0
    assert payload["nav_source"] == "nav_history"


async def test_a_supplied_nav_overrides_the_store(server, store):
    _seed_nav(store)
    _, payload = await _call(server, "get_sleeve_status", nav=50_000.0)
    assert payload["sleeve"]["nav"] == 50_000.0
    assert payload["nav_source"] == "supplied"


async def test_the_affordability_ceiling_filters_before_screening(server, store):
    _seed_nav(store)
    _, payload = await _call(server, "get_sleeve_status", sigma=0.35, tau_years=0.5)
    assert payload["max_underlying_price"] > 0
    assert "never a quote" in payload["max_underlying_price_note"]


# ── the ledger writes ──


async def test_log_entry_rejects_an_entry_at_seventy_dte(server, seeded_store):
    _seed_nav(seeded_store)
    expiry = SESSION + timedelta(days=70)
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00",
        expiry=expiry.isoformat(),
        # Stated so the refusal below is the TENOR gate rather than the earlier
        # one for an entry that never said what trade it is.
        strike=100.0,
        is_call=True,
        target=107.5,
        entry_range_pctile=0.30,
        expected_resolution_days=40,
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "rejected by the server" in payload
    assert "minimum entry tenor" in payload


async def test_log_entry_rejects_an_unknown_contract(server, store):
    _seed_nav(store)
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=999,
        entry_price=2.5,
        thesis_text="thesis",
        invalidation_trigger="spot below 88.00",
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "not in the universe table" in payload


async def test_log_entry_writes_and_freezes_the_thesis(server, seeded_store):
    _seed_nav(seeded_store)
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00 on any session",
        is_option=False,
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload
    assert payload["position_id"] == 1
    assert payload["thesis_is_frozen"] is True

    from obliskiq.store.ledger import get_position

    position = get_position(seeded_store, 1)
    assert position is not None
    assert position.thesis_text.startswith("IV in the bottom decile")


async def test_log_exit_rejects_a_reason_outside_the_config_list(server, seeded_store):
    await _open_one(server, seeded_store)
    result, payload = await _call(
        server, "log_exit", position_id=1, exit_reason="it_went_down", as_of=SESSION.isoformat()
    )
    assert result.isError
    assert "exit_reasons_allowed" in payload
    assert "Price movement alone is never an exit" in payload


async def test_log_exit_accepts_a_configured_reason(server, seeded_store):
    """An equity close is accepted, and books NOTHING to the option sleeve."""
    await _open_one(server, seeded_store)
    result, payload = await _call(
        server,
        "log_exit",
        position_id=1,
        exit_reason="invalidation",
        realised_pnl=-500.0,
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload
    assert payload["exit_reason"] == "invalidation"
    assert payload["is_option"] is False
    assert payload["is_option_source"] == "ledger"
    assert payload["penalty_event"] is None, "an equity loss never touches the option sleeve"


async def test_a_close_cannot_redeclare_which_sleeve_a_position_was_in(server, seeded_store):
    """The defect: is_option was the caller's to state, and nothing could check it.

    Closing this equity position as an option booked a penalty of
    loss_penalty_multiple x loss against a loss the sleeve never carried, and
    the store held nothing that could detect it afterwards.
    """
    await _open_one(server, seeded_store)
    result, payload = await _call(
        server,
        "log_exit",
        position_id=1,
        exit_reason="invalidation",
        realised_pnl=-500.0,
        is_option=True,
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "contradicts the ledger" in payload


async def test_an_option_loss_books_the_penalty_from_the_ledgers_own_flag(server, seeded_store):
    """And the other direction: the sleeve consequence no longer needs declaring."""
    _seed_nav(seeded_store)
    position_id = _open_dead_option(seeded_store)
    result, payload = await _call(
        server,
        "log_exit",
        position_id=position_id,
        exit_reason="invalidation",
        realised_pnl=-500.0,
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload
    assert payload["is_option"] is True
    assert payload["is_option_source"] == "ledger"
    assert payload["penalty_event"]["penalty_added"] > 0


async def test_a_pnl_in_an_unconvertible_currency_is_refused(server, seeded_store):
    """A penalty booked on an unconverted figure is wrong and cannot be reversed.

    The store holds no FX series here, so a USD loss against a EUR sleeve has no
    rate. The close is refused rather than booked at an assumed parity, which is
    the same direction the affordability gate refuses in.
    """
    _seed_nav(seeded_store)
    position_id = _open_dead_option(seeded_store)
    result, payload = await _call(
        server,
        "log_exit",
        position_id=position_id,
        exit_reason="invalidation",
        realised_pnl=-500.0,
        realised_pnl_currency="USD",
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "no rate for that pair" in payload


async def test_an_override_gain_does_not_reduce_the_penalty(server, seeded_store):
    """Cutting a winner early to bank profit cannot unlock the sleeve."""
    await _open_one(server, seeded_store)
    _, payload = await _call(
        server,
        "log_exit",
        position_id=1,
        exit_reason="override",
        realised_pnl=5_000.0,
        is_option=False,
        as_of=SESSION.isoformat(),
    )
    assert payload["penalty_event"] is None
    assert "does not reduce the penalty" in payload["settlement"]


async def test_a_qualifying_gain_reduces_the_penalty(server, seeded_store):
    await _open_one(server, seeded_store)
    _, payload = await _call(
        server,
        "log_exit",
        position_id=1,
        exit_reason="target",
        realised_pnl=5_000.0,
        is_option=False,
        as_of=SESSION.isoformat(),
    )
    assert payload["penalty_event"]["penalty_added"] == -5_000.0


async def test_log_reunderwrite_rejects_a_catalyst_that_existed_at_entry(server, seeded_store):
    await _open_one(server, seeded_store)
    result, payload = await _call(
        server,
        "log_reunderwrite",
        position_id=1,
        catalyst="the same ruling",
        catalyst_date="2026-01-01",
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "part of the original thesis" in payload


async def test_log_reunderwrite_rejects_an_undated_catalyst(server, seeded_store):
    await _open_one(server, seeded_store)
    result, payload = await _call(
        server,
        "log_reunderwrite",
        position_id=1,
        catalyst="things should improve",
        catalyst_date="soon",
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "falling price is not a catalyst" in payload.lower()


async def test_log_reunderwrite_appends_without_touching_the_thesis(server, seeded_store):
    await _open_one(server, seeded_store)
    _, payload = await _call(
        server,
        "log_reunderwrite",
        position_id=1,
        catalyst="antitrust ruling",
        catalyst_date="2026-03-02",
        note="re-underwritten on a new dated catalyst",
        as_of=SESSION.isoformat(),
    )
    assert len(payload["reunderwrite_log"]) == 1
    assert payload["thesis_unchanged"].startswith("IV in the bottom decile")
    assert payload["averaging_down"]["allowed"] is True


# ── external positions ──


async def test_upsert_external_position_round_trips_and_updates_in_place(server, store):
    _, first = await _call(
        server,
        "upsert_external_position",
        broker="DEGIRO",
        symbol="ASML",
        quantity=3,
        currency="EUR",
        market_value_base=2_400.0,
        as_of=SESSION.isoformat(),
    )
    assert first["external_total_base"] == 2_400.0

    _, second = await _call(
        server,
        "upsert_external_position",
        broker="DEGIRO",
        symbol="ASML",
        quantity=4,
        currency="EUR",
        market_value_base=3_200.0,
        as_of=SESSION.isoformat(),
    )
    assert len(second["external_positions"]) == 1
    assert second["external_total_base"] == 3_200.0


async def test_a_stale_external_holding_is_flagged(server, store):
    stale_day = SESSION - timedelta(days=400)
    _, payload = await _call(
        server,
        "upsert_external_position",
        broker="Revolut",
        symbol="VWCE",
        quantity=10,
        currency="EUR",
        market_value_base=1_000.0,
        last_updated=stale_day.isoformat(),
        as_of=SESSION.isoformat(),
    )
    assert payload["stale"] == ["Revolut:VWCE"]
    assert "UNRELIABLE" in payload["stale_note"]


# ── reads over an empty store ──


async def test_the_grid_reader_says_no_grid_has_been_built(server):
    result, payload = await _call(server, "get_grid_rankings")
    assert result.isError
    assert "no grid has been built" in payload


async def test_a_ranked_row_reaches_the_caller_with_its_trajectory(server, store, config):
    """The tool serves the STORED rankings, so this is the path that matters.

    A ranked value is a level; the trajectory says which way it is moving, and
    the whole point of putting it on the row was that a session should not need
    a get_grid_cell call per row to find out. Each leaf keeps its own status and
    window across the JSON round trip, and a leaf the store could not measure
    stays absent rather than arriving as a zero.
    """
    from obliskiq.compute.rankings import Rankings

    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    cells = [
        (f"parkinson_vol_{tenor}d_percentile", 0.04, "realised_vol", "ok", "2y (16 readings)"),
        ("return_21d_local", -0.031, "returns", "ok", "21d (22 bars)"),
        ("return_63d_local", 0.092, "returns", "stale", "63d (64 bars)"),
        ("rv_term_slope", -0.28, "rv_term_structure", "ok", "10d vs 90d"),
    ]
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO grid (as_of, market, sector, indicator, value, status, reason, "
            "window, indicator_family, units) VALUES (?, 'us', 'broad', ?, ?, ?, ?, ?, ?, "
            "'decimal')",
            [
                (
                    SESSION.isoformat(),
                    indicator,
                    value,
                    status,
                    None if status == "ok" else "seeded",
                    window,
                    family,
                )
                for indicator, value, family, status, window in cells
            ],
        )
    rankings = Rankings(store, config)
    rankings.write(rankings.build(as_of=SESSION), as_of=SESSION)

    _, payload = await _call(server, "get_grid_rankings", dimension="most_compressed")
    row = payload["rankings"]["most_compressed"][0]

    assert row["value"] == 0.04
    assert row["trajectory"]["return_21d_local"] == {
        "value": -0.031,
        "status": "ok",
        "window": "21d (22 bars)",
        "units": "decimal",
    }
    assert row["trajectory"]["return_63d_local"]["status"] == "stale", (
        "a stale leaf beside a fresh percentile has to say so in the payload too"
    )
    assert "return_21d_base" not in row["trajectory"], "an unmeasured leaf is absent, not zero"
    assert row["indicator"] not in row["trajectory"], "the ranked leaf is not repeated"
    assert "trajectory" in payload["row_note"]


async def test_get_grid_cell_refuses_an_unnarrowed_request(server):
    result, payload = await _call(server, "get_grid_cell")
    assert result.isError
    assert "at least one of market, sector or indicator" in payload


async def test_screen_options_reports_that_nothing_was_collected(server, store):
    _seed_nav(store)
    result, payload = await _call(server, "screen_options", as_of=SESSION.isoformat())
    assert not result.isError
    assert "no chain snapshots" in payload["blocked"]


async def test_the_catalyst_calendar_names_what_it_cannot_verify(server):
    """The gap is returned, not left to be inferred from an empty calendar."""
    _, payload = await _call(server, "get_catalyst_calendar", as_of=SESSION.isoformat())
    kinds = {entry["kind"] for entry in payload["calendar"]["unverifiable"]}
    assert {"earnings", "index_events", "central_bank", "corporate_actions"} <= kinds
    assert "company IR" in payload["verification_required"]
    assert "closing auction" in payload["verification_required"]


async def test_the_catalyst_calendar_returns_ledger_dates(server, seeded_store):
    await _open_one(server, seeded_store, mandatory_close="2026-04-10")
    _, payload = await _call(server, "get_catalyst_calendar", as_of=SESSION.isoformat())
    events = payload["calendar"]["events"]
    assert any(event["kind"] == "mandatory_close" for event in events)
    assert all(event["source"] for event in events)


async def test_portfolio_review_quotes_the_thesis_verbatim(server, seeded_store):
    _seed_nav(seeded_store)
    await _open_one(server, seeded_store)
    _, payload = await _call(
        server,
        "get_portfolio_review",
        positions=[
            {
                "symbol": "TEST",
                "market_value_base": 20_000.0,
                "sector": "industrials",
                "country": "NL",
                "currency": "EUR",
            }
        ],
        as_of=SESSION.isoformat(),
    )
    row = payload["ledger"][0]
    assert row["thesis_text"] == "IV in the bottom decile into a dated ruling"
    assert row["thesis_is_frozen"] is True
    assert row["days_held"] == 14
    assert payload["caps"]["nav"] == NAV


async def test_caps_are_unreliable_when_external_data_is_stale(server, seeded_store):
    _seed_nav(seeded_store)
    stale_day = SESSION - timedelta(days=400)
    await _call(
        server,
        "upsert_external_position",
        broker="Openbank",
        symbol="SAN",
        quantity=100,
        currency="EUR",
        market_value_base=400.0,
        last_updated=stale_day.isoformat(),
        as_of=SESSION.isoformat(),
    )
    _, payload = await _call(
        server,
        "get_portfolio_review",
        positions=[{"symbol": "TEST", "market_value_base": 20_000.0}],
        as_of=SESSION.isoformat(),
    )
    assert payload["caps_reliable"] is False
    assert payload["caps"]["status"] == "UNRELIABLE"


async def test_check_position_risk_without_contract_terms_says_so(server, seeded_store):
    await _open_one(server, seeded_store)
    _, payload = await _call(
        server, "check_position_risk", position_id=1, as_of=SESSION.isoformat()
    )
    assert payload["option"] is None
    assert "spot, strike, expiry and is_call are all required" in payload["option_note"]
    assert "not one of the four zombie conditions" in payload["drawdown_is_not_a_trigger"]


async def test_the_zombie_test_needs_two_consecutive_sessions(server, seeded_store):
    """One session cannot trigger an exit, and the second books at DETECTION.

    Driven through the tool rather than the engine, because the recording is what
    makes the two-session confirmation possible and a read-only tool could never
    accumulate the second session.
    """
    _seed_nav(seeded_store)
    _seed_dead_chain(seeded_store)
    position_id = _open_dead_option(seeded_store)

    _, first = await _call(server, "check_position_risk", **_dead_args(position_id, ZOMBIE_DAY))
    assert first["zombie"]["fires"] is False
    assert first["zombie"]["consecutive_sessions"] == 1
    assert first["recorded"] is True

    _, second = await _call(
        server, "check_position_risk", **_dead_args(position_id, ZOMBIE_DAY + timedelta(days=1))
    )
    assert second["zombie"]["fires"] is True
    assert second["zombie"]["consecutive_sessions"] == 2

    # Booked once, at detection. A later close reconciles by the delta only.
    from obliskiq.compute.penalty import PenaltyLedger

    penalty = PenaltyLedger(seeded_store, server_config(server))
    assert penalty.balance() > 0
    assert penalty.verify() == []


async def test_n_d2_is_computed_on_the_strikes_own_smile(server, seeded_store):
    """Not ATM vol: constant-vol BS produces false zombie positives on OTM strikes."""
    _seed_nav(seeded_store)
    _seed_dead_chain(seeded_store)
    position_id = _open_dead_option(seeded_store)
    _, payload = await _call(server, "check_position_risk", **_dead_args(position_id, ZOMBIE_DAY))
    assert payload["prob_vol_source"] == "strike_smile_iv"
    # The 3:1 target sits at 129, between the 120 and 140 wings (0.42 and 0.50),
    # so the vol used is the interpolated wing — NOT the 0.36 at-the-money quote
    # a constant-vol model would have used.
    assert payload["option"]["vol_used"] == pytest.approx(0.456, abs=1e-9)
    assert payload["option"]["target"]["level"] == pytest.approx(129.0, abs=1e-9)
    assert "120" in payload["smile_provenance"] and "140" in payload["smile_provenance"]


async def test_a_dry_read_does_not_accumulate_a_session(server, seeded_store):
    _seed_nav(seeded_store)
    _seed_dead_chain(seeded_store)
    position_id = _open_dead_option(seeded_store)

    args = _dead_args(position_id, ZOMBIE_DAY)
    _, dry = await _call(server, "check_position_risk", record=False, **args)
    assert dry["recorded"] is False

    _, live = await _call(
        server, "check_position_risk", **_dead_args(position_id, ZOMBIE_DAY + timedelta(days=1))
    )
    assert live["zombie"]["consecutive_sessions"] == 1
    assert live["zombie"]["fires"] is False


async def test_an_option_can_be_assessed_before_it_is_in_the_ledger(server, seeded_store):
    """prob_target_at_entry has to be computed BEFORE log_entry records it."""
    _seed_nav(seeded_store)
    _seed_dead_chain(seeded_store)
    args = _dead_args(0, ZOMBIE_DAY)
    args.pop("position_id")
    _, payload = await _call(server, "check_position_risk", contract_id=1, **args)
    assert payload["option"]["prob_target"] >= 0.0
    assert "position" not in payload


async def test_the_smile_refuses_to_pool_unrelated_underlyings(server, seeded_store):
    """N(d2) off a surface built from several instruments is a number about nothing."""
    _seed_nav(seeded_store)
    _seed_dead_chain(seeded_store)
    args = _dead_args(0, ZOMBIE_DAY)
    args.pop("position_id")
    result, payload = await _call(server, "check_position_risk", **args)
    assert result.isError
    assert "ONE underlying's chain" in payload


async def test_check_position_risk_refuses_an_unknown_position(server):
    result, payload = await _call(server, "check_position_risk", position_id=42)
    assert result.isError
    assert "no position 42" in payload


async def test_commodity_state_lists_the_tracked_roots(server, store):
    _seed_root(store)
    _, payload = await _call(server, "get_commodity_state", as_of=SESSION.isoformat())
    assert [entry["root"] for entry in payload["roots"]] == ["CL"]
    assert "never one bucket" in payload["note"]


async def test_steep_contango_rejects_a_futures_tracking_wrapper(server, store):
    """Term structure decides the instrument, not just the view.

    A futures-tracking wrapper bleeds roll continuously over a 90-200 day hold
    whether or not the thesis is right, so beyond the configured ceiling the view
    belongs in producer equity or a physical ETC.
    """
    _seed_root(store)
    _seed_curve(store, [70.0, 75.0, 80.0])
    _, payload = await _call(server, "get_commodity_state", root="CL", as_of=SESSION.isoformat())
    assert payload["rejects_futures_tracking"] is True
    assert payload["wrapper"]["rejected"] == "futures_tracking_wrapper"
    assert "futures_tracking_wrapper" not in payload["wrapper"]["alternatives"]
    assert payload["wrapper"]["recommended"] in ("eu_listed_physical_etc", "producer_equity")
    assert "prohibited" in payload["futures_contract_note"]


async def test_a_view_with_no_curve_still_carries_a_wrapper_block(server, store):
    """No curve means no recommendation — and it says so rather than defaulting."""
    _seed_root(store)
    _, payload = await _call(server, "get_commodity_state", root="CL", as_of=SESSION.isoformat())
    assert payload["wrapper"]["recommended"] is None
    assert payload["wrapper"]["reason"]


async def test_commodity_state_refuses_an_undeclared_root(server):
    result, payload = await _call(server, "get_commodity_state", root="NOTAROOT")
    assert result.isError
    assert "is not tracked" in payload


async def test_fx_ranks_on_base_real_only(server):
    _, payload = await _call(server, "get_fx_real_returns", as_of=SESSION.isoformat())
    assert payload["rank_on"] == "base_real"
    assert "display-only" in payload["ranking_note"]
    assert "not an execution mandate" in payload["hedging_note"]


async def test_the_carry_test_needs_the_currency_its_yield_is_quoted_in(server):
    result, payload = await _call(server, "get_fx_real_returns", nominal_yield=0.35)
    assert result.isError
    assert "needs the currency" in payload


async def test_run_collectors_rejects_a_task_outside_the_allowed_list(server):
    result, payload = await _call(server, "run_collectors", task="rm -rf /")
    assert result.isError
    assert "allowed_tasks" in payload


# ── helpers ──

#: A session inside theta acceleration for EXPIRY, so the DTE condition holds.
ZOMBIE_DAY = date(2026, 3, 2)
EXPIRY = date(2026, 4, 10)


def server_config(server):
    """The config the server was built against, for asserting on the store after."""
    from obliskiq.config import load_config

    return load_config("config.yaml")


def _seed_dead_chain(store) -> None:
    """A smile around a spot of 90, with a strike of 120 far out of the money."""
    rows = [
        (80.0, "P", 0.46),
        (90.0, "P", 0.40),
        (100.0, "C", 0.36),
        (120.0, "C", 0.42),
        (140.0, "C", 0.50),
    ]
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, bid, ask, last, iv, open_interest, volume, source, market_data_status) "
            # A spread of 2.3% of mid, a deep book and real session volume: this
            # fixture is here to exercise the zombie test, so it must clear smile
            # hygiene rather than being dropped by it.
            "VALUES (?, ?, ?, ?, ?, 0.43, 0.44, 0.435, ?, 2000, 30, 'cp_api', 'REALTIME')",
            [
                (ZOMBIE_DAY.isoformat(), 1, EXPIRY.isoformat(), strike, right, iv)
                for strike, right, iv in rows
            ],
        )


def _open_dead_option(store) -> int:
    from obliskiq.store.ledger import open_position

    return open_position(
        store,
        contract_id=1,
        is_option=True,
        entry_currency="EUR",
        opened_at="2025-11-01",
        entry_price=3.00,
        thesis_text="implied vol in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00",
        prob_target_at_entry=0.30,
    )


def _dead_args(position_id: int, session: date) -> dict:
    """A position matching all four zombie conditions on the given session."""
    return {
        "position_id": position_id,
        "as_of": session.isoformat(),
        "spot": 90.0,
        "strike": 120.0,
        "is_call": True,
        "expiry": EXPIRY.isoformat(),
        "premium_paid": 3.00,
        "current_bid": 0.60,
        "rate": 0.03,
    }


def _seed_root(store) -> None:
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO futures_roots (root, exchange, currency, bucket, description, "
            "tracked, seasonal) VALUES (?, ?, ?, ?, ?, 1, 0)",
            ("CL", "NYMEX", "USD", "energy", "WTI crude"),
        )


def _seed_curve(store, prices: list[float]) -> None:
    """A monthly ladder with the roll the collector would have computed.

    The engine reads the stored annualised_roll rather than re-deriving it, so a
    ladder seeded without one has no shape and dictates no wrapper.
    """
    from obliskiq.collect.curve_collector import annualised_roll

    rows = []
    for index, price in enumerate(prices):
        expiry = SESSION + timedelta(days=30 * (index + 1))
        roll = (
            None
            if index == 0
            else annualised_roll(
                front_price=prices[0],
                deferred_price=price,
                days_apart=(expiry - (SESSION + timedelta(days=30))).days,
                calendar_days_per_year=365.0,
            )
        )
        rows.append((SESSION.isoformat(), "CL", expiry.isoformat(), price, roll, "cp_api"))
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO curve_snapshots (snapshot_date, root, expiry, price, "
            "annualised_roll, source) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


async def _open_one(server, store, *, mandatory_close: str | None = None) -> None:
    """One open equity position, written through the tool so the gates ran."""
    _seed_nav(store)
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00 on any session",
        is_option=False,
        opened_at="2026-01-01",
        mandatory_close_date=mandatory_close,
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload


# ── record_iv_percentile: the bridge for grid indicator 8 ──


async def test_a_vendor_percentile_is_stored_with_its_source(server, seeded_store):
    """The reading exists on the MCP route; this is how it reaches the store."""
    result, payload = await _call(
        server,
        "record_iv_percentile",
        symbol="TEST",
        iv_percentile_52w=0.1195,
        iv_percentile_26w=0.0315,
        iv_percentile_13w=0.0635,
        snapshot_date=SESSION.isoformat(),
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload
    assert payload["contract_id"] == 1
    assert payload["source"] == "mcp"

    row = seeded_store.execute(
        "SELECT iv_percentile_52w, iv_percentile_26w, iv_percentile_13w, source, "
        "iv_percentile_route FROM underlying_snapshots WHERE contract_id = 1"
    ).fetchone()
    assert row["iv_percentile_52w"] == pytest.approx(0.1195)
    assert row["iv_percentile_26w"] == pytest.approx(0.0315)
    assert row["iv_percentile_13w"] == pytest.approx(0.0635)
    assert row["source"] == "mcp"
    assert row["iv_percentile_route"] == "mcp"


async def test_a_percent_shaped_percentile_is_refused_not_rescaled(server, seeded_store):
    """94 for the 94th percentile is a misreading, and dividing it hides that.

    IBKR already serves a fraction. A rescaled value would look right in a column
    nobody eyeballs, which is exactly how a wrong number survives.
    """
    result, payload = await _call(
        server,
        "record_iv_percentile",
        symbol="TEST",
        iv_percentile_52w=94.0,
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "refused rather than divided" in str(payload)
    assert seeded_store.execute("SELECT COUNT(*) FROM underlying_snapshots").fetchone()[0] == 0, (
        "nothing is written when the value is refused"
    )


async def test_a_shorter_window_percentile_is_validated_too(server):
    result, _ = await _call(
        server,
        "record_iv_percentile",
        symbol="TEST",
        iv_percentile_52w=0.12,
        iv_percentile_13w=6.35,
        as_of=SESSION.isoformat(),
    )
    assert result.isError


async def test_recording_a_percentile_does_not_blank_a_collected_field(server, seeded_store):
    """A blanket upsert would erase the implied vol the chain collector wrote."""
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, "
            "historical_vol_30d, source) VALUES (?, ?, ?, ?, ?)",
            (SESSION.isoformat(), 1, 0.2814, 0.1962, "cp_api"),
        )

    result, payload = await _call(
        server,
        "record_iv_percentile",
        contract_id=1,
        iv_percentile_52w=0.1195,
        snapshot_date=SESSION.isoformat(),
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload

    row = seeded_store.execute(
        "SELECT implied_vol, historical_vol_30d, iv_percentile_52w, source, iv_percentile_route "
        "FROM underlying_snapshots WHERE contract_id = 1"
    ).fetchone()
    assert row["implied_vol"] == pytest.approx(0.2814)
    assert row["historical_vol_30d"] == pytest.approx(0.1962)
    assert row["iv_percentile_52w"] == pytest.approx(0.1195)
    assert row["source"] == "cp_api", (
        "the collector wrote this row; a percentile arriving later does not make "
        "its implied vol MCP-served"
    )
    assert row["iv_percentile_route"] == "mcp", "the percentile carries its own route"


async def test_a_percentile_for_an_unknown_instrument_is_refused(server):
    result, payload = await _call(
        server,
        "record_iv_percentile",
        symbol="NOTINUNIVERSE",
        iv_percentile_52w=0.12,
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "universe" in str(payload)


async def test_a_recorded_percentile_reaches_the_screen_gate(server, seeded_store, config):
    """The point of the bridge: the gate can finally read what the session sees."""
    from obliskiq.compute.screen import OptionScreen

    result, _ = await _call(
        server,
        "record_iv_percentile",
        contract_id=1,
        iv_percentile_52w=0.1195,
        snapshot_date=SESSION.isoformat(),
        as_of=SESSION.isoformat(),
    )
    assert not result.isError
    assert OptionScreen(seeded_store, config).iv_percentile(1) == pytest.approx(0.1195)


# ── the entry disciplines, end to end through the tool ──


async def test_an_option_entry_that_withholds_its_own_terms_is_refused(server, seeded_store):
    """The distinction the guard cannot make and the tool must.

    A gate reported UNTESTED is the STORE lacking something the caller cannot
    fix. This is the caller declining to state its own trade, and letting it
    through would make option_sleeve.min_payoff_ratio,
    discipline.entry_top_decile_requires_catalyst and
    tenor.require_theta_budget_check optional in practice while reading as
    guarantees.
    """
    _seed_nav(seeded_store)
    expiry = SESSION + timedelta(days=150)
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00",
        expiry=expiry.isoformat(),
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "rejected by the server" in payload
    for named in ("strike", "is_call", "target", "entry_range_pctile"):
        assert named in payload
    assert "gates nobody ran" in payload


async def test_an_equity_entry_still_needs_none_of_that(server, seeded_store):
    """The requirement is about options. An equity purchase has no strike, no
    expiry and no theta budget, and demanding them would break the sleeve that
    carries most of the book."""
    _seed_nav(seeded_store)
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00 on any session",
        is_option=False,
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload


async def test_log_entry_rejects_a_top_decile_entry_with_no_catalyst(server, seeded_store, config):
    """ONDS at ~85% of range, IREN at ~75%, both into strength without a dated
    catalyst. The rule that would have caught them was read by no code."""
    _seed_nav(seeded_store)
    pctile = config.require_float("discipline.entry_top_decile_pctile")
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="it keeps going up",
        invalidation_trigger="spot closes below 88.00",
        is_option=False,
        entry_range_pctile=pctile + 0.05,
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "beyond momentum" in payload


async def test_log_entry_rejects_a_target_that_does_not_pay_the_ratio(server, seeded_store, config):
    _seed_nav(seeded_store)
    expiry = SESSION + timedelta(days=150)
    ratio = config.require_float("option_sleeve.min_payoff_ratio")
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00",
        expiry=expiry.isoformat(),
        strike=100.0,
        is_call=True,
        # Pays back barely more than the premium.
        target=100.0 + 2.5 * (ratio - 1.0),
        entry_range_pctile=0.30,
        expected_resolution_days=60,
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "option_sleeve.min_payoff_ratio" in payload


async def test_the_time_window_supplies_the_expected_resolution(server, seeded_store, config):
    """ "The date the thesis has to have resolved by" and "how long the thesis is
    expected to take" are the same commitment written two ways. Asking for both
    invites them to disagree."""
    _seed_nav(seeded_store)
    expiry = SESSION + timedelta(days=150)
    ratio = config.require_float("option_sleeve.min_payoff_ratio")
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00",
        expiry=expiry.isoformat(),
        strike=100.0,
        is_call=True,
        target=100.0 + 2.5 * ratio,
        entry_range_pctile=0.30,
        # No expected_resolution_days: the window is the commitment.
        time_window_end=(SESSION + timedelta(days=60)).isoformat(),
        opened_at=SESSION.isoformat(),
        as_of=SESSION.isoformat(),
    )
    assert result.isError, payload
    assert "expected_resolution_days" not in payload, (
        "the time window should have supplied the resolution horizon"
    )


# ── data quality reaches the tools that display it ──


async def test_check_position_risk_labels_the_market_data_state(server, seeded_store):
    """SKILL.md requires anything FROZEN or DELAYED to be labelled indicative in
    every table it appears in. Until now the column recording it was written by
    the collectors and read by nothing in compute/ or server/."""
    _seed_dead_chain(seeded_store)
    with seeded_store.transaction() as connection:
        connection.execute("UPDATE chain_snapshots SET market_data_status = 'FROZEN'")

    _, payload = await _call(
        server,
        "check_position_risk",
        contract_id=1,
        as_of=ZOMBIE_DAY.isoformat(),
        spot=90.0,
        strike=120.0,
        expiry=EXPIRY.isoformat(),
        is_call=True,
        premium_paid=3.0,
        current_bid=0.6,
        rate=0.03,
        record=False,
    )
    quality = payload["data_quality"]
    assert quality["indicative"] is True
    assert quality["market_data_statuses"] == {"FROZEN": 5}
    assert "INDICATIVE" in quality["note"]


async def test_check_position_risk_reports_what_smile_hygiene_dropped(server, seeded_store):
    """A probability read off three surviving strikes out of forty is a
    different claim from one read off a full book."""
    _seed_dead_chain(seeded_store)

    _, payload = await _call(
        server,
        "check_position_risk",
        contract_id=1,
        as_of=ZOMBIE_DAY.isoformat(),
        spot=90.0,
        strike=120.0,
        expiry=EXPIRY.isoformat(),
        is_call=True,
        premium_paid=3.0,
        current_bid=0.6,
        rate=0.03,
        record=False,
    )
    hygiene = payload["smile_hygiene"]
    assert hygiene["applied"] is True
    assert hygiene["kept"] == 5
    assert "kept" in payload["smile_hygiene_summary"]


async def test_an_undeclared_dividend_yield_is_reported_as_the_zero_dividend_case(
    server, seeded_store
):
    """Not a measured zero. universe.yaml declares no yield for the test row, and
    the response has to say so rather than let a payer be priced as a non-payer
    in silence."""
    _seed_dead_chain(seeded_store)

    _, payload = await _call(
        server,
        "check_position_risk",
        contract_id=1,
        as_of=ZOMBIE_DAY.isoformat(),
        spot=90.0,
        strike=120.0,
        expiry=EXPIRY.isoformat(),
        is_call=True,
        premium_paid=3.0,
        current_bid=0.6,
        rate=0.03,
        record=False,
    )
    data = payload
    assert data["dividend_yield"] == 0.0
    assert data["dividend_yield_source"] == "none_declared"
    assert "ZERO-DIVIDEND case rather than a measured zero" in data["dividend_yield_note"]


async def test_a_declared_dividend_yield_reaches_the_probability(server, seeded_store):
    """And when the universe does declare one, it is used and said so."""
    _seed_dead_chain(seeded_store)
    with seeded_store.transaction() as connection:
        connection.execute(
            "UPDATE universe SET dividend_yield = 0.05, dividend_yield_as_of = '2026-01-02' "
            "WHERE contract_id = 1"
        )

    arguments = {
        "contract_id": 1,
        "as_of": ZOMBIE_DAY.isoformat(),
        "spot": 90.0,
        "strike": 120.0,
        "expiry": EXPIRY.isoformat(),
        "is_call": True,
        "premium_paid": 3.0,
        "current_bid": 0.6,
        "rate": 0.03,
        "record": False,
    }
    _, declared = await _call(server, "check_position_risk", **arguments)
    _, overridden = await _call(server, "check_position_risk", **arguments, dividend_yield=0.0)

    assert declared["dividend_yield"] == 0.05
    assert declared["dividend_yield_source"] == "universe.yaml"
    assert "2026-01-02" in declared["dividend_yield_note"]
    assert overridden["dividend_yield_source"] == "caller"
    assert declared["option"]["prob_target"] < overridden["option"]["prob_target"], (
        "the declared yield did not reach N(d2)"
    )


# ── the sleeve is denominated in base currency, and the WRITE has to agree ──


def _seed_usd_instrument_and_rate(store, *, rate: float = 1.25) -> None:
    """A USD contract, and the ECB rate that converts its premium into base.

    The convention is units of the foreign currency PER EURO, so a 1.25 rate
    makes USD 1,000 of premium EUR 800. BaseCurrencyRates inverts it once so no
    call site has to remember the direction.
    """
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, sector) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (2, "USDNAME", "USD", "STK", "us", "technology"),
        )
        connection.execute(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value) "
            "VALUES (?, ?, ?, ?, ?)",
            ("EURUSD", "ecb", SESSION.isoformat(), SESSION.isoformat(), rate),
        )


def _valid_close_date(config, expiry: date) -> str:
    """The close date the calendar computes, which log_entry requires a match to.

    Computed through the same TradingCalendars the guard uses rather than
    hardcoded: the point of the gate is that a caller-supplied date is checked
    against the calendar, so a test that hardcoded one would break every time a
    holiday list changed and prove nothing about the gate.
    """
    from obliskiq.compute.calendar import TradingCalendars

    calendars = TradingCalendars.from_config(config)
    close = calendars.close_date(
        expiry,
        trading_days=config.require_int("tenor.mandatory_close_trading_days"),
        # The seeded universe row declares no exchange, so the guard resolves the
        # default calendar and this has to resolve the same one.
        exchange=None,
    )
    return close.close_date.isoformat()


async def test_a_usd_commitment_is_booked_in_base_currency(server, seeded_store, config):
    """The defect: the gate converted and the WRITE did not.

    A USD option committed premium x multiplier x contracts in USD, and
    PenaltyLedger summed those raw figures out of a budget denominated in
    account.base_currency — overstating or understating available budget by the
    exchange rate on every non-base position. Every test used EUR, so nothing
    saw it.
    """
    _seed_nav(seeded_store)
    _seed_usd_instrument_and_rate(seeded_store, rate=1.25)
    expiry = SESSION + timedelta(days=150)
    close_date = _valid_close_date(config, expiry)

    result, payload = await _call(
        server,
        "log_entry",
        contract_id=2,
        entry_price=5.0,  # USD 5.00 x 100 x 2 = USD 1,000 = EUR 800 at 1.25
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00 on any session",
        expiry=expiry.isoformat(),
        mandatory_close_date=close_date,
        strike=100.0,
        is_call=True,
        target=200.0,
        entry_range_pctile=0.30,
        expected_resolution_days=40,
        contracts=2,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload

    committed = payload["premium_committed"]
    assert committed["premium_paid"] == pytest.approx(800.0), "booked in base, not in USD"
    assert committed["amount_currency"] == "USD"
    assert committed["amount_quote"] == pytest.approx(1_000.0)
    assert committed["fx_to_base"] == pytest.approx(0.8)
    assert payload["premium_committed_currency"] == "EUR"
    assert payload["sleeve_after"]["committed_premium"] == pytest.approx(800.0)


async def test_an_option_entry_without_an_expiry_is_refused(server, seeded_store):
    """Without it the close date is never computed, and the gate used to vanish."""
    _seed_nav(seeded_store)
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00 on any session",
        strike=100.0,
        is_call=True,
        target=200.0,
        entry_range_pctile=0.30,
        expected_resolution_days=40,
        contracts=1,
        as_of=SESSION.isoformat(),
    )
    assert result.isError
    assert "expiry" in payload


async def test_the_entry_records_its_own_terms(server, seeded_store, config):
    """check_position_risk promised to default the strike from the ledger's terms.

    There were none: position_ledger stored no is_option, contracts, strike,
    right or expiry, so every later gate had to be told again from memory.
    """
    from obliskiq.store.ledger import get_position

    _seed_nav(seeded_store)
    expiry = SESSION + timedelta(days=150)
    close_date = _valid_close_date(config, expiry)
    result, payload = await _call(
        server,
        "log_entry",
        contract_id=1,
        entry_price=2.5,
        thesis_text="IV in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00 on any session",
        expiry=expiry.isoformat(),
        mandatory_close_date=close_date,
        strike=100.0,
        is_call=False,
        target=50.0,
        entry_range_pctile=0.30,
        expected_resolution_days=40,
        contracts=3,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        as_of=SESSION.isoformat(),
    )
    assert not result.isError, payload

    position = get_position(seeded_store, int(payload["position_id"]))
    assert position is not None
    assert position.is_option is True
    assert position.contracts == 3
    assert position.strike == pytest.approx(100.0)
    assert position.is_call is False
    assert position.expiry == expiry.isoformat()
    assert position.entry_currency == "EUR"


async def test_a_stored_nav_is_reported_with_its_age(server, seeded_store):
    """A budget sized on a months-old NAV was returned with nothing saying so."""
    stale_day = SESSION - timedelta(days=90)
    _seed_nav(seeded_store, as_of=stale_day)
    result, payload = await _call(server, "get_sleeve_status", as_of=SESSION.isoformat())

    assert not result.isError, payload
    assert payload["nav_source"] == "nav_history"
    assert payload["source_as_of"] == stale_day.isoformat()
    assert payload["age_days"] == 90
    assert payload["stale"] is True


async def test_a_supplied_nav_carries_no_staleness(server, seeded_store):
    _seed_nav(seeded_store, as_of=SESSION - timedelta(days=90))
    result, payload = await _call(server, "get_sleeve_status", nav=NAV, as_of=SESSION.isoformat())
    assert not result.isError, payload
    assert payload["nav_source"] == "supplied"
    assert "stale" not in payload


# ── system health ──


def _seed_grid(store, *, as_of: date = SESSION) -> None:
    """A handful of leaves with mixed statuses, plus the measured-cell row."""
    rows = [
        ("us", "broad", "return_63d", "returns", 0.04, "ok", None, None),
        ("us", "broad", "implied_vol_atm", "implied_vol", 0.19, "ok", None, None),
        ("us", "energy", "return_63d", "returns", None, "n/a", "no_bars", "no bars collected"),
        ("japan", "broad", "return_63d", "returns", None, "stale", "stale_bars", "bars are old"),
        (
            "japan",
            "energy",
            "implied_vol_atm",
            "implied_vol",
            None,
            "inapplicable",
            "no_instrument_mapped",
            "no fund is listed there",
        ),
    ]
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO grid (as_of, market, sector, indicator, indicator_family, value, "
            "status, reason_code, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (as_of.isoformat(), market, sector, indicator, family, value, status, code, reason)
                for market, sector, indicator, family, value, status, code, reason in rows
            ],
        )
        connection.execute(
            "INSERT INTO grid_coverage (as_of, market, sector, instrument_kind, is_native_index) "
            "VALUES (?, ?, ?, ?, ?)",
            (as_of.isoformat(), "us", "broad", "native_index", 1),
        )


async def test_system_health_reports_an_empty_store_as_empty(server):
    """The failure this tool exists to prevent, and the one easiest to get wrong.

    A store nothing has ever run against must not read as healthy: there is no
    evidence the pipeline works, which is a different claim from evidence that
    it does.
    """
    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    assert payload["verdict"] == "empty"
    assert "EMPTY" in payload["verdict_note"]
    assert payload["grid"]["as_of"] is None
    assert payload["nav"]["as_of"] is None
    assert payload["ibkr_session"]["last_authenticated_at"] is None
    assert payload["collectors"]["sequences"] == []


async def test_system_health_reports_grid_coverage_from_the_stored_build(server, store):
    _seed_grid(store)
    _seed_nav(store, as_of=SESSION - timedelta(days=3))
    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    grid = payload["grid"]
    assert grid["as_of"] == SESSION.isoformat()
    assert grid["age_days"] == 0
    assert grid["cells_written"] == 5
    assert grid["populated"] == 2
    assert grid["absent"] == 1
    assert grid["stale"] == 1
    assert grid["inapplicable"] == 1
    # populated over COLLECTIBLE, which excludes the inapplicable leaf.
    assert grid["collectible"] == 4
    assert grid["coverage_pct"] == pytest.approx(0.5)
    assert grid["reasons"]["no_bars"] == 1
    assert payload["nav"]["age_days"] == 3


async def test_system_health_carries_a_failing_sequence_and_its_reason(server, store):
    """A session must be able to see WHICH step failed without reading a log."""
    detail = json.dumps(
        {
            "steps": [
                {"step": "nav", "status": "ok", "reason": None},
                {"step": "chain", "status": "error", "reason": "HTTP 401 from /iserver"},
            ],
            "notes": {"daily_bars_fresh": False},
        }
    )
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_sequences (sequence_id, command, session_date, started_at, "
            "finished_at, status, steps_planned, steps_completed, exit_code, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "seq-1",
                "daily",
                date.today().isoformat(),
                "2026-01-15T20:15:00Z",
                "2026-01-15T21:10:00Z",
                "partial",
                6,
                4,
                1,
                detail,
            ),
        )
    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    assert payload["verdict"] == "attention"
    sequence = payload["collectors"]["sequences"][0]
    assert sequence["status"] == "partial"
    assert sequence["steps_planned"] == 6
    assert sequence["steps_completed"] == 4
    failing = [step for step in sequence["steps"] if step["status"] == "error"]
    assert failing[0]["reason"] == "HTTP 401 from /iserver"
    assert any("STALE" in note for note in payload["collectors"]["notes"])


async def test_system_health_dates_the_handshake_without_storing_a_token(server, store):
    """The store records WHEN a run authenticated, never what it authenticated with."""
    from obliskiq.collect.state import record_cp_handshake

    record_cp_handshake(store)
    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    session = payload["ibkr_session"]
    assert session["last_authenticated_at"]
    assert session["within_ttl"] is True
    assert session["age_seconds"] < session["ttl_seconds"]
    # The honest label: oneshot collectors mint a token per run, so this is the
    # last successful HANDSHAKE and the payload must not imply a live session.
    assert "not a live token" in session["note"]

    stored = store.execute(
        "SELECT value FROM collector_state WHERE key = ?", ("ibkr.cp_api.last_handshake",)
    ).fetchone()
    assert "authenticated_at" in stored[0]
    assert "token" not in stored[0].lower()


async def test_system_health_reports_a_vendor_ip_block(server, store):
    from obliskiq.collect.state import UPSTREAM_MARKETDATA_APP, set_cooldown

    set_cooldown(store, UPSTREAM_MARKETDATA_APP, seconds=300, reason="HTTP 403: access denied")
    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    assert payload["vendor"]["cooldown_until"]
    assert "403" in payload["vendor"]["cooldown_reason"]
    assert payload["vendor"]["credits_spent"] == 0


def _seed_run(store, collector, *, started_at, status="ok"):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status, rows_written, "
            "targets_ok, targets_failed) VALUES (?, ?, ?, 100, 10, 0)",
            (collector, started_at, status),
        )


async def test_system_health_will_not_call_a_dead_collector_ok(server, store):
    """The defect this tool exists to catch, and the one easiest to ship.

    `collector_status` reports the newest run per collector with NO window on it,
    so a collector that last ran cleanly three months ago arrives as a tidy `ok`
    row with an empty sequence list beside it — which is exactly what a machine
    whose timer stopped firing looks like. A verdict built from run STATUSES
    alone calls that healthy.
    """
    _seed_grid(store)
    _seed_nav(store)
    _seed_run(store, "chain", started_at="2025-10-15T20:15:00Z", status="ok")

    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    assert payload["verdict"] == "attention"
    assert any("newest chain run" in c and "day(s) old" in c for c in payload["concerns"])
    # The run's own status is reported unchanged: it describes that run, and the
    # concern describes the silence since.
    assert payload["collectors"]["last_run_per_collector"][0]["status"] == "ok"


async def test_collectors_running_with_no_grid_is_not_ok(server, store):
    """`obliskiq-compute` is a separate entrypoint with no tool of its own, so a
    machine can collect nightly and never build a grid. That is not an empty
    store and it is not a healthy one."""
    _seed_nav(store)
    _seed_run(store, "chain", started_at=f"{SESSION.isoformat()}T20:15:00Z")

    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    assert payload["verdict"] == "attention"
    assert payload["grid"]["as_of"] is None
    assert any("no grid has ever been built" in c for c in payload["concerns"])


async def test_a_current_store_reports_ok_with_no_concerns(server, store):
    """The verdict has to be reachable, or it is not a verdict."""
    from obliskiq.collect.state import record_cp_handshake

    _seed_grid(store)
    _seed_nav(store)
    _seed_run(store, "chain", started_at=f"{SESSION.isoformat()}T20:15:00Z")
    record_cp_handshake(store)

    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    assert payload["concerns"] == []
    assert payload["verdict"] == "ok"


async def test_a_store_with_no_recorded_handshake_says_so(server, store):
    _seed_grid(store)
    _seed_nav(store)
    _seed_run(store, "chain", started_at=f"{SESSION.isoformat()}T20:15:00Z")

    result, payload = await _call(server, "get_system_health", as_of=SESSION.isoformat())

    assert not result.isError, payload
    assert payload["verdict"] == "attention"
    assert any("Client Portal handshake" in c for c in payload["concerns"])
