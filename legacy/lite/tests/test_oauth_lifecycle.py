"""The OAuth paths that had never been executed: loading, rotation, expiry.

``tests/test_oauth.py`` covers the crypto thoroughly — base strings, the DH
derivation against a locally simulated IBKR, the Java-BigInteger encoding. What
it never covered is the lifecycle around that crypto:

* ``credentials_from_settings`` had no test at all, so the error naming which of
  six environment variables is missing was never asserted.
* ``OAuthCredentials.from_files`` was only ever called inside ``pytest.raises``.
  No test loaded three valid files and got credentials back.
* the missing-file branch was never taken — the nearest test writes a real file
  with junk in it, which is a different branch.
* nothing asserted what happens after a token expires, only that the
  ``needs_live_session_token`` boolean flips.

The last one is a finding rather than a gap, and is recorded as such below:
there is no renewal path to test. See docs/archive/SESSION-8-VALIDATION.md.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import dh, ec, rsa

from obliskiq.errors import ConfigError, EnvVarError
from obliskiq.ibkr.oauth import LiveSessionToken, OAuthCredentials, OAuthSession
from obliskiq.ibkr.session import credentials_from_settings


@pytest.fixture(scope="module")
def keypair():
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


@pytest.fixture(scope="module")
def dh_params():
    return dh.generate_parameters(generator=2, key_size=1024)


def _write_pem(path, key):
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


def _write_dh(path, params):
    path.write_bytes(
        params.parameter_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.ParameterFormat.PKCS3,
        )
    )
    return path


@pytest.fixture()
def key_files(tmp_path, keypair, dh_params):
    signing, encryption = keypair
    return {
        "signing": _write_pem(tmp_path / "signing.pem", signing),
        "encryption": _write_pem(tmp_path / "encryption.pem", encryption),
        "dh": _write_dh(tmp_path / "dhparam.pem", dh_params),
    }


def _settings(key_files, **overrides):
    base = {
        "ibkr_consumer_key": "TESTCONSUMER",
        "ibkr_access_token": "ACCESSTOKEN",
        "ibkr_access_token_secret": base64.b64encode(b"ciphertext").decode("ascii"),
        "ibkr_signature_key_path": key_files["signing"],
        "ibkr_encryption_key_path": key_files["encryption"],
        "ibkr_dh_param_path": key_files["dh"],
    }
    base.update(overrides)
    return base


class _FakeSettings:
    """Only the attributes credentials_from_settings reads."""

    def __init__(self, values):
        for name, value in values.items():
            setattr(self, name, value)


# ── loading a real, complete set of keys ─────────────────────────────────────


def test_three_valid_key_files_load_into_credentials(key_files, config):
    """The success path. Every existing call site is inside pytest.raises."""
    credentials = OAuthCredentials.from_files(
        consumer_key="TESTCONSUMER",
        access_token="ACCESSTOKEN",
        access_token_secret=base64.b64encode(b"ciphertext").decode("ascii"),
        signing_key_path=key_files["signing"],
        encryption_key_path=key_files["encryption"],
        dh_param_path=key_files["dh"],
        config=config,
    )
    assert credentials.consumer_key == "TESTCONSUMER"
    assert credentials.dh_generator == 2
    assert credentials.dh_prime > 0
    assert credentials.signing_key is not credentials.encryption_key


def test_credentials_from_settings_loads_the_same_way(key_files, config):
    settings = _FakeSettings(_settings(key_files))
    credentials = credentials_from_settings(settings, config)
    assert credentials.consumer_key == "TESTCONSUMER"


@pytest.mark.parametrize(
    "missing,variable",
    [
        ("ibkr_consumer_key", "OBLISK_IBKR_CONSUMER_KEY"),
        ("ibkr_access_token", "OBLISK_IBKR_ACCESS_TOKEN"),
        ("ibkr_access_token_secret", "OBLISK_IBKR_ACCESS_TOKEN_SECRET"),
        ("ibkr_signature_key_path", "OBLISK_IBKR_SIGNATURE_KEY"),
        ("ibkr_encryption_key_path", "OBLISK_IBKR_ENCRYPTION_KEY"),
        ("ibkr_dh_param_path", "OBLISK_IBKR_DH_PARAM"),
    ],
)
def test_each_missing_variable_is_named_by_its_own_name(key_files, config, missing, variable):
    """An unattended collector must say which one, not just that one is absent."""
    settings = _FakeSettings(_settings(key_files, **{missing: None}))
    with pytest.raises(EnvVarError, match=variable):
        credentials_from_settings(settings, config)


def test_several_missing_variables_are_all_named_at_once(key_files, config):
    settings = _FakeSettings(_settings(key_files, ibkr_consumer_key=None, ibkr_dh_param_path=None))
    with pytest.raises(EnvVarError) as raised:
        credentials_from_settings(settings, config)
    assert "OBLISK_IBKR_CONSUMER_KEY" in str(raised.value)
    assert "OBLISK_IBKR_DH_PARAM" in str(raised.value)


# ── rotation and failure on the key files themselves ─────────────────────────


def test_a_key_file_that_is_not_there_is_refused_by_name(tmp_path, key_files, config):
    """Distinct from a malformed file: this branch had never been taken."""
    absent = tmp_path / "rotated-away.pem"
    assert not absent.exists()

    with pytest.raises(ConfigError, match="rotated-away"):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret=base64.b64encode(b"x").decode("ascii"),
            signing_key_path=absent,
            encryption_key_path=key_files["encryption"],
            dh_param_path=key_files["dh"],
            config=config,
        )


def test_a_malformed_dh_parameter_file_is_refused(tmp_path, key_files, config):
    """Reached by giving the two RSA slots valid keys, so the DH loader runs.

    The nearest existing test passes one junk file for all three paths, so the
    signing key raises first and this branch is never entered.
    """
    broken = tmp_path / "dhparam-broken.pem"
    broken.write_text("-----BEGIN DH PARAMETERS-----\nnot base64\n-----END DH PARAMETERS-----\n")

    with pytest.raises(ConfigError):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret=base64.b64encode(b"x").decode("ascii"),
            signing_key_path=key_files["signing"],
            encryption_key_path=key_files["encryption"],
            dh_param_path=broken,
            config=config,
        )


def test_an_elliptic_curve_key_in_an_rsa_slot_is_refused(tmp_path, key_files, config):
    """A rotation to the wrong algorithm is a plausible operator error."""
    curve_key = ec.generate_private_key(ec.SECP256R1())
    path = _write_pem(tmp_path / "ec.pem", curve_key)

    with pytest.raises(ConfigError):
        OAuthCredentials.from_files(
            consumer_key="K",
            access_token="T",
            access_token_secret=base64.b64encode(b"x").decode("ascii"),
            signing_key_path=path,
            encryption_key_path=key_files["encryption"],
            dh_param_path=key_files["dh"],
            config=config,
        )


def test_rotating_the_files_on_disk_is_not_picked_up_by_a_live_session(key_files, config):
    """Recorded behaviour, not a defect being fixed here.

    from_files runs once per process, at connect. Replacing the PEMs under a
    running collector changes nothing until it restarts. Operators rotating keys
    need to know that; see docs/archive/SESSION-8-VALIDATION.md.
    """
    first = credentials_from_settings(_FakeSettings(_settings(key_files)), config)
    replacement = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _write_pem(key_files["signing"], replacement)

    assert first.signing_key.private_numbers() != replacement.private_numbers()
    reloaded = credentials_from_settings(_FakeSettings(_settings(key_files)), config)
    assert reloaded.signing_key.private_numbers() == replacement.private_numbers()


# ── expiry ───────────────────────────────────────────────────────────────────


def test_an_expired_token_reports_that_it_needs_replacing(config, key_files):
    credentials = credentials_from_settings(_FakeSettings(_settings(key_files)), config)
    session = OAuthSession(credentials, config)

    margin = config.require_float("ibkr.cp_api.oauth.live_session_token_refresh_margin_seconds")
    session._live_session_token = LiveSessionToken(token=b"x" * 20, expires_at=1000.0)

    assert session.needs_live_session_token(now=1000.0 - margin - 1) is False
    assert session.needs_live_session_token(now=1000.0 - margin + 1) is True
    assert session.needs_live_session_token(now=2000.0) is True


def test_signing_after_expiry_still_uses_the_stale_token(config, key_files):
    """The finding, pinned.

    needs_live_session_token flips, but nothing consults it again: it is checked
    once, at connect. There is no renewal path, so a process alive past the TTL
    keeps signing with a token IBKR has already dropped and every call fails with
    a terminal AuthError. Asserting the current behaviour so that building
    renewal changes a test rather than passing silently.
    """
    credentials = credentials_from_settings(_FakeSettings(_settings(key_files)), config)
    session = OAuthSession(credentials, config)
    session._live_session_token = LiveSessionToken(token=b"x" * 20, expires_at=0.0)

    assert session.needs_live_session_token() is True
    headers = session.sign_request("GET", "https://x.test/v1/api/one")
    assert "Authorization" in headers
    assert "oauth_signature" in headers["Authorization"]
