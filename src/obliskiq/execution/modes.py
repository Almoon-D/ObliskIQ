"""Execution modes: shadow, paper, live-small, live (MASTER_PLAN §5.12, §5.19).

* ``shadow`` sends nothing to the broker: fills are simulated with the cost model
  (``execution/shadow.py``). ``paper`` writes to the IBKR paper account, under profile ``paper``
  only. ``live-small`` and ``live`` write to the live account, under profile ``live`` only.
* **Upgrades** are explicit owner commands and are audited. ``shadow → paper`` is allowed; its
  operational entry criteria are the promotion gate's (Session 18) and are recorded as untested,
  never as passed. The live levels need the promotion gate, which does not exist yet, so every
  upgrade to them is refused (``promotion_gate_unavailable``).
* **Downgrades** are automatic and always allowed; they are audited too.
* The current mode is the latest audited change, else ``execution.modes.initial``. It is read
  from the store before every write, so a downgrade by another process takes effect at the next
  write; a store that cannot be read refuses the write.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from ..config.schema import ObliskConfig
from ..domain.orders import ExecutionMode, ModeChange
from ..errors import ModeChangeRefusedError
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

type Profile = Literal["paper", "live"]

#: The profile each writing mode requires. The live levels write nothing until the promotion
#: gate exists (Session 18), so none is listed.
_PROFILE_FOR: dict[ExecutionMode, Profile] = {ExecutionMode.PAPER: "paper"}

#: Upgrades this build can make; the live levels wait for the promotion gate (Session 18).
_UPGRADABLE = frozenset({ExecutionMode.PAPER})

PROMOTION_UNTESTED = "promotion criteria untested (the promotion gate is Session 18's)"


def write_refusal(mode: ExecutionMode, profile: Profile | None) -> str | None:
    """Why ``mode`` under ``profile`` may not place or modify an order; ``None`` if it may."""
    if mode is ExecutionMode.SHADOW:
        return "shadow mode sends no order to the broker"
    if mode not in _UPGRADABLE:
        # Whatever the audit says: no live write until the promotion gate exists (Session 18).
        return f"promotion_gate_unavailable: mode {mode.value} needs the promotion gate"
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
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._worker = worker
        self._initial = settings.execution.modes.initial
        self._profile = profile
        self._now = now

    @property
    def profile(self) -> Profile | None:
        return self._profile

    async def current(self) -> ExecutionMode:
        change = await self._worker.run(repo.latest_mode_change)
        return self._initial if change is None else change.to_mode

    async def upgrade(self, to: ExecutionMode, *, actor: str, reason: str) -> ModeChange:
        """An owner's explicit upgrade; refused unless this build may make it."""
        current = await self.current()
        if to.rank <= current.rank:
            raise ModeChangeRefusedError(f"{to.value} is not above the current {current.value}")
        if to not in _UPGRADABLE:
            raise ModeChangeRefusedError(
                f"promotion_gate_unavailable: {to.value} needs the promotion gate and an owner "
                "command; the gate is not built yet"
            )
        refusal = write_refusal(to, self._profile)
        if refusal is not None:
            raise ModeChangeRefusedError(refusal)
        change = ModeChange(
            at=self._now(),
            from_mode=current,
            to_mode=to,
            direction="upgrade",
            actor=actor,
            reason=f"{reason}; {PROMOTION_UNTESTED}",
        )
        await self._worker.run(repo.record_mode_change, change)
        return change

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
