"""The collection jobs: one per venue after its close through the calendars, the uncovered venues
and the venues outside the plan folded into the fallback's job.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.data.market.targets import collection_venues
from obliskiq.orchestration.assemble import calendar_extras, collect_plan
from obliskiq.orchestration.schedule import COLLECT, CollectPlan, Job, jobs_on, next_job

WEDNESDAY = date(2026, 10, 21)


def plan(venues: tuple[str, ...], *, others: tuple[str, ...] = ()) -> CollectPlan:
    return CollectPlan(
        venues=venues, after_close_minutes=30, fallback_venue="XNYS", other_venues=others
    )


def test_each_venue_collects_after_its_own_close(
    shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    jobs = [
        j
        for j in jobs_on(calendars, shipped_settings.runtime, WEDNESDAY, plan(("XAMS", "XNYS")))
        if j.kind == COLLECT
    ]
    assert [(j.venue, j.at) for j in jobs] == [
        ("XAMS", datetime(2026, 10, 21, 16, 0, tzinfo=UTC)),  # 17:30 CEST close + 30 min
        ("XNYS", datetime(2026, 10, 21, 20, 30, tzinfo=UTC)),  # 16:00 EDT close + 30 min
    ]
    assert jobs[0].command == "collect:XAMS"
    saturday = jobs_on(calendars, shipped_settings.runtime, date(2026, 10, 24), plan(("XAMS",)))
    assert [j for j in saturday if j.kind == COLLECT] == []
    after_dst = jobs_on(calendars, shipped_settings.runtime, date(2026, 10, 26), plan(("XAMS",)))
    (monday,) = [j for j in after_dst if j.kind == COLLECT]
    assert monday.at == datetime(2026, 10, 26, 17, 0, tzinfo=UTC)  # Amsterdam is on CET again


def test_the_fallback_job_collects_the_uncovered_venues_and_those_outside_the_plan(
    shipped_settings: ObliskConfig,
) -> None:
    narrow = Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=("XAMS", "XNYS")
    )
    collect = plan(("XAMS", "XNYS", "XTKS"), others=("XWBO",))
    jobs = [
        j
        for j in jobs_on(narrow, shipped_settings.runtime, WEDNESDAY, collect)
        if j.kind == COLLECT
    ]
    assert [j.venue for j in jobs] == ["XAMS", "XNYS"]  # XTKS has no calendar to anchor a job
    amsterdam, new_york = jobs
    assert collect.venues_of(narrow, amsterdam) == ("XAMS",)
    assert collect.venues_of(narrow, new_york) == ("XNYS", "XTKS", "XWBO")


def test_next_job_sees_a_collection_job_and_without_a_plan_none_is_scheduled(
    shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    after_digest = datetime(2026, 10, 21, 20, 31, tzinfo=UTC)
    runtime = shipped_settings.runtime
    found = next_job(calendars, runtime, after_digest - timedelta(minutes=2), plan(("XNYS",)))
    assert isinstance(found, Job)
    assert {found.kind} <= {COLLECT, "digest"}
    plain = next_job(calendars, runtime, after_digest)
    assert plain is not None
    assert plain.kind != COLLECT


def test_the_shipped_plan_covers_the_universe_and_its_calendars_are_built(
    shipped_settings: ObliskConfig,
) -> None:
    shipped = collect_plan(shipped_settings)
    assert shipped.venues == collection_venues(shipped_settings)
    assert {"XAMS", "XNYS", "XTKS", "XNSE"} <= set(shipped.venues)
    assert shipped.fallback_venue == shipped_settings.collectors.schedule.fallback_venue
    assert not set(shipped.other_venues) & set(shipped.venues)
    assert set(shipped.venues) <= set(calendar_extras(shipped_settings))
    schedule = shipped_settings.collectors.schedule.model_copy(update={"enabled": False})
    off = shipped_settings.model_copy(
        update={"collectors": shipped_settings.collectors.model_copy(update={"schedule": schedule})}
    )
    assert "XKRX" not in calendar_extras(off)
