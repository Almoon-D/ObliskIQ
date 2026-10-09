"""The installed ``obliskiq`` script, run as a separate process the way the owner runs it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from obliskiq.env import variable_names

SCRIPT = Path(sys.executable).parent / "obliskiq"


def _run(*argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the script with none of ObliskIQ's variables and an empty env file, so neither the
    developer's environment nor a ``.env`` in the checkout can change the result."""
    env = {name: value for name, value in os.environ.items() if name not in variable_names()}
    empty_env_file = cwd / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")
    return subprocess.run(
        [str(SCRIPT), "--env-file", str(empty_env_file), *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(autouse=True)
def _script_installed():
    if not SCRIPT.is_file():  # pragma: no cover - the environment always installs it
        pytest.fail(f"the obliskiq script is not installed at {SCRIPT}")


def test_config_validate_succeeds_on_the_shipped_configuration(config_dir, tmp_path):
    result = _run("--config-dir", str(config_dir), "config", "validate", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "configuration is valid" in result.stdout
    assert "jurisdiction   es (Spain)" in result.stdout


def test_settings_show_succeeds(config_dir, tmp_path):
    result = _run("--config-dir", str(config_dir), "settings", "show", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "account.base_currency" in result.stdout


def test_an_invalid_owner_file_exits_one_with_a_readable_message(config_dir, tmp_path):
    (config_dir / "owner.yaml").write_text(
        "account:\n  base_currency: euro\njurisdiction: es\n", encoding="utf-8"
    )
    result = _run("--config-dir", str(config_dir), "config", "validate", cwd=tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "config/owner.yaml: account.base_currency: must be a three-letter" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_module_runs_too(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "obliskiq", "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("obliskiq ")
