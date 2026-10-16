"""The policy kinds ObliskIQ ships, each with its registered policies.

A kind joins :data:`SHIPPED_POLICY_KINDS` together with its first policy; the command line checks
every profile's declared policies against it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Final

from .eligibility import ELIGIBILITY_POLICIES
from .registry import PolicyKind
from .tax_policies import TAX_POLICIES

SHIPPED_POLICY_KINDS: Final[tuple[PolicyKind, ...]] = (ELIGIBILITY_POLICIES, TAX_POLICIES)
