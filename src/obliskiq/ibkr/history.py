"""Historical bars: one request, or a probe-chunked walk that never trusts the count asked for.

Ported from ObliskIQ-lite-MCP's backfill (``collect/backfill.py``), minus its store watermark,
which arrives with the collectors (Session 8). What it carries over, all measured in lite:

* IBKR serves a span it derives from the request, not the bars asked for: 400 calendar days of
  daily bars came back as 257 bars [CP ✓ 2026-09-10]. So the per-call capacity is PROBED: ask for
  ``probe_request_days`` and size the chunk from the bars that actually came back, converted to
  calendar days. The probed chunk is remembered for the life of the reader.
* The backward walk judges progress by the data: it stops when a chunk's earliest bar did not move
  earlier, when a chunk is empty, or at ``max_chunks`` (a runaway guard). It never assumes
  ``len(bars) == requested`` and never infers a gap from a count.
* A refused span (``HTTP 500`` with a no-data body) is asked again for half of it, down to
  ``span_stepdown_min_days``: BOVA11 refused 730 and 365 days and served 181 bars for 182
  (lite, 2026-09-15). During the backward walk a refusal is the same fact as an empty chunk, served
  differently: it stops the walk and keeps what was collected.
* The exchange is the caller's: some venues refuse history with their own exchange on the request
  and serve it with the exchange omitted (TSEJ, BM, SBF, EBS in lite) — ``history_exchange``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date, datetime, time, timedelta

from ..config.schema_ibkr import IbkrConfig
from ..logging_setup import get_logger
from .decode import decode_history
from .errors import NoDataPermanentError
from .models import History, HistoryBar
from .session import BrokerSession

log = get_logger("ibkr.history")

HISTORY_PATH = "iserver/marketdata/history"

#: IBKR's daily bar size, and its startTime form: the date, then the end of that day so the bar
#: for the date is included.
DAILY_BAR = "1d"
START_TIME_DATE_FORMAT = "%Y%m%d"
START_TIME_SUFFIX = time.max.strftime("%H:%M:%S")


def start_time(day: date) -> str:
    return f"{day.strftime(START_TIME_DATE_FORMAT)}-{START_TIME_SUFFIX}"


def days_period(days: int) -> str:
    return f"{max(int(days), 1)}d"


class HistoryReader:
    """``/iserver/marketdata/history``, single requests and the chunked daily walk."""

    def __init__(
        self, session: BrokerSession, ibkr: IbkrConfig, *, calendar_days_per_year: float
    ) -> None:
        self._session = session
        self._ibkr = ibkr
        self._calendar_days_per_year = calendar_days_per_year
        self._chunk_days: int | None = None

    async def bars(
        self,
        conid: int,
        *,
        period: str,
        bar: str,
        exchange: str | None = None,
        outside_rth: bool = False,
        start: str | None = None,
    ) -> History:
        """One request. ``exchange`` ``None`` sends no exchange at all."""
        body = await self._session.request(
            "GET",
            HISTORY_PATH,
            params={
                "conid": int(conid),
                "period": period,
                "bar": bar,
                "exchange": exchange,
                "outsideRth": "true" if outside_rth else "false",
                "startTime": start,
            },
        )
        return decode_history(
            conid, body, delayed_bound_seconds=self._ibkr.marketdata.delayed_quote_bound_seconds
        )

    async def _stepping_down(
        self, conid: int, span_days: int, *, exchange: str | None, start: str | None
    ) -> History:
        """Ask for the span; if refused, ask for half of it, down to the configured floor."""
        span = max(int(span_days), 1)
        floor = self._ibkr.history.span_stepdown_min_days
        while True:
            try:
                return await self.bars(
                    conid, period=days_period(span), bar=DAILY_BAR, exchange=exchange, start=start
                )
            except NoDataPermanentError:
                following = span // 2
                if following < floor:
                    raise
                log.info(
                    "history refused at this span; asking for half of it",
                    extra={"conid": conid, "refused_days": span, "retry_days": following},
                )
                span = following

    async def chunk_days(self, conid: int, *, exchange: str | None) -> int:
        """The calendar span one daily request reliably covers, probed once per reader."""
        if self._chunk_days is not None:
            return self._chunk_days
        history = self._ibkr.history
        try:
            probe = await self.bars(
                conid,
                period=days_period(history.probe_request_days),
                bar=DAILY_BAR,
                exchange=exchange,
            )
        except NoDataPermanentError:
            log.warning("the daily cap probe was refused; using the configured chunk")
            return history.chunk_days
        if not probe.bars:
            log.warning("the daily cap probe returned nothing; using the configured chunk")
            return history.chunk_days
        as_calendar_days = round(
            len(probe.bars) * (self._calendar_days_per_year / history.sessions_per_year)
        )
        self._chunk_days = max(min(as_calendar_days, history.chunk_days), 1)
        log.info(
            "probed the daily bar capacity",
            extra={
                "requested_days": history.probe_request_days,
                "bars_returned": len(probe.bars),
                "chunk_days": self._chunk_days,
            },
        )
        return self._chunk_days

    async def daily_between(
        self, conid: int, *, earliest: date, latest: date, exchange: str | None = None
    ) -> History:
        """Daily bars from ``earliest`` to ``latest``: the recent window, then a backward walk.

        The result holds what IBKR served inside ``[earliest, latest]``, deduplicated and sorted;
        it may start later than ``earliest`` (the instrument's history is shorter, or the walk
        stopped making progress). The first request asks for the whole window ending at
        ``latest`` (a route may serve it all; a refusal steps down); the walk covers the rest.
        """
        recent = await self._stepping_down(
            conid, (latest - earliest).days, exchange=exchange, start=start_time(latest)
        )
        collected: dict[datetime, HistoryBar] = {bar.start: bar for bar in recent.bars}
        chunk = await self.chunk_days(conid, exchange=exchange) if collected else 0
        known = min((bar.start.date() for bar in collected.values()), default=None)
        cursor = known
        for _ in range(self._ibkr.history.max_chunks if collected else 0):
            if cursor is None or cursor <= earliest:
                break
            try:
                part = await self._stepping_down(
                    conid, chunk, exchange=exchange, start=start_time(cursor)
                )
            except NoDataPermanentError:
                log.info("history refused beyond this point; the backward walk stops")
                break
            if not part.bars:
                break
            reached = min(bar.start.date() for bar in part.bars)
            collected.update({bar.start: bar for bar in part.bars})
            if reached >= cursor:
                log.info("the backward walk stopped making progress")
                break
            cursor = reached - timedelta(days=1)
        # VERIFY-LIVE(daily bar t stamp per venue): the window is filtered on the UTC date of the
        # bar start; a venue stamping its daily bar before UTC midnight would shift by a day.
        bars = tuple(
            sorted(
                (bar for bar in collected.values() if earliest <= bar.start.date() <= latest),
                key=lambda bar: bar.start,
            )
        )
        return History(
            conid=conid,
            bars=bars,
            status_flag=recent.status_flag,
            market_data_status=recent.market_data_status,
        )
