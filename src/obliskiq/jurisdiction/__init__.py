"""Jurisdiction profiles (``config/jurisdictions/<id>.yaml``) and the registry that resolves
the policies a profile selects: what may be bought and held, and how gains are taxed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from .profile import JurisdictionProfile, load_profile
from .registry import SHIPPED_POLICY_KINDS, PolicyRegistry, check_declared_policies

__all__ = [
    "SHIPPED_POLICY_KINDS",
    "JurisdictionProfile",
    "PolicyRegistry",
    "check_declared_policies",
    "load_profile",
]
