"""The reply policy: every branch of every message id it can meet.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from order_rig import quote, terms

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_execution import ConfirmedReply, ReplyPolicyConfig
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import (
    ClearanceAction,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    QuoteEvidence,
    RiskEffect,
)
from obliskiq.execution.replies import ReplyPolicy
from obliskiq.ibkr.order_decode import Question

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
DELAYED = quote(status=MarketDataStatus.DELAYED, delay=1200, at=NOW)


def _clearance(
    order_terms: OrderTerms | None = None,
    *,
    evidence: QuoteEvidence | None = DELAYED,
    asset_class: AssetClass = AssetClass.STOCK,
    sigma: float | None = 0.3,
    effect: RiskEffect = RiskEffect.ADDS,
) -> PretradeClearance:
    return PretradeClearance(
        clearance_id=uuid.uuid4(),
        intent_id=uuid.uuid7(),
        action=ClearanceAction.PLACE,
        terms=order_terms if order_terms is not None else terms(),
        effect=effect,
        asset_class=asset_class,
        quote=evidence,
        sigma=sigma,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )


def _ask(*ids: str) -> Question:
    return Question(reply_id="r1", message_ids=ids, messages=("text",))


@pytest.fixture
def policy(shipped_settings: ObliskConfig) -> ReplyPolicy:
    return ReplyPolicy(shipped_settings)


def test_a_warning_without_an_id_is_declined(policy: ReplyPolicy) -> None:
    verdict = policy.decide(_ask(), _clearance(), now=NOW)
    assert not verdict.confirm


def test_an_id_outside_the_policy_is_declined(policy: ReplyPolicy) -> None:
    for message_id in ("o163", "o382", "o383", "o403", "o451", "o10151", "o10152", "p12", "o0"):
        assert not policy.decide(_ask(message_id), _clearance(), now=NOW).confirm


def test_a_whitelisted_id_for_any_order_is_confirmed(policy: ReplyPolicy) -> None:
    assert policy.decide(_ask("o10153"), _clearance(), now=NOW).confirm


def test_the_stop_order_notice_only_for_a_stop_limit(policy: ReplyPolicy) -> None:
    stop = terms(side=OrderSide.SELL, stop="95", limit="94")
    assert policy.decide(_ask("o10331"), _clearance(stop), now=NOW).confirm
    assert not policy.decide(_ask("o10331"), _clearance(), now=NOW).confirm


def test_the_cash_quantity_notices_only_for_an_order_sized_in_money(policy: ReplyPolicy) -> None:
    cash = terms(cash="2000")
    conversion = terms(cash="2000", conversion=True)
    for message_id in ("o10164", "o10223"):
        assert policy.decide(_ask(message_id), _clearance(cash), now=NOW).confirm
        assert not policy.decide(_ask(message_id), _clearance(), now=NOW).confirm
        assert not policy.decide(_ask(message_id), _clearance(conversion), now=NOW).confirm


def test_a_conversion_condition(shipped_settings: ObliskConfig) -> None:
    execution = shipped_settings.execution.model_copy(
        update={
            "reply_policy": ReplyPolicyConfig(
                confirm=[ConfirmedReply(message_id="o2137", when="conversion")],
                no_market_data_message_id="o354",
            )
        }
    )
    policy = ReplyPolicy(shipped_settings.model_copy(update={"execution": execution}))
    conversion = terms(cash="2000", conversion=True)
    assert policy.decide(_ask("o2137"), _clearance(conversion), now=NOW).confirm
    assert not policy.decide(_ask("o2137"), _clearance(), now=NOW).confirm


def test_every_id_of_a_warning_must_be_allowed(policy: ReplyPolicy) -> None:
    assert policy.decide(_ask("o10153", "o354"), _clearance(), now=NOW).confirm
    assert not policy.decide(_ask("o10153", "o999"), _clearance(), now=NOW).confirm


# ── o354, no market data ──


def test_no_market_data_with_a_delayed_policy_a_fresh_quote_and_inside_the_collar(
    policy: ReplyPolicy,
) -> None:
    verdict = policy.decide(_ask("o354"), _clearance(), now=NOW + timedelta(seconds=30))
    assert verdict.confirm, verdict.reason


@pytest.mark.parametrize("asset_class", [AssetClass.OPTION, AssetClass.FUND, AssetClass.INDEX])
def test_no_market_data_where_the_policy_refuses_delayed_quotes(
    policy: ReplyPolicy, asset_class: AssetClass
) -> None:
    verdict = policy.decide(_ask("o354"), _clearance(asset_class=asset_class), now=NOW)
    assert not verdict.confirm
    assert "refuses delayed" in verdict.reason


def test_no_market_data_without_a_quote(policy: ReplyPolicy) -> None:
    assert not policy.decide(_ask("o354"), _clearance(evidence=None), now=NOW).confirm


def test_no_market_data_with_a_stale_quote(policy: ReplyPolicy) -> None:
    max_age = 60
    verdict = policy.decide(_ask("o354"), _clearance(), now=NOW + timedelta(seconds=max_age + 1))
    assert not verdict.confirm
    assert "stale" in verdict.reason


def test_no_market_data_with_a_quote_stamped_in_the_future(policy: ReplyPolicy) -> None:
    verdict = policy.decide(_ask("o354"), _clearance(), now=NOW - timedelta(seconds=5))
    assert not verdict.confirm


def test_no_market_data_with_a_delay_beyond_the_maximum(policy: ReplyPolicy) -> None:
    late = quote(status=MarketDataStatus.DELAYED, delay=1201, at=NOW)
    verdict = policy.decide(_ask("o354"), _clearance(evidence=late), now=NOW)
    assert not verdict.confirm
    assert "beyond" in verdict.reason


def test_no_market_data_with_a_delay_that_is_not_known(policy: ReplyPolicy) -> None:
    unknown = quote(status=MarketDataStatus.DELAYED, delay=None, at=NOW)
    assert not policy.decide(_ask("o354"), _clearance(evidence=unknown), now=NOW).confirm


@pytest.mark.parametrize(
    "status",
    [MarketDataStatus.REALTIME, MarketDataStatus.FROZEN, MarketDataStatus.NONE, None],
)
def test_no_market_data_needs_a_delayed_quote(policy: ReplyPolicy, status: Any) -> None:
    other = quote(status=status, delay=0, at=NOW)
    assert not policy.decide(_ask("o354"), _clearance(evidence=other), now=NOW).confirm


def test_no_market_data_outside_the_collar(policy: ReplyPolicy) -> None:
    reaching = terms(limit="103")
    verdict = policy.decide(_ask("o354"), _clearance(reaching), now=NOW)
    assert not verdict.confirm
    assert "collar" in verdict.reason


def test_no_market_data_needs_a_volatility_to_widen_the_collar(policy: ReplyPolicy) -> None:
    assert not policy.decide(_ask("o354"), _clearance(sigma=None), now=NOW).confirm


def test_no_market_data_without_a_reference_price(policy: ReplyPolicy) -> None:
    bare = QuoteEvidence(
        conid=265598, status=MarketDataStatus.DELAYED, delay_seconds=1200, observed_at=NOW
    )
    assert not policy.decide(_ask("o354"), _clearance(evidence=bare), now=NOW).confirm


def test_no_market_data_for_a_protective_stop_resting_below_the_band(policy: ReplyPolicy) -> None:
    resting = terms(side=OrderSide.SELL, stop="95", limit="94")
    inside = terms(side=OrderSide.SELL, stop="99.5", limit="99")
    confirm = policy.decide(_ask("o354"), _clearance(resting, effect=RiskEffect.REDUCES), now=NOW)
    assert confirm.confirm, confirm.reason
    refuse = policy.decide(_ask("o354"), _clearance(inside, effect=RiskEffect.REDUCES), now=NOW)
    assert not refuse.confirm


def test_the_decider_is_bound_to_its_clearance(policy: ReplyPolicy) -> None:
    decide = policy.decider(_clearance(), lambda: NOW)
    assert decide(_ask("o354")).confirm
    assert not decide(_ask("o999")).confirm
