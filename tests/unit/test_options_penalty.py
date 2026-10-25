"""The sleeve's pool and penalty balance (lite's ``tests/test_compute_penalty.py``, ported): the
budget is a pool, the penalty is booked at detection and reconciled by the difference at the
close, recovery is on qualifying realised gains only, and the stored running balance agrees with
the replay.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from options_rig import position

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import MarketDataStatus, OptionRight
from obliskiq.domain.options import Conversion, LedgerEventType, ShadowEntry
from obliskiq.engines.options_sleeve.penalty import PenaltyError, PenaltyLedger, replay
from obliskiq.errors import StoreError
from obliskiq.store import Store
from obliskiq.store import options as repo

OPENED = date(2026, 10, 1)
AT = datetime(2026, 10, 1, 21, 0, tzinfo=UTC)
BUDGET = Decimal("10000")


@pytest.fixture
def ledger(store: Store, shipped_settings: ObliskConfig) -> PenaltyLedger:
    return PenaltyLedger(store, shipped_settings, now=lambda: AT)


@pytest.fixture
def opened(store: Store) -> int:
    found = repo.open_position(store, position()).position_id
    assert found is not None
    return found


def multiple(settings: ObliskConfig) -> Decimal:
    return settings.option_sleeve.loss_penalty_multiple


# ── the pool ──


def test_an_empty_ledger_gives_the_whole_budget(ledger: PenaltyLedger) -> None:
    status = ledger.status(budget=BUDGET, as_of=OPENED)
    assert status.available == BUDGET
    assert status.penalty_balance == 0
    assert status.committed_premium == 0
    assert not status.is_locked
    assert status.locked_by is None


def test_premium_committed_by_an_open_position_is_subtracted(
    ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_premium_committed(position_id=opened, premium=Decimal("1500"), on=OPENED)
    status = ledger.status(budget=BUDGET, as_of=OPENED)
    assert status.committed_premium == Decimal("1500")
    assert status.available == BUDGET - Decimal("1500")
    assert status.penalty_balance == 0, "a commitment moves no penalty"


def test_concurrent_positions_cannot_each_be_sized_against_the_whole_sleeve(
    store: Store, ledger: PenaltyLedger
) -> None:
    for _ in range(3):
        number = repo.open_position(store, position()).position_id
        assert number is not None
        ledger.book_premium_committed(position_id=number, premium=Decimal("3000"), on=OPENED)
    assert ledger.status(budget=BUDGET, as_of=OPENED).available == Decimal("1000")


def test_a_commitment_is_released_by_the_position_closing(
    store: Store, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_premium_committed(position_id=opened, premium=Decimal("2000"), on=OPENED)
    repo.close_position(store, opened, closed_on=date(2026, 10, 20), exit_reason="target")
    assert ledger.committed_premium(as_of=date(2026, 10, 19)) == Decimal("2000")
    assert ledger.committed_premium(as_of=date(2026, 10, 20)) == 0


def test_the_penalty_and_the_commitment_both_bite(
    shipped_settings: ObliskConfig, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_premium_committed(position_id=opened, premium=Decimal("1000"), on=OPENED)
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal("500"), on=OPENED)
    status = ledger.status(budget=BUDGET, as_of=OPENED)
    expected = BUDGET - Decimal("1000") - multiple(shipped_settings) * Decimal("500")
    assert status.available == expected
    assert status.gains_required == multiple(shipped_settings) * Decimal("500")


def test_a_commitment_needs_a_position_that_exists_and_a_positive_premium(
    ledger: PenaltyLedger, opened: int
) -> None:
    with pytest.raises(PenaltyError):
        ledger.book_premium_committed(position_id=opened, premium=Decimal(0), on=OPENED)
    with pytest.raises(PenaltyError):
        ledger.book_premium_committed(position_id=999, premium=Decimal(1), on=OPENED)


def test_a_locked_sleeve_says_which_thing_locked_it(
    store: Store, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_premium_committed(position_id=opened, premium=BUDGET, on=OPENED)
    assert ledger.status(budget=BUDGET, as_of=OPENED).locked_by == "commitments"
    second = repo.open_position(store, position()).position_id
    assert second is not None
    ledger.book_loss_detected(position_id=second, expected_loss=Decimal(100), on=OPENED)
    assert ledger.status(budget=BUDGET, as_of=OPENED).locked_by == "penalty_and_commitments"


def test_a_penalty_lock_is_named_apart_from_a_commitment_lock(
    ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=BUDGET, on=OPENED)
    assert ledger.status(budget=BUDGET, as_of=OPENED).locked_by == "penalty"
    assert ledger.status(budget=Decimal(0), as_of=date(2026, 9, 1)).locked_by == "no_budget"


# ── the penalty ──


def test_a_loss_adds_the_configured_multiple(
    shipped_settings: ObliskConfig, ledger: PenaltyLedger, opened: int
) -> None:
    event = ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(200), on=OPENED)
    assert event.penalty_added == multiple(shipped_settings) * Decimal(200)
    assert ledger.balance(as_of=OPENED) == multiple(shipped_settings) * Decimal(200)


def test_a_qualifying_gain_reduces_the_balance_one_for_one(
    ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(100), on=OPENED)
    before = ledger.balance(as_of=OPENED)
    ledger.book_qualifying_gain(
        conid=8314, realised_gain=Decimal(50), exit_reason="target", on=OPENED
    )
    assert ledger.balance(as_of=OPENED) == before - Decimal(50)


def test_every_configured_qualifying_reason_is_accepted(
    shipped_settings: ObliskConfig, ledger: PenaltyLedger
) -> None:
    for reason in shipped_settings.option_sleeve.qualifying_exit_reasons:
        event = ledger.book_qualifying_gain(
            conid=8314, realised_gain=Decimal(1), exit_reason=reason, on=OPENED
        )
        assert event.event_type is LedgerEventType.QUALIFYING_GAIN


def test_an_override_gain_never_reduces_the_balance(ledger: PenaltyLedger) -> None:
    with pytest.raises(PenaltyError, match="override"):
        ledger.book_qualifying_gain(
            conid=8314, realised_gain=Decimal(50), exit_reason="override", on=OPENED
        )


def test_an_option_win_does_not_reduce_the_balance_either(
    ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(100), on=OPENED)
    before = ledger.balance(as_of=OPENED)
    event = ledger.record_option_gain(position_id=opened, realised_gain=Decimal(500), on=OPENED)
    assert event.penalty_added == 0
    assert ledger.balance(as_of=OPENED) == before


def test_the_balance_floors_at_zero(ledger: PenaltyLedger, opened: int) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(10), on=OPENED)
    ledger.book_qualifying_gain(
        conid=8314, realised_gain=Decimal(1000), exit_reason="target", on=OPENED
    )
    assert ledger.balance(as_of=OPENED) == 0
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(10), on=OPENED)
    assert ledger.balance(as_of=OPENED) > 0, "banked gains buy no credit against a later loss"


def test_detection_books_the_penalty_and_a_close_at_the_figure_adds_nothing(
    shipped_settings: ObliskConfig, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(250), on=OPENED)
    booked = ledger.balance(as_of=OPENED)
    closed = ledger.reconcile_loss_closed(position_id=opened, actual_loss=Decimal(250), on=OPENED)
    assert closed.penalty_added == 0
    assert ledger.balance(as_of=OPENED) == booked


def test_a_worse_close_adjusts_by_the_difference_only(
    shipped_settings: ObliskConfig, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(200), on=OPENED)
    closed = ledger.reconcile_loss_closed(position_id=opened, actual_loss=Decimal(260), on=OPENED)
    assert closed.penalty_added == multiple(shipped_settings) * Decimal(60)


def test_a_better_close_gives_the_difference_back(
    shipped_settings: ObliskConfig, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(200), on=OPENED)
    closed = ledger.reconcile_loss_closed(position_id=opened, actual_loss=Decimal(150), on=OPENED)
    assert closed.penalty_added == -multiple(shipped_settings) * Decimal(50)


def test_a_close_with_no_detection_books_the_whole_penalty(
    shipped_settings: ObliskConfig, ledger: PenaltyLedger, opened: int
) -> None:
    closed = ledger.reconcile_loss_closed(position_id=opened, actual_loss=Decimal(80), on=OPENED)
    assert closed.penalty_added == multiple(shipped_settings) * Decimal(80)


def test_two_positions_reconcile_independently(
    store: Store, shipped_settings: ObliskConfig, ledger: PenaltyLedger, opened: int
) -> None:
    second = repo.open_position(store, position()).position_id
    assert second is not None
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(100), on=OPENED)
    closed = ledger.reconcile_loss_closed(position_id=second, actual_loss=Decimal(100), on=OPENED)
    assert closed.penalty_added == multiple(shipped_settings) * Decimal(100)


@pytest.mark.parametrize(
    "call",
    [
        lambda ledger, pid: ledger.book_loss_detected(
            position_id=pid, expected_loss=Decimal(-1), on=OPENED
        ),
        lambda ledger, pid: ledger.reconcile_loss_closed(
            position_id=pid, actual_loss=Decimal(-1), on=OPENED
        ),
        lambda ledger, _pid: ledger.book_qualifying_gain(
            conid=8314, realised_gain=Decimal(0), exit_reason="target", on=OPENED
        ),
        lambda ledger, pid: ledger.record_option_gain(
            position_id=pid, realised_gain=Decimal(-1), on=OPENED
        ),
    ],
)
def test_a_signed_magnitude_is_refused(ledger: PenaltyLedger, opened: int, call: Any) -> None:
    with pytest.raises(PenaltyError):
        call(ledger, opened)


def test_events_are_written_in_date_order(ledger: PenaltyLedger, opened: int) -> None:
    """The balance is a replay in date order; a back-dated event would rewrite history."""
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(1), on=date(2026, 10, 5))
    with pytest.raises(PenaltyError, match="date order"):
        ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(1), on=OPENED)


def test_the_balance_as_of_a_day_counts_only_the_events_dated_by_it(
    shipped_settings: ObliskConfig, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(10), on=date(2026, 10, 5))
    assert ledger.balance(as_of=date(2026, 10, 4)) == 0
    assert ledger.balance(as_of=date(2026, 10, 5)) == multiple(shipped_settings) * Decimal(10)


# ── the audit trail ──


def test_the_running_balance_matches_a_replay(
    store: Store, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(100), on=OPENED)
    ledger.book_qualifying_gain(
        conid=8314, realised_gain=Decimal(40), exit_reason="trailing_stop", on=OPENED
    )
    ledger.reconcile_loss_closed(position_id=opened, actual_loss=Decimal(120), on=OPENED)
    assert ledger.verify(as_of=OPENED) == []
    events = repo.ledger_events(store, as_of=OPENED)
    assert replay(events) == events[-1].penalty_balance_after == ledger.balance(as_of=OPENED)


def test_verify_reports_a_running_balance_that_disagrees(
    store: Store, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(100), on=OPENED)
    store.execute("DROP TRIGGER option_ledger_no_update")
    store.execute("UPDATE option_ledger SET penalty_balance_after = '1'")
    problems = ledger.verify(as_of=OPENED)
    assert len(problems) == 1
    assert "the replay gives" in problems[0]


def test_a_conversion_travels_with_its_event(
    store: Store, ledger: PenaltyLedger, opened: int
) -> None:
    conversion = Conversion(currency="USD", quote_amount=Decimal(350), fx_to_base=Decimal("0.856"))
    ledger.book_premium_committed(
        position_id=opened, premium=Decimal("299.60"), on=OPENED, conversion=conversion
    )
    (event,) = repo.ledger_events(store, as_of=OPENED)
    assert event.conversion == conversion
    assert event.recorded_at == AT


# ── the repository ──


def test_a_position_round_trips_and_closes_once(store: Store) -> None:
    stored = repo.open_position(store, position(target=120.0, prob_target_at_entry=0.3))
    assert stored.position_id is not None
    assert repo.get_position(store, stored.position_id) == stored
    assert repo.open_positions(store, as_of=OPENED) == (stored,)
    assert repo.open_positions(store, as_of=date(2026, 9, 30)) == ()
    repo.close_position(
        store, stored.position_id, closed_on=date(2026, 11, 2), exit_reason="zombie"
    )
    closed = repo.get_position(store, stored.position_id)
    assert closed is not None
    assert not closed.is_open
    assert closed.exit_reason == "zombie"
    with pytest.raises(StoreError, match="already closed"):
        repo.close_position(store, stored.position_id, closed_on=date(2026, 11, 3), exit_reason="x")


def test_a_position_is_refused_closing_twice_early_or_unknown(store: Store) -> None:
    stored = repo.open_position(store, position())
    assert stored.position_id is not None
    with pytest.raises(StoreError, match="before it opened"):
        repo.close_position(store, stored.position_id, closed_on=date(2026, 9, 1), exit_reason="x")
    with pytest.raises(StoreError, match="does not exist"):
        repo.close_position(store, 999, closed_on=OPENED, exit_reason="x")
    with pytest.raises(StoreError, match="unnumbered and open"):
        repo.open_position(store, stored)


def test_a_numbered_event_is_not_appended_again(
    store: Store, ledger: PenaltyLedger, opened: int
) -> None:
    event = ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(1), on=OPENED)
    with pytest.raises(StoreError):
        repo.append_event(store, event)


def test_a_corrupt_ledger_row_is_a_store_error(
    store: Store, ledger: PenaltyLedger, opened: int
) -> None:
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(1), on=OPENED)
    store.execute("DROP TRIGGER option_ledger_no_update")
    store.execute("UPDATE option_ledger SET amount = 'abc'")
    with pytest.raises(StoreError, match="not a decimal"):
        repo.ledger_events(store, as_of=OPENED)


def test_a_ledger_row_its_model_refuses_is_a_store_error(
    store: Store, ledger: PenaltyLedger, opened: int, shipped_settings: ObliskConfig
) -> None:
    assert ledger.loss_penalty_multiple == multiple(shipped_settings)
    ledger.book_loss_detected(position_id=opened, expected_loss=Decimal(1), on=OPENED)
    store.execute("DROP TRIGGER option_ledger_no_update")
    store.execute("PRAGMA ignore_check_constraints = ON")
    store.execute("UPDATE option_ledger SET note = ''")
    with pytest.raises(StoreError, match=r"option_ledger row \d+ cannot be read"):
        repo.ledger_events(store, as_of=OPENED)


def shadow(**changes: Any) -> ShadowEntry:
    fields: dict[str, Any] = {
        "as_of": OPENED,
        "underlying_conid": 756733,
        "expiry": date(2027, 1, 15),
        "strike": 525.0,
        "right": OptionRight.CALL,
        "snapshot_date": OPENED,
        "option_conid": None,
        "bid": 9.9,
        "ask": 10.1,
        "mid": 10.0,
        "iv": 0.19,
        "delta": 0.4,
        "multiplier": 100.0,
        "currency": "USD",
        "premium_base": Decimal("855.94"),
        "iv_percentile": 0.1,
        "market_data_status": MarketDataStatus.REALTIME,
        "untested": ("price_ceiling",),
        "activation": "shadow: evidence fails",
        "computed_at": AT,
    }
    return ShadowEntry(**(fields | changes))


def test_shadow_entries_round_trip_and_replace_their_session_only(store: Store) -> None:
    assert repo.replace_shadow_entries(store, OPENED, [shadow()]) == 1
    assert repo.shadow_entries(store, as_of=OPENED) == (shadow(),)
    with pytest.raises(StoreError, match="must be dated"):
        repo.replace_shadow_entries(store, OPENED, [shadow(as_of=date(2026, 10, 2))])
    assert repo.shadow_entries(store, as_of=OPENED) == (shadow(),), "a refusal writes nothing"


def test_a_corrupt_shadow_row_is_a_store_error(store: Store) -> None:
    repo.replace_shadow_entries(store, OPENED, [shadow()])
    store.execute("PRAGMA ignore_check_constraints = ON")
    store.execute("UPDATE option_shadow_entries SET mid = -1")
    with pytest.raises(StoreError, match=r"option_shadow_entries row .* cannot be read"):
        repo.shadow_entries(store, as_of=OPENED)


def test_a_corrupt_position_row_is_a_store_error(store: Store) -> None:
    stored = repo.open_position(store, position())
    store.execute("DROP TRIGGER option_positions_identity_fixed")
    store.execute("PRAGMA ignore_check_constraints = ON")
    store.execute("UPDATE option_positions SET multiplier = -1")
    assert stored.position_id is not None
    with pytest.raises(StoreError, match="cannot be read"):
        repo.get_position(store, stored.position_id)
