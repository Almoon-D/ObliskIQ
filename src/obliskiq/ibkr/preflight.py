"""``obliskiq preflight --read-only``: walk the read path one step at a time and say which failed.

transport → brokerage session → accounts → account guard → positions → cash → contract identity
→ snapshot with availability → history → contract rules → algos → trading schedule, for the
configured stock. Each step reports on its own and the walk stops at the first failure, because a
failure at step N says nothing about N+1. Under the OAuth transport the handshake itself is walked
first (lite's preflight: credentials, decrypt, exchange, verify — the signature check is never
skipped), so a bad credential names its artefact instead of surfacing as a bare 401.

Nothing is written and no order endpoint is touched. The report masks the account id and prints
no balances or positions: counts, currencies and statuses only.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..config.schema import ObliskConfig
from ..env import Environment
from ..errors import ObliskError
from ..logging_setup import get_logger
from ..util.http import RetryPolicy
from .client import IbkrClient
from .errors import AuthError, excerpt
from .guard import AccountGuard
from .identity import resolve_identity
from .redact import redact
from .session import AlertHook, BrokerSession
from .transport import Transport, build_transport, transport_refusal
from .transport.gateway_login import LoginDriver
from .transport.oauth import HTTP_OK, OAuthCredentials, OAuthSession

log = get_logger("ibkr.preflight")

STEP_TRANSPORT = "transport"
STEP_SESSION = "brokerage-session"
STEP_ACCOUNTS = "accounts"
STEP_GUARD = "account-guard"
STEP_POSITIONS = "positions"
STEP_CASH = "cash"
STEP_IDENTITY = "contract-identity"
STEP_SNAPSHOT = "snapshot"
STEP_HISTORY = "history"
STEP_RULES = "contract-rules"
STEP_ALGOS = "algos"
STEP_SCHEDULE = "trading-schedule"

OAUTH_CREDENTIALS = "oauth-credentials"
OAUTH_DECRYPT = "oauth-decrypt-access-token-secret"
OAUTH_EXCHANGE = "oauth-diffie-hellman-exchange"
OAUTH_VERIFY = "oauth-live-session-token-signature"

READ_STEPS = (
    STEP_TRANSPORT,
    STEP_SESSION,
    STEP_ACCOUNTS,
    STEP_GUARD,
    STEP_POSITIONS,
    STEP_CASH,
    STEP_IDENTITY,
    STEP_SNAPSHOT,
    STEP_HISTORY,
    STEP_RULES,
    STEP_ALGOS,
    STEP_SCHEDULE,
)
OAUTH_STEPS = (OAUTH_CREDENTIALS, OAUTH_DECRYPT, OAUTH_EXCHANGE, OAUTH_VERIFY)

REMEDIES = {
    OAUTH_CREDENTIALS: "Set the consumer key, access token and secret IBKR issued, and the three "
    "key file paths.",
    OAUTH_DECRYPT: "OBLISK_IBKR_ACCESS_TOKEN_SECRET must be IBKR's base64 ciphertext and "
    "OBLISK_IBKR_ENCRYPTION_KEY the private half of the ENCRYPTION key (not the signing key).",
    OAUTH_EXCHANGE: "The live-session-token request was rejected: check the consumer key, the "
    "SIGNING key registered with IBKR, and that the application is approved.",
    OAUTH_VERIFY: "The derived token is not IBKR's: the wrong-encryption-key case PKCS#1 v1.5 "
    "cannot detect. Check OBLISK_IBKR_ENCRYPTION_KEY and OBLISK_IBKR_DH_PARAM. Never skip this.",
    STEP_TRANSPORT: "The transport did not come up. For the Gateway: Java must be installed, the "
    "archive must match its pin, and the paper login must succeed (see the detail).",
    STEP_SESSION: "/iserver/auth/ssodh/init did not authenticate: usually a permissions question "
    "on the account, or another session holding the username.",
    STEP_GUARD: "The configured account is not usable here: check OBLISK_IBKR_ACCOUNT_ID and, "
    "under profile paper, that it is the paper account (DU...).",
}


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return f"  [{mark}] {self.name}{': ' + self.detail if self.detail else ''}"


@dataclass
class PreflightReport:
    steps: list[StepResult] = field(default_factory=list)
    planned: tuple[str, ...] = READ_STEPS

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

    def render(self, known_ids: tuple[str, ...] = ()) -> str:
        lines = ["IBKR read-only preflight:", ""]
        lines += [redact(step.line(), known_ids) for step in self.steps]
        reached = {step.name for step in self.steps}
        lines += [f"  [ -  ] {name} (not reached)" for name in self.planned if name not in reached]
        lines.append("")
        failed = self.failed_step
        if failed is None:
            lines.append("Every step passed.")
        else:
            lines.append(f"Stopped at: {failed.name}")
            remedy = REMEDIES.get(failed.name)
            if remedy:
                lines.append(f"  {remedy}")
        return "\n".join(lines)


async def oauth_handshake(
    env: Environment, settings: ObliskConfig, client: httpx.AsyncClient, report: PreflightReport
) -> bool:
    """Walk the OAuth handshake step by step (lite's preflight). Returns whether it passed."""
    ibkr = settings.ibkr
    try:
        credentials = OAuthCredentials.from_environment(env, ibkr)
    except ObliskError as exc:
        report.add(OAUTH_CREDENTIALS, False, str(exc))
        return False
    report.add(
        OAUTH_CREDENTIALS, True, f"keys loaded; DH prime {credentials.dh_prime.bit_length()} bits"
    )
    try:
        prepend = credentials.decrypt_access_token_secret()
    except AuthError as exc:
        report.add(OAUTH_DECRYPT, False, str(exc))
        return False
    # The plaintext is secret; its length is not, and a wrong key often yields an odd one.
    report.add(OAUTH_DECRYPT, True, f"{len(prepend)} bytes of prepend recovered")
    if env.ibkr_cp_base_url is None:
        report.add(OAUTH_EXCHANGE, False, "OBLISK_IBKR_CP_BASE_URL is unset")
        return False
    oauth = OAuthSession(credentials, ibkr)
    url = env.ibkr_cp_base_url + ibkr.base_path + ibkr.oauth.live_session_token_path
    headers, challenge = oauth.build_live_session_token_request(url)
    try:
        response = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        report.add(OAUTH_EXCHANGE, False, f"the request did not complete ({type(exc).__name__})")
        return False
    if response.status_code != HTTP_OK:
        report.add(OAUTH_EXCHANGE, False, f"HTTP {response.status_code}: {excerpt(response.text)}")
        return False
    try:
        body: Any = response.json()
    except ValueError:
        report.add(OAUTH_EXCHANGE, False, "the response was not JSON")
        return False
    report.add(OAUTH_EXCHANGE, True, f"challenge {len(challenge)} hex characters, answered")
    try:
        oauth.complete_live_session_token(body if isinstance(body, dict) else {})
    except AuthError as exc:
        report.add(OAUTH_VERIFY, False, str(exc))
        return False
    report.add(OAUTH_VERIFY, True, "IBKR's signature over the derived token matches")
    return True


type Step = Callable[[], Awaitable[str]]


async def _step(report: PreflightReport, name: str, run: Step, known: tuple[str, ...]) -> bool:
    try:
        detail = await run()
    except ObliskError as exc:
        report.add(name, False, redact(str(exc), known))
        return False
    report.add(name, True, detail)
    return True


async def run_read_only(
    env: Environment,
    settings: ObliskConfig,
    *,
    data_dir: Path | None,
    transport: Transport | None = None,
    alert: AlertHook | None = None,
    login_driver: LoginDriver | None = None,
) -> PreflightReport:
    """Walk the read path. Never raises for an expected failure: it reports it.

    ``transport`` replaces the one the environment selects; ``login_driver`` replaces the
    Gateway's headless browser inside the transport this builds.
    """
    report = PreflightReport()
    ibkr = settings.ibkr
    account_id = env.ibkr_account_id.get_secret_value() if env.ibkr_account_id else ""
    known = (account_id,)
    # Both before any credential is read: a paper run never opens an OAuth key file.
    if env.ibkr_profile is None:
        report.add(STEP_TRANSPORT, False, "OBLISK_IBKR_PROFILE is unset")
        return report
    refusal = transport_refusal(env) if transport is None else None
    if refusal is not None:
        report.add(STEP_TRANSPORT, False, refusal)
        return report
    if env.ibkr_transport == "oauth" and transport is None:
        report.planned = OAUTH_STEPS + READ_STEPS
        async with httpx.AsyncClient(timeout=ibkr.request_timeout_seconds) as handshake:
            if not await oauth_handshake(env, settings, handshake, report):
                return report
    guard = AccountGuard(account_id, env.ibkr_profile, tuple(ibkr.paper_account_prefixes))
    try:
        guard.check_prefix()
        if transport is None:
            if data_dir is None and env.ibkr_transport == "gateway":
                raise ObliskError("OBLISK_DATA_DIR must be set: the Gateway is installed there")
            transport = build_transport(
                env, settings, data_dir=data_dir or Path(), login_driver=login_driver
            )
    except ObliskError as exc:
        report.add(STEP_GUARD if "paper account" in str(exc) else STEP_TRANSPORT, False, str(exc))
        return report
    session = BrokerSession(
        transport, ibkr, RetryPolicy.from_settings(settings.http), alert=alert, known_ids=known
    )
    client = IbkrClient(session, settings, guard)
    try:
        await _walk(client, settings, report, known)
    finally:
        await client.aclose()
    return report


async def _walk(
    client: IbkrClient, settings: ObliskConfig, report: PreflightReport, known: tuple[str, ...]
) -> None:
    session = client.session
    probe = settings.ibkr.preflight

    async def transport() -> str:
        await session.transport.prepare()
        return f"{session.transport.kind.value} ready"

    async def brokerage() -> str:
        status = await session.open()
        if status is not None and not status.authenticated:
            raise AuthError("ssodh/init did not report an authenticated session")
        competing = " (a competing session was reported)" if status and status.competing else ""
        return "authenticated" + competing

    async def accounts() -> str:
        found = await client.accounts()
        return f"{len(found)} account(s) listed for this login"

    async def guard() -> str:
        client.run_guard(await client.accounts())
        return f"account {client.guard.masked} under profile {client.guard.profile}"

    async def positions() -> str:
        return f"{len(await client.portfolio.positions())} position row(s)"

    async def cash() -> str:
        ledger = await client.portfolio.ledger()
        currencies = ", ".join(balance.currency for balance in ledger.balances) or "none"
        nav = "NAV in base currency read" if ledger.nav else f"NAV refused: {ledger.nav_refusal}"
        return f"cash in {currencies}; {nav}"

    identity_holder: dict[str, int] = {}

    async def identity() -> str:
        found = await resolve_identity(
            client.secdef,
            probe.symbol,
            listing_exchange=probe.listing_exchange,
            currency=probe.currency,
        )
        identity_holder["conid"] = found.conid
        return (
            f"{found.symbol} → conid {found.conid} on {found.listing_exchange} in {found.currency}"
        )

    async def snapshot() -> str:
        conid = identity_holder["conid"]
        quote = (await client.marketdata.snapshot([conid], underlying=True)).get(conid)
        if quote is None:
            raise AuthError("the snapshot returned no row for the contract")
        status = quote.market_data_status.value if quote.market_data_status else "unreported"
        delay = (
            f", delay {quote.delay_seconds} s ({quote.delay_basis.value})"
            if quote.delay_seconds is not None and quote.delay_basis is not None
            else ""
        )
        priced = "priced" if quote.priced else "no price"
        kind = f", last is {quote.last_kind.value}" if quote.last_kind else ""
        return f"{priced}; availability {status} (flag {quote.status_flag}){delay}{kind}"

    async def history() -> str:
        found = await client.history.bars(
            identity_holder["conid"], period=probe.history_period, bar=probe.history_bar
        )
        if not found.bars:
            return "no bars returned"
        first, last = found.bars[0].start.date(), found.bars[-1].start.date()
        return f"{len(found.bars)} bar(s), {first} .. {last}"

    async def rules() -> str:
        found = await client.contracts.rules(identity_holder["conid"])
        steps = ", ".join(f"{r.increment:g}≥{r.lower_edge:g}" for r in found.increment_rules)
        return (
            f"{len(found.order_types)} order type(s), limit "
            f"{'offered' if 'limit' in found.order_types else 'NOT offered'}; tick {steps or '?'}; "
            f"size increment {found.size_increment}; cash-quantity types "
            f"{len(found.cash_quantity_order_types)}"
        )

    async def algos() -> str:
        found = await client.contracts.algos(identity_holder["conid"])
        return ", ".join(algo.algo_id for algo in found) or "none offered"

    async def schedule() -> str:
        found = await client.contracts.trading_schedule(identity_holder["conid"])
        return (
            f"{len(found.sessions)} regular session(s) over {found.first_day} .. {found.last_day} "
            f"({found.timezone})"
        )

    steps: list[tuple[str, Step]] = [
        (STEP_TRANSPORT, transport),
        (STEP_SESSION, brokerage),
        (STEP_ACCOUNTS, accounts),
        (STEP_GUARD, guard),
        (STEP_POSITIONS, positions),
        (STEP_CASH, cash),
        (STEP_IDENTITY, identity),
        (STEP_SNAPSHOT, snapshot),
        (STEP_HISTORY, history),
        (STEP_RULES, rules),
        (STEP_ALGOS, algos),
        (STEP_SCHEDULE, schedule),
    ]
    for name, run in steps:
        if not await _step(report, name, run, known):
            return
