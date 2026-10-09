"""The collector plumbing: the data seam, the run log, retry, and state.

Small pieces, but each one is the difference between a collector that fails
visibly and one that fails silently on a VM nobody is watching.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
import json

import httpx
import pytest

from conftest import without_backoff
from obliskiq.collect.retry import TargetRetryPolicy, pace, with_target_retry
from obliskiq.collect.runlog import CollectorRun, RunStatus, TargetOutcome
from obliskiq.collect.source import (
    SOURCE_CP_API,
    UNDERLYING_SEC_TYPES,
    Bar,
    CpApiMarketData,
    FixtureMarketData,
    Quote,
    _bars_from_body,
    _futures_from_rows,
)
from obliskiq.config import Config
from obliskiq.errors import AuthError, PermanentUpstreamError, UpstreamError
from obliskiq.ibkr.cp_client import CpApiClient

# ── the fixture provider ──


async def test_a_missing_fixture_raises_rather_than_returning_empty(tmp_path):
    """A collector reporting success over nothing is the failure this package
    exists to prevent — and a test that passes because its fixture was never found
    is the same bug wearing a lab coat."""
    provider = FixtureMarketData(tmp_path)
    with pytest.raises(UpstreamError, match="never invents an empty result"):
        await provider.resolve_conid("SPY")


async def test_the_fixture_provider_tags_rows_with_the_collection_route(fixtures_dir):
    assert FixtureMarketData(fixtures_dir).source == SOURCE_CP_API


async def test_quotes_omit_a_conid_with_no_recorded_quote(market_data):
    quotes = await market_data.quotes([756733, 999_999_999])
    assert 756733 in quotes
    assert 999_999_999 not in quotes


# ── Quote ──


def test_mid_needs_both_sides():
    assert Quote(conid=1, bid=10.0, ask=11.0).mid == 10.5
    assert Quote(conid=1, bid=10.0).mid is None
    assert Quote(conid=1, ask=11.0).mid is None


def test_reference_price_prefers_mid_and_falls_back_to_last():
    assert Quote(conid=1, bid=10.0, ask=11.0, last=99.0).reference_price == 10.5
    assert Quote(conid=1, last=99.0).reference_price == 99.0
    assert Quote(conid=1).reference_price is None


# ── bar decoding ──


def test_bars_are_decoded_from_epoch_milliseconds():
    bars = _bars_from_body(
        {"data": [{"t": 1_768_435_200_000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100}]}
    )
    assert len(bars) == 1
    assert bars[0].ts.endswith("Z") and bars[0].close == 1.5


def test_an_undated_bar_is_dropped():
    """An undated bar cannot be deduplicated, and a duplicate bar is worse than a
    missing one."""
    bars = _bars_from_body({"data": [{"o": 1.0, "c": 1.5}, {"t": 1_768_435_200_000, "c": 2.0}]})
    assert len(bars) == 1


def test_bars_come_back_in_chronological_order():
    body = {"data": [{"t": 1_768_521_600_000, "c": 2.0}, {"t": 1_768_435_200_000, "c": 1.0}]}
    assert [bar.close for bar in _bars_from_body(body)] == [1.0, 2.0]


def test_an_empty_body_decodes_to_no_bars():
    assert _bars_from_body({}) == []
    assert _bars_from_body({"data": None}) == []


# ── futures ladder decoding ──


def test_a_futures_ladder_is_sorted_front_to_back():
    rows = [
        {"conid": 3, "expirationDate": "20260401"},
        {"conid": 1, "expirationDate": "20260201"},
        {"conid": 2, "expirationDate": "20260301"},
    ]
    assert [c.conid for c in _futures_from_rows("CL", rows)] == [1, 2, 3]


def test_a_futures_row_without_a_conid_or_expiry_is_dropped():
    """A curve point that cannot be priced or dated is not a curve point."""
    rows = [
        {"expirationDate": "20260201"},
        {"conid": 2},
        {"conid": 3, "expirationDate": "20260301"},
    ]
    assert [c.conid for c in _futures_from_rows("CL", rows)] == [3]


def test_ibkr_yyyymmdd_expiries_become_iso_dates():
    contracts = _futures_from_rows("CL", [{"conid": 1, "expirationDate": "20260219"}])
    assert contracts[0].expiry == "2026-02-19"


# ── the underlying security-type allowlist ──


def test_only_underlying_security_types_are_allowlisted():
    assert {"STK", "ETF", "FUT", "IND"} == UNDERLYING_SEC_TYPES
    for holding in ("OPT", "FOP", "BOND", "CASH", "WAR", "CFD"):
        assert holding not in UNDERLYING_SEC_TYPES


# ── the run log ──


def test_a_run_is_recorded_before_the_work_starts(store):
    """A run the machine killed must leave evidence rather than nothing at all."""
    with CollectorRun(store, "chain") as run:
        row = store.execute(
            "SELECT status FROM collector_runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()
        assert row["status"] == RunStatus.RUNNING


def test_a_clean_run_is_ok(store):
    with CollectorRun(store, "chain") as run:
        run.succeeded("SPY", 120)
    assert run.status() == RunStatus.OK
    assert run.rows_written == 120


def test_a_run_with_a_degraded_target_is_degraded_not_ok(store):
    """Folding degraded into ok hides a silently worsening data set."""
    with CollectorRun(store, "chain") as run:
        run.succeeded("SPY", 120)
        run.succeeded("FCX", 40, degraded=True, reason="fell back to the default sigma")
    assert run.status() == RunStatus.DEGRADED
    assert run.degraded_targets == ["FCX"]


def test_a_run_with_a_failed_target_is_partial_not_degraded(store):
    """And folding it into partial cries wolf on a run that lost nothing."""
    with CollectorRun(store, "chain") as run:
        run.succeeded("SPY", 120, degraded=True)
        run.failed("QQQ", "no market data permissions")
    assert run.status() == RunStatus.PARTIAL


def test_every_target_failing_is_an_error_not_a_partial_run(store):
    with CollectorRun(store, "chain") as run:
        run.failed("SPY", "boom")
        run.failed("QQQ", "boom")
    assert run.status() == RunStatus.ERROR


def test_an_exception_marks_the_run_error_and_is_re_raised(store):
    with pytest.raises(RuntimeError), CollectorRun(store, "chain") as run:
        run.succeeded("SPY", 10)
        raise RuntimeError("the machine went away")

    row = store.execute(
        "SELECT status, detail FROM collector_runs WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert row["status"] == RunStatus.ERROR
    assert "the machine went away" in row["detail"]


def test_the_detail_column_records_every_target(store):
    with CollectorRun(store, "chain") as run:
        run.succeeded("SPY", 120, sigma_source="realised_30d")
        run.failed("QQQ", "no permissions")

    detail = json.loads(
        store.execute(
            "SELECT detail FROM collector_runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()["detail"]
    )
    targets = {entry["target"]: entry for entry in detail["targets"]}
    assert targets["SPY"]["sigma_source"] == "realised_30d"
    assert targets["QQQ"]["ok"] is False


def test_a_dry_run_writes_no_row(store):
    with CollectorRun(store, "chain", dry_run=True) as run:
        run.succeeded("SPY", 10)
    assert run.run_id is None
    assert store.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0] == 0


def test_a_reason_longer_than_the_excerpt_is_truncated():
    from obliskiq.collect.runlog import DETAIL_EXCERPT_CHARS

    outcome = TargetOutcome(target="X", ok=False, reason="e" * (DETAIL_EXCERPT_CHARS * 2))
    assert len(outcome.as_dict()["reason"]) == DETAIL_EXCERPT_CHARS


def test_the_schema_accepts_every_status_the_code_can_produce(store):
    """A status the code emits but the CHECK constraint rejects would turn a
    degraded run into a crash at the moment it most needs to be recorded."""
    for status in (
        RunStatus.RUNNING,
        RunStatus.OK,
        RunStatus.DEGRADED,
        RunStatus.PARTIAL,
        RunStatus.ERROR,
    ):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
                ("t", "2026-01-15T00:00:00Z", status),
            )


# ── per-target retry ──


async def test_a_target_is_retried_up_to_the_configured_count(config):
    policy = TargetRetryPolicy.from_config(_no_wait(config))
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        await with_target_retry(policy, "SPY", flaky)
    assert attempts["n"] == policy.retries + 1


async def test_a_target_that_recovers_returns_its_result(config):
    policy = TargetRetryPolicy.from_config(_no_wait(config))
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        return "collected"

    assert await with_target_retry(policy, "SPY", flaky) == "collected"


async def test_a_cancellation_is_never_retried(config):
    """Cancelling a run must stop it, not restart the target."""
    import asyncio

    policy = TargetRetryPolicy.from_config(_no_wait(config))

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await with_target_retry(policy, "SPY", cancelled)


async def test_a_permanent_failure_is_not_retried(config):
    """A contract that does not exist returns the same answer three times.

    Retrying one costs two more waits for a conclusion nothing could change, and
    on a collector walking dozens of targets that is most of the run spent
    sleeping. The failure still propagates — the caller records exactly what it
    would have recorded three attempts later, only sooner.
    """
    policy = TargetRetryPolicy.from_config(_no_wait(config))
    attempts = 0

    async def gone():
        nonlocal attempts
        attempts += 1
        raise PermanentUpstreamError("no such contract")

    with pytest.raises(PermanentUpstreamError):
        await with_target_retry(policy, "NOPE", gone)
    assert attempts == 1


async def test_an_auth_failure_is_permanent(config):
    """A rejected signature answers the same however often it is asked, and
    retrying one is the shape that gets an account rate-limited."""
    policy = TargetRetryPolicy.from_config(_no_wait(config))
    attempts = 0

    async def rejected():
        nonlocal attempts
        attempts += 1
        raise AuthError("signature rejected")

    with pytest.raises(AuthError):
        await with_target_retry(policy, "SPY", rejected)
    assert attempts == 1


async def test_an_unclassified_failure_still_gets_every_attempt(config):
    """The default stays 'retry'. Classifying can only make the machine faster
    at failing, never quieter about it."""
    policy = TargetRetryPolicy.from_config(_no_wait(config))
    attempts = 0

    async def timing_out():
        nonlocal attempts
        attempts += 1
        raise UpstreamError("read timeout")

    with pytest.raises(UpstreamError):
        await with_target_retry(policy, "SPY", timing_out)
    assert attempts == policy.retries + 1


def test_target_backoff_is_exponential_and_capped(config):
    policy = TargetRetryPolicy.from_config(config)
    delays = [policy.delay_for(attempt) for attempt in range(8)]
    assert delays == sorted(delays)
    assert max(delays) <= policy.max_delay_seconds


async def test_pacing_is_a_no_op_when_disabled(config):
    await pace(TargetRetryPolicy.from_config(_no_wait(config)))


def _no_wait(config) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    return Config(data)


# ── collector state ──


def test_state_round_trips(store):
    from obliskiq.collect.state import get_state, set_state

    set_state(store, "k", {"bars": 250, "chunk_days": 362})
    assert get_state(store, "k") == {"bars": 250, "chunk_days": 362}


def test_absent_state_is_none(store):
    from obliskiq.collect.state import get_state

    assert get_state(store, "nothing") is None


def test_unparseable_state_is_treated_as_absent(store):
    from obliskiq.collect.state import get_state

    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_state (key, value) VALUES (?, ?)", ("k", "not json")
        )
    assert get_state(store, "k") is None


# ── the CP-backed provider ──


async def test_the_cp_provider_decodes_a_snapshot_into_a_quote(config):
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {
                    "conid": 756733,
                    "31": "601.20",
                    "84": "601.10",
                    "86": "601.30",
                    "7283": "0.1842",
                    "7308": "0.55",
                    "7089": "12000",
                    "87": "41,000,000",
                }
            ],
        )

    client = CpApiClient(
        "https://api.ibkr.com",
        _StubOAuth(),
        without_backoff(config),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = CpApiMarketData(client)
    quote = (await provider.quotes([756733]))[756733]

    assert quote.last == 601.20 and quote.mid == 601.20
    assert quote.implied_vol == 0.1842 and quote.delta == 0.55
    assert quote.open_interest == 12000 and quote.volume == 41_000_000
    assert provider.source == SOURCE_CP_API
    await client.aclose()


def _two_listing_search(request):
    """One ticker, two listings, two different option venues.

    The shape that made SAN and TTE collect nothing: a search on a symbol returns
    every listing of it, and the exchange list travelling with the OPT months
    belongs to whichever row it came from.
    """
    return httpx.Response(
        200,
        json=[
            {
                "conid": 111,
                "symbol": "SPY",
                "sections": [{"secType": "OPT", "months": "JAN26", "exchange": "SMART"}],
            },
            {
                "conid": 756733,
                "symbol": "SPY",
                "sections": [{"secType": "OPT", "months": "APR26;JUN26", "exchange": "ARCA,SMART"}],
            },
        ],
    )


def _search_client(config, handler):
    return CpApiClient(
        "https://api.ibkr.com",
        _StubOAuth(),
        without_backoff(config),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_option_months_come_from_the_match_carrying_the_conid_asked_for(config):
    """Not the first exact symbol match — the one that IS this instrument.

    Both rows here answer to "SPY" and the first one is not the conid the universe
    holds. Reading its section would send SMART to /iserver/secdef/strikes against
    a conid whose options are not listed there, which returns HTTP 200 and an
    empty ladder: a chain silently not collected.
    """
    client = _search_client(config, _two_listing_search)
    months, exchanges = await CpApiMarketData(client).option_months("SPY", conid=756733)
    assert months == ("APR26", "JUN26")
    assert exchanges == ("ARCA", "SMART")

    months, exchanges = await CpApiMarketData(client).option_months("SPY", conid=111)
    assert months == ("JAN26",)
    assert exchanges == ("SMART",)
    await client.aclose()


async def test_a_search_carrying_no_row_for_this_conid_yields_no_months(config):
    """And specifically not another listing's, however well the ticker matches.

    Measured 2026-09-10: SAN and TTE reported five and nine option months against
    a US exchange list and served ZERO strikes, because the universe holds the
    Madrid and Paris conids and the section described New York. Returning nothing
    makes the collector report an underlying with no option months — true of the
    conid it asked about, and a failure rather than a foreign chain.
    """
    client = _search_client(config, _two_listing_search)
    assert await CpApiMarketData(client).option_months("SPY", conid=999999) == ((), ())
    await client.aclose()


class _StubOAuth:
    def sign_request(self, method, url, *, params=None):
        return {"Authorization": 'OAuth realm="limited_poa"'}


def test_a_bar_is_immutable():
    """Bars are records of what an endpoint said. Nothing downstream edits one."""
    bar = Bar(ts="2026-01-15T00:00:00Z", open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)
    with pytest.raises(AttributeError):
        bar.close = 2.0  # type: ignore[misc]
