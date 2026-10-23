"""The macro board: region x asset class (MASTER_PLAN §5.7), written to the measurement store.

Where cross-asset stress and policy breaks are. The axes and each cell's inputs are declared in
``config/universe.yaml`` (``macro_board``): an instrument that claims the cell, macro series and
the label that combines them, or futures roots; a cell with none says why, and that reason
reaches every leaf of it as ``inapplicable``. Not in lite, which had no board (DECISIONS D131);
its pieces are lite's: the equity families of the grid on the claiming instrument, the
release-aligned macro reads, the curve labels and the real rate of family 12.

Each asset class emits one fixed set of leaves whatever its status:

* ``equity_index``: the grid's returns, distance-from-high and realised-vol families on the
  claiming instrument, and positioning where the cell names a CFTC contract;
* ``rate_2y``, ``rate_10y``, ``credit``: the level, its change over each horizon (both ends
  release-aligned) and the level's percentile over the window;
* ``vol_index``: the same, and the term slope (short over long minus one) where two are named;
* ``curve``: the slope the label names and its change over each horizon;
* ``real_rate``: the policy rate, CPI year on year and their difference;
* ``fx_vs_base``: the local currency's move against the base over each horizon;
* ``commodities``: per futures root, the annualised roll of the front against the next and the
  front's return over each horizon.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from ..config.schema import ObliskConfig
from ..config.schema_universe import BOARD_ASSET_CLASSES, MacroCellConfig
from ..data.macro.catalog import MacroCatalog
from ..domain.market import CurvePoint
from ..domain.measurement import EntityKind, Measurement
from ..quant.stats import percentile_of, simple_return
from ..store import measurements as measurements_repo
from ..store import repos
from ..store import sources as sources_repo
from ..store.db import Store
from .cells import Cell, inputs_hash, to_measurement
from .equity_grid import Freshness, cell_instrument, stale_reason
from .frames import (
    MacroReader,
    MarketSpec,
    as_decimal,
    calendar_days,
    cpi_yoy,
    fx_ratio,
    known_at,
    market_spec,
)
from .indicators import (
    CellInputs,
    GridParams,
    distance_from_high,
    positioning_cells,
    realised_vol,
    returns,
)
from .vocabulary import FAMILY_INPUTS, Family, Input, Reason, Units

#: The board families of the non-equity asset classes (the leaves' ``indicator``).
LEVEL: Final = "level"
CURVE: Final = "curve"
REAL_RATE: Final = "real_rate"
FX: Final = "fx_vs_base"
COMMODITIES: Final = "commodities"
#: The equity families the board reads on a claiming instrument.
EQUITY_FAMILIES: Final[tuple[tuple[Family, Callable[[CellInputs], list[Cell]]], ...]] = (
    (Family.RETURNS, returns),
    (Family.DISTANCE_FROM_HIGH, distance_from_high),
    (Family.REALISED_VOL, realised_vol),
    (Family.POSITIONING, positioning_cells),
)
#: How a level series' units become a leaf's.
_LEVEL_UNITS: Final = {"%": Units.DECIMAL, "Index": Units.INDEX}


def board_id(region: str, asset_class: str) -> str:
    return f"{region}.{asset_class}"


def _digest(rows: object) -> str:
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Context:
    region: str
    asset_class: str
    cell: MacroCellConfig
    reader: MacroReader
    as_of: date

    @property
    def ref(self) -> str:
        return board_id(self.region, self.asset_class)


class MacroBoardBuilder:
    """Computes every leaf of every region x asset class and writes them as measurements."""

    def __init__(self, store: Store, settings: ObliskConfig) -> None:
        self._store = store
        self._settings = settings
        self._params = GridParams.from_settings(settings)
        self._catalog = MacroCatalog.from_settings(settings)
        self._universe = settings.universe
        self._board_settings = settings.grid.macro_board
        self._claims = self._universe.board_claims()
        self._settings_digest = inputs_hash(
            {"params": repr(self._params), "board": repr(self._board_settings)}
        )

    def build(self, *, as_of: date, computed_at: datetime) -> list[Measurement]:
        reader = MacroReader(self._store, self._catalog, as_of)
        board = self._universe.macro_board
        rows: list[Measurement] = []
        for region in sorted(board.regions):
            for asset_class in board.asset_classes:
                cell = board.regions[region].cells[asset_class]
                context = _Context(region, asset_class, cell, reader, as_of)
                rows += self._cell(context, computed_at)
        return rows

    def write(self, rows: Sequence[Measurement]) -> int:
        return measurements_repo.upsert_measurements(self._store, rows)

    # ── one cell ──

    def _cell(self, context: _Context, computed_at: datetime) -> list[Measurement]:
        kind = BOARD_ASSET_CLASSES[context.asset_class]
        if kind == "instrument":
            cells, source, prints, freshness = self._equity(context)
        elif kind == "roots":
            cells, source, prints, freshness = self._commodities(context)
        else:
            compute = {
                "rate_2y": self._level,
                "rate_10y": self._level,
                "credit": self._level,
                "vol_index": self._level,
                "curve": self._curve,
                "real_rate": self._real_rate,
                "fx_vs_base": self._fx,
            }[context.asset_class]
            cells = compute(context)
            source, prints, freshness = self._series_inputs(context)
        if context.cell.unmapped is not None:
            cells = [
                Cell.inapplicable(
                    cell.leaf,
                    cell.family,
                    f"{context.ref}: {context.cell.unmapped}",
                    code=Reason.CELL_UNMAPPED,
                    window=cell.window,
                )
                for cell in cells
            ]
        elif freshness is not None:
            cells = [cell.stale(freshness) for cell in cells]
        fingerprint = {"settings": self._settings_digest, "as_of": context.as_of, **prints}
        return [
            to_measurement(
                cell,
                entity_kind=EntityKind.MACRO_CELL,
                entity_id=context.ref,
                as_of=context.as_of,
                source=source,
                fingerprint=fingerprint,
                computed_at=computed_at,
            )
            for cell in cells
        ]

    def _series_inputs(self, context: _Context) -> tuple[str | None, dict[str, object], str | None]:
        """The series a cell reads: its source, the fingerprint of each, and a stale reason when
        the newest release among them is older than the macro window."""
        series_ids = list(context.cell.series)
        prints: dict[str, object] = {}
        releases: list[date] = []
        for series_id in series_ids:
            series = context.reader.series(series_id)
            prints[series_id] = (
                None
                if series is None
                else _digest(
                    [(o.reference_date, o.release_date, o.value) for o in series.observations]
                )
            )
            if series is not None and series.latest is not None and series.latest.release_date:
                releases.append(series.latest.release_date)
        freshness = Freshness(
            Input.MACRO,
            max(releases, default=None),
            context.as_of,
            self._settings.grid.freshness_days.macro,
        )
        stale = freshness.describe() if freshness.is_stale else None
        source = "macro:" + ",".join(series_ids) if series_ids else None
        return source, prints, stale

    # ── equity index ──

    def _equity(
        self, context: _Context
    ) -> tuple[list[Cell], str | None, dict[str, object], str | None]:
        symbol = self._claims.get(context.ref)
        declared = None if symbol is None else self._universe.instrument(symbol)
        instrument = (
            None
            if declared is None
            else cell_instrument(self._store, declared, as_of=context.as_of)
        )
        spec = self._spec(context.region)
        inputs = CellInputs(
            store=self._store,
            settings=self._settings,
            params=self._params,
            reader=context.reader,
            as_of=context.as_of,
            market=context.region,
            sector=context.asset_class,
            spec=spec,
            instrument=instrument,
            benchmark=None,
            benchmark_market=self._settings.grid.benchmark_market,
        )
        cells: list[Cell] = []
        for family, compute in EQUITY_FAMILIES:
            computed = compute(inputs)
            if family is Family.POSITIONING or instrument is not None:
                cells += computed
                continue
            reason = (
                f"no instrument claims {context.ref}"
                if symbol is None
                else f"{symbol} claims {context.ref} and the store holds no instrument for it; "
                "run `obliskiq collect universe` (or pin it)"
            )
            cells += [
                Cell.absent(
                    cell.leaf, cell.family, reason, code=Reason.NOT_SEEDED, window=cell.window
                )
                for cell in computed
            ]
        prints: dict[str, object] = {"bars": None if instrument is None else _digest(inputs.daily)}
        stale: str | None = None
        if instrument is not None:
            newest = repos.latest_bar_date(self._store, instrument.conid, as_of=context.as_of)
            bars = Freshness(
                Input.DAILY_BARS,
                newest,
                context.as_of,
                self._settings.grid.freshness_days.daily_bars,
            )
            stale = stale_reason(FAMILY_INPUTS[Family.RETURNS], {Input.DAILY_BARS: bars})
        if spec.cot is not None:
            code = self._settings.sources.positioning.contracts[spec.cot].code
            reports = sources_repo.cot_reports(self._store, code, as_of=context.as_of)
            prints["cot"] = _digest([r.model_dump() for r in reports])
        sources = [f"bars:{instrument.conid}"] if instrument is not None else []
        if spec.cot is not None:
            sources.append(f"cot:{spec.cot}")
        return cells, "+".join(sources) or None, prints, stale

    def _spec(self, region: str) -> MarketSpec:
        return market_spec(self._universe, region)

    # ── level series ──

    def _level(self, context: _Context) -> list[Cell]:
        """The level, its change over each horizon and its percentile, on the first series; the
        vol index adds the term slope of its pair."""
        cell = context.cell
        if not cell.series:
            names = [LEVEL, *[f"change_{m}m" for m in self._board_settings.change_horizons_months]]
            names.append("level_percentile")
            if context.asset_class == "vol_index":
                names.append("term_slope")
            return _undeclared(context, names, LEVEL)
        out = self._level_leaves(context, cell.series[0])
        if context.asset_class == "vol_index":
            out.append(self._term_slope(context))
        return out

    def _level_leaves(self, context: _Context, series_id: str) -> list[Cell]:
        reader = context.reader
        horizons = self._board_settings.change_horizons_months
        series = reader.series(series_id)
        units = reader.units(series_id)
        leaf_units = None if units is None else _LEVEL_UNITS.get(units)
        names = [LEVEL, *[f"change_{m}m" for m in horizons], "level_percentile"]
        if series is None or units is None or leaf_units is None:
            reason = (
                reader.absence(series_id)
                if series is None or units is None
                else f"{series_id} is declared in {units!r}, which the board cannot read as a level"
            )
            code = (
                Reason.SERIES_ABSENT
                if leaf_units is not None or units is None
                else Reason.UNITS_MISMATCH
            )
            return [Cell.absent(name, LEVEL, reason, code=code) for name in names]
        if len(series.units) > 1:
            reason = (
                f"{series_id} carries {len(series.units)} units "
                f"({', '.join(sorted(series.units))}); a level, a change and a rank across them "
                "would compare two measurements"
            )
            return [Cell.absent(name, LEVEL, reason, code=Reason.MIXED_UNITS) for name in names]
        latest = known_at(series, context.as_of)
        if latest is None:
            reason = reader.absence(series_id)
            return [Cell.absent(name, LEVEL, reason, code=Reason.SERIES_ABSENT) for name in names]
        level = as_decimal(latest, units)
        cells = [
            Cell.measured(
                LEVEL,
                LEVEL,
                level,
                units=leaf_units,
                window=f"{series_id} ({latest.reference_date}, released {latest.release_date})",
            )
        ]
        for months in horizons:
            start_day = context.as_of - timedelta(
                days=calendar_days(months, days_per_year=self._params.calendar_days_per_year)
            )
            start = reader.value_at(series_id, start_day)
            leaf = f"change_{months}m"
            if start is None:
                cells.append(
                    Cell.absent(
                        leaf,
                        LEVEL,
                        f"{series_id} holds no observation released on or before {start_day}, so a "
                        f"{months}-month change would be measured over less than its label",
                        code=Reason.SHORT_SAMPLE,
                        window=f"{months}m",
                    )
                )
            else:
                cells.append(
                    Cell.measured(
                        leaf,
                        LEVEL,
                        level - start,
                        units=leaf_units,
                        window=f"{months}m of {series_id}, both ends by release",
                    )
                )
        cells.append(self._percentile(context, series_id, latest.value))
        return cells

    def _percentile(self, context: _Context, series_id: str, current: float) -> Cell:
        series = context.reader.series(series_id)
        years = self._board_settings.percentile_window_years
        start = context.as_of - timedelta(days=self._params.years(years))
        values = (
            []
            if series is None
            else [
                o.value
                for o in series.observations
                if o.release_date is not None and start <= o.release_date <= context.as_of
            ]
        )
        floor = self._params.min_percentile
        window = f"{years}y of {series_id}"
        if len(values) < floor:
            return Cell.absent(
                "level_percentile",
                LEVEL,
                f"{series_id} holds {len(values)} observation(s) released in the {years}-year "
                f"window, below the {floor} floor",
                code=Reason.SHORT_SAMPLE,
                window=window,
            )
        rank = percentile_of(values, current)
        if rank is None:
            return Cell.absent(
                "level_percentile", LEVEL, "no finite value to rank", code=Reason.SHORT_SAMPLE
            )
        return Cell.measured(
            "level_percentile",
            LEVEL,
            rank,
            units=Units.PERCENTILE,
            window=f"{window} ({len(values)} observations)",
            n_obs=len(values),
        )

    def _term_slope(self, context: _Context) -> Cell:
        cell = context.cell
        if cell.label is None or len(cell.series) < len(("short", "long")):
            return Cell.inapplicable(
                "term_slope",
                LEVEL,
                f"{context.ref} names one volatility index, so there is no term to measure",
                code=Reason.SERIES_UNDECLARED,
            )
        short_id, long_id = cell.series[0], cell.series[1]
        short = context.reader.value_at(short_id, context.as_of)
        long = context.reader.value_at(long_id, context.as_of)
        if short is None or long is None or long <= 0:
            missing = short_id if short is None else long_id
            return Cell.absent(
                "term_slope", LEVEL, context.reader.absence(missing), code=Reason.SERIES_ABSENT
            )
        return Cell.measured(
            "term_slope",
            LEVEL,
            short / long - 1.0,
            units=Units.RATIO,
            window=f"{short_id} over {long_id} ({cell.label})",
        )

    # ── curve, real rate, FX ──

    def _curve(self, context: _Context) -> list[Cell]:
        cell = context.cell
        names = [
            "slope",
            *[f"slope_change_{m}m" for m in self._board_settings.change_horizons_months],
        ]
        if cell.label is None or not cell.series:
            return _undeclared(context, names, CURVE)
        long_id, short_id = cell.series[0], cell.series[1]
        reader = context.reader

        def slope_at(day: date) -> float | None:
            long = reader.value_at(long_id, day)
            short = reader.value_at(short_id, day)
            return None if long is None or short is None else long - short

        now = slope_at(context.as_of)
        if now is None:
            missing = long_id if reader.value_at(long_id, context.as_of) is None else short_id
            reason = reader.absence(missing)
            return [Cell.absent(name, CURVE, reason, code=Reason.SERIES_ABSENT) for name in names]
        cells = [Cell.measured("slope", CURVE, now, units=Units.DECIMAL, window=cell.label)]
        for months in self._board_settings.change_horizons_months:
            day = context.as_of - timedelta(
                days=calendar_days(months, days_per_year=self._params.calendar_days_per_year)
            )
            then = slope_at(day)
            leaf = f"slope_change_{months}m"
            if then is None:
                cells.append(
                    Cell.absent(
                        leaf,
                        CURVE,
                        f"{long_id} or {short_id} holds nothing released on or before {day}",
                        code=Reason.SHORT_SAMPLE,
                        window=f"{months}m",
                    )
                )
            else:
                cells.append(
                    Cell.measured(
                        leaf,
                        CURVE,
                        now - then,
                        units=Units.DECIMAL,
                        window=f"{months}m of {cell.label}, both ends by release",
                    )
                )
        return cells

    def _real_rate(self, context: _Context) -> list[Cell]:
        names = ["policy_rate", "cpi_yoy", "real_rate"]
        cell = context.cell
        spec = self._spec(context.region)
        if cell.label is None or spec.policy is None:
            return _undeclared(context, names, REAL_RATE)
        reader = context.reader
        policy = reader.value_at(spec.policy, context.as_of)
        cpi = cpi_yoy(reader, spec, days_per_year=self._params.calendar_days_per_year)
        cells = [
            Cell.absent(
                "policy_rate", REAL_RATE, reader.absence(spec.policy), code=Reason.SERIES_ABSENT
            )
            if policy is None
            else Cell.measured(
                "policy_rate", REAL_RATE, policy, units=Units.DECIMAL, window=spec.policy
            ),
            Cell.absent(
                "cpi_yoy",
                REAL_RATE,
                cpi.problem or "no CPI",
                code=Reason.MIXED_UNITS if cpi.rebased else Reason.SERIES_ABSENT,
            )
            if cpi.value is None
            else Cell.measured(
                "cpi_yoy",
                REAL_RATE,
                cpi.value,
                units=Units.DECIMAL,
                window=f"{spec.cpi} (printed {cpi.released})",
            ),
        ]
        if policy is None or cpi.value is None:
            cells.append(
                Cell.absent(
                    "real_rate",
                    REAL_RATE,
                    "the real rate needs both the policy rate and the CPI beside it",
                    code=Reason.SERIES_ABSENT,
                )
            )
        else:
            cells.append(
                Cell.measured(
                    "real_rate",
                    REAL_RATE,
                    policy - cpi.value,
                    units=Units.DECIMAL,
                    window=(
                        f"{cell.label}: {spec.policy} less {spec.cpi} (CPI printed {cpi.released})"
                    ),
                )
            )
        window = self._settings.grid.freshness_days.macro
        if cpi.released is not None and (context.as_of - cpi.released).days > window:
            # A fresh policy rate less a stale print is not a real rate, and the daily nominal
            # side would make the cell read as current (lite's family 12).
            reason = (
                f"{spec.cpi} was last printed {cpi.released}, beyond the {window}-day "
                "grid.freshness_days.macro window"
            )
            cells = [c.stale(reason) if c.leaf != "policy_rate" else c for c in cells]
        return cells

    def _fx(self, context: _Context) -> list[Cell]:
        horizons = self._board_settings.change_horizons_months
        names = [f"currency_return_{m}m" for m in horizons]
        if not context.cell.series:
            return _undeclared(context, names, FX)
        spec = self._spec(context.region)
        cells: list[Cell] = []
        inputs_reader = context.reader
        for months, name in zip(horizons, names, strict=True):
            days = calendar_days(months, days_per_year=self._params.calendar_days_per_year)
            result = fx_ratio(
                inputs_reader, spec, horizon_days=days, base_currency=self._params.base_currency
            )
            if result.ratio is None:
                cells.append(
                    Cell.absent(
                        name,
                        FX,
                        result.problem or "no rate",
                        code=Reason.NO_FX,
                        window=f"{months}m",
                    )
                )
            else:
                cells.append(
                    Cell.measured(
                        name,
                        FX,
                        result.ratio - 1.0,
                        units=Units.DECIMAL,
                        window=f"{months}m of {spec.fx}, both ends by release",
                    )
                )
        return cells

    # ── commodities ──

    def _commodities(
        self, context: _Context
    ) -> tuple[list[Cell], str | None, dict[str, object], str | None]:
        horizons = self._board_settings.change_horizons_months
        cells: list[Cell] = []
        prints: dict[str, object] = {}
        newest: list[date] = []
        for root in context.cell.roots:
            points = repos.curve(self._store, root, as_of=context.as_of)
            history = repos.curve_front_history(self._store, root, as_of=context.as_of)
            prints[root] = _digest(([p.model_dump() for p in points], history))
            if points:
                newest.append(points[0].snapshot_date)
            cells.append(self._roll(root, points))
            for months in horizons:
                cells.append(self._front_return(context, root, history, months))
        freshness = Freshness(
            Input.DAILY_BARS,
            max(newest, default=None),
            context.as_of,
            self._settings.grid.freshness_days.daily_bars,
        )
        stale = freshness.describe() if freshness.is_stale else None
        source = "curves:" + ",".join(context.cell.roots) if context.cell.roots else None
        return cells, source, prints, stale

    @staticmethod
    def _roll(root: str, curve: Sequence[CurvePoint]) -> Cell:
        leaf = f"roll_yield_{root}"
        if len(curve) < len(("front", "next")) or curve[1].annualised_roll is None:
            return Cell.absent(
                leaf,
                COMMODITIES,
                f"the {root} curve held no priced front and next contract on its newest session",
                code=Reason.NO_CURVE,
            )
        return Cell.measured(
            leaf,
            COMMODITIES,
            curve[1].annualised_roll,
            units=Units.DECIMAL,
            window=f"{root} {curve[0].expiry} vs {curve[1].expiry} on {curve[0].snapshot_date}",
        )

    def _front_return(
        self,
        context: _Context,
        root: str,
        history: Sequence[tuple[date, float]],
        months: int,
    ) -> Cell:
        leaf = f"front_return_{months}m_{root}"
        start_day = context.as_of - timedelta(
            days=calendar_days(months, days_per_year=self._params.calendar_days_per_year)
        )
        before = [price for day, price in history if day <= start_day]
        if not history or not before:
            return Cell.absent(
                leaf,
                COMMODITIES,
                f"the {root} front-month history holds no session on or before {start_day}, so a "
                f"{months}-month return would be measured over less than its label",
                code=Reason.SHORT_SAMPLE,
                window=f"{months}m",
            )
        value = simple_return(before[-1], history[-1][1])
        if value is None:
            return Cell.absent(
                leaf,
                COMMODITIES,
                f"the {root} front price is not positive",
                code=Reason.SHORT_SAMPLE,
            )
        return Cell.measured(
            leaf,
            COMMODITIES,
            value,
            units=Units.DECIMAL,
            window=f"{months}m of the {root} front month (the contract rolls inside it)",
        )


def _undeclared(context: _Context, names: Sequence[str], family: str) -> list[Cell]:
    """The leaves of a cell that names no series: its declared reason replaces this one when the
    board declares it unmapped; otherwise only an instrument claims it, which the board reads on
    an equity index alone."""
    reason = f"{context.ref} names no series the board reads for {context.asset_class}"
    return [
        Cell.inapplicable(name, family, reason, code=Reason.SERIES_UNDECLARED) for name in names
    ]
