"""New intents: a uuid7 and lite's discipline (limit-only entries, the allowed exit reasons).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from order_rig import terms

from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import (
    AlgoLimitPrice,
    ExecutionMode,
    IntentRole,
    OrderSide,
    Sleeve,
)
from obliskiq.execution.intents import IntentRefusedError, new_intent

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def _new(settings: ObliskConfig, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "sleeve": Sleeve.CORE,
        "role": IntentRole.ENTRY,
        "terms": terms(),
        "reason": "entry",
        "mode": ExecutionMode.PAPER,
        "approval_required": False,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    fields.update(overrides)
    return new_intent(settings, **fields)


def test_each_intent_gets_a_fresh_uuid7(shipped_settings: ObliskConfig) -> None:
    first, second = _new(shipped_settings), _new(shipped_settings)
    assert first.intent_id.version == 7
    assert first.intent_id != second.intent_id
    assert first.created_at == NOW


def test_entries_are_limit_orders(shipped_settings: ObliskConfig) -> None:
    algo = terms().model_copy(
        update={"price": AlgoLimitPrice(strategy="Adaptive", limit=terms().price.limit)}
    )
    assert _new(shipped_settings, terms=algo).terms.price.kind == "algo"
    with pytest.raises(IntentRefusedError, match="entry_order_type"):
        _new(shipped_settings, terms=terms(stop="99", limit="100"))


def test_exits_and_stops_give_an_allowed_reason(shipped_settings: ObliskConfig) -> None:
    sell = terms(side=OrderSide.SELL)
    for reason in shipped_settings.discipline.exit_reasons_allowed:
        assert _new(shipped_settings, role=IntentRole.EXIT, terms=sell, reason=reason)
    for role in (IntentRole.EXIT, IntentRole.PROTECTIVE):
        with pytest.raises(IntentRefusedError, match="exit reason"):
            _new(shipped_settings, role=role, terms=sell, reason="the price moved")
    stop = terms(side=OrderSide.SELL, stop="95", limit="94")
    assert _new(shipped_settings, role=IntentRole.PROTECTIVE, terms=stop, reason="stop")
