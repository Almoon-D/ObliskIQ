"""Stock candidates: the iShares files parsed in both sites' forms, listings matched across
files, lines credited to their cells, every cell covered or its gap declared, the refresh on its
cadence, the scanner's rows and resolution on demand.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from market_rig import MarketRig, build_market_rig

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.data.candidates import (
    Candidate,
    CandidateCollector,
    Quoted,
    candidate_cells,
    candidate_key,
    coverage_problems,
    credit,
    holdings_date,
    parse_ishares,
    resolve_candidate,
)
from obliskiq.data.market.plumbing import Pacer
from obliskiq.domain.universe import ScannerRow
from obliskiq.errors import DataSourceError
from obliskiq.store import universe as universe_repo

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ishares"
EWJ_URL = "https://www.ishares.com/us/products/239665/fund/latest-holdings.csv"
IWDA_URL = "https://www.ishares.com/uk/individual/en/products/251882/fund/latest-holdings.csv"
AT = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
OCT_20 = date(2026, 10, 20)


def parsed(settings: ObliskConfig, name: str, fund: str, url: str) -> Any:
    content = (FIXTURES / name).read_bytes()
    return parse_ishares(
        content, fund=fund, url=url, downloaded_at=AT, config=settings.collectors.candidates
    )


# ── parsing ──


def test_the_us_form_is_read_with_its_date_weights_and_mapped_words(
    shipped_settings: ObliskConfig,
) -> None:
    ewj = parsed(shipped_settings, "ewj_us.csv", "EWJ", EWJ_URL)
    assert (ewj.file.as_of, ewj.file.issuer, ewj.file.rows) == (OCT_20, "iShares", 9)
    assert len(ewj.file.content_sha256) == 64
    toyota = next(line for line in ewj.lines if line.ticker == "7203")
    assert toyota.weight == pytest.approx(0.0358)
    assert (toyota.sector, toyota.issuer_sector) == (
        "consumer_discretionary",
        "Consumer Discretionary",
    )
    assert (toyota.country, toyota.ibkr_exchange, toyota.currency) == ("JP", "TSEJ", "JPY")
    assert toyota.candidate_key == "jp:toyota_motor"
    cash = [line for line in ewj.lines if line.asset_class != "Equity"]
    assert len(cash) == 4
    assert all(line.candidate_key is None and line.sector is None for line in cash)
    futures = next(line for line in cash if line.ticker == "TPZ6")
    assert (futures.location, futures.country) == (None, None)  # "-" is no location


def test_the_uk_form_is_read_and_unmapped_words_stay_unmapped(
    shipped_settings: ObliskConfig,
) -> None:
    iwda = parsed(shipped_settings, "iwda_uk.csv", "IWDA", IWDA_URL)
    assert iwda.file.as_of == OCT_20
    sap = next(line for line in iwda.lines if line.ticker == "SAP")
    assert (sap.country, sap.ibkr_exchange, sap.sector) == ("DE", "IBIS", "technology")
    roche = next(line for line in iwda.lines if line.ticker == "ROP")
    assert (roche.country, roche.ibkr_exchange) == ("CH", "EBS")
    ftse = next(line for line in iwda.lines if line.ticker == "Z Z6")
    assert (ftse.exchange, ftse.ibkr_exchange) == ("IFLL", None)  # not a configured exchange


def test_a_table_ends_at_its_last_line_and_a_file_without_a_date_or_table_is_refused(
    shipped_settings: ObliskConfig,
) -> None:
    config = shipped_settings.collectors.candidates
    content = (FIXTURES / "ewj_us.csv").read_bytes() + b'"The content herein is owned by iShares"\n'
    tail = parse_ishares(content, fund="EWJ", url=EWJ_URL, downloaded_at=AT, config=config)
    assert tail.file.rows == 9
    with pytest.raises(DataSourceError, match="no 'Fund Holdings as of' line"):
        parse_ishares(b"Ticker,Name\n", fund="X", url="u", downloaded_at=AT, config=config)
    with pytest.raises(DataSourceError, match="no holdings table"):
        parse_ishares(b'Fund Holdings as of,"Oct 20, 2026"\n', fund="X", url="u",
                      downloaded_at=AT, config=config)  # fmt: skip
    with pytest.raises(DataSourceError, match="none of the known forms"):
        holdings_date('Fund Holdings as of,"2026-10-20"', config.date_formats)


def test_listings_of_one_company_share_a_key() -> None:
    noise = ["CORP", "LTD", "CLASS", "A", "C", "INC"]
    assert candidate_key("JP", "TOYOTA MOTOR CORP", noise) == candidate_key(
        "JP", "Toyota Motor", noise
    )
    assert candidate_key("US", "ALPHABET CLASS A", noise) == candidate_key(
        "US", "ALPHABET CLASS C", noise
    )
    assert candidate_key("US", "A", noise) == "us:a"  # a name made only of noise keeps itself
    assert candidate_key(None, "X", noise) == "xx:x"


# ── crediting ──


def test_a_line_counts_for_its_country_its_source_and_its_sector_and_broad_cells(
    shipped_settings: ObliskConfig,
) -> None:
    lines = [
        *parsed(shipped_settings, "ewj_us.csv", "EWJ", EWJ_URL).lines,
        *parsed(shipped_settings, "iwda_uk.csv", "IWDA", IWDA_URL).lines,
    ]
    cells = credit(shipped_settings, lines, [], {"jp:toyota_motor": date(2026, 9, 1)})
    (toyota,) = [c for c in cells[("japan", "consumer_discretionary")] if c.tickers == ("7203",)]
    assert toyota.sources == ("EWJ", "IWDA")  # one candidate, both files
    assert toyota.weight == pytest.approx(0.0358)  # its largest weight
    assert toyota.since == date(2026, 9, 1)
    assert toyota in cells[("japan", "broad")]
    assert [c.name for c in cells[("germany", "technology")]] == ["SAP"]  # by location
    assert ("us", "technology") in cells  # IWDA's US lines are the US's
    assert not any(key[0] == "eurozone" for key in cells)  # no file is the eurozone's here
    weights = [c.weight or 0 for c in cells[("japan", "broad")]]
    assert weights == sorted(weights, reverse=True)


def test_a_scanner_row_counts_for_its_markets_broad_cell(shipped_settings: ObliskConfig) -> None:
    row = ScannerRow(
        scanned_on=OCT_20, location="STK.US.MAJOR", scan_type="MOST_ACTIVE", rank=0,
        market="us", conid=265598, symbol="AAPL", listing_exchange="NASDAQ",
        company_name="APPLE INC", candidate_key="conid:265598", scanned_at=AT,
    )  # fmt: skip
    stray = row.model_copy(update={"market": "atlantis", "candidate_key": "conid:1"})
    cells = credit(shipped_settings, [], [row, stray])
    (apple,) = cells[("us", "broad")]
    assert (apple.name, apple.sources, apple.since) == (
        "APPLE INC",
        ("scanner STK.US.MAJOR",),
        OCT_20,
    )
    assert list(cells) == [("us", "broad")]


def test_every_cell_has_candidates_or_a_declared_gap_and_a_stale_gap_is_reported(
    shipped_settings: ObliskConfig,
) -> None:
    lines = parsed(shipped_settings, "ewj_us.csv", "EWJ", EWJ_URL).lines
    cells = candidate_cells(shipped_settings, lines, [])
    grid = shipped_settings.universe.grid
    assert len(cells) == len(grid.markets) * len(grid.sectors)
    problems = coverage_problems(cells)
    assert "japan/energy: no candidate and no declared gap" not in problems
    assert "germany/energy: no candidate and no declared gap" not in problems  # declared
    assert "us/broad: no candidate and no declared gap" in problems  # no US file was parsed
    gap = next(c for c in cells if (c.market, c.sector) == ("germany", "energy"))
    assert gap.gap is not None
    assert "Germany" in gap.gap
    stale = [dataclasses.replace(gap, candidates=(_candidate(),))]
    assert coverage_problems(stale) == ["germany/energy: declared a gap but has candidates"]


def _candidate() -> Candidate:
    return Candidate("de:x", "X", (Quoted("X", "IBIS", "EUR"),), "DE", 0.1, ("EWG",), OCT_20)


# ── refreshing and resolving ──


@pytest.fixture
async def rig(
    make_session: Callable[..., Any],
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    tmp_path: Path,
) -> AsyncIterator[MarketRig]:
    with respx.mock(assert_all_called=False) as router:
        built = await build_market_rig(
            settings=shipped_settings,
            session=make_session(),
            router=router,
            calendars=calendars,
            tmp_path=tmp_path,
        )
        yield built
        await built.aclose()


def collector(rig: MarketRig, fetch: Callable[[str], Any], *, scanner: bool = True) -> Any:
    return CandidateCollector(
        rig.settings,
        fetch=fetch,
        scanner=rig.client.scanner if scanner else None,
        worker=rig.worker,
        pacer=Pacer(rig.settings.collectors, rig.session.clock.sleep),
        now=rig.now,
    )


async def test_the_refresh_downloads_each_source_once_per_cadence_and_isolates_a_failure(
    rig: MarketRig,
) -> None:
    fetched: list[str] = []

    async def fetch(url: str) -> bytes:
        fetched.append(url)
        if url == EWJ_URL:
            return (FIXTURES / "ewj_us.csv").read_bytes()
        if url == IWDA_URL:
            return (FIXTURES / "iwda_uk.csv").read_bytes()
        raise DataSourceError(f"{url} answered HTTP 403")

    rig.market.scans["STK.US.MAJOR"] = [(265598, "AAPL", "NASDAQ")]
    rig.now.at = AT
    first = await collector(rig, fetch).run()
    outcomes = {o.target: o for o in first.outcomes}
    assert outcomes["EWJ"].rows == 9
    assert outcomes["IWDA"].rows == 9
    assert not outcomes["EWG"].ok  # its download failed; the others stand
    assert outcomes["scanner STK.US.MAJOR MOST_ACTIVE"].rows == 1
    files = await rig.store(universe_repo.holdings_files, as_of=AT.date())
    assert {f.fund_symbol for f in files} == {"EWJ", "IWDA"}
    (scanned,) = await rig.store(universe_repo.scanner_rows, as_of=AT.date())
    assert (scanned.conid, scanned.market) == (265598, "us")
    fetched.clear()
    rig.now.at = AT + timedelta(days=1)
    await collector(rig, fetch).run()
    assert EWJ_URL not in fetched  # fresh: inside refresh_days
    rig.now.at = AT + timedelta(days=rig.settings.collectors.candidates.refresh_days)
    await collector(rig, fetch).run()
    assert EWJ_URL in fetched


async def test_without_a_session_the_scanner_is_reported_not_run(rig: MarketRig) -> None:
    async def fetch(url: str) -> bytes:
        raise DataSourceError("offline")

    result = await collector(rig, fetch, scanner=False).run(dry_run=True)
    (scan,) = [o for o in result.outcomes if o.target.startswith("scanner")]
    assert "no IBKR session" in scan.reason
    assert await rig.store(universe_repo.holdings_files, as_of=AT.date()) == ()


async def test_a_candidate_resolves_on_its_listing_or_says_why_not(rig: MarketRig) -> None:
    toyota = Candidate(
        "jp:toyota_motor", "TOYOTA MOTOR", (Quoted("7203", "TSEJ", "JPY"),), "JP", 0.03,
        ("EWJ",), OCT_20,
    )  # fmt: skip
    rig.market.contracts[13976] = ("7203", "TSEJ", "JPY")
    found = await resolve_candidate(rig.client.secdef, toyota, at=AT, pinned_for="h-1")
    assert (found.conid, found.listing_exchange, found.pinned, found.failure) == (
        13976, "TSEJ", True, None,
    )  # fmt: skip
    rig.market.contracts[13976] = ("7203", "TSEJ", "USD")
    wrong = await resolve_candidate(rig.client.secdef, toyota, at=AT, pinned_for=None)
    assert wrong.conid is None
    assert "not the declared JPY" in (wrong.failure or "")
    unlisted = dataclasses.replace(toyota, listings=(Quoted("7203", None, "JPY"),))
    nowhere = await resolve_candidate(rig.client.secdef, unlisted, at=AT, pinned_for=None)
    assert nowhere.failure == "no listing on a configured venue with a known currency"


def test_a_candidate_keeps_each_listing_with_its_own_exchange_and_currency(
    shipped_settings: ObliskConfig,
) -> None:
    from obliskiq.domain.universe import Holding

    def held(fund: str, ticker: str, exchange: str | None, currency: str) -> Holding:
        return Holding(
            fund_symbol=fund, as_of=OCT_20, line=0, ticker=ticker, name="SHELL PLC",
            asset_class="Equity", issuer_sector="Energy", sector="energy",
            location="United Kingdom", country="GB", exchange=None, ibkr_exchange=exchange,
            currency=currency, weight=0.05, candidate_key="gb:shell",
        )  # fmt: skip

    lines = [held("ISF", "SHEL", "LSE", "GBP"), held("EZU", "SHELL", "AEB", "EUR"),
             held("EWU", "SHEL", None, "GBP")]  # fmt: skip
    (shell,) = credit(shipped_settings, lines, [])[("uk", "energy")]
    assert shell.listings == (
        Quoted("SHELL", "AEB", "EUR"), Quoted("SHEL", "LSE", "GBP"), Quoted("SHEL", None, "GBP"),
    )  # fmt: skip
    assert shell.venue_listings() == shell.listings[:2]
    assert shell.tickers == ("SHEL", "SHELL")
