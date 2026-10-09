"""Point-in-time discipline: what was KNOWN on the day, not what the period was.

``macro`` carries both ``reference_date`` (the period an observation describes)
and ``release_date`` (when it became public). Aligning a CPI print to its
reference month means every percentile derived from it knows the number weeks
before the market did. The schema says so, the collectors honour it, and
``MacroSeries.known_at`` implements it.

These tests exist because none of that was actually wired to the compute layer,
and because every pre-existing compute fixture set ``release_date =
reference_date``, which made the divergence unobservable. Fixtures here always
set the two dates APART.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date

import pytest

from obliskiq.compute.frames import macro_series
from obliskiq.compute.fx_engine import BaseCurrencyRates
from obliskiq.macro.series import SeriesSpec, load_series, load_source_policies


def _macro(store, rows):
    """Seed macro rows. ``release`` is deliberately never equal to ``reference``."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


# ── the accessor itself ───────────────────────────────────────────────────────


def test_known_at_ignores_an_observation_not_yet_published(store):
    """The August print exists in the table but was not public on the 24th."""
    _macro(
        store,
        [
            ("CPI_X", "fred", "2026-07-01", "2026-08-12", 2.1, "monthly"),
            ("CPI_X", "fred", "2026-08-01", "2026-09-11", 2.4, "monthly"),
        ],
    )
    series = macro_series(store, "CPI_X")

    known = series.known_at(date(2026, 8, 24))
    assert known is not None and known.value == pytest.approx(2.1)

    # The reference-dated accessor picks up the unpublished August figure. This
    # is the look-ahead the two accessors exist to distinguish.
    by_reference = series.value_on_or_before(date(2026, 8, 24))
    assert by_reference is not None and by_reference.value == pytest.approx(2.4)


def test_known_at_excludes_a_null_release_rather_than_assuming_one(store):
    _macro(store, [("ECB_X", "ecb", "2026-07-01", None, 5.0, "monthly")])
    series = macro_series(store, "ECB_X")
    assert series.known_at(date(2026, 8, 24)) is None
    assert series.unaligned_count == 1


def test_unaligned_count_reports_the_coverage_gap(store):
    _macro(
        store,
        [
            ("MIX", "fred", "2026-06-01", "2026-07-10", 1.0, "monthly"),
            ("MIX", "fred", "2026-07-01", None, 2.0, "monthly"),
        ],
    )
    series = macro_series(store, "MIX")
    assert series.unaligned_count == 1
    assert len(series.observations) == 2


# ── the ECB FX policy override (the reason the census price gate went dark) ───


def test_ecb_daily_fx_declares_its_release_date_per_series():
    """ECB's source policy is `unknown`; three kinds of series override it.

    A source-wide setting has to cover everything ECB serves, and `unknown` is
    the right default for a publisher that serves no release date — but a NULL
    release date makes a series INVISIBLE to `known_at`, so an override is the
    difference between collected-and-read and collected-and-ignored. Measured
    2026-09-11: ECB_DFR held 3,652 rows current to that day while eurozone,
    germany and spain all reported it "absent from the store".

    Each override is the publisher's own rule, and the SOURCE DEFAULT is pinned
    too: a series that declares no override must still come out undated, because
    that default is what keeps an undated publisher from being quietly dated.
    """
    from pathlib import Path

    from obliskiq.config import load_config

    config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    specs = {s.id: s for s in load_series(config)}
    policies = load_source_policies(config)
    reference = date(2026, 8, 21)

    # EURCAD replaces EURZAR here: south_africa was dropped from the market
    # axis in Phase 8 and its series with it. The point of naming four is that
    # the per-series override covers the whole family, not one member of it.
    for fx_series in ("EURUSD", "EURGBP", "EURJPY", "EURCAD"):
        spec = specs[fx_series]
        effective = policies[spec.source].for_series(spec)
        assert effective.release_date_for(reference) == "2026-08-21"

    # A policy rate LEVEL in force on a date is public on that date.
    dfr = policies[specs["ECB_DFR"].source].for_series(specs["ECB_DFR"])
    assert dfr.release_date_for(reference) == "2026-08-21"

    # €STR is published at 08:00 CET on the next TARGET business day.
    estr = policies[specs["ESTR"].source].for_series(specs["ESTR"])
    assert estr.release_date_for(reference) == "2026-08-22"

    # And the default the three above override. HICP_YOY used to pin this — an
    # ECB series carrying no override, undated on purpose because no fixed lag is
    # Eurostat's rule and this project does not invent one. That series is gone
    # (the flow stopped at 2025-12; eurozone reads Eurostat via FRED now), so the
    # decision is pinned against the source policy itself rather than against
    # whichever series happens to be declared on it.
    default = policies["ecb"].for_series(
        SeriesSpec(id="UNDECLARED", source="ecb", frequency="monthly", group="euro")
    )
    assert default.release_date_for(reference) is None


def test_a_dated_conversion_works_for_a_non_base_currency(store, config):
    """The regression this override fixes.

    ``UniverseCensus.run`` takes a required ``as_of`` and passes it all the way
    down to ``to_base``. With ECB rates carrying a NULL release date, every
    non-base conversion returned None, every non-EUR price-ceiling gate went
    UNTESTED, and no contract could ever be `fully_tested` — so the census
    reported an empty universe for a plumbing reason.
    """
    _macro(store, [("EURUSD", "ecb", "2026-08-21", "2026-08-21", 1.17468, "daily")])
    rates = BaseCurrencyRates(store, config)

    dated = rates.to_base("USD", as_of=date(2026, 8, 24))
    assert dated is not None
    assert dated == pytest.approx(1 / 1.17468)
    # The undated path always worked; the two must now agree.
    assert rates.to_base("USD") == pytest.approx(dated)


def test_a_conversion_dated_before_the_rate_was_published_is_refused(store, config):
    _macro(store, [("EURUSD", "ecb", "2026-08-21", "2026-08-21", 1.17468, "daily")])
    rates = BaseCurrencyRates(store, config)
    assert rates.to_base("USD", as_of=date(2026, 8, 20)) is None


def test_the_base_currency_needs_no_rate_at_all(store, config):
    rates = BaseCurrencyRates(store, config)
    assert rates.to_base("EUR", as_of=date(2026, 8, 24)) == pytest.approx(1.0)


# ── the regression the old fixtures could not see ─────────────────────────────
#
# Every pre-existing compute fixture seeded release_date == reference_date, which
# makes known_at and value_on_or_before return the same row and the whole bug
# invisible. These set the dates APART, so they fail against a reference-aligned
# read and pass against a release-aligned one.


def test_cpi_uses_the_print_that_was_out_not_the_month_it_describes(store, config):
    """The August CPI is dated 2026-08-01 and published 2026-09-11.

    Read by reference date, a session on 2026-08-24 picks it up eighteen days
    before it exists. That figure feeds policy_rate_real and the whole four-way
    FX decomposition, so the bias is not confined to one cell.
    """
    from obliskiq.compute.frames import cpi_yoy

    _macro(
        store,
        [
            ("CPI_DE", "fred", "2026-07-01", "2026-08-12", 2.1, "monthly"),
            ("CPI_DE", "fred", "2026-08-01", "2026-09-11", 2.9, "monthly"),
        ],
    )
    spec = {"cpi_series": "CPI_DE", "cpi_is_yoy": True}

    on_the_day = cpi_yoy(store, spec, as_of=date(2026, 8, 24), calendar_days_per_year=365.0)
    assert on_the_day == pytest.approx(0.021), "picked up a print that was not published yet"

    # Once September's release date has passed, the newer print is legitimate.
    later = cpi_yoy(store, spec, as_of=date(2026, 9, 12), calendar_days_per_year=365.0)
    assert later == pytest.approx(0.029)


def test_a_percentile_excludes_observations_not_yet_published(store):
    """values_known_at is the list form, and it is what the percentiles use."""
    from obliskiq.compute.frames import macro_series

    _macro(
        store,
        [
            ("COT_X", "cftc", "2026-08-04", "2026-08-07", 10.0, "weekly"),
            ("COT_X", "cftc", "2026-08-11", "2026-08-14", 20.0, "weekly"),
            ("COT_X", "cftc", "2026-08-18", "2026-08-21", 30.0, "weekly"),
            # Tuesday's report, not public until the Friday.
            ("COT_X", "cftc", "2026-08-25", "2026-08-28", 99.0, "weekly"),
        ],
    )
    series = macro_series(store, "COT_X")

    known = series.values_known_at(date(2026, 8, 26))
    assert known == [10.0, 20.0, 30.0]
    assert 99.0 not in known

    # By reference date the unpublished report is in the sample, and because it
    # is an extreme it would drag the percentile with it.
    by_reference = [o.value for o in series.observations if o.reference_date <= date(2026, 8, 26)]
    assert 99.0 in by_reference


def _macro_with_units(store, rows):
    """Seed macro rows carrying units, which ``_macro`` leaves NULL."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency, units) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_units_known_at_sees_a_definition_change_inside_the_window(store):
    """A percentile ranks a value against others of the SAME quantity.

    Migration 015 changed CFTC positioning from a net contract COUNT to a share
    of open interest. A store holding both would rank counts in the tens of
    thousands against shares in the tenths, putting every old row at the top of
    the window and every new one at the bottom — and it reads as an ordinary
    number all the way to the output. The migration deletes those rows; this is
    what stops the same mistake being reintroduced silently by a later change.
    """
    from obliskiq.compute.frames import macro_series

    _macro_with_units(
        store,
        [
            ("COT_U", "cftc", "2026-08-04", "2026-08-07", 185000.0, "weekly", "contracts_net"),
            ("COT_U", "cftc", "2026-08-11", "2026-08-14", 0.10, "weekly", "share_of_oi"),
        ],
    )
    series = macro_series(store, "COT_U")
    assert series.units_known_at(date(2026, 8, 20)) == {"contracts_net", "share_of_oi"}


def test_one_definition_over_the_window_is_not_flagged(store):
    from obliskiq.compute.frames import macro_series

    _macro_with_units(
        store,
        [
            ("COT_U", "cftc", "2026-08-04", "2026-08-07", 0.08, "weekly", "share_of_oi"),
            ("COT_U", "cftc", "2026-08-11", "2026-08-14", 0.10, "weekly", "share_of_oi"),
        ],
    )
    assert len(macro_series(store, "COT_U").units_known_at(date(2026, 8, 20))) == 1


def test_the_units_check_is_bounded_at_as_of_like_every_other_read(store):
    """A definition change published AFTER as_of cannot retroactively spoil a
    window that was single-units at the time."""
    from obliskiq.compute.frames import macro_series

    _macro_with_units(
        store,
        [
            ("COT_U", "cftc", "2026-08-04", "2026-08-07", 185000.0, "weekly", "contracts_net"),
            ("COT_U", "cftc", "2026-08-25", "2026-08-28", 0.10, "weekly", "share_of_oi"),
        ],
    )
    series = macro_series(store, "COT_U")
    assert series.units_known_at(date(2026, 8, 20)) == {"contracts_net"}


def test_an_undeclared_unit_cannot_contradict_a_declared_one(store):
    """NULL units is a gap, like a missing release date. It is not a second
    definition, and treating it as one would flag every pre-units series."""
    from obliskiq.compute.frames import macro_series

    _macro_with_units(
        store,
        [
            ("COT_U", "cftc", "2026-08-04", "2026-08-07", 0.08, "weekly", None),
            ("COT_U", "cftc", "2026-08-11", "2026-08-14", 0.10, "weekly", "share_of_oi"),
        ],
    )
    assert macro_series(store, "COT_U").units_known_at(date(2026, 8, 20)) == {"share_of_oi"}


def test_observations_known_at_carries_the_release_dates_under_the_same_rule(store):
    """The positioning CHANGE needs the dates, not only the values.

    A change over a window in weeks has to know WHEN each endpoint was published,
    so it reads observations rather than values — and reading them means the
    point-in-time rule has to hold in the same place. It is the one filter
    ``values_known_at`` is built on, so the two cannot diverge.
    """
    from obliskiq.compute.frames import macro_series

    _macro(
        store,
        [
            ("COT_X", "cftc", "2026-08-04", "2026-08-07", 10.0, "weekly"),
            ("COT_X", "cftc", "2026-08-11", "2026-08-14", 20.0, "weekly"),
            ("COT_X", "cftc", "2026-08-18", None, 30.0, "weekly"),
            # Tuesday's report, not public until the Friday.
            ("COT_X", "cftc", "2026-08-25", "2026-08-28", 99.0, "weekly"),
        ],
    )
    series = macro_series(store, "COT_X")
    known = series.observations_known_at(date(2026, 8, 26))

    assert [o.value for o in known] == [10.0, 20.0]
    assert [o.release_date for o in known] == [date(2026, 8, 7), date(2026, 8, 14)]
    assert [o.value for o in known] == series.values_known_at(date(2026, 8, 26)), (
        "the two readers must not be able to disagree about what was published"
    )


def test_a_series_with_no_release_dates_yields_nothing_point_in_time(store):
    """The ECB's slower series carry no publication date, by policy.

    HICP is the live case: the ECB Data Portal states no first-publication date
    for it, so ``macro.sources.ecb.release_date_policy`` is ``unknown`` and the
    rows store NULL rather than a reference date wearing a release date's name.
    They go n/a rather than silently reference-aligned, and unaligned_count is
    what tells a reader that is why.
    """
    from obliskiq.compute.frames import macro_series

    _macro(
        store,
        [
            ("HICP_YOY", "ecb", "2026-06-01", None, 100.1, "monthly"),
            ("HICP_YOY", "ecb", "2026-07-01", None, 100.4, "monthly"),
        ],
    )
    series = macro_series(store, "HICP_YOY")
    assert series.values_known_at(date(2026, 8, 24)) == []
    assert series.unaligned_count == 2


# ── the store reads, bounded by as_of ─────────────────────────────────────────
#
# bar_series had `ts >= since` and no upper bound; underlying_snapshots,
# latest_chain_date and recent_daily_closes had no date filter at all. A grid or
# screen rebuilt for a past date therefore read data recorded after it.


def _bars(store, contract_id, rows):
    with store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO universe (contract_id, symbol, currency, asset_class, "
            "exchange) VALUES (?, 'X', 'EUR', 'equity', 'FTA')",
            (contract_id,),
        )
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, '1d', ?, 'mcp')",
            [(contract_id, f"{day}T07:00:00Z", close) for day, close in rows],
        )


def test_bar_series_stops_at_the_session_it_is_asked_for(store):
    from obliskiq.compute.frames import bar_series

    _bars(
        store,
        7,
        [("2026-08-20", 100.0), ("2026-08-21", 101.0), ("2026-08-24", 102.0)],
    )

    unbounded = bar_series(store, 7)
    assert len(unbounded.close) == 3

    bounded = bar_series(store, 7, until=date(2026, 8, 21))
    assert bounded.close == [100.0, 101.0]


def test_the_bound_keeps_the_session_it_names(store):
    """ts carries a time, so a naive '<=' would drop the as_of day itself."""
    from obliskiq.compute.frames import bar_series

    _bars(store, 8, [("2026-08-21", 101.0), ("2026-08-24", 102.0)])
    bounded = bar_series(store, 8, until=date(2026, 8, 24))
    assert bounded.close == [101.0, 102.0], "the as_of session was dropped by its own bound"


def test_recent_daily_closes_stops_at_the_session_it_is_asked_for(store):
    from obliskiq.collect.vol import recent_daily_closes

    _bars(
        store,
        9,
        [("2026-08-20", 100.0), ("2026-08-21", 101.0), ("2026-08-24", 102.0)],
    )
    assert recent_daily_closes(store, 9, limit=10) == [100.0, 101.0, 102.0]
    assert recent_daily_closes(store, 9, limit=10, until=date(2026, 8, 21)) == [100.0, 101.0]


def test_latest_chain_date_cannot_return_a_snapshot_from_the_future(store):
    from obliskiq.compute.frames import latest_chain_date

    with store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO universe (contract_id, symbol, currency, asset_class, "
            "exchange) VALUES (11, 'Y', 'EUR', 'equity', 'FTA')"
        )
        connection.executemany(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, right, "
            "source) VALUES (?, 11, '2026-12-18', 100.0, 'C', 'mcp')",
            [("2026-08-17",), ("2026-08-24",)],
        )

    assert latest_chain_date(store) == date(2026, 8, 24)
    assert latest_chain_date(store, until=date(2026, 8, 20)) == date(2026, 8, 17)


def _snapshots(store, contract_id, rows):
    with store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO universe (contract_id, symbol, currency, asset_class, "
            "exchange) VALUES (?, 'Z', 'EUR', 'equity', 'FTA')",
            (contract_id,),
        )
        connection.executemany(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, source) "
            "VALUES (?, ?, ?, 'cp_api')",
            [(day, contract_id, iv) for day, iv in rows],
        )


def test_underlying_snapshots_stops_at_the_session_it_is_asked_for(store):
    """Named in the Session 8 report and never asserted until now."""
    from obliskiq.compute.frames import underlying_snapshots

    _snapshots(store, 21, [("2026-08-20", 0.30), ("2026-08-21", 0.31), ("2026-08-24", 0.32)])

    assert len(underlying_snapshots(store, 21)) == 3
    bounded = underlying_snapshots(store, 21, until=date(2026, 8, 21))
    assert [s.snapshot_date for s in bounded] == [date(2026, 8, 20), date(2026, 8, 21)]


def test_the_snapshot_count_cannot_see_past_the_session_it_is_asked_for(store):
    """The one read Session 8's sweep missed, because a COUNT(*) does not look
    like a store read.

    It gates the vendor -> self IV-percentile switch at
    data.iv_self_source_min_sessions, so unbounded it lets a grid rebuilt for a
    past date switch on evidence from its own future.
    """
    from obliskiq.compute.frames import underlying_snapshot_count

    _snapshots(store, 22, [("2026-08-20", 0.30), ("2026-08-21", 0.31), ("2026-08-24", 0.32)])

    assert underlying_snapshot_count(store, 22) == 3
    assert underlying_snapshot_count(store, 22, until=date(2026, 8, 20)) == 1, (
        "the count read forward past its own as_of: three snapshots exist, one was "
        "knowable on 2026-08-20"
    )


def test_the_snapshot_count_keeps_the_session_it_names(store):
    """snapshot_date is a plain date, so '<=' is the right bound — but it must
    still include the day it names."""
    from obliskiq.compute.frames import underlying_snapshot_count

    _snapshots(store, 23, [("2026-08-21", 0.31), ("2026-08-24", 0.32)])
    assert underlying_snapshot_count(store, 23, until=date(2026, 8, 24)) == 2


def test_the_snapshot_count_still_ignores_rows_with_no_implied_vol(store):
    """The bound must not quietly change what is being counted: a session where
    the route served no implied vol has never counted toward the switch."""
    from obliskiq.compute.frames import underlying_snapshot_count

    _snapshots(store, 24, [("2026-08-20", 0.30), ("2026-08-21", 0.31)])
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, source) "
            "VALUES ('2026-08-24', 24, NULL, 'cp_api')"
        )

    assert underlying_snapshot_count(store, 24, until=date(2026, 8, 24)) == 2


# ── a fresh nominal rate minus a stale CPI is not a real rate ─────────────────
#
# The shape that hides. `policy_rate_real` is a DIFFERENCE, and its nominal side
# updates daily while the inflation side is monthly at best — so the cell reads
# as current whatever the CPI is doing. The builder's own macro freshness cannot
# catch it either: `_newest_macro` takes the NEWEST release across a market's
# declared series, and a daily FX rate makes the whole macro side look fresh.
#
# Measured live 2026-09-11: the OECD CPALTT01 family FRED serves these markets
# from has stopped publishing. Japan's observations end 2021-06 (series last
# updated 2024-05), Korea's 2023-11, and Germany/Spain/UK/Israel print ~18 months
# back. Every one of those markets was publishing a real policy rate stamped `ok`.


def _real_rate_cell(store, config, *, cpi_release: str, as_of: date):
    """Build the policy-rate family for one market and return `policy_rate_real`."""
    from obliskiq.compute.indicators import IndicatorInputs, policy_rate

    _macro(
        store,
        [
            ("RATE_X", "fred", "2026-09-01", "2026-09-02", 3.5, "daily"),
            ("CPI_X", "fred", "2025-01-01", cpi_release, 2.0, "monthly"),
        ],
    )
    inputs = IndicatorInputs.bind(
        store,
        config,
        as_of=as_of,
        market="testland",
        sector="broad",
        contract_id=None,
        symbol=None,
        market_spec={"policy_rate_series": "RATE_X", "cpi_series": "CPI_X", "cpi_is_yoy": True},
        benchmark_contract_id=None,
        benchmark_market=None,
    )
    return next(c for c in policy_rate(inputs) if c.indicator == "policy_rate_real")


def test_a_recent_cpi_print_publishes_a_real_rate(store, config):
    cell = _real_rate_cell(store, config, cpi_release="2026-09-01", as_of=date(2026, 9, 11))

    assert cell.status == "ok"
    assert cell.value == pytest.approx(0.035 - 0.02)


def test_a_stale_cpi_print_does_not_publish_a_current_real_rate(store, config):
    """Japan's case: today's policy rate less an inflation print from years ago."""
    cell = _real_rate_cell(store, config, cpi_release="2021-06-15", as_of=date(2026, 9, 11))

    assert cell.status == "stale"
    assert "2021-06-15" in (cell.reason or "")


def test_the_cpi_vintage_travels_on_the_window_either_way(store, config):
    """A real rate is a difference, and the reader cannot otherwise see that one
    side of it is years older than the other."""
    fresh = _real_rate_cell(store, config, cpi_release="2026-09-01", as_of=date(2026, 9, 11))

    assert "CPI printed 2026-09-01" in (fresh.window or "")


# ── the rebase, which is neither absence nor staleness ────────────────────────
#
# A CPI series can be fully collected, fully aligned, current — and still
# unusable, because its publisher moved the base underneath it. Measured
# 2026-09-15: ALFRED's initial-release column for CP0000DEM086NEST holds
# 2015=100 through 2025-12 and 2025=100 from 2026-01. The year-on-year ratio
# across that join is about -22%, with a fresh release date on both ends.


def test_a_year_on_year_ratio_across_a_rebase_is_refused_not_computed(store, config):
    from obliskiq.compute.frames import cpi_yoy

    _macro_with_units(
        store,
        [
            ("CPI_R", "fred", "2025-07-01", "2025-08-19", 132.4, "monthly", "Index 2015=100"),
            ("CPI_R", "fred", "2026-07-01", "2026-08-19", 103.2, "monthly", "Index 2025=100"),
        ],
    )
    spec = {"cpi_series": "CPI_R", "cpi_is_yoy": False}

    assert cpi_yoy(store, spec, as_of=date(2026, 9, 11), calendar_days_per_year=365.0) is None


def test_one_base_over_the_window_still_computes(store, config):
    """The check is not vacuous: the same two readings on one base are a rate."""
    from obliskiq.compute.frames import cpi_yoy

    _macro_with_units(
        store,
        [
            ("CPI_R", "fred", "2025-07-01", "2025-08-19", 100.4, "monthly", "Index 2025=100"),
            ("CPI_R", "fred", "2026-07-01", "2026-08-19", 103.2, "monthly", "Index 2025=100"),
        ],
    )
    spec = {"cpi_series": "CPI_R", "cpi_is_yoy": False}

    rate = cpi_yoy(store, spec, as_of=date(2026, 9, 11), calendar_days_per_year=365.0)
    assert rate == pytest.approx(103.2 / 100.4 - 1.0)


def test_the_real_rate_says_rebased_rather_than_absent(store, config):
    """Because the remedy differs: re-collect on one vintage, not wait for a print.

    "absent" would send a reader to the collector logs looking for a series that
    is in fact there, current, and dated.
    """
    from obliskiq.compute.indicators import IndicatorInputs, policy_rate

    _macro(store, [("RATE_R", "fred", "2026-09-01", "2026-09-02", 3.5, "daily")])
    _macro_with_units(
        store,
        [
            ("CPI_R", "fred", "2025-07-01", "2025-08-19", 132.4, "monthly", "Index 2015=100"),
            ("CPI_R", "fred", "2026-07-01", "2026-08-19", 103.2, "monthly", "Index 2025=100"),
        ],
    )
    inputs = IndicatorInputs.bind(
        store,
        config,
        as_of=date(2026, 9, 11),
        market="testland",
        sector="broad",
        contract_id=None,
        symbol=None,
        market_spec={"policy_rate_series": "RATE_R", "cpi_series": "CPI_R", "cpi_is_yoy": False},
        benchmark_contract_id=None,
        benchmark_market=None,
    )
    cell = next(c for c in policy_rate(inputs) if c.indicator == "policy_rate_real")

    assert cell.status == "n/a"
    assert cell.reason_code == "mixed_units_in_window"
    assert "Index 2015=100" in (cell.reason or "")
    assert "rebased" in (cell.reason or "")
