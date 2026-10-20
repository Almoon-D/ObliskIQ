"""The order domain: no plain market order can be expressed, terms bind by digest, and the
intent, clearance and audit models refuse incoherent values.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from order_rig import quote, terms
from pydantic import ValidationError

from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import (
    UNMANAGED,
    ClearanceAction,
    ExecutionMode,
    HoldingAssignment,
    IntentRole,
    LimitPrice,
    ModeChange,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    QuoteEvidence,
    RiskEffect,
    Sleeve,
    TimeInForce,
)

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def _intent(**overrides: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "intent_id": uuid.uuid7(),
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
    return OrderIntent(**fields)


def _clearance(**overrides: Any) -> PretradeClearance:
    fields: dict[str, Any] = {
        "clearance_id": uuid.uuid4(),
        "intent_id": uuid.uuid7(),
        "action": ClearanceAction.PLACE,
        "terms": terms(side=OrderSide.SELL),
        "effect": RiskEffect.REDUCES,
        "asset_class": AssetClass.STOCK,
        "quote": quote(at=NOW),
        "issued_at": NOW,
        "expires_at": NOW + timedelta(seconds=60),
    }
    fields.update(overrides)
    return PretradeClearance(**fields)


# ── no plain market order ──


@pytest.mark.parametrize(
    "price",
    [
        {"kind": "market"},
        {"kind": "limit"},
        {"kind": "stop_limit", "stop": "95"},
        {"kind": "algo", "strategy": "Adaptive"},
        {"kind": "stop", "stop": "95"},
        {"kind": "limit", "limit": "0"},
        {"kind": "limit", "limit": "-1"},
        {"kind": "limit", "limit": "NaN"},
        {"kind": "limit", "limit": "Infinity"},
    ],
)
def test_no_order_without_a_limit_can_be_expressed(price: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OrderTerms.model_validate(
            {
                "conid": 1,
                "side": "BUY",
                "quantity": "1",
                "price": price,
                "tif": "DAY",
                "venue": "SMART",
            }
        )


def test_terms_take_exactly_one_size() -> None:
    with pytest.raises(ValidationError):
        terms(quantity=None)
    with pytest.raises(ValidationError):
        OrderTerms(
            conid=1,
            side=OrderSide.BUY,
            quantity=Decimal(1),
            cash_quantity=Decimal(100),
            price=LimitPrice(limit=Decimal(1)),
            tif=TimeInForce.DAY,
            venue="SMART",
        )


def test_a_conversion_is_a_limit_sized_in_cash() -> None:
    assert terms(cash="100", conversion=True).conversion
    with pytest.raises(ValidationError):
        terms(conversion=True)
    with pytest.raises(ValidationError):
        terms(cash="100", conversion=True, stop="1", limit="1")


def test_the_digest_binds_the_terms_and_ignores_spelling() -> None:
    base = terms(limit="100.50")
    assert base.digest() == terms(limit="100.5").digest()
    for changed in (
        terms(limit="100.51"),
        terms(quantity="11"),
        terms(side=OrderSide.SELL),
        terms(venue="IBIS"),
        terms(cash="1000"),
        base.model_copy(update={"tif": TimeInForce.GTC}),
        terms(stop="99", limit="100.5"),
    ):
        assert changed.digest() != base.digest()
    assert terms(stop="95", limit="94").digest() != terms(stop="96", limit="94").digest()


# ── intents ──


def test_an_intent_id_is_a_uuid7() -> None:
    assert _intent().intent_id.version == 7
    with pytest.raises(ValidationError):
        _intent(intent_id=uuid.uuid4())


def test_an_intent_is_coherent() -> None:
    with pytest.raises(ValidationError):
        _intent(expires_at=NOW)
    with pytest.raises(ValidationError):
        _intent(role=IntentRole.CONVERSION)
    with pytest.raises(ValidationError):
        _intent(terms=terms(cash="1", conversion=True))
    intent_id = uuid.uuid7()
    with pytest.raises(ValidationError):
        _intent(intent_id=intent_id, parent_id=intent_id)
    with pytest.raises(ValidationError):
        _intent(intent_id=intent_id, protective_id=intent_id)
    with pytest.raises(ValidationError):
        _intent(created_at=datetime(2026, 10, 13, 14, 0))  # naive


def test_a_protective_role_may_carry_a_buy_the_gate_must_refuse() -> None:
    """Roles are labels: the domain lets a protective-role buy exist so the effect rules can
    be tested against it (DECISIONS D44)."""
    intent = _intent(role=IntentRole.PROTECTIVE, reason="stop")
    assert intent.side is OrderSide.BUY
    assert intent.conid == intent.terms.conid


# ── clearances ──


def test_only_a_sell_that_is_not_a_conversion_can_reduce_risk() -> None:
    assert _clearance().effect is RiskEffect.REDUCES
    with pytest.raises(ValidationError):
        _clearance(terms=terms(side=OrderSide.BUY))
    with pytest.raises(ValidationError):
        _clearance(terms=terms(side=OrderSide.SELL, cash="10", conversion=True))


def test_a_clearance_is_coherent() -> None:
    with pytest.raises(ValidationError):
        _clearance(action=ClearanceAction.MODIFY)  # names no order
    with pytest.raises(ValidationError):
        _clearance(order_id="1")  # a placement names none
    with pytest.raises(ValidationError):
        _clearance(expires_at=NOW)
    with pytest.raises(ValidationError):
        _clearance(quote=quote(at=NOW, conid=999))
    assert _clearance().terms_digest == terms(side=OrderSide.SELL).digest()


def test_a_quote_reference_price() -> None:
    assert quote(bid="99", ask="101", at=NOW).reference() == Decimal(100)
    crossed = QuoteEvidence(
        conid=1,
        status=MarketDataStatus.REALTIME,
        delay_seconds=0,
        observed_at=NOW,
        bid=Decimal(101),
        ask=Decimal(100),
        last=Decimal("100.5"),
    )
    assert crossed.reference() == Decimal("100.5")
    bare = QuoteEvidence(conid=1, status=None, delay_seconds=None, observed_at=NOW)
    assert bare.reference() is None


# ── modes and holdings ──


def test_a_mode_change_says_which_way_it_goes() -> None:
    up = ModeChange(
        at=NOW,
        from_mode=ExecutionMode.SHADOW,
        to_mode=ExecutionMode.PAPER,
        direction="upgrade",
        actor="owner",
        reason="r",
    )
    assert up.to_mode.rank > up.from_mode.rank
    for from_mode, to_mode, direction in (
        (ExecutionMode.SHADOW, ExecutionMode.PAPER, "downgrade"),
        (ExecutionMode.LIVE, ExecutionMode.PAPER, "upgrade"),
        (ExecutionMode.PAPER, ExecutionMode.PAPER, "downgrade"),
    ):
        with pytest.raises(ValidationError):
            ModeChange(
                at=NOW,
                from_mode=from_mode,
                to_mode=to_mode,
                direction=direction,  # type: ignore[arg-type]
                actor="owner",
                reason="r",
            )


def test_a_holding_belongs_to_a_sleeve_or_is_unmanaged() -> None:
    for owner in (Sleeve.CORE, UNMANAGED):
        HoldingAssignment(conid=1, owner=owner, assigned_by="owner", assigned_at=NOW, reason="r")
    with pytest.raises(ValidationError):
        HoldingAssignment(
            conid=1,
            owner="nobody",
            assigned_by="owner",
            assigned_at=NOW,
            reason="r",  # type: ignore[arg-type]
        )


def test_algo_parameters_are_part_of_the_digest() -> None:
    from obliskiq.domain.orders import AlgoLimitPrice, AlgoParameter

    def algo(priority: str) -> OrderTerms:
        return terms().model_copy(
            update={
                "price": AlgoLimitPrice(
                    strategy="Adaptive",
                    limit=Decimal(100),
                    parameters=(AlgoParameter(name="adaptivePriority", value=priority),),
                )
            }
        )

    assert algo("Patient").digest() == algo("Patient").digest()
    assert algo("Patient").digest() != algo("Urgent").digest()


def test_a_broker_amount_that_is_not_finite_is_refused() -> None:
    from obliskiq.domain.orders import BrokerOrder

    with pytest.raises(ValidationError):
        BrokerOrder(
            order_id="1",
            coid=None,
            conid=1,
            side=None,
            sec_type=None,
            order_type=None,
            status="Submitted",
            total_quantity=Decimal("NaN"),
            filled_quantity=None,
            remaining_quantity=None,
            average_price=None,
            tif=None,
            observed_at=NOW,
        )


def test_a_sell_is_sized_in_units_unless_it_is_a_conversion() -> None:
    with pytest.raises(ValidationError, match="sized in units"):
        terms(side=OrderSide.SELL, cash="1000")
    assert terms(side=OrderSide.SELL, cash="1000", conversion=True).conversion
    assert terms(cash="1000").cash_quantity == Decimal(1000)


def test_a_spec_belongs_to_an_entry_or_a_stop_and_a_run_names_what_freezes() -> None:
    from obliskiq.domain.orders import (
        ProtectiveSpec,
        ReconcileFinding,
        ReconcileRun,
        ReconcileStatus,
    )

    stop_spec = ProtectiveSpec(stop=Decimal(95), limit_offset_fraction=Decimal("0.02"), basis="t")
    assert stop_spec.lowest_limit(Decimal(95)) == Decimal("93.10")
    with pytest.raises(ValidationError, match="protective spec"):
        _intent(
            role=IntentRole.EXIT,
            terms=terms(side=OrderSide.SELL, quantity="1"),
            protective=stop_spec,
        )
    freezing = ReconcileFinding(kind="drift", detail="d", freezes=True)
    note = ReconcileFinding(kind="note", detail="n", freezes=False)
    with pytest.raises(ValidationError, match="not green"):
        ReconcileRun(at=NOW, status=ReconcileStatus.GREEN, findings=(freezing,))
    with pytest.raises(ValidationError, match="names the finding"):
        ReconcileRun(at=NOW, status=ReconcileStatus.DRIFT, findings=(note,))
    assert ReconcileRun(at=NOW, status=ReconcileStatus.INCOMPLETE, findings=(freezing,))
