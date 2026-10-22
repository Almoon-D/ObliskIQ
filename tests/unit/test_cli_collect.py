"""``obliskiq collect``, ``obliskiq universe`` and ``obliskiq research data load-universe``, run in
process without a broker: what they print, what they refuse and what they write.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response

from obliskiq.cli import research as research_cli
from obliskiq.cli.app import EXIT_INVALID, EXIT_NO_CREDENTIALS, EXIT_OK, run
from obliskiq.config.loader import load_config
from obliskiq.store import open_research_store, open_store
from obliskiq.store import universe as universe_repo
from obliskiq.store.research import research_series_info

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ishares"
EWJ_URL = "https://www.ishares.com/us/products/239665/fund/latest-holdings.csv"


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
    return path


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


def test_universe_validate_explains_the_grid_eligibility_venues_and_plan(data_dir: Path) -> None:
    status, out = _run("universe", "validate")
    assert status == EXIT_OK, out
    assert out.startswith("universe is valid: 127 instruments")
    assert "  japan: broad N225, energy 1618" in out
    assert "eligibility: policy eu_priips (Regulation (EU) No 1286/2014" in out
    spy = next(line for line in out.splitlines() if line.startswith("  SPY:"))
    assert "refused: SPY is an etf domiciled in US, UCITS False" in spy
    assert "key information document" in spy
    assert "  IWDA: eligible: IWDA is an etf domiciled in IE, UCITS True" in out
    assert "[never ordered until resolved: not pinned]" in out
    assert re.search(r"XTKS Tokyo Stock Ex\.: JPY, Stocks: Japan, board lot 100; delayed", out)
    assert "XNYS NYSE: USD, Stocks: United States; real time: opra_l1, us_complimentary" in out
    assert "market-data plan: opra_l1, eurex_core_l1, us_complimentary; 1.50 USD, 0 EUR" in out
    assert "candidates: no store yet" in out


def test_universe_validate_reports_the_candidates_coverage_from_the_store(
    data_dir: Path, config_dir: Path
) -> None:
    from obliskiq.data.candidates import parse_ishares

    settings = load_config(config_dir).settings
    parsed = parse_ishares(
        (FIXTURES / "ewj_us.csv").read_bytes(), fund="EWJ", url=EWJ_URL,
        downloaded_at=datetime(2026, 10, 21, tzinfo=UTC), config=settings.collectors.candidates,
    )  # fmt: skip
    with open_store(settings, data_dir) as store:
        universe_repo.save_holdings(store, parsed.file, parsed.lines)
    status, out = _run("universe", "validate")
    assert status == EXIT_OK
    assert "declare a gap" in out
    assert "  problem: us/broad: no candidate and no declared gap" in out


def test_collect_and_pin_refuse_without_credentials(data_dir: Path) -> None:
    status, out = _run("collect", "backfill")
    assert status == EXIT_NO_CREDENTIALS
    assert "no credentials" in out
    status, out = _run("universe", "pin", "TOPIX")
    assert status == EXIT_NO_CREDENTIALS
    status, out = _run("universe", "pin")
    assert (status, "name a SYMBOL" in out) == (EXIT_INVALID, True)
    status, out = _run("universe", "pin", "SPY", "--candidate", "us:x", "--thesis", "t")
    assert status == EXIT_INVALID
    status, out = _run("universe", "pin", "--candidate", "jp:toyota_motor")
    assert "pinned for a thesis" in out


def test_pin_refuses_a_symbol_the_universe_pins_already_or_does_not_declare(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("obliskiq.cli.app._broker_refusal", lambda _env, _out: None)
    status, out = _run("universe", "pin", "SPY")
    assert status == EXIT_INVALID
    assert "SPY is already pinned to 756733" in out
    status, out = _run("universe", "pin", "NOPE")
    assert "NOPE is not declared" in out


def test_collect_candidates_without_credentials_refreshes_the_files_alone(data_dir: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(EWJ_URL).mock(
            return_value=Response(200, content=(FIXTURES / "ewj_us.csv").read_bytes())
        )
        router.get(url__regex=r"https://www\.ishares\.com/.*").mock(return_value=Response(404))
        status, out = _run("collect", "candidates")
    assert "no IBKR credentials: the holdings files alone are refreshed" in out
    assert "candidates: partial" in out
    assert "  EWG: failed: DataSourceError" in out
    assert "scanner STK.US.MAJOR MOST_ACTIVE: failed: no IBKR session" in out
    assert status != EXIT_OK
    settings = load_config(data_dir.parent / "config").settings
    with open_store(settings, data_dir) as store:
        (held,) = universe_repo.holdings_files(store, as_of=date.today())
    assert (held.fund_symbol, held.rows) == ("EWJ", 9)


class _Frame:
    """A yfinance frame of three sessions."""

    def iterrows(self) -> Iterator[tuple[Any, dict[str, float]]]:
        for day in (date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)):
            stamp = datetime(day.year, day.month, day.day, tzinfo=UTC)
            yield _Stamp(stamp), {"Close": 100.0, "Adj Close": 99.0, "Dividends": 0.0}


class _Stamp:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def date(self) -> date:
        return self._at.date()


def test_load_universe_reads_every_tradable_candidate_from_yahoo(
    data_dir: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, date]] = []

    def history(symbol: str, start: date, end: date) -> Any:
        asked.append((symbol, start))
        if symbol == "SAN.MC":
            raise research_cli.DataSourceError("Yahoo has no SAN.MC")
        return _Frame()

    monkeypatch.setattr(research_cli, "yfinance_history", history)
    status, out = _run("research", "data", "load-universe")
    assert status == EXIT_INVALID  # SAN failed, and says so
    assert "  universe_iwda: 3 closes, 2026-10-19 to 2026-10-21" in out
    assert "  universe_san: FAILED: Yahoo has no SAN.MC" in out
    assert "loaded 52 of 53 listings" in out
    assert ("XDWD.DE", date(2014, 7, 22)) in asked  # Xetra's ETF segment, IBIS2
    assert ("IWDA.L", date(2009, 9, 25)) in asked  # from its inception, after the configured start
    assert ("EXV1.DE", date(2006, 1, 1)) in asked
    settings = load_config(config_dir).settings
    with open_research_store(settings, data_dir) as store:
        info = research_series_info(store, "universe_iwda.close")
    assert info is not None
    assert info.vendor.value == "yahoo"


def test_yahoo_symbols_follow_yahoo_s_own_forms(shipped_settings: Any) -> None:
    from obliskiq.research.data import yahoo_symbol

    yahoo = shipped_settings.research.data.yahoo
    assert yahoo_symbol("700", "SEHK", yahoo.suffixes, yahoo.digits) == "0700.HK"
    assert yahoo_symbol("NG.", "LSE", yahoo.suffixes, yahoo.digits) == "NG.L"
    assert yahoo_symbol("RCI.B", "TSE", yahoo.suffixes, yahoo.digits) == "RCI-B.TO"
    assert yahoo_symbol("BRK B", "NYSE", yahoo.suffixes, yahoo.digits) == "BRK-B"
    assert yahoo_symbol("7203", "TSEJ", yahoo.suffixes, yahoo.digits) == "7203.T"
    assert yahoo_symbol("X", "FTA", yahoo.suffixes, yahoo.digits) is None


# ── the commands that read the broker, over the fake market ──


@pytest.fixture
def broker(
    monkeypatch: pytest.MonkeyPatch,
    make_session: Any,
    calendars: Any,
    data_dir: Path,
    config_dir: Path,
) -> dict[str, Any]:
    """The commands' broker session replaced by the fake market; ``arrange`` sets it up."""
    import asyncio
    from types import SimpleNamespace

    from fake_broker import ACCOUNT
    from fake_market import FakeMarket

    from obliskiq.cli import collect as collect_cli
    from obliskiq.ibkr.client import IbkrClient
    from obliskiq.ibkr.guard import AccountGuard
    from obliskiq.store.worker import StoreWorker

    settings = load_config(config_dir).settings
    state: dict[str, Any] = {"arrange": lambda _market: None}

    def with_path(loaded: Any, run: Any) -> Any:
        async def go() -> Any:
            with respx.mock(assert_all_called=False) as router:
                market = FakeMarket(router)
                state["arrange"](market)
                state["market"] = market
                session = make_session()
                guard = AccountGuard(account_id=ACCOUNT, profile="paper", paper_prefixes=("DU",))
                client = IbkrClient(session, settings, guard)
                await client.start()
                worker = StoreWorker(lambda: open_store(settings, data_dir))
                try:
                    path = SimpleNamespace(client=client, worker=worker, calendars=calendars)
                    return await run(path)
                finally:
                    await worker.aclose()
                    await session.aclose()

        return asyncio.run(go())

    monkeypatch.setattr(collect_cli, "_with_collection_path", with_path)
    monkeypatch.setattr("obliskiq.cli.app._broker_refusal", lambda _env, _out: None)
    return state


def _declare(market: Any, settings: Any, *symbols: str) -> None:
    for declared in settings.universe.instruments:
        if declared.symbol in symbols and declared.conid is not None:
            market.contracts[declared.conid] = (
                declared.symbol, declared.listing_exchange, declared.currency,
            )  # fmt: skip


def test_collect_runs_a_step_and_the_whole_job_as_run_records(
    broker: dict[str, Any], config_dir: Path, data_dir: Path
) -> None:
    from obliskiq.store import repos

    settings = load_config(config_dir).settings
    today = date.today()

    def arrange(market: Any) -> None:
        _declare(market, settings, "EMIM", "IBGM", "ASML", "IEMA", "TPSA")
        market.contracts[555001] = ("JDEP", "AEB", "EUR")
        # A week back, so every bar is of a completed session whatever the hour.
        last = today - timedelta(days=7)
        market.bars(settings.universe.instrument("EMIM").conid, [46.0] * 30, last=last)
        market.quote(settings.universe.instrument("EMIM").conid)

    broker["arrange"] = arrange
    status, out = _run("collect", "universe", "--venue", "XAMS")
    assert "universe: degraded" in out  # JDEP resolves but is not pinned
    assert "run: degraded" in out
    assert status == 2  # the run log's exit code for degraded
    status, out = _run("collect", "backfill", "--venue", "XAMS")
    assert "backfill: partial" in out
    emim = settings.universe.instrument("EMIM").conid
    with open_store(settings, data_dir) as store:
        assert repos.bar_count(store, emim, as_of=today) == 30
    status, out = _run("collect", "daily", "--venue", "XAMS", "--dry-run")
    assert [line.split(":")[0] for line in out.splitlines() if not line.startswith(" ")][:5] == [
        "universe", "backfill", "snapshots", "chains", "curves",
    ]  # fmt: skip


def test_pin_writes_a_resolved_conid_into_the_universe_file(
    broker: dict[str, Any], config_dir: Path
) -> None:
    broker["arrange"] = lambda market: market.contracts.update({555001: ("JDEP", "AEB", "EUR")})
    status, out = _run("universe", "pin", "JDEP")
    assert (status, out.strip()) == (EXIT_OK, "JDEP pinned to conid 555001 in universe.yaml")
    assert load_config(config_dir).settings.universe.instrument("JDEP").conid == 555001
    broker["arrange"] = lambda _market: None
    status, out = _run("universe", "pin", "LISN")
    assert status == EXIT_INVALID
    assert out.startswith("pin stopped: LISN on")


def test_pin_resolves_a_candidate_for_a_thesis(
    broker: dict[str, Any], config_dir: Path, data_dir: Path
) -> None:
    from obliskiq.data.candidates import parse_ishares

    settings = load_config(config_dir).settings
    parsed = parse_ishares(
        (FIXTURES / "ewj_us.csv").read_bytes(), fund="EWJ", url=EWJ_URL,
        downloaded_at=datetime.now(UTC), config=settings.collectors.candidates,
    )  # fmt: skip
    with open_store(settings, data_dir) as store:
        universe_repo.save_holdings(store, parsed.file, parsed.lines)
    broker["arrange"] = lambda market: market.contracts.update({13976: ("7203", "TSEJ", "JPY")})
    status, out = _run("universe", "pin", "--candidate", "jp:toyota_motor", "--thesis", "h-1")
    assert (status, out.strip()) == (EXIT_OK, "jp:toyota_motor pinned to conid 13976 for h-1")
    with open_store(settings, data_dir) as store:
        resolution = universe_repo.candidate_resolutions(store)["jp:toyota_motor"]
    assert (resolution.pinned, resolution.pinned_for) == (True, "h-1")
    status, out = _run("universe", "pin", "--candidate", "jp:sony", "--thesis", "h-2")
    assert status == EXIT_INVALID
    assert "did not resolve" in out
    status, out = _run("universe", "pin", "--candidate", "jp:nothing", "--thesis", "h-3")
    assert "refused: no candidate 'jp:nothing'" in out


def test_load_universe_reads_the_largest_candidates_on_their_home_listing(
    data_dir: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.data.candidates import parse_ishares

    settings = load_config(config_dir).settings
    with open_store(settings, data_dir) as store:
        for name, fund, url in (("ewj_us.csv", "EWJ", EWJ_URL), ("iwda_uk.csv", "IWDA", "u")):
            parsed = parse_ishares(
                (FIXTURES / name).read_bytes(), fund=fund, url=url,
                downloaded_at=datetime.now(UTC), config=settings.collectors.candidates,
            )  # fmt: skip
            universe_repo.save_holdings(store, parsed.file, parsed.lines)
    asked: list[str] = []

    def history(symbol: str, start: date, end: date) -> Any:
        asked.append(symbol)
        return _Frame()

    monkeypatch.setattr(research_cli, "yfinance_history", history)
    status, out = _run("research", "data", "load-universe", "--candidates", "1")
    assert status == EXIT_OK, out
    assert "  candidate_jp_mitsubishi_ufj_financial: 3 closes" in out
    assert "8306.T" in asked
    assert "SAP.DE" in asked  # Germany's largest, on its home venue
    assert "AAPL" in asked  # IWDA's US lines are the US's
    assert "ROP.SW" not in asked  # Switzerland is no market of the grid
