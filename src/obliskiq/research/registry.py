"""The trial registry and the pre-registration lock (DECISIONS D63, AGENTS.md session workflow).

**The lock.** A pre-registration is merged in its own pull request; its lock file, written after
the merge, records the file's SHA-256 and that merge commit. A confirmation run refuses, before
reading any data, a pre-registration that has no lock, one whose text changed since its lock, a
lock whose commit is not in the history the run stands on, and a lock whose commit holds another
text under the same path. Git is asked directly; a check that cannot be made fails the run.

**The registry.** Every run of a study, exploratory or the confirmation, is appended to
``research_trials`` with the hash of everything it read; exploration stops at the trial budget the
pre-registration states, and every trial counts in the deflated Sharpe.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid7

from ..domain.research import Trial, TrialStage
from ..errors import PreregistrationError
from ..store import Store
from ..store.research import record_trial, trials_for
from .decide import Outcome

#: The lock of ``<name>.md`` is ``<name>.lock`` beside it.
LOCK_SUFFIX: Final = ".lock"
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PreregistrationError(f"{path} cannot be read: {exc}") from None


def canonical_sha256(value: Any) -> str:
    """The SHA-256 of ``value`` as canonical JSON (sorted keys, no spaces)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def lock_path(prereg: Path) -> Path:
    return prereg.with_name(f"{prereg.stem}{LOCK_SUFFIX}")


@dataclass(frozen=True, slots=True)
class PreregLock:
    file: str
    sha256: str
    merge_commit: str


class Git:
    """The few git questions the lock asks, about the repository holding a path."""

    def __init__(self, path: Path) -> None:
        self._directory = path if path.is_dir() else path.parent
        self.root = Path(self._text("rev-parse", "--show-toplevel"))

    def _run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", "-C", str(self._directory), *args],
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise PreregistrationError(f"git cannot be run: {exc}") from None

    def _text(self, *args: str) -> str:
        done = self._run(*args)
        if done.returncode != 0:
            message = done.stderr.decode(errors="replace").strip()
            raise PreregistrationError(f"git {' '.join(args)} failed: {message}")
        return done.stdout.decode().strip()

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def commit(self, reference: str) -> str:
        return self._text("rev-parse", "--verify", f"{reference}^{{commit}}")

    def is_ancestor_of_head(self, commit: str) -> bool:
        done = self._run("merge-base", "--is-ancestor", commit, "HEAD")
        return done.returncode == 0

    def show(self, commit: str, relative: str) -> bytes:
        done = self._run("show", f"{commit}:{relative}")
        if done.returncode != 0:
            raise PreregistrationError(f"commit {commit} holds no {relative}")
        return done.stdout


def _checked_commit(git: Git, prereg: Path, commit: str, sha256: str) -> str:
    resolved = git.commit(commit)
    if not git.is_ancestor_of_head(resolved):
        raise PreregistrationError(
            f"the lock's commit {resolved} is not in the history this run stands on"
        )
    merged = hashlib.sha256(git.show(resolved, git.relative(prereg))).hexdigest()
    if merged != sha256:
        raise PreregistrationError(
            f"{prereg.name} at commit {resolved} is not the text the lock records"
        )
    return resolved


def write_lock(prereg: Path, merge_commit: str) -> PreregLock:
    """Lock ``prereg`` at ``merge_commit``: the file there must be the file here."""
    sha256 = file_sha256(prereg)
    resolved = _checked_commit(Git(prereg), prereg, merge_commit, sha256)
    lock = PreregLock(prereg.name, sha256, resolved)
    payload = {"file": lock.file, "sha256": lock.sha256, "merge_commit": lock.merge_commit}
    lock_path(prereg).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return lock


def verify_lock(prereg: Path) -> PreregLock:
    """The lock of ``prereg`` if the run may go ahead; otherwise why not."""
    path = lock_path(prereg)
    if not path.is_file():
        raise PreregistrationError(
            f"{prereg.name} is not locked: merge it in its own pull request, then write "
            f"{path.name} with `obliskiq research lock`"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        lock = PreregLock(
            str(payload["file"]), str(payload["sha256"]), str(payload["merge_commit"])
        )
    except ValueError, KeyError, TypeError:
        raise PreregistrationError(f"{path.name} is not a lock file") from None
    if lock.file != prereg.name:
        raise PreregistrationError(f"{path.name} locks {lock.file}, not {prereg.name}")
    if not _COMMIT.fullmatch(lock.merge_commit):
        raise PreregistrationError(f"{path.name} names no commit: {lock.merge_commit!r}")
    if file_sha256(prereg) != lock.sha256:
        raise PreregistrationError(
            f"{prereg.name} changed since its lock: a pre-registration is never edited after "
            "its merge (a change is a new pre-registration)"
        )
    _checked_commit(Git(prereg), prereg, lock.merge_commit, lock.sha256)
    return lock


# ── the trial registry ──


def inputs_sha256(
    *, spec_sha256: str, series: Mapping[str, str], parameters: Mapping[str, Any]
) -> str:
    """The hash of everything a run read: the specification, each series' content hash and the
    run's parameters."""
    return canonical_sha256(
        {"spec": spec_sha256, "series": dict(sorted(series.items())), "parameters": parameters}
    )


class TrialRegistry:
    """Every trial of every study, in the research store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def trials(self, study_id: str) -> list[Trial]:
        return trials_for(self._store, study_id)

    def check_budget(self, study_id: str, budget: int) -> None:
        """Refuse an exploratory trial past the pre-registered budget."""
        used = sum(1 for t in self.trials(study_id) if t.stage is TrialStage.EXPLORATION)
        if used >= budget:
            raise PreregistrationError(
                f"{study_id} has used its trial budget ({used} of {budget} exploratory trials)"
            )

    def check_single_shot(self, study_id: str) -> None:
        """Refuse a second confirmation run once one has produced an outcome: only a run whose
        data were not evaluable may be repeated after the data are fixed (D63)."""
        evaluable = [
            trial
            for trial in self.trials(study_id)
            if trial.stage is TrialStage.CONFIRMATION
            and trial.outcome != Outcome.NOT_EVALUABLE.value
        ]
        if evaluable:
            raise PreregistrationError(
                f"{study_id} already has its confirmation run (outcome {evaluable[0].outcome}); "
                "a new test needs a new pre-registration"
            )

    def record(
        self,
        *,
        study_id: str,
        stage: TrialStage,
        at: datetime,
        spec_sha256: str,
        inputs: str,
        parameters: Mapping[str, Any],
        metric: str,
        estimate: float | None,
        sharpe: float | None,
        observations: int | None,
        outcome: str | None,
    ) -> Trial:
        trial = Trial(
            trial_id=str(uuid7()),
            study_id=study_id,
            stage=stage,
            recorded_at=at,
            spec_sha256=spec_sha256,
            inputs_sha256=inputs,
            parameters=json.dumps(dict(parameters), sort_keys=True, default=str),
            metric=metric,
            estimate=estimate,
            sharpe=sharpe,
            observations=observations,
            outcome=outcome,
        )
        record_trial(self._store, trial)
        return trial


def sharpes(trials: Sequence[Trial]) -> list[float]:
    """The Sharpe ratios the registry holds for a study's trials."""
    return [t.sharpe for t in trials if t.sharpe is not None]
