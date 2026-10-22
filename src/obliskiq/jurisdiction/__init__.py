"""Jurisdiction profiles (``config/jurisdictions/<id>.yaml``) and the registry that resolves
the policies a profile selects: what may be bought and held, and how gains are taxed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from .eligibility import EligibilityPolicy, EligibilityProfile, EligibilityVerdict
from .eligibility_eu_priips import EuPriipsEligibility
from .eligibility_policies import ELIGIBILITY_POLICIES
from .profile import JurisdictionProfile, load_profile
from .registry import PolicyRegistry, check_declared_policies
from .shipped import SHIPPED_POLICY_KINDS
from .tax import TaxPolicy
from .tax_policies import TAX_POLICIES

__all__ = [
    "ELIGIBILITY_POLICIES",
    "SHIPPED_POLICY_KINDS",
    "TAX_POLICIES",
    "EligibilityPolicy",
    "EligibilityProfile",
    "EligibilityVerdict",
    "EuPriipsEligibility",
    "JurisdictionProfile",
    "PolicyRegistry",
    "TaxPolicy",
    "check_declared_policies",
    "load_profile",
]
