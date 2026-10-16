"""OAuth 1.0a with RSA signing for the IBKR Client Portal Web API (transport ``oauth``).

Ported from ObliskIQ-lite-MCP (``ibkr/oauth.py`` and ``ibkr/session.py``), where the handshake
ran live first time and on every collection since [CP ✓ 2026-09-10]. Per process:

1. A Diffie-Hellman exchange against ``/oauth/live_session_token``, signed RSA-SHA256 with the
   signing key. The access-token secret is RSA-decrypted (PKCS#1 v1.5) with the encryption key to
   give the DH "prepend", which precedes the signature base string and is not itself a parameter.
2. The live session token ``LST = HMAC-SHA1(key=B^a mod p, msg=prepend)`` is derived and
   **verified** against ``live_session_token_signature = HMAC-SHA1(key=LST, msg=consumer_key)``
   before it is trusted. SHA-1 is IBKR's protocol, not a choice.
3. Every request is signed HMAC-SHA256 with the token; it is renegotiated before it expires.

The DH shared secret is a SIGNED big integer: a value whose high bit is set carries a leading
zero byte, and omitting it derives a different token. The signature check is never skipped: on
OpenSSL builds with implicit rejection for PKCS#1 v1.5, a wrong encryption key decrypts to random
bytes instead of raising, and the check is what catches it. IBKR accepts the odd-length hex
challenge ``format(x, "x")`` produces [CP ✓ 2026-09-10].

TLS verification is always on; there is no path that disables it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import base64
import hashlib
import hmac
import secrets
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ...config.schema_ibkr import IbkrConfig
from ...env import Environment
from ...errors import ConfigError, EnvVarError, UpstreamError
from ...logging_setup import get_logger
from ...util.http import RetryPolicy, request_with_retry, retry_after_seconds
from ..errors import AuthError, IbkrUpstreamError, excerpt
from .base import TransportKind

log = get_logger("ibkr.oauth")

#: RFC 3986 unreserved set. OAuth percent-encoding is stricter than urlencode's.
_UNRESERVED = "-._~"

#: Bits per byte, so the big-integer conversions read as arithmetic.
_BITS_PER_BYTE = 8

#: IBKR exchanges Diffie-Hellman values as hexadecimal strings.
_HEX_BASE = 16

HTTP_OK = 200


def percent_encode(value: str) -> str:
    """RFC 3986 percent-encoding as OAuth 1.0a requires it."""
    return quote(str(value), safe=_UNRESERVED)


def build_signature_base_string(method: str, url: str, params: Mapping[str, str]) -> str:
    """``METHOD&percent(url)&percent(sorted normalised params)``."""
    normalised = "&".join(
        f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted(params.items())
    )
    return f"{method.upper()}&{percent_encode(url)}&{percent_encode(normalised)}"


def build_authorization_header(realm: str, params: Mapping[str, str]) -> str:
    """The ``Authorization: OAuth ...`` header."""
    pairs = ", ".join(
        f'{percent_encode(k)}="{percent_encode(v)}"' for k, v in sorted(params.items())
    )
    return f'OAuth realm="{realm}", {pairs}'


def to_signed_bytes(value: int) -> bytes:
    """Big-endian bytes with a leading zero when the top bit is set (a signed big integer)."""
    length = (value.bit_length() + _BITS_PER_BYTE) // _BITS_PER_BYTE
    return value.to_bytes(length, "big")


@dataclass(frozen=True)
class OAuthCredentials:
    """Self-service OAuth 1.0a credentials, from the environment and the key files it names."""

    consumer_key: str
    access_token: str
    access_token_secret: str
    signing_key: rsa.RSAPrivateKey
    encryption_key: rsa.RSAPrivateKey
    dh_prime: int
    dh_generator: int

    def __repr__(self) -> str:
        return "OAuthCredentials(<redacted>)"

    @classmethod
    def from_files(
        cls,
        *,
        consumer_key: str,
        access_token: str,
        access_token_secret: str,
        signing_key_path: Path,
        encryption_key_path: Path,
        dh_param_path: Path,
        ibkr: IbkrConfig,
    ) -> Self:
        signing_key = _load_private_key(signing_key_path, "signature")
        encryption_key = _load_private_key(encryption_key_path, "encryption")
        prime, generator = _load_dh_params(dh_param_path)
        if prime.bit_length() < ibkr.oauth.dh_prime_bits_min:
            raise ConfigError(
                f"the Diffie-Hellman prime is {prime.bit_length()} bits, below the configured "
                f"minimum of {ibkr.oauth.dh_prime_bits_min} (ibkr.oauth.dh_prime_bits_min)"
            )
        return cls(
            consumer_key=consumer_key,
            access_token=access_token,
            access_token_secret=access_token_secret,
            signing_key=signing_key,
            encryption_key=encryption_key,
            dh_prime=prime,
            dh_generator=generator,
        )

    @classmethod
    def from_environment(cls, env: Environment, ibkr: IbkrConfig) -> Self:
        """The credentials the environment names; every missing variable is named at once."""
        missing = [
            name
            for name, value in (
                ("OBLISK_IBKR_CONSUMER_KEY", env.ibkr_consumer_key),
                ("OBLISK_IBKR_ACCESS_TOKEN", env.ibkr_access_token),
                ("OBLISK_IBKR_ACCESS_TOKEN_SECRET", env.ibkr_access_token_secret),
                ("OBLISK_IBKR_SIGNATURE_KEY", env.ibkr_signature_key_path),
                ("OBLISK_IBKR_ENCRYPTION_KEY", env.ibkr_encryption_key_path),
                ("OBLISK_IBKR_DH_PARAM", env.ibkr_dh_param_path),
            )
            if value is None
        ]
        if (
            missing
            or env.ibkr_consumer_key is None
            or env.ibkr_access_token is None
            or env.ibkr_access_token_secret is None
            or env.ibkr_signature_key_path is None
            or env.ibkr_encryption_key_path is None
            or env.ibkr_dh_param_path is None
        ):
            raise EnvVarError(
                "the OAuth transport cannot start: "
                + ", ".join(missing)
                + " unset. They come from IBKR's OAuth 1.0a self-service registration."
            )
        return cls.from_files(
            consumer_key=env.ibkr_consumer_key.get_secret_value(),
            access_token=env.ibkr_access_token.get_secret_value(),
            access_token_secret=env.ibkr_access_token_secret.get_secret_value(),
            signing_key_path=env.ibkr_signature_key_path,
            encryption_key_path=env.ibkr_encryption_key_path,
            dh_param_path=env.ibkr_dh_param_path,
            ibkr=ibkr,
        )

    def decrypt_access_token_secret(self) -> bytes:
        """RSA-decrypt the access-token secret into the DH prepend.

        This cannot be relied on to detect a wrong encryption key (implicit rejection returns
        random bytes on some OpenSSL builds); the live-session-token signature check is the
        backstop, which is why that check is never optional.
        """
        try:
            ciphertext = base64.b64decode(self.access_token_secret)
            return self.encryption_key.decrypt(ciphertext, padding.PKCS1v15())
        except Exception as exc:
            raise AuthError(
                "could not RSA-decrypt the access-token secret. OBLISK_IBKR_ACCESS_TOKEN_SECRET "
                "must be the base64 ciphertext IBKR issued, and OBLISK_IBKR_ENCRYPTION_KEY the "
                "matching private key."
            ) from exc


def _load_private_key(path: Path, label: str) -> rsa.RSAPrivateKey:
    resolved = path.expanduser()
    try:
        key = serialization.load_pem_private_key(resolved.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(
            f"could not load the OAuth {label} private key from {resolved}: {exc}"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ConfigError(f"the OAuth {label} key at {resolved} is not an RSA private key")
    return key


def _load_dh_params(path: Path) -> tuple[int, int]:
    """The DH prime and generator from a PEM ``dhparam`` file."""
    resolved = path.expanduser()
    try:
        params = serialization.load_pem_parameters(resolved.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(
            f"could not load Diffie-Hellman parameters from {resolved}: {exc}"
        ) from exc
    numbers = params.parameter_numbers()
    return int(numbers.p), int(numbers.g)


@dataclass(frozen=True)
class LiveSessionToken:
    """A derived live session token and its expiry (seconds since the epoch)."""

    token: bytes
    expires_at: float

    def __repr__(self) -> str:
        return f"LiveSessionToken(<redacted>, expires_at={self.expires_at:.0f})"

    def is_valid(self, *, refresh_margin_seconds: float, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current < (self.expires_at - refresh_margin_seconds)


class OAuthSession:
    """Signs requests and derives the live session token.

    Stateless with respect to the network: it produces headers and parses the
    live-session-token response, so it is tested without an HTTP call.
    """

    def __init__(self, credentials: OAuthCredentials, ibkr: IbkrConfig) -> None:
        self._credentials = credentials
        self._ibkr = ibkr
        self._live_session_token: LiveSessionToken | None = None
        self._dh_private: int | None = None

    @property
    def realm(self) -> str:
        return self._ibkr.oauth.realm

    @property
    def live_session_token(self) -> LiveSessionToken | None:
        return self._live_session_token

    def needs_live_session_token(self, *, now: float | None = None) -> bool:
        token = self._live_session_token
        return token is None or not token.is_valid(
            refresh_margin_seconds=self._ibkr.oauth.live_session_token_refresh_margin_seconds,
            now=now,
        )

    def _base_params(self) -> dict[str, str]:
        return {
            "oauth_consumer_key": self._credentials.consumer_key,
            "oauth_token": self._credentials.access_token,
            "oauth_nonce": secrets.token_hex(self._ibkr.oauth.nonce_bytes),
            "oauth_timestamp": str(int(time.time())),
        }

    def build_live_session_token_request(
        self, url: str, *, dh_private: int | None = None
    ) -> tuple[dict[str, str], str]:
        """``(headers, dh_challenge_hex)`` for ``POST /oauth/live_session_token``.

        A fresh private exponent per exchange; ``A = g^a mod p`` is signed into the base string.
        """
        creds = self._credentials
        self._dh_private = (
            dh_private
            if dh_private is not None
            else int.from_bytes(
                secrets.token_bytes(creds.dh_prime.bit_length() // _BITS_PER_BYTE), "big"
            )
        )
        challenge = pow(creds.dh_generator, self._dh_private, creds.dh_prime)
        challenge_hex = format(challenge, "x")
        prepend = creds.decrypt_access_token_secret().hex()

        params = self._base_params()
        params["oauth_signature_method"] = self._ibkr.oauth.signature_method_rsa
        params["diffie_hellman_challenge"] = challenge_hex
        base_string = prepend + build_signature_base_string("POST", url, params)
        signature = base64.b64encode(
            creds.signing_key.sign(base_string.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        ).decode("ascii")
        params["oauth_signature"] = signature
        headers = {
            "Authorization": build_authorization_header(self._ibkr.oauth.realm, params),
            "User-Agent": self._ibkr.oauth.user_agent,
            "Host": urlsplit(url).netloc,
        }
        return headers, challenge_hex

    def complete_live_session_token(
        self, response_body: Mapping[str, Any], *, now: float | None = None
    ) -> LiveSessionToken:
        """Derive the live session token from IBKR's DH response and verify it."""
        if self._dh_private is None:
            raise AuthError(
                "complete_live_session_token called before build_live_session_token_request"
            )
        try:
            dh_response_hex = str(response_body["diffie_hellman_response"])
            expected_signature = str(response_body["live_session_token_signature"])
        except KeyError as exc:
            raise AuthError(
                f"the live session token response is missing {exc.args[0]!r} "
                f"(keys: {sorted(str(k) for k in response_body)})"
            ) from exc
        try:
            dh_response = int(dh_response_hex, _HEX_BASE)
        except ValueError as exc:
            raise AuthError("the Diffie-Hellman response is not hexadecimal") from exc

        creds = self._credentials
        shared = pow(dh_response, self._dh_private, creds.dh_prime)
        prepend = creds.decrypt_access_token_secret()
        token = hmac.new(to_signed_bytes(shared), prepend, hashlib.sha1).digest()
        computed = hmac.new(token, creds.consumer_key.encode("utf-8"), hashlib.sha1).hexdigest()
        if not hmac.compare_digest(computed, expected_signature):
            raise AuthError(
                "the live session token failed IBKR's signature check. On OpenSSL builds with "
                "implicit rejection a mismatched OBLISK_IBKR_ENCRYPTION_KEY surfaces here rather "
                "than earlier. Check the consumer key, the encryption key registered with IBKR "
                "and the Diffie-Hellman parameters."
            )
        expires_at = (time.time() if now is None else now) + (
            self._ibkr.oauth.live_session_token_ttl_seconds
        )
        self._live_session_token = LiveSessionToken(token=token, expires_at=expires_at)
        log.info("live session token derived", extra={"expires_at": round(expires_at)})
        return self._live_session_token

    def sign_request(
        self, method: str, url: str, *, params: Mapping[str, Any] | None = None
    ) -> dict[str, str]:
        """Headers signing ``method url`` HMAC-SHA256 with the live session token.

        Query parameters take part in the base string, so pass the mapping that goes on the
        wire. A JSON body does not: only form-encoded parameters are signed.
        """
        token = self._live_session_token
        if token is None:
            raise AuthError("no live session token; negotiate one first")
        oauth_params = self._base_params()
        oauth_params["oauth_signature_method"] = self._ibkr.oauth.signature_method_hmac
        signing_params = dict(oauth_params)
        for key, value in (params or {}).items():
            signing_params[str(key)] = str(value)
        base_string = build_signature_base_string(method, url, signing_params)
        oauth_params["oauth_signature"] = base64.b64encode(
            hmac.new(token.token, base_string.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        return {
            "Authorization": build_authorization_header(self._ibkr.oauth.realm, oauth_params),
            "User-Agent": self._ibkr.oauth.user_agent,
            "Host": urlsplit(url).netloc,
            "Accept": "application/json",
        }


async def negotiate_live_session_token(
    oauth: OAuthSession,
    url: str,
    client: httpx.AsyncClient,
    policy: RetryPolicy,
) -> LiveSessionToken:
    """Run the DH exchange and install the verified token on ``oauth``.

    Each attempt is a new signed request (fresh nonce, timestamp and DH challenge): resending
    the same signed headers would be a replay. A host that stays unreachable raises
    :class:`IbkrUpstreamError`, so the session's repair loop counts and alerts it.
    """
    once = replace(policy, retries=0)
    response: httpx.Response | None = None
    for attempt in range(policy.retries + 1):
        headers, _challenge = oauth.build_live_session_token_request(url)
        response = None
        try:
            response = await request_with_retry(client, "POST", url, policy=once, headers=headers)
        except UpstreamError as exc:
            if attempt >= policy.retries:
                raise IbkrUpstreamError(
                    f"/oauth/live_session_token was unreachable after {attempt + 1} attempts"
                ) from exc
        else:
            if response.status_code not in policy.retry_status_codes:
                break
        if attempt < policy.retries:
            asked = retry_after_seconds(response, policy) if response is not None else None
            log.warning(
                "live session token request failed; retrying with a new signature",
                extra={
                    "attempt": attempt + 1,
                    "status": response.status_code if response is not None else None,
                },
            )
            await asyncio.sleep(asked if asked is not None else policy.delay_for(attempt))
    if response is None:  # pragma: no cover - the final attempt either raised or answered
        raise IbkrUpstreamError("/oauth/live_session_token gave no answer")
    if response.status_code in policy.retry_status_codes:
        # Still busy or failing after every retry: an upstream fault, not a refusal.
        raise IbkrUpstreamError(
            f"/oauth/live_session_token still answered HTTP {response.status_code} after "
            f"{policy.retries + 1} attempts"
        )
    if response.status_code != HTTP_OK:
        raise AuthError(
            f"/oauth/live_session_token returned HTTP {response.status_code}: "
            f"{excerpt(response.text)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError("/oauth/live_session_token returned a non-JSON body") from exc
    if not isinstance(body, Mapping):
        raise AuthError("/oauth/live_session_token returned JSON that is not an object")
    return oauth.complete_live_session_token(body)


class OAuthTransport:
    """Transport ``oauth``: signs every request with a live session token kept fresh."""

    def __init__(
        self,
        oauth: OAuthSession,
        *,
        origin: str,
        base_path: str,
        token_path: str,
        timeout_seconds: float,
        policy: RetryPolicy,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._oauth = oauth
        self._origin = origin.rstrip("/")
        self._base_url = self._origin + base_path
        self._token_url = self._base_url + token_path
        self._policy = policy
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(verify=True, timeout=timeout_seconds)
        self._lock = asyncio.Lock()

    @property
    def kind(self) -> TransportKind:
        return TransportKind.OAUTH

    @property
    def base_url(self) -> str:
        return self._base_url

    def verify(self) -> ssl.SSLContext | bool:
        return True

    async def _ensure_token(self, *, force: bool) -> None:
        async with self._lock:
            if force or self._oauth.needs_live_session_token():
                await negotiate_live_session_token(
                    self._oauth, self._token_url, self._client, self._policy
                )

    async def prepare(self) -> None:
        await self._ensure_token(force=False)

    async def refresh(self) -> None:
        # Called before the request takes a pacing slot, so a slow or failing token exchange
        # never holds a concurrency permit.
        await self._ensure_token(force=False)

    async def headers(self, method: str, url: str, params: Mapping[str, Any]) -> dict[str, str]:
        # refresh() already renewed a token inside its margin; this only catches a token that
        # expired in between.
        await self._ensure_token(force=False)
        return self._oauth.sign_request(method, url, params=params)

    async def recover(self) -> None:
        await self._ensure_token(force=True)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
