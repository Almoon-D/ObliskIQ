"""Which dividend yield priced the position, and how the reader is told.

``universe.dividend_yield`` was DECLARED and never collected. It is collected now,
into a dated table, and the two facts coexist: the declaration says which
instruments need a yield, the store says what one was measured at on a session.

The failure this guards is the one migration 009 named and this change could
reintroduce: a yield nobody supplied reading as a measured zero. q enters d1 as
``(r - q + sigma^2/2) * tau``, and ``zombie_test`` compares today's N(d2) against
the one recorded at entry, so the bias sits directly on the exit gate.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.compute.frames import collected_dividend_yield
from obliskiq.server.tools.risk import (
    DIVIDEND_FROM_CALLER,
    DIVIDEND_FROM_COLLECTED,
    DIVIDEND_FROM_UNIVERSE,
    DIVIDEND_NONE_DECLARED,
    _dividend_yield,
)

AS_OF = date(2026, 9, 9)
CONTRACT = 1


@pytest.fixture()
def store_with_universe(store):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, sector) "
            "VALUES (?, 'TEST', 'USD', 'ETF', 'us', 'broad')",
            (CONTRACT,),
        )
    return store


def _collect(store, *, on: date, value: float, basis: str | None = "trailing_12m"):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, dividend_yield, "
            "dividend_yield_basis, source) VALUES (?, ?, ?, ?, 'cp_api')",
            (on.isoformat(), CONTRACT, value, basis),
        )


# ── precedence ──


def test_the_caller_outranks_everything(store_with_universe):
    _collect(store_with_universe, on=AS_OF, value=0.0098)
    value, source, _ = _dividend_yield(
        {"dividend_yield": 0.02},
        0.031,
        store=store_with_universe,
        contract_id=CONTRACT,
        as_of=AS_OF,
    )
    assert (value, source) == (0.031, DIVIDEND_FROM_CALLER)


def test_a_collected_yield_outranks_the_declaration(store_with_universe):
    """Dated and measured beats typed into a file at some past point."""
    _collect(store_with_universe, on=AS_OF, value=0.0098)
    value, source, note = _dividend_yield(
        {"dividend_yield": 0.02},
        None,
        store=store_with_universe,
        contract_id=CONTRACT,
        as_of=AS_OF,
    )
    assert (value, source) == (0.0098, DIVIDEND_FROM_COLLECTED)
    assert "2026-09-09" in note
    # The approximation is stated, never left for the reader to assume.
    assert "TRAILING" in note and "approximation" in note


def test_the_declaration_still_answers_when_nothing_was_collected(store_with_universe):
    value, source, note = _dividend_yield(
        {"dividend_yield": 0.02, "dividend_yield_as_of": "2026-01-31"},
        None,
        store=store_with_universe,
        contract_id=CONTRACT,
        as_of=AS_OF,
    )
    assert (value, source) == (0.02, DIVIDEND_FROM_UNIVERSE)
    assert "2026-01-31" in note


def test_nothing_anywhere_is_the_zero_dividend_case_and_says_so(store_with_universe):
    value, source, note = _dividend_yield(
        {}, None, store=store_with_universe, contract_id=CONTRACT, as_of=AS_OF
    )
    assert (value, source) == (0.0, DIVIDEND_NONE_DECLARED)
    assert "ZERO-DIVIDEND case rather than a measured zero" in note


# ── point-in-time ──


def test_a_yield_collected_after_the_session_is_not_used(store_with_universe):
    """A position assessed for a past session must not be discounted at a yield
    collected after it — the reason the collected figure went in a dated table
    rather than onto the current-state ``universe`` row."""
    _collect(store_with_universe, on=AS_OF + timedelta(days=3), value=0.05)
    value, source, _ = _dividend_yield(
        {"dividend_yield": 0.02},
        None,
        store=store_with_universe,
        contract_id=CONTRACT,
        as_of=AS_OF,
    )
    assert (value, source) == (0.02, DIVIDEND_FROM_UNIVERSE)


def test_the_most_recent_yield_known_by_the_session_wins(store_with_universe):
    _collect(store_with_universe, on=AS_OF - timedelta(days=30), value=0.011)
    _collect(store_with_universe, on=AS_OF - timedelta(days=1), value=0.0098)
    _collect(store_with_universe, on=AS_OF + timedelta(days=1), value=0.05)
    found = collected_dividend_yield(store_with_universe, CONTRACT, as_of=AS_OF)
    assert found is not None
    assert found[0] == pytest.approx(0.0098)
    assert found[1] == AS_OF - timedelta(days=1)
    assert found[2] == "trailing_12m"


# ── a collected zero is a reading ──


def test_a_collected_zero_is_used_rather_than_falling_through(store_with_universe):
    """A measured non-payer is evidence. Falling through to the declaration or to
    the none-declared branch would replace a measurement with a guess, and the
    none-declared note makes the OPPOSITE claim about the same instrument."""
    _collect(store_with_universe, on=AS_OF, value=0.0)
    value, source, _ = _dividend_yield(
        {"dividend_yield": 0.02},
        None,
        store=store_with_universe,
        contract_id=CONTRACT,
        as_of=AS_OF,
    )
    assert (value, source) == (0.0, DIVIDEND_FROM_COLLECTED)


def test_an_absent_store_falls_back_rather_than_failing(store_with_universe):
    """The resolver is called from paths that hold no store. Absence of a store is
    absence of a collected yield, never an error."""
    value, source, _ = _dividend_yield({"dividend_yield": 0.02}, None)
    assert (value, source) == (0.02, DIVIDEND_FROM_UNIVERSE)
