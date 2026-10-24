"""Commodities: the curve, the wrapper it dictates, positioning, cycle position and inventory
(lite's ``compute/commodity_engine.py``), written as leaves of each futures root (entity kind
``commodity_root``).

**The curve decides the instrument, not just the view.** Backwardation (the front above the
deferred, a positive roll) is physical tightness and supports a long exposure; contango does the
opposite, and over a 90-200 day hold a futures-tracking wrapper bleeds roll whether or not the
thesis is right. Beyond ``commodities.term_structure.contango_max_annualised_for_futures_wrapper``
that wrapper is refused and the view goes through the next of
``commodities.preferred_wrapper_order``; under ``es`` a futures-tracking ETC is ineligible then
(:class:`CurveContangoCheck`, Session 8's ``ContangoCheck``). A curve that cannot be classified
recommends nothing and fails the check: an unknown roll is never a flat one.

**Positioning conditions, it never times.** The CFTC reading is Session 10's (its own window,
DECISIONS D134); at or beyond ``commodities.cot.extreme_percentile`` in either tail it is extreme,
and an extreme needs a separate price confirmation (``require_price_confirmation``).

**A short percentile is never cycle position.** The long price history (the root's
``history_series``) is ranked over ``sources.macro.commodity_history.window_years``: a five-year
percentile anchors on the current regime and misleads about a ten-to-twenty-year cycle.

**Inventory is compared with its seasonal band**: the same ISO week (``seasonal_week_halfwidth``
either side) in each of the prior ``seasonal_window_years``, the current year excluded (its own
neighbouring weeks would pull the band toward the reading), and at least
``min_band_observations`` prior-year readings before a deviation is reported.

Reading a futures curve is a data operation: no contract is ever a candidate here.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from ...config.schema import ObliskConfig
from ...config.schema_engines import FUTURES_TRACKING_WRAPPER
from ...config.schema_universe import FuturesRootConfig
from ...data.macro.catalog import MacroCatalog
from ...domain.measurement import EntityKind, Measurement, MeasurementStatus
from ...domain.sources import PositioningStatus
from ...grid.cells import Cell, inputs_hash, to_measurement
from ...grid.frames import MacroReader, positioning
from ...grid.vocabulary import Reason, Units
from ...jurisdiction.eligibility_eu_priips import ContangoVerdict
from ...quant.stats import mean, percentile_of, stdev
from ...store import measurements as measurements_repo
from ...store import repos
from ...store.db import Store

CURVE: Final = "curve"
POSITIONING: Final = "positioning"
CYCLE: Final = "cycle"
INVENTORY: Final = "inventory"
SHAPE_CONTANGO: Final = "contango"
SHAPE_BACKWARDATION: Final = "backwardation"
SHAPE_FLAT: Final = "flat"
#: Two rungs, the fewest a curve has a shape with.
MIN_CURVE_POINTS: Final = 2
#: Below this annualised roll the curve is flat rather than either shape: an order of magnitude
#: under the contango ceiling, so it never masks a wrapper decision (lite's constant).
FLAT_TOLERANCE: Final = 0.001
#: ISO weeks in a year, to wrap the seasonal band across 1 January.
ISO_WEEKS: Final = 52
CURVE_LEAVES: Final = ("front_price", "deferred_price", "annualised_roll", "contango_annualised")


def _digest(rows: object) -> str:
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def week_distance(week: int, reference: int) -> int:
    """The distance between two ISO weeks, wrapping across the year."""
    raw = abs(week - reference)
    return min(raw, ISO_WEEKS - raw)


@dataclass(frozen=True, slots=True)
class CurveShape:
    """One session's term structure for a root, classified, or why it cannot be."""

    root: str
    snapshot_date: date | None
    front_expiry: date | None
    front_price: float | None
    deferred_expiry: date | None
    deferred_price: float | None
    annualised_roll: float | None
    shape: str | None
    reason: str
    #: Why the shape cannot be used for a decision though it was read (an old curve).
    stale: str | None = None

    @property
    def contango(self) -> float | None:
        """What the curve costs to hold, annualised: the roll's negative (a positive roll is
        backwardation, where rolling forward earns)."""
        return None if self.annualised_roll is None else -self.annualised_roll


@dataclass(frozen=True, slots=True)
class WrapperChoice:
    """The wrapper the curve permits, and the one it rules out."""

    root: str
    recommended: str | None
    rejected: str | None
    alternatives: tuple[str, ...]
    reason: str


class CommodityEngine:
    """The commodity layer over one store, bound to configuration once."""

    def __init__(self, store: Store, settings: ObliskConfig) -> None:
        self._store = store
        self._settings = settings
        commodities = settings.commodities
        self._ceiling = commodities.term_structure.contango_max_annualised_for_futures_wrapper
        self._order = tuple(commodities.preferred_wrapper_order)
        self._extreme = commodities.cot.extreme_percentile
        self._confirmation = commodities.cot.require_price_confirmation
        self._seasonal = commodities.history.seasonal_adjustment
        self._inventory = commodities.inventory
        self._history_years = settings.sources.macro.commodity_history.window_years
        self._basis = settings.numerics.bsm.day_count_basis
        self._freshness = settings.grid.freshness_days.daily_bars
        self._catalog = MacroCatalog.from_settings(settings)
        self._digest = inputs_hash(
            {"commodities": repr(commodities), "history_years": self._history_years}
        )

    @property
    def contango_ceiling(self) -> float:
        return self._ceiling

    # ── the curve ──

    def curve_shape(self, root: str, *, as_of: date) -> CurveShape:
        """The newest curve on or before ``as_of``, front against the next priced rung."""
        points = [p for p in repos.curve(self._store, root, as_of=as_of) if p.price and p.price > 0]
        if len(points) < MIN_CURVE_POINTS:
            return CurveShape(
                root,
                points[0].snapshot_date if points else None,
                None,
                None,
                None,
                None,
                None,
                None,
                f"{root} has {len(points)} priced curve point(s) by {as_of}: a single rung has "
                "no shape",
            )
        front, deferred = points[0], points[1]
        session = deferred.snapshot_date
        age = (as_of - session).days
        stale = (
            f"the newest {root} curve is from {session}, {age} days old, beyond the "
            f"{self._freshness}-day grid.freshness_days.daily_bars window"
            if age > self._freshness
            else None
        )
        roll = deferred.annualised_roll
        if roll is None:
            return CurveShape(
                root,
                session,
                front.expiry,
                front.price,
                deferred.expiry,
                deferred.price,
                None,
                None,
                f"{root}'s second rung carries no annualised roll, so the shape cannot be "
                "classified; a flat reading would drag the wrapper choice toward 'fine'",
                stale,
            )
        if abs(roll) <= FLAT_TOLERANCE:
            shape = SHAPE_FLAT
        else:
            shape = SHAPE_BACKWARDATION if roll > 0 else SHAPE_CONTANGO
        return CurveShape(
            root,
            session,
            front.expiry,
            front.price,
            deferred.expiry,
            deferred.price,
            roll,
            shape,
            f"{shape} at {abs(roll):.1%} annualised on the {session} curve ({front.expiry} against "
            f"{deferred.expiry})",
            stale,
        )

    def wrapper(self, shape: CurveShape) -> WrapperChoice:
        """The wrapper the curve permits; every commodity view carries one."""
        order = self._order
        contango = shape.contango
        if contango is None or shape.stale is not None:
            return WrapperChoice(
                shape.root,
                None,
                None,
                order,
                f"the curve cannot be used ({shape.stale or shape.reason}), so no wrapper is "
                "recommended: check the curve before choosing an instrument, every time",
            )
        if contango > self._ceiling:
            permitted = tuple(w for w in order if w != FUTURES_TRACKING_WRAPPER)
            return WrapperChoice(
                shape.root,
                permitted[0],
                FUTURES_TRACKING_WRAPPER,
                permitted,
                f"{shape.root} is in contango at {contango:.1%} annualised, beyond the "
                f"{self._ceiling:.1%} ceiling: a futures-tracking wrapper bleeds roll over a "
                f"90-200 day hold whether or not the thesis is right; express it through "
                f"{permitted[0]}",
            )
        return WrapperChoice(
            shape.root,
            order[0],
            None,
            order,
            f"{shape.reason}, inside the {self._ceiling:.1%} ceiling: every wrapper in the "
            "preference order remains available",
        )

    # ── the leaves ──

    def build(self, *, as_of: date, computed_at: datetime) -> list[Measurement]:
        reader = MacroReader(self._store, self._catalog, as_of)
        rows: list[Measurement] = []
        for root in self._settings.universe.futures_roots:
            if root.tracked:
                rows += self.for_root(root, reader=reader, as_of=as_of, computed_at=computed_at)
        return rows

    def write(self, rows: Sequence[Measurement]) -> int:
        return measurements_repo.upsert_measurements(self._store, rows)

    def for_root(
        self, root: FuturesRootConfig, *, reader: MacroReader, as_of: date, computed_at: datetime
    ) -> list[Measurement]:
        shape = self.curve_shape(root.root, as_of=as_of)
        cells = self._curve_cells(shape)
        cells.append(self._positioning(root, as_of))
        cells.append(self._cycle(root, reader, as_of))
        cells += self._inventory_cells(root, reader, as_of)
        fingerprint = {
            "settings": self._digest,
            "as_of": as_of,
            "curve": _digest(shape),
            "series": _digest(
                [
                    (sid, reader.latest(sid))
                    for sid in (root.history_series, root.inventory_series)
                    if sid is not None
                ]
            ),
        }
        return [
            to_measurement(
                cell,
                entity_kind=EntityKind.COMMODITY_ROOT,
                entity_id=root.root,
                as_of=as_of,
                source=f"curves:{root.root}",
                fingerprint=fingerprint,
                computed_at=computed_at,
            )
            for cell in cells
        ]

    def _curve_cells(self, shape: CurveShape) -> list[Cell]:
        window = None if shape.snapshot_date is None else f"the {shape.snapshot_date} curve"
        if shape.annualised_roll is None:
            return [
                Cell.absent(leaf, CURVE, shape.reason, code=Reason.NO_CURVE, window=window)
                for leaf in CURVE_LEAVES
            ]
        values = (shape.front_price, shape.deferred_price, shape.annualised_roll, shape.contango)
        units = (Units.INDEX, Units.INDEX, Units.DECIMAL, Units.DECIMAL)
        cells: list[Cell] = []
        for leaf, value, unit in zip(CURVE_LEAVES, values, units, strict=True):
            if value is None:  # pragma: no cover - a classified curve has both prices
                cells.append(Cell.absent(leaf, CURVE, shape.reason, code=Reason.NO_CURVE))
                continue
            cell = Cell.measured(leaf, CURVE, value, units=unit, window=window, reason=shape.reason)
            cells.append(cell if shape.stale is None else cell.stale(shape.stale))
        return cells

    def _positioning(self, root: FuturesRootConfig, as_of: date) -> Cell:
        leaf = "positioning_percentile"
        contract = root.cot_series
        if contract is None:
            return Cell.inapplicable(
                leaf,
                POSITIONING,
                f"no CFTC contract is declared for {root.root}: positioning is not read for it",
                code=Reason.NO_POSITIONING,
            )
        reading = positioning(self._store, self._settings, contract, as_of=as_of)
        if reading.status is not PositioningStatus.OK or reading.percentile is None:
            return Cell.absent(leaf, POSITIONING, reading.reason, code=Reason.SHORT_SAMPLE)
        extreme = reading.percentile >= self._extreme or reading.percentile <= 1 - self._extreme
        note = (
            f"; extreme (either tail beyond {self._extreme:.0%}): conditioning only"
            + (", a separate price confirmation is required" if self._confirmation else "")
            if extreme
            else ""
        )
        return Cell.measured(
            leaf,
            POSITIONING,
            reading.percentile,
            units=Units.PERCENTILE,
            window=f"{reading.window} weekly reports",
            n_obs=reading.window,
            reason=f"{reading.reason} (report {reading.report_date}){note}",
        )

    def _cycle(self, root: FuturesRootConfig, reader: MacroReader, as_of: date) -> Cell:
        leaf = "cycle_percentile"
        window = f"{self._history_years}y"
        series_id = root.history_series
        if series_id is None:
            return Cell.inapplicable(
                leaf,
                CYCLE,
                f"no long price history is declared for {root.root}; a shorter series cannot "
                f"stand in for {self._history_years} years",
                code=Reason.SERIES_UNDECLARED,
                window=window,
            )
        series = reader.series(series_id)
        start = as_of - timedelta(days=round(self._history_years * self._basis))
        values = (
            []
            if series is None
            else [o.value for o in series.observations if o.reference_date >= start]
        )
        if not values:
            return Cell.absent(
                leaf, CYCLE, reader.absence(series_id), code=Reason.SERIES_ABSENT, window=window
            )
        rank = percentile_of(values, values[-1])
        if rank is None:  # pragma: no cover - a non-empty series of finite values always ranks
            return Cell.absent(leaf, CYCLE, "no finite value", code=Reason.NON_FINITE)
        return Cell.measured(
            leaf,
            CYCLE,
            rank,
            units=Units.PERCENTILE,
            window=f"{window} ({len(values)} observations)",
            n_obs=len(values),
            reason=f"{series_id} ranked over the long cycle window, never a five-year percentile",
        )

    def _inventory_cells(
        self, root: FuturesRootConfig, reader: MacroReader, as_of: date
    ) -> list[Cell]:
        leaves = ("inventory_level", "inventory_deviation_sd")
        window = f"{self._inventory.seasonal_window_years}y seasonal band"
        series_id = root.inventory_series
        if series_id is None:
            return [
                Cell.inapplicable(
                    leaf,
                    INVENTORY,
                    f"no inventory series is declared for {root.root}",
                    code=Reason.SERIES_UNDECLARED,
                )
                for leaf in leaves
            ]
        series = reader.series(series_id)
        start = as_of - timedelta(days=round(self._inventory.seasonal_window_years * self._basis))
        observations = (
            [] if series is None else [o for o in series.observations if o.reference_date >= start]
        )
        if not observations:
            reason = reader.absence(series_id)
            return [
                Cell.absent(leaf, INVENTORY, reason, code=Reason.SERIES_ABSENT, window=window)
                for leaf in leaves
            ]
        latest = observations[-1]
        week = latest.reference_date.isocalendar().week
        year = latest.reference_date.isocalendar().year
        if self._seasonal:
            band = [
                o.value
                for o in observations[:-1]
                if o.reference_date.isocalendar().year != year
                and week_distance(o.reference_date.isocalendar().week, week)
                <= self._inventory.seasonal_week_halfwidth
            ]
            basis = f"ISO week {week} in prior years"
        else:
            band = [o.value for o in observations[:-1]]
            basis = "the rolling window"
        level = Cell.measured(
            leaves[0],
            INVENTORY,
            latest.value,
            units=Units.INDEX,
            window=f"{latest.reference_date}",
            reason=f"{series_id} for the week of {latest.reference_date}",
        )
        centre, spread = mean(band), stdev(band)
        if (
            centre is None
            or spread is None
            or spread <= 0
            or len(band) < self._inventory.min_band_observations
        ):
            return [
                level,
                Cell.absent(
                    leaves[1],
                    INVENTORY,
                    f"only {len(band)} comparable reading(s) for {basis}, against a floor of "
                    f"{self._inventory.min_band_observations}: a band needs a count and a "
                    "dispersion before a deviation from it means anything",
                    code=Reason.SHORT_SAMPLE,
                    window=window,
                ),
            ]
        return [
            level,
            Cell.measured(
                leaves[1],
                INVENTORY,
                (latest.value - centre) / spread,
                units=Units.SIGMA,
                window=f"{window} ({len(band)} readings)",
                n_obs=len(band),
                reason=f"against {basis}: mean {centre:g}, standard deviation {spread:g}",
            ),
        ]


class CurveContangoCheck:
    """Lite's contango rule for the ``es`` eligibility policy (Session 8's ``ContangoCheck``),
    judged on the curve known on ``as_of``: a futures-tracking product passes only while its
    root's curve is classified and inside the ceiling. Everything else fails closed."""

    def __init__(self, engine: CommodityEngine, *, as_of: date) -> None:
        self._engine = engine
        self._as_of = as_of

    def assess(self, root: str) -> ContangoVerdict:
        shape = self._engine.curve_shape(root, as_of=self._as_of)
        contango = shape.contango
        ceiling = self._engine.contango_ceiling
        if contango is None or shape.stale is not None:
            return ContangoVerdict(
                False,
                f"the contango rule cannot be judged on {root}'s curve: "
                f"{shape.stale or shape.reason}",
            )
        if contango > ceiling:
            return ContangoVerdict(
                False,
                f"{root} is in contango at {contango:.1%} annualised, beyond the {ceiling:.1%} "
                "ceiling for a futures-tracking wrapper",
            )
        return ContangoVerdict(True, f"{root}'s curve passes the contango rule: {shape.reason}")


# ── the view ──


def wrapper_view(
    store: Store, settings: ObliskConfig, *, as_of: date
) -> list[tuple[str, MeasurementStatus | None, WrapperChoice]]:
    """Each tracked root's wrapper choice on ``as_of`` with the status of its curve leaf."""
    engine = CommodityEngine(store, settings)
    session = measurements_repo.measured_session(store, EntityKind.COMMODITY_ROOT, as_of=as_of)
    stored = (
        {}
        if session is None
        else {
            row.entity_id: row.status
            for row in measurements_repo.session_measurements(
                store, EntityKind.COMMODITY_ROOT, as_of=session, leaf="contango_annualised"
            )
        }
    )
    return [
        (
            root.root,
            stored.get(root.root),
            engine.wrapper(engine.curve_shape(root.root, as_of=as_of)),
        )
        for root in settings.universe.futures_roots
        if root.tracked
    ]
