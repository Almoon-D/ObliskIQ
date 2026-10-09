"""Every read answers as the store stood on its ``as_of`` date, and no later.

Ported in intent from the legacy ``test_point_in_time.py``: macro alignment on release date,
units and frequencies inside a window, the bar bound that keeps the session it names, and the
counts that once looked like they could not leak.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest

from obliskiq.domain.instrument import Instrument
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Bar, ChainRow, CurvePoint, Snapshot
from obliskiq.store import Store, repos

SPY = 756733
DAY = date(2026, 10, 8)
BEFORE = DAY - timedelta(days=1)
AFTER = DAY + timedelta(days=1)


@pytest.fixture
def seeded(store: Store, make_instrument: Callable[..., Instrument]) -> Store:
    repos.upsert_instrument(store, make_instrument())
    return store


# ── macro: align on the release date ──


def test_an_observation_not_yet_published_is_not_known(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    published = make_observation(reference_date=date(2026, 6, 1), release_date=date(2026, 7, 14))
    pending = make_observation(reference_date=date(2026, 7, 1), release_date=date(2026, 8, 12))
    repos.upsert_macro(store, [published, pending])
    on_the_day_before = repos.macro_series(store, "CPI_X", as_of=date(2026, 8, 11))
    assert on_the_day_before.observations == (published,)
    assert on_the_day_before.latest == published
    on_the_release_day = repos.macro_series(store, "CPI_X", as_of=date(2026, 8, 12))
    assert on_the_release_day.observations == (published, pending)  # the release day itself counts


def test_aligning_on_the_reference_period_would_read_the_future(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    """The foil: the same data read by reference date returns a print nobody had yet."""
    pending = make_observation(reference_date=date(2026, 7, 1), release_date=date(2026, 8, 12))
    repos.upsert_macro(store, [pending])
    as_of = date(2026, 7, 31)
    by_release = repos.macro_series(store, "CPI_X", as_of=as_of)
    by_reference = store.execute(
        "SELECT COUNT(*) FROM macro WHERE series_id = 'CPI_X' AND reference_date <= ?",
        (as_of.isoformat(),),
    ).fetchone()[0]
    assert len(by_release) == 0
    assert by_reference == 1


def test_a_missing_release_date_is_excluded_not_assumed(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(store, [make_observation(release_date=None)])
    series = repos.macro_series(store, "CPI_X", as_of=date(2026, 12, 31))
    assert series.observations == ()
    assert series.latest is None


def test_the_unaligned_count_reports_the_coverage_gap_and_is_bounded_by_collection(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    held = make_observation(
        reference_date=date(2026, 5, 1),
        release_date=None,
        collected_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    collected_later = make_observation(
        reference_date=date(2026, 6, 1),
        release_date=None,
        collected_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    repos.upsert_macro(store, [held, collected_later])
    assert repos.macro_series(store, "CPI_X", as_of=date(2026, 5, 31)).unaligned == 0
    assert repos.macro_series(store, "CPI_X", as_of=date(2026, 6, 1)).unaligned == 1
    assert repos.macro_series(store, "CPI_X", as_of=date(2026, 12, 1)).unaligned == 2


def test_a_series_with_no_release_dates_yields_nothing_and_says_how_much(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(
        store,
        [
            make_observation(reference_date=date(2026, month, 1), release_date=None)
            for month in (3, 4, 5)
        ],
    )
    series = repos.macro_series(store, "CPI_X", as_of=date(2026, 12, 1))
    assert (len(series), series.unaligned) == (0, 3)


def test_the_print_that_was_out_is_used_not_the_month_it_describes(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(
        store,
        [
            make_observation(
                reference_date=date(2026, 7, 1), release_date=date(2026, 8, 12), value=1.0
            ),
            make_observation(
                reference_date=date(2026, 8, 1), release_date=date(2026, 9, 11), value=2.0
            ),
        ],
    )
    latest = repos.macro_series(store, "CPI_X", as_of=date(2026, 9, 5)).latest
    assert latest is not None
    assert latest.value == 1.0  # August describes a month already over but was published after


def test_values_for_a_window_exclude_what_was_not_yet_published(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(
        store,
        [
            make_observation(
                reference_date=date(2026, month, 1),
                release_date=date(2026, month + 1, 10),
                value=float(month),
            )
            for month in range(1, 8)
        ],
    )
    assert repos.macro_series(store, "CPI_X", as_of=date(2026, 5, 9)).values() == [1.0, 2.0, 3.0]
    assert repos.macro_series(store, "CPI_X", as_of=date(2026, 5, 10)).values() == [1, 2, 3, 4]


# ── macro: units and frequencies inside the window ──


def test_a_definition_change_inside_the_window_is_visible(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(
        store,
        [
            make_observation(
                reference_date=date(2026, 1, 1),
                release_date=date(2026, 2, 5),
                units="net contracts",
            ),
            make_observation(
                reference_date=date(2026, 2, 1), release_date=date(2026, 3, 5), units="share of OI"
            ),
        ],
    )
    both = repos.macro_series(store, "CPI_X", as_of=date(2026, 6, 1))
    assert both.units == frozenset({"net contracts", "share of OI"})
    one = repos.macro_series(store, "CPI_X", as_of=date(2026, 2, 20))
    assert one.units == frozenset({"net contracts"})  # the later change is not seen yet


def test_one_definition_over_the_window_is_not_flagged_and_an_undeclared_unit_cannot_contradict(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(
        store,
        [
            make_observation(
                reference_date=date(2026, 1, 1), release_date=date(2026, 2, 5), units="index"
            ),
            make_observation(
                reference_date=date(2026, 2, 1), release_date=date(2026, 3, 5), units="index"
            ),
            make_observation(
                reference_date=date(2026, 3, 1), release_date=date(2026, 4, 5), units=None
            ),
        ],
    )
    assert repos.macro_series(store, "CPI_X", as_of=date(2026, 6, 1)).units == frozenset({"index"})


def test_frequencies_are_read_the_same_way(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(
        store,
        [
            make_observation(
                reference_date=date(2026, 1, 1), release_date=date(2026, 2, 5), frequency="monthly"
            ),
            make_observation(
                reference_date=date(2026, 2, 1), release_date=date(2026, 3, 5), frequency="daily"
            ),
            make_observation(
                reference_date=date(2026, 3, 1), release_date=date(2026, 4, 5), frequency=None
            ),
        ],
    )
    assert repos.macro_series(store, "CPI_X", as_of=date(2026, 3, 1)).frequencies == frozenset(
        {"monthly"}
    )
    assert repos.macro_series(store, "CPI_X", as_of=date(2026, 6, 1)).frequencies == frozenset(
        {"monthly", "daily"}
    )


def test_a_series_carries_the_release_dates_under_the_same_rule(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(
        store,
        [
            make_observation(reference_date=date(2026, 1, 1), release_date=date(2026, 2, 5)),
            make_observation(reference_date=date(2026, 2, 1), release_date=date(2026, 3, 5)),
        ],
    )
    series = repos.macro_series(store, "CPI_X", as_of=date(2026, 2, 28))
    assert [o.release_date for o in series.observations] == [date(2026, 2, 5)]


# ── bars ──


def test_a_bar_series_stops_at_the_session_it_is_asked_for_and_keeps_that_session(
    seeded: Store, make_bar: Callable[..., Bar]
) -> None:
    repos.upsert_bars(
        seeded, [make_bar(session_date=d) for d in (BEFORE, DAY, AFTER, AFTER + timedelta(days=1))]
    )
    assert [b.session_date for b in repos.bar_series(seeded, SPY, as_of=DAY)] == [BEFORE, DAY]
    assert [b.session_date for b in repos.bar_series(seeded, SPY, as_of=BEFORE)] == [BEFORE]
    assert repos.bar_series(seeded, SPY, as_of=BEFORE - timedelta(days=1)) == ()


def test_the_recent_closes_stop_at_the_session_too(
    seeded: Store, make_bar: Callable[..., Bar]
) -> None:
    repos.upsert_bars(
        seeded,
        [make_bar(session_date=DAY - timedelta(days=n), close=100.0 - n) for n in range(-2, 5)],
    )
    recent = repos.bar_series(seeded, SPY, as_of=DAY, limit=3)
    assert [b.session_date for b in recent] == [
        DAY - timedelta(days=2),
        DAY - timedelta(days=1),
        DAY,
    ]


def test_the_newest_bar_cannot_be_from_the_future(
    seeded: Store, make_bar: Callable[..., Bar]
) -> None:
    repos.upsert_bars(seeded, [make_bar(session_date=DAY), make_bar(session_date=AFTER)])
    assert repos.latest_bar_date(seeded, SPY, as_of=DAY) == DAY
    assert repos.latest_bar_date(seeded, SPY, as_of=BEFORE) is None
    assert repos.latest_bar_date(seeded, SPY, as_of=AFTER) == AFTER


# ── snapshots and their counts ──


def test_snapshots_stop_at_the_session_and_so_does_every_count(
    seeded: Store, make_snapshot: Callable[..., Snapshot]
) -> None:
    repos.upsert_snapshots(
        seeded,
        [make_snapshot(snapshot_date=d) for d in (BEFORE, DAY, AFTER, AFTER + timedelta(days=1))],
    )
    assert [s.snapshot_date for s in repos.snapshots(seeded, SPY, as_of=DAY)] == [BEFORE, DAY]
    assert repos.snapshot_count(seeded, SPY, as_of=DAY) == 2
    assert repos.implied_vol_snapshot_count(seeded, SPY, as_of=DAY) == 2
    assert repos.snapshot_count(seeded, SPY, as_of=BEFORE) == 1
    assert repos.snapshot_count(seeded, SPY, as_of=BEFORE - timedelta(days=1)) == 0


def test_a_count_cannot_open_a_gate_on_evidence_from_its_own_future(
    seeded: Store, make_snapshot: Callable[..., Snapshot]
) -> None:
    """The gate opens at N accumulated sessions; a count past the bound would open it early."""
    start = date(2026, 1, 1)
    repos.upsert_snapshots(
        seeded, [make_snapshot(snapshot_date=start + timedelta(days=n)) for n in range(30)]
    )
    threshold = 20
    assert (
        repos.implied_vol_snapshot_count(seeded, SPY, as_of=start + timedelta(days=14)) < threshold
    )
    assert (
        repos.implied_vol_snapshot_count(seeded, SPY, as_of=start + timedelta(days=19)) == threshold
    )


def test_the_count_ignores_sessions_with_no_implied_vol(
    seeded: Store, make_snapshot: Callable[..., Snapshot]
) -> None:
    repos.upsert_snapshots(
        seeded,
        [
            make_snapshot(snapshot_date=BEFORE, implied_vol=None, implied_vol_source=None),
            make_snapshot(snapshot_date=DAY),
        ],
    )
    assert repos.implied_vol_snapshot_count(seeded, SPY, as_of=DAY) == 1
    assert repos.snapshot_count(seeded, SPY, as_of=DAY) == 2


# ── chains and curves ──


def test_the_latest_chain_cannot_be_a_snapshot_from_the_future(
    seeded: Store, make_chain_row: Callable[..., ChainRow]
) -> None:
    repos.upsert_chain_rows(
        seeded, [make_chain_row(snapshot_date=BEFORE), make_chain_row(snapshot_date=AFTER)]
    )
    assert repos.latest_chain_date(seeded, as_of=DAY) == BEFORE
    assert [r.snapshot_date for r in repos.chain_rows(seeded, as_of=DAY)] == [BEFORE]
    assert repos.chain_row_count(seeded, as_of=DAY) == 1
    assert repos.chain_row_count(seeded, as_of=AFTER) == 2


def test_a_curve_for_a_past_date_is_not_todays_shape(
    store: Store, make_curve_point: Callable[..., CurvePoint]
) -> None:
    repos.upsert_curve_points(
        store,
        [
            make_curve_point(snapshot_date=BEFORE, price=60.0),
            make_curve_point(snapshot_date=AFTER, price=99.0),
        ],
    )
    (point,) = repos.curve(store, "CL", as_of=DAY)
    assert point.price == 60.0
    assert repos.latest_curve_date(store, "CL", as_of=DAY) == BEFORE
    assert [d for d, _ in repos.curve_front_history(store, "CL", as_of=DAY)] == [BEFORE]


# ── instruments ──


def test_an_instrument_created_after_the_date_does_not_exist_yet(
    store: Store, make_instrument: Callable[..., Instrument]
) -> None:
    repos.upsert_instrument(
        store, make_instrument(created_at=datetime(2026, 10, 9, 8, 0, tzinfo=UTC))
    )
    assert repos.get_instrument(store, SPY, as_of=DAY) is None
    assert repos.list_instruments(store, as_of=DAY, include_superseded=True) == ()
    assert repos.get_instrument(store, SPY, as_of=date(2026, 10, 9)) is not None


# ── the same question of every read, at once ──


def test_rows_dated_after_the_bound_are_invisible_to_every_read(
    seeded: Store,
    make_bar: Callable[..., Bar],
    make_snapshot: Callable[..., Snapshot],
    make_chain_row: Callable[..., ChainRow],
    make_curve_point: Callable[..., CurvePoint],
    make_observation: Callable[..., MacroObservation],
) -> None:
    repos.upsert_bars(seeded, [make_bar(session_date=AFTER)])
    repos.upsert_snapshots(seeded, [make_snapshot(snapshot_date=AFTER)])
    repos.upsert_chain_rows(seeded, [make_chain_row(snapshot_date=AFTER)])
    repos.upsert_curve_points(seeded, [make_curve_point(snapshot_date=AFTER)])
    repos.upsert_macro(
        seeded, [make_observation(reference_date=DAY, release_date=AFTER, value=7.0)]
    )
    assert repos.bar_series(seeded, SPY, as_of=DAY) == ()
    assert repos.bar_count(seeded, SPY, as_of=DAY) == 0
    assert repos.latest_bar_date(seeded, SPY, as_of=DAY) is None
    assert repos.snapshots(seeded, SPY, as_of=DAY) == ()
    assert repos.snapshot_count(seeded, SPY, as_of=DAY) == 0
    assert repos.implied_vol_snapshot_count(seeded, SPY, as_of=DAY) == 0
    assert repos.chain_rows(seeded, as_of=DAY) == ()
    assert repos.chain_row_count(seeded, as_of=DAY) == 0
    assert repos.latest_chain_date(seeded, as_of=DAY) is None
    assert repos.curve(seeded, "CL", as_of=DAY) == ()
    assert repos.latest_curve_date(seeded, "CL", as_of=DAY) is None
    assert repos.curve_front_history(seeded, "CL", as_of=DAY) == ()
    assert len(repos.macro_series(seeded, "CPI_X", as_of=DAY)) == 0
