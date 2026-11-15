"""Execution modes: shadow, paper, live-small, live (MASTER_PLAN §5.12, §5.19).

* ``shadow`` sends nothing to the broker: fills are simulated with the cost model
  (``execution/shadow.py``). ``paper`` writes to the IBKR paper account, under profile ``paper``
  only. ``live-small`` and ``live`` write to the live account, under profile ``live`` only.
* **Upgrades** are explicit owner commands and are audited, with the promotion gate's verdict on
  the level (``scoreboard/promotion.py``, MASTER_PLAN §5.19) in the audit row. ``shadow → paper``
  is allowed whatever the verdict says (paper risks no money, and the owner's order test needs
  it); a verdict not handed in is recorded as untested, never as passed. A live level needs the
  gate's passed verdict for that very level, handed in by the owner's command, and profile
  ``live``; otherwise it is refused (``promotion_gate_not_passed``).
* **Downgrades** are automatic and always allowed; they are audited too.
* The current mode is the latest audited change, else ``execution.modes.initial``. It is read
  from the store before every write, so a downgrade by another process takes effect at the next
  write; a store that cannot be read refuses the write.
* **Standing down** (:meth:`Modes.stand_down`: the hard breaker, the failed cycles) is a downgrade
  to shadow that works when the store does not: where the audit row cannot be written, the file
  ``FORCED_SHADOW`` in the data directory holds the mode at shadow, read before the store and
  counted as set when it cannot be checked. The owner's next upgrade writes the missed audit row
  and removes the file. Its name is fixed in code, as the kill switch's is.
* An upgrade is refused while the broker book's hard breaker is latched (``store/breakers.py``):
  the owner resets it first (``obliskiq breakers reset``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from ..config.schema import ObliskConfig
from ..domain.orders import ExecutionMode, ModeChange
from ..errors import ExecutionError, ModeChangeRefusedError
from ..logging_setup import get_logger
from ..store import breakers as breaker_repo
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now, utc_stamp

log = get_logger("execution.modes")

type Profile = Literal["paper", "live"]

#: The profile each writing mode requires.
_PROFILE_FOR: dict[ExecutionMode, Profile] = {
    ExecutionMode.PAPER: "paper",
    ExecutionMode.LIVE_SMALL: "live",
    ExecutionMode.LIVE: "live",
}

#: The levels an upgrade to needs the promotion gate's passed verdict.
_GATED = frozenset({ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE})

PROMOTION_UNTESTED = "promotion criteria untested (no verdict of the promotion gate was given)"


@dataclass(frozen=True, slots=True)
class PromotionCheck:
    """The promotion gate's verdict on an upgrade to ``target``, as the owner's command hands it
    in (the gate reads the scoreboard, which the order path never imports)."""

    target: ExecutionMode
    passed: bool
    summary: str


#: The file that holds the mode at shadow when the store could not record a stand-down. Fixed,
#: so it is read without configuration.
FORCED_SHADOW_FILE: Final = "FORCED_SHADOW"


def forced_shadow_path(data_dir: Path) -> Path:
    return data_dir / FORCED_SHADOW_FILE


def forced_shadow_on(data_dir: Path) -> bool:
    """Whether the file holds the mode at shadow; a file that cannot be checked counts as set
    (the entry itself is ``lstat``-ed, as the kill switch's is)."""
    try:
        forced_shadow_path(data_dir).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _set_forced_shadow(data_dir: Path, *, actor: str, reason: str, at: datetime) -> None:
    path = forced_shadow_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{FORCED_SHADOW_FILE}.tmp")
    staging.write_text(
        json.dumps({"at": utc_stamp(at), "actor": actor, "reason": reason}) + "\n",
        encoding="utf-8",
    )
    staging.replace(path)


def _forced_shadow_note(data_dir: Path) -> str:
    """What the file says of the stand-down it holds, for the audit row written late."""
    try:
        note = json.loads(forced_shadow_path(data_dir).read_text(encoding="utf-8"))
        return f"{note['actor']} at {note['at']}: {note['reason']}"
    except OSError, ValueError, KeyError, TypeError:
        return "the file cannot be read"


@dataclass(frozen=True, slots=True)
class StandDown:
    """A stand-down to shadow: the audited change (``None``: the mode was already shadow, or the
    store could not record it) and whether the file holds it."""

    change: ModeChange | None
    by_file: bool


def write_refusal(mode: ExecutionMode, profile: Profile | None) -> str | None:
    """Why ``mode`` under ``profile`` may not place or modify an order; ``None`` if it may."""
    if mode is ExecutionMode.SHADOW:
        return "shadow mode sends no order to the broker"
    required = _PROFILE_FOR[mode]
    if profile != required:
        return f"mode {mode.value} writes only under profile {required} (profile is {profile})"
    return None


class Modes:
    """The current mode, its audited changes, and what it permits."""

    def __init__(
        self,
        worker: StoreWorker,
        settings: ObliskConfig,
        *,
        profile: Profile | None,
        data_dir: Path | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._worker = worker
        self._initial = settings.execution.modes.initial
        self._profile = profile
        self._data_dir = data_dir
        self._now = now

    @property
    def profile(self) -> Profile | None:
        return self._profile

    async def _forced(self) -> bool:
        if self._data_dir is None:
            return False
        return await asyncio.to_thread(forced_shadow_on, self._data_dir)

    async def _recorded(self) -> ExecutionMode:
        change = await self._worker.run(repo.latest_mode_change)
        return self._initial if change is None else change.to_mode

    async def current(self) -> ExecutionMode:
        """Shadow while the file holds it there, whatever the store says or fails to say; else
        the latest audited change."""
        if await self._forced():
            return ExecutionMode.SHADOW
        return await self._recorded()

    async def upgrade(
        self,
        to: ExecutionMode,
        *,
        actor: str,
        reason: str,
        promotion: PromotionCheck | None = None,
    ) -> ModeChange:
        """An owner's explicit upgrade, with the promotion gate's verdict on ``to``; refused when
        the rules do not allow it."""
        current = await self.current()
        if to.rank <= current.rank:
            raise ModeChangeRefusedError(f"{to.value} is not above the current {current.value}")
        if promotion is not None and promotion.target is not to:
            raise ModeChangeRefusedError(
                f"the promotion verdict is on {promotion.target.value}, not {to.value}"
            )
        if to in _GATED and to.rank != current.rank + 1:
            raise ModeChangeRefusedError(
                f"promotion_gate_not_passed: {to.value} is not the level above {current.value}; "
                "the ladder is climbed one level at a time"
            )
        if to in _GATED and (promotion is None or not promotion.passed):
            raise ModeChangeRefusedError(
                f"promotion_gate_not_passed: {to.value} needs the promotion gate's passed "
                "verdict and an owner command"
                + ("" if promotion is None else f" ({promotion.summary})")
            )
        refusal = write_refusal(to, self._profile)
        if refusal is not None:
            raise ModeChangeRefusedError(refusal)
        latched = await self._worker.run(breaker_repo.tripped, "broker")
        if latched is not None:
            raise ModeChangeRefusedError(
                f"the hard breaker tripped at {latched.at.isoformat()} ({latched.detail}); "
                "reset it first with `obliskiq breakers reset`"
            )
        await self._settle_forced(actor)
        change = ModeChange(
            at=self._now(),
            from_mode=current,
            to_mode=to,
            direction="upgrade",
            actor=actor,
            reason=f"{reason}; "
            + (PROMOTION_UNTESTED if promotion is None else f"promotion gate: {promotion.summary}"),
        )
        await self._worker.run(repo.record_mode_change, change)
        return change

    async def _settle_forced(self, actor: str) -> None:
        """Before an upgrade: the stand-down the file holds gets the audit row the store could
        not take then, and the file is removed."""
        data_dir = self._data_dir
        if data_dir is None or not await self._forced():
            return
        recorded = await self._recorded()
        if recorded.rank > ExecutionMode.SHADOW.rank:
            note = await asyncio.to_thread(_forced_shadow_note, data_dir)
            await self._worker.run(
                repo.record_mode_change,
                ModeChange(
                    at=self._now(),
                    from_mode=recorded,
                    to_mode=ExecutionMode.SHADOW,
                    direction="downgrade",
                    actor=actor,
                    reason=f"recorded late, the store could not take it then ({note})",
                ),
            )
        await asyncio.to_thread(forced_shadow_path(data_dir).unlink, missing_ok=True)
        if await self._forced():
            raise ModeChangeRefusedError(f"the file {FORCED_SHADOW_FILE} cannot be removed")

    async def downgrade(self, to: ExecutionMode, *, actor: str, reason: str) -> ModeChange | None:
        """Lower the mode; ``None`` when it is already at or below ``to``."""
        current = await self.current()
        if to.rank >= current.rank:
            return None
        change = ModeChange(
            at=self._now(),
            from_mode=current,
            to_mode=to,
            direction="downgrade",
            actor=actor,
            reason=reason,
        )
        await self._worker.run(repo.record_mode_change, change)
        return change

    async def stand_down(self, *, actor: str, reason: str) -> StandDown:
        """Shadow, whatever is failing: the audited downgrade, and where the store cannot record
        it the file that holds the mode at shadow. Raises only when neither can be written."""
        try:
            return StandDown(
                await self.downgrade(ExecutionMode.SHADOW, actor=actor, reason=reason), False
            )
        except Exception as exc:
            log.warning(
                "the stand-down cannot be recorded in the store; the file holds it",
                extra={"error": type(exc).__name__},
            )
            if self._data_dir is None:
                raise
            try:
                await asyncio.to_thread(
                    _set_forced_shadow, self._data_dir, actor=actor, reason=reason, at=self._now()
                )
            except OSError as failed:
                raise ExecutionError(
                    f"the stand-down to shadow could not be recorded: the store "
                    f"({type(exc).__name__}) and the file ({failed}) both failed"
                ) from exc
            return StandDown(None, True)
