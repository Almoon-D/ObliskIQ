"""Macro collection: native frequency, both dates, and the refetch cache.

The release date is the load-bearing detail. Aligning a CPI print to its reference
month means every percentile derived from it knows the number weeks before the
market did, and once that bias is baked in nothing downstream can detect it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

from conftest import without_backoff
from obliskiq.collect.macro_pull import MacroPull
from obliskiq.collect.runlog import CollectorRun, RunStatus
from obliskiq.config import Config
from obliskiq.errors import UpstreamError
from obliskiq.macro.fetchers import (
    CFTC_NET_UNITS,
    FRED_INITIAL_RELEASE_ONLY,
    CftcFetcher,
    EcbFetcher,
    EiaFetcher,
    FredFetcher,
    OnsFetcher,
    StatCanFetcher,
    build_fetchers,
)
from obliskiq.macro.series import (
    POLICY_FROM_API,
    POLICY_REFERENCE_PLUS_LAG,
    POLICY_SAME_AS_REFERENCE,
    POLICY_UNKNOWN,
    VALUE_VINTAGE_INITIAL,
    VALUE_VINTAGE_LATEST,
    Observation,
    SeriesSpec,
    SourcePolicy,
    load_series,
    load_source_policies,
)

SESSION = date(2026, 1, 15)


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


#: The vintage range every FRED test that is not about vintage ranges assumes:
#: wide enough that no window is clamped, so these tests keep asserting the
#: request shape rather than the clamp.
WIDE_VINTAGES = ("1900-01-01", "2100-01-01")


#: What the stub FRED says a series carries, so every row it returns is stamped
#: the way a live one is.
FRED_UNITS = "Index 2025=100"


def fred_client(handler, *, vintages=WIDE_VINTAGES, units=FRED_UNITS) -> httpx.AsyncClient:
    """A FRED client that answers the two metadata routes before the handler sees it.

    ``FredFetcher`` asks ALFRED for the range of vintages it holds, and FRED for
    what the series carries, before asking for observations. A test handler
    written for the observations endpoint would otherwise be handed requests it
    was not written for — and would count them, or record their parameters, as
    though they were observations.
    """
    first, last = vintages

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/vintagedates"):
            ascending = dict(request.url.params).get("sort_order") == "asc"
            return httpx.Response(200, json={"vintage_dates": [first if ascending else last]})
        if request.url.path.endswith("/fred/series"):
            return httpx.Response(200, json={"seriess": [{"units_short": units}]})
        return handler(request)

    return client_for(route)


def config_for(config, **collection_overrides) -> Config:
    data = copy.deepcopy(without_backoff(config).as_dict())
    data["macro"]["collection"].update(collection_overrides)
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    return Config(data)


class StubFetcher:
    """Returns fixed observations and counts how often it was asked."""

    def __init__(
        self, observations: list[Observation] | None = None, error: Exception | None = None
    ):
        self.observations = observations or []
        self.error = error
        self.calls = 0
        self.starts: list[date] = []

    async def fetch(self, spec, policy, *, start):
        self.calls += 1
        self.starts.append(start)
        if self.error is not None:
            raise self.error
        return [
            Observation(
                series_id=spec.id,
                source=spec.source,
                reference_date=obs.reference_date,
                release_date=obs.release_date,
                value=obs.value,
                frequency=spec.frequency,
                units=obs.units,
            )
            for obs in self.observations
        ]


def observation(reference: str, release: str | None, value: float) -> Observation:
    return Observation(
        series_id="X",
        source="fred",
        reference_date=reference,
        release_date=release,
        value=value,
        frequency="monthly",
    )


async def pull(store, cfg, fetchers, **kwargs) -> CollectorRun:
    with CollectorRun(store, "macro") as run:
        await MacroPull(store, cfg, fetchers, today=SESSION).run(run, **kwargs)
    return run


# ── the two dates ──


def test_a_release_date_is_never_silently_the_reference_date():
    """The discipline the whole macro layer turns on."""
    reference = date(2026, 1, 1)
    unknown = SourcePolicy("ecb", True, POLICY_UNKNOWN, 0)
    assert unknown.release_date_for(reference) is None, (
        "a NULL release date is a coverage gap downstream code can SEE; a reference "
        "date wearing a release date's name is look-ahead bias nobody can detect"
    )


def test_a_published_schedule_becomes_reference_plus_lag():
    """CFTC publishes Tuesday positions on the Friday after: a fact, not a guess."""
    policy = SourcePolicy("cftc", True, POLICY_REFERENCE_PLUS_LAG, 3)
    assert policy.release_date_for(date(2026, 1, 13)) == "2026-01-16"


def test_same_as_reference_is_available_but_must_be_chosen_explicitly():
    policy = SourcePolicy("fred", True, POLICY_SAME_AS_REFERENCE, 0)
    assert policy.release_date_for(date(2026, 1, 13)) == "2026-01-13"


def test_from_api_uses_the_date_the_source_stated():
    from obliskiq.macro.series import POLICY_FROM_API

    policy = SourcePolicy("fred", True, POLICY_FROM_API, 0)
    assert policy.release_date_for(date(2026, 1, 1), from_api=date(2026, 2, 12)) == "2026-02-12"
    # And when the source did not state one, it stays unknown rather than defaulting.
    assert policy.release_date_for(date(2026, 1, 1)) is None


def test_every_configured_source_declares_a_release_policy(config):
    policies = load_source_policies(config)
    assert policies
    for name, policy in policies.items():
        assert policy.release_date_policy, name


def test_both_dates_reach_the_store(store, config):
    cfg = config_for(config)
    spec = load_series(cfg)[0]
    fetcher = StubFetcher([observation("2025-12-01", "2026-01-13", 3.1)])

    import asyncio

    asyncio.run(pull(store, cfg, {spec.source: fetcher}, only=[spec.id]))
    row = store.execute(
        "SELECT reference_date, release_date, value FROM macro WHERE series_id = ?", (spec.id,)
    ).fetchone()
    assert row["reference_date"] == "2025-12-01"
    assert row["release_date"] == "2026-01-13"


# ── native frequency ──


async def test_a_fetcher_returning_a_resampled_frequency_is_refused(store, config):
    """A resampled macro series propagates into every percentile built on it."""
    cfg = config_for(config)
    spec = load_series(cfg)[0]

    class Resampler(StubFetcher):
        async def fetch(self, spec, policy, *, start):
            return [
                Observation(
                    series_id=spec.id,
                    source=spec.source,
                    reference_date="2025-12-01",
                    release_date="2026-01-13",
                    value=1.0,
                    frequency="daily" if spec.frequency != "daily" else "monthly",
                )
            ]

    run = await pull(store, cfg, {spec.source: Resampler()}, only=[spec.id])
    assert run.status() == RunStatus.ERROR
    assert any("never resampled" in (o.reason or "") for o in run.outcomes)
    assert store.execute("SELECT COUNT(*) FROM macro").fetchone()[0] == 0


def test_every_declared_series_names_a_known_native_frequency(config):
    from obliskiq.macro.series import FREQUENCIES

    for spec in load_series(config):
        assert spec.frequency in FREQUENCIES


def test_an_unknown_frequency_is_refused(config):
    data = copy.deepcopy(config.as_dict())
    data["macro"]["series"][0]["frequency"] = "fortnightly"
    from obliskiq.errors import ConfigError

    with pytest.raises(ConfigError, match="ever resampled"):
        load_series(Config(data))


async def test_the_fred_fetcher_sends_no_frequency_parameter_by_default(config):
    """'NATIVE frequency. Never upsample.' — enforced by never asking for one."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"observations": []})

    fetcher = FredFetcher(without_backoff(config), fred_client(handler), "key")
    spec = SeriesSpec(id="CPIAUCSL", source="fred", frequency="monthly", group="us_core")
    await fetcher.fetch(spec, SourcePolicy("fred", True, "from_api", 0), start=date(2016, 1, 1))

    assert "frequency" not in seen
    assert "aggregation_method" not in seen


async def test_fred_asks_for_the_initial_release_so_the_date_is_a_fact(config):
    """ALFRED output_type=4: realtime_start is then the FIRST publication date."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2025-12-01", "realtime_start": "2026-01-13", "value": "319.775"},
                    {"date": "2025-11-01", "realtime_start": "2025-12-10", "value": "318.400"},
                ]
            },
        )

    fetcher = FredFetcher(without_backoff(config), fred_client(handler), "key")
    spec = SeriesSpec(id="CPIAUCSL", source="fred", frequency="monthly", group="us_core")
    rows = await fetcher.fetch(
        spec, SourcePolicy("fred", True, "from_api", 0), start=date(2016, 1, 1)
    )

    assert seen["output_type"] == "4"
    # Oldest first, whatever order the response came in. The fetcher merges rows
    # across several real-time windows now, so the wire order stopped meaning
    # anything and the reference date is the only ordering that still does.
    assert [(r.reference_date, r.release_date) for r in rows] == [
        ("2025-11-01", "2025-12-10"),
        ("2025-12-01", "2026-01-13"),
    ]
    # And the release lags the reference, which is the entire point.
    assert all(r.release_date > r.reference_date for r in rows)


async def test_fred_always_states_a_real_time_window(config):
    """Without one, FRED defaults the vintage window to TODAY and answers 400.

    Verified live on 2026-09-04: every FRED series returned
    ``No vintage dates exist for the specified real-time period: 2026-09-04 to
    2026-09-04``. output_type=4 is a vintage query, and a vintage query with no
    window asks about a day that usually has no vintage. This is the assertion
    that keeps the bounds on the request.
    """
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"observations": []})

    fetcher = FredFetcher(without_backoff(config), fred_client(handler), "key")
    spec = SeriesSpec(id="CPIAUCSL", source="fred", frequency="monthly", group="us_core")
    await fetcher.fetch(spec, SourcePolicy("fred", True, "from_api", 0), start=date(2016, 1, 1))

    assert seen["realtime_start"] and seen["realtime_end"]
    assert seen["observation_start"] == seen["realtime_start"]
    assert seen["observation_end"] == seen["realtime_end"]


async def test_a_daily_series_is_split_and_a_monthly_one_is_not(config):
    """The cap is on VINTAGE DATES, and a vintage is a release.

    So the window a series can be asked for in one call depends on how often it
    publishes. Ten years of a monthly series is ~120 vintages and fits; ten years
    of a daily one is ~2,500 and does not — FRED counted DGS10 at 2,472 against
    its 2,000 ceiling. Derived from spec.frequency rather than listed per series,
    because a hand-kept list goes on asserting a cadence after FRED changes one.
    """
    calls: list[tuple[str, str]] = []

    def handler(request):
        params = dict(request.url.params)
        calls.append((params["realtime_start"], params["realtime_end"]))
        return httpx.Response(200, json={"observations": []})

    policy = SourcePolicy("fred", True, "from_api", 0)
    start = date(2016, 1, 1)

    fetcher = FredFetcher(without_backoff(config), fred_client(handler), "key")
    await fetcher.fetch(
        SeriesSpec(id="CPIAUCSL", source="fred", frequency="monthly", group="us_core"),
        policy,
        start=start,
    )
    monthly_calls = len(calls)

    calls.clear()
    await fetcher.fetch(
        SeriesSpec(id="DGS10", source="fred", frequency="daily", group="us_core"),
        policy,
        start=start,
    )
    daily_calls = len(calls)

    assert monthly_calls == 1, "a monthly series over any plausible history fits one call"
    assert daily_calls > 1, "a daily series over ten years does not, and 400s if asked in one"
    # Contiguous and non-overlapping in the OBSERVATION dimension, so no
    # reference date is fetched twice; the merge handles the release that
    # arrives after its own window closes.
    for (_, first_end), (second_start, _) in pairwise(calls):
        assert date.fromisoformat(second_start) == date.fromisoformat(first_end) + timedelta(days=1)


async def test_an_unknown_frequency_is_chunked_as_the_densest_one(config):
    """Guessing sparse produces a window FRED refuses; guessing dense costs calls."""
    calls: list[str] = []

    def handler(request):
        calls.append(dict(request.url.params)["realtime_start"])
        return httpx.Response(200, json={"observations": []})

    fetcher = FredFetcher(without_backoff(config), fred_client(handler), "key")
    await fetcher.fetch(
        SeriesSpec(id="MYSTERY", source="fred", frequency=None, group="us_core"),
        SourcePolicy("fred", True, "from_api", 0),
        start=date(2016, 1, 1),
    )
    assert len(calls) > 1


async def test_the_earliest_release_wins_when_a_reference_date_arrives_twice(config):
    """Chunks overlap in the RELEASE dimension, and the initial release is earlier.

    A row dated near the end of one window can be published inside the next, so
    the same reference date can come back from two calls with two release dates.
    output_type=4 exists to record the FIRST publication, so the earlier one is
    the answer — and an undated row never displaces a dated one, because an
    observation that cannot be aligned point-in-time is a coverage gap rather
    than a better reading.
    """
    responses = [
        {"observations": [{"date": "2020-06-01", "realtime_start": "2020-08-15", "value": "2.0"}]},
        {"observations": [{"date": "2020-06-01", "realtime_start": "2020-07-01", "value": "2.0"}]},
        {"observations": [{"date": "2020-06-01", "realtime_start": "", "value": "2.0"}]},
    ]
    sent = iter(responses)

    def handler(request):
        return httpx.Response(200, json=next(sent, {"observations": []}))

    fetcher = FredFetcher(without_backoff(config), fred_client(handler), "key")
    rows = await fetcher.fetch(
        SeriesSpec(id="DGS10", source="fred", frequency="daily", group="us_core"),
        SourcePolicy("fred", True, "from_api", 0),
        start=date(2016, 1, 1),
    )

    assert len(rows) == 1
    assert rows[0].release_date == "2020-07-01"


async def test_fred_maps_a_missing_observation_to_absent_not_zero(config):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2025-12-01", "realtime_start": "2026-01-13", "value": "."}
                ]
            },
        )

    fetcher = FredFetcher(without_backoff(config), fred_client(handler), "key")
    spec = SeriesSpec(id="DFF", source="fred", frequency="daily", group="us_core")
    rows = await fetcher.fetch(spec, SourcePolicy("fred", True, "from_api", 0), start=SESSION)
    assert rows[0].value is None


# ── the third condition on output_type=4: a window has to contain a vintage ───
#
# ALFRED's vintages for a series begin when FRED started archiving it, not when
# the series did. A real-time period entirely before that answers
# `The series does not exist in ALFRED but may exist in FRED` — a message about
# the SERIES for a fact about the WINDOW, and it was read as the former for two
# sessions. DHHNGSP collected nothing because of it: ALFRED holds it from
# 2014-12-10 and the first chunk of its twenty-year window ended in 2014-08.


async def test_a_window_starting_before_the_first_vintage_is_clamped_to_it(config):
    """The DHHNGSP case. Asked of ALFRED, not recovered from its 400."""
    calls: list[tuple[str, str]] = []

    def handler(request):
        params = dict(request.url.params)
        calls.append((params["realtime_start"], params["realtime_end"]))
        return httpx.Response(200, json={"observations": []})

    fetcher = FredFetcher(
        without_backoff(config),
        fred_client(handler, vintages=("2014-12-10", "2026-09-02")),
        "key",
    )
    await fetcher.fetch(
        SeriesSpec(id="DHHNGSP", source="fred", frequency="daily", group="commodity_history"),
        SourcePolicy("fred", True, "from_api", 0),
        start=date(2006, 9, 9),
    )

    assert calls, "the observations call still happens; only its window moved"
    assert calls[0][0] == "2014-12-10", calls
    # And the trailing end is clamped too, so a discontinued series cannot be
    # asked for a chunk that sits entirely after its last vintage.
    assert calls[-1][1] == "2026-09-02", calls


async def test_a_window_inside_the_vintage_range_is_not_clamped(config):
    """The series verified live must not move. WTI's first vintage predates its
    window's first chunk, which is the only reason that chunk answered at all."""
    calls: list[tuple[str, str]] = []

    def handler(request):
        params = dict(request.url.params)
        calls.append((params["realtime_start"], params["realtime_end"]))
        return httpx.Response(200, json={"observations": []})

    fetcher = FredFetcher(
        without_backoff(config),
        fred_client(handler, vintages=("2005-06-28", "2100-01-01")),
        "key",
    )
    await fetcher.fetch(
        SeriesSpec(id="DGS10", source="fred", frequency="daily", group="us_core"),
        SourcePolicy("fred", True, "from_api", 0),
        start=date(2016, 1, 1),
    )
    assert calls[0][0] == "2016-01-01", "the requested start survives when ALFRED can answer it"


async def test_a_window_with_no_vintage_in_it_is_refused_naming_the_range(config):
    """A refusal that names the remedy, not one that names the wrong cause."""
    from obliskiq.errors import UpstreamError

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("no observations call should be made")

    fetcher = FredFetcher(
        without_backoff(config),
        fred_client(handler, vintages=("1998-01-01", "1999-01-01")),
        "key",
    )
    with pytest.raises(UpstreamError, match="1999-01-01"):
        await fetcher.fetch(
            SeriesSpec(id="GONE", source="fred", frequency="monthly", group="us_core"),
            SourcePolicy("fred", True, "from_api", 0),
            start=date(2016, 1, 1),
        )


async def test_a_series_alfred_holds_no_vintages_for_is_refused_not_worked_around(config):
    """The rejected fallback, asserted rather than described.

    Reissuing as a plain observations call would return values with no
    publication date under a `from_api` policy — NULL release dates, excluded
    from every point-in-time read by `known_at`, and counted as collected. A run
    reporting success over data no consumer can use is the failure this whole
    layer is built against, so the target fails instead.
    """
    from obliskiq.errors import UpstreamError

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError(
            "nothing is requested once ALFRED says it holds no vintage: not the plain "
            f"observations endpoint, not series metadata. Got {request.url.path}."
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, json={"vintage_dates": []})
                if r.url.path.endswith("/vintagedates")
                else handler(r)
            )
        )
    )
    fetcher = FredFetcher(without_backoff(config), client, "key")
    with pytest.raises(UpstreamError, match="no vintage dates"):
        await fetcher.fetch(
            SeriesSpec(id="NOVINTAGE", source="fred", frequency="monthly", group="us_core"),
            SourcePolicy("fred", True, "from_api", 0),
            start=date(2016, 1, 1),
        )


async def test_a_missing_value_is_not_written_to_the_store(store, config):
    cfg = config_for(config)
    spec = load_series(cfg)[0]
    fetcher = StubFetcher(
        [
            observation("2025-12-01", "2026-01-13", 3.1),
            observation("2025-11-01", "2025-12-10", None),
        ]
    )
    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    assert store.execute("SELECT COUNT(*) FROM macro").fetchone()[0] == 1


# ── the commodity history window ──


async def test_commodity_history_uses_the_twenty_year_window_not_the_default(store, config):
    """A 5-year percentile is misleading on an asset whose cycle runs 10-20 years."""
    cfg = config_for(config)
    specs = load_series(cfg)
    commodity = next(s for s in specs if s.is_commodity_history)
    ordinary = next(s for s in specs if not s.is_commodity_history and s.source == commodity.source)

    fetcher = StubFetcher([observation("2025-12-01", "2026-01-13", 70.0)])
    await pull(store, cfg, {commodity.source: fetcher}, only=[commodity.id, ordinary.id])

    assert len(fetcher.starts) == 2
    assert min(fetcher.starts) < max(fetcher.starts)
    span_years = (SESSION - min(fetcher.starts)).days / 365
    assert span_years >= config.require_int("commodities.history.monthly_window_years") - 1


async def test_commodity_history_is_native_by_default_and_monthly_only_when_asked(config):
    """The ONE place a frequency is ever requested, and only on an explicit setting."""
    seen = {}

    def handler(request):
        seen.clear()
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"observations": []})

    spec = SeriesSpec(id="DCOILWTICO", source="fred", frequency="daily", group="commodity_history")
    policy = SourcePolicy("fred", True, "from_api", 0)

    native = FredFetcher(without_backoff(config), fred_client(handler), "key")
    await native.fetch(spec, policy, start=date(2006, 1, 1))
    assert "frequency" not in seen

    data = copy.deepcopy(without_backoff(config).as_dict())
    data["macro"]["commodity_history"]["frequency"] = "monthly"
    monthly = FredFetcher(Config(data), fred_client(handler), "key")
    rows = await monthly.fetch(spec, policy, start=date(2006, 1, 1))
    assert seen["frequency"] == "m"
    assert seen["aggregation_method"] == config.require_str(
        "macro.commodity_history.aggregation_method"
    )
    assert rows == []


# ── the refetch cache ──


async def test_a_repeat_run_inside_the_window_issues_no_request(store, config):
    """Running a collector twice in one day is normal — a manual trigger after a
    failure — and must not spend the rate limit twice."""
    cfg = config_for(config)
    spec = load_series(cfg)[0]
    fetcher = StubFetcher([observation("2025-12-01", "2026-01-13", 3.1)])

    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    assert fetcher.calls == 1
    run = await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    assert fetcher.calls == 1
    assert any(o.detail.get("cached") for o in run.outcomes)


async def test_a_live_fetch_that_returns_nothing_is_degraded_not_ok(store, config):
    """Success over nothing is how a broken decoder looks like a working one.

    This is the symmetry that hid the OECD envelope bug: the decoder returned an
    empty list, the target reported collected, and the run said ok with zero
    rows. A run cannot tell "this source serves nothing for that key" from "our
    decoder does not read that shape" — but it must at least stop calling either
    of them a clean collection.

    Not an error, either. The call worked; failing the run would be as wrong in
    the other direction. Degraded is the status that already means "collected, on
    a weaker input than intended".
    """
    cfg = config_for(config)
    spec = load_series(cfg)[0]

    run = await pull(store, cfg, {spec.source: StubFetcher([])}, only=[spec.id])

    outcome = next(o for o in run.outcomes if o.target == spec.id)
    assert outcome.ok, "the upstream answered; this is not a failure"
    assert outcome.degraded
    assert outcome.rows == 0
    assert outcome.reason and "no observation survived decoding" in outcome.reason


async def test_a_cached_skip_writes_nothing_and_stays_clean(store, config):
    """The one legitimate zero, and it must not be swept up by the rule above.

    A cached target wrote no rows because the store already holds them, which is
    the cache working rather than a collection that came back empty."""
    cfg = config_for(config)
    spec = load_series(cfg)[0]
    fetcher = StubFetcher([observation("2025-12-01", "2026-01-13", 3.1)])

    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    run = await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])

    outcome = next(o for o in run.outcomes if o.target == spec.id)
    assert outcome.rows == 0 and outcome.detail.get("cached")
    assert not outcome.degraded


async def test_force_overrides_the_cache(store, config):
    cfg = config_for(config)
    spec = load_series(cfg)[0]
    fetcher = StubFetcher([observation("2025-12-01", "2026-01-13", 3.1)])

    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id], force=True)
    assert fetcher.calls == 2


async def test_a_zero_window_always_refetches(store, config):
    cfg = config_for(config, refetch_after_seconds=0.0)
    spec = load_series(cfg)[0]
    fetcher = StubFetcher([observation("2025-12-01", "2026-01-13", 3.1)])
    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    assert fetcher.calls == 2


async def test_a_failed_fetch_is_not_cached(store, config):
    """A failure must not lock the series out of the next run."""
    cfg = config_for(config)
    spec = load_series(cfg)[0]
    broken = StubFetcher(error=RuntimeError("upstream down"))
    await pull(store, cfg, {spec.source: broken}, only=[spec.id])

    good = StubFetcher([observation("2025-12-01", "2026-01-13", 3.1)])
    await pull(store, cfg, {spec.source: good}, only=[spec.id])
    assert good.calls == 1


# ── failure handling ──


async def test_a_dead_upstream_stops_the_loop_rather_than_failing_fifty_times(store, config):
    cfg = config_for(config)
    limit = config.require_int("collectors.macro.stop_after_consecutive_failures")
    sources = {s.source for s in load_series(cfg)}
    fetchers = {source: StubFetcher(error=RuntimeError("upstream down")) for source in sources}
    run = await pull(store, cfg, fetchers)

    # Each source is tried up to the limit and then abandoned with ONE finding of
    # its own, so a dead upstream produces a handful of failures rather than one
    # per declared series. A source with fewer series than the limit simply runs
    # out first and is never abandoned.
    counts = Counter(spec.source for spec in load_series(cfg))
    expected = sum(min(n, limit) + (1 if n >= limit else 0) for n in counts.values())
    assert run.targets_failed == expected
    assert run.targets_failed < len(load_series(cfg))


async def test_a_dead_source_does_not_stop_the_sources_that_are_answering(store, config):
    """The guard counts per SOURCE, not per run.

    Counting across every source made any dead upstream abandon the rest. FRED is
    first in the declared list and needs an API key, so an unset FRED_API_KEY
    aborted the whole run before it reached CFTC and ECB — neither of which needs
    a key at all. That is the difference between a macro layer a quarter
    collectible without credentials and one not collectible without them.
    """
    cfg = config_for(config)
    series = load_series(cfg)
    dead, alive = series[0].source, next(s.source for s in series if s.source != series[0].source)
    fetchers = {
        source: StubFetcher(error=RuntimeError("upstream down"))
        for source in {s.source for s in series}
    }
    fetchers[alive] = StubFetcher([observation("2025-12-01", "2026-01-13", 3.1)])

    run = await pull(store, cfg, fetchers)

    written = store.execute("SELECT COUNT(*) FROM macro WHERE source = ?", (alive,)).fetchone()[0]
    assert written > 0, f"{alive} answered but nothing from it was written"
    assert any(t.target == f"source:{dead}" for t in run.outcomes if not t.ok)


async def test_idempotency_across_runs(store, config):
    cfg = config_for(config, refetch_after_seconds=0.0)
    spec = load_series(cfg)[0]
    fetcher = StubFetcher([observation("2025-12-01", "2026-01-13", 3.1)])

    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    before = store.execute("SELECT COUNT(*) FROM macro").fetchone()[0]
    await pull(store, cfg, {spec.source: fetcher}, only=[spec.id])
    assert store.execute("SELECT COUNT(*) FROM macro").fetchone()[0] == before


# ── per-source fetchers ──


def _cftc_client(rows):
    def handler(request):
        return httpx.Response(200, json=rows)

    return client_for(handler)


_CFTC_SPEC = SeriesSpec(
    id="COT_WTI", source="cftc", frequency="weekly", group="positioning", key="067651"
)


async def test_cftc_stores_net_non_commercial_positioning_as_a_share_of_open_interest(config):
    """The only flow measure in the whole grid; everything else is price-derived.

    Stored as a SHARE, not a contract count. Both consumers rank it as a
    percentile of its own multi-year window, and open interest is not constant
    over such a window — WTI's net count spans 39,800 to 739,097 across ten years
    of the real series, so a percentile of the count ranks how large the market
    got as much as how crowded it is. open_interest_all arrives in the same row
    and used to be discarded.
    """
    fetcher = CftcFetcher(
        without_backoff(config),
        _cftc_client(
            [
                {
                    "report_date_as_yyyy_mm_dd": "2026-01-13T00:00:00.000",
                    "noncomm_positions_long_all": "310000",
                    "noncomm_positions_short_all": "125000",
                    "open_interest_all": "1850000",
                }
            ]
        ),
    )
    rows = await fetcher.fetch(
        _CFTC_SPEC, SourcePolicy("cftc", True, POLICY_REFERENCE_PLUS_LAG, 3), start=date(2021, 1, 1)
    )
    assert rows[0].value == pytest.approx(185000.0 / 1850000.0)
    assert rows[0].units == CFTC_NET_UNITS
    assert rows[0].reference_date == "2026-01-13"
    assert rows[0].release_date == "2026-01-16"


async def test_the_share_is_a_fraction_and_carries_the_sign_of_the_net(config):
    """Net SHORT is a negative share. A percentile over a one-tailed series would
    rank every short week identically, which is the reading that matters most."""
    fetcher = CftcFetcher(
        without_backoff(config),
        _cftc_client(
            [
                {
                    "report_date_as_yyyy_mm_dd": "2026-01-13T00:00:00.000",
                    "noncomm_positions_long_all": "100000",
                    "noncomm_positions_short_all": "300000",
                    "open_interest_all": "1000000",
                }
            ]
        ),
    )
    rows = await fetcher.fetch(
        _CFTC_SPEC, SourcePolicy("cftc", True, POLICY_REFERENCE_PLUS_LAG, 3), start=date(2021, 1, 1)
    )
    assert rows[0].value == pytest.approx(-0.2)


@pytest.mark.parametrize("open_interest", ["0", None, "not-a-number"])
async def test_a_week_with_no_usable_open_interest_has_no_share_to_report(config, open_interest):
    """None, never a fall back to the raw count.

    One contract count inside a window of shares is exactly the units mix
    migration 015 exists to remove, and it would arrive looking like a real
    reading rather than an absence.
    """
    row = {
        "report_date_as_yyyy_mm_dd": "2026-01-13T00:00:00.000",
        "noncomm_positions_long_all": "310000",
        "noncomm_positions_short_all": "125000",
    }
    if open_interest is not None:
        row["open_interest_all"] = open_interest
    fetcher = CftcFetcher(without_backoff(config), _cftc_client([row]))
    rows = await fetcher.fetch(
        _CFTC_SPEC, SourcePolicy("cftc", True, POLICY_REFERENCE_PLUS_LAG, 3), start=date(2021, 1, 1)
    )
    assert rows[0].value is None
    assert rows[0].units == CFTC_NET_UNITS


async def test_eia_decodes_the_v2_envelope(config):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "response": {"data": [{"period": "2026-01-09", "value": "424500", "units": "MBBL"}]}
            },
        )

    fetcher = EiaFetcher(without_backoff(config), client_for(handler), "key")
    spec = SeriesSpec(
        id="WCESTUS1",
        source="eia",
        frequency="weekly",
        group="energy_inventory",
        key="petroleum/stoc/wstk",
    )
    rows = await fetcher.fetch(
        spec, SourcePolicy("eia", True, POLICY_REFERENCE_PLUS_LAG, 3), start=date(2021, 1, 1)
    )
    assert rows[0].value == 424500.0
    assert rows[0].units == "MBBL"
    assert rows[0].release_date == "2026-01-12"


SDMX = {
    "dataSets": [{"series": {"0:0:0": {"observations": {"0": [3.15], "1": [3.22]}}}}],
    "structure": {
        "dimensions": {
            "observation": [{"id": "TIME_PERIOD", "values": [{"id": "2025-11"}, {"id": "2025-12"}]}]
        }
    },
}


async def test_ecb_decodes_sdmx_json_and_leaves_the_release_date_unknown(config):
    """SDMX carries no first-publication date. NULL is honest; a guess is not."""
    fetcher = EcbFetcher(
        without_backoff(config), client_for(lambda r: httpx.Response(200, json=SDMX))
    )
    spec = SeriesSpec(
        id="HICP_YOY",
        source="ecb",
        frequency="monthly",
        group="euro",
        key="ICP/M.U2.N.000000.4.ANR",
    )
    rows = await fetcher.fetch(spec, SourcePolicy("ecb", True, POLICY_UNKNOWN, 0), start=SESSION)

    assert [(r.reference_date, r.value) for r in rows] == [
        ("2025-11-01", 3.15),
        ("2025-12-01", 3.22),
    ]
    assert all(r.release_date is None for r in rows)


#: What the OECD actually served, observed live on 2026-09-04: SDMX-JSON **2.0**,
#: with dataSets and the structure nested under `data`, and `structures` a LIST.
#: The test above this one used to assert the OECD spoke the ECB's shape, which
#: was the false premise that hid a decoder returning nothing for two sessions.
#: Under dimensionAtObservation=AllDimensions there is no `series` map: the
#: observation key carries every dimension, time included, at its own position.
#:
#: The OECD fetcher has since been deleted with its one unread series, and these
#: three tests were kept and re-pointed at the ECB rather than deleted with it.
#: They document SDMX, not that publisher: a 2.0 envelope, a content dimension
#: whose id starts with TIME, and a key that addresses several series at once are
#: all shapes the next SDMX source will serve, on the decoder the ECB runs on
#: today. Deleting them would take the evidence out with the caller.
SDMX_V2 = {
    "meta": {},
    "errors": [],
    "data": {
        "dataSets": [{"observations": {"0:0:0": [102.1], "0:0:1": [102.4]}}],
        "structures": [
            {
                "dimensions": {
                    "observation": [
                        {"id": "REF_AREA", "values": [{"id": "DEU"}]},
                        # TIME_HORIZ sorts BEFORE TIME_PERIOD and starts with
                        # "TIME". It is a content dimension, not the time axis.
                        {"id": "TIME_HORIZ", "values": [{"id": "H"}]},
                        {
                            "id": "TIME_PERIOD",
                            "values": [{"id": "2025-11"}, {"id": "2025-12"}],
                        },
                    ]
                }
            }
        ],
    },
}


async def test_sdmx_json_2_is_a_different_envelope_and_the_decoder_reads_it(config):
    """The ECB serves 1.0 and the OECD served 2.0, and they are not the same body.

    1.0 puts dataSets and structure at the top level; 2.0 nests both under `data`
    and makes the structure a list. Reading only the 1.0 shape found no dataSets
    in an OECD response — and finding nothing is not an error, so the decoder
    returned an empty list, the fetcher reported the target collected, and the run
    said ok with zero rows for two sessions.
    """
    fetcher = EcbFetcher(
        without_backoff(config), client_for(lambda r: httpx.Response(200, json=SDMX_V2))
    )
    spec = SeriesSpec(id="X", source="ecb", frequency="monthly", group="euro", key="A/B")
    rows = await fetcher.fetch(spec, SourcePolicy("ecb", True, POLICY_UNKNOWN, 0), start=SESSION)

    assert [(r.reference_date, r.value) for r in rows] == [
        ("2025-11-01", 102.1),
        ("2025-12-01", 102.4),
    ]


async def test_the_time_dimension_is_matched_exactly_not_by_prefix(config):
    """TIME_HORIZ is not the time axis, and it comes first.

    A ``startswith("TIME")`` test took TIME_HORIZ's single non-date value as the
    period list and its position as the time position. Every observation was then
    dropped for an unparseable date — which is the LUCKY outcome. Had that value
    parsed as a date, every observation in the response would have been stamped
    with it, which is the failure the decoder's own comment warns about, reached
    from the other end. So this asserts the dates, not merely that rows came back.
    """
    fetcher = EcbFetcher(
        without_backoff(config), client_for(lambda r: httpx.Response(200, json=SDMX_V2))
    )
    spec = SeriesSpec(id="X", source="ecb", frequency="monthly", group="euro", key="A/B")
    rows = await fetcher.fetch(spec, SourcePolicy("ecb", True, POLICY_UNKNOWN, 0), start=SESSION)

    assert [r.reference_date for r in rows] == ["2025-11-01", "2025-12-01"]
    assert len({r.reference_date for r in rows}) == len(rows), (
        "two observations on one date is the wrong-time-dimension signature"
    )


async def test_a_response_holding_several_series_is_refused_not_merged(config):
    """Under AllDimensions there is no `series` map, so nothing counted them.

    The OECD's CLI dataflow with an empty REF_AREA returned 23 countries in one
    body.
    The observations of all of them land in one flat dictionary, so continuing
    means merging 23 countries' values onto the same dates under one id — a
    number nobody could trace back to a country. The `series` branch can warn and
    take the first; here there is no first to take.
    """
    from obliskiq.errors import UpstreamError

    body = copy.deepcopy(SDMX_V2)
    dims = body["data"]["structures"][0]["dimensions"]["observation"]
    dims[0]["values"] = [{"id": "DEU"}, {"id": "FRA"}]

    fetcher = EcbFetcher(
        without_backoff(config), client_for(lambda r: httpx.Response(200, json=body))
    )
    spec = SeriesSpec(id="X", source="ecb", frequency="monthly", group="euro", key="A/B")

    with pytest.raises(UpstreamError) as caught:
        await fetcher.fetch(spec, SourcePolicy("ecb", True, POLICY_UNKNOWN, 0), start=SESSION)

    # The message has to name what to pin, or the reader has a refusal and no
    # remedy — which is how a collector failure becomes a permanent gap.
    assert "REF_AREA" in str(caught.value)
    assert "macro.series" in str(caught.value)


async def test_the_ecb_envelope_still_decodes_after_the_two_versions_were_split(config):
    """1.0 is not broken by teaching the decoder 2.0. The ECB is 13 live series."""
    fetcher = EcbFetcher(
        without_backoff(config), client_for(lambda r: httpx.Response(200, json=SDMX))
    )
    spec = SeriesSpec(id="X", source="ecb", frequency="monthly", group="euro", key="A/B")
    rows = await fetcher.fetch(spec, SourcePolicy("ecb", True, POLICY_UNKNOWN, 0), start=SESSION)
    assert [r.value for r in rows] == [3.15, 3.22]


async def test_a_quarterly_sdmx_period_maps_to_the_first_day_of_the_quarter(config):
    payload = copy.deepcopy(SDMX)
    payload["structure"]["dimensions"]["observation"][0]["values"] = [
        {"id": "2025-Q3"},
        {"id": "2025-Q4"},
    ]
    fetcher = EcbFetcher(
        without_backoff(config), client_for(lambda r: httpx.Response(200, json=payload))
    )
    spec = SeriesSpec(id="X", source="ecb", frequency="quarterly", group="euro", key="A/B")
    rows = await fetcher.fetch(spec, SourcePolicy("ecb", True, POLICY_UNKNOWN, 0), start=SESSION)
    assert [r.reference_date for r in rows] == ["2025-07-01", "2025-10-01"]


async def test_a_keyed_source_without_a_key_says_so_rather_than_returning_empty(config):
    from obliskiq.errors import UpstreamError

    fetcher = FredFetcher(without_backoff(config), client_for(lambda r: httpx.Response(200)), None)
    spec = SeriesSpec(id="DFF", source="fred", frequency="daily", group="us_core")
    with pytest.raises(UpstreamError, match="FRED_API_KEY is unset"):
        await fetcher.fetch(spec, SourcePolicy("fred", True, "from_api", 0), start=SESSION)


def test_build_fetchers_covers_every_declared_source(config):
    fetchers = build_fetchers(
        without_backoff(config), httpx.AsyncClient(), fred_api_key="k", eia_api_key="k"
    )
    assert {spec.source for spec in load_series(config)} <= set(fetchers)


# ── the two national offices, and the rebase ──────────────────────────────────
#
# Every body these read was RECORDED from the live route on 2026-09-15; see
# tests/fixtures/macro/README.md for the exact request each came from. A fixture
# is evidence about this build's parsing and never about the publisher's format.

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "macro"

ONS_KEY = "economy/inflationandpriceindices/timeseries/d7g7/mm23"


def macro_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def ons_spec(**over) -> SeriesSpec:
    return SeriesSpec(
        **{
            "id": "UK_CPI_YOY",
            "source": "ons",
            "frequency": "monthly",
            "group": "cpi",
            "key": ONS_KEY,
            **over,
        }
    )


def statcan_spec(**over) -> SeriesSpec:
    return SeriesSpec(
        **{
            "id": "CA_CPI",
            "source": "statcan",
            "frequency": "monthly",
            "group": "cpi",
            "key": "41690973",
            **over,
        }
    )


FROM_API = SourcePolicy("ons", True, POLICY_FROM_API, 0)


async def test_the_ons_route_is_read_into_observations(config):
    body = macro_fixture("ons_d7g7_mm23")

    def handler(request):
        assert request.url.path == f"/{ONS_KEY}/data"
        return httpx.Response(200, json=body)

    rows = await OnsFetcher(without_backoff(config), client_for(handler)).fetch(
        ons_spec(), FROM_API, start=date(2024, 1, 1)
    )
    assert rows
    newest = rows[-1]
    assert newest.reference_date == "2026-07-01"
    assert newest.value == pytest.approx(2.9)
    assert newest.frequency == "monthly"
    # description.unit, so a percent rate is never mistaken for an index level.
    assert newest.units == "%"
    assert all(o.release_date is not None for o in rows)


async def test_an_ons_release_date_is_the_london_day_not_the_utc_one(config):
    """The one-day look-ahead this fetcher exists to not have.

    ONS stamps July 2026 `2026-08-18T23:00:00.000Z`. London was on BST, so that
    instant is 2026-08-19 00:00 there — and 2026-08-19 is the Wednesday ONS
    published it, as is the 2026-09-16 the same payload names as its next
    release. Taking `.date()` off the UTC value would date every UK CPI print one
    day early, which on a series read point-in-time is look-ahead bias of exactly
    the kind release dates exist to prevent.
    """
    body = macro_fixture("ons_d7g7_mm23")
    july = next(m for m in body["months"] if m["date"] == "2026 JUL")
    assert july["updateDate"].startswith("2026-08-18T23:00")

    rows = await OnsFetcher(
        without_backoff(config), client_for(lambda r: httpx.Response(200, json=body))
    ).fetch(ons_spec(), FROM_API, start=date(2026, 1, 1))

    printed = {o.reference_date: o.release_date for o in rows}
    assert printed["2026-07-01"] == "2026-08-19"


async def test_an_ons_body_with_no_months_is_refused_not_reported_empty(config):
    """A decoded-nothing is a shape change, and it must not read as a thin series.

    `macro_pull` calls a live fetch returning zero rows `degraded` rather than
    failed, which is right when the upstream really has nothing and wrong when
    the response moved. The route answering at all means it has months.
    """
    rows = client_for(lambda r: httpx.Response(200, json={"description": {}, "months": []}))
    with pytest.raises(UpstreamError, match="returned no months"):
        await OnsFetcher(without_backoff(config), rows).fetch(
            ons_spec(), FROM_API, start=date(2024, 1, 1)
        )


async def test_an_ons_month_is_built_from_its_fields_not_its_label(config):
    """`year` and `month` are the data; `2026 JUL` is a display string.

    A row whose month name is not one of the twelve is skipped rather than
    guessed at, and an empty value is stored absent rather than as zero.
    """
    MARCH_STAMP = "2026-04-15T23:00:00.000Z"
    APRIL_STAMP = "2026-05-20T23:00:00.000Z"
    body = {
        "description": {"unit": "%"},
        "months": [
            {"year": "2026", "month": "March", "value": "3.1", "updateDate": MARCH_STAMP},
            {"year": "2026", "month": "Smarch", "value": "9.9", "updateDate": APRIL_STAMP},
            {"year": "2026", "month": "April", "value": "", "updateDate": APRIL_STAMP},
        ],
    }
    rows = await OnsFetcher(
        without_backoff(config), client_for(lambda r: httpx.Response(200, json=body))
    ).fetch(ons_spec(), FROM_API, start=date(2026, 1, 1))

    assert [o.reference_date for o in rows] == ["2026-03-01", "2026-04-01"]
    assert rows[0].value == pytest.approx(3.1)
    assert rows[1].value is None


async def test_the_statcan_vector_is_read_with_its_release_time(config):
    body = macro_fixture("statcan_v41690973")
    sent: list = []

    def handler(request):
        sent.append(json.loads(request.content))
        assert request.method == "POST"
        return httpx.Response(200, json=body)

    rows = await StatCanFetcher(without_backoff(config), client_for(handler)).fetch(
        statcan_spec(), SourcePolicy("statcan", True, POLICY_FROM_API, 0), start=date(2024, 6, 1)
    )
    assert sent[0][0]["vectorId"] == 41690973
    assert sent[0][0]["latestN"] > 0
    newest = rows[-1]
    assert newest.reference_date == "2026-08-01"
    assert newest.release_date == "2026-09-14"
    assert newest.value == pytest.approx(169.8)
    # Genuinely per observation, not one date stamped across the series — which
    # is the property that disqualified every other national route probed.
    assert len({o.release_date for o in rows}) > 1


async def test_a_statcan_vector_that_is_not_a_number_is_refused(config):
    with pytest.raises(UpstreamError, match="not a vector id"):
        await StatCanFetcher(without_backoff(config), client_for(lambda r: None)).fetch(
            statcan_spec(key="all-items"),
            SourcePolicy("statcan", True, POLICY_FROM_API, 0),
            start=date(2024, 6, 1),
        )


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ([{"status": "FAILED", "object": "Vector not found"}], "not SUCCESS"),
        ({"status": "SUCCESS"}, "no vector envelope"),
        (
            [{"status": "SUCCESS", "object": {"terminated": 1, "vectorDataPoint": []}}],
            "TERMINATED",
        ),
    ],
)
async def test_a_statcan_envelope_that_is_not_a_success_is_refused(config, body, match):
    """WDS answers HTTP 200 with a per-vector status, so the envelope is the truth.

    A terminated vector is the worst of the three: it keeps serving its last
    value forever, which reads as a current print rather than as a dead series.
    """
    with pytest.raises(UpstreamError, match=match):
        await StatCanFetcher(
            without_backoff(config), client_for(lambda r: httpx.Response(200, json=body))
        ).fetch(
            statcan_spec(),
            SourcePolicy("statcan", True, POLICY_FROM_API, 0),
            start=date(2024, 6, 1),
        )


async def test_the_statcan_window_is_converted_from_dates_to_a_period_count(config):
    """WDS pages by count, so the collector's date window has to become one.

    Derived from the same publication-rate table the FRED chunking uses, so a
    cadence is not written down twice.
    """
    sent: list = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=macro_fixture("statcan_v41690973"))

    fetcher = StatCanFetcher(without_backoff(config), client_for(handler))
    await fetcher.fetch(
        statcan_spec(),
        SourcePolicy("statcan", True, POLICY_FROM_API, 0),
        start=date.today() - timedelta(days=365),
    )
    # Twelve months, plus the period of slack that puts the opening month inside
    # the window rather than on its edge.
    assert sent[0][0]["latestN"] == 13


# ── the rebase ────────────────────────────────────────────────────────────────


def hicp_client(handler=None):
    """A FRED stub serving the recorded CP0000DEM086NEST bodies.

    The two observation files are the same series over the same months on two
    different bases; which one comes back depends on whether the request asks
    for initial releases, exactly as FRED behaves.
    """

    def route(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.path.endswith("/vintagedates"):
            ascending = params.get("sort_order") == "asc"
            return httpx.Response(
                200, json={"vintage_dates": ["2000-01-01" if ascending else "2026-09-15"]}
            )
        if request.url.path.endswith("/fred/series"):
            return httpx.Response(200, json=macro_fixture("fred_hicp_de_series"))
        if handler is not None:
            handler(request)
        name = (
            "fred_hicp_de_initial_release"
            if params.get("output_type") == FRED_INITIAL_RELEASE_ONLY
            else "fred_hicp_de_latest_vintage"
        )
        return httpx.Response(200, json=macro_fixture(name))

    return client_for(route)


def hicp_spec(**over) -> SeriesSpec:
    return SeriesSpec(
        **{
            "id": "CP0000DEM086NEST",
            "source": "fred",
            "frequency": "monthly",
            "group": "cpi",
            "value_vintage": VALUE_VINTAGE_LATEST,
            **over,
        }
    )


def test_the_recorded_initial_releases_really_do_change_base():
    """The premise, asserted so the regression below cannot pass vacuously.

    If Eurostat's rebase ever washes out of ALFRED's initial-release column, this
    fails first and says why the test underneath stopped meaning anything.
    """
    rows = {
        o["date"][:7]: float(o["value"])
        for o in macro_fixture("fred_hicp_de_initial_release")["observations"]
    }
    assert rows["2025-12"] > 120  # 2015 = 100
    assert rows["2026-01"] < 110  # 2025 = 100


async def test_a_rebased_index_is_valued_from_one_vintage_and_dated_from_the_first(config):
    """The -22.6% that would otherwise be reported as German inflation.

    `cpi_yoy` divides the newest observation by the one a year older. Off the
    initial-release column those two sit on different bases, and the quotient is
    a fifth of what it should be — carrying a fresh release date, stamped `ok`,
    with nothing downstream able to tell. So the value comes from the current
    vintage, which is one base throughout, and only the DATE comes from the
    initial release.
    """
    rows = await FredFetcher(without_backoff(config), hicp_client(), "key").fetch(
        hicp_spec(), SourcePolicy("fred", True, POLICY_FROM_API, 0), start=date(2024, 11, 1)
    )
    by_reference = {o.reference_date: o for o in rows}

    # One base: no step between the December before the rebase and the January
    # after it.
    before = by_reference["2025-12-01"].value
    after = by_reference["2026-01-01"].value
    assert abs(after / before - 1.0) < 0.05

    # And still the real first-publication dates, which is the whole reason the
    # vintage walk still runs.
    assert by_reference["2026-07-01"].release_date == "2026-08-19"
    assert by_reference["2026-06-01"].release_date == "2026-07-17"


async def test_the_default_vintage_is_unchanged_and_values_the_initial_release(config):
    """Every series that did not ask for this keeps the old behaviour exactly."""
    rows = await FredFetcher(without_backoff(config), hicp_client(), "key").fetch(
        hicp_spec(value_vintage=VALUE_VINTAGE_INITIAL),
        SourcePolicy("fred", True, POLICY_FROM_API, 0),
        start=date(2024, 11, 1),
    )
    by_reference = {o.reference_date: o.value for o in rows}
    assert by_reference["2025-12-01"] > 120
    assert by_reference["2026-01-01"] < 110


async def test_every_fred_row_carries_the_units_the_source_states(config):
    """FRED was the one source whose rows could not say what they held.

    It serves percent rates and rebasable index levels side by side, and a NULL
    unit cannot contradict anything — which is what let a base change through.
    """
    rows = await FredFetcher(without_backoff(config), hicp_client(), "key").fetch(
        hicp_spec(), SourcePolicy("fred", True, POLICY_FROM_API, 0), start=date(2024, 11, 1)
    )
    stated = macro_fixture("fred_hicp_de_series")["seriess"][0]["units_short"]
    assert {o.units for o in rows} == {stated}


async def test_the_latest_vintage_call_asks_for_no_real_time_window(config):
    """Because a real-time window is what makes FRED answer with a vintage.

    The point of the second call is to get the series as it stands now; sending
    the bounds the first call needs would get the same mixed-base column back.
    """
    seen: list[dict] = []
    await FredFetcher(
        without_backoff(config), hicp_client(lambda r: seen.append(dict(r.url.params))), "key"
    ).fetch(hicp_spec(), SourcePolicy("fred", True, POLICY_FROM_API, 0), start=date(2024, 11, 1))

    plain = [p for p in seen if "output_type" not in p]
    assert len(plain) == 1
    assert "realtime_start" not in plain[0]
    assert "realtime_end" not in plain[0]
