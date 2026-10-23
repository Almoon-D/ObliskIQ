"""``obliskiq sources``: the list, the health, a run by hand (one source, all, refused), the CFTC
backfill and the catalyst calendar, in process and without a network.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response

from obliskiq.cli import sources as sources_cli
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config.loader import load_config
from obliskiq.data.market.plumbing import Collection
from obliskiq.domain.sources import EarningsDate
from obliskiq.store import open_store
from obliskiq.store import sources as source_repo

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "events"
NOW = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
GAMMA = "https://gamma-api.polymarket.com/events"


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(sources_cli, "utc_now", lambda: NOW)
    return path


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


def test_the_list_shows_built_sources_with_their_policy_and_declared_ones_with_their_condition(
    data_dir: Path,
) -> None:
    status, out = _run("sources", "list")
    assert status == EXIT_OK
    assert "fred [macro, built]" in out
    assert "release: from_api (+0 d); stale after 100 h; runs: XNYS close+120m" in out
    assert "credential: FRED_API_KEY" in out
    assert "eurostat [macro, declared]" in out
    assert "activation:" in out
    assert "runs: heartbeat" in out  # the halt feed


def test_health_says_never_run_then_what_a_run_recorded(data_dir: Path, config_dir: Path) -> None:
    status, out = _run("sources", "health")
    assert status == EXIT_OK
    assert "polymarket     stale    last success never, 0 rows: never run" in out
    settings = load_config(config_dir).settings
    with open_store(settings, data_dir) as store:
        source_repo.record_source_run(store, "polymarket", at=NOW, status="ok", rows=3, detail=None)
    _, again = _run("sources", "health")
    assert "polymarket     ok       last success 2026-10-23T21:00:00+00:00, 3 rows" in again


def test_a_source_is_run_by_name_and_a_dry_run_writes_nothing(
    data_dir: Path, config_dir: Path
) -> None:
    december = json.loads((FIXTURES / "polymarket_event_fed_december.json").read_text())
    with respx.mock as router:
        router.get(GAMMA).mock(
            side_effect=lambda request: Response(
                200,
                json=december
                if request.url.params.get("slug") == "fed-decision-in-december-20260729232808632"
                else [],
            )
        )
        status, out = _run("sources", "run", "polymarket", "--dry-run")
    assert status == 2  # degraded: the other curated events listed no open market
    assert "polymarket: degraded (3 rows)" in out
    assert "event:us-recession-by-end-of-2026: degraded" in out
    assert "run: degraded" in out
    settings = load_config(config_dir).settings
    with open_store(settings, data_dir) as store:
        assert source_repo.source_runs(store) == {}


def test_a_source_that_cannot_run_is_refused(data_dir: Path) -> None:
    status, out = _run("sources", "run", "eurostat")
    assert status == EXIT_INVALID
    assert "refused: eurostat is declared, not built" in out


def test_all_runs_every_runnable_source_once(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[tuple[str, bool]] = []

    async def fake(self: Any, name: str, *, dry_run: bool, force: bool) -> Collection:
        ran.append((name, force))
        collection = Collection(name)
        collection.succeeded(name, 1)
        return collection

    monkeypatch.setattr(sources_cli.SourceRunner, "run", fake)
    status, out = _run("sources", "run", "all", "--force")
    assert status == EXIT_OK
    names = [name for name, _ in ran]
    assert "nasdaq_trader" not in names
    assert "fred" in names
    assert "earnings" in names
    assert all(force for _, force in ran)
    assert out.endswith("run: ok\n")


def test_the_backfill_reads_the_cftc_archives(data_dir: Path) -> None:
    with respx.mock as router:
        router.get(url__startswith="https://www.cftc.gov/files/dea/history/").mock(
            return_value=Response(404)
        )
        status, out = _run("sources", "backfill", "--from", "2024", "--to", "2025", "--dry-run")
    assert status == 2
    assert "cftc_backfill: degraded (0 rows)" in out
    assert "2024: degraded: no archive for the year" in out


def test_the_catalyst_calendar_lists_official_dates_and_research_grade_earnings(
    data_dir: Path, config_dir: Path
) -> None:
    settings = load_config(config_dir).settings
    with open_store(settings, data_dir) as store:
        source_repo.save_earnings_dates(
            store,
            [
                EarningsDate(
                    symbol="FCX",
                    collected_on=date(2026, 10, 22),
                    report_at=datetime(2026, 10, 27, 12, tzinfo=UTC),
                    source="yahoo",
                    grade="research",
                ),
                EarningsDate(
                    symbol="FCX",
                    collected_on=date(2026, 10, 22),
                    report_at=datetime(2027, 1, 22, 12, tzinfo=UTC),
                    source="yahoo",
                    grade="research",
                ),
            ],
        )
    status, out = _run("sources", "catalysts", "--days", "7")
    assert status == EXIT_OK
    lines = out.splitlines()
    assert lines[0].startswith("2026-10-27 earnings       FCX        [research] yahoo")
    assert lines[1].startswith("2026-10-28 fomc_decision  us         [official] https://")
    assert all(line[:10] <= "2026-10-30" for line in lines)
    status, out = _run("sources", "catalysts")  # the earnings horizon by default
    assert "2027-01-27 fomc_decision" in out
    with open_store(settings, data_dir) as store:
        store.execute("DELETE FROM earnings_dates")
    empty_settings_window = _run("sources", "catalysts", "--days", "1")
    assert empty_settings_window == (EXIT_OK, "no catalyst in the window\n")
