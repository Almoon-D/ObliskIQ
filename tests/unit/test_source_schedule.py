"""The source jobs: each built source at its configured moments of a venue's session, named
``<source>.<n>`` so two runs a day are two jobs, and the venues their calendars must cover.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.orchestration.assemble import calendar_extras, source_plan
from obliskiq.orchestration.schedule import SOURCE, jobs_on, next_job

WEDNESDAY = date(2026, 10, 21)


def test_the_plan_names_each_moment_of_each_built_source(shipped_settings: ObliskConfig) -> None:
    plan = source_plan(shipped_settings)
    names = {m.name: m for m in plan.moments}
    assert {"polymarket.1", "polymarket.2", "rss.5", "cftc.1"} <= set(names)
    assert names["polymarket.1"].venue == "XETR"
    assert names["polymarket.2"].venue == "XNYS"
    assert not any(m.source == "nasdaq_trader" for m in plan.moments)  # the heartbeat's
    assert not any(m.source == "eurostat" for m in plan.moments)  # declared, not built
    assert {"XETR", "XNYS", "XTKS"} <= set(plan.venues)


def test_source_jobs_follow_their_venue_s_session(
    shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    plan = source_plan(shipped_settings)
    jobs = [
        j
        for j in jobs_on(calendars, shipped_settings.runtime, WEDNESDAY, sources=plan)
        if j.kind == SOURCE
    ]
    by_name = {j.name: j for j in jobs}
    kalshi = by_name["kalshi.2"]
    assert kalshi.at == datetime(2026, 10, 21, 20, 15, tzinfo=UTC)  # 16:00 EDT close + 15 min
    assert kalshi.command == "source:kalshi.2"
    assert plan.source_of(kalshi) == "kalshi"
    saturday = jobs_on(calendars, shipped_settings.runtime, date(2026, 10, 24), sources=plan)
    assert [j for j in saturday if j.kind == SOURCE] == []
    upcoming = next_job(
        calendars,
        shipped_settings.runtime,
        datetime(2026, 10, 21, 20, 14, tzinfo=UTC),
        sources=plan,
    )
    assert upcoming is not None
    assert upcoming.at == datetime(2026, 10, 21, 20, 15, tzinfo=UTC)


def test_the_service_s_calendars_cover_the_sources_and_the_halt_feed(
    shipped_settings: ObliskConfig,
) -> None:
    extras = set(calendar_extras(shipped_settings))
    assert {"XTKS", "XETR", "XNYS", "XNAS"} <= extras
