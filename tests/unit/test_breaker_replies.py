"""Attacks on the reply policy and the quote policy at their exact edges.

The rule (MASTER_PLAN §5.12, DECISIONS D49): only whitelisted message ids are confirmed; ``o354``
only when the asset class accepts delayed quotes, the clearance carries a fresh delayed quote no
later than ``max_delay_seconds``, and the order is limit-capped inside the collar; a delayed
quote without a sigma is refused; anything else is declined. Each limit is tried at the exact
value and one step past it, and the message ids in spellings IBKR might send.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from order_rig import quote, terms

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_execution import RealtimePreferred
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import (
    ClearanceAction,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    QuoteEvidence,
    RiskEffect,
)
from obliskiq.execution.quotes import QuotePolicy
from obliskiq.execution.replies import ReplyPolicy
from obliskiq.ibkr.order_decode import Question

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
TICK = Decimal("0.01")
REFERENCE = Decimal(100)  # the mid of the rig's 99.9 / 100.1 quote
SIGMA = 0.3


def _clearance(
    order_terms: OrderTerms | None = None,
    *,
    evidence: QuoteEvidence | None = None,
    asset_class: AssetClass = AssetClass.STOCK,
    sigma: float | None = SIGMA,
) -> PretradeClearance:
    chosen = order_terms if order_terms is not None else terms()
    return PretradeClearance(
        clearance_id=uuid.uuid4(),
        intent_id=uuid.uuid7(),
        action=ClearanceAction.PLACE,
        terms=chosen,
        effect=RiskEffect.ADDS,
        asset_class=asset_class,
        quote=evidence if evidence is not None else _delayed(),
        sigma=sigma,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )


def _delayed(delay: int | None = 1200, at: datetime = NOW, **kwargs: Any) -> QuoteEvidence:
    return quote(status=MarketDataStatus.DELAYED, delay=delay, at=at, **kwargs)


def _ask(*ids: str) -> Question:
    return Question(reply_id="r1", message_ids=ids, messages=("text",))


@pytest.fixture
def policy(shipped_settings: ObliskConfig) -> ReplyPolicy:
    return ReplyPolicy(shipped_settings)


@pytest.fixture
def stock_rule(shipped_settings: ObliskConfig) -> RealtimePreferred:
    rule = QuotePolicy(shipped_settings).rule_for(AssetClass.STOCK)
    assert isinstance(rule, RealtimePreferred)
    return rule


def _band(settings: ObliskConfig, delay: int) -> tuple[Decimal, Decimal]:
    fraction = QuotePolicy(settings).collar_fraction(sigma=SIGMA, delay_seconds=delay)
    assert fraction is not None
    return REFERENCE * (1 - fraction), REFERENCE * (1 + fraction)


def _o354(policy: ReplyPolicy, clearance: PretradeClearance, now: datetime = NOW) -> bool:
    return policy.decide(_ask("o354"), clearance, now=now).confirm


# ── freshness and delay, at the limit and one step past it ──


def test_a_quote_exactly_as_old_as_allowed_is_fresh_and_a_microsecond_more_is_not(
    policy: ReplyPolicy, stock_rule: RealtimePreferred
) -> None:
    age = timedelta(seconds=stock_rule.max_quote_age_seconds)
    assert _o354(policy, _clearance(), NOW + age)
    assert not _o354(policy, _clearance(), NOW + age + timedelta(microseconds=1))


def test_a_quote_read_this_instant_is_fresh(policy: ReplyPolicy) -> None:
    assert _o354(policy, _clearance(), NOW)
    assert not _o354(policy, _clearance(), NOW - timedelta(microseconds=1))  # from the future


def test_a_delay_exactly_at_the_maximum_passes_and_one_second_more_does_not(
    policy: ReplyPolicy, stock_rule: RealtimePreferred
) -> None:
    most = stock_rule.max_delay_seconds
    assert _o354(policy, _clearance(evidence=_delayed(delay=most)))
    assert not _o354(policy, _clearance(evidence=_delayed(delay=most + 1)))


# ── the collar, at its edge and one tick past it ──


def test_a_buy_limit_on_the_collar_passes_and_one_tick_above_does_not(
    policy: ReplyPolicy, shipped_settings: ObliskConfig
) -> None:
    _, upper = _band(shipped_settings, 1200)
    assert _o354(policy, _clearance(terms(limit=str(upper))))
    assert not _o354(policy, _clearance(terms(limit=str(upper + TICK))))


def test_a_sell_limit_on_the_collar_passes_and_one_tick_below_does_not(
    policy: ReplyPolicy, shipped_settings: ObliskConfig
) -> None:
    lower, _ = _band(shipped_settings, 1200)
    sell = OrderSide.SELL
    assert _o354(policy, _clearance(terms(side=sell, limit=str(lower))))
    assert not _o354(policy, _clearance(terms(side=sell, limit=str(lower - TICK))))


def test_a_sell_stop_on_the_band_passes_and_one_tick_inside_does_not(
    policy: ReplyPolicy, shipped_settings: ObliskConfig
) -> None:
    lower, _ = _band(shipped_settings, 1200)

    def stop_at(stop: Decimal) -> OrderTerms:
        return terms(side=OrderSide.SELL, stop=str(stop), limit=str(stop - 1))

    assert _o354(policy, _clearance(stop_at(lower)))
    assert not _o354(policy, _clearance(stop_at(lower + TICK)))


def test_a_passive_limit_far_from_the_reference_is_never_refused_by_the_collar(
    policy: ReplyPolicy,
) -> None:
    assert _o354(policy, _clearance(terms(limit="0.01")))
    assert _o354(policy, _clearance(terms(side=OrderSide.SELL, limit="100000")))


def test_a_crossed_quote_without_a_last_price_has_no_reference(policy: ReplyPolicy) -> None:
    crossed = _delayed(bid="100.2", ask="99.8")
    assert not _o354(policy, _clearance(evidence=crossed))


def test_a_delayed_quote_claiming_no_delay_and_without_a_sigma_is_declined(
    policy: ReplyPolicy,
) -> None:
    assert not _o354(policy, _clearance(evidence=_delayed(delay=0), sigma=None))


def test_a_delayed_quote_without_a_sigma_is_declined(policy: ReplyPolicy) -> None:
    """The control for the test above: with a real delay, the missing sigma is refused."""
    assert not _o354(policy, _clearance(evidence=_delayed(delay=1), sigma=None))


@pytest.mark.parametrize("asset_class", [AssetClass.ETF, AssetClass.ETC, AssetClass.BOND])
def test_every_class_that_prefers_real_time_takes_a_fresh_delayed_quote(
    policy: ReplyPolicy, asset_class: AssetClass
) -> None:
    assert _o354(policy, _clearance(asset_class=asset_class))


# ── message ids, as IBKR might spell them ──


@pytest.mark.parametrize(
    "message_id",
    ["O354", "o354 ", " o354", "o0354", "o3540", "p354", "354", "O10153", "o10153x", "o1015"],
)
def test_an_id_spelled_otherwise_than_the_policy_is_declined(
    policy: ReplyPolicy, message_id: str
) -> None:
    assert not policy.decide(_ask(message_id), _clearance(), now=NOW).confirm


def test_the_ids_spelled_as_the_policy_has_them_are_confirmed(policy: ReplyPolicy) -> None:
    """The positive control for the spellings above."""
    for message_id in ("o354", "o10153"):
        assert policy.decide(_ask(message_id), _clearance(), now=NOW).confirm


def test_a_whitelisted_id_repeated_beside_an_unknown_one_is_declined(policy: ReplyPolicy) -> None:
    assert not policy.decide(_ask("o10153", "o10153", "o999"), _clearance(), now=NOW).confirm
