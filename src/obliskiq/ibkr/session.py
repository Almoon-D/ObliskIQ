"""The brokerage session above either transport: one request pipeline and its repairs.

Every request goes rate limiter → transport headers → HTTP → :func:`~obliskiq.ibkr.errors.classify`,
and each failure is handled by the advice its class carries:

* **back off** (429, other 5xx, a transport failure): jittered exponential backoff from the
  ``http`` policy, ``Retry-After`` honoured and applied to the whole endpoint group; each failure
  counts toward the group's circuit breaker.
* **re-arm** (401, or ``HTTP 500 {"error":"Please query /accounts first"}``): the brokerage session
  dropped. Re-open it — ``/iserver/auth/ssodh/init`` FIRST, then both accounts endpoints — and
  repeat the call once. Measured in lite [CP ✓ 2026-09-10]: calling the accounts endpoints alone
  cleared nothing across nine attempts, while ``ssodh/init`` restored the session at once; a bare
  401 31 minutes into a run with ~22 h left on the token was the same drop. If the repeat fails
  too, the transport logs in again (renegotiate the token, or re-run the Gateway login) and the
  call is repeated once more; then it raises.
* **never** (403, a no-data body, a malformed request, an undecodable body, a transport that
  could not sign the request after its own retries): raised at once. The last also counts
  toward the group's circuit breaker.

``/tickle`` keeps the session alive on a configured interval and reads its ``competing`` and
``authenticated`` flags. A competing session (another connection took the username) and a session
that stopped being authenticated (IBKR's daily reset, a logout, a timeout) are re-established
automatically, and each is reported through the ``alert`` hook.

A repair the transport defers (the Gateway's re-login interval has not passed) leaves the session
**lost until a deadline**: requests fail at once, with no repair and no new alert, until it passes;
then the keepalive logs in again (one attempt, alerted either way). A lost session never repeats
its alert on every failing read.

Account ids never appear in a log line or message: paths are redacted before they are named.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import contextlib
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx

from ..config.schema_ibkr import IbkrConfig
from ..errors import UpstreamError
from ..logging_setup import get_logger
from ..util.http import RetryPolicy, retry_after_seconds
from .errors import (
    CompetingSessionError,
    DecodeError,
    IbkrError,
    IbkrUpstreamError,
    RecoveryDeferredError,
    RetryAdvice,
    SessionExpiredError,
    TransportUnavailableError,
    classify,
)
from .ratelimit import Clock, RateLimiter, SystemClock
from .redact import redact
from .transport.base import Transport, TransportKind

log = get_logger("ibkr.session")

#: Set while this task is repairing a session: its own requests must not start another repair.
#: A context variable, not a flag on the session, so a concurrent request in another task still
#: waits for the repair and retries instead of failing at once.
_REPAIRING: ContextVar[bool] = ContextVar("ibkr_session_repairing", default=False)

SSODH_INIT_PATH = "iserver/auth/ssodh/init"
AUTH_STATUS_PATH = "iserver/auth/status"
TICKLE_PATH = "tickle"
ISERVER_ACCOUNTS_PATH = "iserver/accounts"
PORTFOLIO_ACCOUNTS_PATH = "portfolio/accounts"


class SessionAlertKind(StrEnum):
    COMPETING = "competing_session"
    SESSION_LOST = "session_lost"
    REAUTHENTICATED = "reauthenticated"
    REAUTHENTICATION_FAILED = "reauthentication_failed"


@dataclass(frozen=True)
class SessionAlert:
    """Something the owner should hear about the broker session."""

    kind: SessionAlertKind
    detail: str
    at: datetime


type AlertHook = Callable[[SessionAlert], None]


@dataclass(frozen=True)
class AuthStatus:
    """``/iserver/auth/status`` (also nested in ``/tickle``). A missing flag reads as false."""

    authenticated: bool
    competing: bool
    connected: bool
    message: str
    fail: str

    @classmethod
    def from_body(cls, body: Any) -> AuthStatus:
        data = body if isinstance(body, Mapping) else {}
        return cls(
            authenticated=data.get("authenticated") is True,
            competing=data.get("competing") is True,
            connected=data.get("connected") is True,
            message=str(data.get("message") or ""),
            fail=str(data.get("fail") or ""),
        )


def tickle_status(body: Any) -> AuthStatus | None:
    """The auth status a ``/tickle`` answer carries, or ``None`` when it carries none."""
    if not isinstance(body, Mapping):
        return None
    iserver = body.get("iserver")
    if not isinstance(iserver, Mapping) or not isinstance(iserver.get("authStatus"), Mapping):
        return None
    return AuthStatus.from_body(iserver["authStatus"])


class BrokerSession:
    """One brokerage session: the request pipeline, its repairs and the keepalive."""

    def __init__(
        self,
        transport: Transport,
        ibkr: IbkrConfig,
        policy: RetryPolicy,
        *,
        limiter: RateLimiter | None = None,
        clock: Clock | None = None,
        http: httpx.AsyncClient | None = None,
        alert: AlertHook | None = None,
        known_ids: Iterable[str] = (),
    ) -> None:
        self._transport = transport
        self._ibkr = ibkr
        self._policy = policy
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._limiter = limiter if limiter is not None else RateLimiter(ibkr, self._clock)
        self._http = http
        self._owns_http = http is None
        self._alert = alert
        self._known_ids = frozenset(i for i in known_ids if i)
        # One repair at a time; the generation counts finished repairs, so a request that saw a
        # failure before another task's repair finished retries instead of repairing again.
        self._repair_lock = asyncio.Lock()
        self._repairs = 0
        self._authenticated = False
        # When the transport deferred a repair: the monotonic instant the next login is allowed.
        self._deferred_until: float | None = None
        self._tickle_task: asyncio.Task[None] | None = None

    # ── lifecycle ──

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def lost_for(self) -> float | None:
        """Seconds until a deferred repair may run (the session is lost meanwhile), or ``None``."""
        if self._deferred_until is None:
            return None
        return max(0.0, self._deferred_until - self._clock.monotonic())

    def add_known_ids(self, ids: Iterable[str]) -> None:
        """Account ids learnt from IBKR, masked in every message from now on."""
        self._known_ids = self._known_ids | frozenset(i for i in ids if i)

    def redact(self, text: str) -> str:
        return redact(text, self._known_ids)

    async def open(self) -> AuthStatus | None:
        """Prepare the transport, open the HTTP client and, when configured, the session."""
        await self._transport.prepare()
        if self._http is None:
            self._http = httpx.AsyncClient(
                verify=self._transport.verify(),
                timeout=self._ibkr.request_timeout_seconds,
                # The Gateway is on this machine behind a pinned certificate: a proxy from the
                # environment must not carry those calls. OAuth goes to IBKR and may need one.
                trust_env=self._transport.kind is not TransportKind.GATEWAY,
            )
        if self._ibkr.session.ssodh_init_on_connect:
            return await self.init_brokerage_session()
        return None

    async def aclose(self) -> None:
        await self.stop_keepalive()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
        await self._transport.aclose()

    # ── the request pipeline ──

    async def _send(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any],
        json_body: Any,
        label: str,
    ) -> tuple[httpx.Response, str]:
        if self._http is None:
            raise IbkrUpstreamError("the broker session is used before open()")
        url = f"{self._transport.base_url}/{path.lstrip('/')}"
        # The transport readies itself (an OAuth token exchange) before the request holds a
        # pacing slot and a concurrency permit, so a slow exchange blocks no other request.
        try:
            await self._transport.refresh()
        except UpstreamError as exc:
            raise self._transport_failure(exc, self._limiter.group_for(path), label) from exc
        async with self._limiter.slot(path) as group:
            try:
                headers = await self._transport.headers(method, url, query)
            except UpstreamError as exc:
                raise self._transport_failure(exc, group, label) from exc
            try:
                response = await self._http.request(
                    method, url, params=dict(query) or None, json=json_body, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._limiter.failure(group)
                raise IbkrUpstreamError(
                    f"{self.redact(label)}: the request failed at the transport layer "
                    f"({type(exc).__name__})"
                ) from exc
        return response, group

    def _transport_failure(self, exc: UpstreamError, group: str, label: str) -> IbkrError:
        """What a transport failure becomes. A refusal (a failed token signature check) stays
        as it is. A transport that could not reach its own upstream has retried already: it is
        counted once against the group and not retried again by the request's backoff."""
        if isinstance(exc, IbkrError) and not isinstance(exc, IbkrUpstreamError):
            return exc
        self._limiter.failure(group)
        return TransportUnavailableError(
            f"{self.redact(label)}: the transport could not sign the request ({type(exc).__name__})"
        )

    def _decode(self, response: httpx.Response, label: str) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise DecodeError(
                f"{self.redact(label)} returned a body that is not JSON",
                status=response.status_code,
            ) from exc

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        label: str | None = None,
        retry: bool = True,
    ) -> Any:
        """Issue one request and return its decoded JSON body (``None`` for an empty body).

        ``label`` names the call in logs and errors; pass a template such as
        ``portfolio/{account}/ledger`` for any path that carries an account id.

        ``retry=False`` sends the request at most once and raises its first failure, with no
        backoff, re-arm or re-authentication: a write that may have reached the broker is never
        repeated behind the caller's back (the order path looks its cOID up instead).
        """
        name = label if label is not None else path
        wait = self.lost_for
        if wait is not None and wait > 0 and not _REPAIRING.get():
            raise SessionExpiredError(
                f"{self.redact(name)}: the brokerage session is lost; the next login is allowed "
                f"in {wait:.0f} s"
            )
        query = {key: value for key, value in (params or {}).items() if value is not None}
        attempt = 0
        rearmed = False
        reauthenticated = False
        while True:
            generation = self._repairs
            try:
                response, group = await self._send(method, path, query, json_body, name)
            except IbkrUpstreamError as exc:
                error: IbkrError = exc
                group = ""
            else:
                wait = retry_after_seconds(response, self._policy)
                found = classify(
                    response.status_code,
                    response.text,
                    endpoint=name,
                    retry_after=wait,
                    known_ids=self._known_ids,
                )
                if found is None:
                    self._limiter.success(group)
                    return self._decode(response, name)
                error = found
                if error.retry is RetryAdvice.BACKOFF:
                    self._limiter.failure(group)
                    if error.retry_after is not None:
                        self._limiter.pause(group, error.retry_after)
            if not retry:
                raise error
            if error.retry is RetryAdvice.REARM and not _REPAIRING.get():
                if not rearmed:
                    rearmed = True
                    await self.rearm(after=generation)
                    continue
                if not reauthenticated:
                    reauthenticated = True
                    await self.reauthenticate(
                        f"{self.redact(name)} kept failing: {error}", after=generation
                    )
                    continue
            if error.retry is RetryAdvice.BACKOFF and attempt < self._policy.retries:
                delay = error.retry_after or self._policy.delay_for(attempt)
                log.warning(
                    "ibkr request failed; backing off",
                    extra={"endpoint": self.redact(name), "attempt": attempt + 1, "delay": delay},
                )
                attempt += 1
                await self._clock.sleep(delay)
                continue
            raise error

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    # ── the brokerage session ──

    async def init_brokerage_session(self) -> AuthStatus:
        """``POST /iserver/auth/ssodh/init``, then the ``/iserver/accounts`` precondition.

        ``publish`` and ``compete`` go as query parameters, the form lite ran live
        [CP ✓ 2026-09-10]; ``compete`` takes the session over from another connection.
        """
        body = await self.request(
            "POST",
            SSODH_INIT_PATH,
            params={
                "publish": "true",
                "compete": "true" if self._ibkr.session.compete else "false",
            },
        )
        status = AuthStatus.from_body(body)
        self._authenticated = status.authenticated
        log.info(
            "brokerage session init",
            extra={"authenticated": status.authenticated, "competing": status.competing},
        )
        if status.competing:
            self._emit(SessionAlertKind.COMPETING, "ssodh/init reported a competing session")
        if status.authenticated:
            await self._arm_accounts()
        return status

    async def _arm_accounts(self) -> None:
        """``/iserver/accounts``, which the ``/iserver`` market-data routes will not serve
        without. A failure is logged, not raised: the call that follows reports it precisely."""
        try:
            body = await self.request("GET", ISERVER_ACCOUNTS_PATH)
        except IbkrError as exc:
            log.warning("iserver/accounts failed", extra={"error": self.redact(str(exc))})
            return
        if isinstance(body, Mapping):
            self.add_known_ids(str(a) for a in body.get("accounts") or [] if a)

    async def rearm(self, *, after: int | None = None) -> None:
        """Re-open the session after it dropped: ``ssodh/init``, then both accounts endpoints.

        Failures are swallowed: the caller repeats the real request, whose own error says far
        more. One repair runs at a time; ``after`` is the repair count the caller saw before its
        request failed, and a repair finished since then makes this one unnecessary.
        """
        async with self._repair_lock:
            if after is not None and self._repairs != after:
                return
            token = _REPAIRING.set(True)
            try:
                await self._rearm_steps()
            except TransportUnavailableError as exc:
                # Every further step would wait on the same unreachable transport.
                log.warning("re-arm stopped", extra={"error": self.redact(str(exc))})
            finally:
                _REPAIRING.reset(token)
                self._repairs += 1

    async def _rearm_steps(self) -> None:
        """``ssodh/init``, then both accounts endpoints. A step's own failure is swallowed;
        a transport that cannot sign requests stops the sequence."""
        try:
            body = await self.request(
                "POST",
                SSODH_INIT_PATH,
                params={
                    "publish": "true",
                    "compete": "true" if self._ibkr.session.compete else "false",
                },
            )
            self._authenticated = AuthStatus.from_body(body).authenticated
        except TransportUnavailableError:
            raise
        except IbkrError:
            pass
        for path in (ISERVER_ACCOUNTS_PATH, PORTFOLIO_ACCOUNTS_PATH):
            try:
                await self.request("GET", path)
            except TransportUnavailableError:
                raise
            except IbkrError:
                continue
        log.info("re-armed the brokerage session", extra={"authenticated": self._authenticated})

    async def reauthenticate(self, reason: str, *, after: int | None = None) -> AuthStatus:
        """Log the transport in again and re-open the session; alert either way.

        ``after`` as for :meth:`rearm`: a repair finished since the caller's failure is taken as
        the answer, and the caller retries.
        """
        async with self._repair_lock:
            if after is not None and self._repairs != after:
                # Repaired by a concurrent request meanwhile: report the state it left.
                return AuthStatus.from_body({"authenticated": self._authenticated})
            token = _REPAIRING.set(True)
            try:
                return await self._reauthenticate(reason)
            finally:
                _REPAIRING.reset(token)
                self._repairs += 1

    async def _reauthenticate(self, reason: str) -> AuthStatus:
        session = self._ibkr.session
        last: Exception | None = None
        for attempt in range(1, session.reauthenticate_attempts + 1):
            try:
                await self._transport.recover()
                status = await self.init_brokerage_session()
            except Exception as exc:
                # A transport fault that is not an IbkrError (a browser that will not launch, a
                # bug) must still count, alert and leave the session marked unauthenticated.
                last = exc
                if isinstance(exc, RecoveryDeferredError):
                    break  # the transport said when it will try; sooner attempts are wasted
            else:
                if status.authenticated:
                    self._deferred_until = None
                    self._emit(SessionAlertKind.REAUTHENTICATED, reason)
                    return status
                last = SessionExpiredError("ssodh/init did not report an authenticated session")
            log.warning(
                "re-authentication attempt failed",
                extra={"attempt": attempt, "error": self.redact(str(last))},
            )
            if attempt < session.reauthenticate_attempts:
                await self._clock.sleep(session.reauthenticate_pause_seconds)
        self._authenticated = False
        if isinstance(last, RecoveryDeferredError) and last.retry_in is not None:
            self._deferred_until = self._clock.monotonic() + last.retry_in
        detail = f"{reason}; re-authentication failed: {self.redact(str(last))}"
        self._emit(SessionAlertKind.REAUTHENTICATION_FAILED, detail)
        raise SessionExpiredError(detail) from last

    async def auth_status(self) -> AuthStatus:
        return AuthStatus.from_body(await self.request("POST", AUTH_STATUS_PATH))

    async def tickle(self) -> Any:
        return await self.request("POST", TICKLE_PATH)

    def _emit(self, kind: SessionAlertKind, detail: str) -> None:
        alert = SessionAlert(kind=kind, detail=self.redact(detail), at=datetime.now(UTC))
        log.warning("broker session alert", extra={"kind": kind.value, "detail": alert.detail})
        if self._alert is None:
            return
        # Alerts fire inside a repair; work the hook starts is not part of it and may repair.
        token = _REPAIRING.set(False)
        try:
            self._alert(alert)
        except Exception:
            log.exception("session alert hook failed")
        finally:
            _REPAIRING.reset(token)

    # ── keepalive ──

    async def check_session(self) -> None:
        """One keepalive step: tickle, then repair a competing or lost session. While a repair is
        deferred nothing is sent; once its deadline passes the session is logged in again."""
        wait = self.lost_for
        if wait is not None:
            if wait > 0:
                return
            await self.reauthenticate("the deferred re-login is due")
            return
        status = tickle_status(await self.tickle())
        if status is None:
            return
        if status.competing:
            self._emit(SessionAlertKind.COMPETING, "tickle reported a competing session")
            try:
                await self.reauthenticate("a competing session took the brokerage session")
            except SessionExpiredError as exc:
                raise CompetingSessionError(
                    "another connection holds the brokerage session and it could not be reclaimed"
                ) from exc
            return
        if self._authenticated and not status.authenticated:
            self._emit(
                SessionAlertKind.SESSION_LOST,
                "tickle reported the session unauthenticated (a daily reset, a logout or a "
                "timeout)",
            )
            await self.reauthenticate("the brokerage session was lost")

    async def start_keepalive(self) -> None:
        if self._tickle_task is not None:
            return
        self._tickle_task = asyncio.create_task(self._keepalive_loop())

    async def stop_keepalive(self) -> None:
        task, self._tickle_task = self._tickle_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _keepalive_loop(self) -> None:
        session = self._ibkr.session
        failures = 0
        while True:
            await self._clock.sleep(session.tickle_interval_seconds)
            try:
                await self.check_session()
                failures = 0
            except Exception as exc:  # the keepalive must outlive any one failure
                failures += 1
                log.warning(
                    "keepalive failed",
                    extra={
                        "consecutive_failures": failures,
                        "error_type": type(exc).__name__,
                        "error": self.redact(str(exc)),
                    },
                )
                if failures >= session.tickle_failure_grace:
                    failures = 0
                    # reauthenticate alerts on failure itself; the loop goes on either way.
                    with contextlib.suppress(Exception):
                        await self.reauthenticate("the keepalive kept failing")


__all__ = ["AlertHook", "AuthStatus", "BrokerSession", "SessionAlert", "SessionAlertKind"]
