"""Jurisdiction profiles (``config/jurisdictions/<id>.yaml``) and the registry that resolves
the policies a profile selects: what may be bought and held, and how gains are taxed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from .eligibility import (
    ELIGIBILITY_POLICIES,
    EligibilityPolicy,
    EligibilityVerdict,
    SkeletonEligibility,
)
from .profile import JurisdictionProfile, load_profile
from .registry import PolicyRegistry, check_declared_policies
from .shipped import SHIPPED_POLICY_KINDS

__all__ = [
    "ELIGIBILITY_POLICIES",
    "SHIPPED_POLICY_KINDS",
    "EligibilityPolicy",
    "EligibilityVerdict",
    "JurisdictionProfile",
    "PolicyRegistry",
    "SkeletonEligibility",
    "check_declared_policies",
    "load_profile",
]
