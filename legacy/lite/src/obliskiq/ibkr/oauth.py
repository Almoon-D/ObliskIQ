"""OAuth 1.0a with RSA signing for the IBKR Client Portal Web API.

This REPLACES IBKR-Portmanteau's ``IBKRGateway.__init__``, ``check_status_immediately``
and ``_maintenance_loop``. That design pointed at ``https://localhost:5000``, an
interactively-authenticated Client Portal Gateway kept alive by a 45-second tickle.
It cannot run on a headless VM: somebody has to log in through a browser, and the
session dies when they do not.

What replaces it
----------------
Self-service OAuth 1.0a. The consumer key, access token and access-token secret are
issued once through IBKR's web form and stored as environment variables. Per
process the client then:

1. Runs a Diffie-Hellman exchange against ``/oauth/live_session_token``, signed
   RSA-SHA256 with the signing private key. The access-token secret is RSA-decrypted
   with the encryption private key to yield the DH "prepend".
2. Derives the live session token from the shared DH secret, and verifies it against
   the signature IBKR returns before trusting it.
3. Signs every subsequent request HMAC-SHA256 with that token.
4. Opens a brokerage session via ``/iserver/auth/ssodh/init`` and keeps it alive
   with ``/tickle``.

The tickle loop survives from the original design because the Web API genuinely
needs it; what changed is that nothing here requires a human or a local gateway.

Not carried over
----------------
``verify=False``. The original disabled TLS verification whenever ``IBKR_CACERT``
was unset, which is most of the time, and logged a warning nobody reads. The Web
API presents a normal public certificate, so verification is always on. A custom
CA bundle may be supplied via ``OBLISK_IBKR_CA_BUNDLE`` but never bypassed.

.. warning::
   DOCUMENTATION-DERIVED AND UNVERIFIED. Built in a sandbox with no live IBKR
   access. The DH exchange, the live-session-token derivation and the signature
   base strings follow IBKR's published OAuth 1.0a specification and must be
   still be re-tested against the live API. Every finding belongs in
   ``references/ibkr-playbook.md``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..config import Config
from ..errors import AuthError, ConfigError
from ..logging_setup import get_logger

log = get_logger("ibkr.oauth")

#: RFC 3986 unreserved set. OAuth percent-encoding is stricter than urlencode's.
_UNRESERVED = "-._~"

#: Bits per byte. Named so the big-integer conversions below read as arithmetic
#: rather than as tunable constants.
_BITS_PER_BYTE = 8

#: IBKR exchanges Diffie-Hellman values as hexadecimal strings.
_HEX_BASE = 16


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


@dataclass(frozen=True)
class OAuthCredentials:
    """Self-service OAuth 1.0a credentials. All from the environment, never literals."""

    consumer_key: str
    access_token: str
    access_token_secret: str
    signing_key: rsa.RSAPrivateKey
    encryption_key: rsa.RSAPrivateKey
    dh_prime: int
    dh_generator: int

    @classmethod
    def from_files(
        cls,
        *,
        consumer_key: str,
        access_token: str,
        access_token_secret: str,
        signing_key_path: str | Path,
        encryption_key_path: str | Path,
        dh_param_path: str | Path,
        config: Config,
    ) -> OAuthCredentials:
        signing_key = _load_private_key(signing_key_path, "signature")
        encryption_key = _load_private_key(encryption_key_path, "encryption")
        prime, generator = _load_dh_params(dh_param_path)

        min_bits = config.require_int("ibkr.cp_api.oauth.dh_prime_bits_min")
        if prime.bit_length() < min_bits:
            raise ConfigError(
                f"Diffie-Hellman prime is {prime.bit_length()} bits, below the configured "
                f"minimum of {min_bits} (ibkr.cp_api.oauth.dh_prime_bits_min)"
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

    def decrypt_access_token_secret(self) -> bytes:
        """RSA-decrypt the access-token secret into the DH prepend.

        IBKR issues the secret RSA-encrypted (PKCS#1 v1.5) under the consumer's
        encryption public key. The plaintext is the prepend hashed into the live
        session token.

        .. note::
           This cannot be relied on to detect a wrong encryption key. Where
           OpenSSL implements implicit rejection for PKCS#1 v1.5 as a
           Bleichenbacher countermeasure, decrypting with the wrong key returns
           indistinguishable random bytes rather than raising; on other builds
           it raises. Both behaviours were observed across the CI matrix.

           The signature check in
           :meth:`OAuthSession.complete_live_session_token` is therefore the
           backstop that catches a mismatched key on the builds where this
           method returns quietly — which is why that check is not optional.
        """
        try:
            ciphertext = base64.b64decode(self.access_token_secret)
            return self.encryption_key.decrypt(ciphertext, padding.PKCS1v15())
        except Exception as exc:
            raise AuthError(
                "could not RSA-decrypt the access-token secret. The value in "
                "OBLISK_IBKR_ACCESS_TOKEN_SECRET must be the base64 ciphertext IBKR "
                "issued, and OBLISK_IBKR_ENCRYPTION_KEY the matching private key."
            ) from exc


def _load_private_key(path: str | Path, label: str) -> rsa.RSAPrivateKey:
    resolved = Path(path).expanduser()
    try:
        key = serialization.load_pem_private_key(resolved.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(
            f"could not load the OAuth {label} private key from {resolved}: {exc}"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ConfigError(f"the OAuth {label} key at {resolved} is not an RSA private key")
    return key


def _load_dh_params(path: str | Path) -> tuple[int, int]:
    """Read the DH prime and generator from a PEM ``dhparam`` file."""
    resolved = Path(path).expanduser()
    try:
        params = serialization.load_pem_parameters(resolved.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(
            f"could not load Diffie-Hellman parameters from {resolved}: {exc}"
        ) from exc
    numbers = params.parameter_numbers()
    return numbers.p, numbers.g


@dataclass(frozen=True)
class LiveSessionToken:
    """A derived live session token and its expiry."""

    token: bytes
    expires_at: float

    @property
    def base64(self) -> str:
        return base64.b64encode(self.token).decode("ascii")

    def is_valid(self, *, refresh_margin_seconds: float, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current < (self.expires_at - refresh_margin_seconds)


class OAuthSession:
    """Signs requests and derives the live session token.

    Stateless with respect to the network: it produces headers and parses the
    live-session-token response, so it can be unit-tested without an HTTP call.
    """

    def __init__(self, credentials: OAuthCredentials, config: Config) -> None:
        self._credentials = credentials
        oauth_cfg = config.section("ibkr.cp_api.oauth")
        self._realm = oauth_cfg.require_str("realm")
        self._rsa_method = oauth_cfg.require_str("signature_method_rsa")
        self._hmac_method = oauth_cfg.require_str("signature_method_hmac")
        self._token_ttl = oauth_cfg.require_float("live_session_token_ttl_seconds")
        self._refresh_margin = oauth_cfg.require_float("live_session_token_refresh_margin_seconds")
        self._nonce_bytes = oauth_cfg.require_int("nonce_bytes")
        self._live_session_token: LiveSessionToken | None = None
        self._dh_private: int | None = None

    # ── properties ──

    @property
    def realm(self) -> str:
        return self._realm

    @property
    def live_session_token(self) -> LiveSessionToken | None:
        return self._live_session_token

    def needs_live_session_token(self, *, now: float | None = None) -> bool:
        token = self._live_session_token
        return token is None or not token.is_valid(
            refresh_margin_seconds=self._refresh_margin, now=now
        )

    # ── helpers ──

    def _nonce(self) -> str:
        return secrets.token_hex(self._nonce_bytes)

    def _base_params(self) -> dict[str, str]:
        return {
            "oauth_consumer_key": self._credentials.consumer_key,
            "oauth_token": self._credentials.access_token,
            "oauth_nonce": self._nonce(),
            "oauth_timestamp": str(int(time.time())),
        }

    # ── step 1: the live-session-token request ──

    def build_live_session_token_request(
        self, url: str, *, dh_private: int | None = None
    ) -> tuple[dict[str, str], str]:
        """Return ``(headers, dh_challenge_hex)`` for POST ``/oauth/live_session_token``.

        The Diffie-Hellman challenge ``A = g^a mod p`` is signed as part of the base
        string, so the private exponent is retained for the response step.
        """
        creds = self._credentials
        # A fresh private exponent per exchange; never reused across sessions.
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
        params.update(
            {
                "oauth_signature_method": self._rsa_method,
                "diffie_hellman_challenge": challenge_hex,
            }
        )
        # The prepend precedes the base string and is NOT itself a parameter.
        base_string = prepend + build_signature_base_string("POST", url, params)
        signature = base64.b64encode(
            creds.signing_key.sign(base_string.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        ).decode("ascii")
        params["oauth_signature"] = signature

        headers = {
            "Authorization": build_authorization_header(self._realm, params),
            "User-Agent": "obliskiq-lite-mcp",
            "Host": _host_of(url),
        }
        return headers, challenge_hex

    # ── step 2: derive and verify the token ──

    def complete_live_session_token(
        self, response_body: Mapping[str, Any], *, now: float | None = None
    ) -> LiveSessionToken:
        """Derive the live session token from IBKR's DH response and verify it.

        ``K = B^a mod p``; ``LST = HMAC-SHA1(key=K, msg=prepend)``. The SHA-1 is
        IBKR's protocol, not a choice made here. The token is verified against
        ``live_session_token_signature`` before it is trusted — an unverified token
        would fail every later call with no explanation.
        """
        if self._dh_private is None:
            raise AuthError(
                "complete_live_session_token called before build_live_session_token_request"
            )
        try:
            dh_response_hex = str(response_body["diffie_hellman_response"])
            expected_signature = str(response_body["live_session_token_signature"])
        except KeyError as exc:
            raise AuthError(
                f"live session token response is missing {exc.args[0]!r}: {dict(response_body)!r}"
            ) from exc

        creds = self._credentials
        shared = pow(int(dh_response_hex, _HEX_BASE), self._dh_private, creds.dh_prime)
        shared_bytes = _to_signed_bytes(shared)
        prepend = creds.decrypt_access_token_secret()

        token = hmac.new(shared_bytes, prepend, hashlib.sha1).digest()
        computed_signature = hmac.new(
            token, creds.consumer_key.encode("utf-8"), hashlib.sha1
        ).hexdigest()

        if not hmac.compare_digest(computed_signature, expected_signature):
            raise AuthError(
                "live session token failed IBKR's signature check. On OpenSSL builds "
                "that implement implicit rejection, PKCS#1 v1.5 decryption returns "
                "random bytes rather than failing, so a mismatched "
                "OBLISK_IBKR_ENCRYPTION_KEY surfaces here rather than earlier. Verify "
                "the consumer key, the encryption key registered with IBKR, and the "
                "Diffie-Hellman parameters."
            )

        expires_at = (time.time() if now is None else now) + self._token_ttl
        self._live_session_token = LiveSessionToken(token=token, expires_at=expires_at)
        log.info("live session token derived", extra={"expires_at": expires_at})
        return self._live_session_token

    # ── step 3: sign ordinary requests ──

    def sign_request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        """Return headers signing ``method url`` HMAC-SHA256 with the live session token.

        Query parameters participate in the signature base string, so pass the same
        mapping that will go on the wire.
        """
        token = self._live_session_token
        if token is None:
            raise AuthError("no live session token; call the live_session_token flow first")

        oauth_params = self._base_params()
        oauth_params["oauth_signature_method"] = self._hmac_method

        signing_params = dict(oauth_params)
        for key, value in (params or {}).items():
            signing_params[str(key)] = str(value)

        base_string = build_signature_base_string(method, url, signing_params)
        signature = base64.b64encode(
            hmac.new(token.token, base_string.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        oauth_params["oauth_signature"] = signature

        return {
            "Authorization": build_authorization_header(self._realm, oauth_params),
            "User-Agent": "obliskiq-lite-mcp",
            "Host": _host_of(url),
            "Accept": "application/json",
        }


def _to_signed_bytes(value: int) -> bytes:
    """Big-endian bytes with a leading zero when the top bit is set.

    IBKR treats the DH shared secret as a signed big integer, so a value whose
    high bit is set carries a leading zero byte. Omitting it derives a different
    token and every later call fails authentication for no visible reason.
    """
    length = (value.bit_length() + _BITS_PER_BYTE) // _BITS_PER_BYTE
    return value.to_bytes(length, "big")


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc
