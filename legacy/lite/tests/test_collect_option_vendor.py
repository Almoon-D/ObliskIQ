"""The marketdata.app decoder, against a verbatim live response.

``tests/fixtures/option_vendor/raw/chain_SPY_live.json`` is a real response
captured 2026-08-28, not a hand-written one — the same discipline
``test_collect_mcp_capture.py`` applies to the IBKR connector. The other
fixtures are synthetic and say so in their own ``note`` field; they cover shapes
one live call could not produce, and each is here because getting it wrong
writes a plausible number into a column nobody would re-check.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from obliskiq.collect.option_vendor import (
    HTTP_CACHED,
    SOURCE_MARKETDATA_APP,
    VENDOR_CHAIN_URL,
    VendorClient,
    VendorError,
    atm_implied_vol,
    decode_chain,
    eastern_session,
    epoch_seconds_to_eastern_date,
    trading_dates_back,
)
from obliskiq.collect.state import (
    UPSTREAM_MARKETDATA_APP,
    active_cooldown,
    cooldown_key,
    set_cooldown,
    set_state,
)
from obliskiq.errors import ObliskError, UpstreamError

RAW = Path(__file__).parent / "fixtures" / "option_vendor" / "raw"
SESSION = date(2026, 8, 28)


def load(name: str) -> dict:
    return json.loads((RAW / f"{name}.json").read_text())


# ── the verbatim live response ──────────────────────────────────────────────


def test_the_live_response_decodes_into_rows_the_store_can_take():
    chain = decode_chain(load("chain_SPY_live"), symbol="SPY", trade_date=SESSION, credits=3)
    assert chain.status == "ok"
    assert len(chain.contracts) == 3
    assert chain.credits == 3

    first = chain.contracts[0]
    assert first.option_symbol == "SPY261231C00765000"
    assert first.expiry == date(2026, 12, 31)
    assert first.strike == 765.0
    assert first.right == "C"  # 'call' -> the store's 'C'
    assert first.iv == 0.1495
    assert first.open_interest == 1431
    assert chain.underlying_price == 770.83


def test_implied_vol_is_carried_through_unscaled():
    """``chain_snapshots.iv`` is documented "decimal, not percent", and the live
    response is already decimal — so the decoder must apply NO conversion. A
    helpful ``/100`` or ``*100`` slipped in later is the failure this catches;
    asserting only that the live values look like decimals would pass for any
    implementation, since the fixture is frozen."""
    body = load("chain_SPY_live")
    chain = decode_chain(body, symbol="SPY", trade_date=SESSION)
    assert [c.iv for c in chain.contracts] == body["iv"]
    assert [c.delta for c in chain.contracts] == body["delta"]


def test_the_expiry_is_read_as_seconds_in_eastern_time():
    """Two traps in one field. ``timeutil.epoch_ms_to_stamp`` would read these
    as MILLISECONDS and land in January 1970; reading them as UTC works only
    because the 16:00 ET equity close is early enough not to cross midnight."""
    assert epoch_seconds_to_eastern_date(1798750800) == date(2026, 12, 31)
    stamped = datetime.fromtimestamp(1798750800, tz=ZoneInfo("America/New_York"))
    assert (stamped.hour, stamped.minute) == (16, 0)
    # And the trap named directly: the repo's existing helper takes MILLISECONDS,
    # so handing it this value lands twenty-one days after the epoch.
    from obliskiq.util.timeutil import epoch_ms_to_stamp

    assert epoch_ms_to_stamp(1798750800).startswith("1970-")


# ── shapes the live call could not produce ──────────────────────────────────


def test_a_ragged_response_zips_to_the_shortest_column():
    """Three keyed contracts, two implied vols. Pairing past the short column
    aligns a strike with another strike's vol — wrong, and entirely plausible."""
    chain = decode_chain(load("chain_ragged"), symbol="A", trade_date=SESSION)
    assert len(chain.contracts) == 3
    assert [c.strike for c in chain.contracts] == [100.0, 105.0, 110.0]
    assert [c.iv for c in chain.contracts] == [0.30, 0.28, None]


def test_no_data_is_an_outcome_not_a_failure():
    """A session with no chain is recorded as zero rows. Raising here would fail
    a target for a market holiday and make the backfill retry it forever."""
    chain = decode_chain(load("chain_no_data"), symbol="SPY", trade_date=SESSION)
    assert chain.status == "no_data"
    assert chain.contracts == ()


def test_an_error_body_names_what_the_vendor_said():
    with pytest.raises(VendorError, match="Only one device"):
        decode_chain(load("chain_blocked"), symbol="SPY", trade_date=SESSION)


def test_a_response_missing_a_keying_column_is_refused():
    """Without strike or expiry there is no primary key, and a row that cannot
    be keyed must not be guessed into place."""
    body = dict(load("chain_SPY_live"))
    del body["strike"]
    with pytest.raises(VendorError, match="missing required column"):
        decode_chain(body, symbol="SPY", trade_date=SESSION)


def test_a_null_implied_vol_stays_null_and_is_counted():
    """Not solved from mid here: solving needs a rate at the contract's own
    maturity and a dividend yield, and inventing either puts a fabricated number
    in the one column this route exists to fill."""
    chain = decode_chain(load("chain_SPY_both_rights"), symbol="SPY", trade_date=SESSION)
    assert chain.null_iv_count == 1
    assert [c.iv for c in chain.contracts] == [0.15, 0.16, None]


# ── the derived ATM figure ──────────────────────────────────────────────────


def test_the_atm_vol_averages_both_rights_and_says_it_was_derived():
    """``underlying_snapshots.implied_vol`` is the series the self-computed IV
    percentile is built from. The vendor serves none for the underlying, so this
    is derived — and the provenance string is what stops a computed figure being
    read later as a served one."""
    chain = decode_chain(load("chain_SPY_both_rights"), symbol="SPY", trade_date=SESSION)
    value, source = atm_implied_vol(chain)
    assert value == pytest.approx((0.15 + 0.16) / 2)
    assert source == "derived_2026-12-18_nearest_strike_both_rights"


def test_the_atm_vol_reports_when_only_one_right_is_present():
    chain = decode_chain(load("chain_SPY_live"), symbol="SPY", trade_date=SESSION)
    value, source = atm_implied_vol(chain)
    assert value == 0.1466  # strike 770 against a 770.83 spot
    assert source == "derived_2026-12-31_nearest_strike_one_right"


def test_the_atm_vol_pins_one_expiry_instead_of_mixing_the_term_structure():
    """The defect this fixture exists for.

    A 90-200 DTE window holds several expiries. Averaging every contract at the
    nearest strike returns the mean of a near-dated and a far-dated vol — a
    number off the term structure rather than a point on it — and as expiries
    roll through the window that mixture changes, putting a sawtooth into the one
    series the system reads historically.
    """
    chain = decode_chain(load("chain_two_expiries"), symbol="SPY", trade_date=SESSION)
    value, source = atm_implied_vol(chain)

    near, far = (0.14 + 0.16) / 2, (0.24 + 0.26) / 2
    assert value == pytest.approx(near), "the far expiry leaked into the ATM figure"
    assert value != pytest.approx((near + far) / 2), "this is the mixed value, the bug"
    assert "2026-12-18" in source, "the provenance must name the expiry it used"


def test_no_atm_without_a_spot_rather_than_a_guess():
    chain = decode_chain(load("chain_no_data"), symbol="SPY", trade_date=SESSION)
    assert atm_implied_vol(chain) == (None, "no_atm_available")


# ── the request the gates build ─────────────────────────────────────────────


@respx.mock
async def test_the_tenor_window_is_sent_as_expiration_DATES(fast_config):
    """The bug this replaced: ``from``/``to`` are expiration DATE bounds on this
    endpoint, and the code sent the raw DTE integers 90 and 200 into them. The
    vendor either rejects that or — the dangerous branch — ignores it and returns
    the entire chain, every expiry, at full per-symbol price.

    Asserted against a real outgoing request rather than against ``as_params``,
    because restating the implementation is exactly what let the wrong parameter
    names through the first time.
    """
    route = respx.get(url__startswith=f"{VENDOR_CHAIN_URL}/SPY/").mock(
        return_value=httpx.Response(HTTP_CACHED, json=load("chain_SPY_live"))
    )
    async with httpx.AsyncClient() as http:
        await VendorClient(fast_config, http, "tok").chain("SPY", trade_date=SESSION)

    sent = route.calls.last.request.url.params
    dte_min = fast_config.require_int("tenor.entry_dte_min")
    dte_max = fast_config.require_int("tenor.entry_dte_max")
    assert sent["from"] == (SESSION + timedelta(days=dte_min)).isoformat()
    assert sent["to"] == (SESSION + timedelta(days=dte_max)).isoformat()
    assert sent["from"] != str(dte_min), "a raw DTE integer in a date parameter"
    assert sent["minOpenInterest"] == str(fast_config.require_int("liquidity.min_open_interest"))
    assert sent["maxBidAskSpreadPct"] == str(fast_config.require_float("liquidity.max_spread_pct"))


@respx.mock
async def test_session_volume_is_not_filtered_at_collection(fast_config):
    """``liquidity.require_session_volume`` is a SCREENING gate. Filtering it
    here would drop contracts the grid should still see and leave the store
    unable to answer why one failed."""
    assert fast_config.require_bool("liquidity.require_session_volume") is True
    route = respx.get(url__startswith=f"{VENDOR_CHAIN_URL}/SPY/").mock(
        return_value=httpx.Response(HTTP_CACHED, json=load("chain_SPY_live"))
    )
    async with httpx.AsyncClient() as http:
        await VendorClient(fast_config, http, "tok").chain("SPY")
    assert "minVolume" not in route.calls.last.request.url.params


@respx.mock
async def test_an_ignored_tenor_filter_is_detected_rather_than_paid_for_silently(
    fast_config, caplog
):
    """If the filter does not apply, the response carries expiries outside the
    window — the only observable difference between a cheap narrow pull and an
    expensive whole-chain one."""
    respx.get(url__startswith=f"{VENDOR_CHAIN_URL}/A/").mock(
        return_value=httpx.Response(HTTP_CACHED, json=load("chain_ragged"))
    )
    async with httpx.AsyncClient() as http:
        with caplog.at_level("WARNING"):
            chain = await VendorClient(fast_config, http, "tok").chain("A", trade_date=SESSION)
    # chain_ragged expires 2026-12-18, well inside 90-200 DTE of a far-future
    # session, so pin the check on the helper directly too.
    assert chain.outside_window((date(2027, 1, 1), date(2027, 6, 1))) == chain.contracts
    assert chain.outside_window((date(2026, 1, 1), date(2027, 1, 1))) == ()


# ── the client ──────────────────────────────────────────────────────────────


@respx.mock
async def test_http_203_from_the_caching_tier_is_success(fast_config):
    """The vendor answers from cache with 203. ``macro/fetchers.py`` raises on
    anything != 200, so a base class copied from there would reject good data."""
    respx.get(url__startswith=f"{VENDOR_CHAIN_URL}/SPY/").mock(
        return_value=httpx.Response(
            HTTP_CACHED,
            json=load("chain_SPY_live"),
            headers={"x-api-ratelimit-consumed": "3"},
        )
    )
    async with httpx.AsyncClient() as http:
        chain = await VendorClient(fast_config, http, "tok").chain("SPY")
    assert chain.status == "ok"
    assert chain.credits == 3


@respx.mock
async def test_a_missing_token_fails_the_target_and_names_the_variable(fast_config):
    """Checked at fetch time, not construction: a missing key must fail the one
    target that needed it, not stop a process with other collectors to run."""
    async with httpx.AsyncClient() as http:
        with pytest.raises(UpstreamError, match="MARKETDATA_API_TOKEN"):
            await VendorClient(fast_config, http, None).chain("SPY")


@respx.mock
async def test_a_historical_pull_names_the_session_it_wants(fast_config):
    """``date=`` is only sent for a historical pull, and it is an ET session."""
    route = respx.get(url__startswith=f"{VENDOR_CHAIN_URL}/SPY/").mock(
        return_value=httpx.Response(HTTP_CACHED, json=load("chain_SPY_live"))
    )
    async with httpx.AsyncClient() as http:
        client = VendorClient(fast_config, http, "tok")
        await client.chain("SPY", trade_date=date(2026, 3, 16))
        assert route.calls.last.request.url.params["date"] == "2026-03-16"
        await client.chain("SPY")
        assert "date" not in route.calls.last.request.url.params


@respx.mock
async def test_an_http_error_body_is_reported_not_swallowed(fast_config):
    respx.get(url__startswith=f"{VENDOR_CHAIN_URL}/SPY/").mock(
        return_value=httpx.Response(403, json=load("chain_blocked"))
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(UpstreamError, match="403"):
            await VendorClient(fast_config, http, "tok").chain("SPY")


# ── session dating ──────────────────────────────────────────────────────────


def test_the_session_is_resolved_in_new_york():
    """A collector running at 02:00 UTC asking for utc_today() asks for a date
    the New York session has not reached."""
    late_utc = datetime(2026, 8, 28, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert eastern_session(late_utc) == date(2026, 8, 27)


def test_the_backfill_walks_weekdays_newest_first():
    """Weekdays rather than an exchange calendar: a holiday answers no_data,
    which is recorded and never re-fetched, so the log corrects itself."""
    walked = trading_dates_back(date(2026, 8, 29), 5)  # a Saturday
    assert walked[0] == date(2026, 8, 28)
    assert all(d.weekday() < 5 for d in walked)
    assert walked == sorted(walked, reverse=True)


# ── the collector: what actually lands in the store ─────────────────────────


def _vendor_config(config, **overrides):
    """config.yaml with the vendor block overridden and backoff removed."""
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    data["option_vendor"].update(overrides)
    for key in ("base_delay_seconds", "max_delay_seconds", "jitter_seconds"):
        data["http"][key] = 0.0
    return Config(data)


async def _collect(
    store,
    cfg,
    *,
    sessions=1,
    only=None,
    today=SESSION,
    as_of=None,
    force=False,
    dry_run=False,
):
    from obliskiq.collect.option_vendor import VendorChainCollector
    from obliskiq.collect.runlog import CollectorRun

    async with httpx.AsyncClient() as http:
        collector = VendorChainCollector(store, cfg, VendorClient(cfg, http, "tok"))
        # dry_run is threaded into CollectorRun exactly as cli.py does it;
        # building the run object differently from production tests a
        # differently-configured object than the one that ships.
        with CollectorRun(store, "vendor-chain", dry_run=dry_run) as run:
            written = await collector.run(
                run,
                only=only,
                sessions=sessions,
                today=today,
                as_of=as_of,
                force=force,
                dry_run=dry_run,
            )
        return run, written, collector


def _mock_chain(body_name="chain_SPY_live", *, credits="1"):
    return respx.get(url__startswith=VENDOR_CHAIN_URL).mock(
        return_value=httpx.Response(
            HTTP_CACHED,
            json=load(body_name),
            headers={"x-api-ratelimit-consumed": credits},
        )
    )


def test_only_us_listings_are_routed_to_this_vendor(seeded_universe, config):
    """The three European listings stay on the IBKR route, which is the only one
    that reaches them. Split on the declared listing exchange, not on currency."""
    from obliskiq.collect.option_vendor import vendor_targets

    symbols = {t.symbol for t in vendor_targets(seeded_universe, config)}
    assert {"SPY", "QQQ", "FCX", "ALB"} <= symbols
    assert symbols.isdisjoint({"ASML", "SAN", "TTE"})


@respx.mock
async def test_the_rows_land_with_the_vendor_tag_and_the_occ_symbol(seeded_universe, config):
    _mock_chain()
    run, written, _ = await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    assert written == 3
    rows = seeded_universe.execute(
        "SELECT snapshot_date, expiry, strike, right, iv, source, option_symbol, contract_id "
        "FROM chain_snapshots ORDER BY strike"
    ).fetchall()
    assert len(rows) == 3
    assert {r["source"] for r in rows} == {SOURCE_MARKETDATA_APP}
    assert rows[0]["option_symbol"] == "SPY261231C00765000"
    # The vendor names contracts by OCC symbol; there is no IBKR conid to record,
    # and inventing one would put a checked identifier on a guess.
    assert all(r["contract_id"] is None for r in rows)
    assert rows[0]["snapshot_date"] == SESSION.isoformat()
    assert run.status() == "ok"


@respx.mock
async def test_the_underlying_snapshot_carries_the_derived_atm_vol(seeded_universe, config):
    """This row, not the chain, is the series the self-computed IV percentile is
    built from — one per underlying per day."""
    _mock_chain()
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    row = seeded_universe.execute(
        "SELECT last, implied_vol, source, implied_vol_source, market_data_status "
        "FROM underlying_snapshots"
    ).fetchone()
    assert row["last"] == 770.83
    assert row["implied_vol"] == 0.1466
    assert row["source"] == SOURCE_MARKETDATA_APP
    # The derivation gets its own column, mirroring chain_snapshots.sigma_source.
    # market_data_status holds IBKR's market-data state everywhere else, and
    # ROADMAP Phase 1 plans to surface that column across the grid as freshness —
    # a derivation note rendered as a freshness status would be a real confusion.
    assert row["implied_vol_source"] == "derived_2026-12-31_nearest_strike_one_right"
    assert row["market_data_status"] is None


@respx.mock
async def test_a_second_run_spends_no_credits_on_a_day_already_held(seeded_universe, config):
    """The whole point of the fetch log. A backfill restarted after an
    interruption must not re-buy the days it already has."""
    route = _mock_chain()
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    assert route.call_count == 1

    _, written, collector = await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    assert route.call_count == 1, "a held day was re-fetched"
    assert written == 0
    assert collector.credits_spent == 0

    # --force is the escape hatch, as macro_pull has.
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"], force=True)
    assert route.call_count == 2


@respx.mock
async def test_the_credit_ceiling_stops_the_run_cleanly_and_resumably(seeded_universe, config):
    """Stopping at the ceiling beats spending the day's allowance and then
    failing on every remaining session. What was collected stays collected."""
    route = _mock_chain(credits="2")
    cfg = _vendor_config(config, max_credits_per_run=2)
    run, written, collector = await _collect(seeded_universe, cfg, sessions=5, only=["SPY"])

    assert route.call_count == 1, "kept spending past the ceiling"
    assert collector.credits_spent == 2
    assert run.notes["credit_budget"]["ceiling"] == 2
    assert written == 3
    held = seeded_universe.execute("SELECT COUNT(*) FROM vendor_fetch_log").fetchone()[0]
    assert held == 1


@respx.mock
async def test_a_no_data_session_is_recorded_and_never_re_bought(seeded_universe, config):
    """A market holiday answers no_data. Recording it is what lets the weekday
    walk stand in for an exchange calendar at one credit-free call per holiday."""
    route = _mock_chain("chain_no_data")
    run, written, _ = await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    assert written == 0
    assert run.status() == "ok", "a holiday is not a failed target"
    row = seeded_universe.execute("SELECT status, contracts FROM vendor_fetch_log").fetchone()
    assert row["status"] == "no_data"
    assert row["contracts"] == 0

    await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    assert route.call_count == 1


@respx.mock
async def test_a_blocked_call_stays_retryable_and_is_remembered(seeded_universe, config):
    """The IP-change refusal is transient. Recording it as collected would leave
    a permanent hole that looks exactly like a holiday — so the fetch log keeps
    an error row and the day stays buyable.

    The BLOCK, though, is account-wide and outlives the process: it is written
    into collector_state so the next run does not rediscover it, and the run
    that met it reports degraded rather than ok.
    """
    respx.get(url__startswith=VENDOR_CHAIN_URL).mock(
        return_value=httpx.Response(403, json=load("chain_blocked"))
    )
    run, _, _ = await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    assert run.status() == "degraded"
    assert (
        seeded_universe.execute("SELECT status FROM vendor_fetch_log").fetchone()["status"]
        == "error"
    )
    assert active_cooldown(seeded_universe, UPSTREAM_MARKETDATA_APP) is not None

    # Once the cooldown lapses, the day is still there to be bought.
    set_state(seeded_universe, cooldown_key(UPSTREAM_MARKETDATA_APP), None)
    respx.get(url__startswith=VENDOR_CHAIN_URL).mock(
        return_value=httpx.Response(HTTP_CACHED, json=load("chain_SPY_live"))
    )
    _, written, _ = await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    assert written == 3, "a transient block was mistaken for a collected day"


@respx.mock
async def test_the_block_stops_the_run_instead_of_being_rediscovered_per_target(
    seeded_universe, config
):
    """It is account-wide, so the second target would meet the same refusal.

    Before this, every US target hit the 403 independently — each through a full
    target retry — for a condition the first call already settled.
    """
    route = respx.get(url__startswith=VENDOR_CHAIN_URL).mock(
        return_value=httpx.Response(403, json=load("chain_blocked"))
    )
    await _collect(seeded_universe, _vendor_config(config))
    assert route.call_count == 1, "the block was rediscovered rather than remembered"


@respx.mock
async def test_a_run_inside_the_cooldown_calls_nothing_and_reports_degraded(
    seeded_universe, config
):
    """The whole point is that it survives process exit.

    An in-memory breaker would never see two runs: the collectors are
    Type=oneshot and share nothing but this store. So the cooldown is written by
    one call and read by a separate one, against the same store.
    """
    set_cooldown(
        seeded_universe,
        UPSTREAM_MARKETDATA_APP,
        seconds=900,
        reason="Access denied. Only one device is permitted.",
    )
    route = respx.get(url__startswith=VENDOR_CHAIN_URL).mock(
        return_value=httpx.Response(HTTP_CACHED, json=load("chain_SPY_live"))
    )
    run, written, _ = await _collect(seeded_universe, _vendor_config(config), only=["SPY"])

    assert route.call_count == 0, "a cooled-down route was called anyway"
    assert written == 0
    assert run.status() == "degraded", "collecting nothing is never ok"


@respx.mock
async def test_a_lapsed_cooldown_does_not_silence_a_working_route(seeded_universe, config):
    """A stuck cooldown that never clears is worse than none: it would silence a
    healthy route forever, and the block announces itself again if still there."""
    set_cooldown(seeded_universe, UPSTREAM_MARKETDATA_APP, seconds=-1, reason="expired long ago")
    assert active_cooldown(seeded_universe, UPSTREAM_MARKETDATA_APP) is None

    _mock_chain()
    _, written, _ = await _collect(seeded_universe, _vendor_config(config), only=["SPY"])
    assert written == 3


@respx.mock
async def test_too_many_null_implied_vols_report_degraded(seeded_universe, config):
    """One of three contracts carries no iv. Written as NULL and counted, never
    solved here — solving needs a rate at the contract's own maturity and a
    dividend yield this layer does not hold."""
    _mock_chain("chain_SPY_both_rights")
    cfg = _vendor_config(config, max_null_iv_fraction=0.10)
    run, written, _ = await _collect(seeded_universe, cfg, only=["SPY"])
    assert run.status() == "degraded"
    assert run.degraded_targets == [f"SPY:{SESSION.isoformat()}"]
    # Degraded, not failed: the rows are still worth having, and the one that
    # carries no vol is written as NULL rather than dropped or invented.
    assert written == 3
    nulls = seeded_universe.execute(
        "SELECT COUNT(*) FROM chain_snapshots WHERE iv IS NULL"
    ).fetchone()[0]
    assert nulls == 1


@respx.mock
async def test_a_dry_run_writes_nothing_but_still_spends(seeded_universe, config):
    """It writes nothing — and it is NOT free. The call is still made and still
    billed, so --dry-run checks the request shape, not the cost. ROADMAP says so
    too; a reader who thinks otherwise burns credits proving a query."""
    route = _mock_chain(credits="7")
    _, _, collector = await _collect(
        seeded_universe, _vendor_config(config), only=["SPY"], dry_run=True
    )
    assert seeded_universe.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] == 0
    assert seeded_universe.execute("SELECT COUNT(*) FROM vendor_fetch_log").fetchone()[0] == 0
    assert route.call_count == 1
    assert collector.credits_spent == 7


@respx.mock
async def test_a_symbol_this_route_does_not_serve_is_refused_not_silently_skipped(
    seeded_universe, config
):
    from obliskiq.errors import NoMatchingTargets

    _mock_chain()
    with pytest.raises(NoMatchingTargets, match="ASML"):
        await _collect(seeded_universe, _vendor_config(config), only=["ASML"])


# ── the fixes with no coverage before ───────────────────────────────────────


@respx.mock
async def test_a_missing_credit_header_is_charged_not_treated_as_free(seeded_universe, config):
    """A budget guard that switches itself off when its input goes missing is
    worse than no guard. Recording an unreadable header as zero let a 252-session
    backfill run to completion against a ceiling that never tripped."""
    respx.get(url__startswith=VENDOR_CHAIN_URL).mock(
        return_value=httpx.Response(HTTP_CACHED, json=load("chain_SPY_live"))  # no header
    )
    cfg = _vendor_config(config, assumed_credits_when_unreported=25)
    _, _, collector = await _collect(seeded_universe, cfg, only=["SPY"])
    assert collector.credits_spent == 25


@respx.mock
async def test_spend_is_taken_from_the_balance_drop_when_that_is_larger(seeded_universe, config):
    """``request_with_retry`` retries inside itself and returns only the final
    response, so summing the per-response CONSUMED header cannot see the
    attempts before it. The drop in the account's REMAINING balance does — here
    the vendor reports 1 credit consumed while the balance fell by 5, and the
    ceiling must believe the larger figure."""
    respx.get(url__startswith=VENDOR_CHAIN_URL).side_effect = [
        httpx.Response(
            HTTP_CACHED,
            json=load("chain_SPY_live"),
            headers={"x-api-ratelimit-consumed": "1", "x-api-ratelimit-remaining": "1000"},
        ),
        httpx.Response(
            HTTP_CACHED,
            json=load("chain_SPY_live"),
            headers={"x-api-ratelimit-consumed": "1", "x-api-ratelimit-remaining": "995"},
        ),
    ]
    cfg = _vendor_config(config)
    _, _, collector = await _collect(seeded_universe, cfg, only=["SPY"], sessions=2, force=True)
    assert collector.credits_spent == 5, "the balance drop was ignored"


@respx.mock
async def test_a_write_failure_records_the_fetch_and_does_not_abort_the_run(
    seeded_universe, config, monkeypatch
):
    """The credit is already spent by the time the write runs, so the spend has
    to be recorded and the run has to carry on — every remaining target being
    skipped without a verdict is its own failure.

    What the log records changed in PR 3. It used to write the fetch as 'ok'
    BEFORE the write, so a failing write could not cause a re-buy. That traded
    the wrong way: the gap between the two commits is killable, and
    _already_held never re-buys a settled past session logged 'ok', so a kill in
    that window bought a day, wrote nothing, and suppressed every future attempt
    at it. The pair is now one transaction, and a failure records an 'error' —
    which keeps the cost and stays retryable. See docs/DECISIONS.md."""
    from obliskiq.collect.option_vendor import VendorChainCollector

    _mock_chain()
    monkeypatch.setattr(
        VendorChainCollector,
        "_write",
        lambda self, target, chain: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    run, written, _ = await _collect(seeded_universe, _vendor_config(config), only=["SPY"])

    assert written == 0
    assert run.status() == "error", "a write failure must be recorded, not swallowed"
    row = seeded_universe.execute("SELECT status, credits FROM vendor_fetch_log").fetchone()
    assert row is not None, "the spend was not recorded at all"
    assert row["credits"] == 1, "what the call cost is kept even though it failed"
    assert row["status"] == "error", "retryable, because the rows this bought are not in the store"
    assert seeded_universe.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] == 0


@respx.mock
async def test_the_route_can_be_switched_off_without_failing_the_nightly_run(
    seeded_universe, config
):
    """`enabled: false` is a decision somebody took, and the night is healthy.

    The daily sequence runs this unconditionally, so an off-switch that failed
    twelve targets — each after a full retry backoff — for a condition known
    before the first call would be worse than the thing it guards. This one
    raises NoMatchingTargets, which the sequence records as SKIPPED at exit 0.
    """
    from obliskiq.errors import NoMatchingTargets

    _mock_chain()
    with pytest.raises(NoMatchingTargets, match="enabled is false"):
        await _collect(seeded_universe, _vendor_config(config, enabled=False), only=["SPY"])


@respx.mock
async def test_an_enabled_route_with_no_token_degrades_rather_than_skipping(
    seeded_universe, config
):
    """`enabled: true` with no token is the machine unable to do what it is
    scheduled to do, and it must not read like the line above.

    deploy/collect.env.example ships MARKETDATA_API_TOKEN empty while config.yaml
    ships option_vendor.enabled true, so this is what a FRESH INSTALL does: zero
    US option chains collected, every night. As a skip it exited 0 and the
    scheduler reported success.
    """
    from obliskiq.collect.option_vendor import VendorChainCollector
    from obliskiq.collect.runlog import CollectorRun, RunStatus

    cfg = _vendor_config(config)
    async with httpx.AsyncClient() as http:
        collector = VendorChainCollector(seeded_universe, cfg, VendorClient(cfg, http, None))
        with CollectorRun(seeded_universe, "vendor-chain") as run:
            written = await collector.run(run, only=["SPY"])

    assert written == 0
    assert run.status() == RunStatus.DEGRADED
    assert run.degraded_targets == ["no_token"], "one finding about the route, not one per target"
    assert "MARKETDATA_API_TOKEN" in run.outcomes[0].reason


@respx.mock
async def test_an_untokened_route_still_costs_no_retries_and_no_credits(seeded_universe, config):
    """The property the deliberate conflation was protecting, kept.

    Twelve targets each failing after a full retry backoff, nightly, for a
    condition known before the first call is what the old skip was avoiding. A
    louder version of the same failure would have reintroduced it; this asserts
    it did not, by counting what reached the wire.
    """
    from obliskiq.collect.option_vendor import VendorChainCollector
    from obliskiq.collect.runlog import CollectorRun

    route = _mock_chain()
    cfg = _vendor_config(config)
    async with httpx.AsyncClient() as http:
        collector = VendorChainCollector(seeded_universe, cfg, VendorClient(cfg, http, None))
        with CollectorRun(seeded_universe, "vendor-chain") as run:
            await collector.run(run, only=["SPY"])

    assert route.call_count == 0, "nothing was attempted"
    assert collector.credits_spent == 0
    assert run.targets_failed == 0, "a route that cannot start has no failed targets to report"


@respx.mock
async def test_a_settled_past_session_is_never_re_bought(seeded_universe, config):
    """A past session's end-of-day chain will not change, so it is held forever.
    Only the CURRENT session gets the refetch window, because a pull before the
    close is a mid-session partial that must not freeze as the day's EOD row."""
    past = SESSION - timedelta(days=30)
    route = _mock_chain()
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"], today=past, sessions=1)
    assert route.call_count == 1
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"], today=past, sessions=1)
    assert route.call_count == 1, "a settled session was re-bought"


@respx.mock
async def test_the_credit_ceiling_marks_the_run_degraded_not_ok(seeded_universe, config):
    """A backfill that collected one of five sessions and stopped is not healthy,
    and exiting 0 tells the timer that it was."""
    _mock_chain(credits="2")
    cfg = _vendor_config(config, max_credits_per_run=2)
    run, _, _ = await _collect(seeded_universe, cfg, sessions=5, only=["SPY"])
    assert run.status() == "degraded"


# ── as_of moves the walk; today stays the tier reference ────────────────────
#
# These two were one parameter until a live probe needed them apart. The end of
# the walk was always the current session, so the newest thing collected was
# always bought on the LIVE tier -- which bills per option symbol -- and buying
# one past session meant paying for today first. That is also why the question
# docs/NEXT-SESSIONS.md opens the backfill with, whether a HISTORICAL chain
# carries iv and greeks, had no command that could ask it.


@respx.mock
async def test_a_pinned_past_session_is_requested_as_history(seeded_universe, config):
    """``as_of`` before the current session sends ``date=``: the historical tier."""
    past = SESSION - timedelta(days=10)
    route = _mock_chain()
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"], today=SESSION, as_of=past)
    sent = route.calls.last.request.url.params
    assert sent["date"] == past.isoformat()


@respx.mock
async def test_the_current_session_is_still_requested_live(seeded_universe, config):
    """The daily run is unchanged: no ``date=``, so the vendor serves today."""
    route = _mock_chain()
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"], today=SESSION)
    assert "date" not in route.calls.last.request.url.params


@respx.mock
async def test_a_pinned_session_is_stamped_with_the_day_it_was_bought_for(seeded_universe, config):
    """The row carries the session requested, not the session the clock is on.

    The failure this guards is the one that made splitting the parameters
    necessary: an undated request answered with the CURRENT chain while the row
    was stamped with the past date, so the store held today's quotes filed under
    a day they never described.
    """
    past = SESSION - timedelta(days=10)
    _mock_chain()
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"], today=SESSION, as_of=past)
    stamped = {
        row[0]
        for row in seeded_universe.connection.execute(
            "select distinct snapshot_date from chain_snapshots"
        )
    }
    assert stamped == {past.isoformat()}


@respx.mock
async def test_a_session_after_the_current_one_is_refused_before_a_call_is_made(
    seeded_universe, config
):
    """Nothing is spent finding this out."""
    route = _mock_chain()
    with pytest.raises(ObliskError, match="is after the current session"):
        await _collect(
            seeded_universe,
            _vendor_config(config),
            only=["SPY"],
            today=SESSION,
            as_of=SESSION + timedelta(days=1),
        )
    assert route.call_count == 0


@respx.mock
async def test_as_of_defaults_to_the_current_session(seeded_universe, config):
    """Omitting it must leave the daily run exactly as it was."""
    route = _mock_chain()
    await _collect(seeded_universe, _vendor_config(config), only=["SPY"], today=SESSION, as_of=None)
    assert "date" not in route.calls.last.request.url.params
    assert route.call_count == 1
