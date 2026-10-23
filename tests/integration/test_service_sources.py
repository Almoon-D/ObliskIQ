"""The service with its sources: the halt feed read each heartbeat into the calendars (restored
at the start, kept and alerted while it fails), and each source run at its scheduled moment as a
run record of its own.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from service_rig import ServiceRig

from obliskiq.calendars.status import BlockReason
from obliskiq.config import ObliskConfig
from obliskiq.store import runs as run_repo
from obliskiq.store import sources as source_repo

MONDAY = date(2026, 10, 26)
FCX = 7089
HALTS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
FCX_HALTED = b"""<rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><item>
  <ndaq:HaltDate>10/26/2026</ndaq:HaltDate><ndaq:HaltTime>09:40:00</ndaq:HaltTime>
  <ndaq:IssueSymbol>FCX</ndaq:IssueSymbol><ndaq:IssueName>Freeport-McMoRan</ndaq:IssueName>
  <ndaq:Market>NYSE</ndaq:Market><ndaq:ReasonCode>T1</ndaq:ReasonCode>
</item></channel></rss>"""


def quiet(settings: ObliskConfig) -> ObliskConfig:
    """No HTTP retry (its backoff is real time) and no pause between targets."""
    retry = settings.collectors.retry.model_copy(
        update={"target_retries": 0, "request_spacing_seconds": 0.0}
    )
    return settings.model_copy(
        update={
            "http": settings.http.model_copy(update={"retries": 0}),
            "collectors": settings.collectors.model_copy(update={"retry": retry}),
        }
    )


class Web:
    """The internet as the sources see it: the halt feed as set, everything else down."""

    def __init__(self) -> None:
        self.halts: Callable[[], httpx.Response] = lambda: httpx.Response(200, content=FCX_HALTED)
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url.host)
        if str(request.url) == HALTS:
            return self.halts()
        return httpx.Response(503, text="down")


async def run_until(s: ServiceRig, start: datetime, until: datetime) -> None:
    s.clock.wall.now = start
    await s.service.run(asyncio.Event(), until=until)


async def test_a_halt_blocks_its_instrument_and_survives_a_failing_feed_and_a_restart(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    web = Web()
    async with httpx.AsyncClient(transport=httpx.MockTransport(web)) as http:
        s = await make(settings=quiet(shipped_settings), http=http)
        start = datetime(2026, 10, 26, 13, 50, tzinfo=UTC)
        await run_until(s, start, start + timedelta(minutes=6))
        calendars = s.path.calendars
        blocked = calendars.venue_status("XNYS", MONDAY, conid=FCX)
        assert blocked.reason is BlockReason.INCIDENT_OVERLAP
        assert calendars.venue_status("XNYS", MONDAY).reason is not BlockReason.INCIDENT_OVERLAP

        web.halts = lambda: httpx.Response(503, text="down")
        await run_until(s, start + timedelta(minutes=7), start + timedelta(minutes=30))
        (alert,) = s.notifier.alerts("halts.feed_failing")
        assert "last state, 1 halts, stays in force" in alert
        assert calendars.venue_status("XNYS", MONDAY, conid=FCX).reason is (
            BlockReason.INCIDENT_OVERLAP
        )

        restarted = await make(settings=quiet(shipped_settings), http=http)
        origin = shipped_settings.sources.halts.nasdaq_trader.origin
        restarted.path.calendars.accept_incidents(origin, [])  # a fresh process holds nothing
        await restarted.service.prepare(dry_run=True)
        assert restarted.path.calendars.venue_status("XNYS", MONDAY, conid=FCX).reason is (
            BlockReason.INCIDENT_OVERLAP
        )  # restored from the store before the feed answered

        web.halts = lambda: httpx.Response(200, content=b"<rss><channel/></rss>")
        await run_until(s, start + timedelta(minutes=31), start + timedelta(minutes=37))
        assert s.reporter.book.active == {}  # the good read cleared the condition


async def test_each_source_runs_at_its_moment_as_a_run_record(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    web = Web()
    async with httpx.AsyncClient(transport=httpx.MockTransport(web)) as http:
        s = await make(settings=quiet(shipped_settings), http=http)
        await run_until(
            s,
            datetime(2026, 10, 26, 20, 10, tzinfo=UTC),
            datetime(2026, 10, 26, 20, 20, tzinfo=UTC),
        )
    ran = [job.command for job in s.service.ran]
    assert "source:polymarket.2" in ran  # New York's close + 15 minutes
    assert "source:kalshi.2" in ran
    (record,) = await s.rig.store(
        run_repo.runs_for, command="source:polymarket.2", session_date=MONDAY
    )
    assert record.status == "error"  # every answer was 503: a failed run, recorded as one
    runs = await s.rig.store(source_repo.source_runs)
    assert runs["polymarket"].last_status == "error"
    assert "gamma-api.polymarket.com" in web.asked
    # After the news feeds the EventExtractor runs: without its key it is skipped, and says so.
    assert "source:rss.5" in ran
    (extract,) = await s.rig.store(run_repo.runs_for, command="extract", session_date=MONDAY)
    assert extract.status == "skipped"
    assert "generativelanguage" not in " ".join(web.asked)


async def test_the_heartbeat_file_reports_each_source_s_health_or_why_it_cannot(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.orchestration import jobs as jobs_module
    from obliskiq.orchestration.health import read_heartbeat

    s = await make()
    s.clock.wall.now = datetime(2026, 10, 26, 13, 50, tzinfo=UTC)
    await s.service.jobs.heartbeat()
    beat = s.path.data_dir / s.rig.settings.paths.heartbeat_file
    _, facts = read_heartbeat(beat)
    assert facts["sources"]["polymarket"] == "stale"  # never run

    def broken(*_args: Any) -> Any:
        raise RuntimeError("the store is locked")

    monkeypatch.setattr(jobs_module.source_repo, "source_runs", broken)
    await s.service.jobs.heartbeat()
    _, facts = read_heartbeat(beat)
    assert facts["sources"] == "unreadable (RuntimeError)"


async def test_a_halt_feed_that_breaks_never_stops_the_heartbeat(
    make: Any, shipped_settings: ObliskConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = Web()
    async with httpx.AsyncClient(transport=httpx.MockTransport(web)) as http:
        s = await make(settings=quiet(shipped_settings), http=http)
        halts = s.service._halts
        assert halts is not None

        async def broken() -> Any:
            raise RuntimeError("the store is locked")

        monkeypatch.setattr(halts, "refresh", broken)
        start = datetime(2026, 10, 26, 13, 50, tzinfo=UTC)
        await run_until(s, start, start + timedelta(minutes=11))
    last = s.service.jobs.last_heartbeat
    assert last is not None
    assert last >= start + timedelta(minutes=5)  # the heartbeats after the broken read ran
