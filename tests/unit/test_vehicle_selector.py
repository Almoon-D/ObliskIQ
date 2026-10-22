"""The vehicle selector: gates with reasons, the ranking's criteria, hedged classes as distinct
vehicles, and no vehicle proposed whose broker capability is unverified (Session 9).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from vehicle_rig import (
    CORE_ROLES,
    REPO,
    USD,
    attributes,
    inputs,
    replace_instrument,
    select,
    selector,
    unverified,
    with_checks,
    with_vehicles,
)

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_execution import RealtimeRequired
from obliskiq.config.schema_vehicles import RankingWeights
from obliskiq.decision.capabilities import BrokerCapabilities
from obliskiq.decision.vehicle_selector import (
    Criterion,
    ExposureKind,
    Gate,
    ListedProductRule,
    UnknownExposureError,
    VehicleMarket,
    VehicleSelector,
    WindowOverlap,
    resolve_exposure,
    window_overlap,
)
from obliskiq.domain.enums import AssetClass, ShareClass
from obliskiq.jurisdiction import ELIGIBILITY_POLICIES, TAX_POLICIES, load_profile
from obliskiq.quant.tracking import CorrelationReading

# ── the session's named tests ──


def test_a_hedged_and_an_unhedged_class_of_one_fund_are_distinct_vehicles(
    shipped_settings: ObliskConfig,
) -> None:
    """IBC5 is the EUR-hedged class of the fund TPSA belongs to: each is ranked on its own."""
    settings = with_checks(shipped_settings, (AssetClass.ETF, "XAMS"))
    allowed = with_vehicles(settings, hedged_share_classes="allowed")
    ranked = {v.symbol: v for v in select(allowed, "inflation_linked").ranked}
    hedged, unhedged = ranked["IBC5"], ranked["TPSA"]
    assert hedged.hedged
    assert not unhedged.hedged
    assert hedged.conid != unhedged.conid
    assert "hedges to EUR" in hedged.criterion(Criterion.CURRENCY).reason
    assert hedged.all_in_cost != unhedged.all_in_cost  # each class's own fee
    # Undecided, the hedged class is refused on its own while its unhedged sibling is proposed.
    undecided = select(settings, "inflation_linked")
    assert "TPSA" in [v.symbol for v in undecided.ranked]
    refused = {r.symbol: r for r in undecided.rejected}
    assert refused["IBC5"].gate is Gate.HEDGE_NOT_ALLOWED
    assert "not ruled" in refused["IBC5"].reason


def test_an_unverified_capability_is_never_proposed(shipped_settings: ObliskConfig) -> None:
    capabilities = BrokerCapabilities(shipped_settings)
    for role in CORE_ROLES:
        selection = select(shipped_settings, role)
        for vehicle in selection.ranked:
            assert capabilities.assess(vehicle.asset_class, vehicle.venue).verified
        refusals = [r for r in selection.rejected if r.gate is Gate.CAPABILITY_UNVERIFIED]
        for rejection in refusals:
            assert "not verified through the Web API" in rejection.reason
    world = {r.symbol: r for r in select(shipped_settings, "world_equity").rejected}
    assert world["IWDA"].gate is Gate.CAPABILITY_UNVERIFIED  # etf on XLON: read on MCP only
    # Withdraw XETR's preview and its ETFs drop out, with the missing check named.
    base = shipped_settings.broker_capabilities
    checks = [
        c.model_copy(update={"order": unverified()})
        if (c.asset_class, c.venue) == (AssetClass.ETF, "XETR")
        else c
        for c in base.checks
    ]
    withdrawn = shipped_settings.model_copy(
        update={"broker_capabilities": base.model_copy(update={"checks": checks})}
    )
    selection = select(withdrawn, "world_equity")
    assert selection.ranked == ()
    reasons = {r.symbol: r.reason for r in selection.rejected}
    assert "order unverified" in reasons["XDWD"]


def test_ranking_reasons_are_complete(shipped_settings: ObliskConfig) -> None:
    settings = with_checks(
        shipped_settings,
        (AssetClass.ETF, "XLON"),
        (AssetClass.ETF, "XMIL"),
        (AssetClass.ETF, "XAMS"),
    )
    selection = select(settings, "world_equity")
    weights = settings.vehicles.ranking.weights
    for vehicle in selection.ranked:
        assert {s.criterion for s in vehicle.scores} == set(Criterion)
        assert all(s.reason for s in vehicle.scores)
        assert all(Decimal(0) <= s.score <= 1 for s in vehicle.scores)
        assert {s.criterion.value: s.weight for s in vehicle.scores} == weights.model_dump()
        assert vehicle.score == sum(s.contribution for s in vehicle.scores)
        assert len(vehicle.passed) == 3
        assert all(vehicle.passed)
    scores = [v.score for v in selection.ranked]
    assert scores == sorted(scores, reverse=True)
    # Every candidate is accounted for, proposed or refused with a reason.
    declared = {i.symbol for i in selector(settings).candidates(selection.exposure)}
    seen = {v.symbol for v in selection.ranked} | {r.symbol for r in selection.rejected}
    assert seen == declared
    assert all(r.reason for r in selection.rejected)


def test_every_core_role_gets_an_eligible_vehicle_from_fixtures(
    shipped_settings: ObliskConfig,
) -> None:
    """With the capabilities of every candidate's venue verified (a fixture), every role of the
    core has a vehicle; each one proposed is eligible under the profile."""
    keys = {
        (i.asset_class, shipped_settings.ibkr_exchanges[i.listing_exchange].venue)
        for i in shipped_settings.universe.instruments
        if i.exposures.core_roles
    }
    settings = with_checks(shipped_settings, *sorted(keys))
    profile = load_profile(REPO / "config", "es")
    policy = ELIGIBILITY_POLICIES.resolve(profile, settings)
    for role in CORE_ROLES:
        selection = select(settings, role)
        assert selection.best is not None, role
        for vehicle in selection.ranked:
            declared = settings.universe.instrument(vehicle.symbol)
            assert declared is not None
            assert policy.assess_declared(declared).eligible
        candidates = [i for i in settings.universe.instruments if role in i.exposures.core_roles]
        assert len(candidates) >= 2, role


# ── exposures ──


def test_an_exposure_is_a_core_role_or_a_grid_cell(shipped_settings: ObliskConfig) -> None:
    role = resolve_exposure(shipped_settings, "world_equity")
    assert (role.kind, role.series) == (ExposureKind.CORE_ROLE, "msci_world_eur.level")
    cell = resolve_exposure(shipped_settings, "eurozone/financials")
    assert cell.kind is ExposureKind.GRID_CELL
    assert cell.measured_by == "EXV1"
    assert [i.symbol for i in selector(shipped_settings).candidates(cell)] == ["EXV1"]
    unmapped = next(c for c in shipped_settings.universe.cells().values() if c.symbol is None)
    gap = resolve_exposure(shipped_settings, f"{unmapped.market}/{unmapped.sector}")
    assert gap.measured_by is None
    assert "unmapped" in gap.name
    for text in ("nowhere", "us/nothing", "germany"):
        with pytest.raises(UnknownExposureError):
            resolve_exposure(shipped_settings, text)


# ── gates ──


def test_each_gate_refuses_with_its_reason(shipped_settings: ObliskConfig) -> None:
    settings = with_checks(shipped_settings, (AssetClass.ETF, "XLON"), (AssetClass.ETC, "XLON"))
    settings = replace_instrument(
        settings, "SPPW", roles=["data"], data_only_reason="kept as data for the test"
    )
    settings = replace_instrument(settings, "XDWD", description=None)
    settings = replace_instrument(settings, "IWDA", description="ISHARES 2X WORLD")
    refused = {r.symbol: r for r in select(settings, "world_equity").rejected}
    assert refused["SPPW"].gate is Gate.NOT_TRADABLE
    assert "kept as data" in refused["SPPW"].reason
    assert refused["XDWD"].gate is Gate.NOT_DESCRIBED
    assert refused["IWDA"].gate is Gate.PROHIBITED
    assert "2X" in refused["IWDA"].reason
    unpinned = replace_instrument(settings, "XDWD", conid=None)
    refused = {r.symbol: r for r in select(unpinned, "world_equity").rejected}
    assert "not pinned" in refused["XDWD"].reason
    gold = {r.symbol: r for r in select(settings, "gold").rejected}
    assert gold["4GLD"].gate is Gate.INELIGIBLE
    assert "KID unknown" in gold["4GLD"].reason


def test_a_type_without_a_qualification_rule_is_refused_as_such(
    shipped_settings: ObliskConfig,
) -> None:
    rule = ListedProductRule(shipped_settings)
    chosen = selector(shipped_settings, rules={AssetClass.STOCK: rule})
    selection = chosen.select(resolve_exposure(shipped_settings, "cash"), inputs())
    refused = {r.symbol: r for r in selection.rejected}
    assert refused["XEON"].gate is Gate.NO_QUALIFICATION_RULE
    assert "Session 19a" in refused["XEON"].reason


def test_a_type_without_a_quote_policy_does_not_qualify(shipped_settings: ObliskConfig) -> None:
    execution = shipped_settings.execution
    policies = [
        rule.model_copy(update={"asset_classes": [a for a in rule.asset_classes if a != "etf"]})
        for rule in execution.quote_policy
        if [a for a in rule.asset_classes if a != "etf"]
    ]
    settings = shipped_settings.model_copy(
        update={"execution": execution.model_copy(update={"quote_policy": policies})}
    )
    refused = {r.symbol: r for r in select(settings, "cash").rejected}
    assert refused["XEON"].gate is Gate.NOT_QUALIFIED
    assert "no quote policy names etf" in refused["XEON"].reason


def test_hedged_classes_follow_the_owners_ruling(shipped_settings: ObliskConfig) -> None:
    refused = with_vehicles(shipped_settings, hedged_share_classes="refused")
    rejection = {r.symbol: r for r in select(refused, "world_equity").rejected}["IWLE"]
    assert "ruled against" in rejection.reason
    unknown = attributes(shipped_settings, "XDWD", hedged=None)
    rejection = {r.symbol: r for r in select(unknown, "world_equity").rejected}["XDWD"]
    assert rejection.gate is Gate.HEDGE_NOT_ALLOWED
    assert "unknown" in rejection.reason


# ── criteria ──


def ranked(settings: ObliskConfig, role: str, symbol: str, **kw: Any) -> Any:
    found = {v.symbol: v for v in select(settings, role, **kw).ranked}
    return found[symbol]


def test_the_all_in_cost_adds_the_fee_trading_and_conversions(
    shipped_settings: ObliskConfig,
) -> None:
    settings = with_checks(shipped_settings, (AssetClass.ETF, "XLON"))
    eur = ranked(settings, "world_equity", "XDWD")
    usd = ranked(settings, "world_equity", "IWDA")
    assert eur.all_in_cost is not None
    assert usd.all_in_cost is not None
    assert "conversions 0.000%" in eur.criterion(Criterion.COST).reason
    assert "conversions 0.000%" not in usd.criterion(Criterion.COST).reason
    ranking = settings.vehicles.ranking
    expected = Decimal(1) - eur.all_in_cost / ranking.cost_ceiling
    assert eur.criterion(Criterion.COST).score == expected
    # Without a rate for the vehicle's currency its cost cannot be priced, and scores 0.
    unpriced = ranked(settings, "world_equity", "IWDA", rates={})
    cost = unpriced.criterion(Criterion.COST)
    assert (cost.score, cost.measured) == (Decimal(0), False)
    assert "no rate values USD" in cost.reason


def test_a_cost_that_cannot_be_priced_scores_zero_with_why(
    shipped_settings: ObliskConfig,
) -> None:
    no_fee = attributes(shipped_settings, "XDWD", fee=None)
    cost = ranked(no_fee, "world_equity", "XDWD").criterion(Criterion.COST)
    assert cost.score == 0
    assert "ongoing charge is unknown" in cost.reason
    # A per-share schedule without a stored price cannot be priced either.
    per_share = [
        s.model_copy(update={"kind": "per_share", "maximum_fraction_of_value": None})
        if s.id == "europe_eur"
        else s
        for s in shipped_settings.costs.commissions
    ]
    settings = shipped_settings.model_copy(
        update={"costs": shipped_settings.costs.model_copy(update={"commissions": per_share})}
    )
    cost = ranked(settings, "world_equity", "XDWD").criterion(Criterion.COST)
    assert "charged per share" in cost.reason
    priced = ranked(
        settings,
        "world_equity",
        "XDWD",
        market={163606916: VehicleMarket(Decimal("120"), None, 0)},
    ).criterion(Criterion.COST)
    assert priced.measured
    # A conversion nothing prices leaves a USD vehicle unpriced.
    no_fx = [s for s in shipped_settings.costs.commissions if s.id != "spot_fx"]
    usd = with_checks(shipped_settings, (AssetClass.ETF, "XLON")).model_copy(
        update={"costs": shipped_settings.costs.model_copy(update={"commissions": no_fx})}
    )
    cost = ranked(usd, "world_equity", "IWDA").criterion(Criterion.COST)
    assert "no commission schedule prices a currency conversion" in cost.reason
    cost = ranked(usd, "world_equity", "IWDA", rates={"USD": USD}).criterion(Criterion.COST)
    assert not cost.measured


def test_real_time_data_the_policy_requires_is_costed(shipped_settings: ObliskConfig) -> None:
    execution = shipped_settings.execution
    required = RealtimeRequired(
        policy="realtime_required", asset_classes=[AssetClass.ETF], max_quote_age_seconds=60
    )
    others = [
        rule.model_copy(update={"asset_classes": [a for a in rule.asset_classes if a != "etf"]})
        for rule in execution.quote_policy
    ]
    settings = shipped_settings.model_copy(
        update={
            "execution": execution.model_copy(
                update={"quote_policy": [required, *[r for r in others if r.asset_classes]]}
            )
        }
    )
    base = ranked(shipped_settings, "world_equity", "XDWD")
    costed = ranked(settings, "world_equity", "XDWD")
    feed = next(f for f in settings.market_data.feeds if f.id == "xetra_l1")
    yearly = feed.monthly_cost.amount * 12 / settings.vehicles.ranking.reference_amount_base
    assert costed.all_in_cost == base.all_in_cost + yearly
    unpriced_feeds = [
        f.model_copy(update={"monthly_cost": f.monthly_cost.model_copy(update={"currency": "JPY"})})
        for f in settings.market_data.feeds
    ]
    no_rate = settings.model_copy(
        update={"market_data": settings.market_data.model_copy(update={"feeds": unpriced_feeds})}
    )
    cost = ranked(no_rate, "world_equity", "XDWD").criterion(Criterion.COST)
    assert "no feed is priced" in cost.reason


def test_tracking_scores_the_measured_correlation(shipped_settings: ObliskConfig) -> None:
    floor = shipped_settings.vehicles.ranking.tracking_floor
    reading = CorrelationReading(0.97, 60, date(2021, 10, 29), date(2026, 10, 22), None)
    tracking = ranked(
        shipped_settings, "world_equity", "XDWD", tracking={"XDWD": reading}
    ).criterion(Criterion.TRACKING)
    assert tracking.measured
    assert tracking.score == (Decimal("0.97") - floor) / (1 - floor)
    low = CorrelationReading(0.5, 60, date(2021, 10, 29), date(2026, 10, 22), None)
    assert (
        ranked(shipped_settings, "world_equity", "XDWD", tracking={"XDWD": low})
        .criterion(Criterion.TRACKING)
        .score
        == 0
    )
    unmeasured = CorrelationReading.unmeasured("not loaded")
    tracking = ranked(
        shipped_settings, "world_equity", "XDWD", tracking={"XDWD": unmeasured}
    ).criterion(Criterion.TRACKING)
    assert (tracking.score, tracking.measured) == (0, False)
    assert tracking.reason == "unmeasured: not loaded"
    missing = ranked(shipped_settings, "world_equity", "XDWD").criterion(Criterion.TRACKING)
    assert "no reading was taken" in missing.reason


def test_liquidity_scores_decades_of_traded_value(shipped_settings: ObliskConfig) -> None:
    conid = 163606916
    amount = shipped_settings.vehicles.ranking.reference_amount_base
    decades = shipped_settings.vehicles.ranking.liquidity_decades
    deep = VehicleMarket(Decimal("120"), amount * 10**6, 60)
    score = ranked(shipped_settings, "world_equity", "XDWD", market={conid: deep}).criterion(
        Criterion.LIQUIDITY
    )
    assert score.score == 1
    assert "60 sessions" in score.reason
    two = VehicleMarket(Decimal("120"), amount * 100, 60)
    score = ranked(shipped_settings, "world_equity", "XDWD", market={conid: two}).criterion(
        Criterion.LIQUIDITY
    )
    assert score.score == Decimal(2) / decades
    thin = VehicleMarket(Decimal("120"), amount / 2, 60)
    assert (
        ranked(shipped_settings, "world_equity", "XDWD", market={conid: thin})
        .criterion(Criterion.LIQUIDITY)
        .score
        == 0
    )
    none = ranked(shipped_settings, "world_equity", "XDWD").criterion(Criterion.LIQUIDITY)
    assert (none.score, none.measured) == (0, False)
    no_rate = with_checks(shipped_settings, (AssetClass.ETF, "XLON"))
    score = ranked(no_rate, "world_equity", "IWDA", rates={}, market={78999785: deep}).criterion(
        Criterion.LIQUIDITY
    )
    assert "no rate" in score.reason


def test_tax_follows_the_share_class_under_the_policy(shipped_settings: ObliskConfig) -> None:
    acc = ranked(shipped_settings, "euro_govt_long", "DBXG").criterion(Criterion.TAX)
    dist = ranked(shipped_settings, "euro_govt_long", "IBCL").criterion(Criterion.TAX)
    assert (acc.score, dist.score) == (1, 0)
    assert "es_irpf" in acc.reason
    assert "distribution is taxed" in dist.reason
    unknown = attributes(shipped_settings, "DBXG", share_class=None)
    score = ranked(unknown, "euro_govt_long", "DBXG").criterion(Criterion.TAX)
    assert (score.score, score.measured) == (0, False)
    gold = with_checks(shipped_settings, (AssetClass.ETC, "XMIL"))
    score = ranked(gold, "gold", "SGLD").criterion(Criterion.TAX)
    assert score.score == 1
    assert "pays no income" in score.reason


def test_a_policy_taxing_all_income_alike_scores_any_class_whole(
    shipped_settings: ObliskConfig,
) -> None:
    profile = load_profile(REPO / "config", "es")
    tax = profile.tax
    assert tax is not None
    alike = profile.model_copy(
        update={
            "tax": tax.model_copy(
                update={
                    "retained_income": tax.retained_income.model_copy(update={"deferred": False})
                }
            )
        }
    )
    chosen = VehicleSelector(
        shipped_settings,
        ELIGIBILITY_POLICIES.resolve(alike, shipped_settings),
        TAX_POLICIES.resolve(alike, shipped_settings),
    )
    selection = chosen.select(resolve_exposure(shipped_settings, "euro_govt_long"), inputs())
    score = {v.symbol: v for v in selection.ranked}["IBCL"].criterion(Criterion.TAX)
    assert score.score == 1
    assert "alike" in score.reason


def test_currency_window_and_market_data(shipped_settings: ObliskConfig) -> None:
    settings = with_checks(shipped_settings, (AssetClass.ETF, "XLON"))
    eur = ranked(settings, "world_equity", "XDWD", windows={"XETR": WindowOverlap(2, 4, "d")})
    usd = ranked(settings, "world_equity", "IWDA")
    assert eur.criterion(Criterion.CURRENCY).score == 1
    assert usd.criterion(Criterion.CURRENCY).score == 0
    assert eur.criterion(Criterion.TRADING_WINDOW).score == Decimal("0.5")
    window = usd.criterion(Criterion.TRADING_WINDOW)
    assert (window.score, window.measured) == (0, False)
    closed = ranked(settings, "world_equity", "XDWD", windows={"XETR": WindowOverlap(0, 0, "x")})
    assert not closed.criterion(Criterion.TRADING_WINDOW).measured
    data = eur.criterion(Criterion.MARKET_DATA)
    assert data.score == 0
    assert "delayed" in data.reason


def test_a_planned_feed_scores_market_data_whole(shipped_settings: ObliskConfig) -> None:
    """A share on the NYSE, where the plan streams stocks, read as a vehicle of a role."""
    fcx = shipped_settings.universe.instrument("FCX")
    assert fcx is not None
    exposures = fcx.exposures.model_copy(update={"core_roles": ["world_equity"]})
    settings = replace_instrument(shipped_settings, "FCX", exposures=exposures)
    vehicle = ranked(settings, "world_equity", "FCX", rates={"USD": USD})
    assert vehicle.criterion(Criterion.MARKET_DATA).score == 1
    assert "us_complimentary" in vehicle.criterion(Criterion.MARKET_DATA).reason


def test_the_window_counts_cycles_inside_the_order_window(
    shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    xetra = window_overlap(shipped_settings, calendars, "XETR", date(2026, 10, 13))
    assert (xetra.inside, xetra.cycles) == (2, 4)
    assert "europe" in xetra.detail
    assert "us" in xetra.detail
    tokyo = window_overlap(shipped_settings, calendars, "XTKS", date(2026, 10, 13))
    assert tokyo.inside == 1
    assert "asia" in tokyo.detail


def test_an_accumulating_vehicle_outranks_its_distributing_peer(
    shipped_settings: ObliskConfig,
) -> None:
    """DBXG and IBCL cost the same here; the accumulating class wins on tax."""
    ibcl = shipped_settings.universe.instrument("IBCL")
    assert ibcl is not None
    assert ibcl.attributes.share_class is ShareClass.DISTRIBUTING
    selection = select(shipped_settings, "euro_govt_long")
    assert [v.symbol for v in selection.ranked] == ["DBXG", "IBCL"]


def test_a_share_has_no_class_to_hedge(shipped_settings: ObliskConfig) -> None:
    """A share's unknown hedge is no reason to refuse it: only a fund's class hedges."""
    fcx = shipped_settings.universe.instrument("FCX")
    assert fcx is not None
    exposures = fcx.exposures.model_copy(update={"core_roles": ["world_equity"]})
    settings = attributes(
        replace_instrument(shipped_settings, "FCX", exposures=exposures),
        "FCX",
        hedged=None,
        hedge_currency=None,
    )
    assert "FCX" in [v.symbol for v in select(settings, "world_equity", rates={"USD": USD}).ranked]


def test_a_leg_or_a_conversion_that_cannot_be_priced_says_why(
    shipped_settings: ObliskConfig,
) -> None:
    # A share of an issuer whose transaction tax has no rate (Italy's) cannot be priced.
    tte = shipped_settings.universe.instrument("TTE")
    assert tte is not None
    italian = replace_instrument(
        with_checks(shipped_settings, (AssetClass.STOCK, "XPAR")),
        "TTE",
        description="TOTALENERGIES SE",
        exposures=tte.exposures.model_copy(update={"country": "IT"}),
    )
    cost = ranked(italian, "eurozone/energy", "TTE").criterion(Criterion.COST)
    assert (cost.score, cost.measured) == (Decimal(0), False)
    assert cost.reason.startswith("cannot be priced: ")
    assert "it_financial_transaction_tax" in cost.reason
    # Nor can a vehicle no commission schedule covers.
    no_schedule = shipped_settings.model_copy(
        update={
            "costs": shipped_settings.costs.model_copy(
                update={
                    "commissions": [
                        s for s in shipped_settings.costs.commissions if s.id != "europe_eur"
                    ]
                }
            )
        }
    )
    cost = ranked(no_schedule, "world_equity", "XDWD").criterion(Criterion.COST)
    assert cost.reason == "cannot be priced: no commission schedule for etf on XETR in EUR"
    # A conversion whose schedule is quoted in a currency without a rate, or that no schedule
    # prices on the currency venue, leaves a dollar vehicle unpriced.
    usd = with_checks(shipped_settings, (AssetClass.ETF, "XLON"))

    def spot_fx(**changes: Any) -> ObliskConfig:
        schedules = [
            s.model_copy(update=changes) if s.id == "spot_fx" else s for s in usd.costs.commissions
        ]
        return usd.model_copy(
            update={"costs": usd.costs.model_copy(update={"commissions": schedules})}
        )

    cost = ranked(spot_fx(currency="CHF"), "world_equity", "IWDA", rates={"USD": USD}).criterion(
        Criterion.COST
    )
    assert "no rate values CHF in EUR to cost the conversion" in cost.reason
    cost = ranked(spot_fx(venues=["XNYS"]), "world_equity", "IWDA").criterion(Criterion.COST)
    assert "the conversion cannot be priced: no commission schedule for fx on IDEALPRO" in (
        cost.reason
    )


def test_a_cycle_whose_venue_is_closed_is_not_counted(
    shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    """Thanksgiving: Xetra trades, New York does not, so its cycles do not run that day."""
    open_day = window_overlap(shipped_settings, calendars, "XETR", date(2026, 11, 25))
    holiday = window_overlap(shipped_settings, calendars, "XETR", date(2026, 11, 26))
    assert holiday.cycles < open_day.cycles


def test_the_ranking_weights_add_up_to_one(shipped_settings: ObliskConfig) -> None:
    weights = shipped_settings.vehicles.ranking.weights.model_dump(mode="json")
    with pytest.raises(ValidationError, match="add up to 1"):
        RankingWeights.model_validate(weights | {"cost": "0.5"})


def test_roles_and_their_series_are_declared(shipped_settings: ObliskConfig) -> None:
    def refused(change: Any, message: str) -> None:
        data = shipped_settings.model_dump(mode="json")
        change(data)
        with pytest.raises(ValidationError, match=message):
            ObliskConfig.model_validate(data)

    def undeclared_role(data: dict[str, Any]) -> None:
        data["universe"]["instruments"][0]["exposures"]["core_roles"] = ["nowhere"]

    def unloaded_series(data: dict[str, Any]) -> None:
        data["core"]["roles"]["gold"]["exposure_series"] = "nothing.close"

    def not_ecb(data: dict[str, Any]) -> None:
        data["vehicles"]["correlation"]["fx_dataset"] = "msci_world_eur"

    refused(undeclared_role, "nowhere is not in core.roles")
    refused(unloaded_series, "nothing is not a dataset of research.datasets")
    refused(not_ecb, "msci_world_eur is not an ecb dataset")
