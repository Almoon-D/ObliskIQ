"""The analyst's fact sheet: every line dated and statused, every read bounded by ``as_of``, what
is unmeasured said and not filled, and nothing that identifies the account.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from measure_rig import AS_OF as RIG_AS_OF
from measure_rig import STAMP as RIG_STAMP
from measure_rig import seed

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import InstrumentRole
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.market import Bar
from obliskiq.domain.measurement import MeasurementStatus
from obliskiq.domain.sources import (
    CotReport,
    EventExtraction,
    EventMarket,
    EventSnapshot,
    ExtractionStatus,
    GdeltDay,
    InsiderFiling,
    InsiderOwner,
    InsiderTransaction,
    NewsItem,
)
from obliskiq.grid.equity_grid import EquityGridBuilder
from obliskiq.grid.facts import FactLine, FactSheet, build_fact_sheet, nameable, number
from obliskiq.grid.macro_board import MacroBoardBuilder
from obliskiq.store import Store, repos
from obliskiq.store import nav as nav_repo
from obliskiq.store import sources as source_repo

AS_OF = date(2026, 11, 13)  # a Friday
LATER = AS_OF + timedelta(days=1)
STAMP = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)


def stamp(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=UTC)


def first_nameable(settings: ObliskConfig) -> tuple[str, int]:
    declared = nameable(settings)[0]
    assert declared.conid is not None
    return declared.symbol, declared.conid


def seed_bars(store: Store, settings: ObliskConfig, closes: list[float], *, last: date) -> int:
    symbol, conid = first_nameable(settings)
    declared = settings.universe.instrument(symbol)
    assert declared is not None
    repos.upsert_instrument(
        store,
        Instrument(
            conid=conid,
            symbol=symbol,
            listing_exchange=declared.listing_exchange,
            currency=declared.currency,
            asset_class=declared.asset_class,
            domicile=None,
            ucits=None,
            kid_status=None,
            share_class=None,
            lot_size=None,
            option_exchange=None,
            history_exchange=None,
            roles=frozenset({InstrumentRole.DATA}),
            superseded_by=None,
            created_at=STAMP - timedelta(days=400),
            superseded_at=None,
        ),
    )
    days: list[date] = []
    day = last
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    bars = [
        Bar(conid=conid, session_date=d, open=None, high=None, low=None, close=c, volume=None,
            source="t")
        for d, c in zip(reversed(days), closes, strict=True)
    ]  # fmt: skip
    repos.upsert_bars(store, bars)
    return conid


def news(name: str, collected: datetime, *, title: str = "Rates held") -> NewsItem:
    return NewsItem(
        item_id=name * 64,
        feed="fed_press",
        kind="central_bank",
        region="us",
        symbol=None,
        guid=None,
        link=f"https://example.com/{name}",
        title=title,
        summary="The Committee held its rate.",
        published_at=collected - timedelta(hours=1),
        collected_at=collected,
        raw="<item/>",
        fallback_tone=-0.2,
    )


def cot(code: str, report: date, *, long: int) -> CotReport:
    return CotReport(
        contract_code=code,
        report_date=report,
        release_date=report + timedelta(days=3),
        market_name="m",
        open_interest=1000,
        noncomm_long=long,
        noncomm_short=100,
        comm_long=None,
        comm_short=None,
        nonrept_long=None,
        nonrept_short=None,
        source="cftc_socrata",
        collected_at=stamp(report + timedelta(days=3)),
    )


def market(market_id: str, *, curated: bool, question: str = "Will rates fall?") -> EventMarket:
    return EventMarket(
        venue="polymarket",
        market_id=market_id,
        event_id=None,
        question=question,
        outcome="Yes",
        closes_at=None,
        curated=curated,
        tags=(),
    )


def snapshot(market_id: str, at: datetime, probability: float) -> EventSnapshot:
    return EventSnapshot(
        venue="polymarket",
        market_id=market_id,
        observed_at=at,
        probability=probability,
        bid=None,
        ask=None,
        last_price=None,
        volume=None,
        liquidity=None,
    )


def purchase(accession: str, filed: date, *, issuer: int, owner: int) -> InsiderFiling:
    return InsiderFiling(
        accession=accession,
        form="4",
        issuer_cik=issuer,
        issuer_symbol=f"S{issuer}",
        filed_on=filed,
        accepted_at=stamp(filed),
        period_of_report=filed,
        owners=(
            InsiderOwner(
                owner_cik=owner,
                owner_name=f"owner {owner}",
                is_director=True,
                is_officer=False,
                is_ten_percent=False,
                is_other=False,
                officer_title=None,
            ),
        ),
        transactions=(
            InsiderTransaction(
                line=0,
                derivative=False,
                security_title="Common",
                transaction_date=filed,
                code="P",
                shares=100.0,
                price=10.0,
                acquired=True,
                shares_after=None,
                direct=True,
            ),
        ),
        collected_at=stamp(filed),
    )


def seed_sources(store: Store, settings: ObliskConfig) -> None:
    """One of everything the sheet reads, dated on or before ``AS_OF``."""
    seed_bars(store, settings, [100.0 + n for n in range(70)], last=AS_OF)
    code = settings.sources.positioning.contracts["COT_SP500"].code
    weeks = settings.sources.positioning.percentile_min_weeks + 2
    source_repo.upsert_cot_reports(
        store,
        [cot(code, AS_OF - timedelta(days=10 + 7 * n), long=200 + n) for n in range(weeks)],
    )
    source_repo.save_event_markets(
        store,
        [market("calm", curated=False), market("hot", curated=False)],
        seen_at=stamp(AS_OF, 1),
    )
    source_repo.save_event_markets(store, [market("cur", curated=True)], seen_at=stamp(AS_OF, 1))
    source_repo.save_event_snapshots(
        store,
        [
            snapshot("calm", stamp(AS_OF, 2), 0.5),
            snapshot("hot", stamp(AS_OF - timedelta(days=2)), 0.2),
            snapshot("hot", stamp(AS_OF, 2), 0.6),
            snapshot("cur", stamp(AS_OF, 2), 0.3),
        ],
    )
    history = settings.sources.events.gdelt.min_history_days
    source_repo.save_gdelt_days(
        store,
        [
            GdeltDay(
                query_id="europe",
                day=AS_OF - timedelta(days=n + 1),
                articles=10 + n % 3,
                monitored=1000,
                tone=-1.0 - n % 2,
                collected_at=stamp(AS_OF - timedelta(days=n)),
            )
            for n in range(history + 2)
        ]
        + [
            GdeltDay(
                query_id="asia",
                day=AS_OF - timedelta(days=1),
                articles=5,
                monitored=1000,
                tone=None,
                collected_at=stamp(AS_OF),
            )
        ],
    )
    read, raw = news("a", stamp(AS_OF, 9)), news("b", stamp(AS_OF, 10), title="Board meets")
    source_repo.save_news_items(store, [read, raw])
    source_repo.record_extraction(
        store,
        EventExtraction(
            item_id=read.item_id,
            produced_at=stamp(AS_OF, 11),
            provider="gemini",
            model_id="m",
            prompt_version=1,
            prompt_hash="0" * 64,
            status=ExtractionStatus.OK,
            attempts=1,
            output=json.dumps(
                {
                    "entities": ["Federal Reserve"],
                    "region": "us",
                    "sector": None,
                    "type": "monetary_policy",
                    "tone": 0.1,
                    "novelty": 0.3,
                    "sources": [read.link],
                }
            ),
            error=None,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )
    source_repo.save_insider_filing(
        store, purchase("0000000001-26-000001", AS_OF, issuer=7, owner=1)
    )
    source_repo.save_insider_filing(
        store, purchase("0000000001-26-000002", AS_OF - timedelta(days=1), issuer=7, owner=2)
    )
    source_repo.save_insider_filing(
        store, purchase("0000000001-26-000003", AS_OF - timedelta(days=2), issuer=8, owner=3)
    )


def section(sheet: FactSheet, name: str) -> list[FactLine]:
    return [line for line in sheet.lines if line.section == name]


# ── shape ──


def test_every_line_is_numbered_dated_by_as_of_and_statused(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_sources(store, shipped_settings)
    sheet = build_fact_sheet(store, shipped_settings, as_of=AS_OF)
    assert [line.ref for line in sheet.lines] == [f"F{n}" for n in range(1, len(sheet.lines) + 1)]
    assert all(line.day <= AS_OF for line in sheet.lines)
    assert all(line.status for line in sheet.lines)
    assert sheet.instruments == tuple(d.symbol for d in nameable(shipped_settings))
    assert sheet.closes == {first_nameable(shipped_settings)[0]: 169.0}
    first = sheet.lines[0]
    assert first.render() == (
        f"F1 | {first.day.isoformat()} | {first.status} | instrument | {first.text}"
    )
    assert sheet.text().splitlines()[0] == first.render()
    assert sheet.line("f1") == first
    assert sheet.line("F9999") is None
    assert len(sheet.sheet_hash) == 64


def test_an_instrument_line_carries_its_close_and_returns(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_sources(store, shipped_settings)
    sheet = build_fact_sheet(store, shipped_settings, as_of=AS_OF)
    symbol, _ = first_nameable(shipped_settings)
    line = next(line for line in section(sheet, "instrument") if line.text.startswith(symbol))
    assert (line.day, line.status) == (AS_OF, "ok")
    assert "close 169" in line.text
    assert f"return over 21 sessions {number(169 / 148 - 1)}" in line.text
    assert f"return over 63 sessions {number(169 / 106 - 1)}" in line.text
    unpriced = section(sheet, "instrument")[1]
    assert (unpriced.status, unpriced.text.endswith("no close held")) == ("n/a", True)


def test_a_short_or_old_price_history_is_said(store: Store, shipped_settings: ObliskConfig) -> None:
    seed_bars(store, shipped_settings, [10.0, 11.0], last=AS_OF - timedelta(days=12))
    sheet = build_fact_sheet(store, shipped_settings, as_of=AS_OF)
    line = section(sheet, "instrument")[0]
    assert line.status == "stale"
    assert "return over 21 sessions n/a (too few closes)" in line.text


def test_an_empty_store_says_what_is_missing(store: Store, shipped_settings: ObliskConfig) -> None:
    sheet = build_fact_sheet(store, shipped_settings, as_of=AS_OF)
    texts = {line.section: line.text for line in sheet.lines}
    assert texts["macro"] == "no macro board is built"
    assert texts["grid"] == "no equity grid is built"
    assert texts["ranking"] == "no ranked view: no grid cell ranks"
    assert "no report of COT_" in texts["positioning"]
    assert texts["pulse"].endswith("no day collected")
    assert "market" not in texts
    assert "news" not in texts
    assert "insider" not in texts


def test_sources_become_dated_lines(store: Store, shipped_settings: ObliskConfig) -> None:
    seed_sources(store, shipped_settings)
    sheet = build_fact_sheet(store, shipped_settings, as_of=AS_OF)
    positioning = {line.text.split(":")[0]: line for line in section(sheet, "positioning")}
    sp500 = positioning["CFTC E-mini S&P 500 (CME)"]
    assert sp500.status == "ok"
    assert sp500.day == AS_OF - timedelta(days=7)  # the newest report's release
    assert "percentile 0.01852 over 54 weeks" in sp500.text  # the newest is the least long
    assert positioning["CFTC Nasdaq-100 mini (CME)"].status == "no_data"
    markets = [line.text for line in section(sheet, "market")]
    assert markets[0].startswith('polymarket: "Will rates fall?" (Yes) probability 0.3')  # curated
    assert "probability 0.6, change over" in markets[1]  # the largest move next
    assert "no reading" in markets[2]
    pulses = {line.text.split(" (")[0]: line for line in section(sheet, "pulse")}
    assert pulses["GDELT europe"].status == "ok"
    assert "article share z-score" in pulses["GDELT europe"].text
    assert pulses["GDELT asia"].status == "warming_up"
    lines = section(sheet, "news")
    assert [line.status for line in lines] == ["raw", "ok"]  # newest first
    assert "not read by the extractor; offline tone -0.2" in lines[0].text
    assert "event monetary_policy, tone 0.1, novelty 0.3, entities Federal Reserve" in lines[1].text
    insiders = section(sheet, "insider")
    assert insiders[0].text.startswith("S7: 2 officers or directors bought")  # most buyers first
    assert insiders[0].day == AS_OF
    assert insiders[1].text.startswith("S8: 1 officers")


def test_the_caps_hold(store: Store, shipped_settings: ObliskConfig) -> None:
    seed_sources(store, shipped_settings)
    analyst = shipped_settings.llm.analyst
    sheet_config = analyst.fact_sheet.model_copy(
        update={"max_news_lines": 1, "max_market_lines": 1, "max_insider_lines": 0}
    )
    llm = shipped_settings.llm.model_copy(
        update={"analyst": analyst.model_copy(update={"fact_sheet": sheet_config})}
    )
    settings = shipped_settings.model_copy(update={"llm": llm})
    sheet = build_fact_sheet(store, settings, as_of=AS_OF)
    assert (len(section(sheet, "news")), len(section(sheet, "market"))) == (1, 1)
    assert section(sheet, "insider") == []


# ── point in time and privacy ──


def test_what_arrives_after_as_of_changes_nothing(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_sources(store, shipped_settings)
    before = build_fact_sheet(store, shipped_settings, as_of=AS_OF)
    symbol, conid = first_nameable(shipped_settings)
    repos.upsert_bars(
        store,
        [Bar(conid=conid, session_date=LATER + timedelta(days=2), open=None, high=None, low=None,
             close=999.0, volume=None, source="t")],
    )  # fmt: skip
    code = shipped_settings.sources.positioning.contracts["COT_SP500"].code
    source_repo.upsert_cot_reports(store, [cot(code, AS_OF - timedelta(days=1), long=999)])
    source_repo.save_news_items(store, [news("c", stamp(LATER, 1), title="Tomorrow's news")])
    source_repo.save_event_snapshots(store, [snapshot("cur", stamp(LATER, 1), 0.99)])
    source_repo.save_event_markets(store, [market("new", curated=True)], seen_at=stamp(LATER))
    source_repo.save_insider_filing(
        store, purchase("0000000001-26-000009", LATER, issuer=9, owner=9)
    )
    source_repo.save_gdelt_days(
        store,
        [GdeltDay(query_id="asia", day=AS_OF, articles=900, monitored=1000, tone=None,
                  collected_at=stamp(LATER))],
    )  # fmt: skip
    after = build_fact_sheet(store, shipped_settings, as_of=AS_OF)
    assert after.text() == before.text()
    assert after.sheet_hash == before.sheet_hash
    assert symbol in after.instruments


def test_the_sheet_carries_no_account_identifier_or_balance(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_sources(store, shipped_settings)
    account = f"{shipped_settings.ibkr.paper_account_prefixes[0]}7654321"  # nobody's
    nav_repo.record_nav(
        store,
        nav_repo.NavReading(
            as_of=AS_OF,
            nav_base=Decimal("987654.32"),
            currency="EUR",
            source=account,
            recorded_at=STAMP,
        ),
    )
    text = build_fact_sheet(store, shipped_settings, as_of=AS_OF).text()
    assert "987654" not in text
    assert "987,654" not in text
    assert account not in text
    for prefix in shipped_settings.ibkr.paper_account_prefixes:
        assert re.search(rf"\b{prefix}[0-9]+", text) is None


@pytest.mark.parametrize("value", [0.5, -0.000123456, 12345.678])
def test_values_carry_four_significant_digits(value: float) -> None:
    assert number(value) == f"{value:.4g}"


def test_a_built_grid_board_and_rankings_become_lines(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """Over the measurement rig's store (``tests/support/measure_rig.py``), built once."""
    seed(store, shipped_settings)
    grid = EquityGridBuilder(store, shipped_settings)
    board = MacroBoardBuilder(store, shipped_settings)
    grid.write(grid.build(as_of=RIG_AS_OF, computed_at=RIG_STAMP))
    board.write(board.build(as_of=RIG_AS_OF, computed_at=RIG_STAMP))
    sheet = build_fact_sheet(store, shipped_settings, as_of=RIG_AS_OF + timedelta(days=1))
    macro = section(sheet, "macro")
    assert all(line.day == RIG_AS_OF for line in macro)  # the session the board was built for
    us_10y = next(line for line in macro if line.text.startswith("us/rate_10y: "))
    assert us_10y.status == "ok"
    assert "level 0.03797" in us_10y.text
    assert macro[-1].status == "n/a"
    assert macro[-1].text.endswith("board cells have no measured leaf")
    cells = section(sheet, "grid")
    us = next(line for line in cells if line.text.startswith("us/broad: "))
    assert "return_21d_base 0.1151" in us.text
    assert "iv_percentile" not in us.text  # unmeasured there: left out, not filled
    assert cells[-1].text.startswith("187 of 192 grid cells have none of return_21d_base")
    ranks = section(sheet, "ranking")
    assert ranks[0].text.startswith("most_extended #1: canada/broad distance_from_52w_high")
    assert "(ranked over 5 of 61 reachable cells)" in ranks[0].text
    iwda = next(line for line in section(sheet, "instrument") if line.text.startswith("IWDA:"))
    assert (iwda.status, sheet.closes["IWDA"]) == ("ok", pytest.approx(169.2, abs=0.05))


def test_a_stale_measurement_is_marked(store: Store, shipped_settings: ObliskConfig) -> None:
    seed(store, shipped_settings)
    board = MacroBoardBuilder(store, shipped_settings)
    rows = board.build(as_of=RIG_AS_OF, computed_at=RIG_STAMP)
    stale = [
        row.model_copy(
            update={"status": MeasurementStatus.STALE, "reason": "old", "reason_code": "stale"}
        )
        if row.status is MeasurementStatus.OK
        else row
        for row in rows
    ]
    board.write(stale)
    lines = section(build_fact_sheet(store, shipped_settings, as_of=RIG_AS_OF), "macro")
    measured = [line for line in lines if line.status != "n/a"]
    assert measured
    assert all(line.status == "stale" for line in measured)
    assert "(stale)" in measured[0].text
