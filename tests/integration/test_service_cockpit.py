"""The service's side of the cockpit: the board and the dashboard after the digest, a cycle the
owner's pause skips, and the settings watch that applies a valid change of the owner's panel at
the next cycle boundary and alerts an invalid one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from service_rig import ServiceRig

from obliskiq.config import load_config
from obliskiq.orchestration import jobs as jobs_module
from obliskiq.orchestration.pause import set_pause
from obliskiq.orchestration.settings_watch import SettingsWatch
from obliskiq.store import runs as run_repo

MONDAY = date(2026, 10, 26)
BEFORE_EUROPE = datetime(2026, 10, 26, 8, 55, tzinfo=UTC)
DIGEST = datetime(2026, 10, 26, 20, 30, tzinfo=UTC)


async def run_until(
    s: ServiceRig, start: datetime, until: datetime, watch: SettingsWatch | None = None
) -> bool:
    s.clock.wall.now = start
    return await s.service.run(asyncio.Event(), until=until, watch=watch)


async def test_the_digest_writes_the_board_and_sends_the_dashboard(make: Any) -> None:
    s = await make()
    await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=20))
    await run_until(s, DIGEST - timedelta(minutes=1), DIGEST + timedelta(minutes=1))
    data_dir: Path = s.rig.data_dir
    board = json.loads((data_dir / "board.json").read_text(encoding="utf-8"))
    assert board["mode"] == "shadow"
    assert any(p["symbol"] == "IBGM" for p in board["book"]["positions"])
    assert board["performance"]["measured"] is True
    assert isinstance(board["patterns"], list)
    page = (data_dir / "dashboard.html").read_text(encoding="utf-8")
    assert "IBGM" in page
    ((caption, path),) = s.notifier.files
    assert caption == "ObliskIQ dashboard 2026-10-26"
    assert path == data_dir / "dashboard.html"
    (record,) = await s.rig.store(run_repo.runs_for, command="digest", session_date=MONDAY)
    assert (record.status, record.steps_completed) == ("ok", 2)


async def test_a_dashboard_without_its_extras_still_goes_out(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no marks")

    monkeypatch.setattr(jobs_module, "scoreboard_performance", broken)
    monkeypatch.setattr(jobs_module, "repository_root", lambda: None)
    s = await make()
    s.notifier.files.clear()
    await run_until(s, DIGEST - timedelta(minutes=1), DIGEST + timedelta(minutes=1))
    board = json.loads((s.rig.data_dir / "board.json").read_text(encoding="utf-8"))
    assert board["performance"]["reason"] == "the performance cannot be measured: RuntimeError"
    assert board["patterns"] == "no research registry beside this installation"
    assert len(s.notifier.files) == 1


async def test_a_dashboard_that_cannot_be_sent_is_degraded(make: Any) -> None:
    s = await make()

    async def refuse(caption: str, path: Path) -> bool:
        return False

    s.notifier.send_document = refuse  # type: ignore[method-assign]
    await run_until(s, DIGEST - timedelta(minutes=1), DIGEST + timedelta(minutes=1))
    (record,) = await s.rig.store(run_repo.runs_for, command="digest", session_date=MONDAY)
    assert record.status == "degraded"
    assert (s.rig.data_dir / "dashboard.html").exists()


async def test_a_paused_bot_skips_its_cycles_and_records_why(make: Any) -> None:
    s = await make()
    flag = s.rig.data_dir / "PAUSED"
    set_pause(flag, actor="owner", reason="a cloud session uses the paper login", at=BEFORE_EUROPE)
    await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=20))
    assert s.placed() == []
    runs = await s.rig.store(run_repo.runs_since, since=MONDAY)
    cycles = [run for run in runs if run.command.startswith("cycle:")]
    assert cycles
    assert all(run.status == "skipped" for run in cycles)
    detail = json.dumps(cycles[0].detail)
    assert "a cloud session uses the paper login" in detail


async def test_a_valid_change_of_the_panel_stops_the_loop_at_the_boundary(
    make: Any, config_dir: Path
) -> None:
    s = await make()
    owner = config_dir / "owner.yaml"
    watch = SettingsWatch(owner, reload=lambda: load_config(config_dir))
    with owner.open("a", encoding="utf-8") as handle:
        handle.write("thesis:\n  max_positions: 6\n")
    restart = await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=20), watch)
    assert restart is True
    assert s.placed() == []
    assert s.notifier.alerts("settings.applied")


async def test_an_invalid_change_is_alerted_once_and_the_service_goes_on(
    make: Any, config_dir: Path
) -> None:
    s = await make()
    owner = config_dir / "owner.yaml"
    watch = SettingsWatch(owner, reload=lambda: load_config(config_dir))
    with owner.open("a", encoding="utf-8") as handle:
        handle.write("thesis:\n  max_positions: -1\n")
    restart = await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=20), watch)
    assert restart is False
    (alert,) = s.notifier.alerts("settings.invalid")
    assert "the settings in force stay" in alert
