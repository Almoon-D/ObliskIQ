"""Shared fixtures.

Every test runs with the variables ObliskIQ reads removed from the environment, so a
developer's own credentials or overrides can never change a result.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import shutil
from pathlib import Path

import pytest

from obliskiq.env import variable_names

ROOT = Path(__file__).resolve().parents[1]


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
