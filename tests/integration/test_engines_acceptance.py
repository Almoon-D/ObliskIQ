"""Session 12's acceptance, on an offline fixture store:

* the option surface comes out with honest statuses (every leaf measured, or absent with its
  reason and code);
* a futures-tracking ETC turns ineligible under ``es`` when its root's contango breaches the
  ceiling, through the commodity engine's contango check;
* the options sleeve stays in shadow while its activation condition fails, and logs its would-be
  entries.

(The mutation check on three gates is ``tests/unit/test_gates_mutation.py``; ``obliskiq compute``
running every step with each socket refused is ``tests/unit/test_cli_compute.py``.)

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from options_rig import AS_OF, SPY, STAMP, seed_options

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_universe import UniverseConfig, UniverseInstrument
from obliskiq.domain.market import CurvePoint
from obliskiq.domain.measurement import EntityKind, MeasurementStatus
from obliskiq.engines.commodity.engine import CommodityEngine, CurveContangoCheck
from obliskiq.engines.options_sleeve.sleeve import OptionsSleeve
from obliskiq.grid.option_surface import OptionSurfaceBuilder, tenor_leaf
from obliskiq.jurisdiction.eligibility import EligibilityProfile
from obliskiq.jurisdiction.eligibility_eu_priips import EuPriipsEligibility
from obliskiq.store import Store, measurements, repos
from obliskiq.store import options as options_repo


def test_the_option_surface_comes_out_with_honest_statuses(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    builder = OptionSurfaceBuilder(store, shipped_settings)
    rows = builder.build(as_of=AS_OF, computed_at=STAMP)
    builder.write(rows)
    spy = {row.leaf: row for row in rows if row.entity_id == str(SPY)}
    for row in rows:
        if row.status is MeasurementStatus.OK:
            assert row.value is not None
        else:
            assert row.reason, row.leaf
            assert row.reason_code, row.leaf
    assert spy[tenor_leaf(95, "iv_atm")].status is MeasurementStatus.OK
    assert spy[tenor_leaf(135, "iv_atm")].reason_code == "no_expiry_near_tenor"
    assert spy["iv_percentile"].reason_code == "iv_percentile_unavailable"
    unseeded = {row.entity_id for row in rows} - {str(SPY)}
    assert not unseeded, "an underlying the store does not hold is not on the surface"
    stored = measurements.session_measurements(
        store, EntityKind.INSTRUMENT, as_of=AS_OF, indicator="option_surface"
    )
    assert len(stored) == len(rows)


# ── a futures-tracking ETC under its curve ──


def rules() -> EligibilityProfile:
    return EligibilityProfile.model_validate(
        {
            "kid_required": ["etf", "etc", "fund"],
            "ucits_required": ["etf", "fund"],
            "government_bonds": True,
            "corporate_bonds": False,
            "options_on_ineligible_etps": "mandatory_close",
            "source": "the es profile's rules, as the acceptance reads them",
        }
    )


def futures_etc(universe: UniverseConfig) -> UniverseInstrument:
    base = universe.instrument("IWDA")
    assert base is not None
    data = base.model_dump(mode="json")
    data.update(
        symbol="TESTOIL",
        conid=990001,
        asset_class="etc",
        roles=["tradable_candidate", "data"],
    )
    data["exposures"] = data["exposures"] | {"core_roles": []}
    data["attributes"] = data["attributes"] | {
        "ucits": False,
        "replication": "futures",
        "futures_root": "CL",
    }
    return UniverseInstrument.model_validate(data)


def test_a_futures_tracking_etc_turns_ineligible_when_its_contango_breaches_the_limit(
    store: Store, shipped_settings: ObliskConfig, make_curve_point: Callable[..., CurvePoint]
) -> None:
    etc = futures_etc(shipped_settings.universe)
    universe = shipped_settings.universe.model_copy(
        update={"instruments": [*shipped_settings.universe.instruments, etc]}
    )
    engine = CommodityEngine(store, shipped_settings)
    ceiling = engine.contango_ceiling

    def policy() -> EuPriipsEligibility:
        return EuPriipsEligibility(universe, rules(), CurveContangoCheck(engine, as_of=AS_OF))

    def curve(roll: float) -> list[CurvePoint]:
        return [
            make_curve_point(snapshot_date=AS_OF, expiry=AS_OF + timedelta(days=30), price=70.0),
            make_curve_point(
                snapshot_date=AS_OF,
                expiry=AS_OF + timedelta(days=60),
                price=71.0,
                annualised_roll=roll,
            ),
        ]

    repos.upsert_curve_points(store, curve(-(ceiling / 2)))
    inside = policy().assess_declared(etc)
    assert inside.eligible, inside.reason
    repos.upsert_curve_points(store, curve(-(ceiling * 2)))
    beyond = policy().assess_declared(etc)
    assert not beyond.eligible
    assert "contango" in beyond.reason
    assert "beyond" in beyond.reason
    store.execute("DELETE FROM curves")
    unknown = policy().assess_declared(etc)
    assert not unknown.eligible, "no curve fails closed"


# ── the sleeve in shadow ──


def test_the_options_sleeve_stays_in_shadow_while_its_activation_condition_fails(
    store: Store, shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    seed_options(store, shipped_settings)
    sleeve = OptionsSleeve(
        store, shipped_settings, calendars=calendars, evidence=None, now=lambda: STAMP
    )
    for investable in (Decimal(1_000), Decimal(1_000_000_000)):
        run = sleeve.evaluate(as_of=AS_OF, investable=investable)
        assert not run.activation.active, "no budget activates a sleeve without its evidence"
        assert run.activation.legs[0].holds is False
    logged = sleeve.log_shadow(run)
    assert logged > 0
    entries = options_repo.shadow_entries(store, as_of=AS_OF)
    assert {entry.activation for entry in entries} == {run.activation.reason()}
