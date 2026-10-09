"""Run the OAuth handshake one step at a time, and say which step failed.

Why this exists
---------------

The live-session-token flow was written from IBKR's published OAuth 1.0a
specification in a sandbox with no credentials, which made it the most
consequential untested code in this repository. **It is not untested now**: this
command was the first thing run when the credentials arrived and it passed all
six steps on the first attempt ``[CP ✓ 2026-09-10]`` — the step table is in
``references/ibkr-playbook.md``. What it exists for did not change, and is why it
is still shipped: a bad credential otherwise surfaces as a bare ``401`` from
whatever ran first, and a ``401`` says nothing about which of six steps was wrong.

So this walks the handshake deliberately: load, decrypt, exchange, verify, open,
tickle. Each step reports on its own, and a failure names the artefact to check
rather than the HTTP status. It writes nothing and reads no market data; it is
safe to run repeatedly.

**The signature check is never skipped.** On OpenSSL builds implementing implicit
rejection for PKCS#1 v1.5, decrypting with the wrong encryption key returns
indistinguishable random bytes instead of raising. The derived token is then a
well-formed 20-byte HMAC that simply is not IBKR's, and every later call fails
authentication for no visible reason. Verifying against
``live_session_token_signature`` is what turns that into a diagnosable failure
here, at the handshake, instead of a mystery at the first ``/iserver`` call.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..config import Config
from ..env import Settings
from ..errors import ERROR_EXCERPT_CHARS
from ..logging_setup import get_logger
from .cp_client import CpApiClient
from .oauth import OAuthSession
from .session import LIVE_SESSION_TOKEN_PATH, credentials_from_settings

log = get_logger("ibkr.preflight")

#: Step names, in the order they run. A failure at step N says nothing about N+1,
#: so the walk stops at the first one that fails.
STEP_CREDENTIALS = "credentials"
STEP_DECRYPT = "decrypt-access-token-secret"
STEP_EXCHANGE = "diffie-hellman-exchange"
STEP_VERIFY = "live-session-token-signature"
STEP_BROKERAGE = "brokerage-session"
STEP_TICKLE = "tickle"

STEPS = (
    STEP_CREDENTIALS,
    STEP_DECRYPT,
    STEP_EXCHANGE,
    STEP_VERIFY,
    STEP_BROKERAGE,
    STEP_TICKLE,
)


@dataclass
class StepResult:
    """One step of the handshake."""

    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return f"  [{mark}] {self.name}{': ' + self.detail if self.detail else ''}"


@dataclass
class PreflightReport:
    """What happened, in order, and where it stopped."""

    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.ok for step in self.steps)

    @property
    def failed_step(self) -> StepResult | None:
        return next((step for step in self.steps if not step.ok), None)

    def add(self, name: str, ok: bool, detail: str = "") -> StepResult:
        result = StepResult(name=name, ok=ok, detail=detail)
        self.steps.append(result)
        return result

    def render(self) -> str:
        lines = ["OAuth preflight against the IBKR Client Portal Web API:", ""]
        lines.extend(step.line() for step in self.steps)
        skipped = [name for name in STEPS if name not in {s.name for s in self.steps}]
        lines.extend(f"  [ -  ] {name} (not reached)" for name in skipped)
        lines.append("")
        failed = self.failed_step
        if failed is None:
            lines.append("Every step passed. The collectors can run on this route.")
        else:
            lines.append(f"Stopped at: {failed.name}")
            lines.append(_remedy_for(failed.name))
        return "\n".join(lines)


def _remedy_for(step: str) -> str:
    """What to check, per step. The point of walking the handshake at all."""
    return {
        STEP_CREDENTIALS: (
            "  Set the three values IBKR issued and the three key PATHS, in\n"
            "  /etc/obliskiq/collect.env. See docs/DEPLOY.md section 7b."
        ),
        STEP_DECRYPT: (
            "  OBLISK_IBKR_ACCESS_TOKEN_SECRET must be the base64 ciphertext IBKR\n"
            "  issued, and OBLISK_IBKR_ENCRYPTION_KEY the private half of the\n"
            "  ENCRYPTION key you uploaded — not the signing key. Swapping the two\n"
            "  on IBKR's form is the classic cause and it fails exactly here."
        ),
        STEP_EXCHANGE: (
            "  The request was signed with OBLISK_IBKR_SIGNATURE_KEY and rejected.\n"
            "  Check the consumer key, that the SIGNING public key is the one in\n"
            "  IBKR's signing slot, and that the application is approved rather\n"
            "  than still in review."
        ),
        STEP_VERIFY: (
            "  The exchange succeeded and the derived token is NOT IBKR's. This is\n"
            "  the wrong-encryption-key case that PKCS#1 v1.5 decryption cannot\n"
            "  detect on its own. Check OBLISK_IBKR_ENCRYPTION_KEY against the key\n"
            "  registered with IBKR, and OBLISK_IBKR_DH_PARAM for the dhparam file\n"
            "  generated alongside it. Do not skip this check to get past it: an\n"
            "  unverified token fails every later call with no useful error."
        ),
        STEP_BROKERAGE: (
            "  The token is good but /iserver/auth/ssodh/init did not authenticate.\n"
            "  Market-data and portfolio calls will fail until it does. Usually a\n"
            "  permissions question on the IBKR account rather than a key problem."
        ),
        STEP_TICKLE: (
            "  The session opened and would not stay open. The Web API drops an\n"
            "  idle brokerage session, which is why the keepalive loop exists;\n"
            "  a failure here suggests the session is being invalidated elsewhere.\n"
            "  If an IBKR MCP session is open for the same account, close it and\n"
            "  re-run: whether the two can coexist is an OPEN QUESTION this\n"
            "  command is the cheapest way to settle."
        ),
    }.get(step, "")


async def run_preflight(
    settings: Settings, config: Config, client: httpx.AsyncClient
) -> PreflightReport:
    """Walk the handshake. Never raises for an expected failure — it reports it."""
    report = PreflightReport()

    try:
        credentials = credentials_from_settings(settings, config)
    except Exception as exc:
        report.add(STEP_CREDENTIALS, False, str(exc))
        return report
    report.add(
        STEP_CREDENTIALS,
        True,
        f"consumer key and three keys loaded; DH prime {credentials.dh_prime.bit_length()} bits",
    )

    try:
        prepend = credentials.decrypt_access_token_secret()
    except Exception as exc:
        report.add(STEP_DECRYPT, False, str(exc))
        return report
    # The plaintext is secret. Its LENGTH is not, and it is the one thing worth
    # printing: a wrong key that decrypts to random bytes usually yields an
    # implausible length, which is a hint before the signature check confirms it.
    report.add(STEP_DECRYPT, True, f"{len(prepend)} bytes of prepend recovered")

    base_url = settings.ibkr_cp_base_url
    if not base_url:
        report.add(STEP_EXCHANGE, False, "OBLISK_IBKR_CP_BASE_URL is unset")
        return report

    oauth = OAuthSession(credentials, config)
    url = base_url.rstrip("/") + LIVE_SESSION_TOKEN_PATH
    headers, challenge = oauth.build_live_session_token_request(url)
    # Recorded because it is UNVERIFIED whether IBKR accepts an odd-length hex
    # challenge, which is what format(x, "x") emits when the top nibble is small.
    # If the exchange fails only sometimes, this is the first thing to suspect.
    if len(challenge) % 2:
        log.warning(
            "the Diffie-Hellman challenge is odd-length hex; whether IBKR accepts "
            "that is unverified — see references/ibkr-playbook.md",
            extra={"challenge_hex_length": len(challenge)},
        )

    try:
        response = await client.post(url, headers=headers)
    except Exception as exc:
        report.add(STEP_EXCHANGE, False, f"the request did not complete: {exc!r}")
        return report
    if response.status_code != _HTTP_OK:
        report.add(
            STEP_EXCHANGE,
            False,
            f"HTTP {response.status_code}: {response.text[:ERROR_EXCERPT_CHARS]}",
        )
        return report
    try:
        body = response.json()
    except ValueError:
        report.add(STEP_EXCHANGE, False, "the response was not JSON")
        return report
    report.add(STEP_EXCHANGE, True, f"challenge {len(challenge)} hex chars, response received")

    try:
        token = oauth.complete_live_session_token(body)
    except Exception as exc:
        report.add(STEP_VERIFY, False, str(exc))
        return report
    report.add(STEP_VERIFY, True, "IBKR's signature over the derived token matches")

    cp_client = CpApiClient(
        base_url, oauth, config, client=client, ca_bundle=settings.ibkr_ca_bundle
    )
    try:
        authenticated = await cp_client.init_brokerage_session()
    except Exception as exc:
        report.add(STEP_BROKERAGE, False, repr(exc))
        return report
    report.add(
        STEP_BROKERAGE,
        bool(authenticated),
        "authenticated" if authenticated else "the session did not report authenticated",
    )
    if not authenticated:
        return report

    try:
        tickled = await cp_client.tickle()
    except Exception as exc:
        report.add(STEP_TICKLE, False, repr(exc))
        return report
    session_id = tickled.get("session") if isinstance(tickled, dict) else None
    report.add(
        STEP_TICKLE,
        True,
        f"session stays open (token valid until {token.expires_at:.0f}"
        + (f", session {session_id}" if session_id else "")
        + ")",
    )
    return report


#: HTTP 200. Named so the comparison below reads as a status check.
_HTTP_OK = 200
#: How much of an error body to quote. Enough to identify it, not enough to
#: paste a credential into a log.
