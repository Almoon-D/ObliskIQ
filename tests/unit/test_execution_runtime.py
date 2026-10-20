"""The order path a command opens: refused before any credential is read when the profile or
transport says so, started through the account guard, and closed with its store.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import Path
from typing import Any

import pytest
import respx
from fake_broker import ACCOUNT, FakeBroker

from obliskiq.domain.orders import ExecutionMode
from obliskiq.env import Environment
from obliskiq.errors import ExecutionError
from obliskiq.execution.instruments import ConfiguredDirectory
from obliskiq.execution.runtime import calendar_venues, open_order_path
from obliskiq.ibkr.errors import AccountGuardError
from obliskiq.jurisdiction import load_profile


def environment(monkeypatch: pytest.MonkeyPatch, **values: str) -> Environment:
    defaults = {
        "OBLISK_IBKR_PROFILE": "paper",
        "OBLISK_IBKR_ACCOUNT_ID": ACCOUNT,
        "OBLISK_IBKR_TRANSPORT": "gateway",
        "OBLISK_IBKR_GATEWAY_USERNAME": "paper-user",
        "OBLISK_IBKR_GATEWAY_PASSWORD": "paper-pass",
    }
    for name, value in {**defaults, **values}.items():
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    return Environment(_env_file=None)


def test_the_calendars_cover_the_directorys_venues(shipped_settings: Any) -> None:
    directory = ConfiguredDirectory.from_settings(shipped_settings)
    assert calendar_venues(shipped_settings, directory) == ("XAMS", "XETR", "XLON", "XNYS")


async def test_the_order_path_opens_over_the_broker_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    shipped_settings: Any,
    fake_transport: Any,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    env = environment(monkeypatch)
    profile = load_profile(repo_root / "config", "es")
    with respx.mock(assert_all_called=False) as router:
        FakeBroker(router)
        async with open_order_path(
            env, shipped_settings, profile, data_dir=tmp_path, transport=fake_transport
        ) as path:
            assert await path.modes.current() is ExecutionMode.SHADOW
            assert path.directory.describe(7089) is not None
            report = await path.flattener.flatten(actor="test")
            assert report.ok
            assert report.lines == ()
    assert fake_transport.closed == 1
    assert (tmp_path / shipped_settings.paths.database).exists()


@pytest.mark.parametrize(
    ("values", "error", "words"),
    [
        ({"OBLISK_IBKR_PROFILE": ""}, ExecutionError, "OBLISK_IBKR_PROFILE is unset"),
        ({"OBLISK_IBKR_PROFILE": "live"}, ExecutionError, "paper"),
        ({"OBLISK_IBKR_ACCOUNT_ID": "U1234567"}, AccountGuardError, "paper account"),
    ],
    ids=["no_profile", "gateway_live", "live_account_under_paper"],
)
async def test_the_order_path_refuses_before_reading_a_credential(
    monkeypatch: pytest.MonkeyPatch,
    shipped_settings: Any,
    tmp_path: Path,
    repo_root: Path,
    values: dict[str, str],
    error: type[Exception],
    words: str,
) -> None:
    env = environment(monkeypatch, **values)
    profile = load_profile(repo_root / "config", "es")
    with pytest.raises(error, match=words):
        async with open_order_path(env, shipped_settings, profile, data_dir=tmp_path):
            pass  # pragma: no cover - refused before it opens


async def test_the_order_path_builds_the_transport_the_environment_selects(
    monkeypatch: pytest.MonkeyPatch,
    shipped_settings: Any,
    fake_transport: Any,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    from obliskiq.execution import runtime

    built: list[Path] = []

    def build(env: Any, settings: Any, *, data_dir: Path, login_driver: Any) -> Any:
        built.append(data_dir)
        return fake_transport

    monkeypatch.setattr(runtime, "build_transport", build)
    env = environment(monkeypatch)
    profile = load_profile(repo_root / "config", "es")
    with respx.mock(assert_all_called=False) as router:
        FakeBroker(router)
        async with open_order_path(env, shipped_settings, profile, data_dir=tmp_path):
            pass
    assert built == [tmp_path]
