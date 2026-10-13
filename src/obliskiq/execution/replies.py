"""The reply policy: which order warnings are confirmed (MASTER_PLAN §5.12).

A warning is confirmed only when every message id it carries is allowed for this order:

* an id in ``execution.reply_policy.confirm``, for the orders its ``when`` names;
* IBKR's no-market-data warning (``o354``), only when the asset class's quote policy accepts
  delayed quotes, the clearance carries a fresh delayed quote within the maximum delay, and the
  order is limit-capped inside the collar around it;
* nothing else. A warning without an id, or with any id outside the policy, is declined, and the
  order client then cancels whatever the broker lists under the cOID; the caller alerts.

The decision uses only what the clearance recorded: IBKR invalidates a warning as soon as any
other request is sent, so nothing may be read while one is open.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import datetime

from ..config.schema import ObliskConfig
from ..config.schema_execution import RealtimePreferred
from ..domain.orders import OrderTerms, PretradeClearance
from ..ibkr.order_decode import Question
from ..ibkr.orders import ReplyDecider, ReplyVerdict
from .quotes import QuotePolicy


def _applies(when: str, terms: OrderTerms) -> bool:
    if when == "any":
        return True
    if when == "stop_limit":
        return terms.price.kind == "stop_limit"
    if when == "cash_quantity":
        return terms.cash_quantity is not None and not terms.conversion
    return when == "conversion" and terms.conversion


class ReplyPolicy:
    """Decides each warning from ``execution.reply_policy`` and the quote policy."""

    def __init__(self, settings: ObliskConfig, quotes: QuotePolicy | None = None) -> None:
        policy = settings.execution.reply_policy
        self._confirm = {entry.message_id: entry.when for entry in policy.confirm}
        self._no_market_data = policy.no_market_data_message_id
        self._quotes = quotes if quotes is not None else QuotePolicy(settings)

    def decide(
        self, question: Question, clearance: PretradeClearance, *, now: datetime
    ) -> ReplyVerdict:
        if not question.message_ids:
            return ReplyVerdict(False, "a warning without a message id is never confirmed")
        for message_id in question.message_ids:
            verdict = self._one(message_id, clearance, now)
            if not verdict.confirm:
                return verdict
        return ReplyVerdict(True, f"confirmed {', '.join(question.message_ids)}")

    def _one(self, message_id: str, clearance: PretradeClearance, now: datetime) -> ReplyVerdict:
        if message_id == self._no_market_data:
            return self._without_market_data(clearance, now)
        when = self._confirm.get(message_id)
        if when is None:
            return ReplyVerdict(False, f"warning {message_id} is not in the reply policy")
        if not _applies(when, clearance.terms):
            return ReplyVerdict(False, f"warning {message_id} is confirmed only for {when} orders")
        return ReplyVerdict(True, f"{message_id} is confirmed for {when} orders")

    def _without_market_data(self, clearance: PretradeClearance, now: datetime) -> ReplyVerdict:
        message_id = self._no_market_data
        asset_class = clearance.asset_class
        if not isinstance(self._quotes.rule_for(asset_class), RealtimePreferred):
            return ReplyVerdict(
                False,
                f"{message_id}: the quote policy for {asset_class.value} refuses delayed quotes",
            )
        quote = clearance.quote
        check = self._quotes.check(asset_class, quote, now=now, require_delayed=True)
        if not check.ok or quote is None:
            return ReplyVerdict(False, f"{message_id}: {check.reason}")
        collar = self._quotes.within_collar(clearance.terms, quote, sigma=clearance.sigma)
        if not collar.ok:
            return ReplyVerdict(False, f"{message_id}: {collar.reason}")
        return ReplyVerdict(True, f"{message_id}: {check.reason}, {collar.reason}")

    def decider(self, clearance: PretradeClearance, now: Callable[[], datetime]) -> ReplyDecider:
        """The decider for one placement, bound to its clearance."""

        def decide(question: Question) -> ReplyVerdict:
            return self.decide(question, clearance, now=now())

        return decide
