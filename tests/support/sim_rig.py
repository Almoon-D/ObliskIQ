"""A simulator over a toy panel, for the tests that hold the account's code to the research's:
a flat commission, a tax policy of the shipped ``es`` profile, instruments in euros with a distinct
issuer and index each.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from obliskiq.config.schema_calendars import Provenance
from obliskiq.config.schema_costs import CommissionSchedule
from obliskiq.domain.enums import AssetClass
from obliskiq.domain.research import Observation
from obliskiq.domain.tax import SecurityIdentity, SecurityKind
from obliskiq.jurisdiction import load_profile
from obliskiq.jurisdiction.tax_es_irpf import EsIrpfTaxPolicy
from obliskiq.quant.costs import CostBook, FrictionInputs
from obliskiq.research.simulate import SimInstrument, SimulationSettings

D = Decimal
SOURCE = Provenance(origin="test", verify_live=False)


def flat_costs(rate: str = "0.0000001") -> CostBook:
    commission = CommissionSchedule(
        id="flat", venues=None, asset_classes=[AssetClass.ETF], currency="EUR",
        kind="fraction_of_value", rate=D(rate), minimum=None, maximum_fraction_of_value=None,
        up_to_face=None, scope_note="test", source=SOURCE,
    )  # fmt: skip
    friction = FrictionInputs(
        liquid_market_cap_usd=D(0), liquid_spread_fraction=D(0), illiquid_spread_fraction=D(0),
        liquid_slippage_fraction=D(0), illiquid_slippage_fraction=D(0), impact_coefficient=D(0),
        impact_cap_fraction=D(0), liquid_half_spread_fraction=D(0),
        single_name_half_spread_fraction=D(0), liquid_underlyings=frozenset(), unverified=False,
        origin="test",
    )  # fmt: skip
    return CostBook([commission], [], friction)


def es_policy(repo_root: Path) -> EsIrpfTaxPolicy:
    profile = load_profile(repo_root / "config", "es")
    assert profile.tax is not None
    return EsIrpfTaxPolicy(profile.tax)


def instrument(key: str, issuer: str) -> SimInstrument:
    identity = SecurityIdentity(
        key=key, kind=SecurityKind.ETF, isin=None, issuer=issuer, tracked_index=f"{issuer} index",
        listed=True,
    )  # fmt: skip
    return SimInstrument(
        key=key, venue="XETR", asset_class=AssetClass.ETF, currency="EUR", security=identity,
        issuer_country=None, market_cap_usd=D(0), income_country=None,
    )  # fmt: skip


def series(points: list[tuple[date, float]]) -> tuple[Observation, ...]:
    return tuple(Observation(observed_on=d, available_on=d, value=v) for d, v in points)


def settings(capital: str) -> SimulationSettings:
    return SimulationSettings(
        base_currency="EUR", initial_capital=D(capital), no_trade_band=D("0.005"),
        max_price_age_days=7, max_fx_age_days=7, whole_units=True, days_per_year=365.25,
        cost_multiplier=D(1),
    )  # fmt: skip
