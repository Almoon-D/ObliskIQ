"""The curve collector and the sign of annualised roll.

Curve shape history is not retrievable retrospectively, exactly as with option
chains. And the sign convention is the whole payload: get it backwards and every
downstream wrapper recommendation inverts — a futures-tracking product gets
recommended into the contango that destroys it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from obliskiq.collect.curve_collector import CurveCollector, annualised_roll, tracked_roots
from obliskiq.collect.runlog import CollectorRun, RunStatus
from obliskiq.config import Config

SESSION = date(2026, 1, 15)
CALENDAR_DAYS_PER_YEAR = 365.0


def config_for(config, **curve_overrides) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    data["collectors"]["curve"].update(curve_overrides)
    return Config(data)


async def collect(store, cfg, market_data, *, only=None, session=SESSION):
    with CollectorRun(store, "curve") as run:
        await CurveCollector(store, cfg, market_data).run(run, only=only, today=session)
    return run


# ── the sign convention ──


def test_backwardation_is_positive():
    """Front dearer than deferred: rolling forward buys the cheaper contract.
    Supportive of long exposure."""
    roll = annualised_roll(
        front_price=80.0,
        deferred_price=78.0,
        days_apart=90,
        calendar_days_per_year=CALENDAR_DAYS_PER_YEAR,
    )
    assert roll is not None and roll > 0


def test_contango_is_negative():
    """Front cheaper than deferred: rolling forward buys the dearer contract. This
    is the bleed that kills a futures-tracking wrapper over a 90-200 day hold."""
    roll = annualised_roll(
        front_price=78.0,
        deferred_price=80.0,
        days_apart=90,
        calendar_days_per_year=CALENDAR_DAYS_PER_YEAR,
    )
    assert roll is not None and roll < 0


def test_a_flat_curve_is_zero():
    assert annualised_roll(
        front_price=80.0,
        deferred_price=80.0,
        days_apart=90,
        calendar_days_per_year=CALENDAR_DAYS_PER_YEAR,
    ) == pytest.approx(0.0)


def test_the_roll_is_annualised_so_a_nearer_expiry_implies_a_steeper_rate():
    """The same price gap over less time is a steeper annualised curve."""
    near = annualised_roll(
        front_price=80.0,
        deferred_price=78.0,
        days_apart=30,
        calendar_days_per_year=CALENDAR_DAYS_PER_YEAR,
    )
    far = annualised_roll(
        front_price=80.0,
        deferred_price=78.0,
        days_apart=180,
        calendar_days_per_year=CALENDAR_DAYS_PER_YEAR,
    )
    assert near > far > 0


@pytest.mark.parametrize(
    ("front", "deferred", "days"),
    [(0.0, 80.0, 90), (80.0, 0.0, 90), (-1.0, 80.0, 90), (80.0, 78.0, 0), (80.0, 78.0, -5)],
)
def test_an_uncomputable_roll_is_none_not_zero(front, deferred, days):
    """A curve point with an unusable price is MISSING, not flat. A fictional flat
    point drags every downstream contango classification toward 'fine'."""
    assert (
        annualised_roll(
            front_price=front,
            deferred_price=deferred,
            days_apart=days,
            calendar_days_per_year=CALENDAR_DAYS_PER_YEAR,
        )
        is None
    )


# ── the stored curve ──


async def test_the_full_expiry_ladder_is_stored(seeded_universe, config, market_data):
    await collect(seeded_universe, config_for(config), market_data, only=["CL"])
    rows = seeded_universe.execute(
        "SELECT expiry, price, annualised_roll FROM curve_snapshots WHERE root = 'CL' "
        "ORDER BY expiry"
    ).fetchall()
    assert len(rows) > 1
    assert all(row["price"] is not None for row in rows)


async def test_the_front_contract_has_no_roll(seeded_universe, config, market_data):
    """There is nothing to roll it against. Writing 0.0 would put a fictional flat
    point into every curve."""
    await collect(seeded_universe, config_for(config), market_data, only=["CL"])
    rows = seeded_universe.execute(
        "SELECT annualised_roll FROM curve_snapshots WHERE root = 'CL' ORDER BY expiry"
    ).fetchall()
    assert rows[0]["annualised_roll"] is None
    assert all(row["annualised_roll"] is not None for row in rows[1:])


async def test_a_backwardated_root_stores_positive_rolls(seeded_universe, config, market_data):
    await collect(seeded_universe, config_for(config), market_data, only=["CL"])
    rolls = [
        row[0]
        for row in seeded_universe.execute(
            "SELECT annualised_roll FROM curve_snapshots "
            "WHERE root = 'CL' AND annualised_roll IS NOT NULL"
        )
    ]
    assert rolls and all(roll > 0 for roll in rolls)


async def test_a_contangoed_root_stores_negative_rolls(seeded_universe, config, market_data):
    await collect(seeded_universe, config_for(config), market_data, only=["NG"])
    rolls = [
        row[0]
        for row in seeded_universe.execute(
            "SELECT annualised_roll FROM curve_snapshots "
            "WHERE root = 'NG' AND annualised_roll IS NOT NULL"
        )
    ]
    assert rolls and all(roll < 0 for roll in rolls)


async def test_a_contango_beyond_the_config_threshold_is_detectable_downstream(
    seeded_universe, config, market_data
):
    """The collector stores the shape and does not judge it; this asserts the stored
    number is on the scale the Session 3 threshold is expressed in."""
    await collect(seeded_universe, config_for(config), market_data, only=["NG"])
    threshold = config.require_float(
        "commodities.term_structure.contango_max_annualised_for_futures_wrapper"
    )
    worst = seeded_universe.execute(
        "SELECT MIN(annualised_roll) FROM curve_snapshots WHERE root = 'NG'"
    ).fetchone()[0]
    assert -worst > threshold


async def test_a_root_with_a_single_expiry_still_records_its_point(
    seeded_universe, config, market_data
):
    """No curve shape, but the price is still worth having — and the absence of a
    second rung is itself the finding."""
    run = await collect(seeded_universe, config_for(config), market_data, only=["CC"])
    rows = seeded_universe.execute(
        "SELECT annualised_roll FROM curve_snapshots WHERE root = 'CC'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["annualised_roll"] is None
    assert run.status() == RunStatus.OK


async def test_the_expiry_cap_is_honoured(seeded_universe, config, market_data):
    await collect(
        seeded_universe, config_for(config, max_expiries_per_root=2), market_data, only=["CL"]
    )
    assert (
        seeded_universe.execute(
            "SELECT COUNT(*) FROM curve_snapshots WHERE root = 'CL'"
        ).fetchone()[0]
        == 2
    )


# ── idempotency, provenance and partial runs ──


async def test_re_running_the_same_day_updates_in_place(seeded_universe, config, market_data):
    cfg = config_for(config)
    await collect(seeded_universe, cfg, market_data, only=["CL"])
    before = seeded_universe.execute("SELECT COUNT(*) FROM curve_snapshots").fetchone()[0]
    await collect(seeded_universe, cfg, market_data, only=["CL"])
    assert seeded_universe.execute("SELECT COUNT(*) FROM curve_snapshots").fetchone()[0] == before


async def test_a_second_day_appends(seeded_universe, config, market_data):
    cfg = config_for(config)
    await collect(seeded_universe, cfg, market_data, only=["CL"], session=SESSION)
    await collect(
        seeded_universe, cfg, market_data, only=["CL"], session=SESSION + timedelta(days=1)
    )
    dates = {
        row[0]
        for row in seeded_universe.execute("SELECT DISTINCT snapshot_date FROM curve_snapshots")
    }
    assert len(dates) == 2


async def test_the_collection_route_is_recorded(seeded_universe, config, market_data):
    await collect(seeded_universe, config_for(config), market_data, only=["CL"])
    sources = {
        row[0] for row in seeded_universe.execute("SELECT DISTINCT source FROM curve_snapshots")
    }
    assert sources == {market_data.source}


async def test_one_failing_root_does_not_kill_the_run(seeded_universe, config, market_data):
    """CL has a fixture, ZC does not."""
    run = await collect(seeded_universe, config_for(config), market_data, only=["CL", "ZC"])
    assert run.status() == RunStatus.PARTIAL
    assert run.targets_ok == 1 and run.targets_failed == 1
    assert seeded_universe.execute("SELECT COUNT(*) FROM curve_snapshots").fetchone()[0] > 0


def test_only_tracked_roots_are_walked(seeded_universe):
    with seeded_universe.transaction() as connection:
        connection.execute("UPDATE futures_roots SET tracked = 0 WHERE root = 'CL'")
    roots = {row["root"] for row in tracked_roots(seeded_universe)}
    assert "CL" not in roots and roots


def test_futures_data_use_remains_permitted_by_config(config):
    """A futures contract is a prohibited INSTRUMENT. The curve is a DATA call and
    must stay callable, or wrapper selection loses its only input."""
    assert config.require_str("instruments.futures_data_use") == "allowed"
    assert "futures_contracts" in config.require_list("instruments.prohibited")


# ── a ladder with no prices on it is not a curve ──


class UnpricedLadder:
    """The live COMEX behaviour: the ladder lists, every snapshot returns nothing.

    Observed 2026-08-24 — ``get_price_snapshot`` on COMEX gold returned ``{}``
    outright while the same contract's daily bars served at ``delayed: 600``. An
    entitlement gap, not a transient one, and the shape a collector has to survive.
    """

    def __init__(self, inner):
        self._inner = inner

    @property
    def source(self) -> str:
        return self._inner.source

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def quotes(self, conids):
        return {}


async def test_a_curve_with_no_priced_rung_is_degraded_not_ok(seeded_universe, config, market_data):
    """`ok` here would put a healthy line in the log on a night that collected nothing.

    The expiry ladder is retrievable at any time. The PRICES are the part that has
    to be taken today, and roll yield cannot be computed from none of them.
    """
    run = await collect(
        seeded_universe, config_for(config), UnpricedLadder(market_data), only=["CL"]
    )

    assert run.status() == RunStatus.DEGRADED
    assert run.degraded_targets, "no target was marked degraded"
    reason = next(o.reason for o in run.outcomes if o.degraded)
    assert "not one rung carried a price" in reason


async def test_a_priced_curve_is_still_plain_ok(seeded_universe, config, market_data):
    """The guard must not fire on the healthy path."""
    run = await collect(seeded_universe, config_for(config), market_data, only=["CL"])
    assert run.status() == RunStatus.OK


async def test_the_rows_are_still_written_when_nothing_is_priced(
    seeded_universe, config, market_data
):
    """Degraded is a report about quality, not a refusal to record what was seen.

    The listed ladder is worth keeping: it says which contracts existed on the
    day, which is how a later backfill from per-contract history knows what to
    ask for.
    """
    await collect(seeded_universe, config_for(config), UnpricedLadder(market_data), only=["CL"])
    rows = seeded_universe.execute("SELECT price, annualised_roll FROM curve_snapshots").fetchall()
    assert rows, "a degraded run wrote nothing at all"
    assert all(row["price"] is None for row in rows)
