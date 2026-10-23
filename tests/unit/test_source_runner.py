"""The source runner: each built source dispatched to its collector, its run recorded as its
health (a broken collector included), a declared or heartbeat source refused by name, the
scheduled job's run record, and the health each source reports.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from obliskiq.config import ObliskConfig
from obliskiq.data.runner import SourceRunner, client_for, source_health
from obliskiq.domain.sources import SourceRun, SourceState
from obliskiq.errors import ConfigError
from obliskiq.store import Store
from obliskiq.store import runs as run_repo
from obliskiq.store import sources as source_repo
from obliskiq.store.worker import StoreWorker

NOW = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


async def no_sleep(_seconds: float) -> None:
    return None


def no_earnings(_symbol: str, _limit: int) -> Sequence[datetime]:
    return []


def quiet(settings: ObliskConfig) -> ObliskConfig:
    """No retry and no pause: every request is asked once."""
    retry = settings.collectors.retry.model_copy(
        update={"target_retries": 0, "request_spacing_seconds": 0.0}
    )
    return settings.model_copy(
        update={
            "http": settings.http.model_copy(update={"retries": 0}),
            "collectors": settings.collectors.model_copy(update={"retry": retry}),
        }
    )


def runner(
    settings: ObliskConfig,
    worker: StoreWorker,
    client: httpx.AsyncClient,
    secrets: dict[str, str] | None = None,
) -> SourceRunner:
    known = secrets or {}
    return SourceRunner(
        quiet(settings),
        client=client,
        worker=worker,
        secrets=known.get,
        now=lambda: NOW,
        sleep=no_sleep,
        earnings=no_earnings,
    )


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("atlantis", "no source named atlantis"),
        ("eurostat", "declared, not built"),
        ("nasdaq_trader", "read by the service's heartbeat"),
    ],
)
async def test_a_source_that_cannot_run_by_name_is_refused(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, name: str, message: str
) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ConfigError, match=message):
            await runner(shipped_settings, store_worker, client).run(name)


async def test_every_runnable_source_reaches_its_collector_and_records_its_health(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    secrets = {"FRED_API_KEY": "k", "EIA_API_KEY": "k", "SEC_USER_AGENT": "Owner o@example.com"}
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.route().mock(return_value=httpx.Response(503, text="down"))
            sources = runner(shipped_settings, store_worker, client, secrets)
            names = sources.runnable()
            ran = {name: await sources.run(name) for name in names}
    assert "nasdaq_trader" not in names
    assert {"fred", "cftc", "sec_edgar", "polymarket", "kalshi", "gdelt", "rss"} <= set(names)
    assert ran["fred"].step == "fred"
    assert ran["earnings"].status == "degraded"  # Yahoo listed nothing; no HTTP involved
    runs = source_repo.source_runs(store)
    assert set(runs) == set(names)
    assert runs["polymarket"].last_status == "error"
    assert runs["polymarket"].consecutive_failures == 1
    assert runs["polymarket"].detail is not None


async def test_a_broken_collector_is_a_failed_run_and_a_dry_run_records_nothing(
    shipped_settings: ObliskConfig,
    store_worker: StoreWorker,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a bug")

    async with httpx.AsyncClient() as client:
        sources = runner(shipped_settings, store_worker, client)
        monkeypatch.setattr(sources, "_collect", broken)
        dry = await sources.run("gdelt", dry_run=True)
        assert source_repo.source_runs(store) == {}
        failed = await sources.run("gdelt")
    assert dry.status == failed.status == "error"
    assert "RuntimeError: a bug" in failed.outcomes[0].reason
    assert source_repo.source_runs(store)["gdelt"].last_status == "error"


async def test_a_scheduled_run_is_one_run_record_and_the_backfill_reads_the_archives(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.route().mock(return_value=httpx.Response(404))
            sources = runner(shipped_settings, store_worker, client)
            record = await sources.run_job("source:cftc.1", NOW.date(), "cftc")
            backfill = await sources.backfill(2024, 2025, dry_run=True)
    (run,) = run_repo.runs_for(store, command="source:cftc.1", session_date=NOW.date())
    assert run.status == record.status == "error"
    assert backfill.status == "degraded"  # no archive for either year


async def test_the_shared_client_names_obliskiq_and_follows_redirects(
    shipped_settings: ObliskConfig,
) -> None:
    async with client_for(shipped_settings) as client:
        assert client.headers["User-Agent"] == shipped_settings.sources.user_agent
        assert client.follow_redirects


def run(
    source: str,
    status: str,
    *,
    success: datetime | None,
    failures: int = 0,
) -> SourceRun:
    return SourceRun(
        source=source,
        last_attempt_at=NOW - timedelta(hours=1),
        last_success_at=success,
        last_status=status,
        last_rows=5,
        consecutive_failures=failures,
        detail="fine" if failures == 0 else "HTTP 503",
    )


def test_health_is_failing_stale_or_ok(shipped_settings: ObliskConfig) -> None:
    runs = {
        "fred": run("fred", "ok", success=NOW - timedelta(hours=1)),
        "ecb": run("ecb", "error", success=NOW - timedelta(hours=2), failures=3),
        "eia": run("eia", "partial", success=NOW - timedelta(days=30)),
        "cftc": run("cftc", "skipped", success=None),
    }
    health = {h.source: h for h in source_health(shipped_settings, runs, now=NOW)}
    assert set(health) == set(shipped_settings.sources.built())
    assert health["fred"].state is SourceState.OK
    assert health["fred"].reason == "fine"
    assert health["ecb"].state is SourceState.FAILING
    assert health["ecb"].reason == "3 failed runs in a row: HTTP 503"
    assert health["eia"].state is SourceState.STALE
    assert health["eia"].reason == "no success within 200 hours"
    assert health["cftc"].state is SourceState.STALE
    assert health["gdelt"].reason == "never run"
    assert health["gdelt"].last_attempt_at is None
    assert health["fred"].rows == 5


def test_health_reads_the_runs_the_store_holds(
    shipped_settings: ObliskConfig, store: Store
) -> None:
    source_repo.record_source_run(store, "fred", at=NOW, status="ok", rows=4, detail=None)
    health = source_health(shipped_settings, source_repo.source_runs(store), now=NOW)
    assert next(h for h in health if h.source == "fred").state is SourceState.OK
    assert date(2026, 10, 23) == NOW.date()
