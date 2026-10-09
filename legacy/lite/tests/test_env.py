"""Environment loading, and the refusal to serve a ledger unauthenticated.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import pytest

from obliskiq.env import load_settings, require_bool
from obliskiq.errors import EnvVarError


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    for name in list(__import__("os").environ):
        if name.startswith(("OBLISK_", "FRED_", "EIA_", "SEC_", "MARKETDATA_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBLISK_DB_PATH", str(tmp_path / "obliskiq.db"))
    return monkeypatch


def test_db_path_is_required(clean_env):
    clean_env.delenv("OBLISK_DB_PATH")
    with pytest.raises(EnvVarError, match="OBLISK_DB_PATH"):
        load_settings(load_dotenv_file=False)


def test_defaults_are_loopback_and_safe(clean_env):
    settings = load_settings(load_dotenv_file=False)
    assert settings.mcp_host == "127.0.0.1"
    assert settings.binds_publicly is False
    assert settings.log_level == "INFO"


def test_public_bind_without_a_token_is_refused(clean_env):
    clean_env.setenv("OBLISK_MCP_HOST", "0.0.0.0")
    with pytest.raises(EnvVarError, match="OBLISK_MCP_AUTH_TOKEN"):
        load_settings(load_dotenv_file=False)


def test_public_bind_with_a_token_is_allowed(clean_env):
    clean_env.setenv("OBLISK_MCP_HOST", "10.0.0.5")
    clean_env.setenv("OBLISK_MCP_AUTH_TOKEN", "a-long-random-string")
    settings = load_settings(load_dotenv_file=False)
    assert settings.binds_publicly is True
    assert settings.require_mcp_auth_token() == "a-long-random-string"


def test_loopback_without_a_token_is_allowed(clean_env):
    clean_env.setenv("OBLISK_MCP_HOST", "127.0.0.1")
    settings = load_settings(load_dotenv_file=False)
    assert settings.mcp_auth_token is None


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_every_loopback_form_is_recognised(clean_env, host):
    clean_env.setenv("OBLISK_MCP_HOST", host)
    assert load_settings(load_dotenv_file=False).binds_publicly is False


def test_whitespace_only_values_count_as_unset(clean_env):
    clean_env.setenv("OBLISK_MCP_HOST", "0.0.0.0")
    clean_env.setenv("OBLISK_MCP_AUTH_TOKEN", "   ")
    with pytest.raises(EnvVarError):
        load_settings(load_dotenv_file=False)


def test_the_port_has_no_code_default(clean_env):
    """A default port binds silently to the wrong place. The collectors bind
    nothing, so the port is required only when the server starts."""
    settings = load_settings(load_dotenv_file=False)
    assert settings.mcp_port is None
    with pytest.raises(EnvVarError, match="no default port"):
        settings.require_mcp_port()


def test_the_port_is_used_when_set(clean_env):
    clean_env.setenv("OBLISK_MCP_PORT", "9443")
    assert load_settings(load_dotenv_file=False).require_mcp_port() == 9443


def test_non_integer_port_is_rejected(clean_env):
    clean_env.setenv("OBLISK_MCP_PORT", "not-a-port")
    with pytest.raises(EnvVarError, match="must be an integer"):
        load_settings(load_dotenv_file=False)


def test_unknown_timezone_is_rejected(clean_env):
    clean_env.setenv("OBLISK_TZ", "Mars/Olympus_Mons")
    with pytest.raises(EnvVarError, match="timezone"):
        load_settings(load_dotenv_file=False)


def test_timezone_resolves(clean_env):
    clean_env.setenv("OBLISK_TZ", "Europe/Madrid")
    assert load_settings(load_dotenv_file=False).tzinfo is not None


def test_missing_ca_bundle_is_an_error_not_a_downgrade(clean_env, tmp_path):
    """The original disabled TLS verification when the CA file was absent."""
    clean_env.setenv("OBLISK_IBKR_CA_BUNDLE", str(tmp_path / "absent.pem"))
    with pytest.raises(EnvVarError, match="never disabled"):
        load_settings(load_dotenv_file=False)


def test_missing_oauth_key_file_is_an_error(clean_env, tmp_path):
    clean_env.setenv("OBLISK_IBKR_SIGNATURE_KEY", str(tmp_path / "absent.pem"))
    with pytest.raises(EnvVarError, match="does not exist"):
        load_settings(load_dotenv_file=False)


def test_no_secret_has_a_hardcoded_default(clean_env):
    settings = load_settings(load_dotenv_file=False)
    for value in (
        settings.mcp_auth_token,
        settings.fred_api_key,
        settings.eia_api_key,
        settings.marketdata_api_token,
        settings.ibkr_consumer_key,
        settings.ibkr_access_token,
        settings.ibkr_access_token_secret,
    ):
        assert value is None


def test_require_bool_parses_the_usual_spellings(clean_env):
    for raw, expected in (("yes", True), ("ON", True), ("0", False), ("false", False)):
        clean_env.setenv("OBLISK_TEST_FLAG", raw)
        assert require_bool("OBLISK_TEST_FLAG", not expected) is expected
    clean_env.setenv("OBLISK_TEST_FLAG", "maybe")
    with pytest.raises(EnvVarError):
        require_bool("OBLISK_TEST_FLAG", True)


def test_settings_is_frozen(clean_env):
    import dataclasses

    settings = load_settings(load_dotenv_file=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.mcp_port = 1  # type: ignore[misc]
