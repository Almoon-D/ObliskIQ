"""The append-only settings-change audit log.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import os
import stat
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from obliskiq.config import load_config
from obliskiq.config.audit import (
    GENESIS_HASH,
    SettingChange,
    SettingsAuditLog,
    diff_settings,
)
from obliskiq.env import load_environment
from obliskiq.errors import EnvVarError, SettingsAuditError

WHEN = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def log(tmp_path):
    return SettingsAuditLog(tmp_path / "data" / "settings-audit.jsonl")


def _record(log: SettingsAuditLog, *changes: SettingChange, at: datetime = WHEN):
    return log.record(changes, actor="owner", channel="cli", reason="test", at=at)


def test_diff_lists_only_changed_settings(config_dir):
    before = load_config(config_dir).settings
    after = before.model_copy(
        update={"logging": before.logging.model_copy(update={"level": "DEBUG"})}
    )
    assert diff_settings(before, after) == (
        SettingChange(key="logging.level", old="INFO", new="DEBUG"),
    )
    assert diff_settings(before, before) == ()


def test_a_missing_file_has_no_entries(log):
    assert log.entries() == ()


def test_entries_are_appended_in_sequence_and_chained(log):
    first = _record(log, SettingChange(key="logging.level", old="INFO", new="DEBUG"))
    second = _record(
        log,
        SettingChange(key="logging.level", old="DEBUG", new="INFO"),
        SettingChange(key="logging.format", old="json", new="text"),
        at=WHEN + timedelta(minutes=1),
    )
    entries = log.entries()
    assert entries == first + second
    assert [entry.seq for entry in entries] == [1, 2, 3]
    assert entries[0].prev_hash == GENESIS_HASH
    assert entries[1].prev_hash == entries[0].hash
    assert entries[2].prev_hash == entries[1].hash
    assert entries[0].actor == "owner"
    assert entries[0].channel == "cli"
    assert entries[0].reason == "test"


def test_the_file_only_grows(log):
    _record(log, SettingChange(key="a", old=1, new=2))
    size = log.path.stat().st_size
    before = log.path.read_bytes()
    _record(log, SettingChange(key="a", old=2, new=3))
    assert log.path.read_bytes().startswith(before)
    assert log.path.stat().st_size > size


def test_timestamps_are_stored_in_utc(log):
    madrid = timezone(timedelta(hours=2))
    (entry,) = _record(log, SettingChange(key="a", old=None, new=1), at=WHEN.astimezone(madrid))
    assert entry.at == WHEN
    assert json.loads(log.path.read_text(encoding="utf-8"))["at"] == "2026-10-08T12:00:00Z"


def test_a_naive_timestamp_is_refused(log):
    with pytest.raises(SettingsAuditError, match="timezone-aware"):
        _record(log, SettingChange(key="a", old=None, new=1), at=datetime(2026, 10, 8))


def test_no_changes_writes_nothing(log):
    assert _record(log) == ()
    assert not log.path.exists()


def test_the_log_is_private_to_the_owner(log):
    _record(log, SettingChange(key="a", old=None, new=1))
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600


def test_an_edited_entry_breaks_the_chain(log):
    _record(log, SettingChange(key="logging.level", old="INFO", new="DEBUG"))
    text = log.path.read_text(encoding="utf-8").replace('"DEBUG"', '"ERROR"')
    log.path.write_text(text, encoding="utf-8")
    with pytest.raises(SettingsAuditError, match="line 1 was altered after it was written"):
        log.entries()


def test_a_removed_entry_breaks_the_chain(log):
    _record(log, SettingChange(key="a", old=1, new=2))
    _record(log, SettingChange(key="a", old=2, new=3))
    _record(log, SettingChange(key="a", old=3, new=4))
    lines = log.path.read_text(encoding="utf-8").splitlines(keepends=True)
    log.path.write_text(lines[0] + lines[2], encoding="utf-8")
    with pytest.raises(SettingsAuditError, match="line 2 has sequence 3, expected 2"):
        log.entries()


def test_reordered_entries_break_the_chain(log):
    _record(log, SettingChange(key="a", old=1, new=2))
    _record(log, SettingChange(key="a", old=2, new=3))
    lines = log.path.read_text(encoding="utf-8").splitlines(keepends=True)
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    first["seq"], second["seq"] = 2, 1
    log.path.write_text(json.dumps(second) + "\n" + json.dumps(first) + "\n", encoding="utf-8")
    with pytest.raises(SettingsAuditError, match="does not follow the entry before it"):
        log.entries()


def test_a_corrupt_line_is_refused(log):
    log.path.parent.mkdir(parents=True)
    log.path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(SettingsAuditError, match="line 1 is not a valid audit entry"):
        log.entries()


def test_a_corrupt_log_refuses_new_entries(log):
    log.path.parent.mkdir(parents=True)
    log.path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SettingsAuditError):
        _record(log, SettingChange(key="a", old=None, new=1))


def test_an_invalid_change_is_refused(log):
    with pytest.raises(SettingsAuditError, match="invalid audit entry"):
        log.record(
            [SettingChange(key="a", old=None, new=1)],
            actor="",
            channel="cli",
            reason=None,
            at=WHEN,
        )


def test_an_unreadable_log_fails_loudly(tmp_path):
    directory = tmp_path / "log.jsonl"
    directory.mkdir()
    with pytest.raises(SettingsAuditError, match="cannot be read"):
        SettingsAuditLog(directory).entries()


def test_a_location_under_a_file_fails_loudly(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    log = SettingsAuditLog(blocker / "sub" / "log.jsonl")
    with pytest.raises(SettingsAuditError, match="cannot be opened for appending"):
        _record(log, SettingChange(key="a", old=None, new=1))
    with pytest.raises(SettingsAuditError, match="cannot be read"):
        log.entries()


def test_a_log_that_cannot_be_opened_fails_loudly(log, monkeypatch):
    def refuse(*args: object) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(SettingsAuditError, match="cannot be opened for appending"):
        _record(log, SettingChange(key="a", old=None, new=1))


def test_a_failed_write_fails_loudly(log, monkeypatch):
    def refuse(descriptor: int, payload: bytes) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", refuse)
    with pytest.raises(SettingsAuditError, match="could not be written"):
        _record(log, SettingChange(key="a", old=None, new=1))


def test_the_log_lives_in_the_data_dir_at_the_configured_name(config_dir, tmp_path, monkeypatch):
    settings = load_config(config_dir).settings
    monkeypatch.setenv("OBLISK_DATA_DIR", str(tmp_path / "state"))
    log = SettingsAuditLog.for_settings(settings, load_environment(env_file=None))
    assert log.path == tmp_path / "state" / "settings-audit.jsonl"


def test_without_a_data_dir_there_is_no_log(config_dir):
    settings = load_config(config_dir).settings
    with pytest.raises(EnvVarError, match="OBLISK_DATA_DIR"):
        SettingsAuditLog.for_settings(settings, load_environment(env_file=None))


def test_a_non_finite_value_is_refused_before_anything_is_written(log):
    with pytest.raises(ValidationError):
        SettingChange(key="a", old=None, new=float("nan"))
    assert not log.path.exists()


def test_floats_round_trip_through_the_chain(log):
    _record(log, SettingChange(key="threshold", old=0.1, new=1e-7))
    _record(log, SettingChange(key="threshold", old=1e-7, new=2.5))
    assert [entry.new for entry in log.entries()] == [1e-7, 2.5]


def test_the_hash_covers_the_bytes_on_disk(log):
    """Verification reads the line as written, so formatting cannot drift from the hash."""
    _record(log, SettingChange(key="a", old=None, new={"nested": [1, "two", None]}))
    raw = json.loads(log.path.read_text(encoding="utf-8"))
    raw["new"] = {"nested": [1, "two"]}
    log.path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    with pytest.raises(SettingsAuditError, match="altered"):
        log.entries()


def test_a_short_write_is_completed(log, monkeypatch):
    real_write = os.write

    def one_byte_at_a_time(descriptor: int, payload: bytes) -> int:
        return real_write(descriptor, bytes(payload[:1]))

    monkeypatch.setattr(os, "write", one_byte_at_a_time)
    _record(log, SettingChange(key="a", old=None, new="x"))
    monkeypatch.undo()
    assert [entry.new for entry in log.entries()] == ["x"]


def test_a_write_that_accepts_nothing_fails_loudly(log, monkeypatch):
    monkeypatch.setattr(os, "write", lambda *_args: 0)
    with pytest.raises(SettingsAuditError, match="accepted no bytes"):
        _record(log, SettingChange(key="a", old=None, new=1))


def test_concurrent_writers_keep_one_unbroken_chain(log):
    """Two writers on separate descriptors serialise on the lock."""
    rounds = 40

    def write(name: str) -> None:
        writer = SettingsAuditLog(log.path)
        for number in range(rounds):
            _record(writer, SettingChange(key=name, old=number, new=number + 1))

    threads = [
        threading.Thread(target=write, args=(name,)) for name in ("cli", "service", "cockpit")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entries = log.entries()
    assert [entry.seq for entry in entries] == list(range(1, 3 * rounds + 1))
