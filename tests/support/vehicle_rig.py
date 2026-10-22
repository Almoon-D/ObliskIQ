"""Helpers for the vehicle selector's tests: settings with capabilities verified, instruments
changed, the selector over the shipped profile, and its inputs.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_vehicles import CapabilityCheck, CapabilityReading
from obliskiq.decision.vehicle_selector import SelectorInputs, VehicleSelector, resolve_exposure
from obliskiq.domain.enums import AssetClass
from obliskiq.jurisdiction import ELIGIBILITY_POLICIES, TAX_POLICIES, load_profile

REPO = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 10, 22)
USD = Decimal("0.86")
CORE_ROLES = (
    "world_equity",
    "em_equity",
    "euro_govt_intermediate",
    "euro_govt_long",
    "inflation_linked",
    "us_treasuries",
    "gold",
    "broad_commodities",
    "cash",
)


def verified(day: date = date(2026, 10, 21)) -> CapabilityReading:
    return CapabilityReading(status="verified", route="CP", observed_on=day, evidence="read")


def unverified() -> CapabilityReading:
    return CapabilityReading(status="unverified", route=None, observed_on=None, evidence="not read")


def with_checks(settings: ObliskConfig, *keys: tuple[AssetClass, str]) -> ObliskConfig:
    """The settings with every check of ``keys`` verified (search, quote and order)."""
    base = settings.broker_capabilities
    lists = base.checks[0].order_types
    checks = [c for c in base.checks if (c.asset_class, c.venue) not in keys]
    checks += [
        CapabilityCheck(
            asset_class=kind,
            venue=venue,
            probe=None,
            search=verified(),
            quote=verified(),
            order=verified(),
            order_types=lists,
            algos=lists,
        )
        for kind, venue in keys
    ]
    return settings.model_copy(
        update={"broker_capabilities": base.model_copy(update={"checks": checks})}
    )


def with_vehicles(settings: ObliskConfig, **changes: Any) -> ObliskConfig:
    return settings.model_copy(update={"vehicles": settings.vehicles.model_copy(update=changes)})


def replace_instrument(settings: ObliskConfig, symbol: str, **changes: Any) -> ObliskConfig:
    """The settings with one universe instrument changed (attributes through ``attributes=``)."""
    universe = settings.universe
    instruments = [
        i.model_copy(update=changes) if i.symbol == symbol else i for i in universe.instruments
    ]
    return settings.model_copy(
        update={"universe": universe.model_copy(update={"instruments": instruments})}
    )


def attributes(settings: ObliskConfig, symbol: str, **changes: Any) -> ObliskConfig:
    declared = settings.universe.instrument(symbol)
    assert declared is not None
    return replace_instrument(
        settings, symbol, attributes=declared.attributes.model_copy(update=changes)
    )


def selector(settings: ObliskConfig, **kw: Any) -> VehicleSelector:
    profile = load_profile(REPO / "config", "es")
    return VehicleSelector(
        settings,
        ELIGIBILITY_POLICIES.resolve(profile, settings),
        TAX_POLICIES.resolve(profile, settings),
        **kw,
    )


def inputs(**kw: Any) -> SelectorInputs:
    values: dict[str, Any] = {"as_of": AS_OF, "rates": {"USD": USD, "GBP": Decimal("1.15")}}
    values.update(kw)
    return SelectorInputs(**values)


def select(settings: ObliskConfig, role: str, **kw: Any) -> Any:
    return selector(settings).select(resolve_exposure(settings, role), inputs(**kw))
