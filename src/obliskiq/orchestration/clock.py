"""The service's clock: the wall time it schedules by, and waiting until a moment.

Injectable, so a test runs days of the service in milliseconds: a fake clock jumps to the moment
asked for. Every instant is a timezone-aware UTC datetime; local times come from the calendars.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import contextlib
from datetime import datetime
from typing import Protocol

from ..util.timeutil import utc_now


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep_until(self, moment: datetime, stop: asyncio.Event) -> None:
        """Return at ``moment``, or as soon as ``stop`` is set."""
        ...


class SystemClock:
    """The machine's UTC wall clock."""

    def now(self) -> datetime:
        return utc_now()

    async def sleep_until(self, moment: datetime, stop: asyncio.Event) -> None:
        delay = (moment - utc_now()).total_seconds()
        if delay <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
