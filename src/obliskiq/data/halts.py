"""The single-stock halt feed, read into the calendars every few minutes.

Each refresh reads the Nasdaq Trader trade-halts feed, keeps the halts of the universe's
instruments on the venues ``halts.nasdaq_trader.markets`` maps (and the calendars cover), and
replaces the feed's incidents in the calendars wholesale; a halt whose resumption plus the
incident grace has passed is dropped. The state accepted is kept in the store, so a restart
during a feed outage restores it.

A feed that fails keeps its last state in force (a halt is never lifted because the feed went
quiet) and counts the failure; ``alert_after_failures`` in a row raise an alert, which clears on
the next good read.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from pydantic import TypeAdapter, ValidationError

from ..calendars.halt_feeds import (
    HaltRead,
    instrument_key,
    nasdaq_trader_incidents,
    structured_items,
)
from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..domain.calendar import Incident
from ..errors import UpstreamError
from ..logging_setup import get_logger
from ..store import sources as source_repo
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy, get_with_retry
from .market.plumbing import failure_text
from .market.targets import venue_of

log = get_logger("data.halts")

SOURCE = "nasdaq_trader"
HTTP_OK = 200
_INCIDENTS = TypeAdapter(tuple[Incident, ...])


@dataclass(frozen=True, slots=True)
class HaltRefresh:
    """One refresh: the incidents in force after it, and why it failed if it did."""

    ok: bool
    incidents: int
    failures: int
    alert: bool
    error: str | None = None
    read: HaltRead | None = None


def followed_conids(settings: ObliskConfig, venues: set[str]) -> dict[tuple[str, str], int]:
    """(venue, instrument key) -> conid of the universe's pinned instruments on ``venues``."""
    found: dict[tuple[str, str], int] = {}
    for instrument in settings.universe.instruments:
        venue = venue_of(settings, instrument.listing_exchange)
        if instrument.conid is not None and venue in venues:
            found[(venue, instrument_key(instrument.symbol))] = instrument.conid
    return found


class HaltFeed:
    """Reads the halt feed into the calendars, restores the last state, counts failures."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        calendars: Calendars,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        now: Callable[[], datetime],
    ) -> None:
        self._config = settings.sources.halts.nasdaq_trader
        self._grace = timedelta(minutes=settings.calendars.incidents.grace_minutes)
        self._calendars = calendars
        self._client = client
        self._policy = policy
        self._worker = worker
        self._now = now
        covered = {venue.mic for venue in calendars.venues()}
        self._markets = {code: mic for code, mic in self._config.markets.items() if mic in covered}
        self._conids = followed_conids(settings, set(self._markets.values()))
        self._zone = ZoneInfo(calendars.venue(self._config.time_venue).timezone)
        self._failures = 0
        self._last_attempt: datetime | None = None

    @property
    def origin(self) -> str:
        return self._config.origin

    def due(self, now: datetime) -> bool:
        """Whether ``refresh_minutes`` have passed since the last attempt."""
        every = timedelta(minutes=self._config.refresh_minutes)
        return self._last_attempt is None or now - self._last_attempt >= every

    async def restore(self) -> int:
        """Put the last accepted state back into the calendars; how many incidents it held. A
        stored state that cannot be read restores nothing and is logged."""
        held = await self._worker.run(source_repo.halt_state, self.origin)
        if held is None:
            return 0
        fetched_at, payload = held
        try:
            incidents = _INCIDENTS.validate_json(payload)
            self._calendars.accept_incidents(self.origin, self._current(incidents, self._now()))
        except (ValidationError, ValueError) as exc:
            log.error(
                "the halt feed's stored state cannot be restored",
                extra={"error": str(exc), "fetched_at": fetched_at.isoformat()},
            )
            return 0
        return len(incidents)

    async def refresh(self) -> HaltRefresh:
        """Read the feed once; on success replace its incidents, else keep the last state."""
        now = self._now()
        self._last_attempt = now
        try:
            response = await get_with_retry(self._client, self._config.url, policy=self._policy)
            if response.status_code != HTTP_OK:
                raise UpstreamError(f"the halt feed answered HTTP {response.status_code}")
            read = nasdaq_trader_incidents(
                structured_items(response.content),
                origin=self.origin,
                markets=self._markets,
                zone=self._zone,
                conids=self._conids,
            )
            current = self._current(read.incidents, now)
            self._calendars.accept_incidents(self.origin, current)
        except Exception as exc:  # the last state stays in force
            self._failures += 1
            error = failure_text(exc)
            log.warning(
                "the halt feed failed; its last state stays in force",
                extra={"failures": self._failures, "error": error},
            )
            return HaltRefresh(
                ok=False,
                incidents=self._held(),
                failures=self._failures,
                alert=self._failures >= self._config.alert_after_failures,
                error=error,
            )
        self._failures = 0
        await self._worker.run(
            source_repo.save_halt_state,
            self.origin,
            fetched_at=now,
            payload=json.dumps([incident.model_dump(mode="json") for incident in current]),
        )
        return HaltRefresh(ok=True, incidents=len(current), failures=0, alert=False, read=read)

    def _current(self, incidents: tuple[Incident, ...], now: datetime) -> tuple[Incident, ...]:
        """The halts still in force (or within the grace after their resumption)."""
        return tuple(
            i for i in incidents if i.expected_end is None or i.expected_end + self._grace >= now
        )

    def _held(self) -> int:
        return sum(1 for i in self._calendars.incident_book.incidents if i.origin == self.origin)
