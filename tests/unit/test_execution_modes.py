"""Execution modes: explicit, audited upgrades; automatic downgrades; what each mode may write.

Adapted from the legacy paper gate (legacy/old/ObliskIQ/tests/test_paper_gate.py,
``test_assert_live_trading_allowed_raises_when_not_pass``): the live levels stay blocked until
the promotion gate exists and passes; here it does not exist yet, so they are refused outright.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import Path
from typing import Any

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import ExecutionMode
from obliskiq.errors import ModeChangeRefusedError
from obliskiq.execution.modes import PROMOTION_UNTESTED, Modes, write_refusal
from obliskiq.store import open_database
from obliskiq.store import orders as repo
from obliskiq.store.worker import StoreWorker


@pytest.fixture
async def fresh(shipped_settings: ObliskConfig, tmp_path: Path) -> Any:
    worker = StoreWorker(
        lambda: open_database(tmp_path / "modes.db", busy_timeout_ms=1000, synchronous="NORMAL")
    )
    yield worker
    await worker.aclose()


async def test_the_initial_mode_comes_from_configuration(
    fresh: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    modes = Modes(fresh, shipped_settings, profile="paper")
    assert await modes.current() is shipped_settings.execution.modes.initial
    assert await modes.current() is ExecutionMode.SHADOW


async def test_an_upgrade_to_paper_is_an_audited_owner_command(
    fresh: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    modes = Modes(fresh, shipped_settings, profile="paper")
    change = await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="ten clean days")
    assert await modes.current() is ExecutionMode.PAPER
    recorded = await fresh.run(repo.latest_mode_change)
    assert recorded == change.model_copy(update={"at": change.at.replace(microsecond=0)})
    assert recorded is not None
    assert PROMOTION_UNTESTED in recorded.reason
    assert recorded.direction == "upgrade"
    assert recorded.actor == "owner"


@pytest.mark.parametrize("profile", ["live", None])
async def test_paper_needs_the_paper_profile(
    fresh: StoreWorker, shipped_settings: ObliskConfig, profile: Any
) -> None:
    modes = Modes(fresh, shipped_settings, profile=profile)
    with pytest.raises(ModeChangeRefusedError):
        await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="r")
    assert await modes.current() is ExecutionMode.SHADOW


@pytest.mark.parametrize("target", [ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE])
@pytest.mark.parametrize("profile", ["paper", "live"])
async def test_the_live_levels_wait_for_the_promotion_gate(
    fresh: StoreWorker, shipped_settings: ObliskConfig, target: ExecutionMode, profile: Any
) -> None:
    modes = Modes(fresh, shipped_settings, profile=profile)
    with pytest.raises(ModeChangeRefusedError, match="promotion_gate_unavailable"):
        await modes.upgrade(target, actor="owner", reason="r")
    assert await fresh.run(repo.latest_mode_change) is None


async def test_an_upgrade_must_go_up(fresh: StoreWorker, shipped_settings: ObliskConfig) -> None:
    modes = Modes(fresh, shipped_settings, profile="paper")
    with pytest.raises(ModeChangeRefusedError):
        await modes.upgrade(ExecutionMode.SHADOW, actor="owner", reason="r")


async def test_downgrades_are_automatic_and_audited(
    fresh: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    modes = Modes(fresh, shipped_settings, profile="paper")
    assert await modes.downgrade(ExecutionMode.SHADOW, actor="system", reason="r") is None
    await modes.upgrade(ExecutionMode.PAPER, actor="owner", reason="r")
    change = await modes.downgrade(ExecutionMode.SHADOW, actor="system", reason="breaker")
    assert change is not None
    assert change.direction == "downgrade"
    assert await modes.current() is ExecutionMode.SHADOW
    assert modes.profile == "paper"


def test_what_each_mode_may_write() -> None:
    assert write_refusal(ExecutionMode.SHADOW, "paper") is not None
    assert write_refusal(ExecutionMode.SHADOW, "live") is not None
    assert write_refusal(ExecutionMode.PAPER, "paper") is None
    assert write_refusal(ExecutionMode.PAPER, "live") is not None
    assert write_refusal(ExecutionMode.PAPER, None) is not None
    assert write_refusal(ExecutionMode.LIVE_SMALL, "live") is None
    assert write_refusal(ExecutionMode.LIVE_SMALL, "paper") is not None
    assert write_refusal(ExecutionMode.LIVE, "live") is None
    assert write_refusal(ExecutionMode.LIVE, "paper") is not None
