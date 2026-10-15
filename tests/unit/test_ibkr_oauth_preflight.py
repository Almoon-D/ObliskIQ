"""The OAuth handshake walk of the preflight, and the step each failure stops at.

Adapted from ObliskIQ-lite-MCP's tests/test_oauth_preflight.py. A bad credential otherwise
surfaces as a bare 401, which says nothing about which step was wrong; these tests drive each
failure through :func:`~obliskiq.ibkr.preflight.oauth_handshake` and assert where it stops.
The brokerage-session and tickle steps lite walked next belong to the read-only walk and are
tested there. HTTP is mocked with respx; the key files are real PEMs under ``tmp_path``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import base64
import hashlib
import hmac
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import dh, padding, rsa

from obliskiq.config import ObliskConfig
from obliskiq.env import Environment
from obliskiq.ibkr.errors import ERROR_EXCERPT_CHARS
from obliskiq.ibkr.preflight import (
    OAUTH_CREDENTIALS,
    OAUTH_DECRYPT,
    OAUTH_EXCHANGE,
    OAUTH_STEPS,
    OAUTH_VERIFY,
    PreflightReport,
    oauth_handshake,
)

# The 1024-bit MODP group of RFC 2409: exactly the shipped dh_prime_bits_min.
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
CONSUMER_KEY = "TESTCONSUMER"

ORIGIN = "https://api.ibkr.test"
CHALLENGE_MARKER = 'diffie_hellman_challenge="'

OAUTH_VARIABLES = (
    "OBLISK_IBKR_CONSUMER_KEY",
    "OBLISK_IBKR_ACCESS_TOKEN",
    "OBLISK_IBKR_ACCESS_TOKEN_SECRET",
    "OBLISK_IBKR_SIGNATURE_KEY",
    "OBLISK_IBKR_ENCRYPTION_KEY",
    "OBLISK_IBKR_DH_PARAM",
)


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    """Signing, encryption and a stranger's key, generated once."""
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


@pytest.fixture
def token_url(shipped_settings: ObliskConfig) -> str:
    ibkr = shipped_settings.ibkr
    return ORIGIN + ibkr.base_path + ibkr.oauth.live_session_token_path


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> str:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(path)


def _write_dh(path: Path) -> str:
    params = dh.DHParameterNumbers(TEST_PRIME, TEST_GENERATOR).parameters()
    path.write_bytes(
        params.parameter_bytes(serialization.Encoding.PEM, serialization.ParameterFormat.PKCS3)
    )
    return str(path)


def _secret_for(key: rsa.RSAPrivateKey) -> str:
    """The access-token secret as IBKR issues it, encrypted to ``key``."""
    return base64.b64encode(key.public_key().encrypt(PREPEND, padding.PKCS1v15())).decode()


@pytest.fixture
def make_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> Callable[..., Environment]:
    """The environment a handshake reads; the secret is encrypted to ``encrypt_to``."""
    signing, encryption, _ = rsa_keys

    def build(
        *, encrypt_to: rsa.RSAPrivateKey | None = None, omit: tuple[str, ...] = ()
    ) -> Environment:
        values = {
            "OBLISK_IBKR_CP_BASE_URL": ORIGIN,
            "OBLISK_IBKR_CONSUMER_KEY": CONSUMER_KEY,
            "OBLISK_IBKR_ACCESS_TOKEN": "ACCESSTOKEN",
            "OBLISK_IBKR_ACCESS_TOKEN_SECRET": _secret_for(encrypt_to or encryption),
            "OBLISK_IBKR_SIGNATURE_KEY": _write_key(tmp_path / "signing.pem", signing),
            "OBLISK_IBKR_ENCRYPTION_KEY": _write_key(tmp_path / "encryption.pem", encryption),
            "OBLISK_IBKR_DH_PARAM": _write_dh(tmp_path / "dhparam.pem"),
        }
        for name, value in values.items():
            if name not in omit:
                monkeypatch.setenv(name, value)
        return Environment(_env_file=None)

    return build


def _challenge_of(request: httpx.Request) -> str:
    """Pull ``diffie_hellman_challenge`` back out of the Authorization header."""
    header = request.headers["Authorization"]
    start = header.index(CHALLENGE_MARKER) + len(CHALLENGE_MARKER)
    return unquote(header[start : header.index('"', start)])


def _ibkr_response(challenge_hex: str, *, prepend: bytes = PREPEND) -> dict[str, Any]:
    """IBKR's side of the exchange, signed over the token IBKR would derive."""
    server_public = pow(TEST_GENERATOR, SERVER_PRIVATE, TEST_PRIME)
    shared = pow(int(challenge_hex, 16), SERVER_PRIVATE, TEST_PRIME)
    shared_bytes = shared.to_bytes((shared.bit_length() + 8) // 8, "big")
    token = hmac.new(shared_bytes, prepend, hashlib.sha1).digest()
    signature = hmac.new(token, CONSUMER_KEY.encode(), hashlib.sha1).hexdigest()
    return {
        "diffie_hellman_response": format(server_public, "x"),
        "live_session_token_signature": signature,
        "live_session_token_expiration": 0,
    }


def _ibkr_answers(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_ibkr_response(_challenge_of(request)))


async def _walk(env: Environment, settings: ObliskConfig) -> tuple[bool, PreflightReport]:
    report = PreflightReport(planned=OAUTH_STEPS)
    async with httpx.AsyncClient() as client:
        passed = await oauth_handshake(env, settings, client, report)
    return passed, report


def _stopped_at(passed: bool, report: PreflightReport, step: str) -> str:
    """Assert the walk stopped at ``step``; return that step's detail."""
    assert passed is False
    assert not report.ok
    failed = report.failed_step
    assert failed is not None, "the walk should have stopped"
    assert failed.name == step
    assert report.steps[-1] is failed, "nothing runs after the failing step"
    return failed.detail


# ── the walk stops where the failure is ──


async def test_missing_credentials_stop_at_the_first_step(
    respx_mock: Any, make_env: Callable[..., Environment], shipped_settings: ObliskConfig
) -> None:
    """Nothing downstream is attempted or claimed, and every missing variable is named."""
    env = make_env(omit=OAUTH_VARIABLES)
    passed, report = await _walk(env, shipped_settings)

    detail = _stopped_at(passed, report, OAUTH_CREDENTIALS)
    assert len(report.steps) == 1
    for name in OAUTH_VARIABLES:
        assert name in detail
    rendered = report.render()
    assert rendered.count("not reached") == len(OAUTH_STEPS) - 1
    assert not respx_mock.calls, "no request may leave before the credentials load"


async def test_one_missing_credential_is_named_alone(
    respx_mock: Any, make_env: Callable[..., Environment], shipped_settings: ObliskConfig
) -> None:
    """The first step names the variable that is absent, not the ones that are set."""
    env = make_env(omit=("OBLISK_IBKR_DH_PARAM",))
    passed, report = await _walk(env, shipped_settings)

    detail = _stopped_at(passed, report, OAUTH_CREDENTIALS)
    assert "OBLISK_IBKR_DH_PARAM" in detail
    assert "OBLISK_IBKR_CONSUMER_KEY" not in detail
    assert not respx_mock.calls


@pytest.mark.respx(assert_all_called=False)
async def test_a_wrong_encryption_key_stops_at_the_signature_check(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey, rsa.RSAPrivateKey],
    token_url: str,
) -> None:
    """The case that justifies the check existing at all.

    The secret is encrypted to a DIFFERENT key. On OpenSSL builds implementing implicit
    rejection, PKCS#1 v1.5 decryption returns random bytes instead of raising, so decrypt and
    exchange pass and only the signature check catches it. On strict builds decrypt raises.
    Both are correct; neither may yield a session.
    """
    _, _, stranger = rsa_keys
    # IBKR signs over the token derived from the REAL prepend.
    respx_mock.post(token_url).mock(side_effect=_ibkr_answers)
    passed, report = await _walk(make_env(encrypt_to=stranger), shipped_settings)

    assert passed is False
    assert not report.ok
    assert report.failed_step is not None
    assert report.failed_step.name in {OAUTH_DECRYPT, OAUTH_VERIFY}
    if report.failed_step.name == OAUTH_VERIFY:
        assert "signature check" in report.failed_step.detail


async def test_a_rejected_exchange_reports_the_status_and_body(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    token_url: str,
) -> None:
    """The status and an excerpt of the body name the cause; the rest is not quoted."""
    tail = "TAIL-BEYOND-THE-EXCERPT"
    body = "not authorized: consumer key unknown "
    padded = body + "x" * (ERROR_EXCERPT_CHARS - len(body)) + tail
    respx_mock.post(token_url).mock(return_value=httpx.Response(401, text=padded))
    passed, report = await _walk(make_env(), shipped_settings)

    detail = _stopped_at(passed, report, OAUTH_EXCHANGE)
    assert "401" in detail
    assert "consumer key unknown" in detail
    assert tail not in detail
    assert [step.name for step in report.steps] == [
        OAUTH_CREDENTIALS,
        OAUTH_DECRYPT,
        OAUTH_EXCHANGE,
    ]


async def test_a_non_json_exchange_body_is_named_as_such(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    token_url: str,
) -> None:
    """An HTTP 200 maintenance page is reported as not JSON, not as a bad token."""
    respx_mock.post(token_url).mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )
    passed, report = await _walk(make_env(), shipped_settings)

    detail = _stopped_at(passed, report, OAUTH_EXCHANGE)
    assert "not JSON" in detail


async def test_an_exchange_that_never_completes_is_reported_by_kind(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    token_url: str,
) -> None:
    """A transport failure stops at the exchange and names the error type."""
    respx_mock.post(token_url).mock(side_effect=httpx.ConnectError("no route to host"))
    passed, report = await _walk(make_env(), shipped_settings)

    detail = _stopped_at(passed, report, OAUTH_EXCHANGE)
    assert "did not complete" in detail
    assert "ConnectError" in detail


async def test_an_unset_base_url_stops_at_the_exchange(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an origin there is nowhere to send the exchange; the variable is named."""
    make_env()
    monkeypatch.delenv("OBLISK_IBKR_CP_BASE_URL")
    passed, report = await _walk(Environment(_env_file=None), shipped_settings)

    detail = _stopped_at(passed, report, OAUTH_EXCHANGE)
    assert "OBLISK_IBKR_CP_BASE_URL" in detail
    assert not respx_mock.calls


async def test_a_tampered_signature_stops_at_verify(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    token_url: str,
) -> None:
    """The exchange succeeded; the token is not IBKR's. Never proceed."""

    def tampered(request: httpx.Request) -> httpx.Response:
        body = _ibkr_response(_challenge_of(request))
        body["live_session_token_signature"] = "00" * 20
        return httpx.Response(200, json=body)

    respx_mock.post(token_url).mock(side_effect=tampered)
    passed, report = await _walk(make_env(), shipped_settings)

    detail = _stopped_at(passed, report, OAUTH_VERIFY)
    assert "signature check" in detail
    assert [step.ok for step in report.steps] == [True, True, True, False]


async def test_a_good_handshake_passes_all_four_steps(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    token_url: str,
) -> None:
    """Everything up to and including the signature check passes on a clean run."""
    route = respx_mock.post(token_url).mock(side_effect=_ibkr_answers)
    passed, report = await _walk(make_env(), shipped_settings)

    assert passed is True
    assert report.ok, report.render()
    assert [step.name for step in report.steps] == list(OAUTH_STEPS)
    assert route.call_count == 1
    rendered = report.render()
    assert "not reached" not in rendered
    assert "Every step passed." in rendered


async def test_the_report_never_prints_the_prepend_itself(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    token_url: str,
) -> None:
    """Its LENGTH is a useful hint; its bytes are a credential."""
    respx_mock.post(token_url).mock(side_effect=_ibkr_answers)
    env = make_env()
    passed, report = await _walk(env, shipped_settings)
    assert passed is True

    rendered = report.render()
    assert f"{len(PREPEND)} bytes" in rendered
    assert PREPEND.decode("latin-1") not in rendered
    assert PREPEND.hex() not in rendered
    assert env.ibkr_access_token_secret is not None
    assert env.ibkr_access_token_secret.get_secret_value() not in rendered
    assert CONSUMER_KEY not in rendered


# ── the whole walk through the transport it builds itself (Session 4) ──


async def test_a_live_oauth_walk_builds_its_own_transport(
    respx_mock: Any,
    make_env: Callable[..., Environment],
    shipped_settings: ObliskConfig,
    token_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handshake, then the read path over the OAuth transport ``build_transport`` made; mocked
    end to end, nothing injected."""
    from read_path import mock_read_path

    from obliskiq.ibkr.preflight import READ_STEPS, run_read_only

    account = "U_LIVE_TEST"
    for name, value in {
        "OBLISK_IBKR_PROFILE": "live",
        "OBLISK_IBKR_TRANSPORT": "oauth",
        "OBLISK_IBKR_ACCOUNT_ID": account,
    }.items():
        monkeypatch.setenv(name, value)
    env = make_env()
    marketdata = shipped_settings.ibkr.marketdata.model_copy(
        update={"snapshot_settle_delay_seconds": 0.0}
    )
    settings = shipped_settings.model_copy(
        update={"ibkr": shipped_settings.ibkr.model_copy(update={"marketdata": marketdata})}
    )
    route = respx_mock.post(token_url).mock(side_effect=_ibkr_answers)
    listed = [{"id": account, "accountId": account, "currency": "EUR", "type": "INDIVIDUAL"}]
    mock_read_path(
        respx_mock, base=ORIGIN + shipped_settings.ibkr.base_path, account=account, accounts=listed
    )
    report = await run_read_only(env, settings, data_dir=None)
    assert report.ok, report.render()
    assert [step.name for step in report.steps] == [*OAUTH_STEPS, *READ_STEPS]
    assert route.call_count == 2, "the handshake, then the transport's own exchange"
    signed = [call.request for call in respx_mock.calls if "iserver" in str(call.request.url)]
    assert signed
    assert all("oauth_signature" in request.headers["Authorization"] for request in signed)
