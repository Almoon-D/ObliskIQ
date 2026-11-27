"""The analyst replayed after its models' cutoff (H15): the locked registration's dates and
models, the refusals (a model without its own cutoff, an alias, a date inside the margin, a run
past the cap or before the probe), the SEC's index, tickers and facts as parsed, the point in
time of the replay's store, a cohort's not-evaluable names, the probe's scoring, a stopped run
that resumes without repeating a paid call, and the replay's record kept out of the forward reads.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import dataclasses
import itertools
import json
import math
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.cli.replay import graded_book
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_sources import FrameInputConfig
from obliskiq.domain.analyst import FORWARD, REPLAY, AnalystCall, CallKind, DraftOutcome
from obliskiq.domain.sources import ExtractionStatus
from obliskiq.errors import (
    DataSourceError,
    PreregistrationError,
    ReplayRefusedError,
    UpstreamError,
)
from obliskiq.grid.facts import build_fact_sheet
from obliskiq.llm.analyst_prompts import analyst_prompt_hash
from obliskiq.research.data import Sources
from obliskiq.research.prereg_replay import AnalystReplaySpec
from obliskiq.research.replay.plan import Dates, model_status, sheet_dates
from obliskiq.research.replay.prices import NO_HISTORY, NO_TICKER, Cohort, History, cohort
from obliskiq.research.replay.probe import Question, binomial_tail, draw, score
from obliskiq.research.replay.report import ledger_lines, report_text
from obliskiq.research.replay.run import Plan, Replayer, plan_of
from obliskiq.research.replay.sources import (
    ReplaySources,
    Span,
    end_of,
    parse_companyfacts,
    parse_names,
    parse_xbrl_index,
    period_of,
    universe_at,
)
from obliskiq.scoreboard.rules import REPLAY_PATH, load_locked_replay
from obliskiq.store import Store, open_replay_store
from obliskiq.store import analyst as analyst_repo
from obliskiq.store import replay as replay_repo
from obliskiq.store import stocks as stocks_repo
from obliskiq.store.replay import Filing, Name, ProbeAnswer
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
COMPLETIONS = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
KEYS = {"gemini": "test-key-not-a-secret"}
SHEET = date(2025, 3, 3)
CONTROL = date(2024, 11, 25)


@pytest.fixture(scope="module")
def spec(repo_root: Path) -> AnalystReplaySpec:
    return load_locked_replay(repo_root / REPLAY_PATH).spec


def capped(settings: ObliskConfig, usd: float) -> ObliskConfig:
    replay = settings.llm.replay.model_copy(update={"max_usd": usd})
    return settings.model_copy(
        update={
            "llm": settings.llm.model_copy(update={"replay": replay}),
            "http": settings.http.model_copy(update={"retries": 0}),
        }
    )


def with_model(settings: ObliskConfig, name: str, **changes: Any) -> ObliskConfig:
    models = dict(settings.llm.models)
    models[name] = models[name].model_copy(update=changes)
    return settings.model_copy(update={"llm": settings.llm.model_copy(update={"models": models})})


@pytest.fixture
def replay_store(tmp_path: Path, shipped_settings: ObliskConfig) -> Store:
    store = open_replay_store(shipped_settings, tmp_path)
    yield store  # type: ignore[misc]
    store.close()


@pytest.fixture
async def replay_worker(
    replay_store: Store, shipped_settings: ObliskConfig, tmp_path: Path
) -> AsyncIterator[StoreWorker]:
    worker = StoreWorker(lambda: open_replay_store(shipped_settings, tmp_path))
    yield worker
    await worker.aclose()


def a_call(**changes: Any) -> AnalystCall:
    fields: dict[str, Any] = {
        "call_id": 0,
        "kind": CallKind.SCAN,
        "thesis_id": None,
        "as_of": CONTROL,
        "sheet_hash": "a" * 64,
        "produced_at": NOW - timedelta(hours=1),
        "provider": "gemini",
        "model_id": "gemini-3.1-flash-lite",
        "prompt_version": 1,
        "prompt_hash": "b" * 64,
        "status": ExtractionStatus.OK,
        "attempts": 1,
        "output": '{"candidates": []}',
        "error": None,
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.25,
        "segment": "lite",
        "record": REPLAY,
    }
    fields.update(changes)
    return AnalystCall(**fields)


# ── the registration ──


def test_the_locked_registration_gives_its_segments_and_dates(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig
) -> None:
    plan = plan_of(spec, shipped_settings)
    assert [segment.name for segment, _ in plan.segments] == ["lite", "mid"]
    assert plan.models == ("gemini_flash", "gemini_flash_lite")
    _, dates = plan.segments[0]
    assert dates.cutoff == date(2025, 1, 31)
    assert (dates.after[0], dates.after[-1], len(dates.after)) == (
        date(2025, 3, 3),
        date(2026, 10, 19),
        86,
    )
    assert (dates.before[0], dates.before[-1], len(dates.before)) == (
        date(2023, 4, 10),
        date(2024, 11, 25),
        86,
    )
    assert all(day.weekday() == 0 for day in dates.all)


def test_a_date_inside_the_margin_or_off_the_windows_is_refused(spec: AnalystReplaySpec) -> None:
    dates = sheet_dates(spec, spec.segments[0])
    assert dates.side(SHEET) == "after"
    assert dates.side(CONTROL) == "before"
    with pytest.raises(ReplayRefusedError, match="inside the margin"):
        dates.side(date(2025, 1, 6))
    with pytest.raises(ReplayRefusedError, match="not a registered sheet date"):
        dates.side(date(2025, 3, 4))
    with pytest.raises(ReplayRefusedError, match="not a registered sheet date"):
        dates.side(date(2022, 1, 3))


def test_a_model_without_its_own_cutoff_refuses_the_replay(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig
) -> None:
    assert model_status(spec, shipped_settings, "gemini_flash_lite") is None
    undocumented = with_model(
        shipped_settings, "gemini_flash_lite", training_cutoff=None, cutoff_source=None
    )
    assert (model_status(spec, undocumented, "gemini_flash_lite") or "").startswith("no_cutoff")
    with pytest.raises(ReplayRefusedError, match="no_cutoff"):
        plan_of(spec, undocumented)
    later = with_model(shipped_settings, "gemini_flash_lite", training_cutoff="2025-06")
    assert "is not the registered" in (model_status(spec, later, "gemini_flash_lite") or "")


def test_an_alias_a_provider_may_refresh_has_no_cutoff(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig
) -> None:
    alias = "gemini-flash-lite-latest"
    registered = spec.model_copy(
        update={
            "models": [
                m.model_copy(update={"model_id": alias}) if m.name == "gemini_flash_lite" else m
                for m in spec.models
            ]
        }
    )
    configured = with_model(shipped_settings, "gemini_flash_lite", model_id=alias)
    assert (model_status(registered, configured, "gemini_flash_lite") or "").startswith("no_cutoff")
    other = with_model(shipped_settings, "gemini_flash_lite", model_id="gemini-3.2-flash-lite")
    assert "registered as" in (model_status(spec, other, "gemini_flash_lite") or "")


# ── the SEC's records as parsed ──

INDEX = """Description:           XBRL Index of EDGAR Dissemination Feed by Company Name
Last Data Received:    March 31, 2025

CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
320193|Apple Inc.|10-Q|2025-01-31|edgar/data/320193/0000320193-25-000008.txt
320193|Apple Inc.|10-K/A|2025-02-03|edgar/data/320193/0000320193-25-000010.txt
789019|MICROSOFT CORP|8-K|2025-01-29|edgar/data/789019/0000950170-25-000010.txt
"""


def test_the_index_keeps_the_registered_forms_each_known_at_its_day_s_end() -> None:
    found = parse_xbrl_index(INDEX, ["10-K", "10-Q"])
    assert found == [
        Filing(320193, "10-Q", date(2025, 1, 31), "Apple Inc.", end_of(date(2025, 1, 31)))
    ]
    assert found[0].known_at == datetime(2025, 1, 31, 23, 59, 59, tzinfo=UTC)
    with pytest.raises(DataSourceError, match="no dashed line"):
        parse_xbrl_index("CIK|Company Name\n320193|Apple", ["10-K"])
    with pytest.raises(DataSourceError, match="another shape"):
        parse_xbrl_index(INDEX + "1|two|fields\n", ["10-K"])
    with pytest.raises(DataSourceError, match="cannot read"):
        parse_xbrl_index(INDEX + "1|A|10-K|31/01/2025|x.txt\n", ["10-K"])


def test_names_keep_the_first_ticker_the_sec_lists_for_a_company() -> None:
    body = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [320193, "Apple Inc.", "AAPL", "Nasdaq"],
            [320193, "Apple Inc.", "AAPL-W", "Nasdaq"],
            [1750, "AAR CORP", " AIR ", None],
            [2, "No ticker", None, "NYSE"],
        ],
    }
    found = parse_names(body, read_at=NOW)
    assert found == [Name(320193, "AAPL", "Nasdaq", NOW), Name(1750, "AIR", None, NOW)]
    with pytest.raises(UpstreamError, match="another shape"):
        parse_names({"data": [[1, "x"]]}, read_at=NOW)


def test_a_value_belongs_to_its_calendar_frame() -> None:
    assert period_of("annual", date(2024, 1, 1), date(2024, 12, 31)) == "CY2024"
    assert period_of("annual", date(2023, 10, 1), date(2024, 9, 28)) == "CY2024"
    assert period_of("annual", date(2024, 7, 1), date(2024, 9, 30)) is None
    assert period_of("annual", None, date(2024, 12, 31)) is None
    assert period_of("instant", None, date(2024, 9, 28)) == "CY2024Q3I"
    assert period_of("instant", None, date(2025, 1, 2)) == "CY2024Q4I"
    assert period_of("instant", None, date(2024, 11, 15)) is None
    assert period_of("instant", date(2024, 1, 1), date(2024, 3, 31)) is None


def test_company_facts_are_stamped_with_the_day_each_value_was_filed() -> None:
    inputs = {
        "revenue": FrameInputConfig(taxonomy="us-gaap", tag="Revenues", unit="USD", kind="annual")
    }
    values = [
        {
            "start": "2024-01-01",
            "end": "2024-12-31",
            "val": 200,
            "accn": "b",
            "filed": "2025-02-20",
        },
        {
            "start": "2023-01-01",
            "end": "2023-12-31",
            "val": 100,
            "accn": "a",
            "filed": "2024-02-20",
        },
        {"start": "2024-10-01", "end": "2024-12-31", "val": 60, "accn": "c", "filed": "2025-02-20"},
    ]
    body = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": values}}}}}
    found = parse_companyfacts(body, cik=7, inputs=inputs)
    assert [(f.period, f.value, f.known_at) for f in found] == [
        ("CY2023", 100.0, end_of(date(2024, 2, 20))),
        ("CY2024", 200.0, end_of(date(2025, 2, 20))),
    ]
    with pytest.raises(DataSourceError, match="another shape"):
        parse_companyfacts(
            {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"end": "2024-12-31"}]}}}}},
            cik=7,
            inputs=inputs,
        )


# ── the point in time ──


def test_a_filing_published_after_the_sheet_changes_neither_universe_nor_sheet(
    replay_store: Store, shipped_settings: ObliskConfig, spec: AnalystReplaySpec
) -> None:
    def filing(cik: int, day: date) -> Filing:
        return Filing(cik, "10-Q", day, f"Company {cik}", end_of(day))

    replay_repo.save_filings(
        replay_store, [filing(1, date(2024, 11, 5)), filing(2, SHEET), filing(3, date(2023, 6, 1))]
    )
    before = universe_at(replay_store, SHEET, lookback_months=15)
    sheet = build_fact_sheet(
        replay_store, shipped_settings, as_of=SHEET, sections=spec.sheet.sections
    )
    replay_repo.save_filings(replay_store, [filing(4, SHEET + timedelta(days=1))])
    assert before == {1: "Company 1", 2: "Company 2"}
    assert universe_at(replay_store, SHEET, lookback_months=15) == before
    again = build_fact_sheet(
        replay_store, shipped_settings, as_of=SHEET, sections=spec.sheet.sections
    )
    assert again.text() == sheet.text()
    assert "this sheet omits" in sheet.text()
    assert {line.section for line in sheet.lines} <= {"sheet", *spec.sheet.sections}


# ── the cohorts ──


def history(start: date, closes: list[float]) -> History:
    sessions = tuple(start + timedelta(days=i) for i in range(len(closes)))
    changes = (math.nan, *(math.log(b / a) for a, b in itertools.pairwise(closes)))
    return History(sessions, tuple(closes), changes)


def test_a_history_s_return_or_why_it_has_none() -> None:
    found = history(date(2025, 3, 4), [10.0, 11.0, 12.1])
    assert found.growth(date(2025, 3, 4), date(2025, 3, 6), max_age_days=7) == pytest.approx(1.21)
    assert "no close within 7 days after" in str(
        found.growth(date(2025, 4, 1), date(2025, 4, 30), max_age_days=7)
    )
    gap = History((date(2025, 3, 4), date(2025, 3, 20)), (10.0, 11.0), (math.nan, math.log(1.1)))
    assert "no close between" in str(
        gap.growth(date(2025, 3, 4), date(2025, 3, 20), max_age_days=7)
    )
    unknown = History((date(2025, 3, 4), date(2025, 3, 5)), (10.0, 11.0), (math.nan, math.nan))
    assert "is unknown" in str(unknown.growth(date(2025, 3, 4), date(2025, 3, 5), max_age_days=7))


def test_a_name_without_a_ticker_or_prices_is_counted_not_evaluable(replay_store: Store) -> None:
    replay_repo.save_filings(
        replay_store,
        [
            Filing(cik, "10-K", date(2024, 12, 2), f"C{cik}", end_of(date(2024, 12, 2)))
            for cik in (1, 2, 3)
        ],
    )
    replay_repo.save_names(
        replay_store,
        [Name(1, "AAPL", "Nasdaq", NOW), Name(2, None, None, NOW), Name(3, "MSFT", "Nasdaq", NOW)],
    )
    histories = {"AAPL": history(date(2025, 3, 4), [10.0] * 10 + [12.0])}
    kwargs: dict[str, Any] = {
        "sheet": SHEET,
        "horizon": (date(2025, 3, 4), date(2025, 3, 14)),
        "lookback_months": 15,
        "max_age_days": 7,
    }
    strict = cohort(replay_store, histories, limit=Decimal("0.35"), **kwargs)
    assert strict.universe == 3
    assert dict(strict.reasons) == {NO_TICKER: 1, NO_HISTORY: 1}
    assert strict.growth == {"AAPL": pytest.approx(1.2)}
    assert strict.benchmark is None  # two of three not evaluable: the cohort is not, whole
    loose = cohort(replay_store, histories, limit=Decimal(1), **kwargs)
    assert loose.benchmark == pytest.approx(1.2)


# ── the probe ──


def answer(seq: int, probability: float | None, rose: bool, quarter: str = "2025Q2") -> ProbeAnswer:
    return ProbeAnswer(
        provider="gemini",
        model_id="gemini-3.1-flash-lite",
        quarter=quarter,
        seq=seq,
        sheet_date=date(2025, 4, 7),
        ticker="AAPL",
        rose=rose,
        status=ExtractionStatus.OK if probability is not None else ExtractionStatus.INVALID,
        probability=probability,
        error=None if probability is not None else "not JSON",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        produced_at=NOW,
    )


def test_the_binomial_tail() -> None:
    assert binomial_tail(10, 10, 0.5) == pytest.approx(0.5**10)
    assert binomial_tail(0, 7, 0.3) == pytest.approx(1.0)
    assert binomial_tail(8, 10, 0.8) == pytest.approx(0.6778, abs=1e-4)


def test_a_quarter_is_recalled_only_when_hits_beat_its_majority() -> None:
    # 16 of 20 rose: always saying "higher" hits 16, no more than the majority.
    lazy = [answer(i, 0.9, i < 16) for i in range(20)]
    # The same quarter answered right every time, and one invalid answer left unscored.
    knows = [answer(i, 0.9 if i < 16 else 0.1, i < 16, "2025Q3") for i in range(20)]
    knows.append(answer(20, None, True, "2025Q3"))
    found = {r.quarter: r for r in score(lazy + knows)}
    assert (found["2025Q2"].hits, found["2025Q2"].majority) == (16, 0.8)
    assert not found["2025Q2"].recalled(0.05)
    assert (found["2025Q3"].asked, found["2025Q3"].answered, found["2025Q3"].hits) == (21, 20, 20)
    assert found["2025Q3"].p_value == pytest.approx(0.8**20)
    assert found["2025Q3"].recalled(0.05)


def test_the_probe_s_questions_are_drawn_alike_every_time(spec: AnalystReplaySpec) -> None:
    def a_cohort(sheet: date, n: int, *, whole: bool = True) -> Cohort:
        growth = {f"T{sheet.month}{i}": 1 + (i % 3 - 1) / 10 for i in range(n)}
        return Cohort(sheet, sheet, sheet, n, growth, benchmark=1.0 if whole else None)

    cohorts = [
        a_cohort(date(2025, 3, 3), 30),
        a_cohort(date(2025, 3, 10), 30),
        a_cohort(date(2025, 4, 7), 10),
        a_cohort(date(2025, 4, 14), 50, whole=False),
    ]
    companies = {date(2025, 3, 3): {"T30": "Thirty Inc."}}
    first = draw(spec, cohorts, companies)
    assert first == draw(spec, cohorts, companies)
    by_quarter = {q.quarter for q in first}
    assert by_quarter == {"2025Q1", "2025Q2"}
    assert sum(q.quarter == "2025Q1" for q in first) == spec.probe.questions_per_quarter
    assert sum(q.quarter == "2025Q2" for q in first) == 10  # the cohort not evaluable is no pool
    growth = {(c.sheet, t): g for c in cohorts for t, g in c.growth.items()}
    assert all(q.rose == (growth[(q.sheet_date, q.ticker)] > 1) for q in first)
    named = [q for q in first if q.ticker == "T30" and q.sheet_date == date(2025, 3, 3)]
    assert all(q.company == "Thirty Inc." for q in named)


# ── the runs ──


def question() -> Question:
    return Question("2025Q1", 1, SHEET, "AAPL", "Apple Inc.", True)


def one_sheet_plan(spec: AnalystReplaySpec, settings: ObliskConfig) -> Plan:
    segment, dates = plan_of(spec, settings).segments[0]
    return Plan(spec, ((segment, Dates(dates.cutoff, (SHEET,), (CONTROL,))),))


def replayer(
    settings: ObliskConfig,
    plan: Plan,
    client: httpx.AsyncClient,
    worker: StoreWorker,
    keys: dict[str, str | None] | None = None,
) -> Replayer:
    return Replayer(
        settings,
        plan,
        client=client,
        worker=worker,
        keys=KEYS if keys is None else keys,
        now=lambda: NOW,
    )


async def test_the_probe_is_refused_past_the_cap_and_asks_nothing(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig, replay_worker: StoreWorker
) -> None:
    settings = capped(shipped_settings, 0.0)
    plan = one_sheet_plan(spec, settings)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(COMPLETIONS)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ReplayRefusedError, match=r"passes llm.replay.max_usd 0.00"):
                await replayer(settings, plan, client, replay_worker).probe([question()])
            with pytest.raises(ReplayRefusedError, match="GEMINI_API_KEY unset"):
                await replayer(settings, plan, client, replay_worker, {}).probe([question()])
    assert not route.called


async def test_no_segment_is_replayed_before_the_probe_is_complete(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig, replay_worker: StoreWorker
) -> None:
    settings = capped(shipped_settings, 100.0)
    plan = one_sheet_plan(spec, settings)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(COMPLETIONS).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": '{"probability": 0.7}'}}
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                },
            )
        )
        async with httpx.AsyncClient() as client:
            replay = replayer(settings, plan, client, replay_worker)
            with pytest.raises(ReplayRefusedError, match="the probe has questions unanswered"):
                await replay.run([question()])
            asked = await replay.probe([question()])
            again = await replay.probe([question()])
    assert (asked.done, again.done, again.skipped) == (1, 0, 1)  # lite's one model, asked once
    assert route.call_count == 1
    stored = await replay_worker.run(replay_repo.probes)
    assert {a.probability for a in stored} == {0.7}
    assert await replay_worker.run(replay_repo.replay_spend) == pytest.approx(
        sum(a.cost_usd for a in stored)
    )


async def test_a_stopped_run_resumes_without_repeating_a_paid_call(
    spec: AnalystReplaySpec,
    shipped_settings: ObliskConfig,
    replay_store: Store,
    replay_worker: StoreWorker,
) -> None:
    settings = capped(shipped_settings, 100.0)
    plan = one_sheet_plan(spec, settings)
    prompt_hash = analyst_prompt_hash(settings, "lite", sections=spec.sheet.sections)
    # A run stopped part-way on the control sheet: its scan is stored, its unit is not.
    analyst_repo.record_call(replay_store, a_call(prompt_hash=prompt_hash))
    scan = {"choices": [{"message": {"role": "assistant", "content": '{"candidates": []}'}}]}
    with respx.mock(assert_all_called=False) as router:
        route = router.post(COMPLETIONS).mock(return_value=httpx.Response(200, json=scan))
        async with httpx.AsyncClient() as client:
            first = await replayer(settings, plan, client, replay_worker).run([])
            second = await replayer(settings, plan, client, replay_worker).run([])
    assert (first.interrupted, first.done, first.skipped) == (1, 1, 0)
    assert (second.interrupted, second.done, second.skipped) == (0, 0, 2)
    assert route.call_count == 1  # the after sheet's scan; the stopped one is never asked again
    units = {u.sheet_date: u for u in replay_repo.units(replay_store)}
    assert (units[CONTROL].state, units[CONTROL].side) == ("interrupted", "before")
    assert units[CONTROL].cost_usd == pytest.approx(0.25)
    assert (units[SHEET].state, units[SHEET].side) == ("ok", "after")
    calls = replay_repo.unit_calls(
        replay_store, segment="lite", prompt_hash=prompt_hash, sheet_date=SHEET
    )
    assert [(c.kind, c.record) for c in calls] == [(CallKind.SCAN, REPLAY)]
    assert prompt_hash != analyst_prompt_hash(settings, "lite")  # never the forward segment


async def test_a_run_whose_worst_case_passes_the_cap_is_refused(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig, replay_worker: StoreWorker
) -> None:
    settings = capped(shipped_settings, 0.001)
    plan = one_sheet_plan(spec, settings)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(COMPLETIONS)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ReplayRefusedError, match="the 2 sheets left"):
                await replayer(settings, plan, client, replay_worker).run([])
    assert not route.called
    assert await replay_worker.run(replay_repo.units) == []


# ── the record ──


def test_the_replay_s_calls_stay_out_of_the_forward_record(store: Store) -> None:
    analyst_repo.record_call(store, a_call(cost_usd=1.0))
    analyst_repo.record_call(store, a_call(cost_usd=2.0, record=FORWARD))
    as_of = NOW.date()
    assert sum(analyst_repo.label_spend(store, as_of=as_of).values()) == pytest.approx(2.0)
    assert sum(analyst_repo.label_spend(store, as_of=as_of, record=REPLAY).values()) == (
        pytest.approx(1.0)
    )
    assert replay_repo.replay_spend(store) == pytest.approx(1.0)


def test_the_report_and_its_ledger_lines_on_an_empty_replay(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig, replay_store: Store
) -> None:
    book = graded_book(shipped_settings, plan_of(spec, shipped_settings), replay_store)
    text = report_text(book, spec, as_of=NOW.date())
    assert "is inherited from its base model's card" in text
    assert "too few were graded" in text
    assert "claude-fable-5-2 (no_key)" in text
    lines = ledger_lines(book)
    assert lines[0].startswith("The replay after the cutoff (H15)")
    assert [line.split("**")[1] for line in lines[1:]] == ["lite", "mid"]
    assert ledger_lines("no replay run") == [lines[0], "- no replay run."]
    stated = spec.model_copy(
        update={"models": [m.model_copy(update={"cutoff_source": "stated"}) for m in spec.models]}
    )
    assert "inherited" not in report_text(book, stated, as_of=NOW.date())
    other_bins = spec.model_copy(
        update={"calibration": spec.calibration.model_copy(update={"reliability_bins": 4})}
    )
    with pytest.raises(ValueError, match="registers 4 reliability bins"):
        graded_book(shipped_settings, plan_of(other_bins, shipped_settings), replay_store)


# ── the load, end to end, against recorded shapes ──


class Stamp:
    def __init__(self, day: date) -> None:
        self._day = day

    def date(self) -> date:
        return self._day


class Frame:
    """A yfinance history frame's shape: rows by stamp."""

    def __init__(self, rows: list[tuple[date, dict[str, float]]]) -> None:
        self._rows = rows

    def iterrows(self) -> list[tuple[Stamp, dict[str, float]]]:
        return [(Stamp(day), row) for day, row in self._rows]


def yahoo(symbol: str, start: date, end: date) -> Frame:
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    sessions = [day for day in days if day.weekday() < 5]
    return Frame(
        [
            (day, {"Close": 100.0 + i, "Adj Close": 100.0 + i, "Dividends": 0.0, "Volume": 1e6})
            for i, day in enumerate(sessions)
        ]
    )


LOAD_INDEX = """CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
320193|Apple Inc.|10-Q|2024-11-01|edgar/data/320193/a.txt
789019|MICROSOFT CORP|10-K|2024-07-30|edgar/data/789019/b.txt
"""
TICKERS = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [320193, "Apple Inc.", "AAPL", "Nasdaq"],
        [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
        [999, "Not a filer", "NOPE", "NYSE"],
    ],
}


async def test_the_load_fills_the_replay_store_once_and_resumes(
    shipped_settings: ObliskConfig,
    replay_store: Store,
    replay_worker: StoreWorker,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    settings = capped(shipped_settings, 0.0)
    downloads: list[str] = []

    async def download(url: str, path: Path) -> None:
        downloads.append(url)
        assets = {"start": "2023-10-01", "end": "2024-09-28", "val": 1.0, "accn": "x"}
        facts = {
            "facts": {
                "us-gaap": {"GrossProfit": {"units": {"USD": [{**assets, "filed": "2024-11-01"}]}}}
            }
        }
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("CIK0000320193.json", json.dumps(facts))

    async def fetch(url: str, params: Any) -> bytes:
        del params
        assert "ecb.europa" in url
        return (
            repo_root / "tests" / "fixtures" / "research" / "ecb_eurofxref_hist.zip"
        ).read_bytes()

    async def no_sleep(_: float) -> None:
        return None

    span = Span(date(2024, 12, 2), (SHEET,), date(2025, 4, 30))
    with respx.mock(assert_all_called=False) as router:
        index = router.get(url__regex=r"full-index/.*/xbrl\.idx").mock(
            return_value=httpx.Response(200, text=LOAD_INDEX)
        )
        names_route = router.get(settings.sources.stocks.listings.url).mock(
            return_value=httpx.Response(200, json=TICKERS)
        )
        sic = router.get(url__regex=r"data\.sec\.gov/submissions/").mock(
            return_value=httpx.Response(200, json={"sic": "3571"})
        )
        async with httpx.AsyncClient() as client:

            def loader(agent: str | None) -> ReplaySources:
                return ReplaySources(
                    settings,
                    client=client,
                    policy=RetryPolicy.from_settings(settings.http),
                    worker=replay_worker,
                    user_agent=agent,
                    yahoo=yahoo,
                    research=Sources(fetch, yahoo, None),
                    download=download,
                    work_dir=tmp_path,
                    sleep=no_sleep,
                    now=lambda: NOW,
                )

            with pytest.raises(UpstreamError, match="SEC_USER_AGENT is unset"):
                await loader(None).load(span)
            first = await loader("research test@example.invalid").load(span)
            indexes, sics = index.call_count, sic.call_count
            second = await loader("research test@example.invalid").load(span)
    assert [step.step for step in first] == [
        "filings", "names", "sic", "facts", "bars", "instruments", "fx"
    ]  # fmt: skip
    assert indexes == 7  # 2023Q3 (15 months before the history) to 2025Q1
    assert (names_route.call_count, sics, downloads) == (
        1,
        2,
        [settings.llm.replay.companyfacts_zip_url],
    )
    assert universe_at(replay_store, SHEET, lookback_months=15) == {
        320193: "Apple Inc.",
        789019: "MICROSOFT CORP",
    }
    assert set(stocks_repo.symbol_ciks(replay_store, as_of=SHEET)) == {"AAPL", "MSFT"}
    held = {symbol for (symbol,) in replay_store.execute("SELECT DISTINCT symbol FROM stock_bars")}
    assert held == {"AAPL", "MSFT", "NOPE"}
    bar = replay_store.execute(
        "SELECT known_at FROM stock_bars WHERE symbol = 'AAPL' ORDER BY session LIMIT 1"
    ).fetchone()
    assert bar[0].startswith("2024-12-02T23:59:59")  # each close known at its own day's end
    # The second load reads nothing again but the rates, which are a research dataset's refresh.
    assert (index.call_count, sic.call_count, names_route.call_count) == (indexes, sics, 1)
    assert [s.rows for s in second[:5]] == [0, 0, 0, 0, 0]


# ── the registration's own checks ──


def _mutated(spec: AnalystReplaySpec, change: Any) -> dict[str, Any]:
    body = spec.model_dump(mode="json")
    change(body)
    return body


def _set(path: str, value: Any) -> Any:
    def change(body: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        node = body
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node[key]
        node[last] = value

    return change


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_set("dates.after.last", "2025-01-01"), "last sheet is not before its first"),
        (_set("dates.before.last", "2025-03-03"), "the leak control ends before"),
        (_set("dates.before.sheets", 85), "as many sheets"),
        (_set("sheet.sections", ["instrument", "macro"]), "no source dates its own publication"),
        (_set("sheet.omitted", ["instrument"]), "either on the sheet or omitted"),
        (_set("never", ["capital"] * 7), "reaches no capital"),
        (_set("calibration.min_effective", 1), "at least the record's minimum"),
    ],
)
def test_the_registration_refuses_an_incoherent_text(
    spec: AnalystReplaySpec, change: Any, message: str
) -> None:
    assert AnalystReplaySpec.model_validate(_mutated(spec, lambda _: None)) == spec
    with pytest.raises(ValueError, match=message):
        AnalystReplaySpec.model_validate(_mutated(spec, change))


def test_a_segment_is_registered_once_on_replayed_models(spec: AnalystReplaySpec) -> None:
    def twice(body: dict[str, Any]) -> None:
        body["segments"].append(body["segments"][0])

    def stranger(body: dict[str, Any]) -> None:
        body["segments"][0]["dossier_model"] = "claude_fable"

    with pytest.raises(ValueError, match="registered once"):
        AnalystReplaySpec.model_validate(_mutated(spec, twice))
    with pytest.raises(ValueError, match="not a replayed model"):
        AnalystReplaySpec.model_validate(_mutated(spec, stranger))


def test_a_configuration_unlike_the_registration_refuses_its_segment(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig
) -> None:
    lite = spec.segments[0]
    analyst = shipped_settings.llm.analyst

    def with_analyst(**changes: Any) -> ObliskConfig:
        return shipped_settings.model_copy(
            update={
                "llm": shipped_settings.llm.model_copy(
                    update={"analyst": analyst.model_copy(update=changes)}
                )
            }
        )

    segments = dict(analyst.segments)
    del segments["lite"]
    with pytest.raises(ReplayRefusedError, match="segment lite is not configured"):
        plan_of(spec, with_analyst(segments=segments))
    other = dict(analyst.segments)
    other["lite"] = other["lite"].model_copy(update={"dossier_model": "gemini_flash"})
    with pytest.raises(ReplayRefusedError, match="registered with gemini_flash_lite"):
        plan_of(spec, with_analyst(segments=other))
    with pytest.raises(ReplayRefusedError, match="temperature"):
        plan_of(spec, with_analyst(temperature=0.7))
    scan = analyst.scan.model_copy(update={"max_candidates": analyst.scan.max_candidates + 1})
    with pytest.raises(ReplayRefusedError, match="candidates are not the registered"):
        plan_of(spec, with_analyst(scan=scan))
    models = dict(shipped_settings.llm.models)
    del models["gemini_flash_lite"]
    unconfigured = shipped_settings.model_copy(
        update={"llm": shipped_settings.llm.model_copy(update={"models": models})}
    )
    assert model_status(spec, unconfigured, "gemini_flash_lite") == (
        "gemini_flash_lite is not a configured model"
    )
    moved = spec.model_copy(
        update={"dates": spec.dates.model_copy(update={"margin_days": spec.dates.margin_days + 7})}
    )
    with pytest.raises(ReplayRefusedError, match="is not the registered one"):
        sheet_dates(moved, lite)


# ── what a failure leaves ──


def test_a_history_without_a_close_before_the_horizon_s_end() -> None:
    early = history(date(2025, 3, 4), [10.0, 11.0])
    assert "no close within 7 days before 2025-04-01" in str(
        early.growth(date(2025, 3, 4), date(2025, 4, 1), max_age_days=7)
    )


def test_a_stale_name_counts_by_its_kind_and_a_closed_horizon_only(replay_store: Store) -> None:
    from obliskiq.research.replay.prices import Sessions, every_cohort

    replay_repo.save_filings(
        replay_store,
        [
            Filing(cik, "10-K", date(2024, 12, 2), f"C{cik}", end_of(date(2024, 12, 2)))
            for cik in (1, 2)
        ],
    )
    replay_repo.save_names(
        replay_store, [Name(1, "AAPL", "Nasdaq", NOW), Name(2, "MSFT", "Nasdaq", NOW)]
    )
    histories = {
        "AAPL": history(date(2025, 3, 4), [10.0] * 40),
        "MSFT": history(date(2024, 1, 2), [10.0] * 5),  # stopped trading long before
    }
    sessions = Sessions("XNYS", date(2025, 2, 1), date(2025, 6, 30))
    found = every_cohort(
        replay_store,
        histories,
        sheets=(SHEET, date(2025, 6, 23)),
        sessions=sessions,
        horizon_sessions=20,
        lookback_months=15,
        max_age_days=7,
        limit=Decimal(1),
        until=date(2025, 6, 30),
    )
    assert [c.sheet for c in found] == [SHEET]  # the June sheet's horizon is not closed
    (only,) = found
    assert only.reasons == {"no close within 7 days after …": 1}
    assert (only.not_evaluable, only.not_evaluable_share) == (1, 0.5)
    assert Cohort(SHEET, SHEET, SHEET, 0, {}).not_evaluable_share == 1.0
    assert sessions.horizon(date(2025, 6, 20), 20) is None


def _reply(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5},
        },
    )


async def test_a_probe_answer_is_asked_once_more_then_stored_as_it_came(
    spec: AnalystReplaySpec, shipped_settings: ObliskConfig, replay_worker: StoreWorker
) -> None:
    from obliskiq.research.replay.probe import Probe

    settings = capped(shipped_settings, 10.0)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(COMPLETIONS).mock(
            side_effect=[
                _reply("a probability of 0.3"),
                _reply('{"probability": 0.3}'),
                _reply("no"),
                _reply('{"probability": 7}'),
                httpx.ConnectError("refused"),
            ]
        )
        async with httpx.AsyncClient() as client:
            probe = Probe(
                settings, spec, client=client, worker=replay_worker, keys=KEYS, now=lambda: NOW
            )
            second = await probe.ask("gemini_flash_lite", question())
            invalid = await probe.ask(
                "gemini_flash_lite", Question("2025Q1", 2, SHEET, "A", "A", False)
            )
            failed = await probe.ask(
                "gemini_flash_lite", Question("2025Q1", 3, SHEET, "B", "B", True)
            )
            keyless = Probe(
                settings, spec, client=client, worker=replay_worker, keys={}, now=lambda: NOW
            )
            with pytest.raises(ValueError, match="GEMINI_API_KEY unset"):
                await keyless.ask("gemini_flash_lite", question())
    assert route.call_count == 5
    assert (second.status, second.probability, second.input_tokens) == (
        ExtractionStatus.OK,
        0.3,
        100,
    )
    assert (invalid.status, invalid.probability) == (ExtractionStatus.INVALID, None)
    assert failed.status is ExtractionStatus.FAILED
    assert "ConnectError" in (failed.error or "")
    assert len(await replay_worker.run(replay_repo.probes)) == 3


async def test_a_sheet_the_analyst_s_budget_stops_is_recorded_interrupted(
    spec: AnalystReplaySpec,
    shipped_settings: ObliskConfig,
    replay_worker: StoreWorker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obliskiq.research.replay import run as run_module

    # The replay's own check passes (a worst case of nothing); the analyst's does not.
    monkeypatch.setattr(run_module, "unit_worst", lambda *_: 0.0)
    settings = capped(shipped_settings, 1e-9)
    plan = one_sheet_plan(spec, settings)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(COMPLETIONS)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ReplayRefusedError):
                await replayer(settings, plan, client, replay_worker).run([])
    assert not route.called
    (unit,) = await replay_worker.run(replay_repo.units)
    assert (unit.sheet_date, unit.state) == (CONTROL, "interrupted")


async def test_a_replayed_draft_is_recorded_and_never_handed_to_the_book(
    spec: AnalystReplaySpec,
    shipped_settings: ObliskConfig,
    replay_store: Store,
    replay_worker: StoreWorker,
) -> None:
    settings = capped(shipped_settings, 100.0)
    plan = one_sheet_plan(spec, settings)
    sheet = build_fact_sheet(replay_store, settings, as_of=CONTROL, sections=spec.sheet.sections)
    symbol = sheet.instruments[0]
    scan = json.dumps(
        {
            "candidates": [
                {"section": "instrument", "symbol": symbol, "reason": "x", "facts": ["F2"]}
            ]
        }
    )
    with respx.mock(assert_all_called=False) as router:
        router.post(COMPLETIONS).mock(
            side_effect=[_reply(scan), _reply('{"drafts": [{"symbol": "nothing else"}]}')] * 2
        )
        async with httpx.AsyncClient() as client:
            report = await replayer(settings, plan, client, replay_worker).run([])
    assert report.done == 2
    drafts = replay_repo.replay_drafts(replay_store)
    assert [d.outcome for d, _ in drafts] == [DraftOutcome.DROPPED, DraftOutcome.DROPPED]
    assert all(d.thesis_id is None for d, _ in drafts)


async def test_a_failing_company_or_name_never_stops_the_load_but_a_throttle_does(
    shipped_settings: ObliskConfig,
    replay_store: Store,
    replay_worker: StoreWorker,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    settings = capped(shipped_settings, 0.0)
    fixture = repo_root / "tests" / "fixtures" / "research" / "ecb_eurofxref_hist.zip"

    async def download(url: str, path: Path) -> None:
        del url
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("CIK0000000001.json", "{}")

    async def fetch(url: str, params: Any) -> bytes:
        del url, params
        return fixture.read_bytes()

    async def no_sleep(_: float) -> None:
        return None

    def silent(symbol: str, start: date, end: date) -> Frame:
        del symbol, start, end
        return Frame([])

    def flaky(symbol: str, start: date, end: date) -> Frame:
        if symbol == "MSFT" or "." in symbol:
            raise TimeoutError(f"{symbol} timed out")
        return yahoo(symbol, start, end)

    answers = iter([httpx.Response(503)])

    def index(request: httpx.Request) -> httpx.Response:
        del request
        return next(answers, httpx.Response(200, text=LOAD_INDEX.replace("\n320193", "\n\n320193")))

    def submissions(request: httpx.Request) -> httpx.Response:
        body = {} if "789019" in str(request.url) else {"sic": "3571"}
        return httpx.Response(200, json=body)

    span = Span(date(2024, 12, 2), (SHEET,), date(2025, 4, 30))
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"full-index/.*/xbrl\.idx").mock(side_effect=index)
        router.get(settings.sources.stocks.listings.url).mock(
            return_value=httpx.Response(200, json=TICKERS)
        )
        sic = router.get(url__regex=r"data\.sec\.gov/submissions/").mock(side_effect=submissions)
        async with httpx.AsyncClient() as client:

            def loader(history: Any) -> ReplaySources:
                return ReplaySources(
                    settings,
                    client=client,
                    policy=dataclasses.replace(RetryPolicy.from_settings(settings.http), retries=0),
                    worker=replay_worker,
                    user_agent="research test@example.invalid",
                    yahoo=history,
                    research=Sources(fetch, history, None),
                    download=download,
                    work_dir=tmp_path,
                    sleep=no_sleep,
                    now=lambda: NOW,
                )

            with pytest.raises(UpstreamError, match="answered HTTP 503"):
                await loader(yahoo).load(span)
            with pytest.raises(DataSourceError, match="throttled: 3 of 3 names answered nothing"):
                await loader(silent).load(span)
            done = await loader(flaky).load(span)
            sics = sic.call_count
            await loader(flaky).load(span)
    steps = {step.step: step for step in done}
    assert steps["sic"].detail == "1 companies failed (read again on the next load)"
    assert sic.call_count == sics + 1  # the failed company only, asked again
    assert steps["facts"].detail == "held"  # read whole by the throttled load before
    held = {symbol for (symbol,) in replay_store.execute("SELECT DISTINCT symbol FROM stock_bars")}
    assert held == {"AAPL", "NOPE"}
    failed = replay_store.execute(
        "SELECT detail FROM source_targets WHERE source = 'replay' AND target = 'bars:MSFT'"
    ).fetchall()
    assert any("TimeoutError: MSFT timed out" in (row[0] or "") for row in failed)


async def test_a_retried_read_backs_off_and_a_cancel_is_never_swallowed(
    shipped_settings: ObliskConfig, replay_worker: StoreWorker, tmp_path: Path
) -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async with httpx.AsyncClient() as client:
        sources = ReplaySources(
            shipped_settings,
            client=client,
            policy=dataclasses.replace(
                RetryPolicy.from_settings(shipped_settings.http), retries=1, jitter_seconds=0.0
            ),
            worker=replay_worker,
            user_agent=None,
            yahoo=yahoo,
            research=Sources(None, yahoo, None),  # type: ignore[arg-type]
            download=None,  # type: ignore[arg-type]
            work_dir=tmp_path,
            sleep=sleep,
            now=lambda: NOW,
        )
        tries = iter([TimeoutError("once"), "ok"])

        async def once() -> str:
            found = next(tries)
            if isinstance(found, Exception):
                raise found
            return found

        assert await sources._retry(once) == "ok"
        assert len(waits) == 1

        async def always() -> str:
            raise TimeoutError("always")

        with pytest.raises(TimeoutError, match="always"):
            await sources._retry(always)

        async def cancelled() -> str:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await sources._retry(cancelled)
    assert len(waits) == 2


def test_the_sec_s_files_of_another_shape_are_refused() -> None:
    with pytest.raises(UpstreamError, match="not \\{fields, data\\}"):
        parse_names([], read_at=NOW)
    with pytest.raises(DataSourceError, match="are not"):
        parse_companyfacts([], cik=1, inputs={})


def test_a_replayed_review_is_never_read_as_a_forward_one(store: Store) -> None:
    from thesis_rig import thesis

    from obliskiq.store import theses as thesis_repo

    held = thesis_repo.insert_thesis(store, thesis(), actor="test")
    analyst_repo.record_call(
        store, a_call(kind=CallKind.REVIEW, thesis_id=held.thesis_id, as_of=NOW.date())
    )
    assert analyst_repo.reviews(store, as_of=NOW.date()) == []


def test_the_replay_s_registration_is_its_own_kind(repo_root: Path) -> None:
    from obliskiq.research.prereg import FORWARD_KINDS, load_any
    from obliskiq.scoreboard.rules import RULES_PATH

    assert isinstance(load_any(repo_root / REPLAY_PATH), FORWARD_KINDS)  # no history run
    with pytest.raises(PreregistrationError, match="is not the analyst replay's"):
        load_locked_replay(repo_root / RULES_PATH)


def test_a_cutoff_is_documented_with_its_source(shipped_settings: ObliskConfig) -> None:
    model = shipped_settings.llm.models["gemini_flash_lite"]
    with pytest.raises(ValueError, match="given together"):
        type(model).model_validate({**model.model_dump(), "cutoff_source": None})


def test_a_sheet_of_an_unknown_section_is_refused(
    replay_store: Store, shipped_settings: ObliskConfig
) -> None:
    with pytest.raises(ValueError, match="has no section nowhere"):
        build_fact_sheet(replay_store, shipped_settings, as_of=SHEET, sections=("nowhere",))
