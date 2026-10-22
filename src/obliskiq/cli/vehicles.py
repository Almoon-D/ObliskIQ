"""``obliskiq universe capabilities`` and ``obliskiq universe vehicles <exposure>``.

``universe capabilities`` prints what the broker can do through the Web API per asset class and
venue — search, quote and an accepted order preview, each with its status, route and date, the
order types and algos read — and each venue's trading permission. ``--probe`` reads them on the
paper account (whatif only, nothing placed; run on the deployment machine, under the service
lock) and prints the readings to record.

``universe vehicles <exposure>`` ranks an exposure's vehicles (a core role such as
``world_equity``, or a grid cell such as ``us/technology``): every vehicle that passes the gates
with its score and each criterion's reason, every rejection with its gate and reason, and the
swap-partner group of the best vehicle (``--primary`` names another). It reads the market store
(prices and traded value), the research store (tracking and partner correlations, ECB rates) and
the calendars; an input missing is reported as unmeasured, never filled in.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, TextIO

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..config.schema_vehicles import CapabilityReading, ListReading
from ..decision.capabilities import BrokerCapabilities
from ..decision.swap_partners import SwapGroup, SwapPartners
from ..decision.vehicle_selector import (
    Exposure,
    RankedVehicle,
    Selection,
    SelectorInputs,
    UnknownExposureError,
    VehicleMarket,
    VehicleSelector,
    WindowOverlap,
    resolve_exposure,
    window_overlap,
)
from ..errors import ObliskError
from ..execution.capability_probe import CapabilityProbe
from ..execution.runtime import OrderPath
from ..jurisdiction import ELIGIBILITY_POLICIES, TAX_POLICIES
from ..quant.tracking import CorrelationReading
from ..research.data import FxRates, universe_series_id
from ..research.tracking import ResearchCorrelations, pair_readings, tracking_readings
from ..store import open_research_store, open_store
from ..store import repos as market_repo
from ..store.db import Store
from ..util.timeutil import utc_now

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1


# ── capabilities ──


def _reading(name: str, reading: CapabilityReading) -> str:
    when = f" {reading.route} {reading.observed_on}" if reading.route else ""
    return f"{name} {reading.status}{when}"


def _listed(name: str, reading: ListReading) -> str:
    values = ", ".join(reading.values) if reading.values else reading.evidence
    return f"{name} ({reading.status}): {values}"


def capability_lines(settings: ObliskConfig) -> list[str]:
    capabilities = BrokerCapabilities(settings)
    lines = ["broker capabilities through the Web API (verified only on the CP route):"]
    for check in capabilities.checks:
        verdict = capabilities.assess(check.asset_class, check.venue)
        state = "proposable" if verdict.verified else "not proposable"
        probe = f", probe {check.probe}" if check.probe else ", no probe instrument"
        lines.append(f"  {check.asset_class.value} on {check.venue} ({state}{probe}):")
        lines += [
            f"    {_reading(name, reading)}: {reading.evidence}"
            for name, reading in (
                ("search", check.search),
                ("quote", check.quote),
                ("order", check.order),
            )
        ]
        lines.append(f"    {_listed('order types', check.order_types)}")
        lines.append(f"    {_listed('algos', check.algos)}")
    lines.append(
        "  any other asset class and venue: unverified (nothing read there through the Web API)"
    )
    lines.append("trading permissions:")
    lines += [
        f"  {reading.venue} {reading.permission}: "
        + ("held" if reading.held else "unverified")
        + f" ({reading.evidence})"
        for reading in capabilities.permissions()
    ]
    return lines


def _universe_capabilities(loaded: Loaded, out: TextIO) -> int:
    from .app import _broker_refusal
    from .collect import _with_collection_path

    settings = loaded.config.settings
    if not loaded.args.probe:
        for line in capability_lines(settings):
            print(line, file=out)
        return EXIT_OK
    refused = _broker_refusal(loaded.env, out)
    if refused is not None:
        return refused

    async def probe(path: OrderPath) -> str:
        runner = CapabilityProbe(settings, path.client, path.orders, day=utc_now().date())
        report = await runner.run(settings.broker_capabilities.checks)
        return report.render((path.client.guard.account_id,))

    try:
        print(_with_collection_path(loaded, probe), file=out)
    except ObliskError as exc:
        print(f"probe stopped: {exc}", file=out)
        return EXIT_INVALID
    return EXIT_OK


# ── vehicles ──


def market_inputs(
    store: Store, conids: Iterable[int], *, as_of: date, sessions: int
) -> dict[int, VehicleMarket]:
    """Each conid's latest close and average daily traded value over ``sessions`` stored bars."""
    out: dict[int, VehicleMarket] = {}
    for conid in conids:
        bars = market_repo.bar_series(store, conid, as_of=as_of, limit=sessions)
        if not bars:
            continue
        traded = [
            Decimal(repr(bar.close)) * Decimal(repr(bar.volume))
            for bar in bars
            if bar.volume is not None
        ]
        average = sum(traded, Decimal(0)) / len(traded) if traded else None
        out[conid] = VehicleMarket(Decimal(repr(bars[-1].close)), average, len(traded))
    return out


def base_rates(fx: FxRates, currencies: Iterable[str], base: str, on: date) -> dict[str, Decimal]:
    """The base currency's value of one unit of each currency on ``on``, where a fresh rate
    exists."""
    rates: dict[str, Decimal] = {}
    for currency in currencies:
        value = fx.to_base(1.0, currency, base, on)
        if value is not None:
            rates[currency] = Decimal(repr(value))
    return rates


def windows(
    settings: ObliskConfig, calendars: Calendars, venues: Iterable[str], today: date
) -> dict[str, WindowOverlap]:
    """Each venue's overlap with the decision cycles on its next session from ``today``."""
    out: dict[str, WindowOverlap] = {}
    for venue in sorted(set(venues)):
        day = (
            today
            if calendars.session_bounds(venue, today)
            else calendars.next_session(venue, today)
        )
        if day is not None:
            out[venue] = window_overlap(settings, calendars, venue, day)
    return out


def _vehicle_lines(vehicle: RankedVehicle, rank: int) -> list[str]:
    hedged = ", hedged class" if vehicle.hedged else ""
    lines = [
        f"  {rank}. {vehicle.symbol} ({vehicle.asset_class.value} on {vehicle.venue} in "
        f"{vehicle.currency}{hedged}): score {vehicle.score:.3f}"
    ]
    lines += [
        f"     {score.criterion.value} {score.score:.2f} x {score.weight}: {score.reason}"
        for score in vehicle.scores
    ]
    lines += [f"     passed: {reason}" for reason in vehicle.passed]
    return lines


def selection_lines(selection: Selection) -> list[str]:
    exposure = selection.exposure
    lines = [
        f"vehicles for {exposure.key} ({exposure.name}): {len(selection.ranked)} proposable, "
        f"{len(selection.rejected)} rejected"
    ]
    for rank, vehicle in enumerate(selection.ranked, start=1):
        lines += _vehicle_lines(vehicle, rank)
    if not selection.ranked:
        lines.append("  none proposable: see the rejections")
    lines.append("rejected:")
    lines += [f"  {r.symbol} [{r.gate.value}]: {r.reason}" for r in selection.rejected]
    return lines


def group_lines(group: SwapGroup) -> list[str]:
    lines = [f"swap partners of {group.primary}:"]
    lines += [f"  {p.symbol}: {p.reason}" for p in group.partners]
    if not group.partners:
        lines.append("  none")
    lines += [f"  not {r.symbol}: {r.reason}" for r in group.refused]
    return lines


def exposure_series(exposure: Exposure) -> str | None:
    if exposure.series is not None:
        return exposure.series
    return None if exposure.measured_by is None else universe_series_id(exposure.measured_by)


def _unmeasured(keys: Iterable[str], reason: str) -> dict[str, CorrelationReading]:
    return {key: CorrelationReading.unmeasured(reason) for key in keys}


def _universe_vehicles(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    try:
        exposure = resolve_exposure(settings, loaded.args.exposure)
    except UnknownExposureError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    eligibility = ELIGIBILITY_POLICIES.resolve(loaded.profile, settings)
    tax = TAX_POLICIES.resolve(loaded.profile, settings)
    selector = VehicleSelector(settings, eligibility, tax)
    candidates = selector.candidates(exposure)
    symbols = [i.symbol for i in candidates]
    today = utc_now().date()
    data_dir = loaded.env.data_dir
    venues = {selector.venue_of(i) for i in candidates}
    calendars = Calendars.from_settings(
        settings,
        reference=today,
        venues=venues | {cycle.venue for cycle in settings.runtime.cycles},
        data_dir=data_dir,
    )
    with ExitStack() as stack:
        market: Mapping[int, VehicleMarket] = {}
        if data_dir is not None and (data_dir / settings.paths.database).exists():
            store = stack.enter_context(open_store(settings, data_dir))
            market = market_inputs(
                store,
                [i.conid for i in candidates if i.conid is not None],
                as_of=today,
                sessions=settings.vehicles.ranking.liquidity_sessions,
            )
        research: ResearchCorrelations | None = None
        if data_dir is not None and (data_dir / settings.paths.research_database).exists():
            research_store = stack.enter_context(open_research_store(settings, data_dir))
            research = ResearchCorrelations(research_store, settings, as_of=today)
        no_research = "no research store (`obliskiq research data load-universe` fills it)"
        series = {symbol: universe_series_id(symbol) for symbol in symbols}
        tracking = (
            _unmeasured(symbols, no_research)
            if research is None
            else tracking_readings(research, exposure_series(exposure), series)
        )
        currencies = (
            {i.currency for i in candidates}
            | {feed.monthly_cost.currency for feed in settings.market_data.feeds}
            | {s.currency for s in settings.costs.commissions}
        )
        rates = (
            {}
            if research is None
            else base_rates(research.fx, currencies, settings.account.base_currency, today)
        )
        inputs = SelectorInputs(
            as_of=today,
            rates=rates,
            tracking=tracking,
            windows=windows(settings, calendars, venues, today),
            market=market,
        )
        selection = selector.select(exposure, inputs)
        lines = selection_lines(selection)
        primary = loaded.args.primary or (selection.best.symbol if selection.best else None)
        if primary is not None:
            pairs = [(primary, v.symbol) for v in selection.ranked if v.symbol != primary]
            readings: Mapping[tuple[str, str], CorrelationReading] = (
                {pair: CorrelationReading.unmeasured(no_research) for pair in pairs}
                if research is None
                else pair_readings(research, pairs, series)
            )
            try:
                group = SwapPartners(settings, tax).group(selection, primary, readings)
            except ValueError as exc:
                print(f"refused: {exc}", file=out)
                return EXIT_INVALID
            lines += group_lines(group)
    for line in lines:
        print(line, file=out)
    return EXIT_OK


def add_vehicle_commands(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    capabilities = actions.add_parser(
        "capabilities", help="what the broker can do through the Web API, per asset class and venue"
    )
    capabilities.add_argument(
        "--probe",
        action="store_true",
        help="read the capabilities on the paper account (whatif only, nothing placed)",
    )
    capabilities.set_defaults(handler=_universe_capabilities)
    vehicles = actions.add_parser("vehicles", help="an exposure's vehicles, ranked with reasons")
    vehicles.add_argument(
        "exposure", help="a core role (world_equity) or a grid cell (us/technology)"
    )
    vehicles.add_argument("--primary", help="the vehicle whose swap partners to list")
    vehicles.set_defaults(handler=_universe_vehicles)
