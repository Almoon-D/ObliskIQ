"""OAuth 1.0a signing, the Diffie-Hellman live-session-token exchange and its lifecycle.

Adapted from ObliskIQ-lite-MCP's tests/test_oauth.py and tests/test_oauth_lifecycle.py. The
crypto is pinned where it is deterministic: RFC 3986 encoding, signature base strings, the DH
derivation against a locally simulated IBKR, the Java ``BigInteger`` encoding of the shared
secret, and the refusal to trust a token whose signature does not check out. Around it: loading
credentials from the environment and the key files it names, expiry and the refresh margin, and
the transport that renegotiates the token before it expires. HTTP is mocked with respx.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dh, ec, padding, rsa

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_ibkr import IbkrConfig
from obliskiq.env import Environment
from obliskiq.errors import ConfigError, EnvVarError, UpstreamError
from obliskiq.ibkr.errors import ERROR_EXCERPT_CHARS, AuthError, IbkrUpstreamError
from obliskiq.ibkr.transport import oauth as oauth_module
from obliskiq.ibkr.transport.base import TransportKind
from obliskiq.ibkr.transport.oauth import (
    LiveSessionToken,
    OAuthCredentials,
    OAuthSession,
    OAuthTransport,
    build_authorization_header,
    build_signature_base_string,
    negotiate_live_session_token,
    percent_encode,
    to_signed_bytes,
)
from obliskiq.util.http import RetryPolicy

# The 1024-bit MODP group of RFC 2409 (Oakley group 2): enough to exercise the exchange
# arithmetic without generating parameters, and exactly the shipped dh_prime_bits_min.
TEST_PRIME = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E08"
    "8A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
    "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9"
    "A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE6"
    "49286651ECE65381FFFFFFFFFFFFFFFF",
    16,
)
TEST_GENERATOR = 2
PREPEND = b"\x01\x02\x03\x04prepend-secret"
CONSUMER_KEY = "TESTCONSUMER"
ACCESS_TOKEN = "ACCESSTOKEN"

ORIGIN = "https://api.ibkr.test"
API_URL = ORIGIN + "/v1/api"
TOKEN_URL = API_URL + "/oauth/live_session_token"

#: A fixed instant for token arithmetic, so expiry tests never read the wall clock.
T0 = 1_800_000_000.0

AUTH_PARAM_RE = re.compile(r'([A-Za-z0-9_%.~-]+)="([^"]*)"')


# ── keys, parameters and credentials ──


@dataclasses.dataclass(frozen=True)
class Keys:
    signing: rsa.RSAPrivateKey
    encryption: rsa.RSAPrivateKey
    stranger: rsa.RSAPrivateKey


@pytest.fixture(scope="module")
def keys() -> Keys:
    """Three RSA keys, generated once: signing, encryption and one IBKR never saw."""
    return Keys(*(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3)))


@pytest.fixture(scope="module")
def dh_params() -> dh.DHParameters:
    return dh.DHParameterNumbers(TEST_PRIME, TEST_GENERATOR).parameters()


def _encrypted_secret(key: rsa.RSAPrivateKey, plaintext: bytes = PREPEND) -> str:
    """The access-token secret as IBKR issues it: base64 of PKCS#1 v1.5 ciphertext."""
    return base64.b64encode(key.public_key().encrypt(plaintext, padding.PKCS1v15())).decode()


@pytest.fixture(scope="module")
def credentials(keys: Keys) -> OAuthCredentials:
    return OAuthCredentials(
        consumer_key=CONSUMER_KEY,
        access_token=ACCESS_TOKEN,
        access_token_secret=_encrypted_secret(keys.encryption),
        signing_key=keys.signing,
        encryption_key=keys.encryption,
        dh_prime=TEST_PRIME,
        dh_generator=TEST_GENERATOR,
    )


@pytest.fixture
def ibkr(shipped_settings: ObliskConfig) -> IbkrConfig:
    return shipped_settings.ibkr


@pytest.fixture
def session(credentials: OAuthCredentials, ibkr: IbkrConfig) -> OAuthSession:
    return OAuthSession(credentials, ibkr)


def _ibkr_with(ibkr: IbkrConfig, **oauth: Any) -> IbkrConfig:
    return ibkr.model_copy(update={"oauth": ibkr.oauth.model_copy(update=oauth)})


def _write_key(path: Path, key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey) -> Path:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


def _write_dh(path: Path, params: dh.DHParameters) -> Path:
    path.write_bytes(
        params.parameter_bytes(serialization.Encoding.PEM, serialization.ParameterFormat.PKCS3)
    )
    return path


@pytest.fixture
def key_files(tmp_path: Path, keys: Keys, dh_params: dh.DHParameters) -> dict[str, Path]:
    return {
        "signing": _write_key(tmp_path / "signing.pem", keys.signing),
        "encryption": _write_key(tmp_path / "encryption.pem", keys.encryption),
        "dh": _write_dh(tmp_path / "dhparam.pem", dh_params),
    }


#: Each OAuth variable and the key_files entry (or literal) it is set from.
OAUTH_VARIABLES = (
    "OBLISK_IBKR_CONSUMER_KEY",
    "OBLISK_IBKR_ACCESS_TOKEN",
    "OBLISK_IBKR_ACCESS_TOKEN_SECRET",
    "OBLISK_IBKR_SIGNATURE_KEY",
    "OBLISK_IBKR_ENCRYPTION_KEY",
    "OBLISK_IBKR_DH_PARAM",
)


@pytest.fixture
def set_oauth_env(
    monkeypatch: pytest.MonkeyPatch, key_files: dict[str, Path], keys: Keys
) -> Callable[..., Environment]:
    """Set the six OAuth variables (minus ``omit``) and read the environment."""

    def build(*omit: str) -> Environment:
        values = {
            "OBLISK_IBKR_CONSUMER_KEY": CONSUMER_KEY,
            "OBLISK_IBKR_ACCESS_TOKEN": ACCESS_TOKEN,
            "OBLISK_IBKR_ACCESS_TOKEN_SECRET": _encrypted_secret(keys.encryption),
            "OBLISK_IBKR_SIGNATURE_KEY": str(key_files["signing"]),
            "OBLISK_IBKR_ENCRYPTION_KEY": str(key_files["encryption"]),
            "OBLISK_IBKR_DH_PARAM": str(key_files["dh"]),
        }
        for name, value in values.items():
            if name in omit:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return Environment(_env_file=None)

    return build


# ── IBKR's side of the exchange ──


def _simulate_ibkr(
    credentials: OAuthCredentials,
    challenge_hex: str,
    *,
    prepend: bytes = PREPEND,
    server_private: int = 7919,
) -> tuple[dict[str, Any], bytes]:
    """Play IBKR's side of the exchange: return its response body and the token it derived."""
    server_public = pow(credentials.dh_generator, server_private, credentials.dh_prime)
    shared = pow(int(challenge_hex, 16), server_private, credentials.dh_prime)
    shared_bytes = shared.to_bytes((shared.bit_length() + 8) // 8, "big")
    token = hmac.new(shared_bytes, prepend, hashlib.sha1).digest()
    signature = hmac.new(token, credentials.consumer_key.encode(), hashlib.sha1).hexdigest()
    return {
        "diffie_hellman_response": format(server_public, "x"),
        "live_session_token_signature": signature,
        "live_session_token_expiration": 0,
    }, token


def _auth_params(header: str) -> dict[str, str]:
    """The ``key="value"`` pairs of an ``Authorization: OAuth`` header, percent-decoded."""
    assert header.startswith("OAuth ")
    return {unquote(k): unquote(v) for k, v in AUTH_PARAM_RE.findall(header)}


def _install_token(
    session: OAuthSession, credentials: OAuthCredentials, *, now: float | None = None
) -> bytes:
    """Run a full exchange against the simulated IBKR; return the token IBKR derived."""
    _, challenge = session.build_live_session_token_request(TOKEN_URL, dh_private=4242)
    body, token = _simulate_ibkr(credentials, challenge)
    session.complete_live_session_token(body, now=now)
    return token


# ── RFC 3986 encoding ──


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [
        ("abc", "abc"),
        ("a b", "a%20b"),
        ("a+b", "a%2Bb"),
        ("~-._", "~-._"),
        ("/", "%2F"),
        ("=", "%3D"),
        ("&", "%26"),
        ("ñ", "%C3%B1"),
    ],
)
def test_percent_encoding_follows_rfc_3986(raw: str, encoded: str) -> None:
    """'+' is not a space and only the unreserved set passes; anything else breaks signatures."""
    assert percent_encode(raw) == encoded


def test_signature_base_string_sorts_and_double_encodes() -> None:
    """Parameters are sorted, encoded, joined and then encoded again as one component."""
    base = build_signature_base_string("get", API_URL + "/x", {"b": "2", "a": "1 x"})
    assert base.startswith("GET&")
    assert base == "GET&https%3A%2F%2Fapi.ibkr.test%2Fv1%2Fapi%2Fx&a%3D1%2520x%26b%3D2"


def test_authorization_header_is_quoted_and_realmed() -> None:
    """The realm leads and every value is quoted and percent-encoded."""
    header = build_authorization_header("limited_poa", {"oauth_nonce": "ab c"})
    assert header.startswith('OAuth realm="limited_poa", ')
    assert 'oauth_nonce="ab%20c"' in header


# ── credentials ──


def test_access_token_secret_round_trips(credentials: OAuthCredentials) -> None:
    """The secret decrypts to the prepend it was encrypted from."""
    assert credentials.decrypt_access_token_secret() == PREPEND


def test_a_malformed_access_token_secret_is_reported(credentials: OAuthCredentials) -> None:
    """The error names the variable to fix."""
    broken = dataclasses.replace(credentials, access_token_secret="not-base64-ciphertext")
    with pytest.raises(AuthError, match="OBLISK_IBKR_ACCESS_TOKEN_SECRET"):
        broken.decrypt_access_token_secret()


def test_credentials_and_tokens_never_print_their_material(
    credentials: OAuthCredentials,
) -> None:
    """A repr in a log line or a traceback carries no key, secret or token."""
    assert repr(credentials) == "OAuthCredentials(<redacted>)"
    token = LiveSessionToken(token=b"\xaa" * 20, expires_at=T0)
    assert repr(token) == f"LiveSessionToken(<redacted>, expires_at={T0:.0f})"


def test_a_wrong_encryption_key_never_yields_a_session(
    credentials: OAuthCredentials, keys: Keys, ibkr: IbkrConfig
) -> None:
    """A mismatched encryption key must never produce a working session token.

    Which layer catches it varies by OpenSSL build: where implicit rejection is implemented as
    a Bleichenbacher countermeasure, PKCS#1 v1.5 decryption returns random bytes and the
    live-session-token signature check catches the mismatch; elsewhere decryption raises. The
    invariant asserted is the one that matters: no path ends in a usable token.
    """
    wrong_key = dataclasses.replace(credentials, encryption_key=keys.stranger)
    session = OAuthSession(wrong_key, ibkr)

    def attempt() -> None:
        _, challenge = session.build_live_session_token_request(TOKEN_URL, dh_private=12345)
        # Reached only where decryption returned random bytes rather than raising. IBKR
        # derives the token from the REAL prepend, so the signature check must reject ours.
        body, _ = _simulate_ibkr(credentials, challenge)
        session.complete_live_session_token(body)

    with pytest.raises(AuthError):
        attempt()

    assert session.live_session_token is None
    assert session.needs_live_session_token() is True


def test_three_valid_key_files_load_into_credentials(
    key_files: dict[str, Path], ibkr: IbkrConfig, keys: Keys
) -> None:
    """The success path of from_files: real PEM files in, usable credentials out."""
    credentials = OAuthCredentials.from_files(
        consumer_key=CONSUMER_KEY,
        access_token=ACCESS_TOKEN,
        access_token_secret=_encrypted_secret(keys.encryption),
        signing_key_path=key_files["signing"],
        encryption_key_path=key_files["encryption"],
        dh_param_path=key_files["dh"],
        ibkr=ibkr,
    )
    assert credentials.consumer_key == CONSUMER_KEY
    assert credentials.dh_generator == TEST_GENERATOR
    assert credentials.dh_prime == TEST_PRIME
    assert credentials.signing_key.private_numbers() == keys.signing.private_numbers()
    assert credentials.encryption_key.private_numbers() == keys.encryption.private_numbers()
    assert credentials.decrypt_access_token_secret() == PREPEND


def test_a_short_dh_prime_is_refused(key_files: dict[str, Path], ibkr: IbkrConfig) -> None:
    """A prime below the configured minimum is a downgrade, not a warning."""
    stricter = _ibkr_with(ibkr, dh_prime_bits_min=TEST_PRIME.bit_length() + 1)
    with pytest.raises(ConfigError, match="below the configured minimum"):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret="",
            signing_key_path=key_files["signing"],
            encryption_key_path=key_files["encryption"],
            dh_param_path=key_files["dh"],
            ibkr=stricter,
        )


def test_a_prime_exactly_at_the_minimum_is_accepted(
    key_files: dict[str, Path], ibkr: IbkrConfig
) -> None:
    """The minimum is inclusive: the boundary itself loads."""
    exact = _ibkr_with(ibkr, dh_prime_bits_min=TEST_PRIME.bit_length())
    credentials = OAuthCredentials.from_files(
        consumer_key="K",
        access_token="T",
        access_token_secret="",
        signing_key_path=key_files["signing"],
        encryption_key_path=key_files["encryption"],
        dh_param_path=key_files["dh"],
        ibkr=exact,
    )
    assert credentials.dh_prime.bit_length() == exact.oauth.dh_prime_bits_min


def test_a_non_rsa_key_file_is_rejected(tmp_path: Path, ibkr: IbkrConfig) -> None:
    """A file that is not a PEM key at all is refused with a load error."""
    junk = tmp_path / "notakey.pem"
    junk.write_text("-----BEGIN NONSENSE-----\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not load"):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret="",
            signing_key_path=junk,
            encryption_key_path=junk,
            dh_param_path=junk,
            ibkr=ibkr,
        )


def test_credentials_from_the_environment_load_the_same_way(
    set_oauth_env: Callable[..., Environment], ibkr: IbkrConfig
) -> None:
    """from_environment reads the six variables and loads the files they name."""
    credentials = OAuthCredentials.from_environment(set_oauth_env(), ibkr)
    assert credentials.consumer_key == CONSUMER_KEY
    assert credentials.access_token == ACCESS_TOKEN
    assert credentials.decrypt_access_token_secret() == PREPEND


@pytest.mark.parametrize("variable", OAUTH_VARIABLES)
def test_each_missing_variable_is_named_by_its_own_name(
    set_oauth_env: Callable[..., Environment], ibkr: IbkrConfig, variable: str
) -> None:
    """An unattended process must say which variable is absent, not just that one is."""
    env = set_oauth_env(variable)
    with pytest.raises(EnvVarError, match=rf"\b{variable}\b") as raised:
        OAuthCredentials.from_environment(env, ibkr)
    named = {name for name in OAUTH_VARIABLES if re.search(rf"\b{name}\b", str(raised.value))}
    assert named == {variable}


def test_several_missing_variables_are_all_named_at_once(
    set_oauth_env: Callable[..., Environment], ibkr: IbkrConfig
) -> None:
    """Every absent variable is in the one error, so one fix round suffices."""
    env = set_oauth_env("OBLISK_IBKR_CONSUMER_KEY", "OBLISK_IBKR_DH_PARAM")
    with pytest.raises(EnvVarError) as raised:
        OAuthCredentials.from_environment(env, ibkr)
    assert "OBLISK_IBKR_CONSUMER_KEY" in str(raised.value)
    assert "OBLISK_IBKR_DH_PARAM" in str(raised.value)


def test_a_missing_variable_error_never_echoes_a_present_value(
    set_oauth_env: Callable[..., Environment], ibkr: IbkrConfig
) -> None:
    """The variables that ARE set stay out of the message."""
    env = set_oauth_env("OBLISK_IBKR_DH_PARAM")
    with pytest.raises(EnvVarError) as raised:
        OAuthCredentials.from_environment(env, ibkr)
    assert CONSUMER_KEY not in str(raised.value)
    assert ACCESS_TOKEN not in str(raised.value)


# ── rotation and failure on the key files themselves ──


def test_a_key_file_that_is_not_there_is_refused_by_name(
    tmp_path: Path, key_files: dict[str, Path], ibkr: IbkrConfig
) -> None:
    """Distinct from a malformed file: the missing-file branch names the path."""
    absent = tmp_path / "rotated-away.pem"
    assert not absent.exists()
    with pytest.raises(ConfigError, match="rotated-away"):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret="",
            signing_key_path=absent,
            encryption_key_path=key_files["encryption"],
            dh_param_path=key_files["dh"],
            ibkr=ibkr,
        )


def test_a_malformed_dh_parameter_file_is_refused(
    tmp_path: Path, key_files: dict[str, Path], ibkr: IbkrConfig
) -> None:
    """Valid RSA keys in both slots, so the DH loader is the one that runs and refuses."""
    broken = tmp_path / "dhparam-broken.pem"
    broken.write_text(
        "-----BEGIN DH PARAMETERS-----\nnot base64\n-----END DH PARAMETERS-----\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Diffie-Hellman parameters"):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret="",
            signing_key_path=key_files["signing"],
            encryption_key_path=key_files["encryption"],
            dh_param_path=broken,
            ibkr=ibkr,
        )


def test_an_elliptic_curve_key_in_an_rsa_slot_is_refused(
    tmp_path: Path, key_files: dict[str, Path], ibkr: IbkrConfig
) -> None:
    """A rotation to the wrong algorithm is a plausible operator error."""
    path = _write_key(tmp_path / "ec.pem", ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(ConfigError, match="not an RSA private key"):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret="",
            signing_key_path=path,
            encryption_key_path=key_files["encryption"],
            dh_param_path=key_files["dh"],
            ibkr=ibkr,
        )


def test_rotating_the_files_on_disk_is_not_picked_up_by_loaded_credentials(
    set_oauth_env: Callable[..., Environment],
    key_files: dict[str, Path],
    keys: Keys,
    ibkr: IbkrConfig,
) -> None:
    """Credentials are read once; replacing the PEMs takes effect only on the next load."""
    env = set_oauth_env()
    first = OAuthCredentials.from_environment(env, ibkr)
    _write_key(key_files["signing"], keys.stranger)

    assert first.signing_key.private_numbers() == keys.signing.private_numbers()
    reloaded = OAuthCredentials.from_environment(env, ibkr)
    assert reloaded.signing_key.private_numbers() == keys.stranger.private_numbers()


# ── the live-session-token exchange ──


def test_the_exchange_derives_the_same_token_as_the_counterparty(
    session: OAuthSession, credentials: OAuthCredentials, ibkr: IbkrConfig
) -> None:
    """Both sides of the DH exchange arrive at one token, valid for the configured TTL."""
    _, challenge = session.build_live_session_token_request(TOKEN_URL, dh_private=12345)
    body, expected_token = _simulate_ibkr(credentials, challenge)
    derived = session.complete_live_session_token(body, now=T0)
    assert derived.token == expected_token
    assert derived.expires_at == T0 + ibkr.oauth.live_session_token_ttl_seconds
    assert session.live_session_token == derived


def test_a_token_failing_ibkrs_signature_check_is_refused(
    session: OAuthSession, credentials: OAuthCredentials
) -> None:
    """An unverified token fails every later call with no explanation. Catch it here."""
    _, challenge = session.build_live_session_token_request(TOKEN_URL, dh_private=12345)
    body, _ = _simulate_ibkr(credentials, challenge)
    body["live_session_token_signature"] = "00" * 20
    with pytest.raises(AuthError, match="signature check"):
        session.complete_live_session_token(body)
    assert session.live_session_token is None


def test_a_token_from_a_different_prepend_is_refused(
    session: OAuthSession, credentials: OAuthCredentials
) -> None:
    """IBKR signing over a token derived from another prepend never verifies."""
    _, challenge = session.build_live_session_token_request(TOKEN_URL, dh_private=12345)
    body, _ = _simulate_ibkr(credentials, challenge, prepend=b"another prepend")
    with pytest.raises(AuthError, match="signature check"):
        session.complete_live_session_token(body)
    assert session.live_session_token is None


def test_a_malformed_exchange_response_names_the_missing_field(session: OAuthSession) -> None:
    """A response without the DH value says which field it lacks."""
    session.build_live_session_token_request(TOKEN_URL, dh_private=12345)
    with pytest.raises(AuthError, match="diffie_hellman_response"):
        session.complete_live_session_token({"live_session_token_signature": "ab"})


def test_a_non_hex_dh_response_is_refused(session: OAuthSession) -> None:
    """The DH value must be hexadecimal; anything else is an AuthError, not a ValueError."""
    session.build_live_session_token_request(TOKEN_URL, dh_private=12345)
    with pytest.raises(AuthError, match="not hexadecimal"):
        session.complete_live_session_token(
            {"diffie_hellman_response": "xyz", "live_session_token_signature": "ab"}
        )


def test_completing_before_requesting_is_an_error(session: OAuthSession) -> None:
    """Without a private exponent there is nothing to derive from."""
    with pytest.raises(AuthError, match="before build_live_session_token_request"):
        session.complete_live_session_token(
            {"diffie_hellman_response": "1", "live_session_token_signature": "2"}
        )


def test_the_request_header_is_rsa_signed_and_carries_the_challenge(
    session: OAuthSession, credentials: OAuthCredentials, ibkr: IbkrConfig, keys: Keys
) -> None:
    """The request carries A = g^a mod p and an RSA-SHA256 signature that verifies."""
    headers, challenge = session.build_live_session_token_request(TOKEN_URL, dh_private=999)
    params = _auth_params(headers["Authorization"])
    assert params["oauth_signature_method"] == ibkr.oauth.signature_method_rsa
    assert params["realm"] == ibkr.oauth.realm
    assert params["diffie_hellman_challenge"] == challenge
    assert headers["Host"] == "api.ibkr.test"
    assert headers["User-Agent"] == ibkr.oauth.user_agent
    assert challenge == format(pow(TEST_GENERATOR, 999, TEST_PRIME), "x")

    # The signature check: the prepend (hex) precedes the base string, signed by the
    # signing key. A signature over anything else would be rejected by IBKR.
    signature = base64.b64decode(params.pop("oauth_signature"))
    params.pop("realm")
    base = PREPEND.hex() + build_signature_base_string("POST", TOKEN_URL, params)
    keys.signing.public_key().verify(signature, base.encode(), padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(InvalidSignature):
        keys.signing.public_key().verify(
            signature, base[len(PREPEND.hex()) :].encode(), padding.PKCS1v15(), hashes.SHA256()
        )


def test_each_exchange_uses_a_fresh_private_exponent(session: OAuthSession) -> None:
    """Reusing the exponent would make every token derivable from one exchange."""
    challenges = {session.build_live_session_token_request(TOKEN_URL)[1] for _ in range(5)}
    assert len(challenges) == 5


# ── signing ordinary requests ──


def test_signing_requires_a_live_session_token(session: OAuthSession) -> None:
    """No token, no signature: never a request signed with nothing."""
    with pytest.raises(AuthError, match="no live session token"):
        session.sign_request("GET", API_URL + "/portfolio/accounts")


def test_signed_requests_use_hmac_sha256_over_the_token(
    session: OAuthSession, credentials: OAuthCredentials, ibkr: IbkrConfig
) -> None:
    """The signature is HMAC-SHA256 of the base string, keyed by the verified token."""
    token = _install_token(session, credentials)
    url = API_URL + "/iserver/marketdata/snapshot"
    query = {"conids": "265598", "fields": "31,84"}
    headers = session.sign_request("GET", url, params=query)

    params = _auth_params(headers["Authorization"])
    assert params["oauth_signature_method"] == ibkr.oauth.signature_method_hmac
    assert params["oauth_consumer_key"] == CONSUMER_KEY
    assert params["oauth_token"] == ACCESS_TOKEN
    assert headers["Accept"] == "application/json"

    signature = params.pop("oauth_signature")
    params.pop("realm")
    base = build_signature_base_string("GET", url, {**params, **query})
    expected = base64.b64encode(hmac.new(token, base.encode(), hashlib.sha256).digest()).decode()
    assert hmac.compare_digest(signature, expected)


def test_query_parameters_change_the_signature(
    session: OAuthSession, credentials: OAuthCredentials
) -> None:
    """Parameters take part in the base string; a signature ignoring them would be refused."""
    _install_token(session, credentials)
    url = API_URL + "/iserver/marketdata/snapshot"
    first = session.sign_request("GET", url, params={"conids": "1"})
    second = session.sign_request("GET", url, params={"conids": "2"})
    assert first["Authorization"] != second["Authorization"]


def test_nonces_do_not_repeat(session: OAuthSession, credentials: OAuthCredentials) -> None:
    """Fifty signatures in a row carry fifty distinct nonces of the configured size."""
    _install_token(session, credentials)
    nonces = {
        _auth_params(session.sign_request("POST", API_URL + "/tickle")["Authorization"])[
            "oauth_nonce"
        ]
        for _ in range(50)
    }
    assert len(nonces) == 50


def test_nonce_length_follows_the_configuration(
    credentials: OAuthCredentials, ibkr: IbkrConfig
) -> None:
    """The nonce carries nonce_bytes of randomness (hex, so twice as many characters)."""
    longer = _ibkr_with(ibkr, nonce_bytes=ibkr.oauth.nonce_bytes * 2)
    session = OAuthSession(credentials, longer)
    _install_token(session, credentials)
    header = session.sign_request("GET", API_URL + "/tickle")["Authorization"]
    assert len(_auth_params(header)["oauth_nonce"]) == 2 * longer.oauth.nonce_bytes


# ── expiry and the refresh margin ──


def test_the_token_expiry_is_respected(
    session: OAuthSession, credentials: OAuthCredentials, ibkr: IbkrConfig
) -> None:
    """No token needs one; a fresh one does not; one past its TTL does."""
    assert session.needs_live_session_token(now=T0) is True
    _install_token(session, credentials, now=T0)
    assert session.needs_live_session_token(now=T0) is False
    ttl = ibkr.oauth.live_session_token_ttl_seconds
    assert session.needs_live_session_token(now=T0 + ttl + 1) is True


def test_a_token_inside_its_refresh_margin_needs_replacing(
    session: OAuthSession, credentials: OAuthCredentials, ibkr: IbkrConfig
) -> None:
    """Renewal starts refresh_margin seconds before expiry, not at it."""
    _install_token(session, credentials, now=T0)
    expires_at = T0 + ibkr.oauth.live_session_token_ttl_seconds
    margin = ibkr.oauth.live_session_token_refresh_margin_seconds

    assert session.needs_live_session_token(now=expires_at - margin - 1) is False
    assert session.needs_live_session_token(now=expires_at - margin + 1) is True
    assert session.needs_live_session_token(now=expires_at + 1000) is True


def test_live_session_token_validity_is_strict_at_the_margin() -> None:
    """At exactly expires_at - margin the token is no longer valid."""
    token = LiveSessionToken(token=b"x" * 20, expires_at=T0)
    assert token.is_valid(refresh_margin_seconds=60.0, now=T0 - 61) is True
    assert token.is_valid(refresh_margin_seconds=60.0, now=T0 - 60) is False


def test_the_session_signs_with_a_stale_token_and_leaves_renewal_to_the_transport(
    session: OAuthSession, credentials: OAuthCredentials, ibkr: IbkrConfig
) -> None:
    """OAuthSession is mechanical: it signs with what it holds and reports staleness.

    In lite nothing consulted needs_live_session_token after connect, so a process alive past
    the TTL kept signing with a dropped token. Renewal now belongs to OAuthTransport.headers
    (tested below); the session itself still signs, so the transport must ask first.
    """
    expired_at = time.time() - 1
    _install_token(session, credentials, now=expired_at - ibkr.oauth.live_session_token_ttl_seconds)
    assert session.needs_live_session_token() is True
    headers = session.sign_request("GET", API_URL + "/one")
    assert "oauth_signature" in _auth_params(headers["Authorization"])


# ── the signed big integer, against an INDEPENDENT reference ──


def _java_biginteger_to_byte_array(value: int) -> bytes:
    """Java's ``BigInteger.toByteArray()`` for a non-negative value.

    What IBKR's specification means by a signed big integer, written independently of
    ``to_signed_bytes`` on purpose: ``_simulate_ibkr`` re-implements the production formula
    inline, so the round trip agrees with the code by construction. The definition: the minimal
    two's-complement big-endian encoding, which carries a leading 0x00 exactly when the most
    significant bit of the minimal UNSIGNED encoding is set.
    """
    if value == 0:
        return b"\x00"
    unsigned = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return b"\x00" + unsigned if unsigned[0] & 0x80 else unsigned


@pytest.mark.parametrize(
    "value",
    [
        0,
        1,
        0x7F,  # high bit clear at one byte
        0x80,  # high bit SET: needs the leading zero
        0xFF,
        0x0100,
        0x7FFF,
        0x8000,  # high bit set at two bytes
        2**2039,  # 255 bytes exactly, top bit set
        2**2040 - 1,  # 255 bytes, top bit set
        2**2046 + 12345,  # 256 bytes, top bit clear
        2**2047,  # 2048 bits, top bit set: 257 bytes
        2**2048 - 1,  # every bit set
    ],
)
def test_the_dh_shared_secret_is_encoded_as_a_signed_big_integer(value: int) -> None:
    """to_signed_bytes matches Java's BigInteger.toByteArray()."""
    assert to_signed_bytes(value) == _java_biginteger_to_byte_array(value)


def test_a_high_bit_value_really_does_carry_the_leading_zero() -> None:
    """The case the whole convention exists for, stated explicitly."""
    encoded = to_signed_bytes(2**2047)
    assert len(encoded) == 257
    assert encoded[0] == 0x00
    assert encoded[1] == 0x80


def test_a_value_whose_high_bit_is_clear_carries_no_padding() -> None:
    """Padding unconditionally would be just as wrong, in the other direction."""
    encoded = to_signed_bytes(2**2046)
    assert len(encoded) == 256
    assert encoded[0] == 0x40


def test_omitting_the_leading_zero_derives_a_different_token() -> None:
    """Why this matters: the failure is silent and total.

    A token derived from the unsigned encoding is a well-formed 20-byte HMAC that is not the one
    IBKR derived, so every later call fails authentication. The signature check is what turns
    that into a diagnosable failure at the handshake.
    """
    shared = 2**2047 + 7919
    prepend = b"\xde\xad\xbe\xef"

    signed = hmac.new(to_signed_bytes(shared), prepend, hashlib.sha1).digest()
    unsigned = hmac.new(
        shared.to_bytes((shared.bit_length() + 7) // 8, "big"), prepend, hashlib.sha1
    ).digest()

    assert signed != unsigned
    assert len(signed) == len(unsigned) == 20, "both are well-formed; only one is right"


# ── negotiate_live_session_token and OAuthTransport, over respx ──


@pytest.fixture
def policy(shipped_settings: ObliskConfig) -> RetryPolicy:
    """The shipped policy without retries or waits: one attempt per call."""
    return dataclasses.replace(
        RetryPolicy.from_settings(shipped_settings.http),
        retries=0,
        base_delay_seconds=0.0,
        max_delay_seconds=0.0,
        jitter_seconds=0.0,
    )


def _ibkr_answers(credentials: OAuthCredentials) -> Callable[[httpx.Request], httpx.Response]:
    """A respx side effect that answers each exchange as IBKR would."""

    def answer(request: httpx.Request) -> httpx.Response:
        challenge = _auth_params(request.headers["Authorization"])["diffie_hellman_challenge"]
        body, _ = _simulate_ibkr(credentials, challenge)
        return httpx.Response(200, json=body)

    return answer


@pytest.fixture
def make_transport(
    session: OAuthSession, ibkr: IbkrConfig, policy: RetryPolicy
) -> Callable[[httpx.AsyncClient], OAuthTransport]:
    def build(client: httpx.AsyncClient) -> OAuthTransport:
        return OAuthTransport(
            session,
            origin=ORIGIN,
            base_path=ibkr.base_path,
            token_path=ibkr.oauth.live_session_token_path,
            timeout_seconds=policy.timeout_seconds,
            policy=policy,
            client=client,
        )

    return build


async def test_the_transport_token_url_comes_from_configuration(
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport], ibkr: IbkrConfig
) -> None:
    """Origin, base path and token path compose the URL the respx routes below mock."""
    assert ORIGIN + ibkr.base_path + ibkr.oauth.live_session_token_path == TOKEN_URL
    async with httpx.AsyncClient() as client:
        transport = make_transport(client)
        assert transport.base_url == API_URL
        assert transport.kind is TransportKind.OAUTH


async def test_negotiation_installs_a_verified_token(
    respx_mock: Any, session: OAuthSession, credentials: OAuthCredentials, policy: RetryPolicy
) -> None:
    """The happy path over HTTP: one POST, one verified token."""
    route = respx_mock.post(TOKEN_URL).mock(side_effect=_ibkr_answers(credentials))
    async with httpx.AsyncClient() as client:
        token = await negotiate_live_session_token(session, TOKEN_URL, client, policy)
    assert route.call_count == 1
    assert session.live_session_token == token


async def test_a_non_200_answer_raises_without_echoing_the_body_beyond_the_excerpt(
    respx_mock: Any, session: OAuthSession, policy: RetryPolicy
) -> None:
    """The status and an excerpt are named; the rest of the body never reaches the message."""
    tail = "TAIL-THAT-MUST-NOT-APPEAR"
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, text="x" * ERROR_EXCERPT_CHARS + tail)
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthError) as raised:
            await negotiate_live_session_token(session, TOKEN_URL, client, policy)
    message = str(raised.value)
    assert "401" in message
    assert "x" * ERROR_EXCERPT_CHARS in message
    assert tail not in message
    assert isinstance(raised.value, UpstreamError)
    assert session.live_session_token is None


async def test_a_non_json_answer_raises(
    respx_mock: Any, session: OAuthSession, policy: RetryPolicy
) -> None:
    """A maintenance page with HTTP 200 is not a token."""
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthError, match="non-JSON"):
            await negotiate_live_session_token(session, TOKEN_URL, client, policy)
    assert session.live_session_token is None


async def test_a_json_answer_that_is_not_an_object_raises(
    respx_mock: Any, session: OAuthSession, policy: RetryPolicy
) -> None:
    """A JSON array has no fields to derive from."""
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthError, match="not an object"):
            await negotiate_live_session_token(session, TOKEN_URL, client, policy)


async def test_a_tampered_answer_over_http_never_installs_a_token(
    respx_mock: Any, session: OAuthSession, credentials: OAuthCredentials, policy: RetryPolicy
) -> None:
    """The signature check runs on the HTTP path too."""

    def tampered(request: httpx.Request) -> httpx.Response:
        challenge = _auth_params(request.headers["Authorization"])["diffie_hellman_challenge"]
        body, _ = _simulate_ibkr(credentials, challenge)
        body["live_session_token_signature"] = "00" * 20
        return httpx.Response(200, json=body)

    respx_mock.post(TOKEN_URL).mock(side_effect=tampered)
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthError, match="signature check"):
            await negotiate_live_session_token(session, TOKEN_URL, client, policy)
    assert session.live_session_token is None


async def test_headers_negotiate_a_token_when_there_is_none(
    respx_mock: Any,
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport],
    credentials: OAuthCredentials,
    session: OAuthSession,
) -> None:
    """The first signed request negotiates; the next reuses the fresh token."""
    route = respx_mock.post(TOKEN_URL).mock(side_effect=_ibkr_answers(credentials))
    async with httpx.AsyncClient() as client:
        transport = make_transport(client)
        first = await transport.headers("GET", API_URL + "/one", {})
        second = await transport.headers("GET", API_URL + "/two", {})
    assert route.call_count == 1
    assert "oauth_signature" in _auth_params(first["Authorization"])
    assert "oauth_signature" in _auth_params(second["Authorization"])
    assert session.needs_live_session_token() is False


async def test_headers_renegotiate_a_token_inside_its_refresh_margin(
    respx_mock: Any,
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport],
    credentials: OAuthCredentials,
    session: OAuthSession,
    ibkr: IbkrConfig,
) -> None:
    """A token that will expire within the margin is replaced before it is used."""
    oauth = ibkr.oauth
    # Derived so that it expires half a margin from now: valid, but inside the margin.
    ttl = oauth.live_session_token_ttl_seconds
    margin = oauth.live_session_token_refresh_margin_seconds
    issued = time.time() - ttl + margin / 2
    _install_token(session, credentials, now=issued)
    stale = session.live_session_token
    assert stale is not None
    assert session.needs_live_session_token() is True

    route = respx_mock.post(TOKEN_URL).mock(side_effect=_ibkr_answers(credentials))
    async with httpx.AsyncClient() as client:
        await make_transport(client).headers("GET", API_URL + "/one", {})
    assert route.call_count == 1
    renewed = session.live_session_token
    assert renewed is not None
    assert renewed.expires_at > stale.expires_at
    assert session.needs_live_session_token() is False


async def test_headers_keep_a_fresh_token(
    respx_mock: Any,
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport],
    credentials: OAuthCredentials,
    session: OAuthSession,
) -> None:
    """A token outside its margin is used as is: no exchange per request."""
    _install_token(session, credentials)
    route = respx_mock.post(TOKEN_URL).mock(side_effect=_ibkr_answers(credentials))
    async with httpx.AsyncClient() as client:
        transport = make_transport(client)
        await transport.prepare()
        await transport.headers("GET", API_URL + "/one", {"a": "1"})
    assert route.call_count == 0


async def test_recover_forces_renegotiation_of_a_fresh_token(
    respx_mock: Any,
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport],
    credentials: OAuthCredentials,
    session: OAuthSession,
) -> None:
    """After a 401 the token is distrusted even if its clock says it is fine."""
    _install_token(session, credentials, now=T0 + 1)
    before = session.live_session_token
    route = respx_mock.post(TOKEN_URL).mock(side_effect=_ibkr_answers(credentials))
    async with httpx.AsyncClient() as client:
        await make_transport(client).recover()
    assert route.call_count == 1
    assert session.live_session_token != before


async def test_a_failed_renegotiation_raises_and_never_installs_a_token(
    respx_mock: Any,
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport],
    session: OAuthSession,
) -> None:
    """A refused exchange in headers() fails the request; nothing is signed."""
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthError, match="401"):
            await make_transport(client).headers("GET", API_URL + "/one", {})
    assert session.live_session_token is None


async def test_concurrent_requests_share_one_negotiation(
    respx_mock: Any,
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport],
    credentials: OAuthCredentials,
) -> None:
    """The lock makes simultaneous first requests wait for one exchange, not race five."""
    route = respx_mock.post(TOKEN_URL).mock(side_effect=_ibkr_answers(credentials))
    async with httpx.AsyncClient() as client:
        transport = make_transport(client)
        results = await asyncio.gather(
            *(transport.headers("GET", f"{API_URL}/r{i}", {}) for i in range(5))
        )
    assert route.call_count == 1
    assert len(results) == 5


async def test_verify_is_true_never_false(
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport],
) -> None:
    """TLS verification is always on for the OAuth transport."""
    async with httpx.AsyncClient() as client:
        assert make_transport(client).verify() is True


async def test_aclose_leaves_an_injected_client_open(
    make_transport: Callable[[httpx.AsyncClient], OAuthTransport],
) -> None:
    """The caller owns a client it passed in."""
    async with httpx.AsyncClient() as client:
        await make_transport(client).aclose()
        assert client.is_closed is False


def test_ordinary_signing_carries_no_dh_challenge(
    session: OAuthSession, credentials: OAuthCredentials
) -> None:
    """Only the token request carries the challenge; ordinary requests carry the OAuth set."""
    _install_token(session, credentials)
    params = _auth_params(session.sign_request("GET", API_URL + "/x")["Authorization"])
    assert set(params) == {
        "realm",
        "oauth_consumer_key",
        "oauth_token",
        "oauth_nonce",
        "oauth_timestamp",
        "oauth_signature_method",
        "oauth_signature",
    }


async def test_each_retry_is_a_new_signed_request_not_a_replay(
    respx_mock: Any, session: OAuthSession, credentials: OAuthCredentials, policy: RetryPolicy
) -> None:
    """A retried token request carries a fresh nonce and challenge; the answer to the last one
    is the one verified."""
    nonces: list[str] = []
    answer = _ibkr_answers(credentials)

    def flaky(request: httpx.Request) -> httpx.Response:
        nonces.append(_auth_params(request.headers["Authorization"])["oauth_nonce"])
        if len(nonces) == 1:
            return httpx.Response(503, text="busy")
        return answer(request)

    respx_mock.post(TOKEN_URL).mock(side_effect=flaky)
    retrying = dataclasses.replace(policy, retries=1)
    async with httpx.AsyncClient() as client:
        token = await negotiate_live_session_token(session, TOKEN_URL, client, retrying)
    assert len(nonces) == 2
    assert nonces[0] != nonces[1]
    assert session.live_session_token == token


async def test_an_unreachable_token_endpoint_is_an_ibkr_upstream_error(
    respx_mock: Any, session: OAuthSession, policy: RetryPolicy
) -> None:
    """The session's repair loop counts and alerts IbkrError; a dead host must be one."""
    respx_mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))
    retrying = dataclasses.replace(policy, retries=1)
    async with httpx.AsyncClient() as client:
        with pytest.raises(IbkrUpstreamError, match="unreachable after 2 attempts"):
            await negotiate_live_session_token(session, TOKEN_URL, client, retrying)
    assert session.live_session_token is None


async def test_a_retryable_status_on_the_last_attempt_is_an_upstream_fault(
    respx_mock: Any, session: OAuthSession, policy: RetryPolicy
) -> None:
    """Busy to the end is not a refusal: the session counts it against the breaker."""
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="busy"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(IbkrUpstreamError, match="HTTP 503"):
            await negotiate_live_session_token(session, TOKEN_URL, client, policy)


async def test_a_retry_after_on_the_token_endpoint_is_honoured(
    respx_mock: Any,
    session: OAuthSession,
    credentials: OAuthCredentials,
    policy: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(oauth_module.asyncio, "sleep", no_sleep)
    answer = _ibkr_answers(credentials)
    calls: list[int] = []

    def busy_then_ok(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        return answer(request)

    respx_mock.post(TOKEN_URL).mock(side_effect=busy_then_ok)
    async with httpx.AsyncClient() as client:
        await negotiate_live_session_token(
            session, TOKEN_URL, client, dataclasses.replace(policy, retries=1)
        )
    assert waits == [7.0]
