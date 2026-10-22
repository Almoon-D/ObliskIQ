"""The eligibility policies ObliskIQ ships, by id.

``config/jurisdictions/<id>.yaml: policies.eligibility`` selects one; it is built from the
profile's ``eligibility`` section and the universe.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Final

from ..config.schema import ObliskConfig
from .eligibility import ELIGIBILITY_KIND, EligibilityPolicy, require_eligibility_profile
from .eligibility_eu_priips import (
    EU_PRIIPS_POLICY_ID,
    EuPriipsEligibility,
    UnavailableContangoCheck,
)
from .profile import JurisdictionProfile
from .registry import PolicyRegistry


def _eu_priips(profile: JurisdictionProfile, settings: ObliskConfig) -> EligibilityPolicy:
    rules = require_eligibility_profile(profile.id, profile.eligibility)
    return EuPriipsEligibility(settings.universe, rules, UnavailableContangoCheck())


#: The eligibility policies ObliskIQ ships, by id.
ELIGIBILITY_POLICIES: Final = PolicyRegistry[EligibilityPolicy](
    ELIGIBILITY_KIND, [(EU_PRIIPS_POLICY_ID, _eu_priips)]
)
