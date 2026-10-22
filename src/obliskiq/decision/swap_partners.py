"""Swap-partner groups: what the core buys instead of a vehicle a purchase would defer a loss on
(MASTER_PLAN §4, §5.4, §5.8).

A partner of a vehicle is another vehicle of the same exposure that the selector proposes (it passed
every gate) and that:

1. the active TaxPolicy accepts as a swap partner: not homogeneous with the vehicle, and different
   in what the jurisdiction's rule requires (``es``: another issuer, RIRPF art. 8);
2. tracks a different index, unless the owner allows a same-index partner from another issuer
   (``vehicles.swap_partners.same_index``, off by default: DECISIONS D7, D122); an unknown index
   cannot be told apart and is refused;
3. moves with the vehicle: the correlation of their returns (``vehicles.correlation``), measured
   on research history by the caller (:mod:`obliskiq.research.tracking`), is at least
   ``vehicles.swap_partners.min_correlation``. An unmeasured correlation refuses: a partner whose
   tracking nobody has measured is not a partner.

Every candidate that is not a partner says why.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ..config.schema import ObliskConfig
from ..domain.tax import SecurityIdentity
from ..execution.instruments import UniverseDirectory
from ..jurisdiction.tax import TaxPolicy
from ..quant.tracking import CorrelationReading
from ..tax.book import TaxFacts, identity_of, universe_tax_facts
from .vehicle_selector import RankedVehicle, Selection


@dataclass(frozen=True, slots=True)
class Partner:
    symbol: str
    correlation: float
    reason: str


@dataclass(frozen=True, slots=True)
class PartnerRefusal:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class SwapGroup:
    """``primary``'s partners within one exposure, best ranked first, and the refusals."""

    exposure: str
    primary: str
    partners: tuple[Partner, ...]
    refused: tuple[PartnerRefusal, ...]


class SwapPartners:
    """Judges swap partners among a selection's ranked vehicles."""

    def __init__(self, settings: ObliskConfig, tax: TaxPolicy) -> None:
        self._settings = settings
        self._tax = tax
        self._config = settings.vehicles.swap_partners
        self._directory = UniverseDirectory.from_settings(settings)
        self._facts: Mapping[int, TaxFacts] = universe_tax_facts(settings.universe)

    def _identity(self, vehicle: RankedVehicle) -> SecurityIdentity | None:
        return identity_of(vehicle.conid, self._directory, self._facts)

    def group(
        self,
        selection: Selection,
        primary: str,
        correlations: Mapping[tuple[str, str], CorrelationReading],
    ) -> SwapGroup:
        """``primary``'s partners among ``selection``; ``correlations`` keyed (primary, other)."""
        exposure = selection.exposure.key
        held = next((v for v in selection.ranked if v.symbol == primary), None)
        if held is None:
            raise ValueError(f"{primary} is not a vehicle the selector proposes for {exposure}")
        partners: list[Partner] = []
        refused: list[PartnerRefusal] = []
        held_identity = self._identity(held)
        for candidate in selection.ranked:
            if candidate.symbol == primary:
                continue
            outcome = self._judge(held, held_identity, candidate, correlations)
            if isinstance(outcome, Partner):
                partners.append(outcome)
            else:
                refused.append(outcome)
        return SwapGroup(exposure, primary, tuple(partners), tuple(refused))

    def _judge(
        self,
        held: RankedVehicle,
        held_identity: SecurityIdentity | None,
        candidate: RankedVehicle,
        correlations: Mapping[tuple[str, str], CorrelationReading],
    ) -> Partner | PartnerRefusal:
        symbol = candidate.symbol
        identity = self._identity(candidate)
        if held_identity is None or identity is None:
            unknown = held.symbol if held_identity is None else symbol
            return PartnerRefusal(
                symbol, f"{unknown}'s tax identity (ISIN, issuer, index) is not known"
            )
        policy = self._tax.policy_id
        if self._tax.homogeneous(held_identity, identity):
            return PartnerRefusal(
                symbol,
                f"homogeneous with {held.symbol} under {policy} (the same ISIN, or the same "
                f"issuer {identity.issuer} and index {identity.tracked_index})",
            )
        if not self._tax.swap_partner_allowed(held_identity, identity):
            return PartnerRefusal(
                symbol,
                f"{policy}'s swap rule refuses it beside {held.symbol} (issuer "
                f"{held_identity.issuer} and {identity.issuer})",
            )
        index = identity.tracked_index
        if not self._config.same_index:
            if index is None or held_identity.tracked_index is None:
                return PartnerRefusal(
                    symbol, "a tracked index is unknown, so the indices cannot be told apart"
                )
            if index == held_identity.tracked_index:
                return PartnerRefusal(
                    symbol,
                    f"tracks the same index as {held.symbol} ({index}); a same-index partner "
                    "from another issuer needs the owner's switch vehicles.swap_partners."
                    "same_index",
                )
        reading = correlations.get((held.symbol, symbol))
        if reading is None or reading.value is None:
            why = "no reading was taken" if reading is None else reading.describe()
            return PartnerRefusal(symbol, f"correlation with {held.symbol} {why}")
        floor = self._config.min_correlation
        if Decimal(repr(reading.value)) < floor:
            return PartnerRefusal(
                symbol,
                f"correlation with {held.symbol} {reading.describe()}, below {floor}",
            )
        return Partner(
            symbol,
            reading.value,
            f"issuer {identity.issuer}, index {index}; correlation with {held.symbol} "
            f"{reading.describe()}",
        )
