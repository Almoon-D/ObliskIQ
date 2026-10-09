"""Ledger writes, and the gates enforced in the store rather than upstream.

The skill must never be the only thing standing between a bad trade and the
ledger. These are the refusals a Session 4 MCP tool will inherit unchanged.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date

import pytest

from obliskiq.store.ledger import (
    LedgerError,
    append_reunderwrite,
    close_position,
    get_position,
    has_new_dated_catalyst,
    open_position,
    open_positions,
    positions_closed_for,
    thesis_is_frozen,
)

THESIS = "IV in the bottom decile into a dated regulatory ruling"
INVALIDATION = "spot closes below 88.00 on any session"


@pytest.fixture()
def position(seeded_store, config):
    return open_position(
        seeded_store,
        contract_id=1,
        opened_at="2026-01-15",
        entry_price=2.50,
        thesis_text=THESIS,
        invalidation_trigger=INVALIDATION,
        target=12.0,
        prob_target_at_entry=0.22,
    )


# ── entry ──


def test_an_entry_round_trips_verbatim(seeded_store, position):
    """The thesis is restated from the ledger, never reconstructed from price."""
    row = get_position(seeded_store, position)
    assert row.thesis_text == THESIS
    assert row.invalidation_trigger == INVALIDATION
    assert row.prob_target_at_entry == 0.22
    assert row.is_open
    assert open_positions(seeded_store) == [row]


def test_an_entry_without_an_invalidation_trigger_is_refused(seeded_store, config):
    """A thesis with no falsifier is a position nobody can ever be wrong about."""
    with pytest.raises(LedgerError, match="invalidation_trigger"):
        open_position(
            seeded_store,
            contract_id=1,
            opened_at="2026-01-15",
            entry_price=2.5,
            thesis_text=THESIS,
            invalidation_trigger="   ",
        )


def test_an_entry_without_a_thesis_is_refused(seeded_store, config):
    with pytest.raises(LedgerError, match="thesis_text"):
        open_position(
            seeded_store,
            contract_id=1,
            opened_at="2026-01-15",
            entry_price=2.5,
            thesis_text="",
            invalidation_trigger=INVALIDATION,
        )


# ── the frozen thesis ──


def test_thesis_text_cannot_be_updated_in_place(seeded_store, position):
    """Enforced by the store, not by a docstring. Changes append instead."""
    assert thesis_is_frozen(seeded_store, position, "actually it was always a vol trade")
    assert get_position(seeded_store, position).thesis_text == THESIS


def test_the_ledger_module_offers_no_way_to_edit_a_thesis():
    """A function that tried would be a function someone could call."""
    import obliskiq.store.ledger as module

    editors = [
        name
        for name in dir(module)
        if name.startswith(("update_thesis", "set_thesis", "edit_thesis"))
    ]
    assert not editors, f"the ledger exposes thesis editors: {editors}"


def test_a_reunderwrite_appends_a_dated_entry(seeded_store, position):
    entries = append_reunderwrite(
        seeded_store,
        position,
        dated_at="2026-03-01",
        catalyst="antitrust ruling",
        catalyst_date="2026-05-14",
        note="new information, not a lower price",
    )
    assert len(entries) == 1
    assert entries[0]["catalyst_date"] == "2026-05-14"
    # And the original thesis is untouched.
    assert get_position(seeded_store, position).thesis_text == THESIS


def test_a_reunderwrite_needs_a_real_date(seeded_store, position):
    with pytest.raises(LedgerError, match="NEW DATED catalyst"):
        append_reunderwrite(
            seeded_store,
            position,
            dated_at="2026-03-01",
            catalyst="things look better",
            catalyst_date="soon",
        )


def test_a_reunderwrite_catalyst_predating_entry_is_refused(seeded_store, position):
    """A catalyst that existed at entry is part of the original thesis."""
    with pytest.raises(LedgerError, match=r"already existed|not after"):
        append_reunderwrite(
            seeded_store,
            position,
            dated_at="2026-03-01",
            catalyst="the same product launch",
            catalyst_date="2026-01-02",
        )


def test_a_passed_catalyst_no_longer_counts_as_new(seeded_store, position):
    """If the catalyst passed without the move, that is an invalidation."""
    append_reunderwrite(
        seeded_store,
        position,
        dated_at="2026-02-01",
        catalyst="antitrust ruling",
        catalyst_date="2026-03-14",
    )
    row = get_position(seeded_store, position)
    assert has_new_dated_catalyst(row, as_of=date(2026, 3, 1))
    assert not has_new_dated_catalyst(row, as_of=date(2026, 4, 1))


# ── exit ──


def test_an_exit_reason_outside_the_config_list_is_refused(seeded_store, config, position):
    allowed = config.require_list("discipline.exit_reasons_allowed")
    assert "it_was_red" not in allowed
    with pytest.raises(LedgerError, match="exit_reasons_allowed"):
        close_position(
            seeded_store, config, position, closed_at="2026-04-01", exit_reason="it_was_red"
        )


def test_every_configured_exit_reason_is_accepted(seeded_store, config):
    for reason in config.require_list("discipline.exit_reasons_allowed"):
        new_position = open_position(
            seeded_store,
            contract_id=1,
            opened_at="2026-01-15",
            entry_price=2.5,
            thesis_text=THESIS,
            invalidation_trigger=INVALIDATION,
        )
        close_position(
            seeded_store,
            config,
            new_position,
            closed_at="2026-04-01",
            exit_reason=str(reason),
            realised_pnl=100.0,
        )


def test_a_position_cannot_be_closed_twice(seeded_store, config, position):
    close_position(seeded_store, config, position, closed_at="2026-04-01", exit_reason="target")
    with pytest.raises(LedgerError, match="already closed"):
        close_position(
            seeded_store, config, position, closed_at="2026-05-01", exit_reason="override"
        )


def test_qualifying_exits_can_be_selected_by_reason(seeded_store, config, position):
    close_position(
        seeded_store,
        config,
        position,
        closed_at="2026-04-01",
        exit_reason="target",
        realised_pnl=900.0,
    )
    qualifying = [str(r) for r in config.require_list("option_sleeve.qualifying_exit_reasons")]
    found = positions_closed_for(seeded_store, qualifying)
    assert [p.position_id for p in found] == [position]
    assert positions_closed_for(seeded_store, ["override"]) == []
