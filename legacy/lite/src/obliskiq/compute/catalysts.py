"""The catalyst calendar: dated events the store can actually defend.

Every event here is derived from something already written down — a mandatory
close date computed at entry, a time window pre-committed at entry, an option
expiry from a collected chain, a catalyst cited in a logged re-underwrite. None
of it is recalled, and none of it is fetched: the computation layer touches no
network, so this module cannot confirm an earnings date and does not pretend to.

That refusal is the point. SKILL.md's failure-mode list carries "Recalled event
dates — an earnings date was stated five days early on the most time-sensitive
position in the book". A calendar that quietly filled the gap with a plausible
date would reproduce exactly that error with more authority behind it. Instead
the classes this layer cannot verify are returned, named, alongside the events
it can, so the session knows precisely what it still has to confirm against
IBKR and company IR.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import Config
from ..logging_setup import get_logger
from ..store.db import Store
from ..store.ledger import Position, open_positions
from ..util.timeutil import iso_date, parse_date
from .frames import universe_row

log = get_logger("compute.catalysts")

#: Event kinds this layer derives. Each names the row it came from, so a reader
#: can trace any date back to what was written down rather than to a claim.
KIND_MANDATORY_CLOSE = "mandatory_close"
KIND_TIME_WINDOW = "time_window_end"
KIND_REUNDERWRITE = "reunderwrite_catalyst"
KIND_OPTION_EXPIRY = "option_expiry"

#: Where an event came from. ``ledger`` is a pre-commitment made at entry;
#: ``chain_snapshot`` is a contract term the collectors observed.
SOURCE_LEDGER = "position_ledger"
SOURCE_REUNDERWRITE = "position_ledger.reunderwrite_log"
SOURCE_CHAIN = "chain_snapshots"

#: Event classes no offline source can confirm. Returned by name on every call
#: so the gap is visible rather than inferred from an empty calendar.
UNVERIFIABLE_CLASSES: tuple[tuple[str, str], ...] = (
    (
        "earnings",
        "Confirm against company investor relations. IBKR's calendar has been wrong "
        "in this book before, and a date recalled rather than checked is how the most "
        "time-sensitive position in the book got a date five days early.",
    ),
    (
        "index_events",
        "Confirm against the index provider, INCLUDING the mechanical execution "
        "timing: trackers rebalance in the closing auction of the day before an "
        "effective date, so the actionable window is earlier than the headline.",
    ),
    (
        "central_bank",
        "Confirm against the central bank's own published schedule. The macro store "
        "holds release dates for series already published, never future meeting dates.",
    ),
    (
        "corporate_actions",
        "Confirm against the issuer. Dividends, splits and tender deadlines are not "
        "collected by any collector in this pipeline.",
    ),
)


@dataclass(frozen=True)
class CatalystEvent:
    """One dated event, and the row it was derived from."""

    event_date: date
    kind: str
    source: str
    contract_id: int | None
    symbol: str | None
    position_id: int | None
    detail: str
    days_away: int

    @property
    def is_past(self) -> bool:
        return self.days_away < 0


@dataclass(frozen=True)
class UnverifiableClass:
    """An event class this layer cannot confirm, and where to confirm it."""

    kind: str
    note: str


@dataclass(frozen=True)
class CatalystCalendar:
    """Verified dated events, and an honest account of what is missing."""

    as_of: date
    through: date
    events: tuple[CatalystEvent, ...]
    unverifiable: tuple[UnverifiableClass, ...]
    #: Positions with no dated event at all inside the window. A position whose
    #: only exit is a price level has no calendar, and that is worth saying.
    positions_without_events: tuple[int, ...]
    truncated: bool = False

    def summary(self) -> str:
        if not self.events:
            return (
                f"no dated events between {iso_date(self.as_of)} and {iso_date(self.through)} "
                "derive from the ledger or the collected chains. This is a coverage "
                "statement, not a quiet calendar: earnings, index and central-bank dates "
                "are not held by any collector and must be confirmed at source."
            )
        return (
            f"{len(self.events)} dated event(s) between {iso_date(self.as_of)} and "
            f"{iso_date(self.through)}, every one derived from a ledger row or a collected "
            f"contract term. {len(self.unverifiable)} event class(es) remain unverifiable "
            "offline and must be confirmed at source."
        )


class CatalystCalendarBuilder:
    """Assembles the calendar from the ledger and the collected chains."""

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config
        self._entry_dte_max = config.require_int("tenor.entry_dte_max")

    def build(
        self,
        *,
        as_of: date,
        through_days: int | None = None,
        watchlist: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> CatalystCalendar:
        """Every derivable event from today out to the longest hold window.

        ``through_days`` defaults to ``tenor.entry_dte_max`` — the longest hold
        an entry today could still have — because an event past the last date any
        open position can survive to is not a catalyst for this book.
        """
        horizon = through_days if through_days is not None else self._entry_dte_max
        through = as_of + timedelta(days=horizon)

        positions = open_positions(self._store)
        events: list[CatalystEvent] = []
        seen_positions: set[int] = set()

        for position in positions:
            found = self._position_events(position, as_of=as_of, through=through)
            events.extend(found)
            if found:
                seen_positions.add(position.position_id)

        events.extend(
            self._expiry_events(
                as_of=as_of,
                through=through,
                positions=positions,
                watchlist=watchlist,
            )
        )

        events.sort(key=lambda event: (event.event_date, event.kind, event.symbol or ""))
        truncated = False
        if limit is not None and limit > 0 and len(events) > limit:
            events = events[:limit]
            truncated = True

        without = tuple(
            sorted(p.position_id for p in positions if p.position_id not in seen_positions)
        )
        return CatalystCalendar(
            as_of=as_of,
            through=through,
            events=tuple(events),
            unverifiable=tuple(
                UnverifiableClass(kind=kind, note=note) for kind, note in UNVERIFIABLE_CLASSES
            ),
            positions_without_events=without,
            truncated=truncated,
        )

    # ── internals ──

    def _position_events(
        self, position: Position, *, as_of: date, through: date
    ) -> list[CatalystEvent]:
        symbol = self._symbol(position.contract_id)
        events: list[CatalystEvent] = []

        for raw, kind, source, detail in (
            (
                position.mandatory_close_date,
                KIND_MANDATORY_CLOSE,
                SOURCE_LEDGER,
                "mandatory close date set at entry; the exit is controlled rather than "
                "inherited from the OCC or a forced liquidation",
            ),
            (
                position.time_window_end,
                KIND_TIME_WINDOW,
                SOURCE_LEDGER,
                "time window pre-committed at entry; reaching it is an exit reason in "
                "its own right",
            ),
        ):
            when = parse_date(str(raw or ""))
            if when is None or not (as_of <= when <= through):
                continue
            events.append(
                CatalystEvent(
                    event_date=when,
                    kind=kind,
                    source=source,
                    contract_id=position.contract_id,
                    symbol=symbol,
                    position_id=position.position_id,
                    detail=detail,
                    days_away=(when - as_of).days,
                )
            )

        opened = parse_date(position.opened_at)
        for entry in position.reunderwrite_log:
            when = parse_date(str(entry.get("catalyst_date") or ""))
            catalyst = str(entry.get("catalyst") or "").strip()
            if when is None or not catalyst:
                continue
            if opened is not None and when <= opened:
                continue
            if not (as_of <= when <= through):
                continue
            events.append(
                CatalystEvent(
                    event_date=when,
                    kind=KIND_REUNDERWRITE,
                    source=SOURCE_REUNDERWRITE,
                    contract_id=position.contract_id,
                    symbol=symbol,
                    position_id=position.position_id,
                    detail=f"logged re-underwrite cites: {catalyst}",
                    days_away=(when - as_of).days,
                )
            )
        return events

    def _expiry_events(
        self,
        *,
        as_of: date,
        through: date,
        positions: Sequence[Position],
        watchlist: Sequence[str] | None,
    ) -> list[CatalystEvent]:
        """Option expiries for held contracts and any named watchlist symbols.

        An expiry is a contract term the collectors observed, not a forecast —
        which is why it belongs here and an earnings date does not.
        """
        wanted: set[int] = {p.contract_id for p in positions}
        wanted |= self._watchlist_ids(watchlist)
        if not wanted:
            return []

        placeholders = ", ".join("?" for _ in wanted)
        rows = self._store.execute(
            f"SELECT DISTINCT underlying_id, expiry FROM chain_snapshots "
            f"WHERE underlying_id IN ({placeholders}) AND expiry >= ? AND expiry <= ? "
            "ORDER BY expiry, underlying_id",
            [*sorted(wanted), iso_date(as_of), iso_date(through)],
        ).fetchall()

        held = {p.contract_id: p.position_id for p in positions}
        events: list[CatalystEvent] = []
        for row in rows:
            when = parse_date(str(row["expiry"]))
            if when is None:
                continue
            underlying = int(row["underlying_id"])
            events.append(
                CatalystEvent(
                    event_date=when,
                    kind=KIND_OPTION_EXPIRY,
                    source=SOURCE_CHAIN,
                    contract_id=underlying,
                    symbol=self._symbol(underlying),
                    position_id=held.get(underlying),
                    detail=(
                        "collected option expiry. Any position on it must be closed "
                        "before this date, not on it"
                    ),
                    days_away=(when - as_of).days,
                )
            )
        return events

    def _watchlist_ids(self, watchlist: Sequence[str] | None) -> set[int]:
        if not watchlist:
            return set()
        symbols = {symbol.strip().upper() for symbol in watchlist if symbol.strip()}
        if not symbols:
            return set()
        placeholders = ", ".join("?" for _ in symbols)
        rows = self._store.execute(
            f"SELECT contract_id FROM universe WHERE UPPER(symbol) IN ({placeholders})",
            sorted(symbols),
        ).fetchall()
        return {int(row[0]) for row in rows}

    def _symbol(self, contract_id: int) -> str | None:
        row = universe_row(self._store, contract_id)
        if not row:
            return None
        symbol = row.get("symbol")
        return None if symbol is None else str(symbol)
