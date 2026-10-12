"""What can go wrong talking to IBKR, and what each failure allows the caller to do next.

Every failure the client raises is an :class:`IbkrError` whose class carries a
:class:`RetryAdvice`: never (the answer cannot change), re-arm (re-open the brokerage session once
and repeat), re-authenticate (the transport must log in again) or back off (transient; wait and
repeat, honouring ``Retry-After``). :func:`classify` turns one HTTP response into one of them, and
decides by the BODY where IBKR's status is ambiguous:

* ``HTTP 500 {"error":"Please query /accounts first"}`` is a dropped brokerage session, not a
  server fault [CP ✓ 2026-09-10]; so is a bare ``401`` (lite, DECISIONS "A 401 is repaired, a 403
  is not").
* ``HTTP 500`` with ``Chart data unavailable`` or ``Contract details are not available`` is a final
  "no such data" wearing a retryable status [CP ✓ 2026-09-10]; every other 500 stays transient.
* ``HTTP 429 {"error":"Exceed number of conids"}`` is a malformed request, not pacing
  [CP ✓ 2026-09-10]; retrying it can only repeat it.
* ``403`` is an entitlement answer: re-opening the session cannot change it.

Messages quote at most :data:`ERROR_EXCERPT_CHARS` of a body, with account ids masked.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import ClassVar

from ..errors import ObliskError, UpstreamError
from .redact import redact

#: How much of an error body to quote: enough to identify it, not enough to paste a payload.
ERROR_EXCERPT_CHARS = 300

HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500

#: The body IBKR returns when the /iserver/accounts precondition is missing or lost.
ACCOUNTS_PRECONDITION = "please query /accounts first"

#: Bodies the route uses to say "no such data", under HTTP 500. Observed, not documented
#: (docs/knowledge/ibkr.md owns the readings; a new one is recorded there before it is added).
NO_DATA_BODIES = ("chart data unavailable", "contract details are not available")

#: Bodies of a 429 that is a request IBKR will never accept, not a pacing refusal.
REJECTED_429_BODIES = ("exceed number of conids",)


class RetryAdvice(StrEnum):
    """What may follow a failure."""

    NEVER = "never"
    REARM = "rearm"
    REAUTHENTICATE = "reauthenticate"
    BACKOFF = "backoff"


class IbkrError(UpstreamError):
    """A failure talking to IBKR. ``retry`` says what may follow it."""

    retry: ClassVar[RetryAdvice] = RetryAdvice.NEVER

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class AuthError(IbkrError):
    """Credentials or entitlements refused (``403``, a failed OAuth handshake, a failed login)."""


class SessionExpiredError(IbkrError):
    """The brokerage session dropped (``401``, the ``/accounts`` precondition lost)."""

    retry = RetryAdvice.REARM


class CompetingSessionError(IbkrError):
    """Another connection holds this username's brokerage session."""

    retry = RetryAdvice.REAUTHENTICATE


class RateLimitedError(IbkrError):
    """IBKR refused the request for pacing (``429``)."""

    retry = RetryAdvice.BACKOFF


class NoDataPermanentError(IbkrError):
    """A final "there is no such data", decided by the body."""


class IbkrUpstreamError(IbkrError):
    """A transient fault: a 5xx that is not a no-data answer, or the transport failing."""

    retry = RetryAdvice.BACKOFF


class RequestRejectedError(IbkrError):
    """IBKR refused the request as malformed or unknown (other 4xx); repeating it cannot help."""


class DecodeError(IbkrError):
    """A response that is not the shape this client reads (non-JSON, wrong type)."""


class CircuitOpenError(IbkrError):
    """An endpoint group failed repeatedly; requests to it are refused until its pause ends."""

    retry = RetryAdvice.BACKOFF


class GatewayError(IbkrError):
    """The Client Portal Gateway cannot be installed, started or logged in."""


class AccountGuardError(ObliskError):
    """The account in use is not the account this process may use; nothing starts."""


class ProfileRefusedError(ObliskError):
    """The transport refuses the profile (the Gateway never logs in a live account)."""


class AmbiguousContractError(ObliskError):
    """A contract could not be resolved to exactly one listing; nothing is guessed."""


def excerpt(body: str, known_ids: Iterable[str] = ()) -> str:
    """The start of ``body``, with account ids masked."""
    return redact(body[:ERROR_EXCERPT_CHARS], known_ids)


def classify(
    status: int,
    body: str,
    *,
    endpoint: str,
    retry_after: float | None = None,
    known_ids: Iterable[str] = (),
) -> IbkrError | None:
    """The error a response stands for, or ``None`` for a success.

    ``endpoint`` names the call in the message; pass a path template, never a path carrying an
    account id (it is redacted regardless).
    """
    if status < HTTP_BAD_REQUEST:
        return None
    lowered = body.lower()
    where = redact(endpoint, known_ids)
    quoted = excerpt(body, known_ids)
    if ACCOUNTS_PRECONDITION in lowered:
        return SessionExpiredError(
            f"{where}: the brokerage session lost the /accounts precondition (HTTP {status})",
            status=status,
        )
    if status == HTTP_UNAUTHORIZED:
        return SessionExpiredError(
            f"{where}: HTTP 401; the brokerage session is not authenticated", status=status
        )
    if status == HTTP_FORBIDDEN:
        return AuthError(
            f"{where}: HTTP 403, an entitlement refusal; re-opening the session cannot change it: "
            f"{quoted}",
            status=status,
        )
    if status == HTTP_TOO_MANY_REQUESTS:
        if any(marker in lowered for marker in REJECTED_429_BODIES):
            return RequestRejectedError(
                f"{where}: HTTP 429 for a request IBKR never accepts: {quoted}", status=status
            )
        return RateLimitedError(
            f"{where}: HTTP 429, paced by IBKR: {quoted}", status=status, retry_after=retry_after
        )
    if status >= HTTP_SERVER_ERROR:
        if any(marker in lowered for marker in NO_DATA_BODIES):
            return NoDataPermanentError(
                f"{where}: HTTP {status} carrying a final no-data answer: {quoted}", status=status
            )
        return IbkrUpstreamError(f"{where}: HTTP {status}: {quoted}", status=status)
    return RequestRejectedError(f"{where}: HTTP {status}: {quoted}", status=status)
