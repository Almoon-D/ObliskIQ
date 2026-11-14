"""``obliskiq llm``: the EventExtractor and the Analyst by hand.

``status`` shows each role's model and prompt hash (its segment), the items waiting, the analyst's
calls and drafts this week, and the month's spend against the one budget; ``extract`` runs the
extractor once over what is queued; ``facts`` prints the analyst's fact sheet as of a session
(``--as-of``, today by default) and calls nothing; ``analyst`` runs the analyst once on that sheet,
writing its second opinions and handing its drafts to the thesis book's intake, as the service's
daily run does. Without the provider's key the extractor and the analyst are skipped, and say so.
None opens a broker session.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING, TextIO

import httpx

from ..env import secret_reader
from ..grid.facts import build_fact_sheet
from ..llm.analyst import Analyst
from ..llm.analyst_prompts import analyst_prompt_hash
from ..llm.extractor import EventExtractor, ExtractorRun, month_start
from ..orchestration.analyst import AnalystJob, AnalystOutcome, judge_analyst
from ..orchestration.runlog import OK
from ..store import analyst as analyst_repo
from ..store import open_store
from ..store import sources as source_repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_DEGRADED = 2


def _with_extractor[T](
    loaded: Loaded, work: Callable[[EventExtractor], Awaitable[T]]
) -> tuple[EventExtractor, T]:
    """``work`` over the extractor, its key read by the name config gives it."""
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    key_env = settings.llm.extractor_provider().key_env

    async def go() -> tuple[EventExtractor, T]:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            async with httpx.AsyncClient() as client:
                extractor = EventExtractor(
                    settings,
                    client=client,
                    worker=worker,
                    key=secret_reader(loaded.env)(key_env),
                    now=utc_now,
                )
                return extractor, await work(extractor)
        finally:
            await worker.aclose()

    return asyncio.run(go())


async def _pending(extractor: EventExtractor) -> int:
    return len(await extractor.pending())


async def _extract(extractor: EventExtractor) -> ExtractorRun:
    return await extractor.run()


def _as_of(loaded: Loaded) -> date:
    given: date | None = getattr(loaded.args, "as_of", None)
    return given if given is not None else utc_now().date()


def _llm_status(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    llm = settings.llm
    extractor, waiting = _with_extractor(loaded, _pending)
    now = utc_now()
    today = now.date()
    analyst_hash = analyst_prompt_hash(settings)
    monday = today - timedelta(days=today.weekday())
    with open_store(settings, loaded.env.require_data_dir()) as store:
        spent = analyst_repo.llm_spend(store, month_start(now))
        made = source_repo.extractions(store, as_of=today)
        calls = analyst_repo.calls(store, as_of=today)
        handed = analyst_repo.drafts_handed(store, first=monday, last=today)
    model = llm.extractor_model()
    print(
        f"extractor: model {model.model_id} ({model.provider}); prompt hash "
        f"{extractor.prompt_hash}",
        file=out,
    )
    print(f"items waiting: {waiting} (at most {llm.extractor.max_items_per_run} a run)", file=out)
    counts = Counter(e.status.value for e in made if e.prompt_hash == extractor.prompt_hash)
    print(
        "extractions of this prompt: "
        + (", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "none"),
        file=out,
    )
    analyst = llm.analyst_model()
    print(
        f"analyst: model {analyst.model_id} ({analyst.provider}); prompt hash {analyst_hash}",
        file=out,
    )
    mine = Counter(
        f"{c.kind.value} {c.status.value}" for c in calls if c.prompt_hash == analyst_hash
    )
    print(
        "analyst calls of this prompt: "
        + (", ".join(f"{n} {what}" for what, n in sorted(mine.items())) or "none"),
        file=out,
    )
    print(
        f"drafts handed to the intake this week: {handed} of {llm.analyst.max_drafts_per_week}",
        file=out,
    )
    print(f"spent this month: {spent:.4f} of {llm.budget.monthly_usd:.2f} USD", file=out)
    return EXIT_OK


def _llm_extract(loaded: Loaded, out: TextIO) -> int:
    _, result = _with_extractor(loaded, _extract)
    print(f"extract: {result.state}: {result.reason}", file=out)
    print(
        f"  ok {result.ok}, invalid {result.invalid}, failed {result.failed}; "
        f"cost {result.cost_usd:.4f} USD",
        file=out,
    )
    return EXIT_OK if result.state == "ok" and not result.failed else EXIT_DEGRADED


def _llm_facts(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    as_of = _as_of(loaded)
    with open_store(settings, loaded.env.require_data_dir()) as store:
        sheet = build_fact_sheet(store, settings, as_of=as_of)
    print(f"fact sheet as of {as_of.isoformat()} ({sheet.sheet_hash})", file=out)
    print(sheet.text(), file=out)
    return EXIT_OK


def _llm_analyst(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    as_of = _as_of(loaded)
    key = secret_reader(loaded.env)(settings.llm.analyst_provider().key_env)

    async def go() -> AnalystOutcome:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            async with httpx.AsyncClient() as client:
                analyst = Analyst(settings, client=client, worker=worker, key=key, now=utc_now)
                job = AnalystJob(settings, analyst=analyst, worker=worker, now=utc_now)
                return await job.run(as_of)
        finally:
            await worker.aclose()

    outcome = asyncio.run(go())
    verdict = judge_analyst(outcome)
    print(f"analyst: {verdict.status}: {verdict.reason}", file=out)
    print(
        f"  fact lines {outcome.lines}; cost {outcome.run.cost_usd:.4f} USD; theses "
        + (", ".join(f"T{n}" for n in outcome.accepted) or "none"),
        file=out,
    )
    for proposal in outcome.run.proposals:
        if proposal.dropped is not None:
            print(f"  draft {proposal.seq} dropped: {proposal.dropped}", file=out)
    return EXIT_OK if verdict.status == OK else EXIT_DEGRADED


def add_llm_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    llm = commands.add_parser("llm", help="the EventExtractor and the Analyst")
    actions = llm.add_subparsers(dest="action", required=True, metavar="ACTION")
    actions.add_parser(
        "status", help="each role's model, prompt hash, queue and spend"
    ).set_defaults(handler=_llm_status)
    actions.add_parser("extract", help="run the extractor once over the queued items").set_defaults(
        handler=_llm_extract
    )
    facts = actions.add_parser("facts", help="print the analyst's fact sheet (calls nothing)")
    analyst = actions.add_parser(
        "analyst", help="run the analyst once: second opinions, and drafts to the intake"
    )
    for parser, handler in ((facts, _llm_facts), (analyst, _llm_analyst)):
        parser.add_argument(
            "--as-of", type=date.fromisoformat, default=None, help="the session (YYYY-MM-DD)"
        )
        parser.set_defaults(handler=handler)
