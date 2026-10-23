"""SEC Form 4: the submission parsed (header and ownership document, hostile XML refused), the
cluster inputs point in time on the filing date, the daily index and the forward collector.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_sources import InsidersConfig
from obliskiq.data.insiders.clusters import cluster_inputs
from obliskiq.data.insiders.edgar import (
    Form4Collector,
    IndexLine,
    absent,
    followed_tickers,
    index_url,
    normalised_ticker,
    parse_index,
    ticker_ciks,
)
from obliskiq.data.insiders.form4 import parse_submission
from obliskiq.data.market.plumbing import Pacer
from obliskiq.domain.sources import InsiderFiling, InsiderOwner, InsiderTransaction
from obliskiq.domain.universe import Holding
from obliskiq.errors import UpstreamError
from obliskiq.store import Store
from obliskiq.store import sources as source_repo
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "insiders"
NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 2, 23, 0, tzinfo=UTC)
TICKERS = "https://www.sec.gov/files/company_tickers.json"
INDEX = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.{day}.idx"
ARCHIVE = "https://www.sec.gov/Archives"
DENIED = '<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


#: The shipped insiders section with the window and code these tests are written for.
INSIDERS = InsidersConfig(
    daily_index_url_template="https://example.com/{year}/{quarter}/{day}",
    archive_url="https://example.com",
    tickers_url="https://example.com/t.json",
    forms=["4"],
    country="US",
    venues=["XNYS"],
    timezone="America/New_York",
    request_spacing_seconds=0,
    lookback_days=10,
    purchase_code="P",
    cluster_window_days=30,
)


def purchase() -> InsiderFiling:
    return parse_submission(text("form4_purchase_mfm.txt"), zone=NEW_YORK, collected_at=NOW)


# ── the submission ──


def test_a_purchase_is_read_with_its_header_owner_and_line() -> None:
    filing = purchase()
    assert (filing.accession, filing.form, filing.issuer_cik, filing.issuer_symbol) == (
        "0001104659-26-112638",
        "4",
        801961,
        "MFM",
    )
    assert filing.filed_on == date(2026, 10, 1)
    assert filing.accepted_at == datetime(2026, 10, 1, 13, 34, 14, tzinfo=UTC)  # 09:34:14 ET
    (owner,) = filing.owners
    assert (owner.owner_name, owner.is_director, owner.is_officer) == (
        "Baird Gordon A",
        True,
        False,
    )
    (line,) = filing.transactions
    assert (line.code, line.shares, line.price, line.acquired, line.derivative) == (
        "P",
        300.0,
        4.8877,
        True,
        False,
    )
    assert line.direct is not None
    assert line.shares_after is not None


def test_an_award_accepted_after_the_close_keeps_its_filing_date() -> None:
    filing = parse_submission(text("form4_award_fcx.txt"), zone=NEW_YORK, collected_at=NOW)
    assert filing.filed_on == date(2026, 10, 1)
    assert filing.accepted_at == datetime(2026, 10, 1, 21, 15, 28, tzinfo=UTC)
    assert [(t.code, t.acquired) for t in filing.transactions] == [("A", True)]
    assert filing.owners[0].officer_title is None


def ownership(body: str, *, header: bool = True) -> str:
    head = (
        "<ACCEPTANCE-DATETIME>20261001093414\nACCESSION NUMBER:\t\t0000000001-26-000001\n"
        "CONFORMED SUBMISSION TYPE:\t4\nFILED AS OF DATE:\t\t20261001\n"
        if header
        else ""
    )
    return f"{head}<XML>\n{body}\n</XML>"


OWNER = (
    "<reportingOwner><reportingOwnerId><rptOwnerCik>0000000002</rptOwnerCik>"
    "<rptOwnerName>A Person</rptOwnerName></reportingOwnerId>"
    "<reportingOwnerRelationship><isOfficer>true</isOfficer><officerTitle>CEO</officerTitle>"
    "</reportingOwnerRelationship></reportingOwner>"
)


def test_a_derivative_line_and_a_footnoted_price_are_read_as_such() -> None:
    body = (
        "<ownershipDocument><issuer><issuerCik>0000000003</issuerCik></issuer>"
        f"<periodOfReport>2026-09-30</periodOfReport>{OWNER}"
        "<derivativeTable><derivativeTransaction>"
        "<securityTitle><value>Option</value></securityTitle>"
        "<transactionDate><value>2026-09-30</value></transactionDate>"
        "<transactionCoding><transactionCode>M</transactionCode></transactionCoding>"
        "<transactionAmounts><transactionShares><value>10</value></transactionShares>"
        "<transactionPricePerShare><footnoteId id='F1'/></transactionPricePerShare>"
        "<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>"
        "</transactionAmounts><ownershipNature><directOrIndirectOwnership><value>I</value>"
        "</directOrIndirectOwnership></ownershipNature></derivativeTransaction></derivativeTable>"
        "<nonDerivativeTable><nonDerivativeHolding><securityTitle><value>Common</value>"
        "</securityTitle></nonDerivativeHolding></nonDerivativeTable></ownershipDocument>"
    )
    bare_owner = (
        "<reportingOwner><reportingOwnerId><rptOwnerCik>0000000004</rptOwnerCik>"
        "<rptOwnerName>A Fund</rptOwnerName></reportingOwnerId></reportingOwner>"
    )
    uncoded = (
        "<nonDerivativeTable><nonDerivativeTransaction>"
        "<securityTitle><value>Common</value></securityTitle>"
        "<transactionDate><value>2026-09-30</value></transactionDate>"
        "<transactionCoding><transactionCode>J</transactionCode></transactionCoding>"
        "</nonDerivativeTransaction></nonDerivativeTable>"
    )
    body = body.replace(OWNER, OWNER + bare_owner).replace(
        "</ownershipDocument>", uncoded + "</ownershipDocument>"
    )
    filing = parse_submission(ownership(body), zone=NEW_YORK, collected_at=NOW)
    plain, line = filing.transactions
    assert (plain.derivative, plain.code, plain.acquired, plain.direct, plain.shares) == (
        False,
        "J",
        None,
        None,
        None,
    )
    assert not any((filing.owners[1].is_officer, filing.owners[1].is_director))
    assert (line.derivative, line.price, line.acquired, line.direct) == (True, None, False, False)
    assert filing.issuer_symbol is None
    assert filing.period_of_report == date(2026, 9, 30)
    assert filing.owners[0].officer_title == "CEO"
    assert filing.owners[0].is_officer


@pytest.mark.parametrize(
    ("submission", "match"),
    [
        (ownership("<ownershipDocument/>", header=False), "lacks accession, form, filed"),
        (
            "ACCESSION NUMBER: 1\nCONFORMED SUBMISSION TYPE: 4\nFILED AS OF DATE: 20261001\n",
            "acceptance, its XML",
        ),
        (ownership('<!DOCTYPE x [<!ENTITY a "b">]><ownershipDocument/>'), "DTD or an entity"),
        (ownership("<ownershipDocument>"), "not well-formed"),
        (ownership("<other/>"), "not an ownershipDocument"),
        (ownership("<ownershipDocument><issuer/></ownershipDocument>"), "no issuer CIK"),
        (
            ownership(
                "<ownershipDocument><issuer><issuerCik>3</issuerCik></issuer>"
                "<reportingOwner><reportingOwnerId><rptOwnerName>X</rptOwnerName>"
                "</reportingOwnerId></reportingOwner></ownershipDocument>"
            ),
            "without a CIK",
        ),
        (
            ownership(
                f"<ownershipDocument><issuer><issuerCik>3</issuerCik></issuer>{OWNER}"
                "<nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>C</value>"
                "</securityTitle></nonDerivativeTransaction></nonDerivativeTable>"
                "</ownershipDocument>"
            ),
            "lacks its date, code or security",
        ),
    ],
)
def test_a_submission_that_cannot_be_trusted_is_refused(submission: str, match: str) -> None:
    with pytest.raises(UpstreamError, match=match):
        parse_submission(submission, zone=NEW_YORK, collected_at=NOW)


def test_a_filing_date_that_is_no_date_is_refused() -> None:
    body = (
        f"<ownershipDocument><issuer><issuerCik>3</issuerCik></issuer>{OWNER}</ownershipDocument>"
    )
    bad = ownership(body).replace("20261001\n", "20261399\n", 1)
    with pytest.raises(UpstreamError, match="not a date"):
        parse_submission(bad, zone=NEW_YORK, collected_at=NOW)


# ── cluster inputs ──


def filing(
    accession: str,
    filed: date,
    *,
    owners: tuple[InsiderOwner, ...],
    lines: tuple[InsiderTransaction, ...],
    issuer: int = 7,
) -> InsiderFiling:
    return InsiderFiling(
        accession=accession,
        form="4",
        issuer_cik=issuer,
        issuer_symbol="X",
        filed_on=filed,
        accepted_at=datetime.combine(filed, datetime.min.time(), tzinfo=UTC),
        period_of_report=filed,
        owners=owners,
        transactions=lines,
        collected_at=NOW,
    )


def owner(cik: int, *, officer: bool = False, director: bool = False) -> InsiderOwner:
    return InsiderOwner(
        owner_cik=cik,
        owner_name=f"owner {cik}",
        is_director=director,
        is_officer=officer,
        is_ten_percent=False,
        is_other=not (officer or director),
        officer_title=None,
    )


def line(
    code: str = "P",
    *,
    shares: float | None = 100.0,
    price: float | None = 10.0,
    acquired: bool = True,
    derivative: bool = False,
) -> InsiderTransaction:
    return InsiderTransaction(
        line=0,
        derivative=derivative,
        security_title="Common",
        transaction_date=date(2026, 9, 1),
        code=code,
        shares=shares,
        price=price,
        acquired=acquired,
        shares_after=None,
        direct=True,
    )


def test_cluster_inputs_count_open_market_purchases_by_officers_and_directors() -> None:
    as_of = date(2026, 10, 1)
    filings = [
        filing("a", as_of, owners=(owner(1, officer=True),), lines=(line(), line(price=None))),
        filing(
            "b",
            as_of - timedelta(days=5),
            owners=(owner(2, director=True),),
            lines=(line(shares=50, price=4.0),),
        ),
        filing(
            "c",
            as_of - timedelta(days=6),
            owners=(owner(1, officer=True, director=True),),
            lines=(line(),),
        ),
        filing("sale", as_of, owners=(owner(3, officer=True),), lines=(line("S", acquired=False),)),
        filing("award", as_of, owners=(owner(4, director=True),), lines=(line("A"),)),
        filing("option", as_of, owners=(owner(5, officer=True),), lines=(line(derivative=True),)),
        filing("holder", as_of, owners=(owner(6),), lines=(line(),)),
        filing("disposed", as_of, owners=(owner(8, officer=True),), lines=(line(acquired=False),)),
        filing("other issuer", as_of, owners=(owner(9, officer=True),), lines=(line(),), issuer=99),
        filing(
            "too old",
            as_of - timedelta(days=30),
            owners=(owner(10, officer=True),),
            lines=(line(),),
        ),
        filing(
            "not yet filed",
            as_of + timedelta(days=1),
            owners=(owner(11, officer=True),),
            lines=(line(),),
        ),
    ]
    inputs = cluster_inputs(filings, issuer_cik=7, as_of=as_of, config=INSIDERS)
    assert (inputs.filings, inputs.buyers, inputs.officers, inputs.directors) == (3, 2, 1, 2)
    assert inputs.shares == pytest.approx(350.0)
    assert inputs.value == pytest.approx(100 * 10 + 50 * 4 + 100 * 10)
    assert inputs.unpriced_lines == 1
    assert (inputs.first_filed, inputs.last_filed) == (as_of - timedelta(days=6), as_of)


def test_no_purchase_in_the_window_is_an_empty_cluster_not_a_missing_one() -> None:
    inputs = cluster_inputs([], issuer_cik=7, as_of=date(2026, 10, 1), config=INSIDERS)
    assert (inputs.filings, inputs.buyers, inputs.value) == (0, 0, 0.0)
    assert inputs.first_filed is None
    assert inputs.last_filed is None


def test_the_real_purchase_counts_on_its_filing_date_and_not_before() -> None:
    real = purchase()
    on = cluster_inputs([real], issuer_cik=801961, as_of=date(2026, 10, 1), config=INSIDERS)
    before = cluster_inputs([real], issuer_cik=801961, as_of=date(2026, 9, 30), config=INSIDERS)
    assert (on.buyers, on.directors, on.value) == (1, 1, pytest.approx(300 * 4.8877))
    assert before.buyers == 0


# ── the index and the tickers ──


def test_the_daily_index_lists_its_filings() -> None:
    lines = parse_index(text("form_index_20261001.idx"))
    forms = {entry.form for entry in lines}
    assert {"1-K/A", "4", "4/A"} <= forms
    mfm = next(entry for entry in lines if entry.cik == 801961)
    assert mfm.accession == "0001104659-26-112638"
    assert IndexLine("4", 1, "edgar/data/1/odd-name.txt").accession is None
    with pytest.raises(UpstreamError, match="dashed rule"):
        parse_index("no rule here\n")


def test_tickers_are_matched_without_punctuation_and_the_index_url_has_its_quarter(
    shipped_settings: ObliskConfig,
) -> None:
    body = json.loads(text("company_tickers.json"))
    ciks = ticker_ciks({**body, "junk": "x", "blank": {"ticker": "", "cik_str": 1}})
    assert ciks[normalised_ticker("BRKB")] == 1067983
    assert ciks["FCX"] == 831259
    with pytest.raises(UpstreamError, match="not a mapping"):
        ticker_ciks([])
    template = shipped_settings.sources.insiders.daily_index_url_template
    assert index_url(template, date(2026, 1, 30)).endswith("/2026/QTR1/form.20260130.idx")
    assert index_url(template, date(2026, 12, 31)).endswith("/2026/QTR4/form.20261231.idx")


def holding(ticker: str, country: str) -> Holding:
    return Holding(
        fund_symbol="IWDA",
        as_of=date(2026, 10, 20),
        line=0,
        ticker=ticker,
        name=ticker,
        asset_class="Equity",
        issuer_sector=None,
        sector=None,
        location=None,
        country=country,
        exchange=None,
        ibkr_exchange=None,
        currency=None,
        weight=None,
        candidate_key=f"{country.lower()}:{ticker.lower()}",
    )


def test_the_followed_names_are_the_country_candidates_and_the_us_listed_shares(
    shipped_settings: ObliskConfig,
) -> None:
    tickers = followed_tickers(shipped_settings, [holding("AAPL", "US"), holding("7203", "JP")])
    assert "AAPL" in tickers
    assert "7203" not in tickers
    assert {"FCX", "ALB", "MOS", "BG"} <= tickers
    assert "ASML" not in tickers
    assert "SAN" not in tickers


def test_absence_is_a_404_or_storage_denial_never_a_refused_client() -> None:
    assert absent(httpx.Response(404))
    assert absent(httpx.Response(403, text=DENIED))
    assert not absent(httpx.Response(403, text="<html>Undeclared Automated Tool</html>"))
    assert not absent(httpx.Response(200, text=DENIED))


# ── the collector ──


async def no_sleep(_seconds: float) -> None:
    return None


def collector(
    settings: ObliskConfig,
    worker: StoreWorker,
    client: httpx.AsyncClient,
    *,
    user_agent: str | None = "ObliskIQ tests test@example.com",
    now: datetime = NOW,
) -> Form4Collector:
    retry = settings.collectors.retry.model_copy(
        update={"target_retries": 0, "request_spacing_seconds": 0.0}
    )
    insiders = settings.sources.insiders.model_copy(update={"lookback_days": 2})
    fast = settings.model_copy(
        update={
            "collectors": settings.collectors.model_copy(update={"retry": retry}),
            "sources": settings.sources.model_copy(update={"insiders": insiders}),
        }
    )
    return Form4Collector(
        fast,
        client=client,
        policy=RetryPolicy.from_settings(settings.http.model_copy(update={"retries": 0})),
        worker=worker,
        pacer=Pacer(fast.collectors, no_sleep),
        user_agent=user_agent,
        now=lambda: now,
        sleep=no_sleep,
    )


def tickers_with(**extra: int) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(text("company_tickers.json"))
    for i, (ticker, cik) in enumerate(extra.items()):
        body[f"x{i}"] = {"cik_str": cik, "ticker": ticker, "title": ticker}
    return body


async def test_the_new_filings_of_followed_issuers_are_stored_once(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    """Thursday 2026-10-01 has two FCX filings (one fails to parse); Friday's index is not yet
    posted (EDGAR's storage denies it); a second run reads Friday again and nothing else."""
    award = text("form4_award_fcx.txt")
    fcx = "edgar/data/831259/0001214659-26-012436.txt"
    broken = "edgar/data/831259/0001214659-26-012435.txt"
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        sent.append(url)
        assert request.headers["User-Agent"].startswith("ObliskIQ tests")
        if url == TICKERS:
            return httpx.Response(200, json=tickers_with())
        if url == INDEX.format(day="20261001"):
            return httpx.Response(200, text=text("form_index_20261001.idx"))
        if url == INDEX.format(day="20261002"):
            return httpx.Response(403, text=DENIED)
        if url == f"{ARCHIVE}/{fcx}":
            return httpx.Response(200, text=award)
        if url == f"{ARCHIVE}/{broken}":
            return httpx.Response(200, text="not a submission")
        return httpx.Response(500)  # pragma: no cover - nothing else is asked

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.route().mock(side_effect=answer)
            first = await collector(shipped_settings, store_worker, client).run()
            asked = len(sent)
            second = await collector(shipped_settings, store_worker, client).run()
    outcomes = {o.target: o for o in first.outcomes}
    thursday = outcomes["index:2026-10-01"]
    assert thursday.rows == 1
    assert thursday.degraded
    assert "0001214659-26-012435" in thursday.reason
    assert "index:2026-10-02" not in outcomes  # not posted yet, asked again next time
    assert first.notes["issuers"] >= 4
    stored = source_repo.insider_filings(store, 831259, as_of=date(2026, 10, 1))
    assert [f.accession for f in stored] == ["0001214659-26-012436"]
    assert source_repo.insider_filings(store, 831259, as_of=date(2026, 9, 30)) == ()
    again = sent[asked:]
    assert again == [TICKERS, INDEX.format(day="20261002")]
    assert second.status == "skipped"


async def test_a_past_day_without_an_index_is_read_as_closed(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    monday = datetime(2026, 10, 13, 23, 0, tzinfo=UTC)  # Columbus Day, the 12th, has no index

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TICKERS:
            return httpx.Response(200, json=tickers_with())
        if url == INDEX.format(day="20261012"):
            return httpx.Response(403, text=DENIED)
        return httpx.Response(404)

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.route().mock(side_effect=answer)
            await collector(shipped_settings, store_worker, client, now=monday).run()
    done = source_repo.succeeded_targets(store, "sec_edgar", "index:")
    assert done == ("index:2026-10-12",)  # the 11th is a Sunday, never asked


async def test_a_refusal_is_a_failure_and_a_missing_agent_asks_nothing(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TICKERS:
            return httpx.Response(200, json=tickers_with())
        return httpx.Response(403, text="<html>Undeclared Automated Tool</html>")

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.route().mock(side_effect=answer)
            refused = await collector(shipped_settings, store_worker, client).run()
        with respx.mock(assert_all_called=False):
            anonymous = await collector(
                shipped_settings, store_worker, client, user_agent=None
            ).run()
    assert refused.status == "error"
    assert all("HTTP 403" in o.reason for o in refused.outcomes)
    assert anonymous.outcomes[0].target == "issuers"
    assert "SEC_USER_AGENT" in anonymous.outcomes[0].reason


async def test_a_ticker_file_that_fails_or_maps_nothing_stops_the_run_with_its_reason(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.get(TICKERS).mock(return_value=httpx.Response(503))
            failed = await collector(shipped_settings, store_worker, client).run()
        with respx.mock as router:
            router.get(TICKERS).mock(return_value=httpx.Response(200, json={}))
            empty = await collector(shipped_settings, store_worker, client).run()
    assert failed.status == "error"
    assert "HTTP 503" in failed.outcomes[0].reason
    assert empty.status == "skipped"
    assert "maps to an SEC CIK" in empty.empty_reason


async def test_a_dry_run_and_a_filing_about_another_issuer_store_nothing(
    shipped_settings: ObliskConfig, store_worker: StoreWorker, store: Store
) -> None:
    """MFM's filing is listed under a followed CIK in this rig but names another issuer."""
    purchase_text = text("form4_purchase_mfm.txt")
    index = text("form_index_20261001.idx").replace("801961      20261001", "831259      20261001")
    fcx_line = next(line for line in index.splitlines() if "012436" in line)
    odd = fcx_line.replace("0001214659-26-012436.txt", "odd-name.txt")
    index += f"{fcx_line}\n{odd}\nnot an index line\n"

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TICKERS:
            return httpx.Response(200, json=tickers_with())
        if url == INDEX.format(day="20261001"):
            return httpx.Response(200, text=index)
        if url.endswith("0001104659-26-112638.txt"):
            return httpx.Response(200, text=purchase_text)
        if url.endswith(".txt"):
            return httpx.Response(200, text=text("form4_award_fcx.txt"))
        return httpx.Response(404)

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.route().mock(side_effect=answer)
            dry = await collector(shipped_settings, store_worker, client).run(dry_run=True)
            assert source_repo.succeeded_targets(store, "sec_edgar", "index:") == ()
            real = await collector(shipped_settings, store_worker, client).run()
    # Both FCX lines answer the same filing here: a dry run cannot know they are one.
    assert {o.target: o.rows for o in dry.outcomes}["index:2026-10-01"] == 2
    assert source_repo.succeeded_targets(store, "sec_edgar", "index:") == (
        "index:2026-09-30",
        "index:2026-10-01",
    )
    assert {o.target: o.rows for o in real.outcomes}["index:2026-10-01"] == 1
    assert source_repo.insider_filings(store, 801961, as_of=date(2026, 10, 2)) == ()
    assert source_repo.has_insider_filing(store, "0001214659-26-012436")


async def test_a_filing_that_answers_an_error_fails_alone(
    shipped_settings: ObliskConfig, store_worker: StoreWorker
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TICKERS:
            return httpx.Response(200, json=tickers_with())
        if url == INDEX.format(day="20261001"):
            return httpx.Response(200, text=text("form_index_20261001.idx"))
        return httpx.Response(410) if url.endswith(".txt") else httpx.Response(404)

    async with httpx.AsyncClient() as client:
        with respx.mock as router:
            router.route().mock(side_effect=answer)
            result = await collector(shipped_settings, store_worker, client).run()
    thursday = next(o for o in result.outcomes if o.target == "index:2026-10-01")
    assert thursday.degraded
    assert "HTTP 410" in thursday.reason
