"""Session 28's acceptance: one scheduled run of the analyst over a fixture store yields validated
drafts that become shadow theses; its second opinion reaches the digest; with the LLM unreachable
the trading path runs as before; and the daily compute step runs as a job of its own.

The model is a recorded-shape stand-in (``Model``): it answers what the prompt it is sent allows,
never anything a live model said.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from service_rig import CURRENCY, ServiceRig
from thesis_rig import draft

from obliskiq.config import ObliskConfig
from obliskiq.decision.thesis_intake import ThesisIntake
from obliskiq.domain.analyst import DraftOutcome
from obliskiq.domain.enums import AssetClass, InstrumentRole
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.market import Bar
from obliskiq.domain.orders import ExecutionMode
from obliskiq.domain.thesis import Thesis, ThesisSource, ThesisState
from obliskiq.llm.analyst_prompts import OPINION_SYSTEM
from obliskiq.orchestration.assemble import build_compute
from obliskiq.orchestration.schedule import AnalysisPlan
from obliskiq.store import analyst as analyst_repo
from obliskiq.store import repos
from obliskiq.store import runs as run_repo
from obliskiq.store import theses as thesis_repo

D = Decimal
SPPW, FCX = 355720826, 7089
CURRENCY.update({SPPW: "EUR", FCX: "USD"})
#: Monday 26 October 2026: New York closes at 20:00 UTC; the compute step runs at 21:00 and the
#: analyst at 21:30 (config/defaults/runtime.yaml).
MONDAY = datetime(2026, 10, 26, tzinfo=UTC).date()
TUESDAY_EUROPE = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
GEMINI = "generativelanguage.googleapis.com"
KEY = "test-key-not-a-secret"


class Model:
    """The provider as the service sees it; every other host is down."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != GEMINI:
            return httpx.Response(503, text="down")
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        body = json.loads(request.content)
        self.asked.append(body)
        if self.down:
            raise httpx.ConnectError("unreachable")
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if system == OPINION_SYSTEM:
            answer: dict[str, Any] = {
                "critique": "copper demand is not on the sheet; the idea rests on belief",
                "probability": 0.3,
                "facts": [],
            }
        else:
            found = re.search(r"(F\d+) \| [0-9-]+ \| ok \| instrument \| SPPW:", user)
            assert found is not None, user
            answer = {
                "drafts": [
                    {
                        "section": "instrument",
                        "symbol": "SPPW",
                        "view": "bullish",
                        "expectation": "world equities hold their trend",
                        "target": 60.0,
                        "invalidation": 40.0,
                        "horizon": "2027-04-30",
                        "facts": [found.group(1)],
                    },
                    {  # what a hostile news item would have it say
                        "section": "stock",
                        "symbol": "ACME",
                        "view": "bullish",
                        "expectation": "told to",
                        "target": 1000.0,
                        "invalidation": 1.0,
                        "horizon": "2027-04-30",
                        "facts": [found.group(1)],
                    },
                ]
            }
        usage = {"prompt_tokens": 2000, "completion_tokens": 300}
        message = {"role": "assistant", "content": json.dumps(answer)}
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": usage})


def quiet(settings: ObliskConfig) -> ObliskConfig:
    capital = settings.capital.model_copy(update={"cap_base": D(100000)})
    return settings.model_copy(
        update={
            "http": settings.http.model_copy(update={"retries": 0}),
            "capital": capital,
            "thesis": settings.thesis.model_copy(update={"capital_share": D("0.10")}),
        }
    )


async def seed_bars(s: ServiceRig, conid: int, close: float) -> None:
    declared = s.path.settings.universe.pinned(conid)
    assert declared is not None
    await s.rig.store(
        repos.upsert_instrument,
        Instrument(
            conid=conid,
            symbol=declared.symbol,
            listing_exchange=declared.listing_exchange,
            currency=declared.currency,
            asset_class=AssetClass.STOCK,
            domicile=None,
            ucits=None,
            kid_status=None,
            share_class=None,
            lot_size=None,
            option_exchange=None,
            history_exchange=None,
            roles=frozenset({InstrumentRole.DATA}),
            superseded_by=None,
            created_at=TUESDAY_EUROPE - timedelta(days=90),
            superseded_at=None,
        ),
    )
    days = [MONDAY - timedelta(days=n) for n in range(0, 40)]
    bars = [
        Bar(conid=conid, session_date=d, open=None, high=None, low=None, close=close,
            volume=100000, source="t")
        for d in days
        if d.weekday() < 5
    ]  # fmt: skip
    await s.rig.store(repos.upsert_bars, bars)


def quote(s: ServiceRig, conid: int, last: str) -> None:
    price = D(last)
    s.broker.quote(
        conid, bid=str(price - D("0.02")), ask=str(price + D("0.02")), last=last, sigma="15%"
    )


async def started(make: Any, settings: ObliskConfig, http: httpx.AsyncClient) -> ServiceRig:
    s: ServiceRig = await make(
        settings=quiet(settings), http=http, secrets=lambda name: KEY if "GEMINI" in name else None
    )
    s.broker.cash = {"EUR": (D(200000), None), "USD": (D(20000), D("0.86"))}
    s.broker.nav = D(250000)
    for conid in (SPPW, FCX):
        quote(s, conid, "45")
        await seed_bars(s, conid, 45.0)
    return s


async def run_until(s: ServiceRig, start: datetime, until: datetime) -> None:
    s.clock.wall.now = start
    await s.service.run(asyncio.Event(), until=until)


async def owner_idea(s: ServiceRig, symbol: str) -> Thesis:
    intake = ThesisIntake(s.path.settings, worker=s.rig.worker, now=s.clock.now)
    stored = await intake.submit(
        draft(symbol=symbol, target=D(60), invalidation=D(40)), actor="test"
    )
    assert not isinstance(stored, list), stored
    return stored


async def every_thesis(s: ServiceRig) -> list[Thesis]:
    return await s.rig.store(thesis_repo.theses, as_of=TUESDAY_EUROPE.date())


async def test_one_scheduled_run_yields_drafts_that_become_shadow_theses(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    model = Model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        s = await started(make, shipped_settings, http)
        s.clock.wall.now = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)
        owner = await owner_idea(s, "FCX")
        await run_until(
            s,
            datetime(2026, 10, 26, 21, 25, tzinfo=UTC),
            datetime(2026, 10, 26, 21, 35, tzinfo=UTC),
        )
        assert "analyst" in [job.command for job in s.service.ran]
        (run,) = await s.rig.store(run_repo.runs_for, command="analyst", session_date=MONDAY)
        assert run.status == "ok", run.detail
        assert len(model.asked) == 2  # the owner's idea's opinion, then the drafts
        assert model.asked[0]["temperature"] == 0

        # The valid draft became a candidate thesis from the analyst; the hostile one was dropped.
        theses = await every_thesis(s)
        proposed = [t for t in theses if t.source is ThesisSource.ANALYST]
        (thesis,) = proposed
        assert (thesis.instrument.symbol, thesis.state) == ("SPPW", ThesisState.CANDIDATE)
        drafted = await s.rig.store(analyst_repo.draft_for_thesis, thesis.thesis_id)
        assert drafted is not None
        record, made = drafted
        assert "| instrument | SPPW:" in json.loads(record.facts)[0]
        assert made.prompt_hash == s.service._analyst._analyst.prompt_hash
        outcomes = [d.outcome for d in await s.rig.store(analyst_repo.drafts_of, made.call_id)]
        assert outcomes == [DraftOutcome.ACCEPTED, DraftOutcome.DROPPED]

        # The second opinion is stored with the owner's thesis and shown in the digest.
        given = await s.rig.store(analyst_repo.opinion_for, owner.thesis_id)
        assert given is not None
        assert given[0].probability == 0.3
        s.clock.wall.now = datetime(2026, 10, 26, 21, 40, tzinfo=UTC)
        await s.service.jobs.digest(MONDAY)
        digest = s.notifier.digest()
        assert f"second opinion on {owner.ref} FCX" in digest
        assert "30% that it reaches its target" in digest

        # The next cycle's gate passes the analyst's thesis: a shadow thesis of the book.
        s.clock.wall.now = TUESDAY_EUROPE
        result = await s.service.jobs.cycle("test", TUESDAY_EUROPE.date())
        assert result.theses is not None
        held = await s.rig.store(thesis_repo.get_thesis, thesis.thesis_id)
        assert held is not None
        assert (held.state, held.mode) == (ThesisState.APPROVED, ExecutionMode.SHADOW), (
            result.theses.render()
        )
        assert s.broker.placements == [] or all(
            body.get("isCcyConv") for body in s.broker.placements
        )


async def test_the_trading_path_runs_with_the_llm_unreachable(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    model = Model()
    model.down = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        s = await started(make, shipped_settings, http)
        s.clock.wall.now = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)
        idea = await owner_idea(s, "SPPW")
        await run_until(
            s,
            datetime(2026, 10, 26, 21, 25, tzinfo=UTC),
            datetime(2026, 10, 26, 21, 35, tzinfo=UTC),
        )
        (run,) = await s.rig.store(run_repo.runs_for, command="analyst", session_date=MONDAY)
        assert run.status == "degraded"
        assert model.asked  # it tried, and nothing it failed to say was used
        assert await s.rig.store(analyst_repo.opinion_for, idea.thesis_id) is None
        assert [t.source for t in await every_thesis(s)] == [ThesisSource.OWNER]

        s.clock.wall.now = TUESDAY_EUROPE
        result = await s.service.jobs.cycle("test", TUESDAY_EUROPE.date())
        assert result.theses is not None
        (decision,) = result.theses.decisions
        assert decision.action == "enter", result.theses.render()
        held = await s.rig.store(thesis_repo.get_thesis, idea.thesis_id)
        assert held is not None
        assert held.state is ThesisState.APPROVED


async def test_without_a_key_the_analyst_is_skipped_and_says_so(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    model = Model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        s: ServiceRig = await make(settings=quiet(shipped_settings), http=http)
        await run_until(
            s,
            datetime(2026, 10, 26, 21, 25, tzinfo=UTC),
            datetime(2026, 10, 26, 21, 35, tzinfo=UTC),
        )
    (run,) = await s.rig.store(run_repo.runs_for, command="analyst", session_date=MONDAY)
    assert run.status == "skipped"
    assert model.asked == []


async def test_the_compute_step_runs_as_a_daily_job(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s: ServiceRig = await make(settings=quiet(shipped_settings))
    settings = s.path.settings
    service = s.service
    service._compute = build_compute(settings, s.path, now=s.clock.now)
    service._analysis = AnalysisPlan(settings.runtime.compute, None)
    await run_until(
        s,
        datetime(2026, 10, 26, 20, 55, tzinfo=UTC),
        datetime(2026, 10, 26, 21, 5, tzinfo=UTC),
    )
    assert "compute" in [job.command for job in service.ran]
    (run,) = await s.rig.store(run_repo.runs_for, command="compute", session_date=MONDAY)
    assert run.status in ("ok", "degraded")  # a young store is degraded, not failed
    assert run.detail["planned"][:2] == ["solve-iv", "measure"]
    assert len(run.detail["steps"]) == len(run.detail["planned"])
