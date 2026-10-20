"""The process environment, and the rule that a secret never leaves its SecretStr.

Adapted from ObliskIQ-lite-MCP's tests/test_env.py.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import json
import logging
from pathlib import Path

import pytest

from obliskiq.env import (
    Environment,
    default_env_file,
    load_environment,
    repository_root,
    variable_names,
)
from obliskiq.errors import ConfigError, EnvVarError
from obliskiq.ibkr.transport.oauth import OAuthCredentials
from obliskiq.logging_setup import configure_logging, get_logger

#: The credentials the plan names, plus the OBLISK_ locations and logging overrides.
EXPECTED_NAMES = {
    "FRED_API_KEY",
    "EIA_API_KEY",
    "SEC_USER_AGENT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "OBLISK_HEALTH_PING_URL",
    "OBLISK_IBKR_PROFILE",
    "OBLISK_IBKR_ACCOUNT_ID",
    "OBLISK_IBKR_TRANSPORT",
    "OBLISK_IBKR_GATEWAY_USERNAME",
    "OBLISK_IBKR_GATEWAY_PASSWORD",
    "OBLISK_IBKR_GATEWAY_JAVA_HOME",
    "OBLISK_IBKR_CP_BASE_URL",
    "OBLISK_IBKR_CONSUMER_KEY",
    "OBLISK_IBKR_ACCESS_TOKEN",
    "OBLISK_IBKR_ACCESS_TOKEN_SECRET",
    "OBLISK_IBKR_SIGNATURE_KEY",
    "OBLISK_IBKR_ENCRYPTION_KEY",
    "OBLISK_IBKR_DH_PARAM",
    "OBLISK_CONFIG_DIR",
    "OBLISK_DATA_DIR",
    "OBLISK_LOG_LEVEL",
    "OBLISK_LOG_FORMAT",
}

#: Variables whose value grants access or identifies the owner.
SECRET_NAMES = {
    "FRED_API_KEY",
    "EIA_API_KEY",
    "SEC_USER_AGENT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "OBLISK_HEALTH_PING_URL",
    "OBLISK_IBKR_ACCOUNT_ID",
    "OBLISK_IBKR_GATEWAY_USERNAME",
    "OBLISK_IBKR_GATEWAY_PASSWORD",
    "OBLISK_IBKR_CONSUMER_KEY",
    "OBLISK_IBKR_ACCESS_TOKEN",
    "OBLISK_IBKR_ACCESS_TOKEN_SECRET",
}


def _secret(name: str) -> str:
    sentinel = f"sentinel-{name.lower().replace('_', '-')}-value"
    # The ping URL must be one: an https address with the sentinel in its path.
    return f"https://ping.test/{sentinel}" if name == "OBLISK_HEALTH_PING_URL" else sentinel


@pytest.fixture
def secrets_set(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {name: _secret(name) for name in SECRET_NAMES}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_the_environment_reads_exactly_the_declared_names():
    assert variable_names() == EXPECTED_NAMES


def test_llm_keys_are_not_read_here():
    """The LLM layer reads keys by the names config/llm.yaml declares."""
    assert not {name for name in variable_names() if "GEMINI" in name or "LLM" in name}


def test_every_credential_is_a_secret_type():
    from pydantic import SecretStr

    for field in Environment.model_fields.values():
        if field.validation_alias in SECRET_NAMES:
            assert field.annotation == SecretStr | None, field.validation_alias


def test_an_empty_environment_loads_with_everything_absent():
    env = load_environment(env_file=None)
    assert all(getattr(env, name) is None for name in Environment.model_fields)


def test_values_are_read(monkeypatch, tmp_path):
    key = tmp_path / "signature.pem"
    key.write_text("not a real key", encoding="utf-8")
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    monkeypatch.setenv("OBLISK_IBKR_TRANSPORT", "gateway")
    monkeypatch.setenv("OBLISK_IBKR_SIGNATURE_KEY", str(key))
    monkeypatch.setenv("OBLISK_IBKR_CP_BASE_URL", "https://api.ibkr.com/")
    monkeypatch.setenv("OBLISK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OBLISK_LOG_LEVEL", "debug")
    monkeypatch.setenv("OBLISK_LOG_FORMAT", "TEXT")
    env = load_environment(env_file=None)
    assert env.ibkr_profile == "paper"
    assert env.ibkr_transport == "gateway"
    assert env.ibkr_signature_key_path == key
    assert env.ibkr_cp_base_url == "https://api.ibkr.com"
    assert env.require_data_dir() == tmp_path
    assert env.config_overrides() == {
        "logging.level": ("DEBUG", "environment (OBLISK_LOG_LEVEL)"),
        "logging.format": ("text", "environment (OBLISK_LOG_FORMAT)"),
    }


def test_whitespace_only_values_count_as_unset(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "   ")
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "")
    env = load_environment(env_file=None)
    assert env.fred_api_key is None
    assert env.ibkr_profile is None


def test_names_are_case_sensitive(monkeypatch):
    monkeypatch.setenv("fred_api_key", "lower-case-is-not-the-variable")
    assert load_environment(env_file=None).fred_api_key is None


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("OBLISK_IBKR_PROFILE", "staging", "OBLISK_IBKR_PROFILE: must be 'paper' or 'live'"),
        ("OBLISK_IBKR_TRANSPORT", "tws", "OBLISK_IBKR_TRANSPORT: must be 'oauth' or 'gateway'"),
        ("OBLISK_LOG_FORMAT", "xml", "OBLISK_LOG_FORMAT"),
        ("OBLISK_IBKR_CP_BASE_URL", "http://api.ibkr.com", "must be an https:// URL"),
        ("OBLISK_IBKR_CP_BASE_URL", "https://api.ibkr.com/v1/api", "without a path"),
        ("OBLISK_IBKR_CP_BASE_URL", "https://", "must be an https:// URL"),
        ("OBLISK_HEALTH_PING_URL", "http://hc.test/ping", "OBLISK_HEALTH_PING_URL"),
    ],
)
def test_a_bad_value_fails_loudly_naming_the_variable(monkeypatch, name, value, fragment):
    monkeypatch.setenv(name, value)
    with pytest.raises(EnvVarError, match="invalid environment") as raised:
        load_environment(env_file=None)
    assert fragment in str(raised.value)


def test_a_missing_key_file_is_an_error_not_a_downgrade(monkeypatch, tmp_path, shipped_settings):
    """Looked for only when the live OAuth transport loads its keys (DECISIONS D42), and then
    a missing file is an error naming it, never a silent fallback."""
    for name, value in {
        "OBLISK_IBKR_CONSUMER_KEY": "consumer",
        "OBLISK_IBKR_ACCESS_TOKEN": "token",
        "OBLISK_IBKR_ACCESS_TOKEN_SECRET": "c2VjcmV0",
        "OBLISK_IBKR_SIGNATURE_KEY": str(tmp_path / "absent-signature.pem"),
        "OBLISK_IBKR_ENCRYPTION_KEY": str(tmp_path / "absent-encryption.pem"),
        "OBLISK_IBKR_DH_PARAM": str(tmp_path / "absent.pem"),
    }.items():
        monkeypatch.setenv(name, value)
    env = load_environment(env_file=None)
    with pytest.raises(ConfigError, match="absent"):
        OAuthCredentials.from_environment(env, shipped_settings.ibkr)


def test_the_data_dir_has_no_default():
    with pytest.raises(EnvVarError, match="OBLISK_DATA_DIR must be set"):
        load_environment(env_file=None).require_data_dir()


def test_the_config_dir_defaults_to_the_checkout(monkeypatch, tmp_path, repo_root):
    assert load_environment(env_file=None).resolved_config_dir() == repo_root / "config"
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(tmp_path))
    assert load_environment(env_file=None).resolved_config_dir() == tmp_path


def test_outside_a_checkout_the_config_dir_must_be_named(monkeypatch):
    monkeypatch.setattr("obliskiq.env.repository_root", lambda: None)
    with pytest.raises(EnvVarError, match="OBLISK_CONFIG_DIR is unset"):
        load_environment(env_file=None).resolved_config_dir()


def test_the_repository_root_and_env_file(repo_root, monkeypatch, tmp_path):
    assert repository_root() == repo_root
    expected = repo_root / ".env"
    assert default_env_file() == (expected if expected.is_file() else None)
    monkeypatch.setattr("obliskiq.env.repository_root", lambda: None)
    assert default_env_file() is None
    monkeypatch.setattr("obliskiq.env.repository_root", lambda: tmp_path)
    (tmp_path / ".env").write_text("FRED_API_KEY=x\n", encoding="utf-8")
    assert default_env_file() == tmp_path / ".env"


def test_an_env_file_is_read(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OBLISK_IBKR_PROFILE=paper\nUNRELATED=ignored\n", encoding="utf-8")
    assert load_environment(env_file=env_file).ibkr_profile == "paper"


def test_the_environment_is_frozen():
    from pydantic import ValidationError

    env = load_environment(env_file=None)
    with pytest.raises(ValidationError):
        env.ibkr_profile = "live"  # type: ignore[misc]


# ── secrets never leave their SecretStr ──


def test_no_secret_appears_in_repr_str_or_json(secrets_set):
    env = load_environment(env_file=None)
    rendered = [repr(env), str(env), env.model_dump_json()]
    for value in secrets_set.values():
        assert all(value not in text for text in rendered), value
    assert env.ibkr_gateway_password is not None
    assert env.ibkr_gateway_password.get_secret_value() == _secret("OBLISK_IBKR_GATEWAY_PASSWORD")


def test_no_secret_appears_in_a_log_line(secrets_set):
    env = load_environment(env_file=None)
    stream = io.StringIO()
    configure_logging("DEBUG", "json", stream=stream)
    log = get_logger("test")
    log.info("environment loaded", extra={"env": env})
    log.info("account %s", env.ibkr_account_id)
    for field_name in Environment.model_fields:
        log.debug("field", extra={"field": field_name, "value": getattr(env, field_name)})
    text = stream.getvalue()
    for line in text.splitlines():
        json.loads(line)
    for value in secrets_set.values():
        assert value not in text, value
    logging.getLogger().handlers.clear()


def test_no_secret_appears_in_an_error(secrets_set, monkeypatch):
    """A validation failure elsewhere must not echo the secrets that loaded fine."""
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", _secret("OBLISK_IBKR_PROFILE"))
    with pytest.raises(EnvVarError) as raised:
        load_environment(env_file=None)
    message = str(raised.value)
    assert "OBLISK_IBKR_PROFILE" in message
    for value in [*secrets_set.values(), _secret("OBLISK_IBKR_PROFILE")]:
        assert value not in message, value
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_a_secret_with_surrounding_whitespace_is_trimmed(monkeypatch):
    monkeypatch.setenv("OBLISK_IBKR_ACCESS_TOKEN", "  token-with-spaces  ")
    token = load_environment(env_file=None).ibkr_access_token
    assert token is not None
    assert token.get_secret_value() == "token-with-spaces"


def test_paths_expand_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OBLISK_DATA_DIR", "~/obliskiq-data")
    assert load_environment(env_file=None).data_dir == Path(tmp_path) / "obliskiq-data"


def test_an_oauth_key_path_is_not_looked_for_when_the_environment_loads(monkeypatch, tmp_path):
    """A paper run never looks for the live account's OAuth keys (DECISIONS D42)."""
    absent = tmp_path / "live-only.pem"
    monkeypatch.setenv("OBLISK_IBKR_SIGNATURE_KEY", str(absent))
    env = Environment(_env_file=None)
    assert env.ibkr_signature_key_path == absent
