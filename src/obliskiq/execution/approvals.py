"""The approval queue (MASTER_PLAN §2: it exists, off by default).

A sleeve listed in ``execution.approval.sleeves`` holds every new intent that adds risk for the
owner's word, as does an intent made with ``approval_required``. Held intents wait as
``proposed``; the owner approves one (it moves to ``approved``) or rejects it (``rejected``), by
``obliskiq approvals``, and the decision is recorded in the append-only ``approvals`` table. The
pre-trade gate refuses an intent that needs approval and has none (``approval_missing``); an
order that reduces risk waives it (I3).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from ..config.schema import ObliskConfig
from ..domain.orders import ApprovalDecision, IntentState, Sleeve
from ..errors import ExecutionError
from ..store import orders as repo
from ..store.orders import StoredIntent
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .state import IntentBook


class ApprovalQueue:
    """Intents held for the owner, and the owner's decisions on them."""

    def __init__(
        self,
        settings: ObliskConfig,
        worker: StoreWorker,
        book: IntentBook,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._held = frozenset(settings.execution.approval.sleeves)
        self._worker = worker
        self._book = book
        self._now = now

    def required_for(self, sleeve: Sleeve) -> bool:
        """Whether the configuration holds this sleeve's intents for approval."""
        return sleeve in self._held

    async def pending(self) -> list[StoredIntent]:
        """Proposed intents that need the owner's word and have none, oldest first."""
        held = await self._worker.run(repo.intents_in_states, [IntentState.PROPOSED])
        waiting = []
        for stored in held:
            intent = stored.intent
            if not (intent.approval_required or self.required_for(intent.sleeve)):
                continue
            if await self._worker.run(repo.approval_for, intent.intent_id) is None:
                waiting.append(stored)
        return waiting

    async def _decide(
        self, intent_id: UUID, *, approved: bool, actor: str, reason: str
    ) -> ApprovalDecision:
        stored = await self._book.get(intent_id)
        if stored.intent.state is not IntentState.PROPOSED:
            raise ExecutionError(
                f"intent {intent_id} is {stored.intent.state.value}; only a proposed intent "
                "waits for a decision"
            )
        decision = ApprovalDecision(
            intent_id=intent_id, approved=approved, actor=actor, reason=reason, at=self._now()
        )
        await self._worker.run(repo.record_approval, decision)
        target = IntentState.APPROVED if approved else IntentState.REJECTED
        verb = "approved" if approved else "rejected"
        await self._book.advance(intent_id, target, f"{verb} by {actor}: {reason}")
        return decision

    async def approve(self, intent_id: UUID, *, actor: str, reason: str) -> ApprovalDecision:
        return await self._decide(intent_id, approved=True, actor=actor, reason=reason)

    async def reject(self, intent_id: UUID, *, actor: str, reason: str) -> ApprovalDecision:
        return await self._decide(intent_id, approved=False, actor=actor, reason=reason)
