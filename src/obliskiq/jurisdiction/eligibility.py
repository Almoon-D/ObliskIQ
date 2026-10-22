"""Eligibility: what the owner's jurisdiction lets the bot buy (MASTER_PLAN §2, §5.16).

An :class:`EligibilityPolicy` judges one instrument (an option with its underlying) and answers
an :class:`EligibilityVerdict`: eligible or not, why, and whether the instrument may be held only
under the mandatory close (a listed option on an ETP the profile does not allow, closed before
expiry). The pre-trade gate asks it about every order that adds risk; an order that reduces risk
waives buy-side eligibility (I3). Reading any instrument as data is always allowed and never
asks.

A profile selects its policy under ``policies.eligibility`` and states the values the policy
applies under ``eligibility`` (:class:`EligibilityProfile`); the policies ObliskIQ ships are
registered in :mod:`obliskiq.jurisdiction.eligibility_policies`.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from typing import Final, Literal, Protocol, Self

from pydantic import Field, model_validator

from ..config.base import FrozenModel
from ..domain.base import NonEmptyText
from ..domain.enums import AssetClass
from ..domain.instrument import TradedInstrument
from ..errors import PolicyError

#: The policy kind a profile selects under ``policies``.
ELIGIBILITY_KIND: Final = "eligibility"

#: Exchange-traded products: an option on one the policy refuses is held under the mandatory close.
EXCHANGE_TRADED_PRODUCTS: Final = frozenset({AssetClass.ETF, AssetClass.ETC})


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    """A policy's answer. ``mandatory_close``: eligible only if closed before expiry."""

    eligible: bool
    reason: str
    policy: str
    mandatory_close: bool = False


class EligibilityPolicy(Protocol):
    """What may be bought under one jurisdiction."""

    @property
    def policy_id(self) -> str: ...

    def assess(
        self, instrument: TradedInstrument, *, underlying: TradedInstrument | None
    ) -> EligibilityVerdict:
        """Judge ``instrument``; ``underlying`` is an option's underlying, else ``None``."""
        ...


class EligibilityProfile(FrozenModel):
    """The values a profile's eligibility policy applies (``config/jurisdictions/<id>.yaml``)."""

    #: Asset classes bought only when a key information document is available.
    kid_required: list[AssetClass] = Field(min_length=1)
    #: Asset classes bought only in the UCITS form.
    ucits_required: list[AssetClass]
    government_bonds: bool
    corporate_bonds: bool
    #: A listed option on an ETP the policy refuses: held under the mandatory close, or refused.
    options_on_ineligible_etps: Literal["mandatory_close", "refused"]
    source: NonEmptyText

    @model_validator(mode="after")
    def _known_classes(self) -> Self:
        if len(set(self.kid_required)) != len(self.kid_required) or len(
            set(self.ucits_required)
        ) != len(self.ucits_required):
            raise ValueError("an asset class is listed twice")
        if not set(self.ucits_required) <= set(self.kid_required):
            raise ValueError("a class that must be UCITS is a packaged product and needs a KID")
        return self


def require_eligibility_profile(
    profile_id: str, section: EligibilityProfile | None
) -> EligibilityProfile:
    """The profile's ``eligibility`` section; a policy that applies it refuses to build without
    one."""
    if section is None:
        raise PolicyError(
            f"jurisdiction profile '{profile_id}' selects an eligibility policy but carries no "
            "eligibility section"
        )
    return section
