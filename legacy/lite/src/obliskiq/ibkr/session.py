"""Headless connect: environment to a usable Client Portal client.

Wraps the three steps between "the process just started" and "an /iserver call
will work", so the collectors and the server share one route rather than each
assembling it:

1. build :class:`~obliskiq.ibkr.oauth.OAuthCredentials` from the environment;
2. run the Diffie-Hellman exchange for a live session token, and **verify it**
   before trusting it;
3. open the brokerage session and start the tickle loop.

No human, no browser, no local gateway. That is the whole point: the downloadable
Client Portal Gateway and IB Gateway both need an interactive local login and
cannot run on a VM with nobody present.

**VERIFIED LIVE.** This header said the exchange "has never been run against the
live API, because IBKR have not approved the OAuth application" for four phases
after that stopped being true. The application was approved and the handshake ran:
``obliskiq-collect preflight`` passed all six steps on the first attempt
``[CP ✓ 2026-09-10]``, and every live collection since — Phases 4 through 9 —
has reached IBKR through this function. ``references/ibkr-playbook.md`` carries the
step table and the two things it settled, among them that IBKR accepts the
odd-length hex challenge ``format(x, "x")`` produces. New findings go there.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import httpx

from ..config import Config
from ..env import Settings
from ..errors import ERROR_EXCERPT_CHARS, AuthError, EnvVarError
from ..logging_setup import get_logger
from ..util.http import RetryPolicy, request_with_retry
from .cp_client import CpApiClient
from .oauth import OAuthCredentials, OAuthSession

log = get_logger("ibkr.session")

LIVE_SESSION_TOKEN_PATH = "/v1/api/oauth/live_session_token"

HTTP_OK = 200


def credentials_from_settings(settings: Settings, config: Config) -> OAuthCredentials:
    """Assemble OAuth credentials, naming precisely which variable is missing."""
    missing = [
        name
        for name, value in (
            ("OBLISK_IBKR_CONSUMER_KEY", settings.ibkr_consumer_key),
            ("OBLISK_IBKR_ACCESS_TOKEN", settings.ibkr_access_token),
            ("OBLISK_IBKR_ACCESS_TOKEN_SECRET", settings.ibkr_access_token_secret),
            ("OBLISK_IBKR_SIGNATURE_KEY", settings.ibkr_signature_key_path),
            ("OBLISK_IBKR_ENCRYPTION_KEY", settings.ibkr_encryption_key_path),
            ("OBLISK_IBKR_DH_PARAM", settings.ibkr_dh_param_path),
        )
        if not value
    ]
    if missing:
        raise EnvVarError(
            "cannot reach IBKR: " + ", ".join(missing) + " unset. These come from the "
            "OAuth 1.0a self-service registration in IBKR account settings, which is a "
            "web form — the downloadable gateways are out of scope because they need an "
            "interactive local session."
        )
    # Narrowed by the `missing` check above; re-stated as a real guard rather than
    # an assert, which `python -O` strips out of an unattended collector.
    if (
        settings.ibkr_signature_key_path is None
        or settings.ibkr_encryption_key_path is None
        or settings.ibkr_dh_param_path is None
    ):  # pragma: no cover - unreachable while the check above stands
        raise EnvVarError("an IBKR key path is unset")
    return OAuthCredentials.from_files(
        consumer_key=str(settings.ibkr_consumer_key),
        access_token=str(settings.ibkr_access_token),
        access_token_secret=str(settings.ibkr_access_token_secret),
        signing_key_path=settings.ibkr_signature_key_path,
        encryption_key_path=settings.ibkr_encryption_key_path,
        dh_param_path=settings.ibkr_dh_param_path,
        config=config,
    )


async def negotiate_live_session_token(
    oauth: OAuthSession, base_url: str, config: Config, client: httpx.AsyncClient
) -> None:
    """Run the DH exchange and install the resulting token on ``oauth``.

    ``complete_live_session_token`` verifies IBKR's signature over the derived
    token before accepting it. That check is the backstop for a mismatched
    encryption key, which on some OpenSSL builds decrypts to random bytes without
    raising, so it is never skipped.
    """
    if not oauth.needs_live_session_token():
        return
    url = base_url.rstrip("/") + LIVE_SESSION_TOKEN_PATH
    headers, _challenge = oauth.build_live_session_token_request(url)
    response = await request_with_retry(
        client, "POST", url, policy=RetryPolicy.from_config(config), headers=headers
    )
    if response.status_code != HTTP_OK:
        raise AuthError(
            f"/oauth/live_session_token returned HTTP {response.status_code}: "
            f"{response.text[:ERROR_EXCERPT_CHARS]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError("/oauth/live_session_token returned a non-JSON body") from exc
    oauth.complete_live_session_token(body)
    log.info("live session token established")


async def connect_cp_client(
    settings: Settings, config: Config, client: httpx.AsyncClient
) -> CpApiClient:
    """A Client Portal client with a live session token and an open brokerage session.

    The caller owns the returned client and must ``aclose()`` it, which also stops
    the tickle loop.
    """
    base_url = settings.ibkr_cp_base_url
    if not base_url:
        raise EnvVarError(
            "OBLISK_IBKR_CP_BASE_URL is unset. The collectors reach IBKR over the "
            "Client Portal Web API with OAuth 1.0a."
        )
    oauth = OAuthSession(credentials_from_settings(settings, config), config)
    await negotiate_live_session_token(oauth, base_url, config, client)

    cp_client = CpApiClient(
        base_url, oauth, config, client=client, ca_bundle=settings.ibkr_ca_bundle
    )
    if config.require_bool("ibkr.cp_api.session.ssodh_init_on_connect"):
        authenticated = await cp_client.init_brokerage_session()
        if not authenticated:
            log.warning(
                "the brokerage session did not report authenticated. /iserver calls "
                "will fail until it does; the keepalive loop will retry."
            )
    await cp_client.start_keepalive()
    return cp_client
