"""The stock screen: an untested ranking of today's listed US common stocks for the thesis book's
fact sheet (MASTER_PLAN §5.5; DECISIONS D180). It sizes nothing, has no weight in any gate and no
performance record of its own: it is a view of the stocks collector's store, read as of a date.

**The universe.** The names of the newest listing snapshot collected by ``as_of`` that pass the
account's floors, the thesis book's own (``thesis.liquidity``, ``costs``): a position of
``stocks.screen.position_usd`` is at most ``max_share_of_daily_value`` of the name's average
daily traded value over ``sessions`` held bars, and one leg's commission on the configured
schedule is at most the spread the cost model charges the leg (the illiquid tier: the screen
knows no market capitalisation). A name whose last close is older than the sheet's
``price_stale_after_days`` passes nothing. A company with several listed tickers is one line,
its most traded one.

**The ranking.** The mean of three percentile ranks: cheapness (diluted earnings per share over
the last close), quality (gross profit over assets) and the trend of the name's industry (the
12-1 return, ``trend_months`` back skipping the last ``skip_months``, of the names of its SIC
division, equally weighted). A fundamental is the newest value read whose period ended within
``stocks.fundamentals.max_age_days``. A name's return is its sessions' chained changes over the
window, from a bar held before it; a name that left the listings counts to its last bar, so the
industry is not of survivors only; a name whose chain breaks counts for nothing. A name lacking
any input is ranked on none of them and counted on one line.

**Maturity.** While the store holds no bar from before the window's start, the industry trend is
``immature`` and so is the whole view: no ranking. A name's first read loads the window's history
(``stocks.bars.first_read_days``; DECISIONS D190), so the view matures with the first pass. Until
the window starts on or after the first listing snapshot, that history is of the names listed
then, survivors only: the view says so, with the day it ends (``survivors_only_until``).

Every read is bounded by ``as_of`` (``known_at``): what was collected after it changes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Final

from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass
from ..domain.stocks import StockFact, StockListing
from ..quant.costs import CostBook, OrderTerms
from ..store import stocks as stocks_repo
from ..store.db import Store
from ..store.stocks import HeldBar

#: The currency the screen's position and the SEC's listings trade in.
USD: Final = "USD"
MONTHS_PER_YEAR: Final = 12


class ScreenStatus(StrEnum):
    OK = "ok"
    IMMATURE = "immature"
    NO_DATA = "no_data"


@dataclass(frozen=True, slots=True)
class ScreenLine:
    """One ranked name and the inputs it was ranked on."""

    rank: int
    ticker: str
    name: str
    division: str
    earnings_yield: float
    gross_profitability: float
    industry_trend: float
    score: float
    close: float
    session: date


@dataclass(frozen=True, slots=True)
class StockScreen:
    """The screen as of ``as_of``: its status, why, and the ranked names when ``ok``."""

    as_of: date
    status: ScreenStatus
    reason: str
    snapshot_at: datetime | None = None
    listed: int = 0
    passing: int = 0
    lacking: int = 0
    lines: tuple[ScreenLine, ...] = field(default_factory=tuple)
    #: The first listing snapshot's day, and the day from which the trend window lies inside the
    #: collection; set while the window reaches back into loaded history (survivors only).
    collected_from: date | None = None
    survivors_only_until: date | None = None


#: The inputs a name is ranked on, each a percentile rank; the score is their mean.
RANKED_ON: Final = ("earnings_yield", "gross_profitability", "industry_trend")


@dataclass(frozen=True, slots=True)
class _Ranked:
    listing: StockListing
    last: HeldBar
    division: str
    earnings_yield: float
    gross_profitability: float
    industry_trend: float


def months_before(day: date, months: int) -> date:
    """The same day ``months`` calendar months earlier (the month's last day where it is
    shorter); later for a negative ``months``."""
    index = day.year * MONTHS_PER_YEAR + day.month - 1 - months
    year, month = divmod(index, MONTHS_PER_YEAR)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def percentile_ranks(values: Sequence[float]) -> list[float]:
    """Each value's share of the values at or below it, ties at their mean rank, in (0, 1]."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        mean_rank = (start + end) / 2 + 1
        for i in order[start : end + 1]:
            ranks[i] = mean_rank / len(values)
        start = end + 1
    return ranks


def _by_symbol(bars: Sequence[HeldBar]) -> dict[str, list[HeldBar]]:
    grouped: dict[str, list[HeldBar]] = defaultdict(list)
    for bar in bars:
        grouped[bar.symbol].append(bar)
    return grouped


def window_return(bars: Sequence[HeldBar]) -> float | None:
    """The chained return of a name's bars in the window; ``None`` when the chain breaks (or
    starts inside the window) or the window holds no bar of it."""
    if not bars:
        return None
    growth = 1.0
    for bar in bars:
        if bar.change is None:
            return None
        growth *= 1 + bar.change
    return growth - 1


class _Screen:
    def __init__(self, store: Store, settings: ObliskConfig, as_of: date) -> None:
        self._store = store
        self._settings = settings
        self._as_of = as_of
        self._config = settings.sources.stocks.screen
        self._costs = CostBook.from_config(settings)

    def build(self) -> StockScreen:
        as_of, store = self._as_of, self._store
        snapshot = stocks_repo.snapshot_as_of(store, as_of=as_of)
        if snapshot is None:
            return StockScreen(as_of, ScreenStatus.NO_DATA, "no listing snapshot is held")
        names = stocks_repo.listed(store, snapshot)
        start = months_before(as_of, self._config.trend_months)
        end = months_before(as_of, self._config.skip_months)
        first = stocks_repo.first_session(store, as_of=as_of)
        if first is None or first > start:
            held = "no bar" if first is None else f"bars from {first.isoformat()}"
            return StockScreen(
                as_of,
                ScreenStatus.IMMATURE,
                f"the industry trend needs {self._config.trend_months} months of bars (from "
                f"{start.isoformat()}); {held} held",
                snapshot.known_at,
                listed=len(names),
            )
        passing = self._passing(names)
        trends = self._industry_trends(start, end)
        inputs = self._inputs(passing, trends)
        ranked = self._rank(inputs)
        began = stocks_repo.snapshot_as_of(store, as_of=as_of, first=True) or snapshot
        collected_from = began.known_at.date()
        loaded = start < collected_from
        return StockScreen(
            as_of,
            ScreenStatus.OK,
            "",
            snapshot.known_at,
            listed=len(names),
            passing=len(passing),
            lacking=len(passing) - len(ranked),
            lines=tuple(ranked),
            collected_from=collected_from if loaded else None,
            survivors_only_until=(
                months_before(collected_from, -self._config.trend_months) if loaded else None
            ),
        )

    # ── the floors ──

    def _passing(self, names: Sequence[StockListing]) -> list[tuple[StockListing, HeldBar, float]]:
        """The names that pass the floors with their last bar and average traded value, one per
        company (its most traded listing)."""
        liquidity = self._settings.thesis.liquidity
        stale = timedelta(days=self._settings.llm.analyst.fact_sheet.price_stale_after_days)
        recent = _by_symbol(
            stocks_repo.recent_bars(self._store, sessions=liquidity.sessions, as_of=self._as_of)
        )
        position = self._config.position_usd
        best: dict[int, tuple[StockListing, HeldBar, float]] = {}
        for listing in names:
            bars = recent.get(listing.ticker, [])
            traded = [b.close * b.volume for b in bars if b.volume is not None]
            if len(traded) < liquidity.sessions or self._as_of - bars[-1].session > stale:
                continue
            value = sum(traded) / len(traded)
            last = bars[-1]
            if position > liquidity.max_share_of_daily_value * Decimal(repr(value)):
                continue
            if not self._commission_passes(listing.venue, Decimal(repr(last.close))):
                continue
            held = best.get(listing.cik)
            if held is None or value > held[2]:
                best[listing.cik] = (listing, last, value)
        return sorted(best.values(), key=lambda found: found[0].ticker)

    def _commission_passes(self, venue: str, price: Decimal) -> bool:
        """One leg's commission is at most the spread the cost model charges that leg."""
        quantity = (self._config.position_usd / price).to_integral_value(ROUND_FLOOR)
        if quantity < 1:
            return False
        estimate = self._costs.commission(
            OrderTerms(
                venue=venue,
                asset_class=AssetClass.STOCK,
                currency=USD,
                quantity=quantity,
                price=price,
            )
        )
        if estimate.amount is None:
            return False
        equity = self._settings.costs.friction.equity
        spread = (
            equity.liquid_spread_fraction
            if self._costs.is_liquid_share(None)
            else equity.illiquid_spread_fraction
        )
        return estimate.amount <= spread * quantity * price

    # ── the inputs ──

    def _industry_trends(self, start: date, end: date) -> dict[str, float]:
        """Each SIC division's equally weighted 12-1 return over the names with a whole chain
        in the window, one listing per company (its most traded in the window)."""
        window = [
            bar
            for bar in stocks_repo.bars_since(
                self._store, since=start + timedelta(days=1), as_of=self._as_of
            )
            if bar.session <= end
        ]
        ciks = stocks_repo.symbol_ciks(self._store, as_of=self._as_of)
        sics = stocks_repo.company_sics(self._store, as_of=self._as_of)
        chosen: dict[int, tuple[float, float]] = {}
        for symbol, bars in _by_symbol(window).items():
            cik = ciks.get(symbol)
            growth = window_return(bars)
            if cik is None or growth is None:
                continue
            traded = sum(b.close * b.volume for b in bars if b.volume is not None)
            if cik not in chosen or traded > chosen[cik][1]:
                chosen[cik] = (growth, traded)
        members: dict[str, list[float]] = defaultdict(list)
        for cik, (growth, _) in chosen.items():
            division = self._config.division(sics.get(cik))
            if division is not None:
                members[division.code].append(growth)
        return {code: sum(found) / len(found) for code, found in members.items()}

    def _latest(self, facts: Sequence[StockFact]) -> dict[tuple[int, str], StockFact]:
        """Each company's newest value of each input whose period is recent enough."""
        oldest = self._as_of - timedelta(
            days=self._settings.sources.stocks.fundamentals.max_age_days
        )
        found: dict[tuple[int, str], StockFact] = {}
        for fact in facts:
            if fact.period_end < oldest or fact.period_end > self._as_of:
                continue
            key = (fact.cik, fact.input)
            held = found.get(key)
            if held is None or fact.period_end > held.period_end:
                found[key] = fact
        return found

    def _inputs(
        self,
        passing: Sequence[tuple[StockListing, HeldBar, float]],
        trends: Mapping[str, float],
    ) -> list[_Ranked]:
        """The passing names with all three inputs."""
        latest = self._latest(stocks_repo.facts(self._store, as_of=self._as_of))
        sics = stocks_repo.company_sics(self._store, as_of=self._as_of)
        complete = []
        for listing, last, _ in passing:
            eps = latest.get((listing.cik, "earnings_per_share"))
            profit = latest.get((listing.cik, "gross_profit"))
            assets = latest.get((listing.cik, "assets"))
            division = self._config.division(sics.get(listing.cik))
            trend = None if division is None else trends.get(division.code)
            if eps is None or profit is None or assets is None or assets.value <= 0:
                continue
            if division is None or trend is None:
                continue
            complete.append(
                _Ranked(
                    listing,
                    last,
                    division.code,
                    earnings_yield=eps.value / last.close,
                    gross_profitability=profit.value / assets.value,
                    industry_trend=trend,
                )
            )
        return complete

    @staticmethod
    def _rank(inputs: Sequence[_Ranked]) -> list[ScreenLine]:
        if not inputs:
            return []
        ranks = [percentile_ranks([getattr(r, name) for r in inputs]) for name in RANKED_ON]
        scored = sorted(
            (
                (sum(column[i] for column in ranks) / len(RANKED_ON), found)
                for i, found in enumerate(inputs)
            ),
            key=lambda pair: (-pair[0], pair[1].listing.ticker),
        )
        return [
            ScreenLine(
                rank=n,
                ticker=found.listing.ticker,
                name=found.listing.name,
                division=found.division,
                earnings_yield=found.earnings_yield,
                gross_profitability=found.gross_profitability,
                industry_trend=found.industry_trend,
                score=score,
                close=found.last.close,
                session=found.last.session,
            )
            for n, (score, found) in enumerate(scored, start=1)
        ]


def stock_screen(store: Store, settings: ObliskConfig, *, as_of: date) -> StockScreen:
    """The stock screen as of ``as_of`` (the store worker's entry point)."""
    return _Screen(store, settings, as_of).build()


def screen_summary(store: Store, settings: ObliskConfig, *, as_of: date) -> str:
    """One line: names listed, names that left since the first snapshot, the screen's status."""
    screen = stock_screen(store, settings, as_of=as_of)
    first = stocks_repo.snapshot_as_of(store, as_of=as_of, first=True)
    if first is None:
        return f"stock screen (untested, D180): {screen.status.value}, {screen.reason}"
    left = len(stocks_repo.left_listings(store, as_of=as_of))
    status = screen.status.value + (f" ({screen.reason})" if screen.reason else "")
    return (
        f"stock screen (untested, D180): {screen.listed} names listed, {left} left since "
        f"{first.known_at.date().isoformat()}; screen {status}"
    )
