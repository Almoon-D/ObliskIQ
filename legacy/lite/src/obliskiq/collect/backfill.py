"""Historical bars: one-off backfill, then incremental updates only.

Unlike the chain and curve collectors, bars **are** retrievable retrospectively,
so a lagging backfill is recoverable. It still belongs here: the grid cannot
compute a percentile, a realised vol or a correlation without it, and
``chain_collector`` cannot size a strike band without daily bars either — which
is why the scheduler runs this first.

Never re-fetching held history
------------------------------

``bar_watermarks`` holds the earliest and latest bar per ``(contract_id, freq)``.
A run extends the series at both ends and stops; it never re-requests the middle.
Gaps are tolerated by construction — the watermark records what is held, not an
assumption that it is unbroken — so a day the machine or the API missed costs one
day, not a restart.

Probing the daily cap
---------------------

Weekly bars come back whole: ``step=ONE_WEEK, step_count=260`` returns 5+ years in
one call, confirmed. Daily needs chunking, and the per-call cap is **measured**
rather than assumed: ``ibkr.cp_api.history.max_bars_per_request`` is documented
and unverified. The first daily request asks for
``collectors.backfill.probe_request_days`` and the bar count that comes back sets
the chunk size, which is then remembered in ``collector_state``.

Chunk progress is judged by whether the earliest bar actually moved earlier, not
by trusting a request parameter. That makes the loop correct whichever way the
endpoint interprets ``startTime`` — an ambiguity no live run has settled yet.

.. warning::
   UNVERIFIED. Bar-window semantics need live confirmation.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import Config
from ..errors import ERROR_EXCERPT_CHARS, PermanentUpstreamError
from ..logging_setup import get_logger
from ..store import Store
from ..util.timeutil import parse_date, utc_stamp, utc_today
from .retry import TargetRetryPolicy, pace, with_target_retry
from .runlog import CollectorRun
from .source import Bar, MarketData
from .state import KEY_DAILY_BAR_CAP, get_state, set_state
from .universe import assert_seeded
from .vol import FREQ_DAILY, FREQ_MONTHLY, FREQ_WEEKLY

log = get_logger("collect.backfill")

COLLECTOR_NAME = "backfill"

#: IBKR bar-size codes, keyed by the ``bars.freq`` label they are stored under.
BAR_SIZE = {FREQ_DAILY: "1d", FREQ_WEEKLY: "1w", FREQ_MONTHLY: "1m"}

#: The date part of IBKR's startTime form.
START_TIME_DATE_FORMAT = "%Y%m%d"

#: The instant of day a startTime refers to. End of day, so the bar for that date
#: is included rather than sliced off by an hours-ago boundary.
START_TIME_SUFFIX = "23:59:59"


@dataclass(frozen=True)
class BarTarget:
    contract_id: int
    symbol: str
    #: The exchange a HISTORY request sends, from ``universe.history_exchange``,
    #: which defaults to the listing venue and is not always the same thing: a
    #: TSEJ-listed share serves no history with exchange=TSEJ and full history
    #: with the exchange omitted. None means send none.
    exchange: str | None
    asset_class: str
    sector: str | None


@dataclass(frozen=True)
class Watermark:
    """What is already held for one ``(contract_id, freq)``."""

    earliest: date | None
    latest: date | None

    @property
    def is_empty(self) -> bool:
        return self.earliest is None or self.latest is None


def bar_targets(store: Store, *, only: Sequence[str] | None = None) -> list[BarTarget]:
    rows = store.execute(
        "SELECT contract_id, symbol, history_exchange, asset_class, sector FROM universe "
        "WHERE collect_bars = 1 ORDER BY symbol"
    ).fetchall()
    targets = [
        BarTarget(
            contract_id=int(row["contract_id"]),
            symbol=str(row["symbol"]),
            exchange=row["history_exchange"],
            asset_class=str(row["asset_class"]),
            sector=row["sector"],
        )
        for row in rows
    ]
    if only:
        wanted = {s.upper() for s in only}
        targets = [t for t in targets if t.symbol.upper() in wanted]
    return targets


def read_watermark(store: Store, contract_id: int, freq: str) -> Watermark:
    row = store.execute(
        "SELECT earliest_ts, latest_ts FROM bar_watermarks WHERE contract_id = ? AND freq = ?",
        (contract_id, freq),
    ).fetchone()
    if row is None:
        return Watermark(earliest=None, latest=None)
    return Watermark(earliest=parse_date(row["earliest_ts"]), latest=parse_date(row["latest_ts"]))


class Backfill:
    """Extends the bar series for every tracked contract, at every tracked frequency."""

    def __init__(self, store: Store, config: Config, market_data: MarketData) -> None:
        self._store = store
        self._config = config
        self._market_data = market_data
        self._policy = TargetRetryPolicy.from_config(config)

        data = config.section("data")
        self._daily_years = data.require_int("daily_window_years")
        self._weekly_years = data.require_int("weekly_window_years")
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")
        self._trading_days_per_year = data.require_float("trading_days_per_year")
        self._monthly_years = config.require_int("commodities.history.monthly_window_years")

        backfill = config.section("collectors.backfill")
        self._probe_enabled = backfill.require_bool("probe_daily_cap")
        self._probe_request_days = backfill.require_int("probe_request_days")
        self._configured_chunk_days = backfill.require_int("daily_chunk_days")
        self._max_chunks = backfill.require_int("max_chunks_per_contract")
        self._overlap_days = backfill.require_int("watermark_overlap_days")
        self._stepdown_min_days = backfill.require_int("span_stepdown_min_days")
        self._monthly_roots_only = backfill.require_bool("monthly_roots_only")
        self._commodity_buckets = {
            str(bucket) for bucket in config.require_list("commodities.buckets")
        }

    # ── entry point ──

    async def run(
        self,
        run: CollectorRun,
        *,
        only: Sequence[str] | None = None,
        frequencies: Sequence[str] | None = None,
        dry_run: bool = False,
        today: date | None = None,
    ) -> int:
        assert_seeded(self._store, self._config)
        session = today or utc_today()
        wanted = list(frequencies) if frequencies else [FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY]
        written = 0

        for target in bar_targets(self._store, only=only):
            for freq in wanted:
                if not self._applies(target, freq):
                    continue
                label = f"{target.symbol}:{freq}"
                try:
                    rows = await with_target_retry(
                        self._policy,
                        label,
                        lambda t=target, f=freq: self._collect(t, f, session, dry_run=dry_run),
                    )
                except Exception as exc:
                    run.failed(label, repr(exc))
                    continue
                written += rows
                mark = read_watermark(self._store, target.contract_id, freq)
                run.succeeded(
                    label,
                    rows,
                    earliest=mark.earliest.isoformat() if mark.earliest else None,
                    latest=mark.latest.isoformat() if mark.latest else None,
                )
                await pace(self._policy)
        return written

    def _applies(self, target: BarTarget, freq: str) -> bool:
        """Monthly history is for the commodity complex, not for every contract.

        Twenty years of monthly bars exists to place a commodity in its cycle,
        because a 5-year percentile is misleading on an asset with a 10-20 year
        one. It buys nothing on a single equity.
        """
        if freq != FREQ_MONTHLY or not self._monthly_roots_only:
            return True
        return target.sector in self._commodity_buckets

    def _window_years(self, freq: str) -> int:
        return {
            FREQ_DAILY: self._daily_years,
            FREQ_WEEKLY: self._weekly_years,
            FREQ_MONTHLY: self._monthly_years,
        }[freq]

    # ── one (contract, freq) ──

    async def _collect(self, target: BarTarget, freq: str, session: date, *, dry_run: bool) -> int:
        mark = read_watermark(self._store, target.contract_id, freq)
        target_earliest = session - timedelta(
            days=round(self._window_years(freq) * self._calendar_days_per_year)
        )

        collected: dict[str, Bar] = {}

        # 1. Bring the recent end up to date. On an empty series this asks for the
        #    whole window in one call, which is exactly right for weekly.
        recent = await self._fetch_recent(target, freq, mark, session, target_earliest)
        collected.update({bar.ts: bar for bar in recent})

        # 2. Extend backwards only if the held series does not reach far enough.
        #    A series already covering the window issues no historical request at
        #    all — that is what "never re-fetch history already held" means.
        if freq == FREQ_DAILY:
            history = await self._extend_backwards(
                target, freq, mark, target_earliest, collected, session
            )
            collected.update({bar.ts: bar for bar in history})

        rows = sorted(collected.values(), key=lambda bar: bar.ts)
        if dry_run:
            return len(rows)
        written = self._write(target.contract_id, freq, rows)
        self._update_watermark(target.contract_id, freq)
        return written

    async def _fetch_recent(
        self,
        target: BarTarget,
        freq: str,
        mark: Watermark,
        session: date,
        target_earliest: date,
    ) -> list[Bar]:
        if mark.latest is None:
            span_days = (session - target_earliest).days
        else:
            # Overlap deliberately: the last stored bar may have been captured
            # mid-session and needs correcting. The primary key makes the overlap
            # free, and re-requesting a handful of days is not re-fetching history.
            span_days = max((session - mark.latest).days + self._overlap_days, self._overlap_days)
        return await self._bars_stepping_down(target, freq, span_days)

    async def _bars_stepping_down(
        self,
        target: BarTarget,
        freq: str,
        span_days: int,
        *,
        start_time: str | None = None,
    ) -> list[Bar]:
        """Ask for the window; if the route refuses it, ask for less.

        **An instrument with less history than the configured window used to
        collect NOTHING.** `_collect` opens by asking for the whole window in one
        call, and `iserver/marketdata/history` answers a refusal rather than a
        short series, so the whole target failed and the run reported `partial`
        every night. Measured 2026-09-15 on `cp_api`: `BOVA11`, which measures
        brazil/broad, refuses `730d` and `365d` and serves 181 bars for `182d` —
        with the exchange on the request and with it omitted alike, so this is
        NOT the venue inversion the failure was first read as. SPY, STW, BARN and
        LISN all serve the full window on the same run, so it is the instrument's
        own history and not a route-wide cap.

        Halving is the search: it needs no per-instrument declaration, it costs
        one extra call per halving and only on a target that would otherwise have
        collected nothing, and it stops at
        ``collectors.backfill.span_stepdown_min_days`` rather than walking down to
        a single session. A refusal at the floor is still a failure and still
        raises — this widens what can be collected, it does not hide a target
        that cannot be.
        """
        span = max(int(span_days), 1)
        attempted: list[int] = []
        while True:
            attempted.append(span)
            try:
                return await self._market_data.bars(
                    target.contract_id,
                    bar=BAR_SIZE[freq],
                    period=self._period_for(freq, span),
                    exchange=target.exchange,
                    start_time=start_time,
                )
            except PermanentUpstreamError:
                nxt = span // 2
                if nxt < self._stepdown_min_days:
                    raise
                span = nxt
                log.info(
                    "history refused at this span; asking for half of it",
                    extra={
                        "symbol": target.symbol,
                        "freq": freq,
                        "refused_days": attempted[-1],
                        "retry_days": span,
                    },
                )
                await pace(self._policy)

    async def _extend_backwards(
        self,
        target: BarTarget,
        freq: str,
        mark: Watermark,
        target_earliest: date,
        collected: dict[str, Bar],
        session: date,
    ) -> list[Bar]:
        """Walk backwards in chunks until the window is covered or progress stops."""
        known_earliest = mark.earliest
        for bar in collected.values():
            parsed = parse_date(bar.ts)
            if parsed is not None and (known_earliest is None or parsed < known_earliest):
                known_earliest = parsed
        if known_earliest is not None and known_earliest <= target_earliest:
            return []

        chunk_days = await self._daily_chunk_days(target, freq)
        # The session date, not the wall clock: a run told which day it is collecting
        # must not silently walk back from a different one.
        cursor = known_earliest or session
        out: list[Bar] = []

        for _ in range(self._max_chunks):
            if cursor <= target_earliest:
                break
            await pace(self._policy)
            try:
                chunk = await self._bars_stepping_down(
                    target, freq, chunk_days, start_time=_start_time(cursor)
                )
            except PermanentUpstreamError as exc:
                # A REFUSAL HERE IS THE SAME FACT AS AN EMPTY CHUNK, served
                # differently, and it must not fail the target. This walk is
                # speculative by construction: it asks for history that may
                # simply not exist, and `iserver/marketdata/history` answers
                # "before this instrument existed" with HTTP 500 rather than with
                # an empty series. Measured 2026-09-15 on BOVA11, which serves
                # its recent window and then refuses every span at every
                # start_time beyond it — so the recent bars it HAD returned were
                # being thrown away by an exception raised while reaching for
                # bars that were never there.
                log.info(
                    "history refused beyond this point; stopping the backward walk",
                    extra={
                        "symbol": target.symbol,
                        "freq": freq,
                        "cursor": cursor.isoformat(),
                        "detail": str(exc)[:ERROR_EXCERPT_CHARS],
                    },
                )
                break
            dates = [d for d in (parse_date(bar.ts) for bar in chunk) if d is not None]
            if not dates:
                log.info(
                    "no further history returned; stopping the backward walk",
                    extra={"symbol": target.symbol, "freq": freq, "cursor": cursor.isoformat()},
                )
                break
            earliest = min(dates)
            out.extend(chunk)
            # Progress is judged by the data, not by the request. Whichever way the
            # endpoint reads startTime, a chunk that did not reach further back
            # means the walk is done.
            if earliest >= cursor:
                log.info(
                    "backward walk stopped making progress",
                    extra={"symbol": target.symbol, "freq": freq, "earliest": earliest.isoformat()},
                )
                break
            cursor = earliest - timedelta(days=1)
        return out

    # ── the probed daily cap ──

    async def _daily_chunk_days(self, target: BarTarget, freq: str) -> int:
        if freq != FREQ_DAILY or not self._probe_enabled:
            return self._configured_chunk_days
        cached = get_state(self._store, KEY_DAILY_BAR_CAP)
        if isinstance(cached, dict) and cached.get("chunk_days"):
            return int(cached["chunk_days"])
        return await self._probe_daily_cap(target)

    async def _probe_daily_cap(self, target: BarTarget) -> int:
        """Measure how many daily bars one call actually returns.

        Ask for ``probe_request_days`` of calendar span. A full answer contains
        roughly ``span * trading/calendar`` bars; a truncated one contains the cap.
        The chunk size is derived from what came back, so an unverified documented
        figure never sets the pace.
        """
        try:
            bars = await self._market_data.bars(
                target.contract_id,
                bar=BAR_SIZE[FREQ_DAILY],
                period=self._period_for(FREQ_DAILY, self._probe_request_days),
                exchange=target.exchange,
            )
        except PermanentUpstreamError:
            # The probe asks for more than the window on purpose, so the first
            # instrument walked being a short-history one refuses it. That is a
            # fact about THAT instrument and must not decide the chunk for every
            # other target, nor fail the run: fall back exactly as an empty
            # answer does.
            log.warning(
                "daily cap probe was refused; falling back to the configured chunk",
                extra={"symbol": target.symbol, "chunk_days": self._configured_chunk_days},
            )
            return self._configured_chunk_days
        observed = len(bars)
        if observed == 0:
            log.warning(
                "daily cap probe returned nothing; falling back to the configured chunk",
                extra={"symbol": target.symbol, "chunk_days": self._configured_chunk_days},
            )
            return self._configured_chunk_days

        # Bars are trading days; the chunk is expressed in calendar days.
        as_calendar_days = round(
            observed * (self._calendar_days_per_year / self._trading_days_per_year)
        )
        chunk_days = max(min(as_calendar_days, self._configured_chunk_days), 1)
        set_state(
            self._store,
            KEY_DAILY_BAR_CAP,
            {
                "bars": observed,
                "requested_days": self._probe_request_days,
                "chunk_days": chunk_days,
                "probed_with": target.symbol,
                "probed_at": utc_stamp(),
            },
        )
        log.info(
            "probed the daily bar cap",
            extra={
                "symbol": target.symbol,
                "requested_days": self._probe_request_days,
                "bars_returned": observed,
                "chunk_days": chunk_days,
            },
        )
        return chunk_days

    def _period_for(self, freq: str, span_days: int) -> str:
        """IBKR's total-span notation for a request.

        Weekly and monthly are asked for in years, which is how the confirmed
        ``step_count=260`` single-call behaviour is expressed. Daily is asked for
        in days, because that is the axis the chunking works on.
        """
        span = max(int(span_days), 1)
        if freq == FREQ_DAILY:
            return f"{span}d"
        years = max(round(span / self._calendar_days_per_year), 1)
        return f"{years}y"

    # ── writing ──

    def _write(self, contract_id: int, freq: str, bars: list[Bar]) -> int:
        if not bars:
            return 0
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO bars (contract_id, ts, freq, open, high, low, close, volume, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (contract_id, freq, ts) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low,
                    close = excluded.close, volume = excluded.volume, source = excluded.source
                """,
                [
                    (
                        contract_id,
                        bar.ts,
                        freq,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        self._market_data.source,
                    )
                    for bar in bars
                ],
            )
        return len(bars)

    def _update_watermark(self, contract_id: int, freq: str) -> None:
        """Recompute from what is actually stored.

        Derived from the table rather than from the request, so a gap in the middle
        of the series is recorded honestly instead of being papered over by a
        watermark that claims a span nobody holds.
        """
        row = self._store.execute(
            "SELECT MIN(ts) AS earliest, MAX(ts) AS latest FROM bars "
            "WHERE contract_id = ? AND freq = ?",
            (contract_id, freq),
        ).fetchone()
        if row is None or row["earliest"] is None:
            return
        with self._store.transaction() as connection:
            connection.execute(
                "INSERT INTO bar_watermarks (contract_id, freq, earliest_ts, latest_ts, updated_at)"
                " VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (contract_id, freq) DO UPDATE SET "
                "earliest_ts = excluded.earliest_ts, latest_ts = excluded.latest_ts, "
                "updated_at = excluded.updated_at",
                (contract_id, freq, row["earliest"], row["latest"], utc_stamp()),
            )


def _start_time(cursor: date) -> str:
    return f"{cursor.strftime(START_TIME_DATE_FORMAT)}-{START_TIME_SUFFIX}"
