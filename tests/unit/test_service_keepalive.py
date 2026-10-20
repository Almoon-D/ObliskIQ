"""The keepalive at IBKR's daily reset, as the service runs it (DECISIONS D68).

A session lost at the reset is logged in again unattended and the owner hears it did; a failed
re-login reaches the owner within one heartbeat; a repair the Gateway defers (its re-login
interval) leaves the session lost until a deadline, failing reads at once without repeating the
alert, and the keepalive logs in again once the deadline passes.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from service_rig import RecordingNotifier

from obliskiq.config import ObliskConfig
from obliskiq.ibkr.errors import GatewayError, RecoveryDeferredError, SessionExpiredError
from obliskiq.ibkr.session import BrokerSession, SessionAlertKind
from obliskiq.orchestration.assemble import session_state
from obliskiq.orchestration.service import AlertHooks
from obliskiq.reporting.alerts import AlertBook
from obliskiq.reporting.reporter import Reporter
from obliskiq.util.timeutil import utc_now

BASE = "https://gw.test/v1/api"
LOST = {"session": "abc", "iserver": {"authStatus": {"authenticated": False}}}
ALIVE = {"session": "abc", "iserver": {"authStatus": {"authenticated": True}}}


class Gateway:
    """The Gateway's answers: a tickle that reports the session lost or alive, a login."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.alive = True
        self.tickles = 0
        self.reads = 0
        router.post(f"{BASE}/tickle").mock(side_effect=self._tickle)
        router.post(f"{BASE}/iserver/auth/ssodh/init").mock(side_effect=self._init)
        router.get(f"{BASE}/iserver/accounts").mock(
            return_value=httpx.Response(200, json={"accounts": ["DUTEST1"]})
        )
        router.get(f"{BASE}/portfolio/accounts").mock(side_effect=self._read)

    def _tickle(self, request: httpx.Request) -> httpx.Response:
        self.tickles += 1
        return httpx.Response(200, json=ALIVE if self.alive else LOST)

    def _init(self, request: httpx.Request) -> httpx.Response:
        self.alive = True
        return httpx.Response(200, json={"authenticated": True, "competing": False})

    def _read(self, request: httpx.Request) -> httpx.Response:
        self.reads += 1
        return httpx.Response(200, json=[])


@pytest.fixture
def channel(tmp_path: Any) -> tuple[Reporter, RecordingNotifier, AlertHooks]:
    notifier = RecordingNotifier()
    reporter = Reporter(notifier, AlertBook(tmp_path / "alerts.json"))
    return reporter, notifier, AlertHooks(reporter, now=utc_now)


async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


async def test_a_session_lost_at_the_daily_reset_is_logged_in_again(
    make_session: Callable[..., BrokerSession], channel: Any, fake_transport: Any
) -> None:
    reporter, notifier, hooks = channel
    session = make_session(alert=hooks.session)
    with respx.mock(assert_all_called=False) as router:
        gateway = Gateway(router)
        await session.open()
        gateway.alive = False  # IBKR's daily reset
        await session.check_session()
        await hooks.drain()
    assert fake_transport.recovered == 1
    assert session.authenticated
    assert session_state(session) == "authenticated"
    assert [s for s in notifier.subjects() if "session" in s] == [
        "ObliskIQ alert: session.session_lost",
        "ObliskIQ alert: session.reauthenticated",
    ]
    # The re-login cleared the lost session's condition: a later loss is heard again.
    assert reporter.book.active == {}


async def test_a_failed_relogin_reaches_the_owner_within_one_heartbeat(
    make_session: Callable[..., BrokerSession],
    channel: Any,
    fake_transport: Any,
    fake_clock: Any,
    shipped_settings: ObliskConfig,
) -> None:
    _, notifier, hooks = channel
    session = make_session(alert=hooks.session)
    fake_transport.fail_recover = GatewayError("the Gateway's headless login failed")
    with respx.mock(assert_all_called=False) as router:
        gateway = Gateway(router)
        await session.open()
        started = fake_clock.now
        gateway.alive = False
        await session.start_keepalive()
        await _until(lambda: bool(notifier.alerts("session.reauthentication_failed")))
        elapsed = fake_clock.now - started
        await session.stop_keepalive()
        await hooks.drain()
    assert elapsed <= shipped_settings.runtime.heartbeat_seconds
    (failed,) = notifier.alerts("session.reauthentication_failed")
    assert failed.startswith("🚨")
    assert "headless login failed" in failed
    assert not session.authenticated
    assert session_state(session) == "lost (not authenticated)"


def test_a_heartbeat_shorter_than_the_keepalives_slowest_alert_is_refused(
    shipped_settings: ObliskConfig,
) -> None:
    runtime = shipped_settings.runtime.model_copy(update={"heartbeat_seconds": 60})
    data = shipped_settings.model_dump(mode="json")
    data["runtime"] = runtime.model_dump(mode="json")
    with pytest.raises(ValueError, match="slowest path to a failed re-login"):
        ObliskConfig.model_validate(data)


async def test_a_deferred_repair_fails_reads_fast_until_its_deadline_then_logs_in(
    make_session: Callable[..., BrokerSession],
    channel: Any,
    fake_transport: Any,
    fake_clock: Any,
) -> None:
    _, notifier, hooks = channel
    session = make_session(alert=hooks.session)
    fake_transport.fail_recover = RecoveryDeferredError(
        "the next login is allowed in 420 s", retry_in=420
    )
    with respx.mock(assert_all_called=False) as router:
        gateway = Gateway(router)
        await session.open()
        gateway.alive = False
        with pytest.raises(SessionExpiredError, match="allowed in 420 s"):
            await session.check_session()  # lost; the login is deferred
        assert session.lost_for == 420
        assert session_state(session) == "lost; the next login is allowed in 420 s"
        for _ in range(3):  # reads fail at once, with no repair and no new alert
            with pytest.raises(SessionExpiredError, match="allowed in 420 s"):
                await session.request("GET", "portfolio/accounts")
        tickles = gateway.tickles
        await session.check_session()  # before the deadline the keepalive sends nothing
        assert gateway.tickles == tickles
        assert fake_transport.recovered == 1
        fake_clock.now += 421
        fake_transport.fail_recover = None
        await session.check_session()  # the deadline passed: logged in again
        await session.request("GET", "portfolio/accounts")
        await hooks.drain()
    assert gateway.reads == 1
    assert session.lost_for is None
    assert session.authenticated
    # One alert for the loss, one for the failed login, one for the login: none per read.
    assert [s for s in notifier.subjects() if "session" in s] == [
        f"ObliskIQ alert: session.{SessionAlertKind.SESSION_LOST.value}",
        f"ObliskIQ alert: session.{SessionAlertKind.REAUTHENTICATION_FAILED.value}",
        f"ObliskIQ alert: session.{SessionAlertKind.REAUTHENTICATED.value}",
    ]
