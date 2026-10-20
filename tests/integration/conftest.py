"""Fixtures of the service's integration tests: the service over the fake broker.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx
from fake_broker import FakeBroker
from order_rig import build_rig
from service_rig import SERVICE_VENUES, ServiceRig, build_service_rig, skeleton_market

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig


@pytest.fixture(scope="module")
def service_calendars(shipped_settings: ObliskConfig) -> Calendars:
    return Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=SERVICE_VENUES
    )


@pytest.fixture
async def make(
    make_session: Callable[..., Any],
    shipped_settings: ObliskConfig,
    service_calendars: Calendars,
    tmp_path: Path,
    repo_root: Path,
) -> AsyncIterator[Callable[..., Any]]:
    """Builds a service rig over a fresh fake broker; ``paper`` upgrades the mode first."""
    opened: list[Any] = []
    with respx.mock(assert_all_called=False) as router:
        broker = FakeBroker(router)

        async def build(
            *, paper: bool = False, settings: ObliskConfig | None = None, **kw: Any
        ) -> ServiceRig:
            chosen = settings if settings is not None else shipped_settings
            if not opened:
                rig = await build_rig(
                    settings=chosen,
                    session=make_session(),
                    broker=broker,
                    tmp_path=tmp_path,
                    calendars=service_calendars,
                )
                opened.append(rig)
                skeleton_market(broker)
                if paper:
                    await rig.to_paper()
            rig = opened[0]
            return build_service_rig(rig, chosen, service_calendars, repo_root, **kw)

        yield build
        for rig in opened:
            await rig.aclose()
