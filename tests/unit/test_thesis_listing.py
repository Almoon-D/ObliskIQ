"""A stock on the SEC's listings as the thesis book reads it (DECISIONS D195): found by ticker or
CIK on the latest snapshot as of a date, asked of the contract search on its mapped exchange, its
issuer country from its incorporation, and refused with why when any of them fails.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta

from thesis_rig import draft

from obliskiq.config import ObliskConfig
from obliskiq.decision.thesis_intake import (
    ThesisIntake,
    find_listing,
    listing_candidate,
    listing_country,
    stock_candidate,
)
from obliskiq.domain.enums import AssetClass
from obliskiq.domain.stocks import StockListing
from obliskiq.domain.tax import isin_check_digit_holds
from obliskiq.domain.thesis import ThesisInstrument, sec_key
from obliskiq.engines.thesis.gate import Reason
from obliskiq.store import Store
from obliskiq.store import stocks as stocks_repo
from obliskiq.store.worker import StoreWorker

TODAY = date(2026, 11, 20)
KNOWN = datetime(2026, 11, 19, 22, tzinfo=UTC)
ACME = StockListing(cik=7, ticker="ACME", name="ACME CORP", exchange="NYSE", venue="XNYS")


def listed(store: Store, *listings: StockListing) -> None:
    stocks_repo.save_snapshot(store, list(listings), known_at=KNOWN, content_sha256="0" * 64)


def instrument(symbol: str = "ACME", cik: int = 7) -> ThesisInstrument:
    return ThesisInstrument(
        symbol=symbol,
        candidate_key=sec_key(cik),
        conid=None,
        listing_exchange=None,
        currency=None,
        asset_class=AssetClass.STOCK,
        description="ACME CORP",
        sector=None,
        country=None,
        defensive=False,
        isin=None,
    )


def test_a_listing_is_found_on_the_snapshot_known_by_then(store: Store) -> None:
    assert find_listing(store, as_of=TODAY, ticker="ACME") is None  # no snapshot yet
    listed(store, ACME)
    assert find_listing(store, as_of=TODAY, ticker=" acme ") == ACME
    assert find_listing(store, as_of=TODAY, cik=7) == ACME
    assert find_listing(store, as_of=TODAY, ticker="ACME", cik=8) is None
    assert find_listing(store, as_of=KNOWN.date() - timedelta(days=1), ticker="ACME") is None


def test_a_listing_is_asked_on_its_mapped_exchange_or_not_at_all(
    shipped_settings: ObliskConfig,
) -> None:
    found = listing_candidate(shipped_settings, ACME)
    assert found is not None
    assert found.key == "sec:7"
    (quoted,) = found.listings
    assert (quoted.ticker, quoted.exchange, quoted.currency) == ("ACME", "NYSE", "USD")
    elsewhere = ACME.model_copy(update={"exchange": "CBOE"})
    assert listing_candidate(shipped_settings, elsewhere) is None


def test_a_thesis_s_listing_says_why_it_cannot_be_asked_any_more(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    gone = stock_candidate(shipped_settings, store, instrument(), as_of=TODAY)
    assert gone == "ACME (CIK 7) is not on the SEC's latest listings any more"
    listed(store, ACME.model_copy(update={"exchange": "CBOE"}))
    unmapped = stock_candidate(shipped_settings, store, instrument(), as_of=TODAY)
    assert unmapped == "ACME's exchange CBOE maps to no IBKR listing exchange"


def test_the_issuer_country_is_the_home_one_for_a_home_incorporation_only(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    listed(store, ACME, ACME.model_copy(update={"cik": 8, "ticker": "FORN"}))
    stocks_repo.save_company(store, 7, 3571, known_at=KNOWN, incorporated="DE")
    stocks_repo.save_company(store, 8, 3571, known_at=KNOWN, incorporated="L2")
    assert listing_country(shipped_settings, store, 7, as_of=TODAY) == "US"
    assert listing_country(shipped_settings, store, 8, as_of=TODAY) is None
    assert listing_country(shipped_settings, store, 9, as_of=TODAY) is None


def test_an_isin_s_check_digit() -> None:
    assert isin_check_digit_holds("US0378331005")
    assert not isin_check_digit_holds("US0378331006")
    assert not isin_check_digit_holds("not an isin")


async def test_the_intake_refuses_an_unmapped_listing_and_an_isin_for_a_file_share(
    store: Store, store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    listed(store, ACME.model_copy(update={"exchange": "CBOE"}))
    intake = ThesisIntake(
        shipped_settings, worker=store_worker, now=lambda: datetime(2026, 11, 20, 12, tzinfo=UTC)
    )
    refused = await intake.submit(
        draft(symbol=None, listing="ACME", horizon=date(2027, 6, 30)), actor="test"
    )
    assert isinstance(refused, list)
    assert [f.code for f in refused] == [Reason.NO_CALENDAR]
    stored = await intake.submit(draft(horizon=date(2027, 6, 30)), actor="test")
    assert not isinstance(stored, list), stored
    said = await intake.give_isin(stored.thesis_id, "US0378331005", actor="owner")
    assert said == "T1 names no listed stock: its instrument carries its ISIN already"
    assert await intake.give_isin(9, "US0378331005", actor="owner") == "no thesis 9"
