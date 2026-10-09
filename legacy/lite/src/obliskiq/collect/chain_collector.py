"""Daily option-chain snapshot. The highest-priority collector.

Option price history is **not retrievable retrospectively from IBKR**.
``get_price_history`` on an option contract returns "No historical market data
available" even at 49,114 open interest, and there is no as-of endpoint for
chains. Daily self-collection is the only route to per-strike IV history, and a
day not collected is gone permanently. Everything else in this package can be
rebuilt; this cannot.

What it collects, and what it deliberately does not
---------------------------------------------------

Only expiries inside ``tenor.entry_dte_min``-``entry_dte_max``, and only strikes
inside a **volatility-scaled** band — never the whole chain, and never a fixed
percentage band. Roughly a tenfold volume reduction, and the part of the chain
the mandate can actually trade.

Selection keys off ``universe.collect_chain`` and never off
``priips_restricted``. An EU retail account cannot buy SPY for want of a KID but
may trade listed options on it, which is the whole reason the option sleeve
exists.

Call shape
----------

Per month, the expiry dates are resolved with ONE ``/iserver/secdef/info`` call at
the strike nearest spot, rather than one per strike. Bounds are then computed per
expiry against that expiry's own tau, the wanted strikes are unioned across
expiries, and each strike is resolved once for the whole month. A row is written
only where the strike falls inside its own expiry's band.

Degradation
-----------

When the sigma comes from a fallback rung the target is marked degraded and the
run status follows, unless the instrument listed recently enough that thin history
is expected. A degraded run means the daily-bar backfill is not keeping up, and it
will keep not keeping up until somebody looks.

.. warning::
   UNVERIFIED against a live Client Portal session. Built against fixtures; the
   wire format still needs confirming, and what it turns out to be belongs in
   ``references/ibkr-playbook.md``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import Config
from ..errors import NoMatchingTargets, PermanentUpstreamError
from ..logging_setup import get_logger
from ..store import Store
from ..util.numeric import safe_float, safe_int
from ..util.timeutil import parse_date, utc_stamp, utc_today
from .retry import TargetRetryPolicy, pace, with_target_retry
from .runlog import CollectorRun
from .source import MarketData, Quote
from .strikes import TenorWindow, diffusion_tau_years, nearest_strike, select_strikes, strike_bounds
from .universe import assert_seeded
from .vol import (
    FREQ_DAILY,
    Sigma,
    SigmaLadder,
    newest_daily_bar_date,
    recent_daily_closes,
)

log = get_logger("collect.chain")

COLLECTOR_NAME = "chain"

#: The two option rights, and the ``chain_snapshots.right`` values they map to.
RIGHTS = ("C", "P")

#: ``option_strikes`` returns the ladder under these keys, one per right.
_LADDER_SIDE = {"C": "call", "P": "put"}

#: How far an echoed strike may differ from the requested one before it is worth
#: a warning. Floats that round-trip through JSON are not always bit-identical.
STRIKE_REL_TOL = 1e-9

#: Decimal places the sigma is rounded to in the run log. Presentation only —
#: the full value is what goes into chain_snapshots.sigma_used.
SIGMA_LOG_PRECISION = 6

#: What the IBKR routes actually serve for a dividend yield.
#:
#: Black-Scholes-Merton wants a CONTINUOUS yield q over the option's life; IBKR
#: serves a TRAILING one. On broad ETFs at 90-200 DTE that is a defensible
#: approximation, and it is stored AS an approximation: this constant lands in
#: underlying_snapshots.dividend_yield_basis beside every collected figure, so a
#: later reader can tell the two apart instead of assuming. If a continuous yield
#: is ever sourced it gets a different value here, not a silent reuse of this one.
DIVIDEND_YIELD_BASIS = "trailing_12m"


@dataclass(frozen=True)
class ChainTarget:
    """One underlying to snapshot."""

    contract_id: int
    symbol: str
    exchange: str | None
    listed_on: str | None
    #: The venue an option request must send, declared in universe.yaml. None
    #: means undeclared, and NOT that the listing venue or SMART is right — see
    #: :meth:`ChainCollector._exchange_for`.
    option_exchange: str | None = None


def chain_targets(store: Store, *, only: Sequence[str] | None = None) -> list[ChainTarget]:
    """Underlyings flagged for chain collection.

    Note what is NOT in the WHERE clause: ``priips_restricted``. It gates the
    candidate list, never collection.
    """
    rows = store.execute(
        "SELECT contract_id, symbol, exchange, listed_on, option_exchange FROM universe "
        "WHERE collect_chain = 1 ORDER BY symbol"
    ).fetchall()
    targets = [
        ChainTarget(
            contract_id=int(row["contract_id"]),
            symbol=str(row["symbol"]),
            exchange=row["exchange"],
            listed_on=row["listed_on"],
            option_exchange=row["option_exchange"],
        )
        for row in rows
    ]
    if only:
        wanted = {s.upper() for s in only}
        targets = [t for t in targets if t.symbol.upper() in wanted]
        if not targets:
            # Same discipline as the curve collector. Without this the run reports
            # 'ok' over zero rows, which is indistinguishable in the log from a
            # healthy one — and chain history cannot be collected retrospectively,
            # so a day lost this way is lost permanently.
            raise NoMatchingTargets(
                f"none of the requested symbols {sorted(wanted)} is flagged for chain "
                "collection. Set collect_chain on them in universe.yaml, or drop --only."
            )
    return targets


def daily_bars_are_fresh(store: Store, config: Config, *, today: date | None = None) -> bool:
    """Whether the daily bar history is recent enough to size a strike band.

    The scheduler runs the daily backfill before the chain collector precisely so
    this is true. It is checked rather than assumed, because a backfill that
    failed silently would otherwise push every underlying onto a fallback rung
    without anything saying why.
    """
    max_age = config.require_int("collectors.chain.bars_freshness_days")
    row = store.execute(
        "SELECT MAX(ts) AS latest FROM bars WHERE freq = ?", (FREQ_DAILY,)
    ).fetchone()
    latest = parse_date(row["latest"] if row else None)
    if latest is None:
        return False
    return ((today or utc_today()) - latest).days <= max_age


class ChainCollector:
    """Walks the tracked universe and writes one day of chain snapshots."""

    def __init__(self, store: Store, config: Config, market_data: MarketData) -> None:
        self._store = store
        self._config = config
        self._market_data = market_data
        self._policy = TargetRetryPolicy.from_config(config)
        self._window = TenorWindow(config)
        self._ladder = SigmaLadder(config)

        chain = config.section("collectors.chain")
        self._max_expiries = chain.require_int("max_expiries_per_underlying")
        self._max_strikes = chain.require_int("max_strikes_per_expiry")
        self._max_strikes_fallback = chain.require_int("max_strikes_per_expiry_on_fallback")
        self._warn_on_fallback = chain.require_bool("warn_on_fallback")
        self._new_listing_grace_days = chain.require_int("new_listing_grace_days")

        self._k = config.require_float("data.strike_bounds_sigma")
        self._trading_days_per_year = config.require_float("data.trading_days_per_year")
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")

    async def run(
        self,
        run: CollectorRun,
        *,
        only: Sequence[str] | None = None,
        dry_run: bool = False,
        today: date | None = None,
    ) -> int:
        assert_seeded(self._store, self._config)
        session = today or utc_today()
        targets = chain_targets(self._store, only=only)
        if not targets:
            log.warning(
                "no underlyings are flagged for chain collection. Set collect_chain "
                "on the instruments you want in universe.yaml."
            )
        written = 0
        for target in targets:
            try:
                rows = await with_target_retry(
                    self._policy,
                    target.symbol,
                    lambda t=target: self._collect_one(t, session, dry_run=dry_run, run=run),
                )
            except Exception as exc:
                run.failed(target.symbol, repr(exc))
                continue
            written += rows
            await pace(self._policy)
        return written

    # ── one underlying ──

    async def _collect_one(
        self, target: ChainTarget, session: date, *, dry_run: bool, run: CollectorRun
    ) -> int:
        quote = await self._underlying_quote(target)
        spot = self._spot_for(target, quote)
        if spot is None:
            # Permanent for this run: the fallback is the store's own bars, and
            # asking the same quote endpoint twice more does not put a daily bar
            # in the store. Retrying it spent two more backoffs per target for an
            # answer that could not change — which is most of a run when the
            # backfill ahead of it collected nothing.
            raise PermanentUpstreamError(
                f"no spot price for {target.symbol}: the snapshot carried none and "
                "there is no daily bar to fall back on. A strike band cannot be "
                "centred on a price that was never observed."
            )

        closes = recent_daily_closes(
            self._store, target.contract_id, limit=self._ladder.max_bars_needed
        )
        sigma = self._ladder.resolve(
            closes,
            vendor_iv=quote.implied_vol if quote is not None else None,
            newest_close=newest_daily_bar_date(self._store, target.contract_id),
            session=session,
        )
        degraded = self._is_degraded(target, sigma, session)
        cap = self._max_strikes_fallback if sigma.is_fallback else self._max_strikes

        months, exchanges = await self._market_data.option_months(
            target.symbol, conid=target.contract_id
        )
        if not months:
            raise ValueError(
                f"{target.symbol} (conid {target.contract_id}) reports no option months. "
                "The section is read against the conid the universe holds, so another "
                "listing of the same ticker having options does not count."
            )
        exchange = self._exchange_for(target, exchanges)
        in_window = self._window.months(tuple(months), session)
        if not in_window:
            raise ValueError(
                f"{target.symbol} lists no option month overlapping the "
                f"{self._window.dte_min}-{self._window.dte_max} DTE window"
            )

        rows: list[tuple[Any, ...]] = []
        for month in in_window:
            # The cap is per UNDERLYING, so each month gets only what the months
            # before it left unspent. Checking after the fact instead would let two
            # months return twice the budget before the loop noticed.
            remaining = self._max_expiries - len({row[2] for row in rows})
            if remaining <= 0:
                break
            rows.extend(
                await self._collect_month(
                    target,
                    month,
                    exchange,
                    spot=spot,
                    sigma=sigma,
                    session=session,
                    cap=cap,
                    max_expiries=remaining,
                )
            )

        if not rows:
            # **Zero rows is a failure, not a quiet success.** This used to fall
            # through to run.succeeded(symbol, 0), which is how SAN and TTE
            # reported ok on 2026-09-10 while collecting no chain at all: the
            # months came back, the strike ladder came back empty on every one of
            # them, and the run summary counted two more healthy targets. A
            # European chain cannot be re-bought at any price, so the one thing
            # the log must never do is describe a missing one as collected.
            #
            # Raised BEFORE the write, so the underlying snapshot is not written
            # either: the transaction below exists to keep the two halves
            # together, and a snapshot with no chain against it is the half-state
            # it was built to prevent.
            raise ValueError(
                f"{target.symbol} (conid {target.contract_id}) resolved no option "
                f"contract on exchange {exchange!r} across months {list(in_window)}. "
                f"The search reported exchanges {list(exchanges)}. Either the venue is "
                "wrong for this conid or the strike band excluded every listed strike."
            )

        if not dry_run:
            # Both halves commit together, or neither does. The underlying
            # snapshot used to be written at the TOP of this method and the chain
            # rows at the bottom, so anything raising in between — an underlying
            # that lists no month in the tenor window, a connection dropped
            # mid-walk — left a snapshot row with no chain for that date. That is
            # indistinguishable from a chain that legitimately returned nothing,
            # and underlying_snapshots.implied_vol is the series the self-computed
            # IV percentile is built from, so the half-state does not merely look
            # untidy: it feeds a statistic.
            with self._store.transaction():
                self._write_underlying_snapshot(target, session, quote, spot)
                self._write(rows)

        run.succeeded(
            target.symbol,
            len(rows),
            degraded=degraded,
            reason=(
                f"strike band sized from {sigma.source} rather than the primary realised-vol rung"
                if degraded
                else None
            ),
            sigma=round(sigma.value, SIGMA_LOG_PRECISION),
            sigma_source=sigma.source,
            spot=spot,
            expiries=sorted({row[2] for row in rows}),
        )
        return len(rows)

    async def _collect_month(
        self,
        target: ChainTarget,
        month: str,
        exchange: str,
        *,
        spot: float,
        sigma: Sigma,
        session: date,
        cap: int,
        max_expiries: int,
    ) -> list[tuple[Any, ...]]:
        ladder = await self._market_data.option_strikes(
            target.contract_id, month=month, exchange=exchange
        )
        await pace(self._policy)

        anchor = nearest_strike(ladder.get("call") or ladder.get("put") or [], spot)
        if anchor is None:
            log.warning(
                "no strikes listed for month; skipping",
                extra={"symbol": target.symbol, "month": month},
            )
            return []

        # ONE call resolves every expiry in the month. The alternative is a call
        # per strike before knowing which strikes are even wanted.
        anchor_rows = await self._market_data.option_contracts(
            target.contract_id, month=month, exchange=exchange, strike=anchor, right=RIGHTS[0]
        )
        await pace(self._policy)
        expiries = self._window.expiries(
            [str(row.get("maturityDate") or row.get("expiry") or "") for row in anchor_rows],
            session,
        )
        expiries = expiries[:max_expiries]
        if not expiries:
            return []

        # Bounds per expiry against that expiry's own tau, then the union of what
        # each expiry wants, so a strike is resolved once for the whole month.
        wanted: dict[tuple[str, str], set[float]] = {}
        for expiry in expiries:
            bounds = self._bounds_for(expiry, session, spot=spot, sigma=sigma)
            for right in RIGHTS:
                side = ladder.get(_LADDER_SIDE[right]) or []
                wanted[(expiry, right)] = set(select_strikes(side, bounds, cap=cap))

        rows: list[tuple[Any, ...]] = []
        for right in RIGHTS:
            union = sorted({s for (_, r), strikes in wanted.items() if r == right for s in strikes})
            resolved = await self._resolve_strikes(target, month, exchange, right, union)
            quotes = await self._market_data.quotes(sorted(resolved.values()))
            for (expiry, strike), option_conid in resolved.items():
                if strike not in wanted.get((expiry, right), ()):
                    continue
                rows.append(
                    self._row(
                        target,
                        session=session,
                        expiry=expiry,
                        strike=strike,
                        right=right,
                        option_conid=option_conid,
                        quote=quotes.get(option_conid),
                        sigma=sigma,
                        exchange=exchange,
                    )
                )
        return rows

    async def _resolve_strikes(
        self, target: ChainTarget, month: str, exchange: str, right: str, strikes: list[float]
    ) -> dict[tuple[str, float], int]:
        """Map ``(expiry, strike)`` to the option's own conid.

        One call per strike returns every expiry in the month at that strike,
        which is what makes the union approach worth taking.

        Keyed on the strike that was REQUESTED, not the one echoed back. The
        caller matches these against a set built from the same ladder, so keying
        on the echo would let a float that round-trips differently drop the row
        silently — the worst outcome available, since nothing downstream could
        tell a dropped strike from one that was never listed.
        """
        resolved: dict[tuple[str, float], int] = {}
        for strike in strikes:
            contracts = await self._market_data.option_contracts(
                target.contract_id, month=month, exchange=exchange, strike=strike, right=right
            )
            await pace(self._policy)
            for row in contracts:
                option_conid = safe_int(row.get("conid"), None)
                expiry = parse_date(row.get("maturityDate") or row.get("expiry"))
                if option_conid is None or expiry is None:
                    continue
                echoed = safe_float(row.get("strike"), None)
                if echoed is not None and not math.isclose(echoed, strike, rel_tol=STRIKE_REL_TOL):
                    log.warning(
                        "option contract echoed a different strike than was requested; "
                        "keeping the requested one",
                        extra={
                            "symbol": target.symbol,
                            "requested": strike,
                            "echoed": echoed,
                            "expiry": expiry.isoformat(),
                        },
                    )
                resolved[(expiry.isoformat(), strike)] = option_conid
        return resolved

    # ── helpers ──

    async def _underlying_quote(self, target: ChainTarget) -> Quote | None:
        """The underlying's own quote, on the richer underlying field set.

        This call happens anyway, to centre the strike band. Keeping the result
        is what makes an IV percentile self-computable later: per-strike implied
        vol history cannot be retrieved retrospectively, and neither can the
        underlying's.
        """
        quotes = await self._market_data.underlying_quotes([target.contract_id])
        await pace(self._policy)
        return quotes.get(target.contract_id)

    def _write_underlying_snapshot(
        self, target: ChainTarget, session: date, quote: Quote | None, spot: float | None
    ) -> None:
        """Keep the underlying quote rather than discarding it after sizing the band.

        ``iv_percentile_52w`` is NULL until a verified Client Portal field id
        exists for it — see ibkr.cp_api.marketdata.underlying_snapshot_fields. A
        null is a coverage gap the grid reports; a guessed field id would write
        some other quantity into a percentile column and nobody would see it.
        """
        if quote is None and spot is None:
            return
        with self._store.transaction() as connection:
            # The basis travels with the number or not at all. A yield with no
            # basis beside it is exactly the undated-figure problem 009 named, one
            # level down: nobody could later tell a trailing figure from a
            # continuous one, and only one of the two is the model's q.
            yield_value = quote.dividend_yield if quote else None
            connection.execute(
                """
                INSERT INTO underlying_snapshots (
                    snapshot_date, contract_id, last, bid, ask, volume, implied_vol,
                    iv_percentile_52w, historical_vol_30d, source, collected_at,
                    market_data_status, dividend_yield, dividend_yield_basis)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (snapshot_date, contract_id) DO UPDATE SET
                    last = excluded.last, bid = excluded.bid, ask = excluded.ask,
                    volume = excluded.volume, implied_vol = excluded.implied_vol,
                    iv_percentile_52w = excluded.iv_percentile_52w,
                    historical_vol_30d = excluded.historical_vol_30d,
                    source = excluded.source, collected_at = excluded.collected_at,
                    market_data_status = excluded.market_data_status,
                    dividend_yield = excluded.dividend_yield,
                    dividend_yield_basis = excluded.dividend_yield_basis
                """,
                (
                    session.isoformat(),
                    target.contract_id,
                    quote.last if quote else spot,
                    quote.bid if quote else None,
                    quote.ask if quote else None,
                    quote.volume if quote else None,
                    quote.implied_vol if quote else None,
                    quote.iv_percentile_52w if quote else None,
                    quote.historical_vol_30d if quote else None,
                    self._market_data.source,
                    utc_stamp(),
                    quote.market_data_status if quote else None,
                    yield_value,
                    DIVIDEND_YIELD_BASIS if yield_value is not None else None,
                ),
            )

    def _spot_for(self, target: ChainTarget, quote: Quote | None) -> float | None:
        """Spot from the snapshot, else the most recent stored daily close.

        Index contracts return an empty snapshot, per the playbook, so the bar
        fallback is not a nicety. What it never does is invent a price.
        """
        if quote is not None and quote.reference_price is not None:
            return quote.reference_price
        row = self._store.execute(
            "SELECT close FROM bars WHERE contract_id = ? AND freq = ? AND close IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1",
            (target.contract_id, FREQ_DAILY),
        ).fetchone()
        return float(row[0]) if row else None

    def _exchange_for(self, target: ChainTarget, exchanges: tuple[str, ...]) -> str:
        """The venue this underlying's options are requested from.

        **A declared ``option_exchange`` wins outright, and is not checked against
        the list the search returned.** That inversion is the point. The exchange
        list is a property of whichever listing ``/iserver/secdef/search`` matched,
        and the whole defect this fixes is that it can describe a different
        instrument from the conid being queried: a European share whose options
        are listed on EUREX or MONEP came back carrying ``SMART;AMEX;BATS;...``,
        which is a true statement about the New York line and a false one here.
        Treating that list as authoritative is what sent SMART and collected
        nothing.

        Undeclared, the behaviour is unchanged: the row's own exchange when the
        chain lists it — futures and index options need the NATIVE venue, SMART
        returns empty — then whatever the chain reports first.
        """
        if target.option_exchange:
            if exchanges and target.option_exchange not in exchanges:
                log.warning(
                    "the declared option exchange is not in the list the search "
                    "returned; sending the declared one",
                    extra={
                        "symbol": target.symbol,
                        "conid": target.contract_id,
                        "declared": target.option_exchange,
                        "search_reported": list(exchanges),
                    },
                )
            return target.option_exchange
        if target.exchange and target.exchange in exchanges:
            return target.exchange
        if exchanges:
            return exchanges[0]
        if target.exchange:
            return target.exchange
        raise ValueError(f"no option exchange known for {target.symbol}")

    def _bounds_for(self, expiry: str, session: date, *, spot: float, sigma: Sigma):
        parsed = parse_date(expiry)
        if parsed is None:  # pragma: no cover - expiries() already parsed it
            raise ValueError(f"unparseable expiry reached the bounds calculation: {expiry!r}")
        tau = diffusion_tau_years(
            (parsed - session).days,
            trading_days_per_year=self._trading_days_per_year,
            calendar_days_per_year=self._calendar_days_per_year,
        )
        return strike_bounds(spot=spot, sigma=sigma.value, tau_years=tau, k=self._k)

    def _is_degraded(self, target: ChainTarget, sigma: Sigma, session: date) -> bool:
        """A fallback sigma is a warning unless the instrument is genuinely new."""
        if sigma.is_stale:
            log.warning(
                "STALE REALISED VOL: the strike band was sized from daily bars older "
                "than collectors.chain.bars_freshness_days. Enough closes to satisfy "
                "the tenor says nothing about when they were, so the primary rung can "
                "succeed on a series that stopped updating. Check the backfill.",
                extra={"symbol": target.symbol, "sigma_source": sigma.source},
            )
            return True
        if not sigma.is_fallback or not self._warn_on_fallback:
            return False
        listed = parse_date(target.listed_on)
        if listed is not None and (session - listed).days <= self._new_listing_grace_days:
            log.info(
                "sigma fallback on a recent listing; expected, not degraded",
                extra={
                    "symbol": target.symbol,
                    "sigma_source": sigma.source,
                    "listed_on": target.listed_on,
                },
            )
            return False
        log.warning(
            "SIGMA FALLBACK on an established instrument: the strike band was sized "
            "from a weaker source than the primary realised-vol rung. The daily-bar "
            "backfill is not keeping up.",
            extra={
                "symbol": target.symbol,
                "sigma_source": sigma.source,
                "sigma": sigma.value,
                "listed_on": target.listed_on,
            },
        )
        return True

    def _row(
        self,
        target: ChainTarget,
        *,
        session: date,
        expiry: str,
        strike: float,
        right: str,
        option_conid: int,
        quote: Quote | None,
        sigma: Sigma,
        exchange: str,
    ) -> tuple[Any, ...]:
        return (
            session.isoformat(),
            target.contract_id,
            expiry,
            float(strike),
            right,
            option_conid,
            quote.bid if quote else None,
            quote.ask if quote else None,
            quote.last if quote else None,
            quote.implied_vol if quote else None,
            quote.delta if quote else None,
            quote.open_interest if quote else None,
            quote.volume if quote else None,
            self._market_data.source,
            utc_stamp(),
            sigma.value,
            sigma.source,
            quote.market_data_status if quote else None,
            # The VENUE, beside `source` which is the ROUTE. A route says who was
            # asked and a venue says what was answered, and only the second tells
            # a European chain from a US one collected under a European row.
            exchange,
        )

    def _write(self, rows: list[tuple[Any, ...]]) -> None:
        """Append-only across days, idempotent within one.

        The primary key is ``(snapshot_date, underlying_id, expiry, strike, right)``,
        so re-running today updates today's rows and leaves every previous day
        untouched.
        """
        if not rows:
            return
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO chain_snapshots (
                    snapshot_date, underlying_id, expiry, strike, right, contract_id,
                    bid, ask, last, iv, delta, open_interest, volume, source,
                    collected_at, sigma_used, sigma_source, market_data_status,
                    option_exchange)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (snapshot_date, underlying_id, expiry, strike, right) DO UPDATE SET
                    contract_id = excluded.contract_id,
                    bid = excluded.bid, ask = excluded.ask, last = excluded.last,
                    iv = excluded.iv, delta = excluded.delta,
                    open_interest = excluded.open_interest, volume = excluded.volume,
                    source = excluded.source, collected_at = excluded.collected_at,
                    sigma_used = excluded.sigma_used, sigma_source = excluded.sigma_source,
                    market_data_status = excluded.market_data_status,
                    option_exchange = excluded.option_exchange
                """,
                rows,
            )
