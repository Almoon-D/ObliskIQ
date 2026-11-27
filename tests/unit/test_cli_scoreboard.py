"""``obliskiq scoreboard`` and ``obliskiq promotion``, run in-process.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from pathlib import Path

import pytest

from obliskiq.cli import scoreboard as cli
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Path:
    path = tmp_path / "data"
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    return path


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue()


def test_grade_on_an_empty_store(data_dir: Path) -> None:
    status, out = _run("scoreboard", "grade")
    assert status == EXIT_OK
    assert "no layer has a call yet" in out


def test_grade_without_the_repository_s_rules(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "repository_root", lambda: None)
    status, out = _run("scoreboard", "grade")
    assert status == EXIT_OK
    assert "not graded" in out


def test_the_ledger_on_an_empty_store_and_without_its_rules(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, out = _run("scoreboard", "ledger")
    assert status == EXIT_OK
    assert "No idea has been written into the ledger yet" in out
    assert "- no replay run (obliskiq research replay)." in out
    monkeypatch.setattr(cli, "repository_root", lambda: None)
    status, out = _run("scoreboard", "ledger")
    assert status == EXIT_OK
    assert "Not graded" in out


def test_the_replay_refuses_to_run_without_its_keys(data_dir: Path) -> None:
    status, out = _run("research", "replay", "run")
    assert status == EXIT_INVALID
    assert "refused: GEMINI_API_KEY unset: no segment is replayed" in out
    status, out = _run("research", "replay", "probe")
    assert status == EXIT_INVALID
    assert "refused: GEMINI_API_KEY unset: the probe asks nothing" in out


def test_a_weekly_and_a_monthly_report_are_written(data_dir: Path) -> None:
    status, out = _run("scoreboard", "report", "weekly")
    assert status == EXIT_OK
    assert "# Scoreboard, week to" in out
    status, out = _run("scoreboard", "report", "monthly", "--send")
    assert status == EXIT_OK
    assert "## Realised tax" in out
    assert "summary sent" in out
    assert len(list((data_dir / "reports").glob("scoreboard-*.md"))) == 2


def test_the_month_end_run(data_dir: Path) -> None:
    status, out = _run("scoreboard", "month", "--month", "2026-10")
    assert status == EXIT_OK
    assert "month end 2026-10" in out
    # The owner's share of 1, held to H13's live ceiling.
    assert "thesis_book -> 50.00%" in out
    status, out = _run("scoreboard", "month", "--month", "2026-10")
    assert "nothing changed" in out


def test_the_month_end_run_without_rules(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "repository_root", lambda: None)
    status, out = _run("scoreboard", "month")
    assert status == EXIT_OK
    assert "nothing run" in out


def test_unfreeze_refuses_what_is_not_a_frozen_layer(data_dir: Path) -> None:
    status, out = _run("scoreboard", "unfreeze", "nope", "--reason", "r")
    assert status == EXIT_INVALID
    assert "not a layer" in out


def test_unfreeze_lifts_a_freeze(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from obliskiq.config import load_config
    from obliskiq.store import open_store
    from obliskiq.store import scoreboard as repo
    from obliskiq.store.scoreboard import LayerEvent

    root = tmp_path / "repo"
    (root / "research").mkdir(parents=True)
    (root / "research" / "pattern_registry.yaml").write_text(
        "patterns:\n  - id: pat\n    status: active\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "repository_root", lambda: root)
    status, out = _run("scoreboard", "unfreeze", "pat", "--reason", "r")
    assert status == EXIT_INVALID
    assert "not frozen" in out
    settings = load_config(Path(__import__("os").environ["OBLISK_CONFIG_DIR"])).settings
    with open_store(settings, data_dir) as store:
        repo.record_layer_event(
            store, LayerEvent(datetime.now(UTC), "pat", "freeze", "scoreboard", "gap")
        )
    status, out = _run("scoreboard", "unfreeze", "pat", "--reason", "the spread was misread")
    assert status == EXIT_OK
    assert "unfrozen" in out


def test_promotion_status_and_a_drill(data_dir: Path) -> None:
    status, out = _run("promotion", "status")
    assert status == EXIT_OK
    assert "execution mode shadow; paper: not met" in out
    assert "[UNMEASURED] every daily reset re-logged unattended" in out
    status, out = _run("promotion", "status", "--to", "live")
    assert "one level at a time" in out
    status, out = _run("promotion", "drill", "kill", "passed", "--detail", "set and cleared")
    assert status == EXIT_OK
    assert "kill drill recorded in shadow: passed" in out


def test_promotion_status_at_the_top(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from obliskiq.domain.orders import ExecutionMode
    from obliskiq.execution.modes import Modes

    async def live(self: Modes) -> ExecutionMode:
        return ExecutionMode.LIVE

    monkeypatch.setattr(Modes, "current", live)
    status, out = _run("promotion", "status")
    assert status == EXIT_OK
    assert "the top of the ladder" in out


async def test_the_verdict_handed_to_an_upgrade(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from types import SimpleNamespace

    from obliskiq.config import load_config
    from obliskiq.domain.orders import ExecutionMode
    from obliskiq.env import load_environment
    from obliskiq.errors import ObliskError
    from obliskiq.scoreboard.service import Scoreboard
    from obliskiq.store import open_store
    from obliskiq.store.worker import StoreWorker

    settings = load_config(Path(os.environ["OBLISK_CONFIG_DIR"])).settings
    loaded = SimpleNamespace(
        config=SimpleNamespace(settings=settings), env=load_environment(env_file=None)
    )
    worker = StoreWorker(lambda: open_store(settings, data_dir))
    try:
        top = await cli.promotion_verdict(loaded, worker, ExecutionMode.LIVE, ExecutionMode.LIVE)  # type: ignore[arg-type]
        assert not top.passed

        async def broken(self: Scoreboard, *a: object, **k: object) -> None:
            raise ObliskError("no store")

        async def nothing(self: Scoreboard, *a: object, **k: object) -> None:
            return None

        monkeypatch.setattr(Scoreboard, "verdict", nothing)
        top_again = await cli.promotion_verdict(
            loaded,  # type: ignore[arg-type]
            worker,
            ExecutionMode.LIVE,
            ExecutionMode.LIVE,
        )
        assert "top of the ladder" in top_again.summary
        monkeypatch.setattr(Scoreboard, "verdict", broken)
        failed = await cli.promotion_verdict(
            loaded, worker, ExecutionMode.PAPER, ExecutionMode.LIVE_SMALL
        )  # type: ignore[arg-type]
        assert not failed.passed
        assert "could not be read" in failed.summary
    finally:
        await worker.aclose()


def test_grade_lists_each_layer_and_its_calibration(data_dir: Path) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    from obliskiq.config import load_config
    from obliskiq.store import open_store
    from obliskiq.store import scoreboard as repo
    from obliskiq.store.scoreboard import Call

    settings = load_config(Path(os.environ["OBLISK_CONFIG_DIR"])).settings
    with open_store(settings, data_dir) as store:
        repo.record_call(
            store,
            Call("signal", 1, "up", datetime.now(UTC) - timedelta(days=2), "d", probability=0.6),
        )
    status, out = _run("scoreboard", "grade")
    assert status == EXIT_OK
    assert "signal: 0 graded of 1 calls" in out
    assert "signal: uncalibrated (shrink 0.00)" in out
