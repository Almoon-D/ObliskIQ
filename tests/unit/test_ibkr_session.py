"""The brokerage session: opening, re-arming, re-authenticating, the keepalive and alerts.

Ported from ObliskIQ-lite-MCP's ``tests/test_cp_client.py`` (the session lifecycle and error
handling halves), adapted to :class:`~obliskiq.ibkr.session.BrokerSession`. HTTP is mocked with
respx; time is the fake clock, so backoff, Retry-After and the keepalive interval cost nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from obliskiq.ibkr.errors import (
    AuthError,
    DecodeError,
    IbkrError,
    IbkrUpstreamError,
    NoDataPermanentError,
    RateLimitedError,
    RequestRejectedError,
    SessionExpiredError,
)
from obliskiq.ibkr.session import (
    ISERVER_ACCOUNTS_PATH,
    PORTFOLIO_ACCOUNTS_PATH,
    SSODH_INIT_PATH,
    TICKLE_PATH,
    AuthStatus,
    BrokerSession,
    SessionAlert,
    SessionAlertKind,
)
from obliskiq.util.http import RetryPolicy

BASE = "https://gw.test/v1/api"
API_PREFIX = "/v1/api/"
SNAPSHOT = "iserver/marketdata/snapshot"
HISTORY = "iserver/marketdata/history"

#: A placeholder paper account id; not shaped like a real one.
ACCOUNT = "DUTEST1"

AUTHENTICATED = {"authenticated": True, "competing": False, "connected": True}

type Handler = Callable[[httpx.Request], httpx.Response]


def mocked() -> respx.MockRouter:
    """A respx router for the gateway; routes a test declares need not all be hit."""
    return respx.mock(base_url=BASE, assert_all_called=False)


def rel(request: httpx.Request) -> str:
    """The request's endpoint path, without the API prefix."""
    return request.url.path.removeprefix(API_PREFIX)


def wire(mock: respx.MockRouter, handler: Handler) -> respx.Route:
    """Route every request under the base URL to ``handler``."""
    return mock.route().mock(side_effect=handler)


def tickle_body(**auth: Any) -> dict[str, Any]:
    return {"session": "abc", "iserver": {"authStatus": auth}}


def with_session(ibkr: Any, **values: Any) -> Any:
    return ibkr.model_copy(update={"session": ibkr.session.model_copy(update=values)})


@pytest.fixture
def alerts() -> list[SessionAlert]:
    return []


@pytest.fixture
def session(
    make_session: Callable[..., BrokerSession], alerts: list[SessionAlert]
) -> BrokerSession:
    return make_session(alert=alerts.append)


# ── opening the brokerage session ──


async def test_open_posts_ssodh_init_with_publish_and_compete_then_arms_iserver_accounts(
    session, fake_transport
):
    """ssodh/init is the replacement for a human logging into a gateway, and it is followed by
    /iserver/accounts: measured in lite [CP ✓ 2026-09-10], a snapshot after an authenticated
    init still answered ``HTTP 500 {"error":"Please query /accounts first"}`` without it."""
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, rel(request), dict(request.url.params)))
        if rel(request) == ISERVER_ACCOUNTS_PATH:
            return httpx.Response(200, json={"accounts": [ACCOUNT], "selectedAccount": ACCOUNT})
        return httpx.Response(200, json=AUTHENTICATED)

    assert session.authenticated is False
    with mocked() as mock:
        wire(mock, handler)
        status = await session.open()

    assert fake_transport.prepared == 1
    assert status is not None
    assert status.authenticated is True
    assert session.authenticated is True
    assert seen[0] == ("POST", SSODH_INIT_PATH, {"publish": "true", "compete": "true"})
    # The arming call follows the init: it is an /iserver call and needs the session open.
    assert seen[1][:2] == ("GET", ISERVER_ACCOUNTS_PATH)
    assert len(seen) == 2


async def test_open_sends_compete_false_when_configured(make_session, shipped_settings):
    ibkr = with_session(shipped_settings.ibkr, compete=False)
    session = make_session(ibkr=ibkr)
    with mocked() as mock:
        init = mock.post(SSODH_INIT_PATH).respond(200, json=AUTHENTICATED)
        mock.get(ISERVER_ACCOUNTS_PATH).respond(200, json={"accounts": []})
        await session.open()
    assert dict(init.calls.last.request.url.params) == {"publish": "true", "compete": "false"}


async def test_open_without_ssodh_on_connect_prepares_the_transport_and_calls_nothing(
    make_session, shipped_settings, fake_transport
):
    session = make_session(ibkr=with_session(shipped_settings.ibkr, ssodh_init_on_connect=False))
    with mocked() as mock:
        route = mock.route().respond(200, json=AUTHENTICATED)
        assert await session.open() is None
    assert route.call_count == 0
    assert fake_transport.prepared == 1


async def test_the_accounts_ids_learnt_on_open_are_masked_from_then_on(session):
    with mocked() as mock:
        mock.post(SSODH_INIT_PATH).respond(200, json=AUTHENTICATED)
        mock.get(ISERVER_ACCOUNTS_PATH).respond(200, json={"accounts": [ACCOUNT]})
        await session.open()
    assert ACCOUNT not in session.redact(f"portfolio/{ACCOUNT}/ledger")


async def test_an_unauthenticated_init_does_not_claim_a_session(session):
    with mocked() as mock:
        mock.post(SSODH_INIT_PATH).respond(200, json={"authenticated": False})
        accounts = mock.get(ISERVER_ACCOUNTS_PATH).respond(200, json={"accounts": []})
        status = await session.init_brokerage_session()
    assert status.authenticated is False
    assert session.authenticated is False
    assert accounts.call_count == 0, "an unauthenticated session must not be armed"


async def test_an_init_response_without_the_field_is_treated_as_closed(session):
    """Absent is not authenticated; assuming otherwise turns every later call into a 401."""
    with mocked() as mock:
        mock.post(SSODH_INIT_PATH).respond(200, json={})
        status = await session.init_brokerage_session()
    assert status.authenticated is False
    assert session.authenticated is False


async def test_an_init_reporting_a_competing_session_alerts(session, alerts):
    with mocked() as mock:
        mock.post(SSODH_INIT_PATH).respond(200, json={"authenticated": True, "competing": True})
        mock.get(ISERVER_ACCOUNTS_PATH).respond(200, json={"accounts": []})
        await session.init_brokerage_session()
    assert [alert.kind for alert in alerts] == [SessionAlertKind.COMPETING]


def test_auth_status_reads_a_missing_flag_as_false():
    status = AuthStatus.from_body({"connected": True})
    assert status.authenticated is False
    assert status.competing is False
    assert AuthStatus.from_body(["not", "a", "mapping"]).authenticated is False


# ── re-arming a dropped session ──


async def test_the_accounts_precondition_rearms_ssodh_first_then_both_accounts_and_repeats(
    session,
):
    """``HTTP 500 {"error":"Please query /accounts first"}`` is a dropped session, not a fault.
    Measured in lite: the accounts endpoints alone cleared nothing across nine attempts, while
    ssodh/init restored the session at once, so the repair is ssodh/init FIRST."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = rel(request)
        seen.append(path)
        if path == SSODH_INIT_PATH:
            return httpx.Response(200, json=AUTHENTICATED)
        if path == ISERVER_ACCOUNTS_PATH:
            return httpx.Response(200, json={"accounts": [ACCOUNT]})
        if path == PORTFOLIO_ACCOUNTS_PATH:
            return httpx.Response(200, json=[{"id": ACCOUNT}])
        if SSODH_INIT_PATH not in seen:
            return httpx.Response(500, json={"error": "Please query /accounts first"})
        return httpx.Response(200, json=[{"conid": 1, "31": "10.0"}])

    with mocked() as mock:
        wire(mock, handler)
        body = await session.request("GET", SNAPSHOT, params={"conids": "1"})

    assert body == [{"conid": 1, "31": "10.0"}]
    assert seen == [
        SNAPSHOT,
        SSODH_INIT_PATH,
        ISERVER_ACCOUNTS_PATH,
        PORTFOLIO_ACCOUNTS_PATH,
        SNAPSHOT,
    ]
    assert session.authenticated is True


async def test_the_accounts_precondition_is_repaired_not_mistaken_for_a_no_data_500(session):
    """The two body-matched 500 rules are neighbours: one says give up, the other repair."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(rel(request))
        if rel(request) == SNAPSHOT and SSODH_INIT_PATH not in seen:
            return httpx.Response(500, json={"error": "Please query /accounts first"})
        if rel(request) == SNAPSHOT:
            return httpx.Response(200, json=[{"conid": 1}])
        return httpx.Response(200, json=AUTHENTICATED)

    with mocked() as mock:
        wire(mock, handler)
        assert await session.request("GET", SNAPSHOT) == [{"conid": 1}]


async def test_a_bare_401_rearms_the_session_and_repeats_the_call(session, fake_transport):
    """Measured in lite: a 401 on the snapshot 31 minutes into a run, with ~22 h left on the
    token, was a dropped /iserver session that ssodh/init repairs."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = rel(request)
        seen.append(path)
        if path == SNAPSHOT:
            if SSODH_INIT_PATH not in seen:
                return httpx.Response(401, json={"error": "not authenticated"})
            return httpx.Response(200, json=[{"conid": 1, "31": "10.0"}])
        return httpx.Response(200, json=AUTHENTICATED)

    with mocked() as mock:
        wire(mock, handler)
        body = await session.request("GET", SNAPSHOT, params={"conids": "1"})

    assert body == [{"conid": 1, "31": "10.0"}]
    assert seen.index(SSODH_INIT_PATH) < len(seen) - 1
    assert seen.count(SNAPSHOT) == 2
    assert fake_transport.recovered == 0, "a re-arm that worked needs no new login"


async def test_a_401_surviving_the_rearm_reauthenticates_the_transport_then_raises(
    session, fake_transport, alerts
):
    """One re-arm, one re-authentication, then the error: a credential that is really wrong
    must fail rather than re-open the session forever."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(rel(request))
        if rel(request) == SNAPSHOT:
            return httpx.Response(401, json={"error": "no"})
        return httpx.Response(200, json=AUTHENTICATED)

    with mocked() as mock:
        wire(mock, handler)
        with pytest.raises(SessionExpiredError, match="401"):
            await session.request("GET", SNAPSHOT, params={"conids": "1"})

    assert fake_transport.recovered == 1
    assert seen.count(SNAPSHOT) == 3, "the original, the repeat after re-arm, after re-login"
    assert [alert.kind for alert in alerts] == [SessionAlertKind.REAUTHENTICATED]


async def test_a_401_that_survives_a_failed_reauthentication_raises_and_alerts(
    session, fake_transport, alerts, shipped_settings
):
    with mocked() as mock:
        mock.route().respond(401, json={"error": "no"})
        with pytest.raises(SessionExpiredError, match="re-authentication failed"):
            await session.request("GET", SNAPSHOT)
    assert fake_transport.recovered == shipped_settings.ibkr.session.reauthenticate_attempts
    assert [alert.kind for alert in alerts] == [SessionAlertKind.REAUTHENTICATION_FAILED]


async def test_a_403_is_never_rearmed_because_it_is_not_a_dropped_session(session, fake_transport):
    """403 is an entitlement answer, and re-opening the session cannot change it."""
    with mocked() as mock:
        route = mock.route().respond(403, json={"error": "forbidden"})
        with pytest.raises(AuthError, match="403"):
            await session.request("GET", SNAPSHOT, params={"conids": "1"})
    assert route.call_count == 1
    assert all(rel(call.request) == SNAPSHOT for call in route.calls)
    assert fake_transport.recovered == 0


# ── errors decided by the body, and backoff ──


@pytest.mark.parametrize("body", ["Chart data unavailable", "Contract details are not available"])
async def test_a_500_saying_there_is_no_data_costs_one_call(session, body):
    """Measured in lite: history answers HTTP 500 with these bodies for an instrument it holds
    no bars for; retrying spent three attempts being told the same thing."""
    with mocked() as mock:
        route = mock.route().respond(500, json={"error": body})
        with pytest.raises(NoDataPermanentError, match="no-data"):
            await session.request("GET", HISTORY)
    assert route.call_count == 1


async def test_an_ordinary_500_is_retried_per_the_http_policy_then_raises(
    session, shipped_settings, fake_clock
):
    """The status alone must not decide it: reading every 500 as final turns an outage into a
    target abandoned after one try."""
    retries = shipped_settings.http.retries
    with mocked() as mock:
        route = mock.route().respond(500, json={"error": "boom"})
        with pytest.raises(IbkrUpstreamError, match="HTTP 500") as caught:
            await session.request("GET", PORTFOLIO_ACCOUNTS_PATH)
    assert not isinstance(caught.value, NoDataPermanentError)
    assert route.call_count == retries + 1
    assert len([s for s in fake_clock.slept if s > 0]) >= retries


async def test_a_429_with_retry_after_pauses_for_the_asked_wait_then_succeeds(session, fake_clock):
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "too many"}),
        httpx.Response(200, json=[{"conid": 1}]),
    ]
    with mocked() as mock:
        route = mock.route().mock(side_effect=responses)
        assert await session.request("GET", SNAPSHOT) == [{"conid": 1}]
    assert route.call_count == 2
    assert 7.0 in fake_clock.slept


async def test_a_429_without_retry_after_backs_off_and_raises_rate_limited(session):
    with mocked() as mock:
        mock.route().respond(429, json={"error": "too many"})
        with pytest.raises(RateLimitedError):
            await session.request("GET", SNAPSHOT)


async def test_a_429_exceed_number_of_conids_is_a_rejected_request_and_not_retried(session):
    """Measured in lite: 150 conids answered ``HTTP 429 {"error":"Exceed number of conids"}``;
    retrying a malformed request can only repeat it."""
    with mocked() as mock:
        route = mock.route().respond(429, json={"error": "Exceed number of conids"})
        with pytest.raises(RequestRejectedError, match="never accepts"):
            await session.request("GET", SNAPSHOT)
    assert route.call_count == 1


async def test_a_transport_failure_is_retried_then_raised_as_upstream(session, shipped_settings):
    with mocked() as mock:
        route = mock.route().mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(IbkrUpstreamError, match="transport layer"):
            await session.request("GET", SNAPSHOT)
    assert route.call_count == shipped_settings.http.retries + 1


async def test_a_non_json_body_is_a_decode_error_not_a_silent_none(session):
    with mocked() as mock:
        mock.route().respond(200, text="<html>maintenance</html>")
        with pytest.raises(DecodeError, match="not JSON"):
            await session.request("GET", PORTFOLIO_ACCOUNTS_PATH)


async def test_an_empty_body_decodes_to_none_rather_than_raising(session):
    with mocked() as mock:
        mock.route().respond(200, content=b"")
        assert await session.get(TICKLE_PATH) is None


async def test_none_parameters_are_left_off_the_query_string(session):
    with mocked() as mock:
        route = mock.route().respond(200, json={})
        await session.request("GET", HISTORY, params={"conid": 1, "exchange": None})
    assert dict(route.calls.last.request.url.params) == {"conid": "1"}


async def test_a_request_before_open_without_a_client_is_refused(
    shipped_settings, fake_transport, fake_clock
):
    bare = BrokerSession(
        fake_transport,
        shipped_settings.ibkr,
        RetryPolicy.from_settings(shipped_settings.http),
        clock=fake_clock,
    )
    with pytest.raises(IbkrUpstreamError, match="before open"):
        await bare.request("GET", TICKLE_PATH)


async def test_auth_status_is_posted_not_got(session):
    """CP's /iserver/auth/status is a POST, unlike most read endpoints."""
    with mocked() as mock:
        route = mock.route().respond(200, json={"authenticated": True, "connected": True})
        status = await session.auth_status()
    assert route.calls.last.request.method == "POST"
    assert rel(route.calls.last.request) == "iserver/auth/status"
    assert status.authenticated is True
    assert status.connected is True


async def test_tickle_is_posted_and_returns_its_body(session):
    with mocked() as mock:
        route = mock.route().respond(200, json=tickle_body())
        assert await session.tickle() == tickle_body()
    assert route.call_count == 1
    assert route.calls.last.request.method == "POST"
    assert rel(route.calls.last.request) == TICKLE_PATH


# ── the keepalive ──


async def _run_until(predicate: Callable[[], bool], *, spins: int = 5_000) -> None:
    for _ in range(spins):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was never reached")


async def test_keepalive_ticks_on_the_configured_interval(
    make_session, shipped_settings, fake_clock
):
    """The interval is ibkr.session.tickle_interval_seconds; time is the fake clock, so the
    test asserts behaviour, not duration."""
    interval = 30.0
    session = make_session(
        ibkr=with_session(shipped_settings.ibkr, tickle_interval_seconds=interval)
    )
    tickles: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert rel(request) == TICKLE_PATH
        tickles.append(fake_clock.slept.count(interval))
        return httpx.Response(200, json=tickle_body(authenticated=True))

    with mocked() as mock:
        wire(mock, handler)
        await session.start_keepalive()
        await _run_until(lambda: len(tickles) >= 3)
        await session.stop_keepalive()

    # Each tickle is preceded by one more interval wait than the last.
    assert tickles[:3] == [1, 2, 3]


async def test_starting_keepalive_twice_starts_one_loop(session):
    with mocked() as mock:
        mock.route().respond(200, json=tickle_body())
        await session.start_keepalive()
        first = session._tickle_task
        await session.start_keepalive()
        assert session._tickle_task is first
        await session.stop_keepalive()
    assert session._tickle_task is None


async def test_stopping_a_keepalive_that_never_started_is_a_no_op(session):
    await session.stop_keepalive()
    assert session._tickle_task is None


async def test_aclose_stops_the_keepalive_and_closes_the_transport(session, fake_transport):
    with mocked() as mock:
        mock.route().respond(200, json=tickle_body())
        await session.start_keepalive()
        await session.aclose()
    assert session._tickle_task is None
    assert fake_transport.closed == 1


async def test_a_run_of_failing_tickles_reauthenticates_only_after_the_grace(
    make_session, shipped_settings, fake_clock, fake_transport
):
    """A run of failures must rebuild the session, and not before the configured grace."""
    ibkr = shipped_settings.ibkr
    interval = ibkr.session.tickle_interval_seconds
    grace = ibkr.session.tickle_failure_grace
    session = make_session()
    inits_after_ticks: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if rel(request) == TICKLE_PATH:
            return httpx.Response(503, text="gone")
        if rel(request) == SSODH_INIT_PATH:
            inits_after_ticks.append(fake_clock.slept.count(interval))
            return httpx.Response(200, json=AUTHENTICATED)
        return httpx.Response(200, json={"accounts": []})

    with mocked() as mock:
        wire(mock, handler)
        await session.start_keepalive()
        await _run_until(lambda: bool(inits_after_ticks))
        await session.stop_keepalive()

    assert inits_after_ticks[0] >= grace, "re-authenticated before the configured grace"
    assert fake_transport.recovered >= 1


async def test_a_tickle_reporting_competing_alerts_and_reauthenticates(
    session, alerts, fake_transport
):
    def handler(request: httpx.Request) -> httpx.Response:
        if rel(request) == TICKLE_PATH:
            return httpx.Response(200, json=tickle_body(authenticated=True, competing=True))
        if rel(request) == SSODH_INIT_PATH:
            return httpx.Response(200, json=AUTHENTICATED)
        return httpx.Response(200, json={"accounts": []})

    with mocked() as mock:
        wire(mock, handler)
        await session.check_session()

    assert fake_transport.recovered == 1
    assert [alert.kind for alert in alerts] == [
        SessionAlertKind.COMPETING,
        SessionAlertKind.REAUTHENTICATED,
    ]
    assert session.authenticated is True


async def test_a_tickle_reporting_unauthenticated_after_a_session_is_the_daily_reset(
    session, alerts, fake_transport
):
    """IBKR's daily reset (or a logout, or a timeout) shows as authenticated=false on tickle."""
    tickled: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if rel(request) == TICKLE_PATH:
            tickled.append(1)
            return httpx.Response(200, json=tickle_body(authenticated=False, competing=False))
        if rel(request) == SSODH_INIT_PATH:
            return httpx.Response(200, json=AUTHENTICATED)
        return httpx.Response(200, json={"accounts": []})

    with mocked() as mock:
        wire(mock, handler)
        await session.init_brokerage_session()
        assert session.authenticated is True
        await session.check_session()

    assert tickled == [1]
    assert fake_transport.recovered == 1
    assert [alert.kind for alert in alerts] == [
        SessionAlertKind.SESSION_LOST,
        SessionAlertKind.REAUTHENTICATED,
    ]


async def test_an_unauthenticated_tickle_before_any_session_is_not_a_loss(
    session, alerts, fake_transport
):
    with mocked() as mock:
        mock.route().respond(200, json=tickle_body(authenticated=False))
        await session.check_session()
    assert alerts == []
    assert fake_transport.recovered == 0


async def test_a_tickle_without_an_auth_status_changes_nothing(session, alerts, fake_transport):
    with mocked() as mock:
        mock.route().respond(200, json={"session": "abc"})
        await session.check_session()
    assert alerts == []
    assert fake_transport.recovered == 0


async def test_reauthentication_failing_every_attempt_alerts_and_raises(
    session, alerts, fake_transport, fake_clock, shipped_settings
):
    config = shipped_settings.ibkr.session
    with mocked() as mock:
        mock.route().respond(200, json={"authenticated": False})
        with pytest.raises(SessionExpiredError, match="re-authentication failed"):
            await session.reauthenticate("the brokerage session was lost")

    assert fake_transport.recovered == config.reauthenticate_attempts
    assert fake_clock.slept.count(config.reauthenticate_pause_seconds) == (
        config.reauthenticate_attempts - 1
    )
    assert [alert.kind for alert in alerts] == [SessionAlertKind.REAUTHENTICATION_FAILED]
    assert session.authenticated is False


async def test_reauthentication_with_a_transport_that_cannot_log_in_alerts_and_raises(
    session, alerts, fake_transport, shipped_settings
):
    fake_transport.fail_recover = AuthError("login refused")
    with mocked() as mock:
        route = mock.route().respond(200, json=AUTHENTICATED)
        with pytest.raises(SessionExpiredError, match="login refused"):
            await session.reauthenticate("a competing session took the brokerage session")
    assert route.call_count == 0
    assert fake_transport.recovered == shipped_settings.ibkr.session.reauthenticate_attempts
    assert [alert.kind for alert in alerts] == [SessionAlertKind.REAUTHENTICATION_FAILED]


async def test_an_alert_hook_that_raises_does_not_break_the_session(make_session, fake_transport):
    def broken_hook(alert: SessionAlert) -> None:
        raise RuntimeError("the notifier is down")

    session = make_session(alert=broken_hook)

    def handler(request: httpx.Request) -> httpx.Response:
        if rel(request) == TICKLE_PATH:
            return httpx.Response(200, json=tickle_body(authenticated=True, competing=True))
        if rel(request) == SSODH_INIT_PATH:
            return httpx.Response(200, json=AUTHENTICATED)
        return httpx.Response(200, json={"accounts": []})

    with mocked() as mock:
        wire(mock, handler)
        await session.check_session()

    assert fake_transport.recovered == 1
    assert session.authenticated is True


# ── account ids never reach a message ──


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(403, json={"error": f"{ACCOUNT} is not entitled"}), AuthError),
        (httpx.Response(404, json={"error": f"no account {ACCOUNT}"}), RequestRejectedError),
        (httpx.Response(500, json={"error": f"{ACCOUNT} failed"}), IbkrUpstreamError),
        (httpx.Response(200, text=f"<html>{ACCOUNT}</html>"), DecodeError),
    ],
    ids=["403", "404", "500", "non-json"],
)
async def test_account_ids_never_appear_in_error_messages(make_session, response, error):
    session = make_session(known_ids=[ACCOUNT])
    with mocked() as mock:
        mock.route().mock(return_value=response)
        with pytest.raises(error) as caught:
            await session.request("GET", f"portfolio/{ACCOUNT}/ledger")
    assert ACCOUNT not in str(caught.value)
    assert "portfolio/" in str(caught.value)


async def test_account_ids_never_appear_in_a_transport_failure_message(make_session):
    session = make_session(known_ids=[ACCOUNT])
    with mocked() as mock:
        mock.route().mock(side_effect=httpx.ConnectError(f"cannot reach {ACCOUNT}"))
        with pytest.raises(IbkrUpstreamError) as caught:
            await session.request("GET", f"portfolio/{ACCOUNT}/ledger")
    assert ACCOUNT not in str(caught.value)


async def test_account_ids_never_appear_in_a_reauthentication_alert(
    make_session, alerts, fake_transport
):
    session = make_session(known_ids=[ACCOUNT], alert=alerts.append)
    with mocked() as mock:
        mock.route().respond(401, json={"error": f"{ACCOUNT} not authenticated"})
        with pytest.raises(IbkrError) as caught:
            await session.request("GET", f"portfolio/{ACCOUNT}/positions/0")
    assert ACCOUNT not in str(caught.value)
    assert alerts
    assert all(ACCOUNT not in alert.detail for alert in alerts)


@respx.mock
async def test_a_competing_session_that_cannot_be_reclaimed_raises_competing(
    make_session, fake_transport
):
    from obliskiq.ibkr.errors import CompetingSessionError

    respx.post("https://gw.test/v1/api/tickle").mock(
        return_value=httpx.Response(
            200, json={"iserver": {"authStatus": {"authenticated": True, "competing": True}}}
        )
    )
    respx.post("https://gw.test/v1/api/iserver/auth/ssodh/init").mock(
        return_value=httpx.Response(200, json={"authenticated": False})
    )
    session = make_session()
    with pytest.raises(CompetingSessionError):
        await session.check_session()
    assert fake_transport.recovered >= 1
