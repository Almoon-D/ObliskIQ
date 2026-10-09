"""The command line, run in-process.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from pathlib import Path

import pytest

from obliskiq import __version__
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, main, run


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue()


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_config_validate_reports_the_essentials(config_dir):
    status, out, err = _run("--config-dir", str(config_dir), "config", "validate")
    assert status == EXIT_OK
    assert err == ""
    assert out.splitlines() == [
        "configuration is valid (config/defaults/logging.yaml, config/defaults/paths.yaml, "
        "config/owner.yaml)",
        "  base currency  EUR",
        "  jurisdiction   es (Spain)",
        "  policies       none declared",
    ]


def test_settings_show_lists_every_setting_with_its_source(config_dir):
    status, out, _ = _run("--config-dir", str(config_dir), "settings", "show")
    assert status == EXIT_OK
    lines = out.splitlines()
    assert lines[0] == "Effective settings — jurisdiction es (Spain)"
    table = [line.split() for line in lines[2:]]
    assert table == [
        ["SETTING", "VALUE", "SOURCE"],
        ["account.base_currency", "EUR", "config/owner.yaml"],
        ["jurisdiction", "es", "config/owner.yaml"],
        ["logging.format", "json", "config/defaults/logging.yaml"],
        ["logging.level", "INFO", "config/defaults/logging.yaml"],
        ["paths.settings_audit_log", "settings-audit.jsonl", "config/defaults/paths.yaml"],
    ]


def test_the_environment_override_shows_as_such(config_dir, monkeypatch):
    monkeypatch.setenv("OBLISK_LOG_FORMAT", "text")
    status, out, _ = _run("--config-dir", str(config_dir), "settings", "show")
    assert status == EXIT_OK
    assert "logging.format            text" in out
    assert "environment (OBLISK_LOG_FORMAT)" in out


def test_the_config_dir_comes_from_the_environment(config_dir, monkeypatch):
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    _write(config_dir / "owner.yaml", "account: {base_currency: USD}\njurisdiction: es\n")
    status, out, _ = _run("config", "validate")
    assert status == EXIT_OK
    assert "base currency  USD" in out


def test_an_invalid_owner_file_fails_loudly_and_readably(config_dir):
    _write(config_dir / "owner.yaml", "account: {base_currency: EURO}\njurisdiction: es\n")
    status, out, err = _run("--config-dir", str(config_dir), "config", "validate")
    assert status == EXIT_INVALID
    assert out == ""
    assert err.splitlines() == [
        "obliskiq: configuration is invalid (1 problem):",
        "  config/owner.yaml: account.base_currency: must be a three-letter ISO 4217 currency "
        "code in capitals, such as EUR (got 'EURO')",
    ]
    assert "Traceback" not in err


def test_settings_show_fails_the_same_way(config_dir):
    _write(config_dir / "owner.yaml", "account: [\n")
    status, out, err = _run("--config-dir", str(config_dir), "settings", "show")
    assert status == EXIT_INVALID
    assert out == ""
    assert "config/owner.yaml: is not valid YAML: line 2" in err


def test_an_unknown_jurisdiction_fails(config_dir):
    _write(config_dir / "owner.yaml", "account: {base_currency: EUR}\njurisdiction: fr\n")
    status, _, err = _run("--config-dir", str(config_dir), "config", "validate")
    assert status == EXIT_INVALID
    assert "no profile for jurisdiction 'fr' (available: es)" in err


def test_a_profile_declaring_an_unknown_policy_fails(config_dir):
    _write(
        config_dir / "jurisdictions" / "es.yaml",
        "id: es\nname: Spain\npolicies:\n  eligibility: eu_priips\n",
    )
    status, _, err = _run("--config-dir", str(config_dir), "config", "validate")
    assert status == EXIT_INVALID
    assert "no policy kind 'eligibility' exists" in err


def test_a_bad_environment_fails_loudly(config_dir, monkeypatch):
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "staging")
    status, _, err = _run("--config-dir", str(config_dir), "config", "validate")
    assert status == EXIT_INVALID
    assert "OBLISK_IBKR_PROFILE: must be 'paper' or 'live'" in err


def test_the_configured_logging_is_installed(config_dir):
    _run("--config-dir", str(config_dir), "config", "validate")
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert type(root.handlers[0].formatter).__name__ == "JsonFormatter"


def test_version(capsys):
    with pytest.raises(SystemExit) as raised:
        _run("--version")
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"obliskiq {__version__}"


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as raised:
        _run()
    assert raised.value.code == 2
    assert "COMMAND" in capsys.readouterr().err


def test_main_uses_the_real_streams(config_dir, capsys, monkeypatch):
    monkeypatch.setattr("obliskiq.cli.app.default_env_file", lambda: None)
    assert main(["--config-dir", str(config_dir), "config", "validate"]) == EXIT_OK
    assert "configuration is valid" in capsys.readouterr().out


def test_an_env_file_named_on_the_command_line_is_read(config_dir, tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("OBLISK_LOG_FORMAT=text\n", encoding="utf-8")
    status, out, _ = _run(
        "--env-file", str(env_file), "--config-dir", str(config_dir), "settings", "show"
    )
    assert status == EXIT_OK
    assert "environment (OBLISK_LOG_FORMAT)" in out


def test_a_missing_env_file_fails_loudly(config_dir, tmp_path):
    missing = tmp_path / "absent.env"
    status, _, err = _run(
        "--env-file", str(missing), "--config-dir", str(config_dir), "config", "validate"
    )
    assert status == EXIT_INVALID
    assert f"env file not found: {missing}" in err


def test_dev_null_means_no_env_file(config_dir):
    status, _, _ = _run(
        "--env-file", "/dev/null", "--config-dir", str(config_dir), "config", "validate"
    )
    assert status == EXIT_OK


def test_a_directory_is_not_an_env_file(config_dir, tmp_path):
    status, _, err = _run(
        "--env-file", str(tmp_path), "--config-dir", str(config_dir), "config", "validate"
    )
    assert status == EXIT_INVALID
    assert "env file not found" in err
