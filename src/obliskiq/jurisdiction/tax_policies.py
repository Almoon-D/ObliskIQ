"""The tax policies ObliskIQ ships, by id.

``config/jurisdictions/<id>.yaml: policies.tax`` selects one; it is built from the profile's
``tax`` section.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Final

from ..config.schema import ObliskConfig
from .profile import JurisdictionProfile
from .registry import PolicyRegistry
from .tax import TAX_KIND, TaxPolicy, require_tax_profile
from .tax_es_irpf import ES_IRPF_POLICY_ID, EsIrpfTaxPolicy
from .tax_generic import GENERIC_POLICY_ID, GenericTaxPolicy


def _es_irpf(profile: JurisdictionProfile, _settings: ObliskConfig) -> TaxPolicy:
    return EsIrpfTaxPolicy(require_tax_profile(profile))


def _generic(profile: JurisdictionProfile, _settings: ObliskConfig) -> TaxPolicy:
    return GenericTaxPolicy(GENERIC_POLICY_ID, require_tax_profile(profile))


#: The tax policies ObliskIQ ships, by id.
TAX_POLICIES: Final = PolicyRegistry[TaxPolicy](
    TAX_KIND, [(ES_IRPF_POLICY_ID, _es_irpf), (GENERIC_POLICY_ID, _generic)]
)
