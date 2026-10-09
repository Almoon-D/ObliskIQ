"""The penalty balance — the most heavily specified rule in the brief.

Loss adds 2x. A qualifying gain reduces 1:1. An override gain does not. The
budget floors at zero. The penalty is booked at DETECTION, and a later close
adjusts by the delta only rather than counting the same loss twice.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import pytest

from obliskiq.compute.penalty import PenaltyError, PenaltyLedger
from obliskiq.store.ledger import (
    EVENT_GAIN_REALISED,
    EVENT_LOSS_CLOSED,
    EVENT_LOSS_DETECTED,
    EVENT_PREMIUM_COMMITTED,
    close_position,
    open_position,
)

NAV = 200_000.0


@pytest.fixture()
def ledger(seeded_store, config):
    return PenaltyLedger(seeded_store, config)


def _open(store, config, *, thesis="long vol into a dated catalyst"):
    """A real position row, so option_ledger's foreign key resolves."""
    return open_position(
        store,
        contract_id=1,
        opened_at="2026-01-15",
        entry_price=2.50,
        thesis_text=thesis,
        invalidation_trigger="spot closes below 88.00",
    )


@pytest.fixture()
def position_id(seeded_store, config):
    return _open(seeded_store, config)


@pytest.fixture()
def second_position_id(seeded_store, config):
    return _open(seeded_store, config, thesis="a second, unrelated position")


def test_an_empty_ledger_gives_the_full_sleeve(config, ledger):
    budget_pct = config.require_float("option_sleeve.budget_pct")
    status = ledger.status(NAV)
    assert status.penalty_balance == 0.0
    assert status.committed_premium == 0.0
    assert status.available_budget == pytest.approx(budget_pct * NAV)
    assert status.gains_required == 0.0
    assert not status.is_locked


# ── the sleeve budget is a shared pool ──


def test_premium_committed_by_an_open_position_is_subtracted(config, ledger, position_id):
    """The subtraction that makes budget_pct a cap rather than an allowance."""
    budget_pct = config.require_float("option_sleeve.budget_pct")
    event = ledger.book_premium_committed(premium_paid=2_500.0, position_id=position_id)

    assert event.event_type == EVENT_PREMIUM_COMMITTED
    assert event.penalty_added == 0.0, "a commitment is not a penalty"
    assert ledger.balance() == 0.0, "and it must not move the balance"

    status = ledger.status(NAV)
    assert status.committed_premium == pytest.approx(2_500.0)
    assert status.available_budget == pytest.approx(budget_pct * NAV - 2_500.0)


def test_concurrent_positions_cannot_each_be_sized_against_the_whole_sleeve(
    config, ledger, position_id, second_position_id
):
    """The defect, stated as arithmetic.

    Two positions each committing most of the sleeve must leave the second
    entrant with the REMAINDER, not with the sleeve again. Before the pool
    existed, max_concurrent_positions entries could each spend it in full.
    """
    budget_pct = config.require_float("option_sleeve.budget_pct")
    gross = budget_pct * NAV
    slots = config.require_int("option_sleeve.max_concurrent_positions")
    assert slots > 1, "with one slot the pool and the old formula agree"

    ledger.book_premium_committed(premium_paid=gross * 0.6, position_id=position_id)
    ledger.book_premium_committed(premium_paid=gross * 0.3, position_id=second_position_id)

    status = ledger.status(NAV)
    assert status.committed_premium == pytest.approx(gross * 0.9)
    assert status.available_budget == pytest.approx(gross * 0.1)


def test_unspent_budget_stays_unspent(config, ledger, position_id):
    """It is never redistributed across the remaining concurrency slots."""
    budget_pct = config.require_float("option_sleeve.budget_pct")
    gross = budget_pct * NAV
    slots = config.require_int("option_sleeve.max_concurrent_positions")

    ledger.book_premium_committed(premium_paid=gross * 0.5, position_id=position_id)
    available = ledger.status(NAV).available_budget

    assert available == pytest.approx(gross * 0.5)
    assert available != pytest.approx(gross * 0.5 / (slots - 1))


def test_a_commitment_is_released_when_the_position_closes(
    config, seeded_store, ledger, position_id
):
    """No reversal event: closing the position IS the release."""
    budget_pct = config.require_float("option_sleeve.budget_pct")
    ledger.book_premium_committed(premium_paid=3_000.0, position_id=position_id)
    assert ledger.committed_premium() == pytest.approx(3_000.0)

    close_position(
        seeded_store,
        config,
        position_id,
        closed_at="2026-02-01",
        exit_reason="target",
        realised_pnl=100.0,
    )

    assert ledger.committed_premium() == 0.0
    assert ledger.status(NAV).available_budget == pytest.approx(budget_pct * NAV)


def test_the_penalty_and_the_commitment_both_bite(config, ledger, position_id):
    budget_pct = config.require_float("option_sleeve.budget_pct")
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")

    ledger.book_premium_committed(premium_paid=1_000.0, position_id=position_id)
    ledger.book_loss_detected(expected_loss=500.0)

    status = ledger.status(NAV)
    assert status.available_budget == pytest.approx(budget_pct * NAV - multiple * 500.0 - 1_000.0)


def test_a_commitment_needs_a_position_and_a_positive_premium(ledger, position_id):
    """A commitment with no position would never be released."""
    with pytest.raises(PenaltyError, match="positive amount"):
        ledger.book_premium_committed(premium_paid=0.0, position_id=position_id)
    with pytest.raises(TypeError):
        ledger.book_premium_committed(premium_paid=100.0)  # type: ignore[call-arg]


def test_commitments_do_not_disturb_the_penalty_audit_trail(ledger, position_id):
    ledger.book_premium_committed(premium_paid=1_000.0, position_id=position_id)
    ledger.book_loss_detected(expected_loss=400.0, position_id=position_id)
    ledger.book_premium_committed(premium_paid=250.0, position_id=position_id)
    assert ledger.verify() == []


def test_a_loss_adds_the_configured_multiple(config, ledger):
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")
    ledger.book_loss_detected(expected_loss=1_000.0, position_id=None)

    assert ledger.balance() == pytest.approx(multiple * 1_000.0)
    status = ledger.status(NAV)
    assert status.available_budget == pytest.approx(
        config.require_float("option_sleeve.budget_pct") * NAV - multiple * 1_000.0
    )
    assert status.gains_required == pytest.approx(multiple * 1_000.0)


def test_a_qualifying_gain_reduces_the_balance_one_for_one(config, ledger):
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")
    qualifying = config.require_list("option_sleeve.qualifying_exit_reasons")

    ledger.book_loss_detected(expected_loss=1_000.0)
    ledger.book_qualifying_gain(realised_pnl=600.0, exit_reason=str(qualifying[0]))

    assert ledger.balance() == pytest.approx(multiple * 1_000.0 - 600.0)


def test_every_configured_qualifying_reason_is_accepted(config, ledger):
    ledger.book_loss_detected(expected_loss=5_000.0)
    for reason in config.require_list("option_sleeve.qualifying_exit_reasons"):
        ledger.book_qualifying_gain(realised_pnl=100.0, exit_reason=str(reason))


def test_an_override_gain_does_not_reduce_the_balance(config, ledger):
    """Otherwise the sleeve unlocks by cutting winners early to bank profit."""
    assert "override" not in config.require_list("option_sleeve.qualifying_exit_reasons")
    ledger.book_loss_detected(expected_loss=1_000.0)
    before = ledger.balance()

    with pytest.raises(PenaltyError, match="override"):
        ledger.book_qualifying_gain(realised_pnl=5_000.0, exit_reason="override")

    assert ledger.balance() == before


def test_an_option_win_does_not_reduce_the_balance_either(config, ledger):
    """Recovery is measured on NON-OPTION realised gains, per the brief."""
    assert config.require_str("option_sleeve.recovery_basis") == "realised_gains"
    ledger.book_loss_detected(expected_loss=1_000.0)
    before = ledger.balance()

    event = ledger.record_option_gain(realised_pnl=4_000.0)

    assert event.event_type == EVENT_GAIN_REALISED
    assert event.penalty_added == 0.0
    assert ledger.balance() == before


def test_the_balance_floors_at_zero(ledger):
    """Banking gains beyond the penalty does not buy credit against a future loss."""
    ledger.book_loss_detected(expected_loss=100.0)
    ledger.book_qualifying_gain(realised_pnl=10_000.0, exit_reason="target")
    assert ledger.balance() == 0.0

    ledger.book_loss_detected(expected_loss=50.0)
    assert ledger.balance() == pytest.approx(100.0)


def test_the_budget_floors_at_zero_when_the_penalty_exceeds_the_sleeve(config, ledger):
    ledger.book_loss_detected(expected_loss=NAV)
    status = ledger.status(NAV)
    assert status.penalty_balance > status.gross_budget
    assert status.available_budget == 0.0
    assert status.is_locked


def test_the_penalty_is_booked_at_detection_not_at_close(config, ledger, position_id):
    """Booking at close would reward holding a dead position to postpone the cost."""
    assert config.require_str("zombie_test.book_penalty_at") == "detection"
    assert ledger.books_at_detection

    detection = ledger.book_loss_detected(expected_loss=1_000.0, position_id=position_id)

    assert detection.event_type == EVENT_LOSS_DETECTED
    assert ledger.balance() > 0.0, "the penalty applies at detection, before any close"


def test_a_later_close_at_the_detected_figure_does_not_double_count(config, ledger, position_id):
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")

    ledger.book_loss_detected(expected_loss=1_000.0, position_id=position_id)
    after_detection = ledger.balance()

    closed = ledger.reconcile_loss_closed(actual_loss=1_000.0, position_id=position_id)

    assert closed.event_type == EVENT_LOSS_CLOSED
    assert closed.penalty_added == pytest.approx(0.0)
    assert ledger.balance() == pytest.approx(after_detection)
    assert ledger.balance() == pytest.approx(multiple * 1_000.0)


def test_a_close_worse_than_detected_adjusts_by_the_delta_only(config, ledger, position_id):
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")

    ledger.book_loss_detected(expected_loss=1_000.0, position_id=position_id)
    ledger.reconcile_loss_closed(actual_loss=1_400.0, position_id=position_id)

    assert ledger.balance() == pytest.approx(multiple * 1_400.0)


def test_a_close_better_than_detected_gives_the_difference_back(config, ledger, position_id):
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")

    ledger.book_loss_detected(expected_loss=1_000.0, position_id=position_id)
    reconciliation = ledger.reconcile_loss_closed(actual_loss=600.0, position_id=position_id)

    assert reconciliation.penalty_added < 0
    assert ledger.balance() == pytest.approx(multiple * 600.0)


def test_a_close_with_no_prior_detection_books_the_whole_penalty(config, ledger, position_id):
    """An invalidation exit that was never flagged as a zombie still costs 2x."""
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")
    ledger.reconcile_loss_closed(actual_loss=800.0, position_id=position_id)
    assert ledger.balance() == pytest.approx(multiple * 800.0)


def test_two_positions_reconcile_independently(config, ledger, position_id, second_position_id):
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")
    ledger.book_loss_detected(expected_loss=1_000.0, position_id=position_id)
    ledger.book_loss_detected(expected_loss=500.0, position_id=second_position_id)

    ledger.reconcile_loss_closed(actual_loss=1_000.0, position_id=position_id)

    assert ledger.balance() == pytest.approx(multiple * 1_500.0)


def test_the_running_balance_column_matches_a_replay(ledger, position_id):
    """penalty_balance_after is the audit trail; verify() is the audit."""
    ledger.book_loss_detected(expected_loss=1_000.0, position_id=position_id)
    ledger.book_qualifying_gain(realised_pnl=400.0, exit_reason="target")
    ledger.reconcile_loss_closed(actual_loss=1_200.0, position_id=position_id)
    assert ledger.verify() == []


def test_a_negative_loss_is_refused(ledger):
    with pytest.raises(PenaltyError, match="positive magnitude"):
        ledger.book_loss_detected(expected_loss=-100.0)
    with pytest.raises(PenaltyError, match="positive magnitude"):
        ledger.reconcile_loss_closed(actual_loss=-100.0)


def test_a_non_positive_qualifying_gain_is_refused(ledger):
    with pytest.raises(PenaltyError, match="must be positive"):
        ledger.book_qualifying_gain(realised_pnl=-100.0, exit_reason="target")


def test_a_nav_recovery_basis_is_refused_outright(config, seeded_store):
    """NAV measures market beta, not decisions. It can never be the basis."""
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["option_sleeve"]["recovery_basis"] = "nav"
    ledger = PenaltyLedger(seeded_store, Config(data, config.source))

    with pytest.raises(PenaltyError, match="market beta"):
        ledger.book_qualifying_gain(realised_pnl=1_000.0, exit_reason="target")


def test_the_two_readers_of_available_budget_agree(config, seeded_store, ledger, position_id):
    """One number, computed in two places, must never drift apart.

    PenaltyLedger.status is what the session reports and
    CandidateGates.affordability is what the gates enforce. A rule enforced
    twice is a rule that will eventually be enforced two different ways, and
    here the two ways would be "what you were told you could spend" and "what
    the server will actually let you spend".
    """
    from obliskiq.compute.screen import OptionScreen

    ledger.book_loss_detected(expected_loss=800.0)
    ledger.book_premium_committed(premium_paid=1_500.0, position_id=position_id)

    reported = ledger.status(NAV).available_budget
    enforced = OptionScreen(seeded_store, config).affordability(NAV).available_budget

    assert reported == pytest.approx(enforced)


def test_a_locked_sleeve_says_which_thing_locked_it(config, ledger, position_id):
    """Penalty and commitments both empty the budget, and clear differently.

    A sleeve shut by the penalty reopens only on realised qualifying gains. One
    shut by commitments reopens when a position closes. Reporting both as
    "locked" would send a session looking for the wrong remedy.
    """
    budget_pct = config.require_float("option_sleeve.budget_pct")
    gross = budget_pct * NAV

    assert ledger.status(NAV).locked_by is None

    ledger.book_premium_committed(premium_paid=gross, position_id=position_id)
    status = ledger.status(NAV)
    assert status.is_locked
    assert status.locked_by == "commitments"
    assert status.gains_required == 0.0, "there is no penalty to earn back here"

    ledger.book_loss_detected(expected_loss=100.0)
    assert ledger.status(NAV).locked_by == "penalty_and_commitments"


def test_a_penalty_lock_is_named_apart_from_a_commitment_lock(config, ledger):
    ledger.book_loss_detected(expected_loss=NAV)
    status = ledger.status(NAV)
    assert status.is_locked
    assert status.locked_by == "penalty"
    assert status.gains_required > 0.0


def test_a_trailing_stop_gain_reduces_the_balance_one_for_one(config, ledger):
    """The equity discipline's PREFERRED winning exit has to clear the penalty.

    SKILL.md prefers a trailing stop for winners because it "protects gains
    without capping upside, which is why it is used and mandatory scaling out is
    not". While qualifying_exit_reasons held only [target, time_window], the
    recommended way to realise an equity gain earned nothing back, and the one
    key that would have produced time_window exits — positions.hold_days_max —
    was read by no code. The sleeve was therefore close to permanently locked by
    a rule nobody had chosen. Phase 2 opened this channel; Phase 3 deleted that
    key rather than leave a hold limit nothing enforced.
    """
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")
    assert "trailing_stop" in config.require_list("option_sleeve.qualifying_exit_reasons")

    ledger.book_loss_detected(expected_loss=1_000.0)
    ledger.book_qualifying_gain(realised_pnl=750.0, exit_reason="trailing_stop")

    assert ledger.balance() == pytest.approx(multiple * 1_000.0 - 750.0)
