"""The settings-change audit log.

The record of changes to effective settings: one JSON line per changed key with who made
the change, through which channel, when, the old and new values and why. The code that
applies a settings change appends to it through :meth:`SettingsAuditLog.record`.

The file is append-only: it is opened with ``O_APPEND`` and never rewritten. A writer holds
an exclusive lock while it reads the chain and appends, so two writers cannot take the same
sequence number; a reader holds a shared lock, so it never sees a half-written line. Each
entry carries a sequence number and the SHA-256 of the previous entry, taken over the entry
as written, parsed back and canonically encoded; an edited, reordered or removed entry breaks
the chain and reading the log fails loudly. A chain cannot reveal entries cut from the end.

Both reading and writing block on the lock, so asynchronous callers run them off the event
loop.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import fcntl
import hashlib
import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, Field, JsonValue, ValidationError

from ..env import Environment
from ..errors import SettingsAuditError
from .loader import flatten
from .schema import FrozenModel, NonEmptyText, ObliskConfig

#: The ``prev_hash`` of the first entry.
GENESIS_HASH = "0" * hashlib.sha256().digest_size * 2

#: Owner read and write only: the log records the owner's decisions.
LOG_FILE_MODE = 0o600
DATA_DIR_MODE = 0o700


class SettingChange(FrozenModel):
    """One effective setting that changed. ``None`` stands for "not set"."""

    key: NonEmptyText
    old: JsonValue
    new: JsonValue


class AuditEntry(FrozenModel):
    """One line of the log."""

    seq: int = Field(ge=1)
    at: AwareDatetime
    actor: NonEmptyText
    channel: NonEmptyText
    reason: str | None
    key: NonEmptyText
    old: JsonValue
    new: JsonValue
    prev_hash: str
    hash: str


def diff_settings(before: ObliskConfig, after: ObliskConfig) -> tuple[SettingChange, ...]:
    """The settings whose effective value differs, by dotted key."""
    old = flatten(before.model_dump(mode="json"))
    new = flatten(after.model_dump(mode="json"))
    return tuple(
        SettingChange(key=key, old=old.get(key), new=new.get(key))
        for key in sorted(old.keys() | new.keys())
        if old.get(key) != new.get(key)
    )


def _digest(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _draft(
    seq: int,
    change: SettingChange,
    actor: str,
    channel: str,
    reason: str | None,
    at: datetime,
    prev_hash: str,
) -> AuditEntry:
    """A validated entry without its hash yet."""
    try:
        return AuditEntry(
            seq=seq,
            at=at.astimezone(UTC),
            actor=actor,
            channel=channel,
            reason=reason,
            key=change.key,
            old=change.old,
            new=change.new,
            prev_hash=prev_hash,
            hash="",
        )
    except ValidationError as exc:
        raise SettingsAuditError(f"invalid audit entry for {change.key}: {exc}") from exc


class SettingsAuditLog:
    """The log at ``path``. Reading verifies the chain; writing only ever appends."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_settings(cls, settings: ObliskConfig, env: Environment) -> SettingsAuditLog:
        """The log in ``OBLISK_DATA_DIR`` at ``paths.settings_audit_log``."""
        return cls(env.require_data_dir() / settings.paths.settings_audit_log)

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> tuple[AuditEntry, ...]:
        """Every entry, after checking sequence and hash chain. No file means no entries."""
        try:
            descriptor = os.open(self._path, os.O_RDONLY)
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise SettingsAuditError(f"{self._path} cannot be read: {exc}") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            return self._read_entries()
        finally:
            os.close(descriptor)

    def record(
        self,
        changes: Iterable[SettingChange],
        *,
        actor: str,
        channel: str,
        reason: str | None,
        at: datetime,
    ) -> tuple[AuditEntry, ...]:
        """Append one entry per change and flush them to disk before returning them.

        Everything is validated before the file is touched, so a refused record leaves no
        trace.
        """
        if at.tzinfo is None:
            raise SettingsAuditError("an audit timestamp must be timezone-aware")
        pending = tuple(changes)
        for change in pending:
            _draft(1, change, actor, channel, reason, at, GENESIS_HASH)
        if not pending:
            return ()
        descriptor = self._open_for_append()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            existing = self._read_entries()
            previous = existing[-1].hash if existing else GENESIS_HASH
            written: list[AuditEntry] = []
            for offset, change in enumerate(pending, start=1):
                draft = _draft(len(existing) + offset, change, actor, channel, reason, at, previous)
                # Hash the entry as it will be read back, so the two can never disagree.
                body = json.loads(draft.model_dump_json(exclude={"hash"}))
                entry = draft.model_copy(update={"hash": _digest(body)})
                written.append(entry)
                previous = entry.hash
            payload = "".join(entry.model_dump_json() + "\n" for entry in written)
            _write_all(descriptor, payload.encode("utf-8"))
            os.fsync(descriptor)
        except OSError as exc:
            raise SettingsAuditError(f"{self._path} could not be written: {exc}") from exc
        finally:
            os.close(descriptor)
        return tuple(written)

    def _read_entries(self) -> tuple[AuditEntry, ...]:
        """Read and verify the chain. The caller holds the file open and locked, so a file
        that has vanished meanwhile is an error, not an empty log."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SettingsAuditError(f"{self._path} cannot be read: {exc}") from exc
        entries: list[AuditEntry] = []
        previous = GENESIS_HASH
        for number, line in enumerate(lines, start=1):
            where = f"{self._path} line {number}"
            try:
                raw = json.loads(line)
                entry = AuditEntry.model_validate(raw)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise SettingsAuditError(f"{where} is not a valid audit entry") from exc
            if entry.seq != number:
                raise SettingsAuditError(f"{where} has sequence {entry.seq}, expected {number}")
            if entry.prev_hash != previous:
                raise SettingsAuditError(f"{where} does not follow the entry before it")
            body = {key: value for key, value in raw.items() if key != "hash"}
            if _digest(body) != entry.hash:
                raise SettingsAuditError(f"{where} was altered after it was written")
            entries.append(entry)
            previous = entry.hash
        return tuple(entries)

    def _open_for_append(self) -> int:
        try:
            self._path.parent.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
            return os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, LOG_FILE_MODE)
        except OSError as exc:
            raise SettingsAuditError(f"{self._path} cannot be opened for appending: {exc}") from exc


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write every byte: ``os.write`` may write fewer than it was given."""
    remaining = memoryview(payload)
    while remaining:
        count = os.write(descriptor, remaining)
        if count <= 0:
            raise OSError("the file accepted no bytes")
        remaining = remaining[count:]
