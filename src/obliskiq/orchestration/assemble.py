"""The service assembled: the reporter, the alert hooks, the order path, the jobs and the service.

:func:`open_service` opens the order path the way every order command does
(``execution.runtime.open_order_path``: the profile and transport refusal first, the paper guard,
the account guard) with the service's alert hooks, the venues its jobs are anchored to and IBKR's
trading schedule as the calendars' cross-check, and builds the :class:`Service` over it.
:func:`build_service` does the building over a path that is already open; the tests drive it over
the fake broker.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import SecretStr

from ..config.schema import ObliskConfig
from ..decision.rebalance_basic import BasicRebalancer
from ..env import Environment
from ..execution.runtime import OrderPath, calendar_venues, open_order_path
from ..ibkr.session import BrokerSession
from ..ibkr.transport import Transport
from ..ibkr.transport.gateway_login import LoginDriver
from ..jurisdiction.profile import JurisdictionProfile
from ..quant.costs import CostBook
from ..reporting.alerts import AlertBook
from ..reporting.notifier import LogNotifier, Notifier, TelegramNotifier
from ..reporting.reporter import Reporter
from .clock import Clock
from .jobs import Jobs
from .nav import NavCollector
from .service import AlertHooks, Service


def anchor_venues(settings: ObliskConfig) -> tuple[str, ...]:
    """The venues the service's jobs are anchored to."""
    runtime = settings.runtime
    return tuple(sorted({cycle.venue for cycle in runtime.cycles} | {runtime.digest.venue}))


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


def build_service(
    settings: ObliskConfig,
    *,
    path: OrderPath,
    reporter: Reporter,
    hooks: AlertHooks,
    clock: Clock,
    ping_url: SecretStr | None = None,
) -> Service:
    """The service over an open order path."""
    rebalancer = BasicRebalancer(
        settings,
        reader=path.reader,
        contracts=path.client.contracts,
        directory=path.directory,
        calendars=path.calendars,
        worker=path.worker,
        book=path.book,
        pricer=path.pricer,
        costs=CostBook.from_config(settings),
        kill=path.kill,
        now=clock.now,
    )
    venues = sorted({*calendar_venues(settings, path.directory), *anchor_venues(settings)})
    jobs = Jobs(
        settings,
        path=path,
        rebalancer=rebalancer,
        nav=NavCollector(settings, reader=path.reader, worker=path.worker, now=clock.now),
        reporter=reporter,
        data_dir=path.data_dir,
        venues=venues,
        session_state=lambda: session_state(path.session),
        now=clock.now,
        ping_url=ping_url,
    )
    return Service(
        settings,
        path=path,
        jobs=jobs,
        rebalancer=rebalancer,
        reporter=reporter,
        hooks=hooks,
        clock=clock,
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
    async with open_order_path(
        env,
        settings,
        profile,
        data_dir=data_dir,
        transport=transport,
        login_driver=login_driver,
        alert=hooks.order,
        session_alert=hooks.session,
        calendar_alert=hooks.calendar,
        extra_venues=anchor_venues(settings),
        cross_check=True,
    ) as path:
        yield build_service(
            settings,
            path=path,
            reporter=reporter,
            hooks=hooks,
            clock=clock,
            ping_url=env.health_ping_url,
        )
