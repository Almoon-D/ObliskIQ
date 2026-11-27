"""``obliskiq research replay``: the analyst replayed after its models' cutoff (H15, D191, D199).

``load`` fills the replay's own store (``llm.replay.store_file``) from the SEC and Yahoo, each
row stamped with the day its source published it, then computes every sheet date's universe;
``probe`` asks each replayed model the memory probe's questions; ``run`` replays every
registered segment on every registered sheet, after the probe, resumably and inside
``llm.replay.max_usd``; ``report`` grades the calls and writes
``research/reports/analyst-replay.md``. Every command reads the locked pre-registration and
refuses what it does not allow. None reads the forward store or opens a broker session; ``load``
needs ``SEC_USER_AGENT`` and the network, ``probe`` and ``run`` the providers' keys.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections.abc import Callable, Coroutine
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

import httpx

from ..config.schema import ObliskConfig
from ..env import repository_root, secret_reader
from ..errors import ObliskError
from ..research.data import Sources, http_fetch, yfinance_history
from ..research.prereg_replay import AnalystReplaySpec
from ..research.registry import Git
from ..research.replay.grade import Grader, ReplayBook, grade_replay
from ..research.replay.prices import Sessions, every_cohort, stock_histories, stored
from ..research.replay.probe import Question, draw
from ..research.replay.report import ledger_lines, report_text
from ..research.replay.run import Plan, Replayer, RunReport, plan_of
from ..research.replay.sources import ReplaySources, Span, http_download, span_of, universe_at
from ..scoreboard.rules import REPLAY_PATH, load_locked_replay
from ..store import open_replay_store
from ..store import replay as replay_repo
from ..store.db import Store
from ..store.replay import names
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy
from ..util.timeutil import utc_now

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_REFUSED = 1
REPORT_NAME = "analyst-replay.md"


def _prereg_path() -> Path:
    """The locked pre-registration in the checkout this runs from (relative in an installed
    wheel, read from the working directory)."""
    root = repository_root()
    return REPLAY_PATH if root is None else root / REPLAY_PATH


def _context(loaded: Loaded) -> tuple[ObliskConfig, AnalystReplaySpec, Plan, Path]:
    settings = loaded.config.settings
    prereg = load_locked_replay(loaded.args.prereg)
    return settings, prereg.spec, plan_of(prereg.spec, settings), loaded.env.require_data_dir()


def _span(settings: ObliskConfig, plan: Plan) -> Span:
    return span_of(settings, [dates for _, dates in plan.segments], today=utc_now().date())


def _sessions(settings: ObliskConfig, span: Span) -> Sessions:
    return Sessions(settings.llm.replay.session_venue, span.sheets[0], span.until)


def _cohorts_in_memory(
    store: Store, settings: ObliskConfig, spec: AnalystReplaySpec, span: Span
) -> tuple[list[Any], dict[date, dict[str, str]]]:
    """Every closed cohort with its names' returns, and each sheet's companies by ticker."""
    histories = stock_histories(store)
    found = every_cohort(
        store,
        histories,
        sheets=span.sheets,
        sessions=_sessions(settings, span),
        horizon_sessions=spec.outcome.horizon_sessions,
        lookback_months=settings.llm.replay.filer_lookback_months,
        max_age_days=spec.outcome.max_mark_age_days,
        limit=spec.outcome.not_evaluable_cohort_share,
        until=span.until,
    )
    tickers = names(store)
    companies: dict[date, dict[str, str]] = {}
    for cohort in found:
        universe = universe_at(
            store, cohort.sheet, lookback_months=settings.llm.replay.filer_lookback_months
        )
        companies[cohort.sheet] = {
            ticker: universe[cik]
            for cik, name in tickers.items()
            if (ticker := name.ticker) is not None and cik in universe
        }
    return found, companies


def _questions(
    store: Store, settings: ObliskConfig, spec: AnalystReplaySpec, span: Span
) -> list[Question]:
    found, companies = _cohorts_in_memory(store, settings, spec, span)
    return draw(spec, found, companies)


def _go[T](work: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(work())


def _keys(loaded: Loaded, settings: ObliskConfig) -> dict[str, str | None]:
    read = secret_reader(loaded.env)
    return {name: read(provider.key_env) for name, provider in settings.llm.providers.items()}


def _load(loaded: Loaded, out: TextIO) -> int:
    settings, spec, plan, data_dir = _context(loaded)
    span = _span(settings, plan)
    policy = RetryPolicy.from_settings(settings.http)
    agent = loaded.env.sec_user_agent
    user_agent = None if agent is None else agent.get_secret_value()

    async def work() -> list[str]:
        worker = StoreWorker(lambda: open_replay_store(settings, data_dir))
        try:
            timeout = httpx.Timeout(settings.research.data.timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                sources = ReplaySources(
                    settings,
                    client=client,
                    policy=policy,
                    worker=worker,
                    user_agent=user_agent,
                    yahoo=yfinance_history,
                    research=Sources(http_fetch(client, policy), yfinance_history, None),
                    download=http_download(client, user_agent=user_agent),
                    work_dir=data_dir,
                    sleep=asyncio.sleep,
                    now=utc_now,
                )
                done = await sources.load(span)
        finally:
            await worker.aclose()
        return [f"{step.step}: {step.rows} rows; {step.detail}" for step in done]

    for line in _go(work):
        print(line, file=out)
    with open_replay_store(settings, data_dir) as store:
        found, _ = _cohorts_in_memory(store, settings, spec, span)
        saved = replay_repo.record_cohorts(store, [stored(c) for c in found], at=utc_now())
        whole = sum(1 for c in found if c.benchmark is None)
    print(
        f"cohorts: {len(found)} sheet dates closed, {saved} stored now, {whole} not evaluable "
        "whole",
        file=out,
    )
    return EXIT_OK


def _replayer_run(
    loaded: Loaded, action: Callable[[Replayer, list[Question]], Coroutine[Any, Any, RunReport]]
) -> RunReport:
    settings, spec, plan, data_dir = _context(loaded)
    span = _span(settings, plan)
    with open_replay_store(settings, data_dir) as store:
        questions = _questions(store, settings, spec, span)
    keys = _keys(loaded, settings)

    async def work() -> RunReport:
        worker = StoreWorker(lambda: open_replay_store(settings, data_dir))
        try:
            async with httpx.AsyncClient() as client:
                replayer = Replayer(
                    settings, plan, client=client, worker=worker, keys=keys, now=utc_now
                )
                return await action(replayer, questions)
        finally:
            await worker.aclose()

    return _go(work)


def _summary(report: RunReport, out: TextIO) -> None:
    for line in report.lines:
        print(line, file=out)
    print(
        f"done {report.done}, already held {report.skipped}, interrupted {report.interrupted}; "
        f"cost {report.cost_usd:.4f} USD",
        file=out,
    )


def _probe(loaded: Loaded, out: TextIO) -> int:
    try:
        report = _replayer_run(loaded, lambda r, q: r.probe(q))
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_REFUSED
    _summary(report, out)
    return EXIT_OK


def _run(loaded: Loaded, out: TextIO) -> int:
    try:
        report = _replayer_run(loaded, lambda r, q: r.run(q))
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_REFUSED
    _summary(report, out)
    return EXIT_OK


def graded_book(settings: ObliskConfig, plan: Plan, store: Store) -> ReplayBook:
    """The replay graded on its store, as of today's prices."""
    span = _span(settings, plan)
    grader = Grader(
        store,
        settings,
        plan.spec,
        histories=stock_histories(store),
        cohorts=replay_repo.cohorts(store),
        sessions=_sessions(settings, span),
    )
    return grade_replay(store, settings, plan, grader=grader, prices_end=span.until)


def _report(loaded: Loaded, out: TextIO) -> int:
    try:
        settings, spec, plan, data_dir = _context(loaded)
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_REFUSED
    with open_replay_store(settings, data_dir) as store:
        book = graded_book(settings, plan, store)
    text = report_text(book, spec, as_of=utc_now().date())
    path = Git(loaded.args.prereg).root / settings.research.reports_dir / REPORT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(text, file=out)
    print(f"written to {path}", file=out)
    return EXIT_OK


def replay_ledger(loaded: Loaded) -> list[str]:
    """The replay's lines beside the forward ledger; a plain line when it has not run."""
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    if not (data_dir / settings.llm.replay.store_file).exists():
        return ledger_lines("no replay run (obliskiq research replay)")
    try:
        prereg = load_locked_replay(_prereg_path())
        plan = plan_of(prereg.spec, settings)
    except ObliskError as exc:
        return ledger_lines(f"not graded: {exc}")
    with open_replay_store(settings, data_dir) as store:
        if not replay_repo.units(store):
            return ledger_lines("no replayed sheet yet (obliskiq research replay run)")
        return ledger_lines(graded_book(settings, plan, store))


def add_replay_commands(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    replay = actions.add_parser(
        "replay", help="the analyst replayed after its models' training cutoff (H15)"
    )
    steps = replay.add_subparsers(dest="replay_action", required=True, metavar="STEP")
    for name, help_text, handler in (
        ("load", "fill the replay's store from the SEC and Yahoo (network)", _load),
        ("probe", "ask each replayed model the memory probe (paid calls)", _probe),
        ("run", "replay every segment on every sheet, after the probe (paid calls)", _run),
        ("report", "grade the replayed calls and write the report", _report),
    ):
        parser = steps.add_parser(name, help=help_text)
        parser.add_argument(
            "--prereg", type=Path, default=_prereg_path(), help="the locked pre-registration"
        )
        parser.set_defaults(handler=handler)
