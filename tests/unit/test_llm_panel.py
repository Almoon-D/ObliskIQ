"""The weekly forecast panel's producer (research/preregistrations/scoreboard.md; ported from
Inversor-Inteligente's tests/test_forecast_panel.py): the universe and its hash, the features by
hand, the prompt and the answer's contract, and the weekly run against a mocked provider (no
network): one cohort an ISO week, a stale feed and a thin cohort refused before or after the calls.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from tests.support.core_rig import seed_closes, seed_vehicle

from obliskiq.config import ObliskConfig
from obliskiq.llm.panel import (
    ForecastPanel,
    PanelRules,
    features,
    iso_week,
    panel_prompt,
    panel_prompt_hash,
    panel_universe,
    parse_answer,
    universe_hash,
)
from obliskiq.orchestration.analyst import judge_panel, panel_rules
from obliskiq.store import Store
from obliskiq.store import analyst as analyst_repo
from obliskiq.store import panel as panel_repo
from obliskiq.store import scoreboard as scoreboard_repo
from obliskiq.store.scoreboard import PANEL_LAYER, Call
from obliskiq.store.worker import StoreWorker

D = Decimal
KEY = "test-key"
COMPLETIONS = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
#: Wednesday 6 January 2027, ISO week 2027-W01.
NOW = datetime(2027, 1, 6, 12, tzinfo=UTC)
RULES = PanelRules(horizon_days=28, max_mark_age_days=7, min_names=20, min_coverage=D("0.6"))


def settings_for(settings: ObliskConfig, **panel: Any) -> ObliskConfig:
    llm = settings.llm.model_copy(
        update={"panel": settings.llm.panel.model_copy(update={"batch_pause_seconds": 0, **panel})}
    )
    return settings.model_copy(
        update={"http": settings.http.model_copy(update={"retries": 0}), "llm": llm}
    )


# ── the universe ──


def test_the_universe_is_the_tradable_candidates_without_the_reference(
    shipped_settings: ObliskConfig,
) -> None:
    universe = panel_universe(shipped_settings)
    conids = [i.conid for i in universe]
    assert conids == sorted(conids)
    assert len(universe) >= 20
    assert all(i.tradable for i in universe)
    reference = shipped_settings.portfolio_circuit_breaker.equity_reference_conid
    assert reference not in conids
    prompt = panel_prompt_hash(shipped_settings)
    found = universe_hash(universe, prompt)
    assert found == universe_hash(list(reversed(universe)), prompt)  # order-independent
    assert found != universe_hash(universe[:-1], prompt)  # membership-sensitive
    assert found != universe_hash(universe, "0" * 64)  # a new prompt is a new record


# ── the features ──


def _seed(store: Store, conid: int, closes: list[float], *, last: date) -> None:
    seed_vehicle(store, conid, f"S{conid}")
    days = [last - timedelta(days=len(closes) - 1 - i) for i in range(len(closes))]
    seed_closes(store, conid, dict(zip(days, closes, strict=True)))


def test_features_by_hand_for_a_constant_growth_series(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    (first, second, third, *_) = panel_universe(shipped_settings)
    growth = 1.001
    _seed(store, first.conid or 0, [100 * growth**i for i in range(300)], last=date(2027, 1, 5))
    _seed(store, second.conid or 0, [100.0] * 63, last=date(2027, 1, 5))  # too short
    _seed(store, third.conid or 0, [100 * growth**i for i in range(100)], last=date(2027, 1, 5))
    loaded = features(
        store, shipped_settings, [first, second, third], as_of=NOW.date(), max_age_days=7
    )
    assert loaded.status == "ok"
    assert set(loaded.rows) == {first.conid, third.conid}
    row = loaded.rows[first.conid or 0]
    assert row.returns[21] == pytest.approx((growth**21 - 1) * 100)
    assert row.returns[63] == pytest.approx((growth**63 - 1) * 100)
    assert row.momentum == pytest.approx(growth**231 - 1)  # close 21 back over close 252 back
    assert row.volatility == pytest.approx(0.0, abs=1e-9)  # a constant daily return
    assert row.off_high == pytest.approx(0.0)
    # A name with less than a year has no momentum, and is still forecast.
    assert loaded.rows[third.conid or 0].momentum is None
    assert loaded.rows[third.conid or 0].row()["mom_12_1"] is None


def test_a_stale_feed_and_a_stale_name(store: Store, shipped_settings: ObliskConfig) -> None:
    first, second = panel_universe(shipped_settings)[:2]
    _seed(store, first.conid or 0, [100.0] * 80, last=date(2026, 12, 20))
    stale = features(store, shipped_settings, [first], as_of=NOW.date(), max_age_days=7)
    assert stale.status == "stale"
    assert "2026-12-20" in stale.reason
    empty = features(store, shipped_settings, [second], as_of=NOW.date(), max_age_days=7)
    assert empty.status == "stale"
    assert "no close" in empty.reason
    _seed(store, second.conid or 0, [100.0] * 80, last=date(2027, 1, 5))
    mixed = features(store, shipped_settings, [first, second], as_of=NOW.date(), max_age_days=7)
    assert mixed.status == "ok"
    assert set(mixed.rows) == {second.conid}  # the stale name is not forecast


# ── the prompt and the answer ──


def test_the_prompt_lists_every_name_and_states_the_base_rate(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    names = panel_universe(shipped_settings)[:3]
    for instrument in names:
        _seed(store, instrument.conid or 0, [100.0 + i for i in range(80)], last=date(2027, 1, 5))
    loaded = features(store, shipped_settings, names, as_of=NOW.date(), max_age_days=7)
    prompt = panel_prompt(
        shipped_settings, list(loaded.rows.values()), as_of=date(2027, 1, 5), horizon_days=28
    )
    for instrument in names:
        assert f'"id":{instrument.conid}' in prompt
    assert "Today is 2027-01-05" in prompt
    assert "next 28 calendar days, in EUR" in prompt
    assert "about half" in prompt
    assert "[0.02, 0.98]" in prompt
    # Trailing statistics only: no date after the prompt's day, no outcome.
    assert not re.search(r"2027-0[2-9]", prompt)


def _answer(items: list[dict[str, Any]]) -> str:
    return json.dumps({"forecasts": items})


def test_the_answer_is_clamped_and_unknown_or_repeated_ids_are_ignored(
    shipped_settings: ObliskConfig,
) -> None:
    content = _answer(
        [
            {"id": 1, "p_outperform": 0.99, "expected_excess_pct": 3.0},
            {"id": 2, "p_outperform": 0.01, "expected_excess_pct": -2.0},
            {"id": 2, "p_outperform": 0.7, "expected_excess_pct": 9.0},
            {"id": 3, "p_outperform": 0.5, "expected_excess_pct": 0.0},
            {"id": 4, "p_outperform": 0.6, "expected_excess_pct": 80.0},  # beyond the limit
            {"id": 5, "p_outperform": 0.4, "expected_excess_pct": -1.0},
            {"id": 99, "p_outperform": 0.5, "expected_excess_pct": 0.0},
        ]
    )
    found = parse_answer(content, [1, 2, 3, 4, 5], shipped_settings)
    assert found == {1: (0.98, 3.0), 2: (0.02, -2.0), 3: (0.5, 0.0), 5: (0.4, -1.0)}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "Invalid JSON"),
        (json.dumps({"forecasts": [{"id": 1, "p_outperform": 1.5}]}), "p_outperform"),
        (_answer([{"id": 1, "p_outperform": 0.5, "expected_excess_pct": 0.0}]), "only 1 of 2"),
    ],
)
def test_a_bad_answer_is_refused(
    shipped_settings: ObliskConfig, content: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_answer(content, [1, 2], shipped_settings)


# ── the weekly run ──


def _seed_universe(
    store: Store, settings: ObliskConfig, *, count: int | None = None, last: date | None = None
) -> list[int]:
    seeded: list[int] = []
    for n, instrument in enumerate(panel_universe(settings)[:count]):
        conid = instrument.conid or 0
        closes = [100 * (1 + 0.0005 * (n % 7)) ** i for i in range(260)]
        _seed(store, conid, closes, last=last or date(2027, 1, 5))
        seeded.append(conid)
    return seeded


def _forecasting(request: httpx.Request) -> httpx.Response:
    """A model that answers every id it is asked about."""
    body = json.loads(request.content)
    ids = [int(found) for found in re.findall(r'"id":(\d+)', body["messages"][1]["content"])]
    items = [
        {"id": conid, "p_outperform": 0.55, "expected_excess_pct": (conid % 5) - 2.0}
        for conid in ids
    ]
    content = _answer(items)
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}}], "usage": usage}
    )


async def _run(
    settings: ObliskConfig,
    worker: StoreWorker,
    responder: Any,
    *,
    key: str | None = KEY,
    rules: PanelRules | str = RULES,
    now: datetime = NOW,
) -> tuple[Any, respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(COMPLETIONS).mock(side_effect=responder)
        async with httpx.AsyncClient() as client:
            panel = ForecastPanel(
                settings, rules=rules, client=client, worker=worker, key=key, now=lambda: now
            )
            result = await panel.run()
    return result, route


async def test_the_weekly_run_keeps_one_cohort_with_its_baseline_and_universe(
    store: Store, store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    settings = settings_for(shipped_settings, batch_size=20)
    seeded = _seed_universe(store, settings)
    result, route = await _run(settings, store_worker, _forecasting)
    assert result.state == "ok", result.reason
    assert result.forecast == len(seeded)
    assert route.call_count == math.ceil(len(seeded) / 20)
    kept = scoreboard_repo.calls(store, as_of=NOW, layer=PANEL_LAYER)
    assert len(kept) == len(seeded)
    first = kept[0]
    assert (first.direction, first.probability) == ("up", 0.55)
    assert first.baseline is not None
    assert first.expected_excess == pytest.approx(((first.conid % 5) - 2.0) / 100)
    identity = universe_hash(panel_universe(settings), panel_prompt_hash(settings))
    assert first.detail.startswith(f"universe {identity};")
    calls = panel_repo.panel_calls(store, iso_week(NOW))
    assert all(c.status == "ok" for c in calls)
    assert panel_repo.panel_spend(store, NOW - timedelta(days=1)) == pytest.approx(result.cost_usd)
    assert analyst_repo.llm_spend(store, NOW - timedelta(days=1)) == pytest.approx(result.cost_usd)
    assert judge_panel(result).status == "ok"
    # A second run in the same ISO week makes no call.
    again, route = await _run(settings, store_worker, _forecasting, now=NOW + timedelta(days=2))
    assert again.state == "skipped"
    assert route.call_count == 0
    assert judge_panel(again).status == "skipped"


async def test_a_stale_feed_or_too_few_fresh_names_makes_no_call(
    store: Store, store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    settings = settings_for(shipped_settings)
    _seed_universe(store, settings, last=date(2026, 12, 20))
    stale, route = await _run(settings, store_worker, _forecasting)
    assert stale.state == "skipped"
    assert "stale feed" in stale.reason
    assert route.call_count == 0
    _seed_universe(store, settings, count=10)  # ten fresh names: below 20 and 60 %
    thin, route = await _run(settings, store_worker, _forecasting)
    assert thin.state == "incomplete"
    assert "needed: no call made" in thin.reason
    assert route.call_count == 0
    assert judge_panel(thin).status == "skipped"
    assert scoreboard_repo.calls(store, as_of=NOW, layer=PANEL_LAYER) == []


async def test_a_thin_cohort_after_the_calls_keeps_nothing_and_the_week_is_retried(
    store: Store, store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    settings = settings_for(shipped_settings, batch_size=20, max_consecutive_failures=2)
    _seed_universe(store, settings)
    failing = [_forecasting, httpx.Response(500, text="down"), httpx.Response(500, text="down")]

    def answers(request: httpx.Request) -> httpx.Response:
        step = failing.pop(0) if failing else _forecasting
        return step(request) if callable(step) else step

    thin, route = await _run(settings, store_worker, answers)
    assert thin.state == "incomplete"
    assert "nothing kept" in thin.reason
    assert "2 batches failed in a row" in thin.reason
    assert thin.failed == 2
    assert route.call_count == 3
    assert scoreboard_repo.calls(store, as_of=NOW, layer=PANEL_LAYER) == []
    assert judge_panel(thin).status == "degraded"
    retried, _ = await _run(settings, store_worker, _forecasting, now=NOW + timedelta(days=1))
    assert retried.state == "ok"


async def test_a_malformed_batch_is_asked_again_once_with_its_error(
    store: Store, store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    settings = settings_for(shipped_settings, batch_size=100)
    _seed_universe(store, settings)
    bad = httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    replies: list[Any] = [bad]

    def answers(request: httpx.Request) -> httpx.Response:
        return replies.pop(0) if replies else _forecasting(request)

    result, route = await _run(settings, store_worker, answers)
    assert result.state == "ok"
    assert route.call_count == 2
    retry = json.loads(route.calls[1].request.content)["messages"]
    assert retry[-1]["content"].startswith("That answer is not valid")
    (call,) = panel_repo.panel_calls(store)
    assert call.attempts == 2


async def test_without_a_key_rules_or_budget_nothing_is_spent(
    store: Store, store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    settings = settings_for(shipped_settings)
    _seed_universe(store, settings)
    keyless, route = await _run(settings, store_worker, _forecasting, key=None)
    assert keyless.state == "skipped"
    assert "GEMINI_API_KEY is unset" in keyless.reason
    ruleless, route = await _run(settings, store_worker, _forecasting, rules="not locked")
    assert ruleless.state == "skipped"
    assert "not locked" in ruleless.reason
    poor = settings.model_copy(
        update={
            "llm": settings.llm.model_copy(
                update={"budget": settings.llm.budget.model_copy(update={"monthly_usd": 0.0001})}
            )
        }
    )
    spent, route = await _run(poor, store_worker, _forecasting)
    assert spent.state == "budget_spent"
    assert route.call_count == 0
    assert judge_panel(spent).status == "degraded"


def test_the_panel_s_rules_are_the_locked_registration_s(repo_root: Path, tmp_path: Path) -> None:
    rules = panel_rules(repo_root)
    assert rules == RULES
    assert "installed wheel" in str(panel_rules(None))
    assert "no readable lock" in str(panel_rules(tmp_path))


def test_the_panel_s_grading_reads_its_latest_universe_only() -> None:
    from obliskiq.scoreboard.run import current_universe

    at = NOW
    old = Call(PANEL_LAYER, 1, "up", at, "universe a; m", probability=0.5)
    new = Call(PANEL_LAYER, 2, "up", at + timedelta(days=7), "universe b; m", probability=0.5)
    assert current_universe([old, new]) == [new]
    assert current_universe([]) == []


async def test_batches_are_paced_and_an_answer_invalid_twice_fails_closed(
    store: Store, store_worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    settings = settings_for(shipped_settings, batch_size=20)
    settings = settings.model_copy(
        update={
            "llm": settings.llm.model_copy(
                update={"panel": settings.llm.panel.model_copy(update={"batch_pause_seconds": 4})}
            )
        }
    )
    seeded = _seed_universe(store, settings)
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    bad = httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    replies: list[Any] = [bad, bad]

    def answers(request: httpx.Request) -> httpx.Response:
        return replies.pop(0) if replies else _forecasting(request)

    with respx.mock(assert_all_called=False) as router:
        router.post(COMPLETIONS).mock(side_effect=answers)
        async with httpx.AsyncClient() as client:
            panel = ForecastPanel(
                settings, rules=RULES, client=client, worker=store_worker, key=KEY,
                now=lambda: NOW, sleep=sleep,
            )  # fmt: skip
            result = await panel.run()
    batches = math.ceil(len(seeded) / 20)
    assert pauses == [4] * (batches - 1)
    assert result.invalid == 1
    (first, *_) = panel_repo.panel_calls(store)
    assert (first.status, first.attempts, first.valid) == ("invalid", 2, 0)
    assert first.error is not None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"probability_bounds": (D("0.9"), D("0.1"))}, "lower bound to the upper"),
        ({"momentum_skip_sessions": 300}, "skips fewer sessions"),
        ({"asked_excess_range_pct": D(60)}, "inside the limit"),
        ({"min_history_sessions": 10}, "below the 64"),
    ],
)
def test_the_panel_s_configuration_is_coherent(
    shipped_settings: ObliskConfig, changes: dict[str, Any], message: str
) -> None:
    from pydantic import ValidationError

    from obliskiq.config.schema_llm import PanelConfig

    fields = {**shipped_settings.llm.panel.model_dump(), **changes}
    with pytest.raises(ValidationError, match=message):
        PanelConfig.model_validate(fields)
