"""A test double of a jurisdiction's eligibility policy, for the order-path tests.

Eligible: exactly the conids it lists. An option is eligible when its underlying is listed, and
held only under the mandatory close when its underlying is an ETF or ETC it does not list (the
shape of ``eu_priips``'s rule, so the gate's handling of each verdict is tested apart from how
the real policy reaches it). The real policy is tested in ``tests/unit/test_eligibility.py``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.instrument import TradedInstrument
from obliskiq.jurisdiction.eligibility import EXCHANGE_TRADED_PRODUCTS, EligibilityVerdict

POLICY_ID = "test_allowlist"


class AllowlistEligibility:
    def __init__(self, allowed: Iterable[int]) -> None:
        self.allowed = set(allowed)

    @property
    def policy_id(self) -> str:
        return POLICY_ID

    def _verdict(
        self, eligible: bool, reason: str, *, mandatory_close: bool = False
    ) -> EligibilityVerdict:
        return EligibilityVerdict(eligible, reason, POLICY_ID, mandatory_close)

    def assess(
        self, instrument: TradedInstrument, *, underlying: TradedInstrument | None
    ) -> EligibilityVerdict:
        if instrument.conid not in self.allowed:
            return self._verdict(False, f"conid {instrument.conid} is not in the allowlist")
        if instrument.asset_class is not AssetClass.OPTION:
            return self._verdict(True, f"{instrument.symbol} is in the allowlist")
        if underlying is None or underlying.conid != instrument.underlying_conid:
            return self._verdict(False, f"the underlying of {instrument.symbol} is not described")
        if underlying.conid in self.allowed:
            return self._verdict(True, f"{instrument.symbol} and its underlying are allowed")
        if underlying.asset_class in EXCHANGE_TRADED_PRODUCTS:
            return self._verdict(
                True,
                f"{instrument.symbol} is an option on {underlying.symbol}, an ETP the policy does "
                "not allow: held only if closed before expiry",
                mandatory_close=True,
            )
        return self._verdict(
            False, f"the underlying {underlying.symbol} of {instrument.symbol} is not allowed"
        )
