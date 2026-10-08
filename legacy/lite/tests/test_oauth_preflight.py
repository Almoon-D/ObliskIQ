"""The OAuth preflight walk, and the step each failure stops at.

The handshake has never run against IBKR. When it first does, it will either
work or return a 401 — and a 401 says nothing about which of six steps was
wrong. These tests drive each failure mode through the walk and assert it stops
where it should, because that routing is the entire value of the command.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from obliskiq.env import Settings
from obliskiq.ibkr.oauth import OAuthCredentials
from obliskiq.ibkr.preflight import (
    STEP_CREDENTIALS,
    STEP_DECRYPT,
    STEP_EXCHANGE,
    STEP_VERIFY,
    STEPS,
    run_preflight,
)

BASE_URL = "https://api.ibkr.com"
TOKEN_PATH = "/v1/api/oauth/live_session_token"

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
SERVER_PRIVATE = 7919


@pytest.fixture(scope="module")
def rsa_keys():
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def _credentials(signing_key, encryption_key, *, encrypt_to=None) -> OAuthCredentials:
    """Credentials whose secret is encrypted to ``encrypt_to`` (default: the right key)."""
    target = encrypt_to if encrypt_to is not None else encryption_key
    ciphertext = target.public_key().encrypt(PREPEND, padding.PKCS1v15())
    return OAuthCredentials(
        consumer_key="TESTCONSUMER",
        access_token="ACCESSTOKEN",
        access_token_secret=base64.b64encode(ciphertext).decode("ascii"),
        signing_key=signing_key,
        encryption_key=encryption_key,
        dh_prime=TEST_PRIME,
        dh_generator=TEST_GENERATOR,
    )


def _settings(tmp_path) -> Settings:
    """Settings carrying the three IBKR values, so the credentials step passes.

    The key PATHS are placeholders: every test patches credentials_from_settings,
    because what is under test is the WALK, not the file loading that
    tests/test_oauth.py already covers.
    """
    return Settings(
        db_path=tmp_path / "unused.db",
        config_path=tmp_path / "unused.yaml",
        log_level="INFO",
        log_format="json",
        timezone="UTC",
        ibkr_cp_base_url=BASE_URL,
        ibkr_connector_url=None,
        ibkr_consumer_key="TESTCONSUMER",
        ibkr_access_token="ACCESSTOKEN",
        ibkr_access_token_secret="ignored",
        ibkr_signature_key_path=tmp_path / "signature.pem",
        ibkr_encryption_key_path=tmp_path / "encryption.pem",
        ibkr_dh_param_path=tmp_path / "dhparam.pem",
        ibkr_ca_bundle=None,
        mcp_host="127.0.0.1",
        mcp_port=8000,
        mcp_auth_token=None,
        fred_api_key=None,
        eia_api_key=None,
        marketdata_api_token=None,
    )


def _ibkr_response(credentials: OAuthCredentials, challenge_hex: str, *, prepend=PREPEND) -> dict:
    """IBKR's side of the exchange, signed over the token IBKR would derive."""
    server_public = pow(credentials.dh_generator, SERVER_PRIVATE, credentials.dh_prime)
    shared = pow(int(challenge_hex, 16), SERVER_PRIVATE, credentials.dh_prime)
    shared_bytes = shared.to_bytes((shared.bit_length() + 8) // 8, "big")
    token = hmac.new(shared_bytes, prepend, hashlib.sha1).digest()
    signature = hmac.new(token, credentials.consumer_key.encode("utf-8"), hashlib.sha1).hexdigest()
    return {
        "diffie_hellman_response": format(server_public, "x"),
        "live_session_token_signature": signature,
        "live_session_token_expiration": 0,
    }


def _transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _walk(settings, config, credentials, monkeypatch, handler):
    monkeypatch.setattr(
        "obliskiq.ibkr.preflight.credentials_from_settings",
        lambda *_args, **_kwargs: credentials,
    )
    async with _transport(handler) as client:
        return await run_preflight(settings, config, client)


def _stopped_at(report, step):
    assert report.failed_step is not None, "the walk should have stopped"
    assert report.failed_step.name == step
    assert not report.ok


# ── the walk stops where the failure is ──


async def test_missing_credentials_stop_at_the_first_step(tmp_path, config, monkeypatch):
    """Nothing downstream is attempted, and nothing downstream is claimed."""
    settings = replace(_settings(tmp_path), ibkr_consumer_key=None)

    async with _transport(lambda request: httpx.Response(500)) as client:
        report = await run_preflight(settings, config, client)

    _stopped_at(report, STEP_CREDENTIALS)
    assert len(report.steps) == 1
    rendered = report.render()
    assert "not reached" in rendered
    assert rendered.count("not reached") == len(STEPS) - 1


async def test_a_wrong_encryption_key_stops_at_the_signature_check(
    tmp_path, config, rsa_keys, monkeypatch
):
    """The case that justifies the check existing at all.

    The secret is encrypted to a DIFFERENT key. On OpenSSL builds implementing
    implicit rejection, PKCS#1 v1.5 decryption returns random bytes instead of
    raising, so the decrypt step passes, the exchange passes, and the derived
    token is a well-formed 20-byte HMAC that simply is not IBKR's. Only the
    signature check catches it — and it must, because otherwise every later call
    fails authentication with no useful error.
    """
    signing_key, encryption_key = rsa_keys
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    credentials = _credentials(signing_key, encryption_key, encrypt_to=stranger)

    # IBKR signs over the token derived from the REAL prepend.
    def handler(request):
        challenge = _challenge_of(request)
        return httpx.Response(200, json=_ibkr_response(credentials, challenge))

    report = await _walk(_settings(tmp_path), config, credentials, monkeypatch, handler)

    # Either the decrypt raised (strict builds) or it returned junk and the
    # signature check caught it. Both are correct; neither may yield a session.
    assert report.failed_step is not None
    assert report.failed_step.name in {STEP_DECRYPT, STEP_VERIFY}
    assert not report.ok


async def test_a_rejected_exchange_reports_the_status_and_body(
    tmp_path, config, rsa_keys, monkeypatch
):
    signing_key, encryption_key = rsa_keys
    credentials = _credentials(signing_key, encryption_key)

    def handler(request):
        return httpx.Response(401, text="not authorized: consumer key unknown")

    report = await _walk(_settings(tmp_path), config, credentials, monkeypatch, handler)

    _stopped_at(report, STEP_EXCHANGE)
    assert "401" in report.failed_step.detail
    assert "consumer key unknown" in report.failed_step.detail


async def test_a_non_json_exchange_body_is_named_as_such(tmp_path, config, rsa_keys, monkeypatch):
    signing_key, encryption_key = rsa_keys
    credentials = _credentials(signing_key, encryption_key)

    report = await _walk(
        _settings(tmp_path),
        config,
        credentials,
        monkeypatch,
        lambda request: httpx.Response(200, text="<html>maintenance</html>"),
    )

    _stopped_at(report, STEP_EXCHANGE)
    assert "not JSON" in report.failed_step.detail


async def test_a_tampered_signature_stops_at_verify(tmp_path, config, rsa_keys, monkeypatch):
    """The exchange succeeded; the token is not IBKR's. Never proceed."""
    signing_key, encryption_key = rsa_keys
    credentials = _credentials(signing_key, encryption_key)

    def handler(request):
        body = _ibkr_response(credentials, _challenge_of(request))
        body["live_session_token_signature"] = "00" * 20
        return httpx.Response(200, json=body)

    report = await _walk(_settings(tmp_path), config, credentials, monkeypatch, handler)

    _stopped_at(report, STEP_VERIFY)
    assert "signature check" in report.failed_step.detail


async def test_a_good_handshake_reaches_the_brokerage_session(
    tmp_path, config, rsa_keys, monkeypatch
):
    """Everything up to and including the signature check passes on a clean run."""
    signing_key, encryption_key = rsa_keys
    credentials = _credentials(signing_key, encryption_key)

    def handler(request):
        if TOKEN_PATH in request.url.path:
            return httpx.Response(200, json=_ibkr_response(credentials, _challenge_of(request)))
        if "ssodh/init" in request.url.path:
            return httpx.Response(200, json={"authenticated": True})
        if "tickle" in request.url.path:
            return httpx.Response(200, json={"session": "abc123"})
        return httpx.Response(404)

    report = await _walk(_settings(tmp_path), config, credentials, monkeypatch, handler)

    names = [step.name for step in report.steps]
    assert names[:4] == [STEP_CREDENTIALS, STEP_DECRYPT, STEP_EXCHANGE, STEP_VERIFY]
    assert all(step.ok for step in report.steps[:4]), report.render()


async def test_the_report_never_prints_the_prepend_itself(tmp_path, config, rsa_keys, monkeypatch):
    """Its LENGTH is a useful hint; its bytes are a credential."""
    signing_key, encryption_key = rsa_keys
    credentials = _credentials(signing_key, encryption_key)

    report = await _walk(
        _settings(tmp_path),
        config,
        credentials,
        monkeypatch,
        lambda request: httpx.Response(
            200, json=_ibkr_response(credentials, _challenge_of(request))
        ),
    )

    rendered = report.render()
    assert str(len(PREPEND)) in rendered
    assert PREPEND.decode("latin-1") not in rendered
    assert PREPEND.hex() not in rendered
    assert credentials.access_token_secret not in rendered


def _challenge_of(request: httpx.Request) -> str:
    """Pull ``diffie_hellman_challenge`` back out of the Authorization header."""
    header = request.headers["Authorization"]
    marker = 'diffie_hellman_challenge="'
    start = header.index(marker) + len(marker)
    return header[start : header.index('"', start)]
