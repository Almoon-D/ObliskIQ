"""A call written from an analyst's draft carries the kinds of the event lines it cites into the
idea ledger (DECISIONS D189, D200).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from datetime import UTC, datetime
from typing import Any

from thesis_rig import thesis

from obliskiq.config import ObliskConfig
from obliskiq.decision.rebalance_thesis import write_call
from obliskiq.domain.analyst import (
    UNSTORED_CALL,
    AnalystCall,
    CallKind,
    DraftOutcome,
    DraftRecord,
)
from obliskiq.domain.sources import ExtractionStatus
from obliskiq.domain.thesis import ThesisSource
from obliskiq.store import Store
from obliskiq.store import analyst as analyst_repo
from obliskiq.store import ledger as ledger_repo
from obliskiq.store import theses as thesis_repo

AT = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
FACTS = [
    "F1 | 2026-11-20 | ok | instrument | EMIM: close 30.1",
    "F2 | 2026-11-20 | ok | events | results: BJ (BJ's Wholesale Club Holdings, Inc.) 8-K filed "
    "2026-11-20, item 2.02",
    "F3 | 2026-11-20 | ok | events | departure: A (AGILENT TECHNOLOGIES, INC.) 8-K filed "
    "2026-11-20, item 5.02",
    "F4 | 2026-11-20 | ok | events | results: BJ again",
]


def drafted_thesis(store: Store, facts: list[str]) -> Any:
    made = thesis_repo.insert_thesis(store, thesis(source=ThesisSource.ANALYST), actor="analyst")
    call = analyst_repo.record_call(
        store,
        AnalystCall(
            call_id=UNSTORED_CALL,
            kind=CallKind.DRAFTS,
            thesis_id=None,
            as_of=AT.date(),
            sheet_hash="a" * 64,
            produced_at=AT,
            provider="gemini",
            model_id="m",
            prompt_version=1,
            prompt_hash="b" * 64,
            status=ExtractionStatus.OK,
            attempts=1,
            output="{}",
            error=None,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )
    analyst_repo.record_drafts(
        store,
        [
            DraftRecord(
                call_id=call.call_id,
                seq=1,
                draft="{}",
                facts=json.dumps(facts),
                outcome=DraftOutcome.ACCEPTED,
                reason=None,
                thesis_id=made.thesis_id,
            )
        ],
    )
    return made


def test_a_call_from_an_event_line_carries_its_kind_into_the_ledger(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    kinds = shipped_settings.sources.filings.kinds()
    made = drafted_thesis(store, FACTS)
    written = write_call(store, made, at=AT, kinds=kinds)
    assert written is not None
    held = ledger_repo.calls(store, as_of=AT.date())
    assert [call.event_tags for call in held] == [("results", "departure")]
    # Written once: a second cycle writes nothing.
    assert write_call(store, made, at=AT, kinds=kinds) is None


def test_a_call_citing_no_event_and_the_owner_s_idea_carry_no_tag(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    kinds = shipped_settings.sources.filings.kinds()
    plain = drafted_thesis(store, FACTS[:1])
    owner = thesis_repo.insert_thesis(store, thesis(), actor="owner")
    assert write_call(store, plain, at=AT, kinds=kinds) is not None
    assert write_call(store, owner, at=AT, kinds=kinds) is not None
    assert [call.event_tags for call in ledger_repo.calls(store, as_of=AT.date())] == [(), ()]
