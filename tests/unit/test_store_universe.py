"""The universe's records in the store: a holdings file is stored whole and read as of the date it
was of and held, a scan's newest run is read, refusals are kept once, and a pin survives a
re-resolution but not a failed one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime

import pytest

from obliskiq.domain.universe import (
    CandidateResolution,
    Holding,
    HoldingsFile,
    Rejection,
    RejectionOrigin,
    ScannerRow,
)
from obliskiq.errors import StoreError
from obliskiq.store import universe as repo
from obliskiq.store.db import Store

OCT_1, OCT_8 = date(2026, 10, 1), date(2026, 10, 8)


def stamp(day: date, hour: int = 18) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def holdings_file(fund: str, as_of: date, rows: int, downloaded: datetime) -> HoldingsFile:
    return HoldingsFile(
        fund_symbol=fund,
        as_of=as_of,
        issuer="iShares",
        url=f"https://example.invalid/{fund}.csv",
        downloaded_at=downloaded,
        content_sha256="0" * 64,
        rows=rows,
    )


def line(fund: str, as_of: date, number: int, ticker: str, key: str | None) -> Holding:
    return Holding(
        fund_symbol=fund,
        as_of=as_of,
        line=number,
        ticker=ticker,
        name=f"{ticker} CORP",
        asset_class="Equity" if key else "Cash",
        issuer_sector="Energy" if key else None,
        sector="energy" if key else None,
        location="Japan",
        country="JP",
        exchange="Tokyo Stock Exchange",
        ibkr_exchange="TSEJ",
        currency="JPY",
        weight=0.05,
        candidate_key=key,
    )


def save(store: Store, fund: str, as_of: date, downloaded: datetime, *tickers: str) -> None:
    lines = [line(fund, as_of, i, t, f"jp:{t.lower()}") for i, t in enumerate(tickers)]
    lines.append(line(fund, as_of, len(tickers), "JPY", None))
    repo.save_holdings(store, holdings_file(fund, as_of, len(lines), downloaded), lines)


def test_a_stock_enters_on_the_date_its_file_showed_it_and_once_the_file_was_held(
    store: Store,
) -> None:
    save(store, "EWJ", OCT_1, stamp(OCT_1), "TOYOTA", "SONY")
    save(store, "EWJ", OCT_8, stamp(OCT_8, 23), "TOYOTA", "INPEX")  # held from 23:00 on the 8th
    assert repo.holdings_of(store, "EWJ", as_of=date(2026, 9, 30)) == ()
    assert [h.ticker for h in repo.candidate_holdings(store, as_of=OCT_1)] == ["TOYOTA", "SONY"]
    on_8th = repo.candidate_holdings(store, as_of=OCT_8)
    assert [h.ticker for h in on_8th] == ["TOYOTA", "INPEX"]
    assert [h.ticker for h in repo.holdings_of(store, "EWJ", as_of=OCT_8)][-1] == "JPY"
    assert repo.first_held(store, "jp:inpex", as_of=date(2026, 10, 7)) is None
    assert repo.first_held(store, "jp:inpex", as_of=OCT_8) == OCT_8
    assert repo.first_held(store, "jp:toyota", as_of=OCT_8) == OCT_1
    assert [f.as_of for f in repo.holdings_files(store, as_of=OCT_8)] == [OCT_8]


def test_a_file_downloaded_after_the_bound_is_not_read_although_it_is_of_an_earlier_date(
    store: Store,
) -> None:
    save(store, "EWJ", OCT_1, stamp(OCT_8), "TOYOTA")  # a file of the 1st fetched on the 8th
    assert repo.candidate_holdings(store, as_of=date(2026, 10, 5)) == ()
    assert [h.ticker for h in repo.candidate_holdings(store, as_of=OCT_8)] == ["TOYOTA"]


def test_a_redownload_replaces_the_file_and_its_lines(store: Store) -> None:
    save(store, "EWJ", OCT_1, stamp(OCT_1), "TOYOTA", "SONY")
    save(store, "EWJ", OCT_1, stamp(OCT_1, 20), "TOYOTA")
    assert [h.ticker for h in repo.holdings_of(store, "EWJ", as_of=OCT_1)] == ["TOYOTA", "JPY"]
    (file,) = repo.holdings_files(store, as_of=OCT_1)
    assert file.rows == 2


def test_a_file_whose_lines_disagree_with_it_is_refused(store: Store) -> None:
    lines = [line("EWJ", OCT_1, 0, "TOYOTA", "jp:toyota")]
    with pytest.raises(StoreError, match="claim 2 lines"):
        repo.save_holdings(store, holdings_file("EWJ", OCT_1, 2, stamp(OCT_1)), lines)
    with pytest.raises(StoreError, match="belongs to EWJ of 2026-10-01"):
        repo.save_holdings(store, holdings_file("EWG", OCT_1, 1, stamp(OCT_1)), lines)
    twice = [*lines, line("EWJ", OCT_1, 0, "SONY", "jp:sony")]
    with pytest.raises(StoreError, match="repeat a line number"):
        repo.save_holdings(store, holdings_file("EWJ", OCT_1, 2, stamp(OCT_1)), twice)
    assert repo.holdings_files(store, as_of=OCT_8) == ()


def scanned(day: date, rank: int, conid: int, location: str = "STK.US.MAJOR") -> ScannerRow:
    return ScannerRow(
        scanned_on=day,
        location=location,
        scan_type="MOST_ACTIVE",
        rank=rank,
        market="us",
        conid=conid,
        symbol=f"S{conid}",
        listing_exchange="NYSE",
        company_name=None,
        candidate_key=f"conid:{conid}",
        scanned_at=stamp(day),
    )


def test_the_newest_scan_held_is_read_and_a_rerun_replaces_its_day(store: Store) -> None:
    repo.save_scanner_rows(store, [scanned(OCT_1, 0, 11), scanned(OCT_1, 1, 12)])
    repo.save_scanner_rows(store, [scanned(OCT_8, 0, 13)])
    assert [r.conid for r in repo.scanner_rows(store, as_of=date(2026, 10, 7))] == [11, 12]
    assert [r.conid for r in repo.scanner_rows(store, as_of=OCT_8)] == [13]
    repo.save_scanner_rows(store, [scanned(OCT_8, 0, 14)])
    assert [r.conid for r in repo.scanner_rows(store, as_of=OCT_8)] == [14]
    assert repo.save_scanner_rows(store, []) == 0


def test_a_refusal_is_kept_once_and_read_by_its_date(store: Store) -> None:
    refusal = Rejection(
        rejected_at=stamp(OCT_1),
        symbol="SQQQ",
        origin=RejectionOrigin.DECLARED,
        listing_exchange="NASDAQ",
        conid=None,
        description="PROSHARES ULTRAPRO SHORT QQQ",
        reason="reads as a leveraged or inverse product (marker 'ultrapro')",
    )
    assert repo.record_rejections(store, [refusal, refusal]) == 2
    assert repo.rejections(store, as_of=date(2026, 9, 30)) == ()
    assert repo.rejections(store, as_of=OCT_1) == (refusal,)
    assert repo.record_rejections(store, []) == 0


def resolution(conid: int | None, *, pinned: bool = False) -> CandidateResolution:
    return CandidateResolution(
        candidate_key="jp:toyota",
        resolved_at=stamp(OCT_1),
        conid=conid,
        symbol="7203" if conid else None,
        listing_exchange="TSEJ" if conid else None,
        currency="JPY" if conid else None,
        pinned=pinned,
        pinned_for="h-toyota" if pinned else None,
        failure=None if conid else "no contract matched",
    )


def test_a_pin_survives_a_reresolution_and_falls_with_a_failed_one(store: Store) -> None:
    with pytest.raises(StoreError, match="no resolved contract to pin"):
        repo.record_pin(store, "jp:toyota", pinned_for="h-toyota")
    repo.record_resolution(store, resolution(13976))
    repo.record_pin(store, "jp:toyota", pinned_for="h-toyota")
    repo.record_resolution(store, resolution(13976))
    kept = repo.candidate_resolutions(store)["jp:toyota"]
    assert (kept.pinned, kept.pinned_for) == (True, "h-toyota")
    repo.record_resolution(store, resolution(None))
    failed = repo.candidate_resolutions(store)["jp:toyota"]
    assert (failed.conid, failed.pinned, failed.pinned_for) == (None, False, None)
    with pytest.raises(StoreError, match="no resolved contract to pin"):
        repo.record_pin(store, "jp:toyota", pinned_for="h-toyota")
    repo.record_resolution(store, resolution(13976, pinned=True))
    repo.record_pin(store, "jp:toyota", pinned_for=None)
    assert not repo.candidate_resolutions(store)["jp:toyota"].pinned


def test_a_resolution_names_its_contract_or_its_failure() -> None:
    with pytest.raises(ValueError, match="not both"):
        CandidateResolution(
            candidate_key="k", resolved_at=stamp(OCT_1), conid=1, symbol=None,
            listing_exchange=None, currency=None, pinned=False, pinned_for=None, failure="x",
        )  # fmt: skip
    with pytest.raises(ValueError, match="only a resolved candidate"):
        CandidateResolution(
            candidate_key="k", resolved_at=stamp(OCT_1), conid=None, symbol=None,
            listing_exchange=None, currency=None, pinned=True, pinned_for=None, failure="x",
        )  # fmt: skip
