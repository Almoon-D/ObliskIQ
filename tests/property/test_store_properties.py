"""Properties of the point-in-time reads over generated rows and bounds.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from obliskiq.domain.instrument import Instrument
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Bar, Snapshot
from obliskiq.store import Store, open_database, repos

#: The factory fixtures only build immutable objects, so sharing them between examples is safe.
STATELESS_FIXTURES = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])

ORIGIN = date(2026, 1, 1)
days = st.dates(min_value=ORIGIN, max_value=ORIGIN + timedelta(days=60))
SPY = 756733


@contextmanager
def fresh(make_instrument: Callable[..., Instrument]) -> Iterator[Store]:
    store = open_database(":memory:", busy_timeout_ms=1000, synchronous="NORMAL")
    try:
        repos.upsert_instrument(store, make_instrument())
        yield store
    finally:
        store.close()


@STATELESS_FIXTURES
@given(session_days=st.sets(days, max_size=25), as_of=days, limit=st.none() | st.integers(1, 10))
def test_bars_are_exactly_those_on_or_before_the_bound(
    make_instrument: Callable[..., Instrument],
    make_bar: Callable[..., Bar],
    session_days: set[date],
    as_of: date,
    limit: int | None,
) -> None:
    with fresh(make_instrument) as store:
        repos.upsert_bars(store, [make_bar(session_date=d) for d in session_days])
        expected = sorted(d for d in session_days if d <= as_of)
        if limit is not None:
            expected = expected[-limit:]
        read = repos.bar_series(store, SPY, as_of=as_of, limit=limit)
        assert [b.session_date for b in read] == expected
        if limit is None:
            assert repos.bar_count(store, SPY, as_of=as_of) == len(expected)
            latest = repos.latest_bar_date(store, SPY, as_of=as_of)
            assert latest == (expected[-1] if expected else None)


@STATELESS_FIXTURES
@given(
    snapshot_days=st.dictionaries(days, st.booleans(), max_size=25),
    as_of=days,
)
def test_snapshot_counts_never_see_past_the_bound(
    make_instrument: Callable[..., Instrument],
    make_snapshot: Callable[..., Snapshot],
    snapshot_days: dict[date, bool],
    as_of: date,
) -> None:
    with fresh(make_instrument) as store:
        repos.upsert_snapshots(
            store,
            [
                make_snapshot(
                    snapshot_date=d,
                    implied_vol=0.2 if quoted else None,
                    implied_vol_source="cp_api" if quoted else None,
                )
                for d, quoted in snapshot_days.items()
            ],
        )
        visible = {d: quoted for d, quoted in snapshot_days.items() if d <= as_of}
        assert repos.snapshot_count(store, SPY, as_of=as_of) == len(visible)
        assert repos.implied_vol_snapshot_count(store, SPY, as_of=as_of) == sum(visible.values())


@STATELESS_FIXTURES
@given(
    releases=st.lists(
        st.tuples(st.none() | st.integers(0, 40), st.integers(0, 40)),
        min_size=0,
        max_size=15,
        unique_by=lambda pair: pair[1],
    ),
    as_of_offset=st.integers(0, 80),
)
def test_a_macro_series_holds_exactly_what_had_been_released(
    make_observation: Callable[..., MacroObservation],
    make_instrument: Callable[..., Instrument],
    releases: list[tuple[int | None, int]],
    as_of_offset: int,
) -> None:
    """Each pair is (days between period and release or None, the period's day offset)."""
    as_of = ORIGIN + timedelta(days=as_of_offset)
    collected = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        make_observation(
            reference_date=ORIGIN + timedelta(days=offset),
            release_date=None if lag is None else ORIGIN + timedelta(days=offset + lag),
            value=float(offset),
            collected_at=collected,
        )
        for lag, offset in releases
    ]
    with fresh(make_instrument) as store:
        repos.upsert_macro(store, observations)
        series = repos.macro_series(store, "CPI_X", as_of=as_of)
        expected = sorted(
            (o for o in observations if o.release_date is not None and o.release_date <= as_of),
            key=lambda o: o.reference_date,
        )
        assert list(series.observations) == expected
        assert series.unaligned == sum(1 for o in observations if o.release_date is None)
        assert all(
            o.release_date is not None and o.release_date <= as_of for o in series.observations
        )
