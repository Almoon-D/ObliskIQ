"""The weekly forecast panel's producer (research/preregistrations/scoreboard.md "The weekly
forecast panel"; MASTER_PLAN §5.19).

The scoreboard grades a layer's calls, a handful a week; the panel asks the model the same question
about the same universe every week, so its skill shows in months rather than years. Every ISO
week, at its first run that keeps a cohort:

* **the universe** is the universe file's tradable candidates, the equity reference left out (it
  is the yardstick). It is hashed, with the panel's prompt hash, and the hash is on every forecast:
  a change of universe or prompt starts a new record;
* **the features** are trailing price statistics of each name's stored closes, in its own
  currency, and nothing else: its returns over the configured sessions, 12-1 momentum, annualised
  volatility and its distance from the high. A name without a close at most the pre-registration's
  mark age old, or without enough history, is not forecast (never filled in); a feed whose newest
  close is older than that skips the week before any call;
* **the model** is the analyst's, on the analyst's provider and within the one monthly LLM
  budget: per batch of names, the probability that each beats the equity reference over the
  horizon in the base currency and its expected excess return. An answer that is not the schema's
  JSON, or covers too few of its batch, is asked again once with the error, then fails closed;
  failing batches in a row stop the run;
* **a cohort** is kept only when at least the pre-registration's share of the universe and its
  minimum of names were forecast: each forecast a scoreboard call of layer ``panel`` (direction
  ``up``, its probability, its expected excess as a fraction and 12-1 momentum beside it as the
  baseline). Otherwise nothing is kept and the week is tried again at its next run.

Ported from Inversor-Inteligente's ``modules/analytics/forecast_panel.py`` @68280e1 (Apache License
2.0), modified and used here under AGPL-3.0-or-later as the Apache License permits. The model sees
no outcome and no account figure; nothing here sizes, orders or gates.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import dataclasses
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import stdev
from typing import Final, Literal

import httpx
from pydantic import Field, ValidationError

from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..domain.base import DomainModel
from ..logging_setup import get_logger
from ..store import analyst as analyst_repo
from ..store import panel as panel_repo
from ..store import repos
from ..store import scoreboard as scoreboard_repo
from ..store.db import Store
from ..store.panel import PanelCall, PanelStatus
from ..store.scoreboard import PANEL_LAYER, Call
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy
from .analyst import problems
from .analyst_prompts import schema_of
from .extractor import TOKENS_PER_PRICE_UNIT, month_start
from .transport import TRANSPORTS, Reply

log = get_logger("llm.panel")

type PanelState = Literal["ok", "skipped", "incomplete", "budget_spent"]
PERCENT: Final = 100.0

PANEL_SYSTEM: Final = (
    "You are a systematic forecaster of funds and shares. You answer with one JSON object that "
    "matches the schema you are given, and nothing else."
)


@dataclass(frozen=True, slots=True)
class PanelRules:
    """What the pre-registration fixes for the panel: the horizon, the age of a close, and the
    names and share of the universe a cohort needs."""

    horizon_days: int
    max_mark_age_days: int
    min_names: int
    min_coverage: Decimal


# ── the universe and its features ──


def panel_universe(settings: ObliskConfig) -> list[UniverseInstrument]:
    """The tradable candidates with a conid, the equity reference left out, by conid."""
    reference = settings.portfolio_circuit_breaker.equity_reference_conid
    return sorted(
        (
            i
            for i in settings.universe.instruments
            if i.tradable and i.conid is not None and i.conid != reference
        ),
        key=lambda i: i.conid or 0,
    )


def universe_hash(universe: Sequence[UniverseInstrument], prompt_hash: str) -> str:
    """The record's identity: the universe's names and the prompt's hash."""
    lines = [f"{i.conid}:{i.symbol}:{i.listing_exchange}" for i in universe]
    payload = prompt_hash + "\n" + "\n".join(sorted(lines))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Features:
    """A name's trailing statistics, in percent of its own currency's price (``momentum`` as a
    fraction too, for the baseline)."""

    conid: int
    symbol: str
    currency: str
    last_day: date
    returns: dict[int, float]
    momentum: float | None
    volatility: float | None
    off_high: float

    def row(self) -> dict[str, object]:
        return {
            "id": self.conid,
            "symbol": self.symbol,
            "currency": self.currency,
            **{f"ret_{s}": round(v, 1) for s, v in self.returns.items()},
            "mom_12_1": None if self.momentum is None else round(self.momentum * PERCENT, 1),
            "vol": None if self.volatility is None else round(self.volatility, 1),
            "off_high": round(self.off_high, 1),
        }


@dataclass(frozen=True, slots=True)
class FeatureLoad:
    """The features, or why the week is skipped (``stale``: no close fresh enough anywhere)."""

    status: Literal["ok", "stale"]
    rows: dict[int, Features]
    newest: date | None
    reason: str = ""


def features(
    store: Store,
    settings: ObliskConfig,
    universe: Sequence[UniverseInstrument],
    *,
    as_of: date,
    max_age_days: int,
) -> FeatureLoad:
    """Each name's statistics from its closes to ``as_of``."""
    config = settings.llm.panel
    depth = max(
        config.min_history_sessions,
        config.momentum_lookback_sessions + 1,
        config.high_sessions,
        *(s + 1 for s in config.return_sessions),
    )
    oldest = as_of - timedelta(days=max_age_days)
    rows: dict[int, Features] = {}
    newest: date | None = None
    for instrument in universe:
        conid = instrument.conid
        assert conid is not None  # the universe keeps pinned names
        bars = repos.bar_series(store, conid, as_of=as_of, limit=depth)
        closes = [b.close for b in bars if b.close > 0]
        if not bars:
            continue
        last_day = bars[-1].session_date
        newest = last_day if newest is None else max(newest, last_day)
        if last_day < oldest or len(closes) < config.min_history_sessions:
            continue
        last = closes[-1]
        daily = [b / a - 1 for a, b in pairwise(closes)]
        recent = daily[-config.volatility_sessions :]
        volatility = (
            stdev(recent) * math.sqrt(config.annualisation_sessions) * PERCENT
            if len(recent) >= config.min_volatility_returns
            else None
        )
        lookback = config.momentum_lookback_sessions
        skip = config.momentum_skip_sessions
        momentum = closes[-1 - skip] / closes[-1 - lookback] - 1 if len(closes) > lookback else None
        rows[conid] = Features(
            conid=conid,
            symbol=instrument.symbol,
            currency=instrument.currency,
            last_day=last_day,
            returns={s: (last / closes[-1 - s] - 1) * PERCENT for s in config.return_sessions},
            momentum=momentum,
            volatility=None if volatility is None or not math.isfinite(volatility) else volatility,
            off_high=(last / max(closes[-config.high_sessions :]) - 1) * PERCENT,
        )
    if newest is None or newest < oldest:
        when = "no close" if newest is None else f"the newest close is of {newest.isoformat()}"
        return FeatureLoad(
            "stale", {}, newest, f"{when}, older than {max_age_days} days: a stale feed"
        )
    return FeatureLoad("ok", rows, newest)


# ── the prompt and the answer ──


class PanelForecast(DomainModel):
    id: int
    p_outperform: float = Field(ge=0, le=1)
    expected_excess_pct: float


class PanelAnswer(DomainModel):
    forecasts: list[PanelForecast]


def panel_prompt(
    settings: ObliskConfig, rows: Sequence[Features], *, as_of: date, horizon_days: int
) -> str:
    """One batch's question: the names' trailing statistics and nothing else."""
    config = settings.llm.panel
    low, high = config.probability_bounds
    reference = settings.universe.pinned(settings.portfolio_circuit_breaker.equity_reference_conid)
    yardstick = "the equity reference" if reference is None else reference.symbol
    base = settings.account.base_currency
    sessions = ", ".join(f"ret_{s}" for s in config.return_sessions)
    return (
        f"Today is {as_of.isoformat()}.\n\n"
        f"For EACH instrument below estimate (a) p_outperform: the probability that its total "
        f"return over the next {horizon_days} calendar days, in {base}, beats that of "
        f"{yardstick} (global equities), and (b) expected_excess_pct: its expected excess return "
        f"over {yardstick} in {base} over the same days, in percent.\n\n"
        "Rules:\n"
        "- Use only the data given and your general knowledge as of the date above. Do not "
        "invent prices, earnings or news.\n"
        "- In a typical window about half the instruments beat the benchmark. Stay near 0.50 "
        "unless you have a specific reason.\n"
        f"- p_outperform lies in [{low}, {high}]; expected_excess_pct in "
        f"[-{config.asked_excess_range_pct}, {config.asked_excess_range_pct}].\n"
        "- With no view on an instrument answer 0.5 and 0.\n"
        "- Answer every id listed, exactly once.\n\n"
        f"Instruments ({sessions} are returns over that many sessions, mom_12_1 the return from "
        "twelve months to one month ago, off_high the distance from the 52-week high, all in "
        "percent of the instrument's own currency; vol is the annualised volatility in percent; "
        "null is not available):\n"
        f"{json.dumps([r.row() for r in rows], separators=(',', ':'))}"
    )


def panel_prompt_hash(settings: ObliskConfig) -> str:
    """The panel's identity besides the model: what its answers depend on."""
    identity = {
        "system": PANEL_SYSTEM,
        "template": panel_prompt(settings, [], as_of=date.min, horizon_days=0),
        "schema": schema_of(PanelAnswer),
        "config": settings.llm.panel.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def parse_answer(
    content: str, expected: Sequence[int], settings: ObliskConfig
) -> dict[int, tuple[float, float]]:
    """The valid forecasts of a batch: ``id -> (probability clamped, expected excess %)``. Raises
    ``ValueError`` when the answer is not the schema's JSON or covers too few of the batch."""
    config = settings.llm.panel
    answer = PanelAnswer.model_validate_json(content)
    low, high = (float(b) for b in config.probability_bounds)
    limit = float(config.max_abs_excess_pct)
    wanted = set(expected)
    found: dict[int, tuple[float, float]] = {}
    for item in answer.forecasts:
        if item.id not in wanted or item.id in found:
            continue  # unknown or repeated: ignored, not an error
        if not math.isfinite(item.expected_excess_pct) or abs(item.expected_excess_pct) > limit:
            continue
        found[item.id] = (min(high, max(low, item.p_outperform)), item.expected_excess_pct)
    if len(found) < float(config.min_batch_coverage) * len(wanted):
        missing = sorted(wanted - set(found))[:8]
        raise ValueError(
            f"only {len(found)} of {len(wanted)} ids have a valid forecast (missing {missing})"
        )
    return found


# ── the run ──


def iso_week(at: datetime) -> str:
    year, week, _ = at.isocalendar()
    return f"{year:04d}-W{week:02d}"


@dataclass(frozen=True, slots=True)
class PanelRun:
    """What one run did."""

    state: PanelState
    reason: str
    week: str
    universe: int = 0
    forecast: int = 0
    calls: int = 0
    failed: int = 0
    invalid: int = 0
    cost_usd: float = 0.0


def cohort_kept(store: Store, *, week: str, as_of: datetime) -> bool:
    """Whether a cohort of ``week`` is already among the scoreboard's calls."""
    calls = scoreboard_repo.calls(store, as_of=as_of, layer=PANEL_LAYER)
    return any(iso_week(call.called_at) == week for call in calls)


def keep_cohort(store: Store, calls: Sequence[Call]) -> None:
    """Every forecast of a cohort, together or none."""
    with store.transaction():
        for call in calls:
            scoreboard_repo.record_call(store, call)


class ForecastPanel:
    """Asks the analyst's model for the week's forecasts and keeps the cohort."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        rules: PanelRules | str,
        client: httpx.AsyncClient,
        worker: StoreWorker,
        key: str | None,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        llm = settings.llm
        self._settings = settings
        self._rules = rules
        self._config = llm.panel
        self._budget = llm.budget
        self._model = llm.analyst_model()
        self._provider_name = self._model.provider
        self._provider = llm.analyst_provider()
        self._chat = TRANSPORTS[self._provider.protocol]
        self._client = client
        self._worker = worker
        self._key = key
        self._now = now
        self._sleep = sleep
        self._policy = dataclasses.replace(
            RetryPolicy.from_settings(settings.http),
            timeout_seconds=float(self._config.request_timeout_seconds),
        )
        self.prompt_hash = panel_prompt_hash(settings)

    async def run(self) -> PanelRun:
        now = self._now()
        week = iso_week(now)
        rules = self._rules
        if isinstance(rules, str):
            return PanelRun("skipped", f"the scoreboard's rules cannot be read: {rules}", week)
        if await self._worker.run(cohort_kept, week=week, as_of=now):
            return PanelRun("skipped", f"the cohort of {week} is kept already", week)
        if self._key is None:
            return PanelRun("skipped", f"{self._provider.key_env} is unset: no call made", week)
        universe = panel_universe(self._settings)
        identity = universe_hash(universe, self.prompt_hash)
        loaded = await self._worker.run(
            features,
            self._settings,
            universe,
            as_of=now.date(),
            max_age_days=rules.max_mark_age_days,
        )
        size = len(universe)
        needed = max(rules.min_names, math.ceil(float(rules.min_coverage) * size))
        if loaded.status == "stale":
            return PanelRun("skipped", f"no call made: {loaded.reason}", week, size)
        if len(loaded.rows) < needed:
            return PanelRun(
                "incomplete",
                f"only {len(loaded.rows)} of {size} names have a fresh close and their history, "
                f"{needed} needed: no call made",
                week,
                size,
            )
        return await self._ask(loaded, rules, week=week, identity=identity, needed=needed)

    async def _ask(
        self, loaded: FeatureLoad, rules: PanelRules, *, week: str, identity: str, needed: int
    ) -> PanelRun:
        config = self._config
        spent = await self._worker.run(analyst_repo.llm_spend, month_start(self._now()))
        ids = sorted(loaded.rows)
        batches = [ids[i : i + config.batch_size] for i in range(0, len(ids), config.batch_size)]
        found: dict[int, tuple[float, float]] = {}
        cost = 0.0
        calls = failed = invalid = in_a_row = 0
        state: PanelState = "ok"
        stopped = ""
        for index, batch in enumerate(batches):
            if index and config.batch_pause_seconds > 0:
                await self._sleep(config.batch_pause_seconds)
            rows = [loaded.rows[conid] for conid in batch]
            prompt = panel_prompt(
                self._settings,
                rows,
                as_of=loaded.newest or self._now().date(),
                horizon_days=rules.horizon_days,
            )
            messages = [
                {"role": "system", "content": PANEL_SYSTEM},
                {"role": "user", "content": prompt},
            ]
            worst = self._worst_cost(messages)
            if spent + worst > self._budget.monthly_usd:
                state = "budget_spent"
                stopped = (
                    f"the month's LLM spend {spent:.4f} plus a call's worst case {worst:.4f} "
                    f"passes {self._budget.monthly_usd:.2f}"
                )
                break
            call, parsed = await self._call(messages, batch, week=week, identity=identity)
            calls += 1
            spent += call.cost_usd
            cost += call.cost_usd
            if parsed is None:
                failed += call.status == "failed"
                invalid += call.status == "invalid"
                in_a_row += 1
                if in_a_row >= config.max_consecutive_failures:
                    stopped = f"{in_a_row} batches failed in a row"
                    break
                continue
            in_a_row = 0
            found.update(parsed)
        size = len(panel_universe(self._settings))
        if len(found) < needed:
            why = f"; {stopped}" if stopped else ""
            return PanelRun(
                "budget_spent" if state == "budget_spent" else "incomplete",
                f"{len(found)} of {size} names forecast, {needed} needed: nothing kept, the week "
                f"is tried again at its next run{why}",
                week,
                size,
                len(found),
                calls,
                failed,
                invalid,
                cost,
            )
        at = self._now()
        kept = [
            Call(
                layer=PANEL_LAYER,
                conid=conid,
                direction="up",
                called_at=at,
                detail=f"universe {identity}; {self._model.model_id}",
                probability=probability,
                expected_excess=excess / PERCENT,
                baseline=loaded.rows[conid].momentum,
            )
            for conid, (probability, excess) in sorted(found.items())
        ]
        await self._worker.run(keep_cohort, kept)
        log.info("kept the forecast panel's cohort", extra={"week": week, "names": len(kept)})
        return PanelRun(
            "ok",
            f"{len(kept)} of {size} names forecast and kept for {week}",
            week,
            size,
            len(kept),
            calls,
            failed,
            invalid,
            cost,
        )

    # ── one call ──

    def _tokens(self, characters: int) -> int:
        return int(characters / self._budget.chars_per_token_estimate) + 1

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        model = self._model
        return (
            input_tokens * model.input_usd_per_million
            + output_tokens * model.output_usd_per_million
        ) / TOKENS_PER_PRICE_UNIT

    def _input_tokens(self, messages: Sequence[dict[str, str]]) -> int:
        return self._tokens(sum(len(m["content"]) for m in messages))

    def _worst_cost(self, messages: Sequence[dict[str, str]]) -> float:
        """Every attempt at its estimated input and its full output allowance."""
        attempts = self._config.retries_on_invalid + 1
        per_call = self._input_tokens(messages) + self._config.max_output_tokens
        return attempts * self._cost(per_call, self._config.max_output_tokens)

    async def _call(
        self, messages: list[dict[str, str]], batch: list[int], *, week: str, identity: str
    ) -> tuple[PanelCall, dict[int, tuple[float, float]] | None]:
        """One batch, asked again once on an invalid answer, recorded as produced."""
        assert self._key is not None  # the run checks the key first
        schema = schema_of(PanelAnswer)
        used_in = used_out = attempts = 0
        status: PanelStatus = "invalid"
        problem = ""
        parsed: dict[int, tuple[float, float]] | None = None
        for _ in range(self._config.retries_on_invalid + 1):
            attempts += 1
            try:
                reply: Reply = await self._chat(
                    self._client,
                    base_url=str(self._provider.base_url),
                    key=self._key,
                    model_id=self._model.model_id,
                    messages=messages,
                    schema=schema,
                    temperature=self._config.temperature,
                    max_tokens=self._config.max_output_tokens,
                    policy=self._policy,
                    worst_input_tokens=self._input_tokens(messages),
                )
            except Exception as exc:  # the call failed: nothing of it is used
                status, problem = "failed", f"{type(exc).__name__}: {exc}"
                break
            used_in += reply.input_tokens
            used_out += reply.output_tokens
            try:
                parsed = parse_answer(reply.content, batch, self._settings)
            except (ValidationError, ValueError) as exc:
                problem = problems(exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": reply.content},
                    {
                        "role": "user",
                        "content": f"That answer is not valid: {problem}. Answer again with "
                        "one JSON object that matches the schema, and nothing else.",
                    },
                ]
                continue
            status = "ok"
            break
        if status != "ok":
            log.warning("a panel batch fails closed", extra={"problem": problem})
        call = PanelCall(
            week=week,
            universe=identity,
            produced_at=self._now(),
            provider=self._provider_name,
            model_id=self._model.model_id,
            prompt_hash=self.prompt_hash,
            status=status,
            attempts=attempts,
            asked=len(batch),
            valid=0 if parsed is None or status != "ok" else len(parsed),
            error=None if status == "ok" else problem or "no answer",
            input_tokens=used_in,
            output_tokens=used_out,
            cost_usd=self._cost(used_in, used_out),
        )
        await self._worker.run(panel_repo.record_panel_call, call)
        return call, parsed if status == "ok" else None
