"""The service assembled: the reporter, the alert hooks, the order path, the jobs and the service.

:func:`open_service` opens the order path the way every order command does
(``execution.runtime.open_order_path``: the profile and transport refusal first, the paper guard,
the account guard) with the service's alert hooks, the venues its jobs are anchored to and IBKR's
trading schedule as the calendars' cross-check, opens the HTTP client the sources share, and
builds the :class:`Service` over them. :func:`build_service` does the building over a path that
is already open; the tests drive it over the fake broker. Without an HTTP client the service runs
no source and reads no halt feed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from ..config.schema import ObliskConfig
from ..data.events.earnings import EarningsSource, yfinance_earnings
from ..data.halts import HaltFeed
from ..data.market.daily import MarketCollector
from ..data.market.targets import collection_venues
from ..data.runner import HEARTBEAT_SOURCES, SourceRunner, client_for
from ..decision.core_vehicles import CoreVehicleChooser, CorrelationSource
from ..decision.rebalance_core import CoreRebalancer
from ..decision.swap_partners import SwapPartners
from ..decision.vehicle_selector import VehicleSelector
from ..env import Environment, secret_reader
from ..execution.runtime import OrderPath, calendar_venues, open_order_path
from ..ibkr.session import BrokerSession
from ..ibkr.transport import Transport
from ..ibkr.transport.gateway_login import LoginDriver
from ..jurisdiction import ELIGIBILITY_POLICIES
from ..jurisdiction.profile import JurisdictionProfile
from ..llm.extractor import EventExtractor
from ..quant.costs import CostBook
from ..reporting.alerts import AlertBook
from ..reporting.notifier import LogNotifier, Notifier, TelegramNotifier
from ..reporting.reporter import Reporter
from ..util.http import RetryPolicy
from .clock import Clock
from .correlations import ResearchCorrelationSource
from .jobs import Jobs
from .nav import NavCollector
from .parts import CapitalParts, build_capital
from .schedule import CollectPlan, SourceMoment, SourcePlan
from .service import AlertHooks, Service


def collect_plan(settings: ObliskConfig) -> CollectPlan:
    """Where and when the service collects market data (``collectors.schedule``)."""
    venues = collection_venues(settings)
    schedule = settings.collectors.schedule
    return CollectPlan(
        venues=venues,
        after_close_minutes=schedule.after_close_minutes,
        fallback_venue=schedule.fallback_venue,
        other_venues=tuple(sorted(set(settings.venues) - {*venues, schedule.fallback_venue})),
    )


def source_plan(settings: ObliskConfig) -> SourcePlan:
    """Each built source's moments (``sources.providers.<id>.schedule``), named ``<id>.<n>``."""
    return SourcePlan(
        tuple(
            SourceMoment(f"{name}.{n}", name, at.venue, at.anchor, at.offset_minutes)
            for name, provider in settings.sources.built().items()
            if name not in HEARTBEAT_SOURCES
            for n, at in enumerate(provider.schedule, start=1)
        )
    )


def anchor_venues(settings: ObliskConfig) -> tuple[str, ...]:
    """The venues the service's decision cycles and digest are anchored to."""
    runtime = settings.runtime
    return tuple(sorted({cycle.venue for cycle in runtime.cycles} | {runtime.digest.venue}))


def calendar_extras(settings: ObliskConfig) -> tuple[str, ...]:
    """The venues the service's calendars cover beyond the order path's: the jobs' anchors and,
    when the service collects, every venue it collects on."""
    extras = set(anchor_venues(settings))
    extras |= set(source_plan(settings).venues)
    extras |= set(settings.sources.halts.nasdaq_trader.markets.values())
    extras.add(settings.sources.halts.nasdaq_trader.time_venue)
    if settings.collectors.schedule.enabled:
        plan = collect_plan(settings)
        extras |= {plan.fallback_venue, *plan.venues}
    return tuple(sorted(extras))


def build_notifier(settings: ObliskConfig, env: Environment) -> Notifier:
    """Telegram when its token and chat are set; the log otherwise."""
    if env.telegram_bot_token is not None and env.telegram_chat_id is not None:
        return TelegramNotifier(
            settings.reporting,
            token=env.telegram_bot_token,
            chat_id=env.telegram_chat_id,
        )
    return LogNotifier()


def build_reporter(settings: ObliskConfig, notifier: Notifier, data_dir: Path) -> Reporter:
    return Reporter(notifier, AlertBook(data_dir / settings.paths.alert_state))


def session_state(session: BrokerSession | None) -> str:
    """The broker session in words, for the heartbeat file, the digest and the alerts."""
    if session is None:
        return "not open"
    wait = session.lost_for
    if wait is not None:
        return f"lost; the next login is allowed in {wait:.0f} s"
    return "authenticated" if session.authenticated else "lost (not authenticated)"


def _no_secret(_name: str) -> str | None:
    return None


def build_rebalancer(
    settings: ObliskConfig,
    path: OrderPath,
    parts: CapitalParts,
    *,
    now: Callable[[], datetime],
    correlations: CorrelationSource | None = None,
) -> CoreRebalancer:
    """The core's rebalancer over an open order path: the variant ``core.variant`` names, its
    roles' vehicles chosen by the vehicle selector under the profile's policies."""
    policy = parts.tax.policy
    selector = VehicleSelector(
        settings, ELIGIBILITY_POLICIES.resolve(path.profile, settings), policy
    )
    return CoreRebalancer(
        settings,
        capital=parts.capital,
        directory=path.directory,
        calendars=path.calendars,
        worker=path.worker,
        maker=parts.maker,
        costs=CostBook.from_config(settings),
        kill=path.kill,
        tax=parts.tax,
        chooser=CoreVehicleChooser(settings, selector, SwapPartners(settings, policy)),
        value=parts.value,
        correlations=correlations,
        now=now,
    )


def build_service(
    settings: ObliskConfig,
    *,
    path: OrderPath,
    reporter: Reporter,
    hooks: AlertHooks,
    clock: Clock,
    ping_url: SecretStr | None = None,
    http: httpx.AsyncClient | None = None,
    secrets: Callable[[str], str | None] | None = None,
    earnings: EarningsSource = yfinance_earnings,
    correlations: CorrelationSource | None = None,
) -> Service:
    """The service over an open order path; with ``http``, it runs the sources and reads the
    halt feed (``secrets`` resolves the credentials sources name); ``correlations`` hands the
    core's vehicle selector its research correlations."""
    parts = build_capital(settings, path, now=clock.now)
    rebalancer = build_rebalancer(settings, path, parts, now=clock.now, correlations=correlations)
    venues = sorted({*calendar_venues(settings, path.directory), *anchor_venues(settings)})
    jobs = Jobs(
        settings,
        path=path,
        rebalancer=rebalancer,
        capital=parts,
        nav=NavCollector(settings, reader=path.reader, worker=path.worker, now=clock.now),
        reporter=reporter,
        data_dir=path.data_dir,
        venues=venues,
        session_state=lambda: session_state(path.session),
        now=clock.now,
        ping_url=ping_url,
    )
    collector = (
        MarketCollector(
            settings,
            client=path.client,
            worker=path.worker,
            calendars=path.calendars,
            now=clock.now,
            sleep=path.client.session.clock.sleep,
        )
        if settings.collectors.schedule.enabled
        else None
    )
    sleep = path.client.session.clock.sleep
    sources = (
        SourceRunner(
            settings,
            client=http,
            worker=path.worker,
            secrets=secrets or _no_secret,
            now=clock.now,
            sleep=sleep,
            earnings=earnings,
        )
        if http is not None
        else None
    )
    extractor = (
        EventExtractor(
            settings,
            client=http,
            worker=path.worker,
            key=(secrets or _no_secret)(settings.llm.extractor_provider().key_env),
            now=clock.now,
        )
        if http is not None
        else None
    )
    halts = (
        HaltFeed(
            settings,
            calendars=path.calendars,
            client=http,
            policy=RetryPolicy.from_settings(settings.http),
            worker=path.worker,
            now=clock.now,
        )
        if http is not None
        else None
    )
    return Service(
        settings,
        path=path,
        jobs=jobs,
        rebalancer=rebalancer,
        reporter=reporter,
        hooks=hooks,
        clock=clock,
        collector=collector,
        collect=collect_plan(settings),
        sources=sources,
        source_plan=source_plan(settings),
        halts=halts,
        extractor=extractor,
    )


@asynccontextmanager
async def open_service(
    env: Environment,
    settings: ObliskConfig,
    profile: JurisdictionProfile,
    *,
    data_dir: Path,
    clock: Clock,
    reporter: Reporter,
    transport: Transport | None = None,
    login_driver: LoginDriver | None = None,
) -> AsyncIterator[Service]:
    """The service over a freshly opened order path; the path closes when the block ends."""
    hooks = AlertHooks(reporter, now=clock.now)
    async with (
        open_order_path(
            env,
            settings,
            profile,
            data_dir=data_dir,
            transport=transport,
            login_driver=login_driver,
            alert=hooks.order,
            session_alert=hooks.session,
            calendar_alert=hooks.calendar,
            extra_venues=calendar_extras(settings),
            cross_check=True,
        ) as path,
        client_for(settings) as http,
    ):
        correlations = ResearchCorrelationSource(settings, data_dir)
        try:
            yield build_service(
                settings,
                path=path,
                reporter=reporter,
                hooks=hooks,
                clock=clock,
                ping_url=env.health_ping_url,
                http=http,
                secrets=secret_reader(env),
                correlations=correlations,
            )
        finally:
            await correlations.aclose()
