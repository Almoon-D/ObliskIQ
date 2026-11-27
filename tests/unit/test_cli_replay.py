"""``obliskiq research replay``, run in-process from the load to the report (H15): the SEC and
Yahoo answered by recorded shapes, the replayed drafts written as a run writes them, the probe's
answers trimming a recalled quarter, and the record beside the forward ledger.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
import io
import json
import logging
import math
import shutil
import subprocess
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.cli import replay as cli
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.config.loader import load_config
from obliskiq.domain.analyst import (
    REPLAY,
    AnalystCall,
    CallKind,
    DraftOutcome,
    DraftRecord,
    segment_label,
)
from obliskiq.domain.sources import ExtractionStatus
from obliskiq.env import variable_names
from obliskiq.llm.analyst_prompts import analyst_prompt_hash
from obliskiq.research.decide import Outcome
from obliskiq.research.replay.grade import Grader, ReplayCall, _lower
from obliskiq.research.replay.prices import stock_histories
from obliskiq.research.replay.probe import PROBE_SYSTEM
from obliskiq.research.replay.report import report_text
from obliskiq.research.replay.run import REPLAYED, plan_of
from obliskiq.scoreboard.rules import REPLAY_PATH, load_locked_replay
from obliskiq.store import analyst as analyst_repo
from obliskiq.store import open_replay_store
from obliskiq.store import replay as replay_repo
from obliskiq.store.replay import ProbeAnswer, Unit

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
AFTER = date(2025, 3, 3)
BEFORE = date(2024, 11, 25)
SECTIONS = ("instrument", "stock_screen")
INDEX = """CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
320193|Apple Inc.|10-Q|{day}|edgar/data/320193/a.txt
789019|MICROSOFT CORP|10-K|{day}|edgar/data/789019/b.txt
1018724|AMAZON COM INC|10-Q|{day}|edgar/data/1018724/c.txt
"""
TICKERS = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [320193, "Apple Inc.", "AAPL", "Nasdaq"],
        [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
        [1018724, "AMAZON COM INC", "AMZN", "Nasdaq"],
    ],
}
#: Each name's daily drift: Apple rises, Microsoft holds, Amazon falls.
DRIFT = {"AAPL": 0.002, "MSFT": 0.0, "AMZN": -0.001}


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


class Stamp:
    def __init__(self, day: date) -> None:
        self._day = day

    def date(self) -> date:
        return self._day


class Frame:
    def __init__(self, rows: list[tuple[date, dict[str, float]]]) -> None:
        self._rows = rows

    def iterrows(self) -> list[tuple[Stamp, dict[str, float]]]:
        return [(Stamp(day), row) for day, row in self._rows]


def yahoo(symbol: str, start: date, end: date) -> Frame:
    drift = DRIFT.get(symbol, 0.0005)
    rows = []
    close = 100.0
    day = start
    while day <= end:
        if day.weekday() < 5:
            rows.append(
                (day, {"Close": close, "Adj Close": close, "Dividends": 0.0, "Volume": 2e6})
            )
            close *= 1 + drift
        day += timedelta(days=1)
    return Frame(rows)


def ecb_zip() -> bytes:
    lines = ["Date,USD,GBP,CHF,JPY,"]
    day = date(2026, 11, 26)
    while day >= date(2021, 1, 1):
        if day.weekday() < 5:
            lines.append(f"{day.isoformat()},1.1,0.85,0.95,160,")
        day -= timedelta(days=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("eurofxref-hist.csv", "\n".join(lines) + "\n")
    return buffer.getvalue()


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


def _patch(monkeypatch: pytest.MonkeyPatch, config_dir: Path, data_dir: Path) -> None:
    monkeypatch.setenv("OBLISK_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    monkeypatch.setenv("SEC_USER_AGENT", "research test@example.com")
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    monkeypatch.setattr(cli, "yfinance_history", yahoo)

    async def download(url: str, path: Path) -> None:
        del url
        value = {"start": "2023-10-01", "end": "2024-09-28", "val": 1.0, "accn": "x"}
        facts = {"facts": {"us-gaap": {"GrossProfit": {"units": {"USD": [
            {**value, "filed": "2024-11-01"}
        ]}}}}}  # fmt: skip
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("CIK0000320193.json", json.dumps(facts))

    async def fetch(url: str, params: Any) -> bytes:
        del url, params
        return ecb_zip()

    monkeypatch.setattr(cli, "http_download", lambda *_, **__: download)
    monkeypatch.setattr(cli, "http_fetch", lambda *_, **__: fetch)


def _sec(router: respx.MockRouter) -> None:
    router.get(url__regex=r"full-index/\d{4}/QTR\d/xbrl\.idx").mock(
        side_effect=lambda request: httpx.Response(
            200, text=INDEX.format(day=_quarter_day(str(request.url)))
        )
    )
    router.get(url__regex=r"company_tickers_exchange\.json").mock(
        return_value=httpx.Response(200, json=TICKERS)
    )
    router.get(url__regex=r"data\.sec\.gov/submissions/").mock(
        return_value=httpx.Response(200, json={"sic": "3571"})
    )


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory, repo_root: Path) -> tuple[Path, Path, str]:
    """The replay's store loaded once (from the SEC's and Yahoo's recorded shapes), with the
    configuration it was loaded under and the load's output."""
    root = tmp_path_factory.mktemp("replay")
    config_dir = root / "config"
    shutil.copytree(repo_root / "config", config_dir)
    sources = config_dir / "sources.yaml"
    text = sources.read_text(encoding="utf-8")
    sources.write_text(
        text.replace("request_spacing_seconds: 0.15", "request_spacing_seconds: 0").replace(
            "request_spacing_seconds: 0.3", "request_spacing_seconds: 0"
        ),
        encoding="utf-8",
    )
    data_dir = root / "data"
    with pytest.MonkeyPatch.context() as monkeypatch:
        for name in (*variable_names(), "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        _patch(monkeypatch, config_dir, data_dir)
        with respx.mock(assert_all_called=False) as router:
            _sec(router)
            status, out = _run("research", "replay", "load")
    assert status == EXIT_OK, out
    return config_dir, data_dir, out


@pytest.fixture
def data_dir(
    template: tuple[Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A copy of the loaded store and its configuration, this test's own."""
    config_dir, loaded, _ = template
    mine = tmp_path / "data"
    shutil.copytree(loaded, mine)
    own_config = tmp_path / "config"
    shutil.copytree(config_dir, own_config)
    _patch(monkeypatch, own_config, mine)
    return mine


@pytest.fixture
def config_dir(data_dir: Path) -> Path:
    return data_dir.parent / "config"


def settings_of(config_dir: Path) -> ObliskConfig:
    return load_config(config_dir).settings


def replayed(store: Any, settings: ObliskConfig, day: date, reference: str) -> None:
    prompt_hash = analyst_prompt_hash(settings, "lite", sections=SECTIONS)
    call = analyst_repo.record_call(
        store,
        AnalystCall(
            call_id=0,
            kind=CallKind.DOSSIER,
            thesis_id=None,
            as_of=day,
            sheet_hash="a" * 64,
            produced_at=NOW,
            provider="gemini",
            model_id="gemini-3.1-flash-lite",
            prompt_version=1,
            prompt_hash=prompt_hash,
            status=ExtractionStatus.OK,
            attempts=1,
            output="{}",
            error=None,
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.01,
            segment="lite",
            record=REPLAY,
        ),
    )

    def draft(section: str, symbol: str, target: float, invalidation: float) -> str:
        return json.dumps(
            {
                "section": section,
                "symbol": symbol,
                "view": "bullish",
                "expectation": "it keeps rising",
                "target": target,
                "invalidation": invalidation,
                "horizon": (day + timedelta(days=60)).isoformat(),
                "probability": 0.6,
                "facts": ["F2"],
            }
        )

    analyst_repo.record_drafts(
        store,
        [
            DraftRecord(
                call_id=call.call_id,
                seq=1,
                draft=draft("stock", "AAPL", 1e6, 1.0),
                facts="[]",
                outcome=DraftOutcome.REFUSED,
                reason=REPLAYED,
                thesis_id=None,
            ),
            DraftRecord(
                call_id=call.call_id,
                seq=2,
                draft=draft("instrument", reference, 1e6, 1.0),
                facts="[]",
                outcome=DraftOutcome.REFUSED,
                reason=REPLAYED,
                thesis_id=None,
            ),
            DraftRecord(
                call_id=call.call_id,
                seq=3,
                draft='{"symbol": "AAPL"}',
                facts="[]",
                outcome=DraftOutcome.DROPPED,
                reason="the draft is not the schema's",
                thesis_id=None,
            ),
        ],
    )
    replay_repo.record_unit(
        store,
        Unit(
            label=segment_label("lite", prompt_hash),
            segment="lite",
            sheet_date=day,
            side="after" if day == AFTER else "before",
            state="ok",
            reason="1 draft",
            cost_usd=0.01,
            finished_at=NOW,
        ),
    )


def probed(store: Any, seq: int) -> None:
    """One probe answer in 2025Q1, right every time on a sample split down the middle."""
    rose = seq % 2 == 0
    replay_repo.record_probe(
        store,
        ProbeAnswer(
            provider="gemini",
            model_id="gemini-3.1-flash-lite",
            quarter="2025Q1",
            seq=seq,
            sheet_date=AFTER,
            ticker="AAPL",
            rose=rose,
            status=ExtractionStatus.OK,
            probability=0.9 if rose else 0.1,
            error=None,
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
            produced_at=NOW,
        ),
    )


def locked_copy(tmp_path: Path, repo_root: Path) -> Path:
    """The locked registration in a repository of its own, so the report is written there."""
    root = tmp_path / "repo"
    target = root / REPLAY_PATH
    target.parent.mkdir(parents=True)
    shutil.copy(repo_root / REPLAY_PATH, target)
    shutil.copy((repo_root / REPLAY_PATH).with_suffix(".lock"), target.with_suffix(".lock"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return target


def test_the_replay_from_its_load_to_its_report(
    template: tuple[Path, Path, str],
    data_dir: Path,
    config_dir: Path,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    out = template[2]
    assert "filings: " in out
    assert "sheet dates closed" in out
    status, out = _run("scoreboard", "ledger")
    assert "- no replayed sheet yet (obliskiq research replay run)." in out

    settings = settings_of(config_dir)
    reference = next(
        d.symbol
        for d in settings.universe.instruments
        if d.conid == settings.portfolio_circuit_breaker.equity_reference_conid
    )
    with open_replay_store(settings, data_dir) as store:
        stored = replay_repo.cohorts(store)
        assert stored[AFTER].evaluable == 3
        assert stored[AFTER].benchmark is not None
        for day in (AFTER, BEFORE):
            replayed(store, settings, day, reference)
        for seq in range(1, 7):
            probed(store, seq)

    prereg = locked_copy(tmp_path, repo_root)
    status, out = _run("research", "replay", "report", "--prereg", str(prereg))
    assert status == EXIT_OK, out
    report = (prereg.parents[1] / "reports" / "analyst-replay.md").read_text(encoding="utf-8")
    assert "### lite" in report
    assert "- Calls: 4 valid drafts (2 on stocks), 2 dropped by the forward checks." in report
    # Six right answers of six on an even split recall 2025Q1: its after sheet is trimmed.
    assert "the verdict that counts**: 0 calls graded" in report
    assert "quarters trimmed by the probe: 2025Q1" in report
    assert "- After the cutoff, untrimmed: 1 calls graded in 1 weeks" in report
    assert "- Before the cutoff (the leak control): 1 calls graded in 1 weeks" in report
    assert "- After the cutoff, over the equity reference (beside): 2 calls graded" in report
    assert "2 horizon; 2 resolved by the prices' end" in report
    assert "skill undefined" in report
    assert "6/6 hits against a majority of 50% (recalled)" in report
    assert "2 sheet runs (0 interrupted); spent 0.03 USD" in report

    status, out = _run("scoreboard", "ledger")
    assert status == EXIT_OK
    assert "after the cutoff 0 calls graded" in out


def _quarter_day(url: str) -> str:
    """A filing day inside the index's quarter, so each filer is one within the lookback."""
    year, quarter = url.split("full-index/")[1].split("/")[:2]
    month = (int(quarter.removeprefix("QTR")) - 1) * 3 + 2
    return date(int(year), month, 15).isoformat()


def test_the_report_is_refused_without_a_locked_registration(
    data_dir: Path, tmp_path: Path
) -> None:
    unlocked = tmp_path / "analyst-replay.md"
    unlocked.write_text("not the registration", encoding="utf-8")
    status, out = _run("research", "replay", "report", "--prereg", str(unlocked))
    assert status == EXIT_INVALID
    assert "refused:" in out


def a_replayed_call(sheet: date, symbol: str, *, stock: bool = True, **changes: Any) -> Any:
    fields: dict[str, Any] = {
        "label": "lite@x",
        "segment": "lite",
        "side": "after",
        "sheet": sheet,
        "stock": stock,
        "symbol": symbol,
        "rises": True,
        "target": Decimal(1_000_000),
        "invalidation": Decimal(1),
        "horizon": sheet + timedelta(days=60),
        "probability": 0.6,
        "call_id": 1,
    }
    fields.update(changes)
    return ReplayCall(**fields)


def test_the_grader_says_why_a_call_is_not_evaluable(data_dir: Path, config_dir: Path) -> None:
    settings = settings_of(config_dir)
    spec = load_locked_replay(REPLAY_PATH).spec
    plan = plan_of(spec, settings)
    span = cli._span(settings, plan)
    with open_replay_store(settings, data_dir) as store:
        histories = stock_histories(store)
        # A name with prices and no listing snapshot (its exchange is unknown).
        histories["ORPH"] = histories["AAPL"]

        def grader(of: ObliskConfig = settings) -> Grader:
            return Grader(
                store,
                of,
                spec,
                histories=histories,
                cohorts=replay_repo.cohorts(store),
                sessions=cli._sessions(of, span),
            )

        found = grader()
        end = span.until

        def reasons(item: Any, using: Grader = found) -> tuple[str | None, ...]:
            graded = using.grade(item, prices_end=end)
            primary = None if graded.primary is None else graded.primary.reason
            return primary, graded.reference.reason, graded.levels.reason

        assert reasons(a_replayed_call(AFTER, "ZZZZ")) == (
            "no close at Yahoo",
            "no close at Yahoo",
            "no close at Yahoo",
        )
        assert reasons(a_replayed_call(AFTER, "ZZZZ", stock=False)) == (
            None,
            "not a universe instrument",
            "not an instrument",
        )
        late = date(2026, 11, 23)
        assert (
            reasons(a_replayed_call(late, "AAPL")) == ("its horizon passes the calendar's end",) * 3
        )
        # A Monday inside the margin is no sheet and has no cohort: not evaluable whole.
        off = reasons(a_replayed_call(date(2025, 1, 6), "AAPL"))
        assert off[0] == "its cohort is not evaluable whole"
        assert off[1] is None
        # Without the rates, nothing in dollars is read in the base currency.
        replay = settings.llm.replay.model_copy(update={"fx_dataset": "ff3_monthly"})
        no_fx = settings.model_copy(
            update={"llm": settings.llm.model_copy(update={"replay": replay})}
        )
        blind = grader(no_fx)
        assert (
            reasons(a_replayed_call(AFTER, "AAPL"), blind)[:2]
            == ("no dollar rate within the age allowed",) * 2
        )
        # An index without a close held, and a fund whose currency has no rate.
        assert reasons(a_replayed_call(AFTER, "SPX", stock=False))[1] == (
            "no close of it within the age allowed"
        )
        assert reasons(a_replayed_call(AFTER, "K200", stock=False))[1:] == (
            "no KRW rate within the age allowed",
            "K200 has no mark in the base currency",
        )
        # A name whose closes stop before the horizon has no return, nor a round trip.
        stale = dict(histories)
        aapl = histories["AAPL"]
        cut = sum(1 for day in aapl.sessions if day < date(2025, 1, 1))
        stale["AAPL"] = type(aapl)(aapl.sessions[:cut], aapl.closes[:cut], aapl.log_changes[:cut])
        stopped = Grader(
            store,
            settings,
            spec,
            histories=stale,
            cohorts=replay_repo.cohorts(store),
            sessions=cli._sessions(settings, span),
        ).grade(a_replayed_call(AFTER, "AAPL"), prices_end=end)
        assert stopped.primary is not None
        assert (stopped.primary.reason or "").startswith("no close within 7 days after")
        # Without the equity reference in the universe nothing is read over it.
        breaker = settings.portfolio_circuit_breaker.model_copy(
            update={"equity_reference_conid": 2}
        )
        unreferenced = grader(settings.model_copy(update={"portfolio_circuit_breaker": breaker}))
        assert reasons(a_replayed_call(AFTER, "AAPL"), unreferenced)[1:] == (
            "the equity reference is not a universe instrument",
            "no mark of the equity reference within the age allowed",
        )
        # A name with no listing snapshot cannot price its round trip.
        orphan = found.grade(a_replayed_call(AFTER, "ORPH"), prices_end=end)
        assert orphan.primary is not None
        assert orphan.primary.reason == "ORPH has no venue, close or rate to price its round trip"


def test_a_whole_record_is_graded_its_leak_measured_and_nothing_proposed_on_it(
    data_dir: Path, config_dir: Path
) -> None:
    settings = settings_of(config_dir)
    spec = load_locked_replay(REPLAY_PATH).spec
    plan = plan_of(spec, settings)
    _, dates = plan.segments[0]
    prompt_hash = analyst_prompt_hash(settings, "lite", sections=SECTIONS)
    names = ("AAPL", "MSFT", "AMZN")
    with open_replay_store(settings, data_dir) as store:
        for n, day in enumerate(dates.all):
            call = analyst_repo.record_call(
                store,
                AnalystCall(
                    call_id=0,
                    kind=CallKind.DOSSIER,
                    thesis_id=None,
                    as_of=day,
                    sheet_hash="a" * 64,
                    produced_at=NOW,
                    provider="gemini",
                    model_id="gemini-3.1-flash-lite",
                    prompt_version=1,
                    prompt_hash=prompt_hash,
                    status=ExtractionStatus.OK,
                    attempts=1,
                    output="{}",
                    error=None,
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.001,
                    segment="lite",
                    record=REPLAY,
                ),
            )
            # Five drafts a sheet, as the registration's power assumes.
            records = []
            for k in range(1, 8 if n == len(dates.before) else 6):
                rises = (n + k) % 2 == 0 or k == 1
                # The first draft's target is below the price: it is reached at once, so some
                # calls come out right and the calibration has a climatology to beat.
                target, invalidation = (2.0, 1.0) if k == 1 else (1e6, 1.0)
                body = {
                    "section": "stock",
                    "symbol": "ZZZZ" if k >= 6 else names[(n + k) % 3],
                    "view": "bullish" if rises else "bearish",
                    "expectation": "it moves as its drift says",
                    "target": target if rises else 1.0,
                    "invalidation": invalidation if rises else 1e6,
                    "horizon": (day + timedelta(days=60)).isoformat(),
                    "probability": 0.55,
                    "facts": ["F2"],
                }
                records.append(
                    DraftRecord(
                        call_id=call.call_id,
                        seq=k,
                        draft=json.dumps(body),
                        facts="[]",
                        outcome=DraftOutcome.REFUSED,
                        # The seventh is refused for another reason: not a replayed draft.
                        reason=REPLAYED if k < 7 else "another reason",
                        thesis_id=None,
                    )
                )
            analyst_repo.record_drafts(store, records)
        book = cli.graded_book(settings, plan, store)
    lite = book.segments[0]
    assert (lite.calls, lite.stock_calls) == (861, 861)
    assert lite.not_evaluable == {"no close at Yahoo": 1}
    assert (lite.after.graded, lite.before.graded) == (430, 430)
    assert lite.after.decision is not None
    assert lite.before.decision is not None
    assert lite.after_without_best.graded == 425
    assert lite.leak.difference == pytest.approx(
        (lite.before.estimate.mean or 0.0) - (lite.after.estimate.mean or 0.0)
    )
    assert lite.leak.interval is not None
    sure = dataclasses.replace(
        lite,
        after=dataclasses.replace(
            lite.after,
            decision=dataclasses.replace(lite.after.decision, outcome=Outcome.INCONCLUSIVE),
        ),
    )
    assert (sure.verdict, sure.sufficient, sure.negative) == ("inconclusive", False, False)
    assert _lower(lite.after) == lite.after.estimate.ci[0]  # type: ignore[index]
    no_interval = dataclasses.replace(lite.after.estimate, ci=None)
    assert _lower(dataclasses.replace(lite.after, estimate=no_interval)) == -math.inf
    assert lite.verdict in {"insufficient", "negative", "sufficient", "inconclusive"}
    assert book.tests == 1
    text = report_text(book, spec, as_of=NOW.date())
    assert f"its picks did {abs(lite.after.estimate.mean or 0.0):.1%} a month" in text
    assert "Leak measure: before minus after" in text
    assert "- Not evaluable: 1 no close at Yahoo." in text
    assert "against its climatology's" in text
    assert lite.calibration.skill is not None


def test_the_probe_then_every_sheet_replayed_by_the_command(
    data_dir: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = config_dir / "llm.yaml"
    llm.write_text(
        llm.read_text(encoding="utf-8").replace("    max_usd: 0\n", "    max_usd: 500\n"),
        encoding="utf-8",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-secret")

    def answer(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        content = '{"probability": 0.6}' if system == PROBE_SYSTEM else '{"candidates": []}'
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20},
            },
        )

    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=r"generativelanguage").mock(side_effect=answer)
        status, out = _run("research", "replay", "run")
        assert status == EXIT_INVALID
        assert "the probe has questions unanswered" in out
        status, out = _run("research", "replay", "probe")
        assert status == EXIT_OK, out
        asked = route.call_count
        status, out = _run("research", "replay", "run")
        assert status == EXIT_OK, out
        assert route.call_count == asked + 2 * 172  # one scan a segment a sheet, nothing named
        assert "done 344, already held 0, interrupted 0" in out
        status, out = _run("research", "replay", "run")
    assert status == EXIT_OK
    assert "done 0, already held 344, interrupted 0" in out
    assert route.call_count == asked + 344
    settings = settings_of(config_dir)
    with open_replay_store(settings, data_dir) as store:
        probes = replay_repo.probes(store)
        assert {a.model_id for a in probes} == {"gemini-3.1-flash-lite", "gemini-3.8-flash"}
        assert len(probes) == asked
        assert len(replay_repo.units(store)) == 344


def test_the_ledger_says_when_the_replay_s_rules_cannot_be_read(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_prereg_path", lambda: tmp_path / "missing.md")
    status, out = _run("scoreboard", "ledger")
    assert status == EXIT_OK
    assert "- not graded: missing.md has no readable lock" in out
