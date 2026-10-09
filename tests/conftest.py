"""Shared fixtures.

Every test runs with the variables ObliskIQ reads removed from the environment, so a
developer's own credentials or overrides can never change a result.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import os
import shutil
from pathlib import Path

import pytest
from hypothesis import settings

from obliskiq.env import variable_names

ROOT = Path(__file__).resolve().parents[1]

# Generated cases are never judged on speed (a slow runner is not a failing property); in CI
# the examples are derandomised so a red run reproduces from the commit alone.
settings.register_profile("default", deadline=None)
settings.register_profile("ci", deadline=None, derandomize=True)
settings.load_profile("ci" if os.environ.get("CI") else "default")


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in variable_names():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the shipped ``config/`` directory."""
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    return target
