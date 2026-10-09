"""Non-finite and structurally-invalid input at the MCP tool boundary.

The MCP wire genuinely admits this. JSON's own grammar allows the literals
``NaN`` and ``Infinity``, and an overflowing literal like ``-1e999`` decodes to
``-inf`` rather than raising, so a real client can hand a tool call a value
that is not a number at all::

    >>> mcp.types.JSONRPCMessage.model_validate_json(
    ...   '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
    ...   '{"name":"log_exit","arguments":{"realised_pnl":-1e999}}}'
    ... ).root.params["arguments"]
    {'realised_pnl': -inf}

``mcp.shared.memory.create_connected_server_and_client_session`` — the harness
``tests/test_mcp_tools.py`` drives every other tool test through — masks this:
its in-memory client serialiser rewrites ``NaN``/``inf`` to ``null`` on the way
out, so a handler reached through it never sees a non-finite float regardless
of what the caller asked for. That is a property of the test double, not of the
protocol, which is why these tests call the registered tool handler directly
through ``FastMCP``'s own tool manager instead — the same object the real
transport dispatches through, one layer below the client-side JSON round trip.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from obliskiq.server.app import build_server
from obliskiq.store.ledger import open_position

SESSION = date(2026, 1, 15)
NAV = 200_000.0

THESIS = "IV in the bottom decile into a dated regulatory ruling"
INVALIDATION = "spot closes below 88.00 on any session"


@pytest.fixture()
def server(server_context):
    return build_server(server_context)


def _seed_nav(store, *, as_of: date = SESSION, nav: float = NAV) -> None:
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO nav_history (as_of, nav_base, source, recorded_at) VALUES (?, ?, ?, ?)",
            (as_of.isoformat(), nav, "ibkr", "2026-01-15T00:00:00Z"),
        )


async def _call(server, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke the registered handler the way the real transport does.

    Bypasses the in-memory client's JSON round trip on purpose — see the module
    docstring for why that round trip cannot deliver a non-finite float at all.
    """
    return await server._tool_manager.call_tool(name, arguments, context=None, convert_result=False)


# ── A1 / A2: log_exit and a non-finite realised_pnl ──


async def test_log_exit_refuses_a_negative_infinite_loss(server, seeded_store):
    """-inf must not book an infinite penalty and lock the sleeve forever.

    Before the guard: `realised_pnl < 0` is a real comparison against -inf, so
    the option-loss path ran, multiplied it by the penalty multiple, and wrote
    `penalty_balance_after = inf` into the append-only `option_ledger` — a
    balance `docs/DECISIONS.md` deliberately never recomputes.
    """
    _seed_nav(seeded_store)
    position_id = open_position(
        seeded_store,
        contract_id=1,
        opened_at="2026-01-02",
        entry_price=10.0,
        thesis_text=THESIS,
        invalidation_trigger=INVALIDATION,
        is_option=True,
        contracts=1,
        entry_currency="EUR",
    )
    with pytest.raises(ToolError, match="realised_pnl"):
        await _call(
            server,
            "log_exit",
            {
                "position_id": position_id,
                "exit_reason": "target",
                "realised_pnl": float("-inf"),
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM option_ledger").fetchall() == []
    position = seeded_store.execute(
        "SELECT closed_at FROM position_ledger WHERE position_id = ?", (position_id,)
    ).fetchone()
    assert position["closed_at"] is None, "a refused exit must not close the position either"


async def test_log_exit_refuses_a_nan_loss_recorded_as_a_gain(server, seeded_store):
    """NaN satisfies neither `< 0` nor `<= 0`, so it fell through to the GAIN path.

    A real loss of unknown size was recorded as `gain_realised` with no penalty
    booked — an unknown amount of work reported as success in the other
    direction from A1.
    """
    _seed_nav(seeded_store)
    position_id = open_position(
        seeded_store,
        contract_id=1,
        opened_at="2026-01-02",
        entry_price=10.0,
        thesis_text=THESIS,
        invalidation_trigger=INVALIDATION,
        is_option=True,
        contracts=1,
        entry_currency="EUR",
    )
    with pytest.raises(ToolError, match="realised_pnl"):
        await _call(
            server,
            "log_exit",
            {
                "position_id": position_id,
                "exit_reason": "target",
                "realised_pnl": float("nan"),
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM option_ledger").fetchall() == []


async def test_log_exit_refuses_a_non_finite_proceeds(server, seeded_store):
    _seed_nav(seeded_store)
    position_id = open_position(
        seeded_store,
        contract_id=1,
        opened_at="2026-01-02",
        entry_price=10.0,
        thesis_text=THESIS,
        invalidation_trigger=INVALIDATION,
        is_option=True,
        contracts=1,
        entry_currency="EUR",
    )
    with pytest.raises(ToolError, match="proceeds"):
        await _call(
            server,
            "log_exit",
            {
                "position_id": position_id,
                "exit_reason": "target",
                "realised_pnl": -5.0,
                "proceeds": float("inf"),
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM option_ledger").fetchall() == []


async def test_log_exit_accepts_a_finite_loss_unchanged(server, seeded_store):
    """The guard must not touch the ordinary path it sits in front of."""
    _seed_nav(seeded_store)
    position_id = open_position(
        seeded_store,
        contract_id=1,
        opened_at="2026-01-02",
        entry_price=10.0,
        thesis_text=THESIS,
        invalidation_trigger=INVALIDATION,
        is_option=True,
        contracts=1,
        entry_currency="EUR",
    )
    out = await _call(
        server,
        "log_exit",
        {
            "position_id": position_id,
            "exit_reason": "target",
            "realised_pnl": -50.0,
            "as_of": SESSION.isoformat(),
        },
    )
    assert out["settlement"].startswith("option loss reconciled")
    row = seeded_store.execute("SELECT realised_pnl FROM option_ledger").fetchone()
    assert row["realised_pnl"] == -50.0


# ── A3: log_entry and an infinite entry_price ──


async def test_log_entry_refuses_an_infinite_price(server, seeded_store):
    """The equity path has no affordability ceiling to catch this — only the

    option path is protected today, because the sleeve's affordability gate
    refuses a premium it cannot afford at any size. An equity purchase has no
    such ceiling, so `entry_price = inf` reached `position_ledger` and priced
    every later stop, zombie test and portfolio review off it.
    """
    _seed_nav(seeded_store)
    with pytest.raises(ToolError, match="entry_price"):
        await _call(
            server,
            "log_entry",
            {
                "contract_id": 1,
                "entry_price": float("inf"),
                "thesis_text": THESIS,
                "invalidation_trigger": INVALIDATION,
                "is_option": False,
                "quantity": 1.0,
                "entry_range_pctile": 0.2,
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM position_ledger").fetchall() == []


async def test_log_entry_refuses_a_nan_price_with_a_gate_reason(server, seeded_store):
    """Before the fix, NaN passed `entry_price <= 0` (both comparisons are
    False against NaN) and reached SQLite's own NOT NULL constraint — a raw
    driver message, naming no gate, rather than a refusal a caller can act
    on. The message must be the gate's own reason, not the constraint's."""
    _seed_nav(seeded_store)
    with pytest.raises(ToolError, match="entry_price") as excinfo:
        await _call(
            server,
            "log_entry",
            {
                "contract_id": 1,
                "entry_price": float("nan"),
                "thesis_text": THESIS,
                "invalidation_trigger": INVALIDATION,
                "is_option": False,
                "quantity": 1.0,
                "entry_range_pctile": 0.2,
                "as_of": SESSION.isoformat(),
            },
        )
    assert "finite" in str(excinfo.value)
    assert "NOT NULL constraint" not in str(excinfo.value)
    assert seeded_store.execute("SELECT * FROM position_ledger").fetchall() == []


# ── A4: upsert_external_position and infinite quantity / market value ──


async def test_upsert_external_position_refuses_infinite_quantity(server, store):
    with pytest.raises(ToolError, match="quantity"):
        await _call(
            server,
            "upsert_external_position",
            {
                "broker": "DEGIRO",
                "symbol": "TEST",
                "quantity": float("inf"),
                "currency": "EUR",
                "market_value_base": 1_000.0,
                "as_of": SESSION.isoformat(),
            },
        )
    assert store.execute("SELECT * FROM external_positions").fetchall() == []


async def test_upsert_external_position_refuses_infinite_market_value(server, store):
    with pytest.raises(ToolError, match="market_value_base"):
        await _call(
            server,
            "upsert_external_position",
            {
                "broker": "DEGIRO",
                "symbol": "TEST",
                "quantity": 10.0,
                "currency": "EUR",
                "market_value_base": float("-inf"),
                "as_of": SESSION.isoformat(),
            },
        )
    assert store.execute("SELECT * FROM external_positions").fetchall() == []


# ── A5: record_iv_percentile's unchecked companion fields ──


async def test_record_iv_percentile_refuses_an_implausible_implied_vol(server, seeded_store):
    """The collector's own decoder drops this magnitude with a warning
    (ibkr/cp_client.py's _reject_implausible); this tool's separate write path
    had no equivalent, so a caller could land the same unit error in the same
    column the collector was built to keep it out of."""
    with pytest.raises(ToolError, match="implied_vol"):
        await _call(
            server,
            "record_iv_percentile",
            {
                "contract_id": 1,
                "iv_percentile_52w": 0.5,
                "implied_vol": float("inf"),
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM underlying_snapshots").fetchall() == []


async def test_record_iv_percentile_refuses_ibkrs_sentinel_on_historical_vol(server, seeded_store):
    """-1.0 is IBKR's own documented 'no value here' sentinel (references/
    ibkr-playbook.md), inside the plausible-magnitude range this tool never
    checked before."""
    with pytest.raises(ToolError, match="historical_vol_30d"):
        await _call(
            server,
            "record_iv_percentile",
            {
                "contract_id": 1,
                "iv_percentile_52w": 0.5,
                "historical_vol_30d": -1.0,
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM underlying_snapshots").fetchall() == []


async def test_record_iv_percentile_refuses_a_negative_price(server, seeded_store):
    with pytest.raises(ToolError, match="last"):
        await _call(
            server,
            "record_iv_percentile",
            {
                "contract_id": 1,
                "iv_percentile_52w": 0.5,
                "last": -42.0,
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM underlying_snapshots").fetchall() == []


async def test_record_iv_percentile_accepts_plausible_companions_unchanged(server, seeded_store):
    out = await _call(
        server,
        "record_iv_percentile",
        {
            "contract_id": 1,
            "iv_percentile_52w": 0.5,
            "implied_vol": 0.32,
            "historical_vol_30d": 0.28,
            "last": 142.5,
            "as_of": SESSION.isoformat(),
        },
    )
    assert sorted(out["fields_written"]) == sorted(
        ["historical_vol_30d", "implied_vol", "iv_percentile_52w", "iv_percentile_route", "last"]
    )


# ── A6: a crossed book at the tool boundary ──


async def test_log_entry_refuses_a_crossed_spread(server, seeded_store):
    """ask < bid must fail the liquidity gate outright, not read as untested
    (which would not block the write, per GateResult.passed) and not read as
    a spread tighter than any real quote on the board (which is what the
    signed fraction did before this fix)."""
    _seed_nav(seeded_store)
    with pytest.raises(ToolError, match=re.compile("crossed", re.IGNORECASE)):
        await _call(
            server,
            "log_entry",
            {
                "contract_id": 1,
                "entry_price": 2.0,
                "thesis_text": THESIS,
                "invalidation_trigger": INVALIDATION,
                "is_option": True,
                "contracts": 1,
                "strike": 100.0,
                "is_call": True,
                "target": 300.0,
                "entry_range_pctile": 0.2,
                "expiry": "2026-05-15",
                "mandatory_close_date": "2026-05-01",
                "expected_resolution_days": 60,
                "spread_pct_of_mid": -5.0,
                "open_interest": 1000,
                "volume": 100,
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM position_ledger").fetchall() == []


async def test_a_crossed_book_is_not_rescued_by_the_depth_waiver(server, seeded_store, config):
    """liquidity.spread_waiver_min_open_interest waives a WIDE spread on a deep
    book. It must not waive a CROSSED one: depth does not make a broken quote
    tradeable."""
    waiver_oi = config.require_int("liquidity.spread_waiver_min_open_interest")
    _seed_nav(seeded_store)
    with pytest.raises(ToolError, match=re.compile("crossed", re.IGNORECASE)):
        await _call(
            server,
            "log_entry",
            {
                "contract_id": 1,
                "entry_price": 2.0,
                "thesis_text": THESIS,
                "invalidation_trigger": INVALIDATION,
                "is_option": True,
                "contracts": 1,
                "strike": 100.0,
                "is_call": True,
                "target": 300.0,
                "entry_range_pctile": 0.2,
                "expiry": "2026-05-15",
                "mandatory_close_date": "2026-05-01",
                "expected_resolution_days": 60,
                "spread_pct_of_mid": -0.5,
                "open_interest": waiver_oi + 1_000,
                "volume": 100,
                "as_of": SESSION.isoformat(),
            },
        )


# ── A11: log_entry and a future-dated opened_at ──


async def test_log_entry_refuses_an_opened_at_after_as_of(server, seeded_store):
    """Days-held, the zombie test's two-session rule, the time window and the
    mandatory close are all measured from opened_at. A session that has not
    happened cannot be the date any of them started from."""
    _seed_nav(seeded_store)
    with pytest.raises(ToolError, match="opened_at"):
        await _call(
            server,
            "log_entry",
            {
                "contract_id": 1,
                "entry_price": 2.0,
                "thesis_text": THESIS,
                "invalidation_trigger": INVALIDATION,
                "is_option": False,
                "quantity": 1.0,
                "entry_range_pctile": 0.2,
                "opened_at": "9999-01-01",
                "as_of": SESSION.isoformat(),
            },
        )
    assert seeded_store.execute("SELECT * FROM position_ledger").fetchall() == []


async def test_log_entry_accepts_opened_at_equal_to_as_of(server, seeded_store):
    """The boundary itself — opened today — must not be refused."""
    _seed_nav(seeded_store)
    out = await _call(
        server,
        "log_entry",
        {
            "contract_id": 1,
            "entry_price": 2.0,
            "thesis_text": THESIS,
            "invalidation_trigger": INVALIDATION,
            "is_option": False,
            "quantity": 1.0,
            "entry_range_pctile": 0.2,
            "opened_at": SESSION.isoformat(),
            "as_of": SESSION.isoformat(),
        },
    )
    assert out["position_id"] == 1


# ── A12: the store path must not reach the client ──


def test_a_transaction_failure_does_not_leak_the_database_path(seeded_store):
    """A raw sqlite failure inside Store.transaction() must not carry the
    absolute path on disk into a message that reaches an MCP caller.

    Provoked directly against Store.transaction() rather than through a gate,
    because A3 now catches the one reachable case (a non-finite entry_price)
    before store/ledger.py ever opens a transaction — which is the fix working,
    not a reason this guard is untestable. The path-leak is a property of
    Store.transaction() itself and belongs to every caller of it, not only to
    the one this session happened to find first.
    """
    from obliskiq.errors import StoreError

    with pytest.raises(StoreError) as excinfo, seeded_store.transaction() as connection:
        connection.execute("INSERT INTO no_such_table (x) VALUES (1)")
    assert str(seeded_store.path) not in str(excinfo.value)
