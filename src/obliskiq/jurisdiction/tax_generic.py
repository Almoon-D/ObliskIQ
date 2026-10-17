"""The ``generic`` tax policy: a template for a new jurisdiction.

FIFO lots, no wash-sale rule, losses offset gains and carry forward as the profile says, the
profile's rate scale and payment date. Two securities are homogeneous only when they are the
same security, no switch keeps a gain deferred, and a swap partner is any other security that
the profile's swap rule allows. A new country copies this module and states what differs.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Final

from ..domain.tax import SecurityIdentity
from .tax import ProfileTaxRules

GENERIC_POLICY_ID: Final = "generic"


class GenericTaxPolicy(ProfileTaxRules):
    """FIFO, no wash-sale rule; everything else from the profile."""

    def homogeneous(self, first: SecurityIdentity, second: SecurityIdentity) -> bool:
        return first.key == second.key or (first.isin is not None and first.isin == second.isin)

    def wash_sale_months(self, security: SecurityIdentity) -> int | None:
        del security  # no security has a wash-sale window here
        return None

    def switch_deferral(self, sold: SecurityIdentity, bought: SecurityIdentity) -> bool:
        del sold, bought  # every switch is a sale and a purchase
        return False

    def _own_rules(self) -> tuple[str, ...]:
        return ("no wash-sale rule; every switch is a sale",)

    def swap_partner_allowed(self, held: SecurityIdentity, candidate: SecurityIdentity) -> bool:
        return not self.homogeneous(held, candidate) and self._differs_as_a_swap_partner(
            held, candidate
        )
