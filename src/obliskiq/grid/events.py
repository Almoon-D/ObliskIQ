"""The fact sheet's events: what the SEC's filings say is happening to a listed company, dated
(MASTER_PLAN §5.5; DECISIONS D189, D200). A view of the events collector's store, read as of a
date; it sizes nothing and weighs nothing.

**The names.** The companies of the newest listing snapshot by ``as_of`` that pass the stock
screen's floors (``grid/stock_screen.py: floor_passing``), each by its most traded ticker.

**The lines.** For each such company, every event filed in the last ``events_window_days`` up to
``as_of``: an 8-K's kept items (``sources.filings.items``), a registration form's kind, an initial
public offering's pricing with the terms its prospectus states; and every stated lock-up that
ends in the next ``events_window_days``, dated the day its prospectus was dated (a line is a fact
known on the sheet's date, never a forecast). Each line opens with its kind (``results: BJ ...``),
newest first, at most ``max_event_lines``. An event no free source states is one ``no_source``
line, never inferred.

Every read is bounded by ``as_of`` (``known_at``): what was collected after it changes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

from ..config.schema import ObliskConfig
from ..domain.filings import Filing, IpoTerms
from ..domain.stocks import StockListing
from ..store import filings as filings_repo
from ..store.db import Store
from ..store.stocks import HeldBar
from .stock_screen import floor_passing

#: The status of an event no free source states.
NO_SOURCE: Final = "no_source"


@dataclass(frozen=True, slots=True)
class EventLine:
    """One dated event of a listed name: the day it is of, its kind and what it says."""

    day: date
    kind: str
    ticker: str
    text: str
    #: A lock-up that ends after the sheet's date.
    ahead: bool = False


@dataclass(frozen=True, slots=True)
class EventView:
    """The events as of ``as_of``: the names they cover, the lines and each name's last close."""

    as_of: date
    names: int
    lines: tuple[EventLine, ...] = field(default_factory=tuple)
    closes: dict[str, float] = field(default_factory=dict)

    @property
    def ahead(self) -> int:
        return sum(1 for line in self.lines if line.ahead)


class _Events:
    def __init__(self, store: Store, settings: ObliskConfig, as_of: date) -> None:
        self._store = store
        self._config = settings.sources.filings
        self._window = timedelta(days=settings.llm.analyst.fact_sheet.events_window_days)
        self._settings = settings
        self._as_of = as_of

    def build(self) -> EventView:
        as_of = self._as_of
        passing = floor_passing(self._store, self._settings, as_of=as_of)
        terms = filings_repo.ipo_terms(self._store, as_of=as_of)
        found: list[EventLine] = []
        for filing in filings_repo.filings(self._store, as_of=as_of, since=as_of - self._window):
            name = passing.get(filing.cik)
            if name is not None:
                found += self._filed(filing, name[0], terms.get(filing.accession))
        for held in terms.values():
            name = passing.get(held.cik)
            ends = held.lockup_ends
            if name is not None and ends is not None and as_of < ends <= as_of + self._window:
                found.append(self._lockup(held, name[0]))
        found.sort(key=lambda line: (line.ahead, line.day, line.ticker, line.kind), reverse=True)
        lines = tuple(found)
        closes = {listing.ticker: last.close for listing, last in _covered(passing, lines)}
        return EventView(as_of, names=len(passing), lines=lines, closes=closes)

    def _filed(
        self, filing: Filing, listing: StockListing, terms: IpoTerms | None
    ) -> list[EventLine]:
        config = self._config
        head = f"{listing.ticker} ({listing.name}) {filing.form} filed {filing.filed.isoformat()}"
        if filing.form in config.item_forms:
            return [
                EventLine(filing.filed, kind, listing.ticker, f"{kind}: {head}, item {item}")
                for item in filing.items or ()
                if (kind := config.items.get(item)) is not None
            ]
        kind = config.registration_forms.get(filing.form)
        if kind is not None:
            return [EventLine(filing.filed, kind, listing.ticker, f"{kind}: {head}")]
        if terms is None:
            return []  # not read yet, or not an initial public offering's
        kind = config.prospectus_kind
        price = "not stated" if terms.price is None else str(terms.price)
        shares = "not stated" if terms.shares is None else f"{terms.shares}"
        lockup = (
            "no lock-up stated"
            if terms.lockup_days is None or terms.lockup_ends is None
            else f"shortest lock-up stated {terms.lockup_days} days, ends "
            f"{terms.lockup_ends.isoformat()}"
        )
        return [
            EventLine(
                filing.filed,
                kind,
                listing.ticker,
                f"{kind}: {head}, initial public offering at {price} a share, {shares} shares "
                f"offered, {lockup}",
            )
        ]

    def _lockup(self, terms: IpoTerms, listing: StockListing) -> EventLine:
        assert terms.lockup_ends is not None
        assert terms.lockup_days is not None
        kind = self._config.lockup_kind
        dated = min(terms.prospectus_day or terms.known_at.date(), self._as_of)
        return EventLine(
            dated,
            kind,
            listing.ticker,
            f"{kind}: {listing.ticker} ({listing.name}) the shortest lock-up its initial public "
            f"offering's prospectus states ({terms.lockup_days} days, stated) ends "
            f"{terms.lockup_ends.isoformat()}",
            ahead=True,
        )


def _covered(
    passing: dict[int, tuple[StockListing, HeldBar]], lines: tuple[EventLine, ...]
) -> list[tuple[StockListing, HeldBar]]:
    tickers = {line.ticker for line in lines}
    return [pair for pair in passing.values() if pair[0].ticker in tickers]


def event_view(store: Store, settings: ObliskConfig, *, as_of: date) -> EventView:
    """The events as of ``as_of`` (the store worker's entry point)."""
    return _Events(store, settings, as_of).build()


def events_summary(store: Store, settings: ObliskConfig, *, as_of: date) -> str:
    """One line for the digest: the events of the last window and those ahead."""
    view = event_view(store, settings, as_of=as_of)
    window = settings.llm.analyst.fact_sheet.events_window_days
    missing = ", ".join(settings.sources.filings.no_source)
    return (
        f"events: {len(view.lines) - view.ahead} filed in the last {window} days and "
        f"{view.ahead} ahead in the next {window} (lock-ups), of {view.names} names passing the "
        f"floors; {NO_SOURCE}: {missing}"
    )
