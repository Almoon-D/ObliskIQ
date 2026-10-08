"""The settings-change audit log.

Every change to an effective setting — from an edit of ``config/owner.yaml`` applied at a
cycle boundary, ``obliskiq settings set``, Telegram or the cockpit — is appended here as one
JSON line: who, through which channel, when, which key, the old and new values and why.

The file is append-only: it is opened with ``O_APPEND`` and never rewritten. Each entry
carries a sequence number and the SHA-256 of the previous entry, so an edited, reordered or
removed entry breaks the chain and reading the log fails loudly. (A chain cannot reveal
entries cut from the end; the log's backups cover that.)

SPDX-License-Identifier: AGPL-3.0-or-later
"""

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
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise SettingsAuditError(f"{self._path} cannot be read: {exc}") from exc
        entries: list[AuditEntry] = []
        previous = GENESIS_HASH
        for number, line in enumerate(lines, start=1):
            where = f"{self._path} line {number}"
            try:
                entry = AuditEntry.model_validate_json(line)
            except ValidationError as exc:
                raise SettingsAuditError(f"{where} is not a valid audit entry") from exc
            if entry.seq != number:
                raise SettingsAuditError(f"{where} has sequence {entry.seq}, expected {number}")
            if entry.prev_hash != previous:
                raise SettingsAuditError(f"{where} does not follow the entry before it")
            fields = entry.model_dump(mode="json", exclude={"hash"})
            if _digest(fields) != entry.hash:
                raise SettingsAuditError(f"{where} was altered after it was written")
            entries.append(entry)
            previous = entry.hash
        return tuple(entries)

    def record(
        self,
        changes: Iterable[SettingChange],
        *,
        actor: str,
        channel: str,
        reason: str | None,
        at: datetime,
    ) -> tuple[AuditEntry, ...]:
        """Append one entry per change and flush them to disk before returning them."""
        if at.tzinfo is None:
            raise SettingsAuditError("an audit timestamp must be timezone-aware")
        existing = self.entries()
        previous = existing[-1].hash if existing else GENESIS_HASH
        moment = at.astimezone(UTC)
        written: list[AuditEntry] = []
        for offset, change in enumerate(changes, start=1):
            try:
                draft = AuditEntry(
                    seq=len(existing) + offset,
                    at=moment,
                    actor=actor,
                    channel=channel,
                    reason=reason,
                    key=change.key,
                    old=change.old,
                    new=change.new,
                    prev_hash=previous,
                    hash="",
                )
            except ValidationError as exc:
                raise SettingsAuditError(f"invalid audit entry for {change.key}: {exc}") from exc
            digest = _digest(draft.model_dump(mode="json", exclude={"hash"}))
            entry = draft.model_copy(update={"hash": digest})
            written.append(entry)
            previous = entry.hash
        if not written:
            return ()
        payload = "".join(entry.model_dump_json() + "\n" for entry in written)
        self._append(payload.encode("utf-8"))
        return tuple(written)

    def _append(self, payload: bytes) -> None:
        try:
            self._path.parent.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
            descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, LOG_FILE_MODE)
        except OSError as exc:
            raise SettingsAuditError(f"{self._path} cannot be opened for appending: {exc}") from exc
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except OSError as exc:
            raise SettingsAuditError(f"{self._path} could not be written: {exc}") from exc
        finally:
            os.close(descriptor)
