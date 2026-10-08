"""Typed reads over the store.

Every engine above this module reads the database through these functions and
never writes SQL of its own. That is not tidiness: the reads carry the two rules
that decide whether a derived number is trustworthy.

**Macro series align on RELEASE, not on reference.** Aligning a CPI print to the
month it describes means every percentile computed from it knew the number weeks
before the market did. :meth:`MacroSeries.known_at` filters on ``release_date``
and simply excludes observations that carry none, reporting how many it dropped.
A series whose source cannot state a release date is a coverage gap the caller
must report, not a series to align optimistically.

**A short series is not a series.** Readers return what the store holds and say
how much that was; they never pad, interpolate or carry a value forward. The
caller compares the count against ``grid.min_observations`` and writes ``n/a``
with the shortfall in the reason.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..store import Store
from ..util.timeutil import parse_date

#: ``bars.freq`` labels, as the collectors write them.
FREQ_DAILY = "1d"
FREQ_WEEKLY = "1w"
FREQ_MONTHLY = "1mo"

#: ``chain_snapshots.right``.
RIGHT_CALL = "C"
RIGHT_PUT = "P"

#: FRED and the ECB both publish rates and CPI growth in percent.
PERCENT_TO_DECIMAL = 100.0

#: ``YYYY-MM-DD``. The date part of an ISO-8601 stamp — a format fact, not a
#: window somebody might tune.
ISO_DATE_CHARS = 10

#: What a row's ``market_data_status`` amounts to, once classified against
#: ``data.market_data_status_live``. Three states, not two: 006 records that a
#: NULL means "the route said nothing", never "realtime", and the vendor route
#: writes NULL by design — so an unreported status is its own answer rather
#: than either of the other two wearing a disguise.
QUALITY_LIVE = "live"
QUALITY_INDICATIVE = "indicative"
QUALITY_UNREPORTED = "unreported"

#: Worst first. A set of rows is only as good as its weakest member, so a table
#: mixing live and frozen quotes is labelled by the frozen one.
_QUALITY_ORDER = (QUALITY_INDICATIVE, QUALITY_UNREPORTED, QUALITY_LIVE)


def classify_market_data_status(status: str | None, live_values: Sequence[str]) -> str:
    """One row's market-data state, as the label a reader should print.

    ``live_values`` comes from ``data.market_data_status_live``. Matched
    case-insensitively on the trimmed value, because the flag is passed through
    from the route verbatim and unnormalised — which is the right thing for the
    stored column and the wrong thing to compare on.
    """
    if status is None or not str(status).strip():
        return QUALITY_UNREPORTED
    wanted = {str(value).strip().upper() for value in live_values}
    return QUALITY_LIVE if str(status).strip().upper() in wanted else QUALITY_INDICATIVE


def worst_market_data_quality(qualities: Iterable[str]) -> str | None:
    """The label for a table built from many rows. ``None`` for no rows at all.

    SKILL.md: "Never mix frozen and live quotes unmarked." A table holding one
    frozen quote among fifty live ones is an indicative table, so the weakest
    row decides.
    """
    seen = set(qualities)
    if not seen:
        return None
    for quality in _QUALITY_ORDER:
        if quality in seen:
            return quality
    return None


@dataclass(frozen=True)
class BarSeries:
    """Bars for one contract at one frequency, oldest first."""

    contract_id: int
    freq: str
    ts: list[str] = field(default_factory=list)
    open: list[float] = field(default_factory=list)
    high: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    close: list[float] = field(default_factory=list)
    volume: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.close)

    @property
    def last_close(self) -> float | None:
        return self.close[-1] if self.close else None

    @property
    def last_ts(self) -> str | None:
        return self.ts[-1] if self.ts else None

    @property
    def last_date(self) -> date | None:
        return parse_date(self.ts[-1]) if self.ts else None

    @property
    def trailing_high(self) -> float | None:
        """The highest high in the series, ignoring bars that carry none.

        ``high`` is annotated ``list[float]`` and holds ``None`` wherever the
        column is NULL — ``bar_series`` drops a row with no CLOSE and keeps one
        with no high, because a bar with a close still carries a return. So a
        bare ``max(series.high)`` raises ``TypeError`` in Python 3 rather than
        returning anything, and it did: both ``check_position_risk`` and
        ``get_portfolio_review`` reached the trailing stop through one, so a
        position on any instrument with a single NULL high failed the whole tool
        call. ``stats.true_ranges`` beside it has always guarded; this is the same
        guard, written once so the two callers cannot diverge again.
        """
        highs = [high for high in self.high if high is not None]
        return max(highs) if highs else None

    def tail(self, count: int) -> BarSeries:
        if count <= 0 or count >= len(self.close):
            return self
        return BarSeries(
            contract_id=self.contract_id,
            freq=self.freq,
            ts=self.ts[-count:],
            open=self.open[-count:],
            high=self.high[-count:],
            low=self.low[-count:],
            close=self.close[-count:],
            volume=self.volume[-count:],
        )


def bar_series(
    store: Store,
    contract_id: int,
    *,
    freq: str = FREQ_DAILY,
    since: date | None = None,
    until: date | None = None,
    limit: int | None = None,
) -> BarSeries:
    """Bars for a contract, oldest first, with any close missing dropped.

    A bar with no close carries no return and no range; keeping it would put a
    ``None`` into the middle of a log-return series where it silently becomes a
    skipped period rather than a visible gap.
    """
    sql = (
        "SELECT ts, open, high, low, close, volume FROM bars "
        "WHERE contract_id = ? AND freq = ? AND close IS NOT NULL"
    )
    params: list[object] = [contract_id, freq]
    if since is not None:
        sql += " AND ts >= ?"
        params.append(since.isoformat())
    if until is not None:
        # ts is a full ISO-8601 stamp, so a plain "<= until" would drop the
        # session dated `until` itself: '2026-08-24T07:00:00Z' sorts AFTER
        # '2026-08-24'. Bounding by the following midnight keeps the day and
        # still lets the index range-scan.
        sql += " AND ts < ?"
        params.append((until + timedelta(days=1)).isoformat())
    sql += " ORDER BY ts"
    if limit is not None and limit > 0:
        # Newest N, then reversed: an ORDER BY ts LIMIT would take the OLDEST.
        sql = (
            "SELECT * FROM (" + sql.replace(" ORDER BY ts", " ORDER BY ts DESC LIMIT ?") + ")"
            " ORDER BY ts"
        )
        params.append(limit)

    rows = store.execute(sql, params).fetchall()
    series = BarSeries(contract_id=contract_id, freq=freq)
    for row in rows:
        series.ts.append(str(row["ts"]))
        series.open.append(_as_float(row["open"]))
        series.high.append(_as_float(row["high"]))
        series.low.append(_as_float(row["low"]))
        series.close.append(float(row["close"]))
        series.volume.append(_as_float(row["volume"]))
    return series


@dataclass(frozen=True)
class MacroObservation:
    reference_date: date
    release_date: date | None
    value: float
    frequency: str | None
    units: str | None


@dataclass(frozen=True)
class MacroSeries:
    """One macro series, oldest first by reference date."""

    series_id: str
    source: str | None
    observations: list[MacroObservation] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.observations)

    @property
    def unaligned_count(self) -> int:
        """Observations carrying no release date, and so unusable point-in-time."""
        return sum(1 for o in self.observations if o.release_date is None)

    @property
    def latest(self) -> MacroObservation | None:
        """The newest observation by reference date, whatever its release status."""
        return self.observations[-1] if self.observations else None

    def known_at(self, as_of: date) -> MacroObservation | None:
        """The newest observation PUBLISHED on or before ``as_of``.

        Observations with no release date are excluded rather than assumed to
        have been published on their reference date. Check
        :attr:`unaligned_count` and say so when it is non-zero: a series that
        cannot be aligned point-in-time is a coverage gap, not a free pass.
        """
        published = [
            o for o in self.observations if o.release_date is not None and o.release_date <= as_of
        ]
        return published[-1] if published else None

    def values(self) -> list[float]:
        return [o.value for o in self.observations]

    def observations_known_at(self, as_of: date) -> list[MacroObservation]:
        """Every observation PUBLISHED on or before ``as_of``, oldest first.

        The list form of :meth:`known_at`, and the one place the point-in-time
        rule is applied to a whole sample. A caller that needs the release dates
        as well as the values — a change measured over a window in weeks has to
        know WHEN each endpoint was published — reads this rather than filtering
        ``observations`` itself, because a filter written at the call site is a
        second copy of the rule that can be written without the release-date
        test and look right.
        """
        return [
            o for o in self.observations if o.release_date is not None and o.release_date <= as_of
        ]

    def values_known_at(self, as_of: date) -> list[float]:
        """Every value PUBLISHED on or before ``as_of``, oldest first.

        The list form of :meth:`known_at`, for the percentiles. Building one from
        ``reference_date`` instead is the whole look-ahead problem: a percentile
        is a claim about what the distribution looked like at the time, so an
        observation nobody had yet cannot be in it. Observations with no release
        date are excluded, and :attr:`unaligned_count` says how many.
        """
        return [o.value for o in self.observations_known_at(as_of)]

    def frequencies_known_at(self, as_of: date) -> set[str]:
        """The distinct non-null frequencies among the observations known at ``as_of``.

        A correlation pairs two series period by period, so the two have to be
        measured on the SAME period. Correlating a daily equity return against a
        monthly rate change does not compare them — it compares twelve readings a
        year against two hundred and fifty, and any alignment that makes the
        lengths match has resampled one of them. ``MacroPull._sane`` already
        refuses a series the publisher resampled; this is the same refusal one
        layer down, where the mixing would happen between two series rather than
        inside one.

        ``None`` frequencies are not counted, for the reason
        :meth:`units_known_at` does not count null units: a row that never
        declared what cadence it carries cannot contradict one that did.
        """
        return {
            o.frequency
            for o in self.observations
            if o.frequency is not None and o.release_date is not None and o.release_date <= as_of
        }

    def units_known_at(self, as_of: date) -> set[str]:
        """The distinct non-null units among the values :meth:`values_known_at` returns.

        A percentile is a rank of a value against others of the SAME quantity.
        Two units inside one window is not a thin sample or a stale one — it is a
        ranking of two different measurements against each other, and it reads as
        an ordinary number all the way to the output.

        This is not hypothetical: migration 015 changed CFTC positioning from a
        net contract COUNT to a share of open interest, and without a guard a
        store holding both would have ranked counts in the tens of thousands
        against shares in the tenths, putting every old row at the top of the
        window and every new one at the bottom. The migration deletes those rows;
        this makes the same mistake impossible to reintroduce silently, for any
        series, by any later change.

        ``None`` units are not counted: a row that never declared what it carries
        cannot contradict one that did, and it is the same gap
        :attr:`unaligned_count` reports for release dates.
        """
        return {
            o.units
            for o in self.observations
            if o.units is not None and o.release_date is not None and o.release_date <= as_of
        }

    def value_on_or_before(self, reference: date) -> MacroObservation | None:
        """The newest observation whose REFERENCE period ends on or before a date.

        For a level read where the point-in-time question does not arise — the
        FX rate three years ago, say. Never use it to build a percentile that a
        decision keys off; use :meth:`known_at` for that.

        **It has no production call site, deliberately.** Every compute path that
        once used it aligned on the reference period and so read prints weeks
        before they existed; all of them now use :meth:`known_at`. It survives as
        the foil in ``tests/test_point_in_time.py``, which asserts the two return
        DIFFERENT observations — the cheapest guard against the alignment
        quietly reverting. Delete it only together with those tests.
        """
        earlier = [o for o in self.observations if o.reference_date <= reference]
        return earlier[-1] if earlier else None


def macro_series(
    store: Store,
    series_id: str,
    *,
    source: str | None = None,
    since: date | None = None,
) -> MacroSeries:
    """One macro series from the store, oldest first. Missing values are dropped."""
    sql = (
        "SELECT series_id, source, reference_date, release_date, value, frequency, units "
        "FROM macro WHERE series_id = ? AND value IS NOT NULL"
    )
    params: list[object] = [series_id]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    if since is not None:
        sql += " AND reference_date >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY reference_date"

    observations: list[MacroObservation] = []
    resolved_source = source
    for row in store.execute(sql, params).fetchall():
        reference = parse_date(row["reference_date"])
        if reference is None:
            continue
        resolved_source = resolved_source or str(row["source"])
        observations.append(
            MacroObservation(
                reference_date=reference,
                release_date=parse_date(row["release_date"]),
                value=float(row["value"]),
                frequency=row["frequency"],
                units=row["units"],
            )
        )
    return MacroSeries(series_id=series_id, source=resolved_source, observations=observations)


@dataclass(frozen=True)
class ChainRow:
    """One strike of one expiry on one session."""

    snapshot_date: date
    underlying_id: int
    expiry: date
    strike: float
    right: str
    bid: float | None
    ask: float | None
    last: float | None
    iv: float | None
    delta: float | None
    open_interest: int | None
    volume: int | None
    contract_id: int | None
    #: Which route collected this row: mcp | cp_api | marketdata_app. A standing
    #: rule of this build is that collection can be migrated wholesale, which
    #: needs the source on the row rather than in the run log.
    source: str | None = None
    #: The market-data state the quote was captured in, verbatim from the route:
    #: FROZEN, DELAYED, REALTIME, or NULL where it reported nothing. Carried up
    #: here because SKILL.md requires anything frozen or delayed to be labelled
    #: indicative in every table it appears in, and a label cannot be applied to
    #: a fact the reader never receives.
    market_data_status: str | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread_pct_of_mid(self) -> float | None:
        """Bid-ask spread as a fraction of mid. ``None`` when either side is absent.

        A CROSSED book (``ask < bid``) is deliberately NOT folded into ``None``
        here: this is the one signal a consumer needs to tell "the spread is
        broken" from "the spread could not be measured", and collapsing both to
        the same absence would erase it before either the liquidity gate or the
        smile's hygiene pass ever sees it. Both consumers check the sign
        explicitly and treat a negative reading as a failure, never as a pass —
        see ``CandidateGates.liquidity`` and ``smile._hygiene_failure``.
        """
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid


def latest_bar_date(
    store: Store, contract_id: int, *, freq: str = FREQ_DAILY, until: date | None = None
) -> date | None:
    """The newest bar for a contract, optionally as the store looked on a past day.

    A ``MAX`` rather than a windowed read, because this answers "how old is the
    newest bar" and any window narrower than the answer distorts it. The grid's
    freshness check used to ask ``bar_series`` for a lookback of a few multiples
    of the staleness window: an instrument whose newest bar was OLDER than that
    lookback returned nothing at all, which read as "no bars" — absence, not
    staleness — so the very stalest instruments were the ones reported `ok`.
    """
    sql = "SELECT MAX(ts) FROM bars WHERE contract_id = ? AND freq = ? AND close IS NOT NULL"
    params: list[object] = [contract_id, freq]
    if until is not None:
        # ts carries a time, so the bound is the following midnight — the same
        # reasoning as bar_series, and for the same reason: a plain "<= until"
        # drops the session named by its own bound.
        sql += " AND ts < ?"
        params.append((until + timedelta(days=1)).isoformat())
    row = store.execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return None
    return parse_date(str(row[0])[:ISO_DATE_CHARS])


def latest_chain_date(
    store: Store, underlying_id: int | None = None, *, until: date | None = None
) -> date | None:
    """The newest chain snapshot, optionally as the store looked on a past day."""
    sql = "SELECT MAX(snapshot_date) FROM chain_snapshots"
    where: list[str] = []
    params: list[object] = []
    if underlying_id is not None:
        where.append("underlying_id = ?")
        params.append(underlying_id)
    if until is not None:
        where.append("snapshot_date <= ?")
        params.append(until.isoformat())
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = store.execute(sql, params).fetchone()
    return parse_date(row[0]) if row and row[0] else None


def chain_rows(
    store: Store,
    *,
    underlying_id: int | None = None,
    snapshot_date: date | None = None,
    expiry: date | None = None,
) -> list[ChainRow]:
    """Chain rows for a session, defaulting to the latest one collected."""
    session = snapshot_date or latest_chain_date(store, underlying_id)
    if session is None:
        return []
    sql = (
        "SELECT snapshot_date, underlying_id, expiry, strike, right, bid, ask, last, iv, "
        "delta, open_interest, volume, contract_id, source, market_data_status "
        "FROM chain_snapshots WHERE snapshot_date = ?"
    )
    params: list[object] = [session.isoformat()]
    if underlying_id is not None:
        sql += " AND underlying_id = ?"
        params.append(underlying_id)
    if expiry is not None:
        sql += " AND expiry = ?"
        params.append(expiry.isoformat())
    sql += " ORDER BY underlying_id, expiry, right, strike"

    out: list[ChainRow] = []
    for row in store.execute(sql, params).fetchall():
        parsed_expiry = parse_date(row["expiry"])
        if parsed_expiry is None:
            continue
        out.append(
            ChainRow(
                snapshot_date=session,
                underlying_id=int(row["underlying_id"]),
                expiry=parsed_expiry,
                strike=float(row["strike"]),
                right=str(row["right"]),
                bid=_as_float(row["bid"]),
                ask=_as_float(row["ask"]),
                last=_as_float(row["last"]),
                iv=_as_float(row["iv"]),
                delta=_as_float(row["delta"]),
                open_interest=_as_int(row["open_interest"]),
                volume=_as_int(row["volume"]),
                contract_id=_as_int(row["contract_id"]),
                source=_as_text(row["source"]),
                market_data_status=_as_text(row["market_data_status"]),
            )
        )
    return out


@dataclass(frozen=True)
class UnderlyingSnapshot:
    snapshot_date: date
    contract_id: int
    last: float | None
    implied_vol: float | None
    iv_percentile_52w: float | None
    historical_vol_30d: float | None
    #: As on :class:`ChainRow`. The grid's implied-vol and IV-percentile cells
    #: read this table rather than the chain, so the label has to travel on
    #: both or half the tables that need it would go unmarked.
    source: str | None = None
    market_data_status: str | None = None
    #: COLLECTED, decimal, and a different fact from the DECLARED
    #: ``universe.dividend_yield``: this one is dated, so a grid rebuilt for a past
    #: session prices it with the yield known then rather than today's.
    dividend_yield: float | None = None
    #: Which basis the figure beside it actually is. IBKR serves a TRAILING yield;
    #: Black-Scholes-Merton wants a continuous one. Recording the basis is what
    #: stops the approximation passing as the model's q.
    dividend_yield_basis: str | None = None
    #: An implied vol this repository SOLVED out of a mid price, never one a
    #: vendor quoted. Kept in its own field for the reason migration 017 gives:
    #: a percentile series that mixes the two has a methodology break at the join.
    implied_vol_solved: float | None = None
    implied_vol_solved_source: str | None = None


def underlying_snapshots(
    store: Store, contract_id: int, *, until: date | None = None, limit: int | None = None
) -> list[UnderlyingSnapshot]:
    """Underlying snapshots for a contract, oldest first.

    ``until`` bounds the read to what the store held on that day, so a grid
    rebuilt for a past date cannot read a snapshot taken after it.
    """
    sql = (
        "SELECT snapshot_date, contract_id, last, implied_vol, iv_percentile_52w, "
        "historical_vol_30d, source, market_data_status, dividend_yield, "
        "dividend_yield_basis, implied_vol_solved, implied_vol_solved_source "
        "FROM underlying_snapshots WHERE contract_id = ?"
    )
    params: list[object] = [contract_id]
    if until is not None:
        sql += " AND snapshot_date <= ?"
        params.append(until.isoformat())
    sql += " ORDER BY snapshot_date DESC"
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    rows = store.execute(sql, params).fetchall()
    out: list[UnderlyingSnapshot] = []
    for row in reversed(rows):
        session = parse_date(row["snapshot_date"])
        if session is None:
            continue
        out.append(
            UnderlyingSnapshot(
                snapshot_date=session,
                contract_id=int(row["contract_id"]),
                last=_as_float(row["last"]),
                implied_vol=_as_float(row["implied_vol"]),
                iv_percentile_52w=_as_float(row["iv_percentile_52w"]),
                historical_vol_30d=_as_float(row["historical_vol_30d"]),
                source=_as_text(row["source"]),
                market_data_status=_as_text(row["market_data_status"]),
                dividend_yield=_as_float(row["dividend_yield"]),
                dividend_yield_basis=_as_text(row["dividend_yield_basis"]),
                implied_vol_solved=_as_float(row["implied_vol_solved"]),
                implied_vol_solved_source=_as_text(row["implied_vol_solved_source"]),
            )
        )
    return out


def collected_dividend_yield(
    store: Store, contract_id: int | None, *, as_of: date | None = None
) -> tuple[float, date, str | None] | None:
    """The most recent COLLECTED dividend yield as known at ``as_of``, and its basis.

    One implementation for every caller, because the alternative is two readers
    disagreeing about which yield an instrument had on a given day.

    Bounded at ``as_of`` like every other store read here. Unbounded, a position
    assessed for a past session would be discounted at a yield collected after it
    — the same point-in-time failure that put the collected figure in a dated
    table rather than on ``universe`` in the first place.

    ``None`` when no collector has written one, which lets the caller fall through
    to the DECLARED yield rather than to zero. A collected zero is NOT ``None``:
    it is a real reading that the instrument pays nothing, and the two claims are
    exactly the pair migration 009 exists to keep apart.
    """
    if contract_id is None:
        return None
    snapshots = underlying_snapshots(store, contract_id, until=as_of, limit=1)
    if not snapshots:
        return None
    latest = snapshots[-1]
    if latest.dividend_yield is None:
        return None
    return latest.dividend_yield, latest.snapshot_date, latest.dividend_yield_basis


def fx_ratio(
    store: Store,
    market_spec: Mapping[str, Any],
    *,
    as_of: date,
    horizon_days: int,
    base_currency: str,
) -> float | None:
    """``fx_start / fx_end`` — the base-currency return contribution of an FX move.

    ECB reference rates are quoted as **units of local currency per euro**, so a
    rising series is the euro strengthening and the local currency weakening. A
    local asset is worth ``local_value / fx`` in euros, which makes the euro
    return ``(1 + local) * (fx_start / fx_end) - 1``.

    Written once, here, because getting that ratio upside down inverts every
    emerging-market reading in the grid and in the four-way decomposition alike.
    Returns ``1.0`` for the base currency itself and ``None`` when the series is
    absent — never a silent 1.0 for a currency that simply was not collected.
    """
    series_id = market_spec.get("fx_series")
    if series_id is None:
        return 1.0 if str(market_spec.get("currency")) == base_currency else None

    series = macro_series(store, str(series_id))
    if not len(series):
        return None
    end = series.known_at(as_of)
    start = series.known_at(as_of - timedelta(days=horizon_days))
    if end is None or start is None or end.value <= 0 or start.value <= 0:
        return None
    return start.value / end.value


def cpi_yoy(
    store: Store,
    market_spec: Mapping[str, Any],
    *,
    as_of: date,
    calendar_days_per_year: float,
) -> float | None:
    """Local inflation, year on year, as a decimal.

    Handles both shapes the sources publish. A series declared ``cpi_is_yoy`` is
    already a rate and every source used here publishes those in percent; an
    index level yields the rate directly and needs no unit conversion at all.

    The index path refuses two observations carrying **different units**. An index
    is rebased — Eurostat moved HICP to 2025=100 during 2026 — and a ratio taken
    across a rebase divides one unit by another and reports the result as an
    inflation rate. The collector's job is not to serve a mixed-base window at all
    (``value_vintage: latest`` in config.yaml exists for exactly these series),
    and this is the reading that refuses one if it ever arrives. Same rule as
    :meth:`MacroSeries.units_known_at`: a NULL unit cannot contradict a declared
    one, so it does not trigger the refusal.
    """
    series_id = market_spec.get("cpi_series")
    if not series_id:
        return None
    series = macro_series(store, str(series_id))
    if not len(series):
        return None

    latest = series.known_at(as_of)
    if latest is None:
        return None
    if bool(market_spec.get("cpi_is_yoy", True)):
        return latest.value / PERCENT_TO_DECIMAL

    year_ago = series.known_at(as_of - timedelta(days=round(calendar_days_per_year)))
    if year_ago is None or year_ago.value <= 0:
        return None
    if mixed_cpi_units(latest, year_ago):
        return None
    return latest.value / year_ago.value - 1.0


def mixed_cpi_units(latest: MacroObservation, year_ago: MacroObservation) -> bool:
    """Do these two index readings measure on different bases?

    Split out because the indicator has to SAY which of the several reasons a
    real rate is absent applies, and re-deriving it there would be a second
    implementation of the same test.
    """
    return (
        latest.units is not None and year_ago.units is not None and latest.units != year_ago.units
    )


def underlying_snapshot_count(store: Store, contract_id: int, *, until: date | None = None) -> int:
    """How many snapshots carrying a usable implied vol have ACCUMULATED.

    The self-computed IV percentile switches on at
    ``data.iv_self_source_min_sessions`` accumulated snapshots. Counting the
    accumulation rather than demanding a perfect 52-week window matters: one
    session where the vendor served no implied vol would otherwise block the
    switch permanently.

    ``until`` bounds the count to what the store held on that day, exactly as
    :func:`underlying_snapshots` does. Without it a grid rebuilt for a past date
    counts snapshots recorded after it and can switch to the self-computed
    percentile before the data existed — the gate opening on evidence from its
    own future. The point-in-time pass closed this class for every row read; this
    one is a ``COUNT(*)`` and did not look like a store read.

    ``snapshot_date`` is a plain date, so the bound is ``<=`` rather than the
    following-midnight form :func:`bar_series` needs for a timestamp column.
    """
    sql = (
        "SELECT COUNT(*) FROM underlying_snapshots "
        "WHERE contract_id = ? AND implied_vol IS NOT NULL"
    )
    params: list[object] = [contract_id]
    if until is not None:
        sql += " AND snapshot_date <= ?"
        params.append(until.isoformat())
    row = store.execute(sql, params).fetchone()
    return 0 if row is None else int(row[0])


@dataclass(frozen=True)
class CurvePoint:
    snapshot_date: date
    root: str
    expiry: date
    price: float | None
    annualised_roll: float | None


def latest_curve_date(store: Store, root: str, *, until: date | None = None) -> date | None:
    """The newest curve snapshot for a root, bounded at ``until``.

    ``until`` is what keeps a commodity state rebuilt for a past date from
    classifying contango off a curve collected after it. Every other store read
    in this module takes the bound; this one did not, and the wrapper below is
    the only reader that reaches the curve at all.
    """
    if until is None:
        row = store.execute(
            "SELECT MAX(snapshot_date) FROM curve_snapshots WHERE root = ?", (root,)
        ).fetchone()
    else:
        row = store.execute(
            "SELECT MAX(snapshot_date) FROM curve_snapshots WHERE root = ? AND snapshot_date <= ?",
            (root, until.isoformat()),
        ).fetchone()
    return parse_date(row[0]) if row and row[0] else None


def curve(
    store: Store,
    root: str,
    *,
    snapshot_date: date | None = None,
    until: date | None = None,
) -> list[CurvePoint]:
    """One session's term structure for a root, front expiry first.

    ``until`` bounds which session "latest" means. Without it a curve read for a
    past ``as_of`` returned today's shape, so a wrapper recommendation made for
    a past date was made on information that did not exist then — the same
    look-ahead every other read here is bounded against.
    """
    session = snapshot_date or latest_curve_date(store, root, until=until)
    if session is None:
        return []
    rows = store.execute(
        "SELECT snapshot_date, root, expiry, price, annualised_roll FROM curve_snapshots "
        "WHERE root = ? AND snapshot_date = ? ORDER BY expiry",
        (root, session.isoformat()),
    ).fetchall()
    out: list[CurvePoint] = []
    for row in rows:
        expiry = parse_date(row["expiry"])
        if expiry is None:
            continue
        out.append(
            CurvePoint(
                snapshot_date=session,
                root=str(row["root"]),
                expiry=expiry,
                price=_as_float(row["price"]),
                annualised_roll=_as_float(row["annualised_roll"]),
            )
        )
    return out


def curve_front_history(
    store: Store, root: str, *, since: date | None = None
) -> list[tuple[date, float]]:
    """Front-month price per session, oldest first.

    The only self-collected price series for a commodity with no free spot feed.
    It starts empty and compounds, exactly like the chain history, so a proxy
    regression against it reports its observation count rather than pretending.
    """
    sql = (
        "SELECT snapshot_date, MIN(expiry) AS front, price FROM curve_snapshots "
        "WHERE root = ? AND price IS NOT NULL"
    )
    params: list[object] = [root]
    if since is not None:
        sql += " AND snapshot_date >= ?"
        params.append(since.isoformat())
    sql += " GROUP BY snapshot_date ORDER BY snapshot_date"

    out: list[tuple[date, float]] = []
    for row in store.execute(sql, params).fetchall():
        session = parse_date(row["snapshot_date"])
        if session is None or row["price"] is None:
            continue
        out.append((session, float(row["price"])))
    return out


def contract_id_for_symbol(store: Store, symbol: str) -> int | None:
    """The universe conid for a symbol, or ``None``.

    Exact match on the upper-cased symbol. A near match is not a match: resolving
    a grid cell to the wrong instrument is worse than reporting the cell absent.
    """
    row = store.execute(
        "SELECT contract_id FROM universe WHERE UPPER(symbol) = ? LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    return None if row is None else int(row[0])


def universe_row(store: Store, contract_id: int) -> dict[str, object] | None:
    """One universe row as a plain mapping, for the gates."""
    row = store.execute("SELECT * FROM universe WHERE contract_id = ?", (contract_id,)).fetchone()
    return None if row is None else dict(row)


@dataclass(frozen=True)
class NavObservation:
    """A NAV, and the date it was recorded for.

    The date is not decoration. The sleeve budget is a fraction of NAV and every
    affordability gate is measured against it, so a figure from three months ago
    sizes today's entries against a book that no longer exists — and the tools
    were returning it with nothing to say how old it was. ``nav_history`` is
    written when something records one, not on a schedule, so a gap is normal
    and reporting the age is the only honest answer.
    """

    nav: float
    as_of: date | None


def latest_nav_observation(store: Store) -> NavObservation | None:
    """The most recent NAV, with its date, or ``None`` when none is recorded.

    Everything the sleeve can reach is measured against this, so it has one
    reader: a second one would eventually disagree about which row is latest.
    """
    row = store.execute(
        "SELECT nav_base, as_of FROM nav_history ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return NavObservation(nav=float(row[0]), as_of=parse_date(row[1]))


def latest_nav(store: Store) -> float | None:
    """The most recent NAV figure, without its date.

    Kept for callers that only size something with it. Anything REPORTING a
    NAV-derived figure should use :func:`latest_nav_observation` and say how old
    the reading is.
    """
    observation = latest_nav_observation(store)
    return None if observation is None else observation.nav


def grid_instruments(store: Store) -> list[dict[str, object]]:
    """Every instrument declared as the measurement for a market x sector cell."""
    rows = store.execute(
        "SELECT * FROM universe WHERE grid_measures = 1 ORDER BY market, sector"
    ).fetchall()
    return [dict(row) for row in rows]


def _as_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _as_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]


def _as_text(value: object) -> str | None:
    """A text column, with blank treated as absent.

    An empty string in ``market_data_status`` is the route having reported
    nothing in a shape SQLite stored as text rather than NULL. Both mean the
    same thing and must classify the same way."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
