"""``obliskiq llm``: the EventExtractor and the Analyst by hand.

``status`` shows the extractor's model and prompt hash, the items waiting, each analyst segment's
models, prompt hash, calls, drafts this week and spend against its share, and the month's spend
against the one budget; ``extract`` runs the extractor once over what is queued; ``facts`` prints
the analyst's fact sheet as of a session (``--as-of``, today by default) and calls nothing;
``analyst`` runs every segment once on that sheet, as the service's daily run does: second
opinions (the trading segment), the scan, the dossiers handed to the thesis book's intake, and the
week's reviews. Without a provider's key the extractor or a segment is skipped, and says so. None
opens a broker session.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING, TextIO

import httpx

from ..data.events.filing_text import company_texts
from ..env import secret_reader
from ..grid.facts import build_fact_sheet
from ..llm.analyst import Analyst
from ..llm.analyst_prompts import analyst_prompt_hash
from ..llm.extractor import EventExtractor, ExtractorRun, month_start
from ..orchestration.analyst import AnalystJob, AnalystOutcome, describe_segment, judge_analyst
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
    monday = today - timedelta(days=today.weekday())
    order = llm.segment_order()
    with open_store(settings, loaded.env.require_data_dir()) as store:
        spent = analyst_repo.llm_spend(store, month_start(now))
        made = source_repo.extractions(store, as_of=today)
        calls = analyst_repo.calls(store, as_of=today)
        handed = {
            name: analyst_repo.drafts_handed(store, first=monday, last=today, segment=name)
            for name in order
        }
        segment_spent = {
            name: analyst_repo.segment_spend(store, name, month_start(now)) for name in order
        }
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
    for name in order:
        segment = llm.analyst.segments[name]
        prompt_hash = analyst_prompt_hash(settings, name)
        trading = " (trading)" if name == llm.analyst.trading_segment else " (shadow)"
        scan, dossier = llm.models[segment.scan_model], llm.models[segment.dossier_model]
        print(
            f"analyst segment {name}{trading}: scan {scan.model_id} ({scan.provider}), dossier "
            f"{dossier.model_id} ({dossier.provider}); prompt hash {prompt_hash}",
            file=out,
        )
        print(
            f"  prices read: scan {scan.prices_read.isoformat()}, dossier "
            f"{dossier.prices_read.isoformat()}",
            file=out,
        )
        mine = Counter(
            f"{c.kind.value} {c.status.value}"
            for c in calls
            if c.segment == name and c.prompt_hash == prompt_hash
        )
        print(
            "  calls of this prompt: "
            + (", ".join(f"{n} {what}" for what, n in sorted(mine.items())) or "none"),
            file=out,
        )
        share = float(segment.budget_share) * llm.budget.monthly_usd
        print(
            f"  drafts handed to the intake this week: {handed[name]} of "
            f"{llm.analyst.max_drafts_per_week}; spent this month {segment_spent[name]:.4f} of "
            f"its share {share:.2f} USD",
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
    read = secret_reader(loaded.env)
    keys = {name: read(provider.key_env) for name, provider in settings.llm.providers.items()}

    async def go() -> AnalystOutcome:
        worker = StoreWorker(lambda: open_store(settings, data_dir))
        try:
            async with httpx.AsyncClient() as client:
                texts = company_texts(
                    settings,
                    client=client,
                    worker=worker,
                    secrets=read,
                    now=utc_now,
                    sleep=asyncio.sleep,
                )
                analysts = [
                    Analyst(
                        settings,
                        segment=name,
                        client=client,
                        worker=worker,
                        keys=keys,
                        now=utc_now,
                        texts=texts,
                    )
                    for name in settings.llm.segment_order()
                ]
                job = AnalystJob(settings, analysts=analysts, worker=worker, now=utc_now)
                return await job.run(as_of)
        finally:
            await worker.aclose()

    outcome = asyncio.run(go())
    verdict = judge_analyst(outcome)
    print(f"analyst: {verdict.status}", file=out)
    print(
        f"  fact lines {outcome.lines}; cost {outcome.cost_usd:.4f} USD; theses "
        + (", ".join(f"T{n}" for n in outcome.accepted) or "none"),
        file=out,
    )
    for segment in outcome.segments:
        print(f"  {describe_segment(segment)}", file=out)
        for proposal in segment.run.proposals:
            if proposal.dropped is not None:
                print(
                    f"    draft {proposal.call_id}.{proposal.seq} dropped: {proposal.dropped}",
                    file=out,
                )
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
        "analyst", help="run every analyst segment once: opinions, scan, dossiers, reviews"
    )
    for parser, handler in ((facts, _llm_facts), (analyst, _llm_analyst)):
        parser.add_argument(
            "--as-of", type=date.fromisoformat, default=None, help="the session (YYYY-MM-DD)"
        )
        parser.set_defaults(handler=handler)
