"""CFTC positioning: the archive and Socrata parsed into one report shape, the registry's codes,
the crowding reading (share, percentile with warm-up, velocity) point in time, and the collector.

Ported in intent from the old ObliskIQ's ``tests/test_cftc_cot_history.py`` (parser, the corrected
30-year code, warm-up, rank, as-of) and lite's CFTC tests (net non-commercial as a share of open
interest, no share without a usable open interest).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_sources import PositioningConfig
from obliskiq.data.market.plumbing import Pacer
from obliskiq.data.positioning.cot import CotCollector, count, parse_annual, parse_socrata
from obliskiq.data.positioning.readings import positioning_reading
from obliskiq.domain.sources import CotReport, PositioningStatus
from obliskiq.errors import UpstreamError
from obliskiq.store import Store
from obliskiq.store import sources as source_repo
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "positioning"
NOW = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
SOCRATA = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
ARCHIVE = "https://www.cftc.gov/files/dea/history/deacot{year}.zip"


def archive(text: str, member: str = "annual.txt") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr(member, text)
    return buffer.getvalue()


def sample() -> str:
    return (FIXTURES / "cot_annual_sample.csv").read_text(encoding="utf-8")


def report(day: date, net: int, *, open_interest: int = 1000, **overrides: Any) -> CotReport:
    fields: dict[str, Any] = {
        "contract_code": "088691",
        "report_date": day,
        "release_date": day + timedelta(days=3),
        "market_name": "GOLD",
        "open_interest": open_interest,
        "noncomm_long": max(net, 0) + 100,
        "noncomm_short": max(-net, 0) + 100,
        "comm_long": 300,
        "comm_short": 200,
        "nonrept_long": 10,
        "nonrept_short": 20,
        "source": "cftc_socrata",
        "collected_at": NOW,
    }
    fields.update(overrides)
    return CotReport(**fields)


def weekly(nets: list[int], start: date = date(2020, 1, 7)) -> list[CotReport]:
    return [report(start + timedelta(weeks=i), net) for i, net in enumerate(nets)]


# ── parsing ──


def test_the_archive_is_read_into_reports_of_the_registry_contracts() -> None:
    reports = parse_annual(
        archive(sample()), {"020601", "088691", "13874A"}, lag_days=3, collected_at=NOW
    )
    assert {r.contract_code for r in reports} == {"020601", "088691"}  # 13874+ is not 13874A
    bond = max((r for r in reports if r.contract_code == "020601"), key=lambda r: r.report_date)
    assert bond.market_name == "UST BOND - CHICAGO BOARD OF TRADE"
    assert (bond.report_date, bond.release_date) == (date(2024, 12, 31), date(2025, 1, 3))
    assert bond.open_interest == 1914824
    assert (bond.noncomm_long, bond.noncomm_short) == (433673, 468538)
    assert bond.source == "cftc_annual"
    assert bond.net_noncommercial_share == pytest.approx((433673 - 468538) / 1914824)
    assert bond.net_commercial_share == pytest.approx((1255062 - 1274692) / 1914824)


def test_the_registry_names_the_thirty_year_bond_by_its_own_code(
    shipped_settings: ObliskConfig,
) -> None:
    """The old ObliskIQ had 043607, which is the ultra 10-year note."""
    contracts = shipped_settings.sources.positioning.contracts
    assert contracts["COT_UST30Y"].code == "020601"
    assert "043607" not in {c.code for c in contracts.values()}


def test_a_row_without_open_interest_or_a_date_is_not_a_report_and_blank_legs_stay_blank() -> None:
    header = sample().splitlines()[0]
    rows = [
        header,
        "GOLD,2024-12-24,088691,454646,.,,1,2,3,4",
        "GOLD,2024-12-17,088691,0,1,2,3,4,5,6",
        "GOLD,not a date,088691,100,1,2,3,4,5,6",
        "GOLD,2024-12-10,088691,1,234,1,2,3,4,5,6",
    ]
    reports = parse_annual(archive("\n".join(rows)), {"088691"}, lag_days=3, collected_at=NOW)
    assert len(reports) == 2
    blank = reports[0]
    assert (blank.noncomm_long, blank.noncomm_short) == (None, None)
    assert blank.net_noncommercial_share is None and blank.net_commercial_share == pytest.approx(
        -1 / 454646
    )


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (b"not a zip", "not a ZIP"),
        (archive("x", member="annual.pdf"), "no .txt"),
        (archive("a,b\n1,2\n"), "lacks the contract market code"),
    ],
)
def test_an_archive_that_is_not_one_is_refused(content: bytes, match: str) -> None:
    with pytest.raises(UpstreamError, match=match):
        parse_annual(content, {"088691"}, lag_days=3, collected_at=NOW)


def test_socrata_rows_are_read_into_the_same_shape() -> None:
    rows = json.loads((FIXTURES / "socrata_6dca.json").read_text(encoding="utf-8"))
    reports = parse_socrata(
        [*rows, "junk", {"report_date_as_yyyy_mm_dd": "x"}], lag_days=3, collected_at=NOW
    )
    assert len(reports) == len(rows)
    first = reports[0]
    assert (first.contract_code, first.report_date, first.release_date) == (
        "020601",
        date(2026, 10, 6),
        date(2026, 10, 9),
    )
    assert first.source == "cftc_socrata" and first.open_interest == 1898067
    with pytest.raises(UpstreamError, match="list of rows"):
        parse_socrata({"error": True}, lag_days=3, collected_at=NOW)


def test_a_cftc_count_reads_commas_and_refuses_what_is_not_a_count() -> None:
    assert count("1,234") == 1234
    assert count(" 12 ") == 12
    assert count(None) is None
    assert count(".") is None
    assert count("n/a") is None
    assert count("abc") is None
    assert count("-5") is None


# ── the reading ──


def config(**overrides: Any) -> PositioningConfig:
    fields: dict[str, Any] = {
        "percentile_window_weeks": 6,
        "percentile_min_weeks": 4,
        "velocity_weeks": 2,
    }
    fields.update(overrides)
    return PositioningConfig.model_validate(
        {
            "annual_url_template": ARCHIVE,
            "socrata_url": SOCRATA,
            "socrata_page_size": 5000,
            "incremental_weeks": 8,
            "backfill_start_year": 2006,
            "contracts": {
                "COT_GOLD": {"code": "088691", "name": "Gold", "consumers": ["component:crowding"]}
            },
            **fields,
        }
    )


def test_the_percentile_waits_for_the_warm_up_and_says_so() -> None:
    reports = weekly([10, 20, 30])
    reading = positioning_reading("COT_GOLD", reports, as_of=date(2021, 1, 1), config=config())
    assert reading.status is PositioningStatus.WARMING_UP
    assert reading.percentile is None and reading.window == 3
    assert reading.reason == "warming up: 3/4 weekly reports"
    assert reading.velocity == pytest.approx((30 - 10) / 1000)


def test_the_percentile_is_the_inclusive_rank_within_the_trailing_window() -> None:
    reports = weekly([500, -50, 10, 20, 30, 40, 50])  # the first falls out of a six-week window
    reading = positioning_reading("COT_GOLD", reports, as_of=date(2021, 1, 1), config=config())
    assert reading.status is PositioningStatus.OK
    assert reading.window == 6
    assert reading.percentile == pytest.approx(1.0)
    assert reading.share == pytest.approx(0.05)
    low = positioning_reading(
        "COT_GOLD", weekly([50, 40, 30, 20, 10, -50]), as_of=date(2021, 1, 1), config=config()
    )
    assert low.percentile == pytest.approx(1 / 6)


def test_a_report_not_yet_released_is_not_known() -> None:
    """Tuesday positions released on Friday: on Thursday the newest is the week before."""
    reports = weekly([10, 20, 30, 40, 99])
    tuesday = reports[-1].report_date
    thursday = positioning_reading(
        "COT_GOLD", reports, as_of=tuesday + timedelta(days=2), config=config()
    )
    friday = positioning_reading(
        "COT_GOLD", reports, as_of=tuesday + timedelta(days=3), config=config()
    )
    assert thursday.report_date == tuesday - timedelta(weeks=1)
    assert thursday.status is PositioningStatus.OK and thursday.share == pytest.approx(0.04)
    assert friday.report_date == tuesday and friday.percentile == pytest.approx(1.0)


def test_no_report_released_is_no_data_and_a_share_less_report_is_skipped() -> None:
    nothing = positioning_reading("COT_GOLD", [], as_of=date(2021, 1, 1), config=config())
    assert nothing.status is PositioningStatus.NO_DATA and nothing.share is None
    blank = report(date(2020, 1, 7), 0, noncomm_long=None)
    alone = positioning_reading("COT_GOLD", [blank], as_of=date(2021, 1, 1), config=config())
    assert alone.status is PositioningStatus.NO_DATA


# ── the collector ──


async def no_sleep(_seconds: float) -> None:
    return None


def collector(
    settings: ObliskConfig, worker: StoreWorker, client: httpx.AsyncClient
) -> CotCollector:
    retry = settings.collectors.retry.model_copy(
        update={"target_retries": 0, "request_spacing_seconds": 0.0}
    )
    fast = settings.collectors.model_copy(update={"retry": retry})
    return CotCollector(
        settings,
        client=client,
        policy=RetryPolicy.from_settings(settings.http.model_copy(update={"retries": 0})),
        worker=worker,
        pacer=Pacer(fast, no_sleep),
        now=lambda: NOW,
    )


def socrata_rows(code: str, day: date) -> list[dict[str, str]]:
    return [
        {
            "market_and_exchange_names": f"MARKET {code}",
            "report_date_as_yyyy_mm_dd": f"{day.isoformat()}T00:00:00.000",
            "cftc_contract_market_code": code,
            "open_interest_all": "1000",
            "noncomm_positions_long_all": "600",
            "noncomm_positions_short_all": "400",
        }
    ]


async def test_each_contract_is_read_from_the_backfill_year_then_incrementally(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    contracts = shipped_settings.sources.positioning.contracts
    wheres: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        where = request.url.params["$where"]
        wheres.append(where)
        code = where.split("'")[1]
        return httpx.Response(200, json=socrata_rows(code, date(2026, 10, 13)))

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(SOCRATA).mock(side_effect=answer)
            first = await collector(shipped_settings, store_worker, client).run()
            again = await collector(shipped_settings, store_worker, client).run()
    assert first.status == "ok" and len(first.outcomes) == len(contracts)
    assert all("'2006-01-01T00:00:00.000'" in w for w in wheres[: len(contracts)])
    eight_weeks_back = (date(2026, 10, 13) - timedelta(weeks=8)).isoformat()
    assert all(eight_weeks_back in w for w in wheres[len(contracts) :])
    assert again.status == "ok"
    gold = source_repo.cot_reports(store, "088691", as_of=date(2026, 10, 16))
    assert [(r.report_date, r.net_noncommercial_share) for r in gold] == [(date(2026, 10, 13), 0.2)]
    assert source_repo.cot_reports(store, "088691", as_of=date(2026, 10, 15)) == ()
    assert source_repo.newest_cot_report(store, "088691") == date(2026, 10, 13)


async def test_one_failing_contract_never_stops_the_others_and_a_dry_run_writes_nothing(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        code = request.url.params["$where"].split("'")[1]
        if code == "088691":
            return httpx.Response(500, text="down")
        if code == "084691":
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json=socrata_rows(code, date(2026, 10, 13)))

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(SOCRATA).mock(side_effect=answer)
            result = await collector(shipped_settings, store_worker, client).run(dry_run=True)
    failed = {o.target: o.reason for o in result.outcomes if not o.ok}
    assert set(failed) == {"COT_GOLD", "COT_SILVER"}
    assert "HTTP 500" in failed["COT_GOLD"] and "not JSON" in failed["COT_SILVER"]
    assert result.status == "partial"
    assert source_repo.newest_cot_report(store, "067651") is None


async def test_the_backfill_reads_each_year_and_says_which_were_missing(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(ARCHIVE.format(year=2024)).mock(
                return_value=httpx.Response(200, content=archive(sample()))
            )
            router.get(ARCHIVE.format(year=2025)).mock(return_value=httpx.Response(404))
            router.get(ARCHIVE.format(year=2026)).mock(return_value=httpx.Response(403))
            result = await collector(shipped_settings, store_worker, client).backfill(2024, 2026)
    outcomes = {o.target: o for o in result.outcomes}
    # The bond, the 10-year note, WTI, gold and the euro: three weeks each (13874+ is not ours).
    assert outcomes["2024"].ok and outcomes["2024"].rows == 15
    assert outcomes["2025"].degraded and outcomes["2025"].reason == "no archive for the year"
    assert not outcomes["2026"].ok and "HTTP 403" in outcomes["2026"].reason
    assert len(source_repo.cot_reports(store, "020601", as_of=date(2025, 1, 31))) == 3
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(ARCHIVE.format(year=2024)).mock(
                return_value=httpx.Response(200, content=archive(sample()))
            )
            dry = await collector(shipped_settings, store_worker, client).backfill(
                2024, 2024, dry_run=True
            )
    assert dry.rows == 15
