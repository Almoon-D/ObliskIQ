"""Eligibility: what the owner's jurisdiction lets the bot buy (MASTER_PLAN §2, §5.16).

An :class:`EligibilityPolicy` judges one instrument (an option with its underlying) and answers
an :class:`EligibilityVerdict`: eligible or not, why, and whether the instrument may be held only
under the mandatory close (a listed option on an ETP the profile does not allow, closed before
expiry). The pre-trade gate asks it about every order that adds risk; an order that reduces risk
waives buy-side eligibility (I3). Reading any instrument as data is always allowed and never
asks.

The one policy this build registers is the **skeleton**: only the instruments in
``eligibility.skeleton_allowlist`` are eligible. It is labelled as a skeleton wherever it answers
and is removed in Session 8, when the universe and the ``eu_priips`` policy exist.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Protocol

from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass
from ..domain.instrument import TradedInstrument
from .profile import JurisdictionProfile
from .registry import PolicyRegistry

#: The policy kind a profile selects under ``policies``.
ELIGIBILITY_KIND: Final = "eligibility"

#: The skeleton policy's id (``config/jurisdictions/es.yaml`` selects it until Session 8).
SKELETON_POLICY_ID: Final = "skeleton"

#: Exchange-traded products: an option on one the policy refuses is held under the mandatory close.
EXCHANGE_TRADED_PRODUCTS: Final = frozenset({AssetClass.ETF, AssetClass.ETC})


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    """A policy's answer. ``mandatory_close``: eligible only if closed before expiry."""

    eligible: bool
    reason: str
    policy: str
    skeleton: bool
    mandatory_close: bool = False


class EligibilityPolicy(Protocol):
    """What may be bought under one jurisdiction."""

    @property
    def policy_id(self) -> str: ...

    @property
    def skeleton(self) -> bool:
        """Whether this is a stand-in policy, to be replaced (it says so in every verdict)."""
        ...

    def assess(
        self, instrument: TradedInstrument, *, underlying: TradedInstrument | None
    ) -> EligibilityVerdict:
        """Judge ``instrument``; ``underlying`` is an option's underlying, else ``None``."""
        ...


class SkeletonEligibility:
    """Eligible: exactly the instruments in ``eligibility.skeleton_allowlist``. A skeleton."""

    def __init__(self, allowlist: Iterable[TradedInstrument]) -> None:
        self._allowed = {instrument.conid: instrument for instrument in allowlist}

    @property
    def policy_id(self) -> str:
        return SKELETON_POLICY_ID

    @property
    def skeleton(self) -> bool:
        return True

    def _verdict(
        self, eligible: bool, reason: str, *, mandatory_close: bool = False
    ) -> EligibilityVerdict:
        return EligibilityVerdict(
            eligible=eligible,
            reason=f"{reason} (skeleton policy)",
            policy=self.policy_id,
            skeleton=True,
            mandatory_close=mandatory_close,
        )

    def assess(
        self, instrument: TradedInstrument, *, underlying: TradedInstrument | None
    ) -> EligibilityVerdict:
        listed = self._allowed.get(instrument.conid)
        if listed is None:
            return self._verdict(False, f"conid {instrument.conid} is not in the allowlist")
        if listed != instrument:
            return self._verdict(
                False, f"conid {instrument.conid} is described otherwise than the allowlist does"
            )
        if instrument.asset_class is not AssetClass.OPTION:
            return self._verdict(True, f"{instrument.symbol} is in the allowlist")
        if underlying is None or underlying.conid != instrument.underlying_conid:
            return self._verdict(False, f"the underlying of {instrument.symbol} is not described")
        if underlying.conid in self._allowed:
            return self._verdict(True, f"{instrument.symbol} and its underlying are allowed")
        if underlying.asset_class in EXCHANGE_TRADED_PRODUCTS:
            return self._verdict(
                True,
                f"{instrument.symbol} is an option on {underlying.symbol}, an ETP the policy "
                "does not allow: held only if closed before expiry",
                mandatory_close=True,
            )
        return self._verdict(
            False, f"the underlying {underlying.symbol} of {instrument.symbol} is not allowed"
        )


def _skeleton(_profile: JurisdictionProfile, settings: ObliskConfig) -> EligibilityPolicy:
    return SkeletonEligibility(settings.eligibility.skeleton_allowlist)


#: The eligibility policies ObliskIQ ships, by id.
ELIGIBILITY_POLICIES: Final = PolicyRegistry[EligibilityPolicy](
    ELIGIBILITY_KIND, [(SKELETON_POLICY_ID, _skeleton)]
)
