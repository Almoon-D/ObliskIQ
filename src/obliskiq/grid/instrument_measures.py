"""Per-instrument measurements: the realised-volatility forecast and the overnight-gap
distribution of every universe instrument whose bars are collected.

The forecast (``grid/har.py``) feeds the vol-mispricing component and the base-rate probabilities
of the decision layer; the gap quantiles (``grid/gaps.py``) feed the gap-adjusted sizing and the
protective spec (DECISIONS D67). Both read the daily window of bars on or before ``as_of`` and
nothing after it.

Leaves, per instrument (its conid is the entity id):

* ``har_rv_forecast_<h>d`` (an annualised vol) and ``har_rv_r2_<h>d`` (the fit's in-sample R²) for
  each horizon in ``grid.har_rv.horizons_days``;
* ``gap_q<p>`` over every gap and ``gap_ex_earnings_q<p>`` over the gaps known not to span a
  report, for each quantile in ``grid.overnight_gaps.quantiles``; ``gap_earnings_min`` and
  ``gap_earnings_max`` over the gaps that do (too few for a quantile). A fund, an index or a
  commodity product reports no earnings, which is a fact about it (``inapplicable``); a stock
  without collected earnings dates is a gap in collection (``n/a``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Final

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..domain.enums import AssetClass
from ..domain.measurement import EntityKind, Measurement
from ..errors import CalendarError
from ..store import measurements as measurements_repo
from ..store import sources as sources_repo
from ..store.db import Store
from .cells import Cell, inputs_hash, to_measurement
from .equity_grid import Freshness, resolve_conid
from .frames import BarFrame, bar_frame
from .gaps import Gap, flag_earnings, gaps, quantiles, restated_share
from .har import fit_har
from .indicators import GridParams
from .vocabulary import Input, Reason, Units

HAR: Final = "har_rv"
GAPS: Final = "overnight_gap"
#: A fraction as a percentage, for a quantile's leaf name (a unit).
PERCENT: Final = 100
#: Asset classes that report earnings: a share.
REPORTS_EARNINGS: Final = frozenset({AssetClass.STOCK})


def quantile_name(level: float) -> str:
    """``0.05`` -> ``q5``, ``0.5`` -> ``q50``, ``0.025`` -> ``q2_5``."""
    return "q" + f"{level * PERCENT:g}".replace(".", "_")


def _digest(rows: object) -> str:
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


class InstrumentMeasures:
    """Writes the forecast and the gap distribution of every collected universe instrument."""

    def __init__(self, store: Store, settings: ObliskConfig, calendars: Calendars) -> None:
        self._store = store
        self._settings = settings
        self._calendars = calendars
        self._params = GridParams.from_settings(settings)
        self._har = settings.grid.har_rv
        self._gaps = settings.grid.overnight_gaps
        self._digest = inputs_hash(
            {"params": repr(self._params), "har": repr(self._har), "gaps": repr(self._gaps)}
        )

    def build(self, *, as_of: date, computed_at: datetime) -> list[Measurement]:
        rows: list[Measurement] = []
        for declared in self._settings.universe.instruments:
            if not declared.collect_bars:
                continue
            conid = resolve_conid(self._store, declared, as_of=as_of)
            if conid is None:
                continue
            rows += self.for_instrument(declared, conid, as_of=as_of, computed_at=computed_at)
        return rows

    def write(self, rows: Sequence[Measurement]) -> int:
        return measurements_repo.upsert_measurements(self._store, rows)

    def for_instrument(
        self, declared: UniverseInstrument, conid: int, *, as_of: date, computed_at: datetime
    ) -> list[Measurement]:
        since = as_of - timedelta(days=self._params.years(self._params.daily_window_years))
        bars = bar_frame(self._store, conid, as_of=as_of, since=since)
        reports = (
            tuple(
                e.report_at
                for e in sources_repo.earnings_dates(self._store, declared.symbol, as_of=as_of)
            )
            if declared.asset_class in REPORTS_EARNINGS
            else ()
        )
        cells = self._har_cells(bars) + self._gap_cells(declared, bars, reports)
        freshness = Freshness(
            Input.DAILY_BARS, bars.last_date, as_of, self._settings.grid.freshness_days.daily_bars
        )
        if freshness.is_stale:
            cells = [cell.stale(freshness.describe()) for cell in cells]
        fingerprint = {
            "settings": self._digest,
            "as_of": as_of,
            "bars": _digest(bars),
            "earnings": _digest(reports),
        }
        return [
            to_measurement(
                cell,
                entity_kind=EntityKind.INSTRUMENT,
                entity_id=str(conid),
                as_of=as_of,
                source=f"bars:{conid}" + ("+earnings:" + declared.symbol if reports else ""),
                fingerprint=fingerprint,
                computed_at=computed_at,
            )
            for cell in cells
        ]

    # ── the forecast ──

    def _har_cells(self, bars: BarFrame) -> list[Cell]:
        cells: list[Cell] = []
        through = f"to {bars.last_date}" if bars.last_date else "on no bars"
        for horizon in self._har.horizons_days:
            fit = fit_har(
                bars.close,
                horizon=horizon,
                weekly=self._har.weekly_sessions,
                monthly=self._har.monthly_sessions,
                min_sessions=self._har.min_sessions,
                trading_days_per_year=self._params.trading_days_per_year,
            )
            window = (
                f"{horizon}d forecast, fitted on {fit.training_rows} sessions {through} "
                f"(overlapping {horizon}-session targets)"
            )
            forecast_leaf, r2_leaf = f"har_rv_forecast_{horizon}d", f"har_rv_r2_{horizon}d"
            if fit.forecast_vol is None:
                reason = fit.problem or "no forecast"
                code = Reason.SHORT_SAMPLE if fit.training_rows == 0 else Reason.NO_FORECAST
                cells.append(Cell.absent(forecast_leaf, HAR, reason, code=code, window=window))
            else:
                cells.append(
                    Cell.measured(
                        forecast_leaf,
                        HAR,
                        fit.forecast_vol,
                        units=Units.DECIMAL,
                        window=window,
                        n_obs=fit.training_rows,
                    )
                )
            if fit.r_squared is None:
                cells.append(
                    Cell.absent(
                        r2_leaf,
                        HAR,
                        fit.problem or "the fitted targets have no dispersion",
                        code=Reason.SHORT_SAMPLE if fit.training_rows == 0 else Reason.NO_FORECAST,
                        window=window,
                    )
                )
            else:
                cells.append(
                    Cell.measured(
                        r2_leaf,
                        HAR,
                        fit.r_squared,
                        units=Units.RATIO,
                        window=window,
                        n_obs=fit.training_rows,
                    )
                )
        return cells

    # ── the gaps ──

    def _gap_names(self) -> tuple[list[str], list[str]]:
        names = [quantile_name(level) for level in self._gaps.quantiles]
        return [f"gap_{name}" for name in names], [f"gap_ex_earnings_{name}" for name in names]

    def _gap_cells(
        self, declared: UniverseInstrument, bars: BarFrame, reports: Sequence[datetime]
    ) -> list[Cell]:
        every, outside = self._gap_names()
        extremes = ["gap_earnings_min", "gap_earnings_max"]
        floor = self._gaps.min_gaps
        raw = gaps(bars)
        share = restated_share(bars)
        limit = self._params.garman_klass_max_restated_open_share
        if share is not None and share > limit:
            reason = (
                f"{share:.0%} of {declared.symbol}'s opens are exactly the previous close, above "
                "grid.garman_klass_max_restated_open_share: the venue publishes no opening print, "
                "so its gaps are zeros by construction"
            )
            return [
                Cell.absent(name, GAPS, reason, code=Reason.OPEN_RESTATED)
                for name in [*every, *outside, *extremes]
            ]
        if len(raw) < floor:
            reason = (
                f"{len(raw)} overnight gap(s) in the daily window, below the {floor} in "
                "grid.overnight_gaps.min_gaps"
            )
            return [
                Cell.absent(name, GAPS, reason, code=Reason.SHORT_SAMPLE)
                for name in [*every, *outside, *extremes]
            ]
        flagged = self._flagged(declared, raw, reports)
        span = f"{raw[0][1]} to {raw[-1][1]}"
        cells = self._quantile_cells(every, [g.value for g in flagged], f"every gap, {span}")
        known = [g.value for g in flagged if g.earnings is False]
        unplaced = sum(1 for g in flagged if g.earnings is None)
        note = f"; {unplaced} gap(s) the calendar could not place are left out" if unplaced else ""
        if declared.asset_class not in REPORTS_EARNINGS:
            reason = (
                f"{declared.symbol} is a {declared.asset_class.value}, which reports no earnings"
            )
            cells += self._quantile_cells(outside, [g.value for g in flagged], f"every gap, {span}")
            return cells + [
                Cell.inapplicable(name, GAPS, reason, code=Reason.NO_EARNINGS) for name in extremes
            ]
        if not reports:
            reason = (
                f"no earnings dates are held for {declared.symbol}, so no gap can be told apart "
                "as an earnings gap"
            )
            return cells + [
                Cell.absent(name, GAPS, reason, code=Reason.NO_EARNINGS_DATES)
                for name in [*outside, *extremes]
            ]
        if len(known) < floor:
            reason = (
                f"{len(known)} gap(s) known not to span a report, below the {floor} floor{note}"
            )
            cells += [Cell.absent(name, GAPS, reason, code=Reason.SHORT_SAMPLE) for name in outside]
        else:
            cells += self._quantile_cells(outside, known, f"gaps outside earnings, {span}{note}")
        return cells + self._earnings_extremes(flagged, extremes, span)

    def _flagged(
        self,
        declared: UniverseInstrument,
        raw: list[tuple[date, date, float]],
        reports: Sequence[datetime],
    ) -> list[Gap]:
        try:
            venue = self._calendars.venue_for_ibkr(declared.listing_exchange)
        except CalendarError:
            return [Gap(session, value, None if reports else False) for _, session, value in raw]
        return flag_earnings(raw, reports, calendars=self._calendars, venue=venue)

    def _quantile_cells(
        self, names: Sequence[str], values: Sequence[float], what: str
    ) -> list[Cell]:
        levels = quantiles(values, self._gaps.quantiles)
        return [
            Cell.measured(
                name,
                GAPS,
                level,
                units=Units.DECIMAL,
                window=f"{what} ({len(values)} gaps)",
                n_obs=len(values),
            )
            for name, level in zip(names, levels, strict=True)
        ]

    @staticmethod
    def _earnings_extremes(flagged: Sequence[Gap], names: Sequence[str], span: str) -> list[Cell]:
        values = [g.value for g in flagged if g.earnings]
        if not values:
            reason = f"no gap between {span} spans a known earnings report"
            return [Cell.absent(name, GAPS, reason, code=Reason.SHORT_SAMPLE) for name in names]
        window = f"gaps across earnings, {span} ({len(values)} gaps)"
        return [
            Cell.measured(
                names[0], GAPS, min(values), units=Units.DECIMAL, window=window, n_obs=len(values)
            ),
            Cell.measured(
                names[1], GAPS, max(values), units=Units.DECIMAL, window=window, n_obs=len(values)
            ),
        ]
