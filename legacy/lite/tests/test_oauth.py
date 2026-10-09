"""OAuth 1.0a signing and the Diffie-Hellman live-session-token exchange.

There is no live IBKR access in this sandbox, so these tests pin the parts that
are deterministic: RFC 3986 encoding, signature base strings, the DH derivation
against a locally-simulated counterparty, and the refusal to trust a token whose
signature does not check out.

Everything network-facing stays UNVERIFIED until Session 5.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from obliskiq.errors import AuthError, ConfigError
from obliskiq.ibkr.oauth import (
    OAuthCredentials,
    OAuthSession,
    _to_signed_bytes,
    build_authorization_header,
    build_signature_base_string,
    percent_encode,
)

# A small prime and generator, enough to exercise the exchange arithmetic without
# spending seconds on key generation. Real deployments load a 2048-bit dhparam.
TEST_PRIME = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E08"
    "8A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
    "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9"
    "A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE6"
    "49286651ECE65381FFFFFFFFFFFFFFFF",
    16,
)
TEST_GENERATOR = 2


@pytest.fixture(scope="module")
def rsa_keys():
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


@pytest.fixture()
def prepend() -> bytes:
    return b"\x01\x02\x03\x04prepend-secret"


@pytest.fixture()
def credentials(rsa_keys, prepend):
    signing_key, encryption_key = rsa_keys
    encrypted = encryption_key.public_key().encrypt(prepend, padding.PKCS1v15())
    return OAuthCredentials(
        consumer_key="TESTCONSUMER",
        access_token="ACCESSTOKEN",
        access_token_secret=base64.b64encode(encrypted).decode("ascii"),
        signing_key=signing_key,
        encryption_key=encryption_key,
        dh_prime=TEST_PRIME,
        dh_generator=TEST_GENERATOR,
    )


@pytest.fixture()
def session(credentials, config):
    return OAuthSession(credentials, config)


# ── RFC 3986 encoding ──


@pytest.mark.parametrize(
    "raw,encoded",
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
def test_percent_encoding_follows_rfc_3986(raw, encoded):
    """OAuth's encoding is stricter than urlencode's: '+' is not a space, and only
    the unreserved set passes through. Getting this wrong breaks every signature."""
    assert percent_encode(raw) == encoded


def test_signature_base_string_sorts_and_double_encodes():
    base = build_signature_base_string(
        "get", "https://api.ibkr.com/v1/api/x", {"b": "2", "a": "1 x"}
    )
    assert base.startswith("GET&")
    assert base == "GET&https%3A%2F%2Fapi.ibkr.com%2Fv1%2Fapi%2Fx&a%3D1%2520x%26b%3D2"


def test_authorization_header_is_quoted_and_realmed():
    header = build_authorization_header("limited_poa", {"oauth_nonce": "ab c"})
    assert header.startswith('OAuth realm="limited_poa", ')
    assert 'oauth_nonce="ab%20c"' in header


# ── credentials ──


def test_access_token_secret_round_trips(credentials, prepend):
    assert credentials.decrypt_access_token_secret() == prepend


def test_a_malformed_access_token_secret_is_reported(credentials):
    import dataclasses

    broken = dataclasses.replace(credentials, access_token_secret="not-base64-ciphertext")
    with pytest.raises(AuthError, match="OBLISK_IBKR_ACCESS_TOKEN_SECRET"):
        broken.decrypt_access_token_secret()


def test_a_wrong_encryption_key_never_yields_a_session(credentials, prepend, config):
    """A mismatched encryption key must never produce a working session token.

    Which layer catches it is NOT guaranteed, and varies by OpenSSL build. Where
    implicit rejection is implemented as a Bleichenbacher countermeasure, PKCS#1
    v1.5 decryption returns indistinguishable random bytes and the
    live-session-token signature check is what catches the mismatch; elsewhere
    the decryption itself raises. Both were observed in CI across the matrix.

    So the invariant asserted here is the one that actually matters — no path
    ends in a usable token — rather than one platform's particular failure mode.
    """
    import dataclasses

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_key = dataclasses.replace(credentials, encryption_key=other)
    session = OAuthSession(wrong_key, config)

    with pytest.raises(AuthError):
        _, challenge = session.build_live_session_token_request(
            "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=12345
        )
        # Reached only where decryption returned random bytes rather than raising.
        # IBKR computes the token from the REAL prepend, so the signature check
        # must reject the one derived from ours.
        body, _ = _simulate_ibkr(session, credentials, prepend, challenge)
        session.complete_live_session_token(body)

    assert session.live_session_token is None
    assert session.needs_live_session_token() is True


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_short_dh_prime_is_refused(config, rsa_keys, tmp_path):
    """A prime below the configured minimum is a downgrade, not a warning."""
    signing_key, encryption_key = rsa_keys
    for name, key in (("sign.pem", signing_key), ("enc.pem", encryption_key)):
        (tmp_path / name).write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    from cryptography.hazmat.primitives.asymmetric import dh

    weak = dh.generate_parameters(generator=2, key_size=512)
    (tmp_path / "dh.pem").write_bytes(
        weak.parameter_bytes(serialization.Encoding.PEM, serialization.ParameterFormat.PKCS3)
    )
    with pytest.raises(ConfigError, match="below the configured minimum"):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret="",
            signing_key_path=tmp_path / "sign.pem",
            encryption_key_path=tmp_path / "enc.pem",
            dh_param_path=tmp_path / "dh.pem",
            config=config,
        )


def test_a_non_rsa_key_file_is_rejected(config, tmp_path):
    (tmp_path / "notakey.pem").write_text("-----BEGIN NONSENSE-----\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not load"):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret="",
            signing_key_path=tmp_path / "notakey.pem",
            encryption_key_path=tmp_path / "notakey.pem",
            dh_param_path=tmp_path / "notakey.pem",
            config=config,
        )


# ── the live-session-token exchange ──


def _simulate_ibkr(session, credentials, prepend, challenge_hex, *, server_private=7919):
    """Play IBKR's side of the exchange: return B and the token signature."""
    server_public = pow(credentials.dh_generator, server_private, credentials.dh_prime)
    shared = pow(int(challenge_hex, 16), server_private, credentials.dh_prime)
    shared_bytes = shared.to_bytes((shared.bit_length() + 8) // 8, "big")
    token = hmac.new(shared_bytes, prepend, hashlib.sha1).digest()
    signature = hmac.new(token, credentials.consumer_key.encode("utf-8"), hashlib.sha1).hexdigest()
    return {
        "diffie_hellman_response": format(server_public, "x"),
        "live_session_token_signature": signature,
        "live_session_token_expiration": 0,
    }, token


def test_the_exchange_derives_the_same_token_as_the_counterparty(session, credentials, prepend):
    _, challenge = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=12345
    )
    body, expected_token = _simulate_ibkr(session, credentials, prepend, challenge)
    derived = session.complete_live_session_token(body)
    assert derived.token == expected_token
    assert derived.base64 == base64.b64encode(expected_token).decode("ascii")


def test_a_token_failing_ibkrs_signature_check_is_refused(session, credentials, prepend):
    """An unverified token fails every later call with no explanation. Catch it here."""
    _, challenge = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=12345
    )
    body, _ = _simulate_ibkr(session, credentials, prepend, challenge)
    body["live_session_token_signature"] = "00" * 20
    with pytest.raises(AuthError, match="signature check"):
        session.complete_live_session_token(body)


def test_a_malformed_exchange_response_names_the_missing_field(session, credentials, prepend):
    session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=12345
    )
    with pytest.raises(AuthError, match="diffie_hellman_response"):
        session.complete_live_session_token({"live_session_token_signature": "ab"})


def test_completing_before_requesting_is_an_error(session):
    with pytest.raises(AuthError, match="before build_live_session_token_request"):
        session.complete_live_session_token(
            {"diffie_hellman_response": "1", "live_session_token_signature": "2"}
        )


def test_the_request_header_is_rsa_signed_and_carries_the_challenge(session, credentials):
    headers, challenge = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=999
    )
    assert "diffie_hellman_challenge" in headers["Authorization"]
    assert 'oauth_signature_method="RSA-SHA256"' in headers["Authorization"]
    assert headers["Host"] == "api.ibkr.com"
    expected = format(pow(credentials.dh_generator, 999, credentials.dh_prime), "x")
    assert challenge == expected


def test_each_exchange_uses_a_fresh_private_exponent(session):
    _, first = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token"
    )
    _, second = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token"
    )
    assert first != second


# ── signing ordinary requests ──


def test_signing_requires_a_live_session_token(session):
    with pytest.raises(AuthError, match="no live session token"):
        session.sign_request("GET", "https://api.ibkr.com/v1/api/portfolio/accounts")


def test_signed_requests_use_hmac_sha256(session, credentials, prepend):
    _, challenge = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=4242
    )
    body, _ = _simulate_ibkr(session, credentials, prepend, challenge)
    session.complete_live_session_token(body)

    headers = session.sign_request(
        "GET",
        "https://api.ibkr.com/v1/api/iserver/marketdata/snapshot",
        params={"conids": "265598", "fields": "31,84"},
    )
    assert 'oauth_signature_method="HMAC-SHA256"' in headers["Authorization"]
    assert 'oauth_consumer_key="TESTCONSUMER"' in headers["Authorization"]
    assert "oauth_signature=" in headers["Authorization"]


def test_query_parameters_change_the_signature(session, credentials, prepend):
    """Parameters participate in the base string, so a signature that ignored them
    would be rejected by IBKR for every call that has any."""
    _, challenge = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=4242
    )
    body, _ = _simulate_ibkr(session, credentials, prepend, challenge)
    session.complete_live_session_token(body)

    url = "https://api.ibkr.com/v1/api/iserver/marketdata/snapshot"
    first = session.sign_request("GET", url, params={"conids": "1"})
    second = session.sign_request("GET", url, params={"conids": "2"})
    assert first["Authorization"] != second["Authorization"]


def test_the_token_expiry_is_respected(session, credentials, prepend, config):
    import time

    _, challenge = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=4242
    )
    body, _ = _simulate_ibkr(session, credentials, prepend, challenge)
    assert session.needs_live_session_token() is True

    session.complete_live_session_token(body)
    assert session.needs_live_session_token() is False

    ttl = config.require_float("ibkr.cp_api.oauth.live_session_token_ttl_seconds")
    assert session.needs_live_session_token(now=time.time() + ttl + 1) is True


def test_nonces_do_not_repeat(session, credentials, prepend):
    _, challenge = session.build_live_session_token_request(
        "https://api.ibkr.com/v1/api/oauth/live_session_token", dh_private=1
    )
    body, _ = _simulate_ibkr(session, credentials, prepend, challenge)
    session.complete_live_session_token(body)

    url = "https://api.ibkr.com/v1/api/tickle"
    nonces = {session.sign_request("POST", url)["Authorization"] for _ in range(50)}
    assert len(nonces) == 50


# ── the signed big integer, against an INDEPENDENT reference ──


def _java_biginteger_to_byte_array(value: int) -> bytes:
    """Java's ``BigInteger.toByteArray()`` for a non-negative value.

    This is what IBKR's specification means by a signed big integer, and it is
    written independently of ``_to_signed_bytes`` on purpose. ``_simulate_ibkr``
    re-implements the production formula inline, so the round-trip test agrees
    with the code by construction: if the convention were wrong, both sides would
    be wrong together and that test would still pass.

    The definition, not the implementation: the minimal two's-complement
    big-endian encoding, which carries a leading 0x00 exactly when the most
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
def test_the_dh_shared_secret_is_encoded_as_a_signed_big_integer(value):
    assert _to_signed_bytes(value) == _java_biginteger_to_byte_array(value)


def test_a_high_bit_value_really_does_carry_the_leading_zero():
    """The case the whole convention exists for, stated explicitly."""
    encoded = _to_signed_bytes(2**2047)
    assert len(encoded) == 257
    assert encoded[0] == 0x00
    assert encoded[1] == 0x80


def test_a_value_whose_high_bit_is_clear_carries_no_padding():
    """Padding unconditionally would be just as wrong, in the other direction."""
    encoded = _to_signed_bytes(2**2046)
    assert len(encoded) == 256
    assert encoded[0] == 0x40


def test_omitting_the_leading_zero_derives_a_different_token():
    """Why this matters: the failure is silent and total.

    A token derived from the unsigned encoding is a perfectly well-formed
    20-byte HMAC. It simply is not the one IBKR derived, so every later call
    fails authentication with no useful error. The signature check is what turns
    that into a diagnosable failure at the handshake.
    """
    shared = 2**2047 + 7919
    prepend = b"\xde\xad\xbe\xef"

    signed = hmac.new(_to_signed_bytes(shared), prepend, hashlib.sha1).digest()
    unsigned = hmac.new(
        shared.to_bytes((shared.bit_length() + 7) // 8, "big"), prepend, hashlib.sha1
    ).digest()

    assert signed != unsigned
    assert len(signed) == len(unsigned) == 20, "both are well-formed; only one is right"
