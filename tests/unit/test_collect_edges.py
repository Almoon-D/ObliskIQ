"""The collectors' and the candidates' less travelled branches: refused older history, an
uncovered calendar, candidate seeding that skips, unreadable stored rows, the holdings parser's
blanks, a scanner that fails or finds nothing, a download that is refused, the chain and curve
collectors' edge cases and the pacer's.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fake_market import Chain, Option
from market_rig import AFTER_CLOSE, ASML, EMIM, MarketRig, build_market_rig

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_universe import DECLARABLE_CLASSES
from obliskiq.data.candidates import CandidateCollector, http_fetch, parse_ishares
from obliskiq.data.market.curves import reference_price
from obliskiq.data.market.plumbing import Pacer, is_permanent
from obliskiq.data.market.targets import declared_targets, root_targets, venue_session
from obliskiq.data.market.universe import SEARCH_SEC_TYPE
from obliskiq.domain.universe import CandidateResolution
from obliskiq.errors import DataSourceError, StoreError
from obliskiq.ibkr.errors import NoDataPermanentError, RateLimitedError
from obliskiq.ibkr.models import Quote
from obliskiq.store import repos
from obliskiq.store import universe as universe_repo
from obliskiq.util.http import RetryPolicy

SESSION = date(2026, 10, 21)


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


def target(rig: MarketRig, conid: int) -> Any:
    return next(t for t in declared_targets(rig.settings) if t.conid == conid)


async def test_refused_older_history_keeps_the_newest_bars(rig: MarketRig) -> None:
    rig.market.contracts[EMIM] = ("EMIM", "AEB", "EUR")
    await rig.collector.universe.run({"XAMS"})
    rig.market.bars(EMIM, [46.0] * 10, last=SESSION - timedelta(days=1))
    await rig.collector.backfill.run([target(rig, EMIM)])  # ten sessions, younger than the window
    rig.market.bars(EMIM, [46.0] * 11, last=SESSION)
    calls = 0
    reader = rig.collector.backfill._history
    walk = reader.daily_between

    async def refusing_older(conid: int, *, earliest: date, latest: date, exchange: Any) -> Any:
        nonlocal calls
        calls += 1
        if latest < SESSION - timedelta(days=12):
            raise NoDataPermanentError("Chart data unavailable")
        return await walk(conid, earliest=earliest, latest=latest, exchange=exchange)

    reader.daily_between = refusing_older  # type: ignore[method-assign]
    collection = await rig.collector.backfill.run([target(rig, EMIM)])
    (outcome,) = collection.outcomes
    assert outcome.ok
    assert calls == 2
    assert await rig.store(repos.latest_bar_date, EMIM, as_of=SESSION) == SESSION


def test_a_venue_the_calendars_do_not_cover_dates_by_its_own_zone(
    shipped_settings: ObliskConfig,
) -> None:
    narrow = Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=("XNYS",)
    )
    tokyo = next(t for t in declared_targets(shipped_settings) if t.venue == "XTKS")
    late = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)  # 01:00 on the 22nd in Tokyo
    assert venue_session(narrow, "XTKS", tokyo.zone, late) == date(2026, 10, 22)


def test_every_declarable_class_has_a_search_type() -> None:
    assert set(SEARCH_SEC_TYPE) == set(DECLARABLE_CLASSES)


async def test_candidate_seeding_skips_what_it_cannot_seed(rig: MarketRig) -> None:
    def resolution(key: str, **kw: Any) -> CandidateResolution:
        fields: dict[str, Any] = {
            "candidate_key": key, "resolved_at": AFTER_CLOSE, "conid": 900001, "symbol": "X",
            "listing_exchange": "TSEJ", "currency": "JPY", "pinned": True, "pinned_for": "h",
            "failure": None,
        }  # fmt: skip
        fields.update(kw)
        return CandidateResolution(**fields)

    for found in (
        resolution("a", pinned=False, pinned_for=None),
        resolution("b", conid=900002, symbol=None),
        resolution("c", conid=900003, currency=None),
        resolution("d", conid=900004),
    ):
        await rig.store(universe_repo.record_resolution, found)
    collection = await rig.collector.universe.run(set())
    assert collection.notes["candidates_seeded"] == 1
    (seeded,) = await rig.store(repos.list_instruments, as_of=SESSION, include_superseded=True)
    assert (seeded.conid, seeded.lot_size) == (900004, 100)  # Tokyo's board lot
    again = await rig.collector.universe.run(set())
    assert again.notes["candidates_seeded"] == 0


async def test_an_unreadable_stored_row_is_a_store_error(rig: MarketRig) -> None:
    def corrupt(store: Any) -> None:
        store.execute(
            "INSERT INTO candidate_resolutions (candidate_key, resolved_at, conid, symbol, "
            "listing_exchange, currency, pinned, pinned_for, failure) "
            "VALUES ('k', 'not-a-stamp-at-all-0000Z', 1, NULL, NULL, NULL, 0, NULL, NULL)"
        )

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await rig.store(corrupt)

    def corrupt_currency(store: Any) -> None:
        store.execute(
            "INSERT INTO candidate_resolutions (candidate_key, resolved_at, conid, symbol, "
            "listing_exchange, currency, pinned, pinned_for, failure) "
            "VALUES ('k', '2026-10-21T21:00:00Z', 1, '', 'TSEJ', 'JPY', 0, NULL, NULL)"
        )

    await rig.store(corrupt_currency)
    with pytest.raises(StoreError, match="candidate_resolutions row"):
        await rig.store(universe_repo.candidate_resolutions)


def test_the_parser_reads_blanks_and_unreadable_numbers_as_nothing(
    shipped_settings: ObliskConfig,
) -> None:
    content = (
        b'Fund Holdings as of,"Oct 20, 2026"\n\n'
        b"Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Location,Exchange,"
        b"Market Currency\n"
        b'"A","ALPHA CORP","Energy","Equity","1","-","Japan","Tokyo Stock Exchange","JPY"\n'
        b'"B","BETA CORP","Energy","Equity","1","n/a","Japan","Tokyo Stock Exchange","yen"\n'
        b'"C","GAMMA CORP","Other","Equity","1","","Atlantis","Moon Exchange","JPY"\n'
    )
    lines = parse_ishares(
        content, fund="EWJ", url="u", downloaded_at=AFTER_CLOSE,
        config=shipped_settings.collectors.candidates,
    ).lines  # fmt: skip
    assert [line.weight for line in lines] == [None, None, None]
    assert lines[1].currency is None  # not a currency code
    assert (lines[2].sector, lines[2].country, lines[2].ibkr_exchange) == (None, None, None)
    assert lines[2].candidate_key == "xx:gamma"


async def test_a_scanner_that_fails_or_finds_nothing_is_reported(
    rig: MarketRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def nothing(url: str) -> bytes:
        raise DataSourceError("offline")

    collector = CandidateCollector(
        rig.settings, fetch=nothing, scanner=rig.client.scanner, worker=rig.worker,
        pacer=Pacer(rig.settings.collectors, rig.session.clock.sleep), now=rig.now,
    )  # fmt: skip
    empty = await collector.run()
    (scan,) = [o for o in empty.outcomes if o.target.startswith("scanner")]
    assert (scan.ok, scan.degraded, scan.reason) == (True, True, "the scan returned nothing")

    async def broken(**kw: Any) -> Any:
        raise RateLimitedError("pacing")

    monkeypatch.setattr(rig.client.scanner, "run", broken)
    failed = await collector.run()
    (scan,) = [o for o in failed.outcomes if o.target.startswith("scanner")]
    assert not scan.ok
    assert "RateLimitedError" in scan.reason


async def test_a_download_refused_or_unreachable_is_a_data_source_error(
    shipped_settings: ObliskConfig,
) -> None:
    policy = dataclasses.replace(
        RetryPolicy.from_settings(shipped_settings.http), retries=0, base_delay_seconds=0.0
    )
    with respx.mock() as router:
        router.get("https://example.invalid/a.csv").mock(return_value=httpx.Response(404))
        router.get("https://example.invalid/b.csv").mock(side_effect=httpx.ConnectError("down"))
        router.get("https://example.invalid/c.csv").mock(
            return_value=httpx.Response(200, content=b"x")
        )
        async with httpx.AsyncClient() as client:
            fetch = http_fetch(client, policy)
            with pytest.raises(DataSourceError, match="answered HTTP 404"):
                await fetch("https://example.invalid/a.csv")
            with pytest.raises(DataSourceError, match="could not be reached"):
                await fetch("https://example.invalid/b.csv")
            assert await fetch("https://example.invalid/c.csv") == b"x"


async def test_the_chain_walk_keeps_its_expiry_cap_and_drops_unparseable_or_unwanted_rows(
    rig: MarketRig,
) -> None:
    declared = rig.settings.universe.instrument("ASML")
    assert declared is not None
    rig.market.contracts[ASML] = ("ASML", "AEB", "EUR")
    await rig.collector.universe.run({"XAMS"})
    rig.market.quote(ASML, bid="799", ask="801", last="800")
    months = ["FEB27", "MAR27", "APR27"]
    chain = Chain(months=months, exchanges=["FTA", "EUREX"])
    strikes = [700.0, 800.0, 900.0]
    expiries = {"FEB27": date(2027, 2, 19), "MAR27": date(2027, 3, 19), "APR27": date(2027, 4, 16)}
    for n, (month, expiry) in enumerate(expiries.items()):
        chain.strikes[month] = strikes
        for strike in strikes:
            for right in ("C", "P"):
                conid = 700000 + n * 100 + int(strike) // 100 * 2 + (right == "P")
                options = [Option(conid, expiry, multiplier=None)]
                if strike == 800.0:  # the anchor names an expiry outside the window too
                    options.append(Option(conid + 50000, date(2028, 1, 21)))
                chain.contracts[(month, strike, right)] = options
                rig.market.option_quote(conid, bid=5.0, ask=5.2, iv=0.3, delta=1.5)
    rig.market.chains[ASML] = chain
    capped = rig.settings.collectors.chain.model_copy(update={"max_expiries_per_underlying": 2})
    rig.collector.chains._chain = capped
    collection = await rig.collector.chains.run([target(rig, ASML)])
    (outcome,) = collection.outcomes
    assert outcome.ok, outcome.reason
    rows = await rig.store(repos.chain_rows, as_of=SESSION, underlying_conid=ASML)
    assert {r.expiry for r in rows} == {date(2027, 2, 19), date(2027, 3, 19)}  # the cap
    assert {r.multiplier for r in rows} == {None}  # IBKR stated none
    assert {r.delta for r in rows} == {None}  # 1.5 is not a delta


def test_a_reference_price_is_never_a_non_positive_one() -> None:
    def quote(**kw: Any) -> Quote:
        fields: dict[str, Any] = dict.fromkeys(Quote.model_fields)
        fields.update(conid=1, crossed=False, **kw)
        return Quote(**fields)

    assert reference_price(None) is None
    assert reference_price(quote(last=0.0)) is None
    assert reference_price(quote(bid=1.0, ask=1.2)) == pytest.approx(1.1)
    assert reference_price(quote(last=2.0)) == 2.0


async def test_a_one_rung_curve_is_too_short_for_a_shape(rig: MarketRig) -> None:
    rig.market.futures_ladders["CL"] = [(5002, date(2026, 11, 20))]
    rig.market.quote(5002, bid="70", ask="70.2", last="70.1")
    cl = [r for r in root_targets(rig.settings) if r.root.root == "CL"]
    (outcome,) = (await rig.collector.curves.run(cl, dry_run=True)).outcomes
    assert outcome.degraded
    assert "too few for a curve shape" in outcome.reason
    assert await rig.store(repos.curve, "CL", as_of=SESSION) == ()


async def test_the_pacer_paces_and_classes_failures(rig: MarketRig) -> None:
    assert is_permanent(NoDataPermanentError("no data"))
    assert not is_permanent(RateLimitedError("pacing"))
    assert not is_permanent(ValueError("anything else"))
    collectors = rig.settings.collectors
    still = collectors.retry.model_copy(update={"request_spacing_seconds": 0.0})
    quiet = Pacer(collectors.model_copy(update={"retry": still}), rig.session.clock.sleep)
    before = len(rig.session.clock.slept)  # type: ignore[attr-defined]
    await quiet.pace()
    assert len(rig.session.clock.slept) == before  # type: ignore[attr-defined]
    await rig.collector.chains._pacer.pace()
    assert len(rig.session.clock.slept) == before + 1  # type: ignore[attr-defined]
