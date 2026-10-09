"""Repositories: round trips, idempotent writes, ordering, atomicity and corrupt rows.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest

from obliskiq.domain.enums import (
    AssetClass,
    InstrumentRole,
    KidStatus,
    MarketDataStatus,
    OptionRight,
    ShareClass,
)
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Bar, ChainRow, CurvePoint, Snapshot
from obliskiq.errors import StoreError
from obliskiq.store import Store, repos

SPY = 756733
OTHER = 4815
AS_OF = date(2026, 10, 9)
LATER = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def seeded(store: Store, make_instrument: Callable[..., Instrument]) -> Store:
    repos.upsert_instrument(store, make_instrument())
    repos.upsert_instrument(store, make_instrument(conid=OTHER, symbol="EWJ", currency="USD"))
    return store


# ── instruments ──


def test_an_instrument_round_trips_with_every_optional_field(
    store: Store, make_instrument: Callable[..., Instrument]
) -> None:
    full = make_instrument(
        domicile="IE",
        ucits=True,
        kid_status=KidStatus.AVAILABLE,
        share_class=ShareClass.ACCUMULATING,
        lot_size=10,
        history_exchange="SMART",
        roles={InstrumentRole.DATA, InstrumentRole.TRADABLE_CANDIDATE},
    )
    bare = make_instrument(
        conid=2,
        symbol="IDX",
        asset_class=AssetClass.INDEX,
        domicile=None,
        ucits=None,
        kid_status=None,
        share_class=None,
        lot_size=None,
        option_exchange=None,
        history_exchange=None,
        roles={InstrumentRole.DATA},
    )
    for instrument in (full, bare):
        repos.upsert_instrument(store, instrument)
    assert repos.get_instrument(store, full.conid, as_of=AS_OF) == full
    assert repos.get_instrument(store, bare.conid, as_of=AS_OF) == bare


def test_ucits_false_is_not_unknown(
    store: Store, make_instrument: Callable[..., Instrument]
) -> None:
    repos.upsert_instrument(store, make_instrument(ucits=False))
    stored = repos.get_instrument(store, SPY, as_of=AS_OF)
    assert stored is not None
    assert stored.ucits is False


def test_updating_an_instrument_keeps_when_it_was_first_seen(
    seeded: Store, make_instrument: Callable[..., Instrument]
) -> None:
    changed = make_instrument(symbol="SPYX", created_at=datetime(2026, 9, 1, tzinfo=UTC))
    repos.upsert_instrument(seeded, changed)
    stored = repos.get_instrument(seeded, SPY, as_of=AS_OF)
    assert stored is not None
    assert stored.symbol == "SPYX"
    assert stored.created_at == datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


def test_an_instrument_did_not_exist_before_it_was_created(seeded: Store) -> None:
    assert repos.get_instrument(seeded, SPY, as_of=date(2026, 1, 4)) is None
    assert repos.get_instrument(seeded, SPY, as_of=date(2026, 1, 5)) is not None
    assert repos.get_instrument(seeded, 999, as_of=AS_OF) is None


def test_a_supersession_is_invisible_before_the_day_it_was_recorded(
    seeded: Store, make_instrument: Callable[..., Instrument]
) -> None:
    repos.upsert_instrument(seeded, make_instrument(conid=2, symbol="NEW"))
    repos.supersede_instrument(seeded, SPY, 2, at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC))
    before = repos.get_instrument(seeded, SPY, as_of=date(2026, 6, 9))
    after = repos.get_instrument(seeded, SPY, as_of=date(2026, 6, 10))
    assert before is not None
    assert before.superseded_by is None
    assert before.superseded_at is None
    assert after is not None
    assert after.superseded_by == 2
    assert after.superseded_at == datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def test_superseding_refuses_the_unknown_the_repeated_and_the_circular(
    seeded: Store, make_instrument: Callable[..., Instrument]
) -> None:
    repos.upsert_instrument(seeded, make_instrument(conid=2, symbol="NEW"))
    with pytest.raises(StoreError, match="unknown instrument 999"):
        repos.supersede_instrument(seeded, 999, 2, at=LATER)
    repos.supersede_instrument(seeded, SPY, 2, at=LATER)
    with pytest.raises(StoreError, match="already superseded by 2"):
        repos.supersede_instrument(seeded, SPY, OTHER, at=LATER)
    with pytest.raises(StoreError, match="would make a cycle"):
        repos.supersede_instrument(seeded, 2, SPY, at=LATER)
    with pytest.raises(StoreError, match="would make a cycle"):
        repos.supersede_instrument(seeded, OTHER, OTHER, at=LATER)
    with pytest.raises(StoreError, match="transaction failed"):  # the replacement must exist
        repos.supersede_instrument(seeded, OTHER, 999, at=LATER)
    unchanged = repos.get_instrument(seeded, OTHER, as_of=date(2027, 1, 1))
    assert unchanged is not None
    assert unchanged.superseded_by is None


def test_a_routine_upsert_neither_erases_nor_moves_a_supersession(
    seeded: Store, make_instrument: Callable[..., Instrument]
) -> None:
    repos.upsert_instrument(seeded, make_instrument(conid=2, symbol="NEW"))
    repos.supersede_instrument(seeded, SPY, 2, at=LATER)
    refresh = repos.get_instrument(seeded, SPY, as_of=date(2027, 1, 1))
    assert refresh is not None
    repos.upsert_instrument(
        seeded, refresh.model_copy(update={"superseded_by": None, "superseded_at": None})
    )
    stored = repos.get_instrument(seeded, SPY, as_of=date(2027, 1, 1))
    assert stored is not None
    assert (stored.superseded_by, stored.superseded_at) == (2, LATER)
    moved = refresh.model_copy(update={"superseded_by": OTHER, "superseded_at": LATER})
    with pytest.raises(StoreError, match="supersede_instrument only"):
        repos.upsert_instrument(seeded, moved)
    unsuperseded = make_instrument(conid=OTHER, symbol="EWJ", superseded_by=2, superseded_at=LATER)
    with pytest.raises(StoreError, match="supersede_instrument only"):
        repos.upsert_instrument(seeded, unsuperseded)


def test_resolving_follows_the_chain_as_it_stood_on_the_day(
    seeded: Store, make_instrument: Callable[..., Instrument]
) -> None:
    repos.upsert_instrument(seeded, make_instrument(conid=3, symbol="THIRD"))
    repos.supersede_instrument(seeded, SPY, OTHER, at=datetime(2026, 3, 1, tzinfo=UTC))
    repos.supersede_instrument(seeded, OTHER, 3, at=datetime(2026, 6, 1, tzinfo=UTC))
    assert repos.resolve_instrument(seeded, SPY, as_of=date(2026, 2, 1)).conid == SPY  # type: ignore[union-attr]
    assert repos.resolve_instrument(seeded, SPY, as_of=date(2026, 4, 1)).conid == OTHER  # type: ignore[union-attr]
    assert repos.resolve_instrument(seeded, SPY, as_of=date(2026, 7, 1)).conid == 3  # type: ignore[union-attr]
    assert repos.resolve_instrument(seeded, 999, as_of=AS_OF) is None


def test_a_looping_chain_in_a_corrupt_store_raises_instead_of_hanging(seeded: Store) -> None:
    stamp = "2026-06-01T00:00:00Z"
    seeded.execute(
        "UPDATE instruments SET superseded_by = ?, superseded_at = ? WHERE conid = ?",
        (OTHER, stamp, SPY),
    )
    seeded.execute(
        "UPDATE instruments SET superseded_by = ?, superseded_at = ? WHERE conid = ?",
        (SPY, stamp, OTHER),
    )
    with pytest.raises(StoreError, match="supersession cycle"):
        repos.resolve_instrument(seeded, SPY, as_of=AS_OF)


def test_a_replacement_that_has_gone_missing_raises(seeded: Store) -> None:
    seeded.connection.execute("PRAGMA foreign_keys = OFF")
    seeded.execute(
        "UPDATE instruments SET superseded_by = 999, superseded_at = '2026-06-01T00:00:00Z' "
        "WHERE conid = ?",
        (SPY,),
    )
    with pytest.raises(StoreError, match="replacement 999, which does not exist"):
        repos.resolve_instrument(seeded, SPY, as_of=AS_OF)


def test_listing_instruments_honours_the_day_the_role_and_supersession(
    seeded: Store, make_instrument: Callable[..., Instrument]
) -> None:
    repos.upsert_instrument(
        seeded,
        make_instrument(
            conid=3,
            symbol="CAND",
            roles={InstrumentRole.TRADABLE_CANDIDATE},
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )
    repos.supersede_instrument(seeded, OTHER, 3, at=datetime(2026, 9, 15, tzinfo=UTC))

    def listed(as_of: date, **kw: object) -> list[int]:
        found = repos.list_instruments(seeded, as_of=as_of, include_superseded=False, **kw)  # type: ignore[arg-type]
        return [i.conid for i in found]

    assert listed(date(2026, 8, 1)) == [OTHER, SPY]
    assert listed(date(2026, 9, 10)) == [3, OTHER, SPY]
    assert listed(AS_OF) == [3, SPY]  # OTHER was superseded by then
    assert listed(AS_OF, role=InstrumentRole.TRADABLE_CANDIDATE) == [3]
    assert listed(AS_OF, role=InstrumentRole.OPTION_UNDERLYING) == [SPY]
    everything = repos.list_instruments(seeded, as_of=AS_OF, include_superseded=True)
    assert [i.conid for i in everything] == [3, OTHER, SPY]
    assert repos.list_instruments(seeded, as_of=date(2025, 1, 1), include_superseded=True) == ()


def test_an_unreadable_instrument_row_raises_naming_the_table(seeded: Store) -> None:
    seeded.execute("UPDATE instruments SET asset_class = 'spaceship' WHERE conid = ?", (SPY,))
    with pytest.raises(StoreError, match=r"instruments row .* cannot be read"):
        repos.get_instrument(seeded, SPY, as_of=AS_OF)


# ── bars ──


def _bars(make_bar: Callable[..., Bar], conid: int = SPY, days: int = 5) -> list[Bar]:
    first = date(2026, 10, 1)
    return [
        make_bar(conid=conid, session_date=first + timedelta(days=n), close=100.0 + n)
        for n in range(days)
    ]


def test_bars_round_trip_oldest_first(seeded: Store, make_bar: Callable[..., Bar]) -> None:
    bars = _bars(make_bar)
    assert repos.upsert_bars(seeded, list(reversed(bars))) == 5
    assert repos.bar_series(seeded, SPY, as_of=date(2026, 10, 31)) == tuple(bars)


def test_a_bar_with_missing_fields_round_trips_as_missing(
    seeded: Store, make_bar: Callable[..., Bar]
) -> None:
    sparse = make_bar(open=None, high=None, low=None, volume=None)
    repos.upsert_bars(seeded, [sparse])
    assert repos.bar_series(seeded, SPY, as_of=AS_OF) == (sparse,)


def test_writing_the_same_bar_again_replaces_it(
    seeded: Store, make_bar: Callable[..., Bar]
) -> None:
    repos.upsert_bars(seeded, [make_bar(close=100.0)])
    repos.upsert_bars(seeded, [make_bar(close=105.0, source="mcp")])
    (bar,) = repos.bar_series(seeded, SPY, as_of=AS_OF)
    assert (bar.close, bar.source) == (105.0, "mcp")
    assert repos.bar_count(seeded, SPY, as_of=AS_OF) == 1


def test_an_empty_write_is_a_no_op(seeded: Store) -> None:
    assert repos.upsert_bars(seeded, []) == 0
    assert repos.upsert_snapshots(seeded, []) == 0
    assert repos.upsert_chain_rows(seeded, []) == 0
    assert repos.upsert_curve_points(seeded, []) == 0
    assert repos.upsert_macro(seeded, []) == 0


def test_since_and_limit_narrow_a_bar_series(seeded: Store, make_bar: Callable[..., Bar]) -> None:
    repos.upsert_bars(seeded, _bars(make_bar, days=8))
    since = repos.bar_series(seeded, SPY, as_of=date(2026, 10, 31), since=date(2026, 10, 5))
    assert [b.session_date.day for b in since] == [5, 6, 7, 8]
    newest = repos.bar_series(seeded, SPY, as_of=date(2026, 10, 31), limit=3)
    assert [b.session_date.day for b in newest] == [6, 7, 8]  # the newest three, oldest first
    bounded = repos.bar_series(seeded, SPY, as_of=date(2026, 10, 5), limit=2)
    assert [b.session_date.day for b in bounded] == [4, 5]


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_is_refused(seeded: Store, limit: int) -> None:
    with pytest.raises(StoreError, match="limit must be positive"):
        repos.bar_series(seeded, SPY, as_of=AS_OF, limit=limit)
    with pytest.raises(StoreError, match="limit must be positive"):
        repos.snapshots(seeded, SPY, as_of=AS_OF, limit=limit)


def test_the_newest_bar_and_the_count(seeded: Store, make_bar: Callable[..., Bar]) -> None:
    assert repos.latest_bar_date(seeded, SPY, as_of=AS_OF) is None
    assert repos.bar_count(seeded, SPY, as_of=AS_OF) == 0
    repos.upsert_bars(seeded, _bars(make_bar))
    assert repos.latest_bar_date(seeded, SPY, as_of=date(2026, 10, 31)) == date(2026, 10, 5)
    assert repos.bar_count(seeded, SPY, as_of=date(2026, 10, 31)) == 5
    assert repos.bar_count(seeded, OTHER, as_of=date(2026, 10, 31)) == 0


def test_a_batch_with_one_bad_row_writes_nothing(
    seeded: Store, make_bar: Callable[..., Bar]
) -> None:
    good = make_bar()
    orphan = make_bar(conid=424242, session_date=date(2026, 10, 7))  # no such instrument
    with pytest.raises(StoreError, match="transaction failed"):
        repos.upsert_bars(seeded, [good, orphan])
    assert repos.bar_count(seeded, SPY, as_of=AS_OF) == 0


def test_a_write_joins_the_callers_transaction(seeded: Store, make_bar: Callable[..., Bar]) -> None:
    def write_and_fail() -> None:
        with seeded.transaction():
            repos.upsert_bars(seeded, _bars(make_bar))
            repos.upsert_bars(seeded, [make_bar(session_date=date(2026, 12, 1), conid=OTHER)])
            raise RuntimeError("the caller changes its mind")

    with pytest.raises(RuntimeError):
        write_and_fail()
    assert repos.bar_count(seeded, SPY, as_of=date(2027, 1, 1)) == 0
    assert repos.bar_count(seeded, OTHER, as_of=date(2027, 1, 1)) == 0


def test_a_date_that_looks_right_but_is_not_a_date_raises_on_read(seeded: Store) -> None:
    seeded.execute(
        "INSERT INTO bars (conid, session_date, close, source) VALUES (?, '2026-13-45', 1.0, 'x')",
        (SPY,),
    )
    with pytest.raises(StoreError, match=r"bars\.session_date holds '2026-13-45'"):
        repos.bar_series(seeded, SPY, as_of=date(2027, 1, 1))
    with pytest.raises(StoreError, match="not a YYYY-MM-DD date"):
        repos.latest_bar_date(seeded, SPY, as_of=date(2027, 1, 1))


# ── snapshots ──


def test_a_snapshot_round_trips_with_its_market_data_fields(
    seeded: Store, make_snapshot: Callable[..., Snapshot]
) -> None:
    full = make_snapshot(
        dividend_yield=0.012,
        dividend_yield_basis="trailing",
        implied_vol_solved=0.205,
        implied_vol_solved_source="mid",
        market_data_status=MarketDataStatus.DELAYED,
        market_data_raw="DELAYED",
        quote_delay_seconds=900,
    )
    unreported = make_snapshot(
        snapshot_date=date(2026, 10, 7),
        market_data_status=None,
        market_data_raw="BRAND_NEW_FLAG",
        implied_vol=None,
        implied_vol_source=None,
    )
    repos.upsert_snapshots(seeded, [full, unreported])
    assert repos.snapshots(seeded, SPY, as_of=AS_OF) == (unreported, full)


def test_writing_a_snapshot_again_replaces_that_day(
    seeded: Store, make_snapshot: Callable[..., Snapshot]
) -> None:
    repos.upsert_snapshots(seeded, [make_snapshot(last=100.0)])
    repos.upsert_snapshots(seeded, [make_snapshot(last=101.0, source="mcp")])
    (only,) = repos.snapshots(seeded, SPY, as_of=AS_OF)
    assert (only.last, only.source) == (101.0, "mcp")


def test_snapshots_keep_the_newest_when_limited(
    seeded: Store, make_snapshot: Callable[..., Snapshot]
) -> None:
    repos.upsert_snapshots(
        seeded, [make_snapshot(snapshot_date=date(2026, 10, d)) for d in range(1, 8)]
    )
    newest = repos.snapshots(seeded, SPY, as_of=date(2026, 10, 6), limit=2)
    assert [s.snapshot_date.day for s in newest] == [5, 6]


def test_the_counts_of_snapshots_differ_by_whether_a_vol_was_quoted(
    seeded: Store, make_snapshot: Callable[..., Snapshot]
) -> None:
    repos.upsert_snapshots(
        seeded,
        [
            make_snapshot(snapshot_date=date(2026, 10, 1)),
            make_snapshot(
                snapshot_date=date(2026, 10, 2), implied_vol=None, implied_vol_source=None
            ),
            make_snapshot(snapshot_date=date(2026, 10, 3)),
        ],
    )
    assert repos.snapshot_count(seeded, SPY, as_of=AS_OF) == 3
    assert repos.implied_vol_snapshot_count(seeded, SPY, as_of=AS_OF) == 2
    assert repos.snapshot_count(seeded, OTHER, as_of=AS_OF) == 0


# ── option chains ──


def _chain(make_chain_row: Callable[..., ChainRow]) -> list[ChainRow]:
    return [
        make_chain_row(strike=105.0, right=OptionRight.PUT),
        make_chain_row(strike=100.0, right=OptionRight.PUT),
        make_chain_row(strike=105.0, right=OptionRight.CALL),
        make_chain_row(strike=100.0, right=OptionRight.CALL),
        make_chain_row(strike=100.0, expiry=date(2026, 12, 18)),
    ]


def test_a_chain_round_trips_in_a_stable_order(
    seeded: Store, make_chain_row: Callable[..., ChainRow]
) -> None:
    rows = _chain(make_chain_row)
    assert repos.upsert_chain_rows(seeded, rows) == 5
    read = repos.chain_rows(seeded, as_of=AS_OF, underlying_conid=SPY)
    assert [(r.expiry, r.right.value, r.strike) for r in read] == [
        (date(2026, 12, 18), "C", 100.0),
        (date(2027, 1, 15), "C", 100.0),
        (date(2027, 1, 15), "C", 105.0),
        (date(2027, 1, 15), "P", 100.0),
        (date(2027, 1, 15), "P", 105.0),
    ]
    assert set(read) == set(rows)


def test_a_chain_row_with_every_optional_field_round_trips(
    seeded: Store, make_chain_row: Callable[..., ChainRow]
) -> None:
    row = make_chain_row(
        option_conid=99,
        option_symbol="SPY   270115C00100000",
        sigma_used=0.2,
        sigma_source="realised_30d",
        market_data_status=MarketDataStatus.FROZEN,
        market_data_raw="FROZEN_DELAYED",
        quote_delay_seconds=900,
    )
    repos.upsert_chain_rows(seeded, [row])
    assert repos.chain_rows(seeded, as_of=AS_OF) == (row,)


def test_writing_a_chain_row_again_replaces_it(
    seeded: Store, make_chain_row: Callable[..., ChainRow]
) -> None:
    repos.upsert_chain_rows(seeded, [make_chain_row(bid=5.0, ask=5.2)])
    repos.upsert_chain_rows(seeded, [make_chain_row(bid=6.0, ask=6.4, open_interest=7)])
    (row,) = repos.chain_rows(seeded, as_of=AS_OF)
    assert (row.bid, row.ask, row.open_interest) == (6.0, 6.4, 7)


def test_the_default_chain_is_the_newest_by_the_bound_and_can_be_narrowed(
    seeded: Store, make_chain_row: Callable[..., ChainRow]
) -> None:
    repos.upsert_chain_rows(
        seeded,
        [
            make_chain_row(snapshot_date=date(2026, 10, 6)),
            make_chain_row(snapshot_date=date(2026, 10, 8)),
            make_chain_row(snapshot_date=date(2026, 10, 8), underlying_conid=OTHER),
            make_chain_row(snapshot_date=date(2026, 10, 8), expiry=date(2026, 12, 18)),
        ],
    )
    assert repos.latest_chain_date(seeded, as_of=AS_OF) == date(2026, 10, 8)
    assert repos.latest_chain_date(seeded, as_of=date(2026, 10, 7)) == date(2026, 10, 6)
    assert repos.latest_chain_date(seeded, as_of=date(2026, 10, 5)) is None
    assert len(repos.chain_rows(seeded, as_of=AS_OF)) == 3
    assert len(repos.chain_rows(seeded, as_of=AS_OF, underlying_conid=OTHER)) == 1
    assert len(repos.chain_rows(seeded, as_of=AS_OF, expiry=date(2026, 12, 18))) == 1
    assert len(repos.chain_rows(seeded, as_of=AS_OF, snapshot_date=date(2026, 10, 6))) == 1
    assert repos.chain_rows(seeded, as_of=date(2026, 10, 5)) == ()
    assert repos.chain_row_count(seeded, as_of=AS_OF) == 4
    assert repos.chain_row_count(seeded, as_of=AS_OF, underlying_conid=OTHER) == 1


def test_asking_for_a_chain_from_after_the_bound_raises(
    seeded: Store, make_chain_row: Callable[..., ChainRow]
) -> None:
    repos.upsert_chain_rows(seeded, [make_chain_row()])
    with pytest.raises(StoreError, match="after as_of"):
        repos.chain_rows(seeded, as_of=date(2026, 10, 7), snapshot_date=date(2026, 10, 8))


# ── curves ──


def _curve(make_curve_point: Callable[..., CurvePoint]) -> list[CurvePoint]:
    return [
        make_curve_point(expiry=date(2027, 1, 19), price=71.0),
        make_curve_point(expiry=date(2026, 11, 20), price=69.0),
        make_curve_point(expiry=date(2026, 12, 21), price=70.0),
    ]


def test_a_curve_reads_front_expiry_first(
    store: Store, make_curve_point: Callable[..., CurvePoint]
) -> None:
    points = _curve(make_curve_point)
    assert repos.upsert_curve_points(store, points) == 3
    read = repos.curve(store, "CL", as_of=AS_OF)
    assert [p.expiry for p in read] == sorted(p.expiry for p in points)
    assert set(read) == set(points)
    assert repos.curve(store, "NG", as_of=AS_OF) == ()


def test_a_curve_point_with_every_field_round_trips(
    store: Store, make_curve_point: Callable[..., CurvePoint]
) -> None:
    point = make_curve_point(conid=555, market_data_status=None, market_data_raw="NEW")
    repos.upsert_curve_points(store, [point])
    assert repos.curve(store, "CL", as_of=AS_OF) == (point,)


def test_writing_a_curve_again_replaces_it(
    store: Store, make_curve_point: Callable[..., CurvePoint]
) -> None:
    repos.upsert_curve_points(store, [make_curve_point(price=70.0)])
    repos.upsert_curve_points(store, [make_curve_point(price=72.0)])
    (point,) = repos.curve(store, "CL", as_of=AS_OF)
    assert point.price == 72.0


def test_the_curve_in_force_is_the_newest_by_the_bound(
    store: Store, make_curve_point: Callable[..., CurvePoint]
) -> None:
    repos.upsert_curve_points(
        store,
        [
            make_curve_point(snapshot_date=date(2026, 10, 6), price=60.0),
            make_curve_point(snapshot_date=date(2026, 10, 8), price=62.0),
        ],
    )
    assert repos.latest_curve_date(store, "CL", as_of=AS_OF) == date(2026, 10, 8)
    assert repos.latest_curve_date(store, "CL", as_of=date(2026, 10, 7)) == date(2026, 10, 6)
    assert repos.latest_curve_date(store, "CL", as_of=date(2026, 10, 1)) is None
    (older,) = repos.curve(store, "CL", as_of=date(2026, 10, 7))
    assert older.price == 60.0
    (named,) = repos.curve(store, "CL", as_of=AS_OF, snapshot_date=date(2026, 10, 6))
    assert named.price == 60.0
    with pytest.raises(StoreError, match="after as_of"):
        repos.curve(store, "CL", as_of=date(2026, 10, 7), snapshot_date=date(2026, 10, 8))


def test_the_front_history_never_splices_two_contracts(
    store: Store, make_curve_point: Callable[..., CurvePoint]
) -> None:
    repos.upsert_curve_points(
        store,
        [
            make_curve_point(
                snapshot_date=date(2026, 10, 5), expiry=date(2026, 11, 20), price=60.0
            ),
            make_curve_point(
                snapshot_date=date(2026, 10, 5), expiry=date(2026, 12, 21), price=61.0
            ),
            # the front has no price this day: the second month must not stand in for it
            make_curve_point(
                snapshot_date=date(2026, 10, 6), expiry=date(2026, 11, 20), price=None
            ),
            make_curve_point(
                snapshot_date=date(2026, 10, 6), expiry=date(2026, 12, 21), price=62.0
            ),
            make_curve_point(
                snapshot_date=date(2026, 10, 7), expiry=date(2026, 11, 20), price=63.0
            ),
            make_curve_point(
                snapshot_date=date(2026, 10, 8), expiry=date(2026, 11, 20), price=64.0
            ),
        ],
    )
    history = repos.curve_front_history(store, "CL", as_of=date(2026, 10, 7))
    assert history == ((date(2026, 10, 5), 60.0), (date(2026, 10, 7), 63.0))
    later = repos.curve_front_history(store, "CL", as_of=AS_OF, since=date(2026, 10, 7))
    assert later == ((date(2026, 10, 7), 63.0), (date(2026, 10, 8), 64.0))


# ── macro ──


def test_a_macro_observation_round_trips_through_a_series(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    observation = make_observation()
    assert repos.upsert_macro(store, [observation]) == 1
    series = repos.macro_series(store, "CPI_X", as_of=AS_OF)
    assert series.observations == (observation,)
    assert series.source == "fred"
    assert series.as_of == AS_OF
    assert series.unaligned == 0
    assert len(series) == 1


def test_a_revision_overwrites_the_period(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(store, [make_observation(value=1.0)])
    repos.upsert_macro(
        store, [make_observation(value=1.2, release_date=date(2026, 9, 1), units="index")]
    )
    series = repos.macro_series(store, "CPI_X", as_of=AS_OF)
    (only,) = series.observations
    assert (only.value, only.release_date, only.units) == (1.2, date(2026, 9, 1), "index")


def test_a_series_asked_for_by_source_or_since_is_narrowed(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(
        store,
        [
            make_observation(reference_date=date(2026, 5, 1), release_date=date(2026, 6, 10)),
            make_observation(reference_date=date(2026, 6, 1), release_date=date(2026, 7, 10)),
            make_observation(source="oecd", reference_date=date(2026, 6, 1), value=9.0),
        ],
    )
    fred = repos.macro_series(store, "CPI_X", as_of=AS_OF, source="fred", since=date(2026, 6, 1))
    assert [o.reference_date for o in fred.observations] == [date(2026, 6, 1)]
    oecd = repos.macro_series(store, "CPI_X", as_of=AS_OF, source="oecd")
    assert [o.value for o in oecd.observations] == [9.0]
    assert repos.macro_series(store, "NO_SUCH", as_of=AS_OF).source is None


def test_a_series_from_several_sources_must_be_asked_for_by_one(
    store: Store, make_observation: Callable[..., MacroObservation]
) -> None:
    repos.upsert_macro(store, [make_observation(), make_observation(source="oecd", value=2.0)])
    with pytest.raises(StoreError, match="several sources \\(fred, oecd\\)"):
        repos.macro_series(store, "CPI_X", as_of=AS_OF)


def test_an_unreadable_macro_stamp_raises(store: Store) -> None:
    store.execute(
        "INSERT INTO macro VALUES ('X', 'fred', '2026-07-01', '2026-08-12', 1.0, NULL, NULL, "
        "'2026-13-45T25:61:61Z')"
    )
    with pytest.raises(StoreError, match=r"macro\.collected_at holds"):
        repos.macro_series(store, "X", as_of=AS_OF)
