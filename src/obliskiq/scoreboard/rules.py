"""The scoreboard's rules: ``research/preregistrations/scoreboard.md``, read as its lock records it.

Every number the scoreboard decides with (rho, the horizon, θ, the minimums, the boundaries, the
budget steps) is the pre-registration's, so none lives in configuration or code. The running
system reads the file only when it is the text the lock records (its SHA-256): a missing lock or
a changed file leaves the scoreboard without rules, and nothing it would decide is decided. The
lock's commit is checked against git by the research harness (``obliskiq research lock``); a
deployment holds no history to ask.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..errors import PreregistrationError
from ..research.prereg import (
    AnalystReplayPrereg,
    ForwardBookPrereg,
    IdeaLedgerPrereg,
    ScoreboardPrereg,
    ScoreboardSpec,
    load_any,
)
from ..research.registry import file_sha256, lock_path

#: The scoreboard's pre-registration, relative to the repository root.
RULES_PATH: Final = Path("research") / "preregistrations" / "scoreboard.md"
#: The thesis book's pre-registration (H13), relative to the repository root.
H13_PATH: Final = Path("research") / "preregistrations" / "thesis-book.md"
#: The idea ledger's pre-registration (H14), relative to the repository root.
LEDGER_PATH: Final = Path("research") / "preregistrations" / "idea-ledger.md"
#: The analyst's replay (H15), relative to the repository root.
REPLAY_PATH: Final = Path("research") / "preregistrations" / "analyst-replay.md"
DAYS_PER_WEEK: Final = 7


def _check_lock(path: Path) -> None:
    lock = lock_path(path)
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
        locked = str(payload["sha256"])
    except OSError, ValueError, KeyError, TypeError:
        raise PreregistrationError(
            f"{path.name} has no readable lock ({lock.name}): the scoreboard decides nothing "
            "on rules that are not locked"
        ) from None
    if file_sha256(path) != locked:
        raise PreregistrationError(f"{path.name} is not the text its lock records")


def load_locked(path: Path) -> ScoreboardPrereg:
    """The scoreboard's pre-registration at ``path`` when its lock records exactly this text."""
    _check_lock(path)
    prereg = load_any(path)
    if not isinstance(prereg, ScoreboardPrereg):
        raise PreregistrationError(f"{path.name} is not the scoreboard's rules")
    return prereg


def load_locked_book(path: Path) -> ForwardBookPrereg:
    """H13's pre-registration at ``path`` when its lock records exactly this text."""
    _check_lock(path)
    prereg = load_any(path)
    if not isinstance(prereg, ForwardBookPrereg):
        raise PreregistrationError(f"{path.name} is not a forward book's pre-registration")
    return prereg


def load_locked_ledger(path: Path) -> IdeaLedgerPrereg:
    """H14's pre-registration at ``path`` when its lock records exactly this text."""
    _check_lock(path)
    prereg = load_any(path)
    if not isinstance(prereg, IdeaLedgerPrereg):
        raise PreregistrationError(f"{path.name} is not the idea ledger's pre-registration")
    return prereg


def load_locked_replay(path: Path) -> AnalystReplayPrereg:
    """H15's pre-registration at ``path`` when its lock records exactly this text."""
    _check_lock(path)
    prereg = load_any(path)
    if not isinstance(prereg, AnalystReplayPrereg):
        raise PreregistrationError(f"{path.name} is not the analyst replay's pre-registration")
    return prereg


@dataclass(frozen=True, slots=True)
class GradingRules:
    """Forward grading under D62, as the rules fix it (``theta`` per horizon)."""

    rho: float
    horizon_days: int
    alpha: float
    theta: float
    min_cohorts: int
    min_effective: float
    max_mark_age_days: int
    target_power: float

    @property
    def horizon_weeks(self) -> int:
        return max(1, math.ceil(self.horizon_days / DAYS_PER_WEEK))

    @classmethod
    def of(cls, spec: ScoreboardSpec, *, target_power: float) -> GradingRules:
        grading = spec.grading
        return cls(
            rho=float(spec.dependence.rho),
            horizon_days=grading.horizon_days,
            alpha=float(grading.alpha),
            theta=float(grading.theta_per_horizon),
            min_cohorts=grading.min_cohorts,
            min_effective=float(grading.min_effective),
            max_mark_age_days=grading.max_mark_age_days,
            target_power=target_power,
        )

    @classmethod
    def panel(cls, spec: ScoreboardSpec, *, target_power: float) -> GradingRules:
        """The panel's: its own horizon, θ (an IC) and minimums."""
        panel = spec.panel
        return cls(
            rho=float(spec.dependence.rho),
            horizon_days=panel.horizon_days,
            alpha=float(spec.grading.alpha),
            theta=float(panel.theta),
            min_cohorts=panel.min_cohorts,
            min_effective=float(panel.min_effective),
            max_mark_age_days=spec.grading.max_mark_age_days,
            target_power=target_power,
        )
