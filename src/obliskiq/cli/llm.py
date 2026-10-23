"""``obliskiq llm``: the EventExtractor by hand.

``status`` shows the model, the prompt's hash (H7's segment), the items waiting and the month's
spend against the budget; ``extract`` runs the extractor once over what is queued (skipped,
and said so, without the provider's key). Neither opens a broker session.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TextIO

import httpx

from ..env import secret_reader
from ..llm.extractor import EventExtractor, ExtractorRun, month_start
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


def _llm_status(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    llm = settings.llm
    extractor, waiting = _with_extractor(loaded, _pending)
    now = utc_now()
    with open_store(settings, loaded.env.require_data_dir()) as store:
        spent = source_repo.extraction_spend(store, month_start(now))
        made = source_repo.extractions(store, as_of=now.date())
    model = llm.extractor_model()
    print(
        f"model {model.model_id} ({model.provider}); prompt hash {extractor.prompt_hash}", file=out
    )
    print(f"items waiting: {waiting} (at most {llm.extractor.max_items_per_run} a run)", file=out)
    print(f"spent this month: {spent:.4f} of {llm.budget.monthly_usd:.2f} USD", file=out)
    counts = Counter(e.status.value for e in made if e.prompt_hash == extractor.prompt_hash)
    print(
        "extractions of this prompt: "
        + (", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "none"),
        file=out,
    )
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


def add_llm_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    llm = commands.add_parser("llm", help="the EventExtractor")
    actions = llm.add_subparsers(dest="action", required=True, metavar="ACTION")
    actions.add_parser("status", help="its model, prompt hash, queue and spend").set_defaults(
        handler=_llm_status
    )
    actions.add_parser("extract", help="run it once over the queued items").set_defaults(
        handler=_llm_extract
    )
