"""The service's settings watch, the audit of changes no command recorded, the pause flag and the
``settings set|diff|history`` and ``pause`` commands.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import load_config
from obliskiq.config.audit import SettingChange, SettingsAuditLog
from obliskiq.config.changes import settings_fingerprint
from obliskiq.errors import ConfigError
from obliskiq.orchestration.health import write_heartbeat
from obliskiq.orchestration.pause import clear_pause, read_pause, set_pause
from obliskiq.orchestration.settings_watch import (
    DEFAULTS_CHANNEL,
    FILE_CHANNEL,
    SettingsWatch,
    audit_unrecorded,
)

AT = datetime(2026, 11, 16, 9, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


def _append(path: Path, text: str) -> None:
    """Text at the end of ``path``: owner.yaml ends in its ``thesis`` section, so an indented key
    lands in it."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


# ── the watch ──


def test_the_watch_sees_no_change_until_the_panel_changes(config_dir: Path) -> None:
    owner = config_dir / "owner.yaml"
    watch = SettingsWatch(owner, reload=lambda: load_config(config_dir))
    assert watch.owner_file == owner
    assert not watch.check().changed
    _append(owner, "  max_positions: 6\n")
    found = watch.check()
    assert found.changed
    assert found.problem is None


def test_an_invalid_change_is_reported_once_and_changes_nothing(config_dir: Path) -> None:
    owner = config_dir / "owner.yaml"
    watch = SettingsWatch(owner, reload=lambda: load_config(config_dir))
    _append(owner, "  max_positions: -1\n")
    first = watch.check()
    assert not first.changed
    assert first.problem is not None
    assert "max_positions" in first.problem
    second = watch.check()
    assert (second.changed, second.problem) == (False, None)


# ── the audit of what no command recorded ──


def test_the_first_start_records_the_settings_only(config_dir: Path, tmp_path: Path) -> None:
    loaded = load_config(config_dir)
    audit = SettingsAuditLog(tmp_path / "audit.jsonl")
    assert audit_unrecorded(loaded, tmp_path, audit, at=AT) == ()
    snapshot = json.loads((tmp_path / loaded.settings.paths.settings_applied).read_text())
    assert snapshot["core.variant"] == "A"


def test_a_hand_edit_and_a_new_default_are_audited_by_their_source(
    config_dir: Path, tmp_path: Path
) -> None:
    audit = SettingsAuditLog(tmp_path / "audit.jsonl")
    audit_unrecorded(load_config(config_dir), tmp_path, audit, at=AT)
    _append(config_dir / "owner.yaml", "  max_positions: 6\n")
    defaults = config_dir / "defaults" / "reporting.yaml"
    defaults.write_text(
        defaults.read_text(encoding="utf-8").replace("digest_rows: 10", "digest_rows: 12"),
        encoding="utf-8",
    )
    entries = audit_unrecorded(load_config(config_dir), tmp_path, audit, at=AT)
    found = {(e.key, e.channel, e.old, e.new) for e in entries}
    assert found == {
        ("thesis.max_positions", FILE_CHANNEL, 8, 6),
        ("reporting.digest_rows", DEFAULTS_CHANNEL, 10, 12),
    }
    assert audit_unrecorded(load_config(config_dir), tmp_path, audit, at=AT) == ()


def test_a_change_a_command_audited_is_not_audited_twice(config_dir: Path, tmp_path: Path) -> None:
    audit = SettingsAuditLog(tmp_path / "audit.jsonl")
    audit_unrecorded(load_config(config_dir), tmp_path, audit, at=AT)
    _append(config_dir / "owner.yaml", "  max_positions: 6\n")
    audit.record(
        [SettingChange(key="thesis.max_positions", old=8, new=6)],
        actor="owner",
        channel="cli",
        reason=None,
        at=AT,
    )
    assert audit_unrecorded(load_config(config_dir), tmp_path, audit, at=AT) == ()


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_an_unreadable_record_of_the_last_start_fails_loudly(
    config_dir: Path, tmp_path: Path, content: str
) -> None:
    loaded = load_config(config_dir)
    (tmp_path / loaded.settings.paths.settings_applied).write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="settings last applied"):
        audit_unrecorded(loaded, tmp_path, SettingsAuditLog(tmp_path / "a.jsonl"), at=AT)


# ── the pause ──


def test_a_pause_is_set_read_and_cleared(tmp_path: Path) -> None:
    path = tmp_path / "PAUSED"
    assert read_pause(path).describe() == "not paused"
    set_pause(path, actor="owner", reason="a cloud session", at=AT)
    state = read_pause(path)
    assert (state.on, state.actor, state.reason, state.at) == (True, "owner", "a cloud session", AT)
    assert state.describe() == f"paused by owner since {AT.isoformat()}: a cloud session"
    assert not clear_pause(path).on
    assert not read_pause(path).on


def test_a_pause_flag_that_cannot_be_read_holds(tmp_path: Path) -> None:
    folder = tmp_path / "PAUSED"
    folder.mkdir()
    state = read_pause(folder)
    assert state.on
    assert "cannot be read" in state.describe()
    garbled = tmp_path / "garbled"
    garbled.write_text("{}", encoding="utf-8")
    assert read_pause(garbled).describe() == ("paused by someone: the pause flag is unreadable")


# ── the commands ──


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    return path


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


def test_settings_set_previews_then_writes_and_audits(data_dir: Path, config_dir: Path) -> None:
    status, out = _run("settings", "set", "thesis.max_positions", "6", "--reason", "fewer")
    assert status == EXIT_INVALID
    assert "thesis.max_positions: 8 -> 6" in out
    assert "nothing was changed" in out
    status, out = _run(
        "settings", "set", "thesis.max_positions", "6", "--reason", "fewer", "--confirm"
    )
    assert status == EXIT_OK
    assert "changed and audited (1 entry)" in out
    assert load_config(config_dir).settings.thesis.max_positions == 6
    status, out = _run("settings", "history")
    assert status == EXIT_OK
    assert "thesis.max_positions: 8 -> 6 by owner (command line) via cli (fewer)" in out


def test_settings_set_refuses_what_does_not_validate(data_dir: Path) -> None:
    status, out = _run("settings", "set", "capital.budgets.core", "2", "--reason", "x")
    assert status == EXIT_INVALID
    assert out.startswith("refused: the configuration would be invalid")


def test_settings_set_refuses_when_the_audit_cannot_be_written(
    data_dir: Path, config_dir: Path
) -> None:
    (data_dir / "settings-audit.jsonl").write_text("garbage\n", encoding="utf-8")
    status, out = _run("settings", "set", "thesis.max_positions", "6", "--reason", "x", "--confirm")
    assert status == EXIT_INVALID
    assert "refused:" in out


def test_settings_history_with_a_broken_log_says_so(data_dir: Path) -> None:
    (data_dir / "settings-audit.jsonl").write_text("garbage\n", encoding="utf-8")
    status, out = _run("settings", "history", "--limit", "0")
    assert status == EXIT_INVALID
    assert "not a valid audit entry" in out


def test_settings_history_limits_and_lists_every_change(data_dir: Path) -> None:
    for value in ("7", "6", "5"):
        _run("settings", "set", "thesis.max_positions", value, "--reason", "x", "--confirm")
    _, out = _run("settings", "history", "--limit", "1")
    assert "3 change(s) audited; the latest 1:" in out
    _, out = _run("settings", "history", "--limit", "0")
    assert "the latest 3:" in out


def test_settings_diff_says_whether_the_service_runs_on_them(
    data_dir: Path, config_dir: Path
) -> None:
    status, out = _run("settings", "diff")
    assert status == EXIT_OK
    assert "core.variant: A (default A)" in out
    assert "no heartbeat says" in out
    settings = load_config(config_dir).settings
    write_heartbeat(data_dir / "heartbeat.json", AT, {"settings": settings_fingerprint(settings)})
    assert "the service runs on these settings" in _run("settings", "diff")[1]
    write_heartbeat(data_dir / "heartbeat.json", AT, {"settings": "older"})
    assert "next cycle boundary" in _run("settings", "diff")[1]


def test_settings_diff_without_a_data_directory_lists_the_panel(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    status, out = _run("settings", "diff")
    assert status == EXIT_OK
    assert "heartbeat" not in out


def test_pause_on_status_and_off(data_dir: Path) -> None:
    status, out = _run("pause", "on", "--reason", "a cloud session")
    assert status == EXIT_OK
    assert "paused by owner (command line)" in out
    assert "a cloud session" in _run("pause", "status")[1]
    assert _run("pause", "off")[1].strip() == "not paused"
