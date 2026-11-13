"""``obliskiq idea add|list|show|withdraw`` and the intake behind them: an idea is stored whole
or refused with its reason; none of them opens a brokerage session.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from thesis_rig import ISIN, NOW, TODAY, draft

from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import ObliskConfig, load_config
from obliskiq.data.candidates import parse_ishares
from obliskiq.decision.thesis_intake import ThesisIntake
from obliskiq.domain.thesis import View
from obliskiq.domain.universe import ScannerRow
from obliskiq.store import open_store
from obliskiq.store import theses as thesis_repo
from obliskiq.store import universe as universe_repo
from obliskiq.store.worker import StoreWorker

D = Decimal
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ishares"
TOYOTA = "jp:toyota_motor"
TOYOTA_ISIN = "JP3633400001"
IDEA = ("--expect", "copper tightens", "--target", "60", "--invalidation", "30")
HORIZON = (date.today() + timedelta(days=200)).isoformat()


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


def add(symbol: str, *extra: str) -> tuple[int, str]:
    return _run("idea", "add", symbol, *IDEA, "--horizon", HORIZON, *extra)


def seed_holdings(settings: ObliskConfig, data_dir: Path, at: datetime) -> None:
    parsed = parse_ishares(
        (FIXTURES / "ewj_us.csv").read_bytes(),
        fund="EWJ",
        url="https://example.invalid/ewj.csv",
        downloaded_at=at,
        config=settings.collectors.candidates,
    )
    with open_store(settings, data_dir) as store:
        universe_repo.save_holdings(store, parsed.file, parsed.lines)


# ── the commands ──


def test_an_idea_is_stored_listed_shown_and_withdrawn(data_dir: Path, config_dir: Path) -> None:
    assert _run("idea", "list") == (EXIT_OK, "no open idea: the book holds nothing\n")
    assert _run("idea", "list", "--all") == (EXIT_OK, "no idea\n")
    status, out = add("FCX", "--conviction", "high", "--entry-max", "45")
    assert status == EXIT_OK, out
    assert out.startswith("stored: T1 FCX candidate (owner, high, bullish): target 60, ")
    assert "the service judges it at its next cycle" in out
    status, out = _run("idea", "list")
    assert (status, out.count("\n")) == (EXIT_OK, 1)
    assert out.startswith("T1 FCX candidate")
    status, out = _run("idea", "show", "T1")
    assert status == EXIT_OK
    assert "instrument: Freeport-McMoRan Inc (conid 7089)" in out
    assert "expects: copper tightens" in out
    assert "entered only at or below 45" in out
    assert "intake (owner (command line)): handed in by owner, conviction high" in out
    status, out = _run("idea", "withdraw", "1")
    assert (status, out.split(":")[0]) == (EXIT_OK, "withdrawn")
    assert _run("idea", "list")[1] == "no open idea: the book holds nothing\n"
    assert "T1 FCX withdrawn" in _run("idea", "list", "--all")[1]
    status, out = _run("idea", "withdraw", "T1")
    assert status == EXIT_INVALID
    assert "T1 is withdrawn: an idea is withdrawn before its first fill" in out
    with open_store(load_config(config_dir).settings, data_dir) as store:
        assert [e.detail for e in thesis_repo.log_entries(store, 1)][-1] == (
            "withdrawn by its source"
        )


@pytest.mark.parametrize(
    ("argv", "words"),
    [
        (("FCX", "--expect", "x", "--invalidation", "30", "--horizon", HORIZON), "has no target"),
        (("FCX", "--expect", "x", "--target", "60", "--horizon", HORIZON), "no invalidation"),
        (("FCX", "--expect", "x", "--target", "60", "--invalidation", "30"), "has no horizon"),
        (("FCX", *IDEA[2:], "--horizon", HORIZON), "what is expected"),
        (("NOSUCH", *IDEA, "--horizon", HORIZON), "instrument_unknown: NOSUCH is not in"),
        (("ASML", *IDEA, "--horizon", HORIZON), "not_tradable: ASML cannot be ordered"),
        (("SPY", *IDEA, "--horizon", HORIZON), "not_tradable: SPY cannot be ordered"),
        (("FCX", *IDEA, "--horizon", HORIZON, "--bearish"), "bearish_not_defensive"),
        (("FCX", *IDEA, "--horizon", "soon"), "--horizon 'soon' is not a date"),
        (("FCX", "--expect", "x", "--target", "a lot"), "--target 'a lot' is not a price"),
        (("FCX", *IDEA, "--horizon", HORIZON, "--candidate", "jp:x", "--isin", "XX"), "isin"),
        ((*IDEA, "--horizon", HORIZON, "--candidate", "jp:nothing"), "needs its ISIN"),
        (
            (*IDEA, "--horizon", HORIZON, "--candidate", "jp:nothing", "--isin", TOYOTA_ISIN),
            "instrument_unknown: no stock candidate 'jp:nothing'",
        ),
    ],
)
def test_an_idea_that_cannot_be_traded_is_refused_and_not_stored(
    data_dir: Path, argv: tuple[str, ...], words: str
) -> None:
    status, out = _run("idea", "add", *argv)
    assert status == EXIT_INVALID
    assert "refused" in out
    assert words in out
    assert _run("idea", "list", "--all")[1] == "no idea\n"


def test_a_bearish_idea_is_stored_on_a_defensive_instrument(data_dir: Path) -> None:
    status, out = _run(
        "idea", "add", "SGLD", "--expect", "equities fall", "--target", "400",
        "--invalidation", "300", "--horizon", HORIZON, "--bearish", "--enter-by", HORIZON,
    )  # fmt: skip
    assert status == EXIT_OK, out
    assert "T1 SGLD candidate (owner, normal, bearish)" in out
    assert f"enter by {HORIZON}" in out


def test_a_share_outside_the_universe_file_is_named_by_its_candidate(
    data_dir: Path, config_dir: Path
) -> None:
    seed_holdings(load_config(config_dir).settings, data_dir, datetime.now(UTC))
    status, out = _run(
        "idea", "add", *IDEA[:2], "--target", "4000", "--invalidation", "2500",
        "--horizon", HORIZON, "--candidate", TOYOTA, "--isin", TOYOTA_ISIN,
    )  # fmt: skip
    assert status == EXIT_OK, out
    assert "T1 7203 candidate" in out
    shown = _run("idea", "show", "T1")[1]
    assert "instrument: TOYOTA MOTOR (not pinned yet)" in shown


def test_show_and_withdraw_refuse_what_names_no_idea(data_dir: Path) -> None:
    assert _run("idea", "show", "T9") == (EXIT_INVALID, "refused: no idea T9\n")
    assert _run("idea", "show", "nine") == (
        EXIT_INVALID,
        "refused: 'nine' names no idea: write T3 or 3\n",
    )
    assert _run("idea", "withdraw", "T9") == (EXIT_INVALID, "refused: no thesis 9\n")


# ── the intake ──


@pytest.fixture
def intake(store_worker: StoreWorker, shipped_settings: ObliskConfig) -> ThesisIntake:
    return ThesisIntake(shipped_settings, worker=store_worker, now=lambda: NOW)


async def test_a_universe_instrument_carries_its_sector_country_and_role(
    intake: ThesisIntake,
) -> None:
    stock = await intake.submit(draft(symbol="FCX"), actor="t")
    gold = await intake.submit(
        draft(symbol="SGLD", view=View.BEARISH, target=D(400), invalidation=D(300)), actor="t"
    )
    fund = await intake.submit(draft(symbol="SPPW"), actor="t")
    assert not isinstance(stock, list)
    assert not isinstance(gold, list)
    assert not isinstance(fund, list)
    assert (stock.instrument.sector, stock.instrument.country) == ("materials", "US")
    assert (stock.instrument.defensive, stock.instrument.isin) == (False, "US35671D8570")
    assert gold.instrument.defensive
    assert (fund.instrument.sector, fund.instrument.country) == (None, None)
    # No last entry day was named: the entry window, never past the horizon.
    assert stock.enter_by == TODAY + timedelta(days=20)
    short = await intake.submit(draft(horizon=TODAY + timedelta(days=5)), actor="t")
    assert not isinstance(short, list)
    assert short.enter_by == TODAY + timedelta(days=5)


async def test_an_instrument_on_a_venue_without_a_calendar_is_refused(
    store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    exchanges = {k: v for k, v in shipped_settings.ibkr_exchanges.items() if k != "NYSE"}
    settings = shipped_settings.model_copy(update={"ibkr_exchanges": exchanges})
    refused = await ThesisIntake(settings, worker=store_worker, now=lambda: NOW).submit(
        draft(symbol="FCX"), actor="t"
    )
    assert isinstance(refused, list)
    assert [f.code.value for f in refused] == ["no_calendar"]


async def test_a_candidate_needs_a_holdings_file_and_a_configured_listing(
    store_worker: StoreWorker, shipped_settings: ObliskConfig, intake: ThesisIntake
) -> None:
    def seed(store: Any) -> None:
        parsed = parse_ishares(
            (FIXTURES / "ewj_us.csv").read_bytes(),
            fund="EWJ",
            url="u",
            downloaded_at=NOW,
            config=shipped_settings.collectors.candidates,
        )
        universe_repo.save_holdings(store, parsed.file, parsed.lines)
        universe_repo.save_scanner_rows(
            store,
            [
                ScannerRow(
                    scanned_on=TODAY, location="STK.US.MAJOR", scan_type="MOST_ACTIVE", rank=0,
                    market="us", conid=265598, symbol="AAPL", listing_exchange="NASDAQ",
                    company_name="APPLE INC", candidate_key="conid:265598", scanned_at=NOW,
                )
            ],
        )  # fmt: skip

    await store_worker.run(seed)
    share = draft(
        symbol=None, candidate=TOYOTA, isin=TOYOTA_ISIN, target=D(4000), invalidation=D(2500)
    )
    stored = await intake.submit(share, actor="t")
    assert not isinstance(stored, list)
    held = stored.instrument
    assert (held.candidate_key, held.symbol, held.description) == (TOYOTA, "7203", "TOYOTA MOTOR")
    assert (held.sector, held.country, held.pinned) == ("consumer_discretionary", "JP", False)
    scanned = await intake.submit(
        share.model_copy(update={"candidate": "conid:265598", "isin": ISIN}), actor="t"
    )
    assert isinstance(scanned, list)
    assert "only a market scanner shows APPLE INC" in scanned[0].reason


async def test_a_candidate_listed_on_no_configured_venue_has_no_calendar(
    store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    bare = shipped_settings.collectors.candidates.model_copy(update={"exchanges": {}})
    nowhere = shipped_settings.model_copy(
        update={"collectors": shipped_settings.collectors.model_copy(update={"candidates": bare})}
    )

    def seed(store: Any) -> None:
        parsed = parse_ishares(
            (FIXTURES / "ewj_us.csv").read_bytes(),
            fund="EWJ",
            url="u",
            downloaded_at=NOW,
            config=bare,
        )
        universe_repo.save_holdings(store, parsed.file, parsed.lines)

    await store_worker.run(seed)
    share = draft(
        symbol=None, candidate=TOYOTA, isin=TOYOTA_ISIN, target=D(4000), invalidation=D(2500)
    )
    refused = await ThesisIntake(nowhere, worker=store_worker, now=lambda: NOW).submit(
        share, actor="t"
    )
    assert isinstance(refused, list)
    assert [f.code.value for f in refused] == ["no_calendar"]
