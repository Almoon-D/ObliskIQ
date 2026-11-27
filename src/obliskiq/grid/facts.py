"""The analyst's fact sheet: one point-in-time text from the measurement store and the sources
(MASTER_PLAN §5.5, §5.7, §5.10).

Every line is numbered (``F1``, ``F2``, …) so a thesis can name the facts it rests on, and carries
its date (the session a measurement was built for, the day a report was released, a filing filed,
a snapshot observed or an item collected) and its status: ``ok``, ``stale``, ``n/a``,
``warming_up``, ``no_data`` as the reading says, or ``raw`` for a news item the extractor has not
read. The sections, in order:

* **instrument**: each universe instrument a thesis may name (a tradable candidate, pinned,
  described, on a venue with a calendar), its last close and its returns;
* **macro**: each macro-board cell with a measured leaf, every such leaf;
* **grid**: each equity-grid cell with one of ``fact_sheet.grid_leaves`` measured;
* **ranking**: the four ranked views of the newest grid;
* **positioning**: each configured CFTC contract's crowding;
* **market**: prediction markets, curated first, then the largest moves;
* **pulse**: each GDELT query's newest day against its history;
* **news**: the items collected in the lookback, newest first, with what the extractor read;
* **insider**: issuers whose officers or directors bought in the open market in the cluster window;
* **stock_screen**: the untested stock screen (``grid/stock_screen.py``; DECISIONS D180): a header
  that says it is no signal, then its first ``stock_screen_top`` names, each ending
  ``untested (D180)``, and the names it could not rank counted on one line; a screen that is
  immature or cannot be built is its status line and no name.

The section list (:data:`SECTIONS`) is part of the analyst's segment (``analyst_prompt_hash``):
theses written from a sheet with other sections are another group of the record. A sheet built
with fewer sections (the replay's, H15) opens with one line naming those it omits.

What is not measured is said, never filled: a view with nothing built is one ``n/a`` line, and the
cells without a measured leaf are counted on one. Every read is bounded by ``as_of``. The sheet
reads no account, position, balance or order: nothing in it identifies the account (a test).
Feed text (a title, a market's question) is untrusted: the prompt that carries the sheet escapes
it (``llm/analyst.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..data.events.readings import event_reading, pulse_reading
from ..data.insiders.clusters import cluster_inputs
from ..data.positioning.readings import positioning_reading
from ..domain.measurement import EntityKind, Measurement, MeasurementStatus
from ..domain.sources import ExtractionStatus, NewsItem
from ..store import measurements as measurements_repo
from ..store import repos
from ..store import sources as sources_repo
from ..store.db import Store
from .rankings import Rankings
from .stock_screen import ScreenStatus, stock_screen
from .views import equity_grid, macro_board

#: The prefix of a line's reference.
REF_PREFIX: Final = "F"
#: Significant digits a value is shown with.
DIGITS: Final = 4
#: The status of a news item no extraction has read.
RAW: Final = "raw"
#: The sheet's sections, in order.
SECTIONS: Final = (
    "instrument",
    "macro",
    "grid",
    "ranking",
    "positioning",
    "market",
    "pulse",
    "news",
    "insider",
    "stock_screen",
)
#: The section of the line that names the sections a sheet omits.
OMITTED: Final = "sheet"
#: The section of the stock screen, and what ends each of its names' lines.
SCREEN: Final = "stock_screen"
UNTESTED: Final = "untested (D180)"
#: The status of a screen that could not be built.
FAILED: Final = "failed"


@dataclass(frozen=True, slots=True)
class FactLine:
    """One dated fact: its reference, the day it is of, its status, its section and its text."""

    ref: str
    day: date
    status: str
    section: str
    text: str

    def render(self) -> str:
        return f"{self.ref} | {self.day.isoformat()} | {self.status} | {self.section} | {self.text}"


@dataclass(frozen=True, slots=True)
class FactSheet:
    """The facts as of ``as_of``, the instruments a thesis may name and the last close of each
    that has one on the sheet (a thesis's levels are judged against it)."""

    as_of: date
    lines: tuple[FactLine, ...]
    instruments: tuple[str, ...] = field(default_factory=tuple)
    closes: Mapping[str, float] = field(default_factory=dict)
    #: The stock screen's names on the sheet, by ticker, and each one's last close there: a
    #: thesis may name one as well as an instrument (DECISIONS D195).
    stocks: tuple[str, ...] = field(default_factory=tuple)
    stock_closes: Mapping[str, float] = field(default_factory=dict)

    def text(self) -> str:
        return "\n".join(line.render() for line in self.lines)

    @property
    def sheet_hash(self) -> str:
        """The sheet's identity: what a call that read it was given."""
        return hashlib.sha256(f"{self.as_of.isoformat()}\n{self.text()}".encode()).hexdigest()

    def line(self, ref: str) -> FactLine | None:
        wanted = ref.strip().upper()
        return next((line for line in self.lines if line.ref == wanted), None)


def number(value: float) -> str:
    """A value as the sheet shows it: four significant digits."""
    return f"{value:.{DIGITS}g}"


def _leaf_text(row: Measurement) -> str:
    assert row.value is not None  # measured leaves only
    stale = " (stale)" if row.status is MeasurementStatus.STALE else ""
    return f"{row.leaf} {number(row.value)}{stale}"


def _status_of(rows: Sequence[Measurement]) -> str:
    stale = any(row.status is MeasurementStatus.STALE for row in rows)
    return MeasurementStatus.STALE.value if stale else MeasurementStatus.OK.value


def nameable(settings: ObliskConfig) -> tuple[UniverseInstrument, ...]:
    """The universe instruments a thesis may name: what the intake would take (a tradable
    candidate, pinned and described, on a venue with a calendar)."""
    return tuple(
        declared
        for declared in settings.universe.instruments
        if declared.tradable
        and declared.conid is not None
        and declared.description is not None
        and declared.listing_exchange in settings.ibkr_exchanges
    )


class FactSheetBuilder:
    """Builds the sheet from one store, every read bounded by ``as_of``."""

    def __init__(self, store: Store, settings: ObliskConfig) -> None:
        self._store = store
        self._settings = settings
        self._config = settings.llm.analyst.fact_sheet
        self._drafts: list[tuple[date, str, str, str]] = []
        self._closes: dict[str, float] = {}
        self._stocks: dict[str, float] = {}

    def _add(self, day: date, status: str, section: str, text: str) -> None:
        self._drafts.append((day, status, section, text))

    def build(self, *, as_of: date, sections: Sequence[str] = SECTIONS) -> FactSheet:
        unknown = sorted(set(sections) - set(SECTIONS))
        if unknown:
            raise ValueError(f"the fact sheet has no section {', '.join(unknown)}")
        self._drafts = []
        self._closes = {}
        self._stocks = {}
        omitted = [name for name in SECTIONS if name not in sections]
        if omitted:
            self._add(
                as_of,
                MeasurementStatus.NA.value,
                OMITTED,
                f"this sheet omits {', '.join(omitted)}: they cannot be rebuilt as they stood on "
                "its date",
            )
        instruments = nameable(self._settings)
        steps = {
            "instrument": lambda: self._instruments(instruments, as_of),
            "macro": lambda: self._macro(as_of),
            "grid": lambda: self._grid(as_of),
            "ranking": lambda: self._rankings(as_of),
            "positioning": lambda: self._positioning(as_of),
            "market": lambda: self._markets(as_of),
            "pulse": lambda: self._pulses(as_of),
            "news": lambda: self._news(as_of),
            "insider": lambda: self._insiders(as_of),
            SCREEN: lambda: self._stock_screen(as_of),
        }
        for name in SECTIONS:
            if name in sections:
                steps[name]()
        lines = tuple(
            FactLine(f"{REF_PREFIX}{n}", day, status, section, text)
            for n, (day, status, section, text) in enumerate(self._drafts, start=1)
        )
        symbols = (
            tuple(declared.symbol for declared in instruments) if "instrument" in sections else ()
        )
        return FactSheet(
            as_of, lines, symbols, dict(self._closes), tuple(self._stocks), dict(self._stocks)
        )

    # ── the sections ──

    def _instruments(self, instruments: Sequence[UniverseInstrument], as_of: date) -> None:
        defensive = set(self._settings.thesis.defensive_roles)
        sessions = self._config.return_sessions
        stale_after = timedelta(days=self._config.price_stale_after_days)
        for declared in instruments:
            assert declared.conid is not None  # nameable instruments are pinned
            flags = ", defensive" if defensive & set(declared.exposures.core_roles) else ""
            head = (
                f"{declared.symbol}: {declared.description} ({declared.asset_class.value}, "
                f"{declared.currency}{flags})"
            )
            bars = repos.bar_series(
                self._store, declared.conid, as_of=as_of, limit=max(sessions) + 1
            )
            if not bars:
                self._add(as_of, MeasurementStatus.NA.value, "instrument", f"{head}: no close held")
                continue
            last = bars[-1]
            self._closes[declared.symbol] = last.close
            moves = []
            for count in sessions:
                if len(bars) > count and bars[-1 - count].close > 0:
                    move = last.close / bars[-1 - count].close - 1
                    moves.append(f"return over {count} sessions {number(move)}")
                else:
                    moves.append(f"return over {count} sessions n/a (too few closes)")
            status = (
                MeasurementStatus.OK
                if as_of - last.session_date <= stale_after
                else (MeasurementStatus.STALE)
            )
            self._add(
                last.session_date,
                status.value,
                "instrument",
                f"{head}: close {number(last.close)}; {'; '.join(moves)}",
            )

    def _macro(self, as_of: date) -> None:
        view = macro_board(self._store, self._settings, as_of=as_of)
        if view.session is None:
            self._add(as_of, MeasurementStatus.NA.value, "macro", "no macro board is built")
            return
        empty = 0
        for (region, asset_class), cell in sorted(view.cells.items()):
            measured = [row for _, row in sorted(cell.leaves.items()) if row.status.has_value]
            if not measured:
                empty += 1
                continue
            self._add(
                view.session,
                _status_of(measured),
                "macro",
                f"{region}/{asset_class}: " + ", ".join(_leaf_text(row) for row in measured),
            )
        if empty:
            self._add(
                view.session,
                MeasurementStatus.NA.value,
                "macro",
                f"{empty} of {len(view.cells)} board cells have no measured leaf",
            )

    def _grid(self, as_of: date) -> None:
        view = equity_grid(self._store, self._settings, as_of=as_of)
        if view.session is None:
            self._add(as_of, MeasurementStatus.NA.value, "grid", "no equity grid is built")
            return
        wanted = self._config.grid_leaves
        empty = 0
        for market in view.markets:
            for sector in view.sectors:
                cell = view.cells[(market, sector)]
                measured = [
                    row
                    for name in wanted
                    if (row := cell.leaves.get(name)) is not None and row.status.has_value
                ]
                if not measured:
                    empty += 1
                    continue
                self._add(
                    view.session,
                    _status_of(measured),
                    "grid",
                    f"{market}/{sector}: " + ", ".join(_leaf_text(row) for row in measured),
                )
        if empty:
            self._add(
                view.session,
                MeasurementStatus.NA.value,
                "grid",
                f"{empty} of {len(view.cells)} grid cells have none of "
                f"{', '.join(wanted)} measured",
            )

    def _rankings(self, as_of: date) -> None:
        rows = Rankings(self._settings).build(self._store, as_of=as_of)
        session = measurements_repo.measured_session(
            self._store, EntityKind.EQUITY_CELL, as_of=as_of
        )
        if not rows or session is None:
            self._add(
                as_of, MeasurementStatus.NA.value, "ranking", "no ranked view: no grid cell ranks"
            )
            return
        for row in rows:
            self._add(
                session,
                row.status.value,
                "ranking",
                f"{row.dimension.value} #{row.rank}: {row.market}/{row.sector} {row.leaf} "
                f"{number(row.value)} (ranked over {row.ranked_over} of {row.reachable} reachable "
                "cells)",
            )

    def _positioning(self, as_of: date) -> None:
        config = self._settings.sources.positioning
        for contract_id, contract in sorted(config.contracts.items()):
            reports = sources_repo.cot_reports(self._store, contract.code, as_of=as_of)
            reading = positioning_reading(contract_id, reports, as_of=as_of, config=config)
            if reading.share is None:
                self._add(
                    as_of,
                    reading.status.value,
                    "positioning",
                    f"CFTC {contract.name}: {reading.reason}",
                )
                continue
            percentile = "n/a" if reading.percentile is None else number(reading.percentile)
            velocity = "n/a" if reading.velocity is None else number(reading.velocity)
            self._add(
                reading.release_date or as_of,
                reading.status.value,
                "positioning",
                f"CFTC {contract.name}: non-commercial net share of open interest "
                f"{number(reading.share)} (report of {reading.report_date}), percentile "
                f"{percentile} over {reading.window} weeks, change over {config.velocity_weeks} "
                f"weeks {velocity}",
            )

    def _markets(self, as_of: date) -> None:
        config = self._settings.sources.events
        at = datetime.combine(as_of, time.max, tzinfo=UTC)
        found = []
        for market in sources_repo.event_markets(self._store, as_of=as_of):
            snapshots = sources_repo.event_snapshots(
                self._store, market.venue, market.market_id, as_of=as_of
            )
            reading = event_reading(snapshots, at=at, config=config)
            if reading is not None:
                found.append((market, reading))
        found.sort(
            key=lambda pair: (
                not pair[0].curated,
                -abs(pair[1].velocity) if pair[1].velocity is not None else 0.0,
                pair[0].venue,
                pair[0].market_id,
            )
        )
        for market, reading in found[: self._config.max_market_lines]:
            velocity = (
                f"change over {config.velocity_hours} hours {number(reading.velocity)}"
                if reading.velocity is not None
                else f"no reading {config.velocity_hours} hours older"
            )
            self._add(
                reading.observed_at.date(),
                MeasurementStatus.OK.value,
                "market",
                f'{market.venue}: "{market.question}" ({market.outcome}) probability '
                f"{number(reading.probability)}, {velocity}",
            )

    def _pulses(self, as_of: date) -> None:
        config = self._settings.sources.events.gdelt
        for query_id, query in sorted(config.queries.items()):
            days = sources_repo.gdelt_days(self._store, query_id, as_of=as_of)
            reading = pulse_reading(days, query_id=query_id, as_of=as_of, config=config)
            head = f"GDELT {query_id} ({query.theme})"
            if reading.day is None:
                self._add(as_of, MeasurementStatus.NA.value, "pulse", f"{head}: no day collected")
                continue
            if reading.warming_up:
                self._add(
                    reading.day,
                    "warming_up",
                    "pulse",
                    f"{head}: {reading.history} earlier days of {config.min_history_days} needed",
                )
                continue
            share = "n/a" if reading.share_z is None else number(reading.share_z)
            tone = "n/a" if reading.tone_z is None else number(reading.tone_z)
            self._add(
                reading.day,
                MeasurementStatus.OK.value,
                "pulse",
                f"{head}: article share z-score {share}, tone z-score {tone} against "
                f"{reading.history} earlier days",
            )

    def _news(self, as_of: date) -> None:
        since = as_of - timedelta(days=self._config.news_lookback_days - 1)
        items = sources_repo.news_items(self._store, as_of=as_of, since=since)
        read: dict[str, str] = {}
        for extraction in sources_repo.extractions(self._store, as_of=as_of, since=since):
            if extraction.status is ExtractionStatus.OK and extraction.output is not None:
                read[extraction.item_id] = extraction.output
        newest = sorted(items, key=lambda item: (item.collected_at, item.item_id), reverse=True)
        for item in newest[: self._config.max_news_lines]:
            self._news_line(item, read.get(item.item_id))

    def _news_line(self, item: NewsItem, output: str | None) -> None:
        published = "unknown" if item.published_at is None else item.published_at.date().isoformat()
        head = f"{item.feed} ({item.kind}): {item.title} (published {published})"
        if output is None:
            tone = (
                "" if item.fallback_tone is None else f"; offline tone {number(item.fallback_tone)}"
            )
            self._add(
                item.collected_at.date(), RAW, "news", f"{head}; not read by the extractor{tone}"
            )
            return
        event = json.loads(output)
        entities = ", ".join(event.get("entities") or []) or "none named"
        self._add(
            item.collected_at.date(),
            MeasurementStatus.OK.value,
            "news",
            f"{head}; event {event.get('type')}, tone {event.get('tone')}, novelty "
            f"{event.get('novelty')}, entities {entities}",
        )

    def _insiders(self, as_of: date) -> None:
        config = self._settings.sources.insiders
        since = as_of - timedelta(days=config.cluster_window_days - 1)
        found = []
        for cik in sources_repo.insider_issuers(self._store, since=since, as_of=as_of):
            filings = sources_repo.insider_filings(self._store, cik, as_of=as_of, since=since)
            inputs = cluster_inputs(filings, issuer_cik=cik, as_of=as_of, config=config)
            if inputs.filings and inputs.last_filed is not None:
                symbols = [f.issuer_symbol for f in filings if f.issuer_symbol is not None]
                found.append((inputs, symbols[-1] if symbols else f"CIK {cik}"))
        found.sort(key=lambda pair: (-pair[0].buyers, -pair[0].value, pair[0].issuer_cik))
        for inputs, name in found[: self._config.max_insider_lines]:
            assert inputs.last_filed is not None
            self._add(
                inputs.last_filed,
                MeasurementStatus.OK.value,
                "insider",
                f"{name}: {inputs.buyers} officers or directors bought in the open market over "
                f"{inputs.window_days} days ({inputs.officers} officers, {inputs.directors} "
                f"directors; {inputs.filings} filings; {number(inputs.shares)} shares, value "
                f"{number(inputs.value)}; {inputs.unpriced_lines} lines unpriced)",
            )

    def _stock_screen(self, as_of: date) -> None:
        head = (
            "untested stock screen of listed US common stocks passing the account's floors, "
            "ranked by cheapness, quality and industry trend: no signal, no weight in any gate, "
            "no tested return (D180)"
        )
        try:
            screen = stock_screen(self._store, self._settings, as_of=as_of)
        except Exception as exc:  # the sheet stands without the screen; its status says why
            self._add(as_of, FAILED, SCREEN, f"{head}; it cannot be built: {type(exc).__name__}")
            return
        if screen.status is not ScreenStatus.OK:
            self._add(
                as_of,
                screen.status.value,
                SCREEN,
                f"{head}; {screen.status.value}: {screen.reason}",
            )
            return
        loaded = (
            ""
            if screen.survivors_only_until is None or screen.collected_from is None
            else f"; until {screen.survivors_only_until.isoformat()} the industry trend counts "
            f"only the names still listed on {screen.collected_from.isoformat()} (loaded "
            "history, survivors only)"
        )
        self._add(
            as_of,
            MeasurementStatus.OK.value,
            SCREEN,
            f"{head}; {screen.listed} names listed, {screen.passing} pass the floors{loaded}",
        )
        for line in screen.lines[: self._config.stock_screen_top]:
            self._stocks[line.ticker] = line.close
            self._add(
                line.session,
                MeasurementStatus.OK.value,
                SCREEN,
                f"#{line.rank} {line.ticker} ({line.name}, SIC division {line.division}): close "
                f"{number(line.close)}, earnings to price {number(line.earnings_yield)}, gross "
                f"profit to assets {number(line.gross_profitability)}, industry 12-1 return "
                f"{number(line.industry_trend)}, score {number(line.score)}; {UNTESTED}",
            )
        if screen.lacking:
            self._add(
                as_of,
                MeasurementStatus.NA.value,
                SCREEN,
                f"{screen.lacking} of the {screen.passing} names that pass the floors lack an "
                "input and are not ranked",
            )


def build_fact_sheet(
    store: Store, settings: ObliskConfig, *, as_of: date, sections: Sequence[str] = SECTIONS
) -> FactSheet:
    """The fact sheet as of ``as_of`` with ``sections`` (the store worker's entry point)."""
    return FactSheetBuilder(store, settings).build(as_of=as_of, sections=sections)
