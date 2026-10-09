"""Commodities: the curve, the wrapper it dictates, positioning, inventory, proxies.

Commodities are a primary regime layer here, not a satellite, and four rules
shape this module.

**The curve decides the instrument, not just the view.** Steep backwardation
signals physical tightness and is directionally supportive of a long position.
Steep contango does the opposite and, over a ninety-to-two-hundred-day hold, a
futures-tracking wrapper bleeds roll continuously **whether or not the thesis is
right**. Above ``commodities.term_structure.contango_max_annualised_for_futures_wrapper``
that wrapper is rejected and the view goes through producer equity or a
physically-backed ETC instead. Every commodity read carries a wrapper
recommendation for that reason — there is no path that returns a view without
one.

**A five-year percentile is never cycle position.** Oil runs multi-decade
supercycles and uranium spent fifteen years in a bear market, so a five-year
percentile computed today anchors on the recent regime and is actively
misleading about where the cycle is. :class:`PositioningRead` and
:class:`CyclePosition` are separate types with separate windows, and
:class:`CyclePosition` refuses to be constructed on a window shorter than
``commodities.history.monthly_window_years``.

**Positioning conditions, it does not time.** Speculative positioning at an
extreme means an unwind would be violent, which is what the mandate hunts, but
extremes persist for months. ``commodities.cot.use`` makes it a conditioning
variable and ``commodities.cot.require_price_confirmation`` demands a separate
trigger before acting.

**Inventory is compared to the SEASONAL band.** Crude builds every spring
whatever the regime. A rolling-window comparison reads that as signal, so the
reference is the same week in prior years.

Reading the futures curve is a DATA operation. ``search_futures`` and its
Client Portal equivalent stay in use precisely because the curve drives wrapper
selection; the contract itself is never a candidate, and nothing in this module
returns one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Config
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..store.db import Store
from ..util.timeutil import iso_date, utc_stamp
from .frames import (
    FREQ_MONTHLY,
    bar_series,
    contract_id_for_symbol,
    curve,
    curve_front_history,
    macro_series,
)
from .stats import log_returns, mean, ols, percentile_of, stdev

log = get_logger("compute.commodity")

#: Curve classifications.
SHAPE_CONTANGO = "contango"
SHAPE_BACKWARDATION = "backwardation"
SHAPE_FLAT = "flat"
SHAPE_UNKNOWN = "n/a"

#: Wrapper identifiers, as declared in commodities.preferred_wrapper_order.
WRAPPER_FUTURES_TRACKING = "futures_tracking_wrapper"

#: Cell statuses, matching the grid.
STATUS_OK = "ok"
STATUS_NA = "n/a"

#: Proxy reference kinds, as declared in universe.yaml.
REFERENCE_MACRO = "macro"
REFERENCE_CURVE = "curve"
REFERENCE_NONE = "none"

#: Months in a year, for turning a lag in months into calendar days.
MONTHS_PER_YEAR = 12

#: Days in an ISO week, for the seasonal band's half-width.
DAYS_PER_WEEK = 7

#: Month keys are "YYYY-MM": the leading characters of any ISO stamp.
MONTH_KEY_FORMAT = "%Y-%m"
_MONTH_KEY_LENGTH = len("YYYY-MM")


@dataclass(frozen=True)
class CurveShape:
    """One session's term structure, classified."""

    root: str
    as_of: date
    front_expiry: date | None
    front_price: float | None
    deferred_expiry: date | None
    deferred_price: float | None
    annualised_roll: float | None
    shape: str
    contango_threshold: float
    status: str = STATUS_OK
    reason: str | None = None

    @property
    def contango_magnitude(self) -> float | None:
        """How much the curve costs to hold, annualised. Negative in backwardation.

        ``annualised_roll`` is the front against the deferred, so a positive roll
        is backwardation — the deferred is cheaper and rolling forward earns.
        """
        return None if self.annualised_roll is None else -self.annualised_roll

    @property
    def beyond_contango_threshold(self) -> bool:
        magnitude = self.contango_magnitude
        return magnitude is not None and magnitude > self.contango_threshold


@dataclass(frozen=True)
class WrapperRecommendation:
    """Which wrapper the curve permits, and which it rules out."""

    root: str
    recommended: str | None
    rejected: str | None
    alternatives: tuple[str, ...]
    reason: str

    @property
    def rejects_futures_tracking(self) -> bool:
        return self.rejected == WRAPPER_FUTURES_TRACKING


@dataclass(frozen=True)
class PositioningRead:
    """CFTC speculative positioning, as a percentile of its OWN window.

    The ranked quantity is net non-commercial positioning as a SHARE of open
    interest, not a contract count: open interest moves a great deal over a
    multi-year window, so a percentile of the count ranks how large the market
    became as much as how crowded it is. See ``macro/fetchers.py``'s
    ``CFTC_NET_UNITS`` and migration 015.

    The window travels with the figure everywhere it goes. This is a
    **conditioning** variable: an extreme means an unwind would be violent, not
    that one is due.
    """

    root: str
    series_id: str | None
    percentile: float | None
    window_years: int
    observations: int
    extreme: bool
    requires_price_confirmation: bool
    status: str = STATUS_OK
    reason: str | None = None

    def describe(self) -> str:
        if self.percentile is None:
            return f"positioning unavailable: {self.reason}"
        return (
            f"{self.percentile:.0%} of its {self.window_years}-year range "
            f"({self.observations} weekly observations). Conditioning only: "
            "a separate price confirmation is required before acting."
        )


@dataclass(frozen=True)
class CyclePosition:
    """Where the price sits in the LONG cycle. Never a five-year percentile.

    Refuses construction on a window shorter than
    ``commodities.history.monthly_window_years``: a percentile over five years of
    crude anchors on the post-pandemic period and says nothing about the cycle.
    """

    root: str
    series_id: str | None
    percentile: float | None
    window_years: int
    observations: int
    frequency: str = FREQ_MONTHLY
    status: str = STATUS_OK
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.percentile is not None and self.window_years < _MIN_CYCLE_WINDOW_YEARS:
            raise ValueError(
                f"a {self.window_years}-year window cannot express cycle position. "
                f"Commodity cycles run {_MIN_CYCLE_WINDOW_YEARS} years and longer; a "
                "shorter percentile anchors on the current regime and is actively "
                "misleading. Report it as a percentile with its own window instead."
            )

    def describe(self) -> str:
        if self.percentile is None:
            return f"cycle position unavailable: {self.reason}"
        return (
            f"{self.percentile:.0%} of its {self.window_years}-year {self.frequency} "
            f"range ({self.observations} observations)"
        )


@dataclass(frozen=True)
class InventoryRead:
    """Deviation from the SEASONAL band, not from a rolling window."""

    root: str
    series_id: str | None
    level: float | None
    seasonal_mean: float | None
    seasonal_sd: float | None
    deviation_sd: float | None
    window_years: int
    reference_week: int | None
    observations: int
    status: str = STATUS_OK
    reason: str | None = None

    def describe(self) -> str:
        if self.deviation_sd is None:
            return f"inventory unavailable: {self.reason}"
        return (
            f"{self.deviation_sd:+.1f} standard deviations from the "
            f"{self.window_years}-year band for ISO week {self.reference_week} "
            f"({self.observations} comparable observations)"
        )


@dataclass(frozen=True)
class ProxyQuality:
    """A proxy equity's beta and R-squared against its reference commodity."""

    symbol: str
    root: str | None
    reference: str | None
    reference_kind: str
    beta: float | None
    r2: float | None
    observations: int
    window_months: int
    min_r2: float
    reliable: bool
    reason: str | None = None
    note: str | None = None

    def describe(self) -> str:
        if not self.reliable:
            return (
                f"{self.symbol} is NOT a usable commodity exposure: {self.reason}. "
                "It must not be presented as one."
            )
        return (
            f"{self.symbol} beta {self.beta:.2f} to {self.reference} with R-squared "
            f"{self.r2:.2f} over {self.observations} monthly observations "
            f"(floor {self.min_r2:.2f})"
        )


@dataclass(frozen=True)
class AgriLag:
    lag_months: int
    correlation: float | None
    observations: int


@dataclass(frozen=True)
class AgriLinkage:
    """Fertiliser input costs leading crop prices, at the configured lags."""

    driver_series: str
    crop_root: str
    lags: tuple[AgriLag, ...]
    status: str = STATUS_OK
    reason: str | None = None


@dataclass(frozen=True)
class CommodityState:
    """One root, fully read. Every view carries its wrapper recommendation."""

    root: str
    bucket: str | None
    as_of: date
    tradable: bool
    macro_signal: bool
    untradeable_reason: str | None
    curve: CurveShape
    wrapper: WrapperRecommendation
    positioning: PositioningRead
    cycle: CyclePosition
    inventory: InventoryRead
    proxies: tuple[ProxyQuality, ...] = field(default_factory=tuple)
    status: str = STATUS_OK
    reason: str | None = None


class CommodityEngine:
    """The commodity layer, bound to config and the store once."""

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config

        commodities = config.section("commodities")
        self._buckets = [str(b) for b in commodities.require_list("buckets")]
        self._wrapper_order = [str(w) for w in commodities.require_list("preferred_wrapper_order")]
        self._contango_threshold = config.require_float(
            "commodities.term_structure.contango_max_annualised_for_futures_wrapper"
        )
        self._backwardation_directional = config.require_bool(
            "commodities.term_structure.backwardation_is_directional"
        )

        cot = config.section("commodities.cot")
        self._cot_extreme = cot.require_float("extreme_percentile")
        self._cot_window_years = cot.require_int("percentile_window_years")
        self._cot_use = cot.require_str("use")
        self._cot_requires_confirmation = cot.require_bool("require_price_confirmation")

        inventory = config.section("commodities.inventory")
        self._inventory_window_years = inventory.require_int("seasonal_window_years")
        self._inventory_halfwidth = inventory.require_int("seasonal_week_halfwidth")
        self._inventory_min_band = inventory.require_int("min_band_observations")
        self._seasonal_adjustment = config.require_bool("commodities.history.seasonal_adjustment")

        proxies = config.section("commodities.proxies")
        self._min_r2 = proxies.require_float("min_r2")
        self._min_proxy_months = proxies.require_int("min_observations_months")
        self._require_beta = proxies.require_bool("require_commodity_beta")

        self._monthly_window_years = config.require_int("commodities.history.monthly_window_years")
        self._nitrogen_driver = config.require_str("commodities.nitrogen_input_driver")
        self._track_agri_lag = config.require_bool(
            "commodities.agri_linkage.track_fertiliser_to_crop_lag"
        )
        self._agri_lags = [
            int(lag) for lag in config.require_list("commodities.agri_linkage.lag_months")
        ]
        self._agri_driver_root = config.require_str("commodities.agri_linkage.driver_root")
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")

    # ── roots ──

    def tracked_roots(self) -> list[dict[str, Any]]:
        rows = self._store.execute(
            "SELECT root, bucket, seasonal, tradable, macro_signal, untradeable_reason, "
            "cot_series, inventory_series, history_series FROM futures_roots "
            "WHERE tracked = 1 ORDER BY root"
        ).fetchall()
        return [dict(row) for row in rows]

    def root_spec(self, root: str) -> dict[str, Any] | None:
        row = self._store.execute(
            "SELECT root, bucket, seasonal, tradable, macro_signal, untradeable_reason, "
            "cot_series, inventory_series, history_series FROM futures_roots WHERE root = ?",
            (root,),
        ).fetchone()
        return None if row is None else dict(row)

    # ── term structure ──

    def curve_shape(self, root: str, *, as_of: date | None = None) -> CurveShape:
        """Classify the curve against the configured contango threshold.

        ``as_of`` bounds which snapshot "the curve" means. It used to reach only
        the reason string while the read itself was unbounded, so a commodity
        state rebuilt for a past date classified contango — and therefore chose a
        wrapper — off a curve collected after the decision it was pricing. That
        was the one store read in this layer still doing it.
        """
        points = [
            point
            for point in curve(self._store, root, until=as_of)
            if point.price and point.price > 0
        ]
        if len(points) < _MIN_CURVE_POINTS:
            return CurveShape(
                root=root,
                as_of=as_of or date.today(),
                front_expiry=None,
                front_price=None,
                deferred_expiry=None,
                deferred_price=None,
                annualised_roll=None,
                shape=SHAPE_UNKNOWN,
                contango_threshold=self._contango_threshold,
                status=STATUS_NA,
                reason=(
                    f"{root} has {len(points)} usable curve point(s); a single listed "
                    "expiry has no shape. Curve history is not retrievable "
                    "retrospectively, so this is a gap that cannot be backfilled."
                ),
            )

        front, deferred = points[0], points[1]
        roll = deferred.annualised_roll if deferred.annualised_roll is not None else None
        if roll is None:
            return CurveShape(
                root=root,
                as_of=deferred.snapshot_date,
                front_expiry=front.expiry,
                front_price=front.price,
                deferred_expiry=deferred.expiry,
                deferred_price=deferred.price,
                annualised_roll=None,
                shape=SHAPE_UNKNOWN,
                contango_threshold=self._contango_threshold,
                status=STATUS_NA,
                reason=(
                    f"{root} carries no annualised roll on its second expiry, so the "
                    "shape cannot be classified. A flat classification would drag the "
                    "wrapper decision toward 'fine'."
                ),
            )

        if abs(roll) <= _FLAT_TOLERANCE:
            shape = SHAPE_FLAT
        else:
            shape = SHAPE_BACKWARDATION if roll > 0 else SHAPE_CONTANGO
        return CurveShape(
            root=root,
            as_of=deferred.snapshot_date,
            front_expiry=front.expiry,
            front_price=front.price,
            deferred_expiry=deferred.expiry,
            deferred_price=deferred.price,
            annualised_roll=roll,
            shape=shape,
            contango_threshold=self._contango_threshold,
        )

    def wrapper_recommendation(self, shape: CurveShape) -> WrapperRecommendation:
        """The wrapper the curve permits. Every commodity view carries one.

        The order comes from ``commodities.preferred_wrapper_order``, which is
        PRIIPs-aware: European physically-backed ETCs generally carry KIDs and are
        reachable where US commodity ETFs are not.
        """
        order = list(self._wrapper_order)
        if shape.status != STATUS_OK:
            return WrapperRecommendation(
                root=shape.root,
                recommended=None,
                rejected=None,
                alternatives=tuple(order),
                reason=(
                    f"the curve could not be classified ({shape.reason}), so no wrapper "
                    "can be recommended. Check the curve before selecting an instrument, "
                    "every time."
                ),
            )

        if shape.beyond_contango_threshold:
            permitted = [w for w in order if w != WRAPPER_FUTURES_TRACKING]
            magnitude = shape.contango_magnitude or 0.0
            return WrapperRecommendation(
                root=shape.root,
                recommended=permitted[0] if permitted else None,
                rejected=WRAPPER_FUTURES_TRACKING,
                alternatives=tuple(permitted),
                reason=(
                    f"{shape.root} is in contango at {magnitude:.1%} annualised, beyond the "
                    f"{shape.contango_threshold:.1%} ceiling. A futures-tracking wrapper "
                    "bleeds roll continuously over a 90-200 day hold whether or not the "
                    f"thesis is right. Express the view through {permitted[0]} or "
                    f"{permitted[1] if len(permitted) > 1 else 'a producer equity'} instead."
                ),
            )

        directional = (
            " Backwardation is directionally supportive of long exposure and the positive "
            "roll helps; it is not a dislocation flag."
            if shape.shape == SHAPE_BACKWARDATION and self._backwardation_directional
            else ""
        )
        return WrapperRecommendation(
            root=shape.root,
            recommended=order[0] if order else None,
            rejected=None,
            alternatives=tuple(order),
            reason=(
                f"{shape.root} is in {shape.shape} at "
                f"{abs(shape.annualised_roll or 0.0):.1%} annualised, inside the "
                f"{shape.contango_threshold:.1%} ceiling, so every wrapper in the "
                f"preference order remains available.{directional}"
            ),
        )

    # ── positioning ──

    def positioning(self, root: str, *, as_of: date) -> PositioningRead:
        """CFTC speculative positioning as a percentile of its own window."""
        spec = self.root_spec(root) or {}
        series_id = spec.get("cot_series")
        if not series_id:
            return PositioningRead(
                root=root,
                series_id=None,
                percentile=None,
                window_years=self._cot_window_years,
                observations=0,
                extreme=False,
                requires_price_confirmation=self._cot_requires_confirmation,
                status=STATUS_NA,
                reason=(
                    f"no COT series is declared for {root} in universe.yaml. Positioning "
                    "is the only flow measure in the whole grid; everything else is "
                    "price-derived."
                ),
            )

        since = as_of - timedelta(days=round(self._cot_window_years * self._calendar_days_per_year))
        series = macro_series(self._store, str(series_id), since=since)
        # Release-aligned: CFTC's Tuesday report is public on the Friday, and
        # that lag is stored on the row.
        values = series.values_known_at(as_of)
        units = series.units_known_at(as_of)
        if len(units) > 1:
            return PositioningRead(
                root=root,
                series_id=str(series_id),
                percentile=None,
                window_years=self._cot_window_years,
                observations=len(values),
                extreme=False,
                requires_price_confirmation=self._cot_requires_confirmation,
                status=STATUS_NA,
                reason=(
                    f"{series_id} carries {len(units)} different units inside the window "
                    f"({', '.join(sorted(units))}). A percentile ranks a value against "
                    "others of the same quantity; this one would rank two measurements "
                    "against each other. Re-collect the series so one definition covers it."
                ),
            )
        if not values:
            return PositioningRead(
                root=root,
                series_id=str(series_id),
                percentile=None,
                window_years=self._cot_window_years,
                observations=0,
                extreme=False,
                requires_price_confirmation=self._cot_requires_confirmation,
                status=STATUS_NA,
                reason=f"{series_id} holds no observations inside the window",
            )

        percentile = percentile_of(values, values[-1])
        extreme = percentile is not None and (
            percentile >= self._cot_extreme or percentile <= 1.0 - self._cot_extreme
        )
        return PositioningRead(
            root=root,
            series_id=str(series_id),
            percentile=percentile,
            window_years=self._cot_window_years,
            observations=len(values),
            extreme=bool(extreme),
            requires_price_confirmation=self._cot_requires_confirmation,
        )

    # ── cycle position ──

    def cycle_position(self, root: str, *, as_of: date) -> CyclePosition:
        """Where the price sits in the long cycle, from the LONG history.

        Deliberately a different type from :class:`PositioningRead`, over a
        different window, so a five-year percentile can never be printed as this.
        """
        spec = self.root_spec(root) or {}
        series_id = spec.get("history_series")
        if not series_id:
            return CyclePosition(
                root=root,
                series_id=None,
                percentile=None,
                window_years=self._monthly_window_years,
                observations=0,
                status=STATUS_NA,
                reason=(
                    f"no long price history is declared for {root}. Cycle position needs "
                    f"{self._monthly_window_years} years; the {self._cot_window_years}-year "
                    "series cannot stand in for it."
                ),
            )

        since = as_of - timedelta(
            days=round(self._monthly_window_years * self._calendar_days_per_year)
        )
        series = macro_series(self._store, str(series_id), since=since)
        values = series.values_known_at(as_of)
        if not values:
            return CyclePosition(
                root=root,
                series_id=str(series_id),
                percentile=None,
                window_years=self._monthly_window_years,
                observations=0,
                status=STATUS_NA,
                reason=f"{series_id} holds no observations inside the window",
            )
        return CyclePosition(
            root=root,
            series_id=str(series_id),
            percentile=percentile_of(values, values[-1]),
            window_years=self._monthly_window_years,
            observations=len(values),
        )

    # ── inventory ──

    def inventory(self, root: str, *, as_of: date) -> InventoryRead:
        """Deviation from the same ISO week in prior years, in standard deviations."""
        spec = self.root_spec(root) or {}
        series_id = spec.get("inventory_series")
        if not series_id:
            return InventoryRead(
                root=root,
                series_id=None,
                level=None,
                seasonal_mean=None,
                seasonal_sd=None,
                deviation_sd=None,
                window_years=self._inventory_window_years,
                reference_week=None,
                observations=0,
                status=STATUS_NA,
                reason=f"no inventory series is declared for {root}",
            )

        since = as_of - timedelta(
            days=round(self._inventory_window_years * self._calendar_days_per_year)
        )
        series = macro_series(self._store, str(series_id), since=since)
        observations = [
            o for o in series.observations if o.release_date is not None and o.release_date <= as_of
        ]
        if not observations:
            return InventoryRead(
                root=root,
                series_id=str(series_id),
                level=None,
                seasonal_mean=None,
                seasonal_sd=None,
                deviation_sd=None,
                window_years=self._inventory_window_years,
                reference_week=None,
                observations=0,
                status=STATUS_NA,
                reason=f"{series_id} holds no observations inside the window",
            )

        latest = observations[-1]
        reference_week = latest.reference_date.isocalendar().week
        reference_year = latest.reference_date.isocalendar().year
        if self._seasonal_adjustment:
            # PRIOR YEARS, and the year filter is the point. Matching on the ISO
            # week alone let the CURRENT year's neighbouring weeks into the band
            # — the two readings most correlated with the level being tested —
            # which pulled the mean towards it and widened the sd, understating
            # every seasonal deviation. "The same week in prior years" is what
            # config.yaml and SKILL.md both say this compares against.
            band = [
                o.value
                for o in observations[:-1]
                if o.reference_date.isocalendar().year != reference_year
                and _week_distance(o.reference_date.isocalendar().week, reference_week)
                <= self._inventory_halfwidth
            ]
            basis = f"ISO week {reference_week} in prior years"
        else:
            band = [o.value for o in observations[:-1]]
            basis = "a rolling window"

        band_mean = mean(band)
        band_sd = stdev(band)
        if (
            band_mean is None
            or band_sd is None
            or band_sd <= 0
            or len(band) < self._inventory_min_band
        ):
            return InventoryRead(
                root=root,
                series_id=str(series_id),
                level=latest.value,
                seasonal_mean=band_mean,
                seasonal_sd=band_sd,
                deviation_sd=None,
                window_years=self._inventory_window_years,
                reference_week=reference_week,
                observations=len(band),
                status=STATUS_NA,
                reason=(
                    f"only {len(band)} comparable observation(s) for {basis}, against a "
                    f"floor of {self._inventory_min_band}; a band needs both a count and "
                    "dispersion before a deviation from it means anything"
                ),
            )
        return InventoryRead(
            root=root,
            series_id=str(series_id),
            level=latest.value,
            seasonal_mean=band_mean,
            seasonal_sd=band_sd,
            deviation_sd=(latest.value - band_mean) / band_sd,
            window_years=self._inventory_window_years,
            reference_week=reference_week,
            observations=len(band),
        )

    # ── proxies ──

    def proxy_map(self) -> list[dict[str, Any]]:
        rows = self._store.execute(
            "SELECT symbol, root, reference, reference_kind, note FROM commodity_proxy_map "
            "ORDER BY symbol"
        ).fetchall()
        return [dict(row) for row in rows]

    def proxy_quality(self, entry: dict[str, Any], *, as_of: date) -> ProxyQuality:
        """Monthly beta and R-squared against the declared reference series.

        Below ``commodities.proxies.min_r2`` the proxy is flagged unreliable and
        must not be presented as commodity exposure at all. A diversified
        specialty chemicals company is not lithium, and an industrial gases
        oligopoly on contracted pricing is not helium.
        """
        symbol = str(entry["symbol"])
        kind = str(entry.get("reference_kind") or REFERENCE_NONE)
        reference = entry.get("reference")
        note = entry.get("note")
        base = {
            "symbol": symbol,
            "root": entry.get("root"),
            "reference": reference,
            "reference_kind": kind,
            "window_months": self._min_proxy_months,
            "min_r2": self._min_r2,
            "note": note,
        }

        if kind == REFERENCE_NONE or not reference:
            return ProxyQuality(
                **base,
                beta=None,
                r2=None,
                observations=0,
                reliable=False,
                reason=(
                    "no free reference price series exists for this commodity, so no beta "
                    f"can be computed. {note or ''}".strip()
                ),
            )
        if not self._require_beta:
            return ProxyQuality(
                **base,
                beta=None,
                r2=None,
                observations=0,
                reliable=False,
                reason="commodities.proxies.require_commodity_beta is false",
            )

        proxy_returns = self._monthly_returns_for_symbol(symbol, as_of=as_of)
        reference_returns = self._monthly_reference_returns(str(reference), kind, as_of=as_of)
        paired = _pair_on_month(proxy_returns, reference_returns)

        if len(paired) < self._min_proxy_months:
            return ProxyQuality(
                **base,
                beta=None,
                r2=None,
                observations=len(paired),
                reliable=False,
                reason=(
                    f"only {len(paired)} paired monthly observation(s) against "
                    f"{reference}, below the {self._min_proxy_months} floor. A beta on "
                    "eight months is noise wearing a coefficient."
                ),
            )

        fit = ols([y for _, y, _ in paired], [x for _, _, x in paired])
        if fit is None:
            return ProxyQuality(
                **base,
                beta=None,
                r2=None,
                observations=len(paired),
                reliable=False,
                reason=f"the regression against {reference} is undefined over this sample",
            )
        reliable = fit.r2 >= self._min_r2
        return ProxyQuality(
            **base,
            beta=fit.beta,
            r2=fit.r2,
            observations=fit.observations,
            reliable=reliable,
            reason=(
                None
                if reliable
                else (
                    f"R-squared {fit.r2:.2f} against {reference} is below the "
                    f"{self._min_r2:.2f} floor. {note or ''}".strip()
                )
            ),
        )

    def proxies_for(self, root: str, *, as_of: date) -> list[ProxyQuality]:
        return [
            self.proxy_quality(entry, as_of=as_of)
            for entry in self.proxy_map()
            if str(entry.get("root") or "").upper() == root.upper()
        ]

    # ── agriculture ──

    def agri_linkage(self, crop_root: str, *, as_of: date) -> AgriLinkage:
        """Fertiliser input costs leading crop prices, at the configured lags.

        Expensive nitrogen leads to under-application, which leads to weaker
        yields, which leads to higher crop prices — with a lag measured in
        seasons, not weeks. ``commodities.nitrogen_input_driver`` names the input
        because gas is 70-80% of ammonia cost.
        """
        if not self._track_agri_lag:
            return AgriLinkage(
                driver_series=self._nitrogen_driver,
                crop_root=crop_root,
                lags=(),
                status=STATUS_NA,
                reason="commodities.agri_linkage.track_fertiliser_to_crop_lag is false",
            )

        driver_series = self._driver_series_id()
        driver = (
            []
            if driver_series is None
            else self._monthly_reference_returns(driver_series, REFERENCE_MACRO, as_of=as_of)
        )
        crop = self._monthly_reference_returns(crop_root, REFERENCE_CURVE, as_of=as_of)

        # Every configured lag is reported, whether or not it resolved. "We looked
        # at 6 and 18 months and had nothing" is a coverage statement; an empty
        # list is silence.
        lags: list[AgriLag] = []
        for lag in self._agri_lags:
            paired = _pair_on_month(crop, driver, lag_months=lag)
            correlation = None
            if len(paired) >= _MIN_LINKAGE_OBSERVATIONS:
                fit = ols([y for _, y, _ in paired], [x for _, _, x in paired])
                correlation = None if fit is None else fit.beta
            lags.append(AgriLag(lag_months=lag, correlation=correlation, observations=len(paired)))

        resolved = any(lag.correlation is not None for lag in lags)
        if driver_series is None:
            reason = (
                f"the {self._agri_driver_root} root declares no history_series, so the "
                f"{self._nitrogen_driver} input-cost leg cannot be measured"
            )
        elif not resolved:
            reason = (
                f"fewer than {_MIN_LINKAGE_OBSERVATIONS} paired monthly observations at "
                "any configured lag. The crop leg reads the self-collected curve history, "
                "which starts empty and compounds."
            )
        else:
            reason = None
        return AgriLinkage(
            driver_series=driver_series or self._nitrogen_driver,
            crop_root=crop_root,
            lags=tuple(lags),
            status=STATUS_OK if resolved else STATUS_NA,
            reason=reason,
        )

    # ── the whole read ──

    def state(self, root: str, *, as_of: date) -> CommodityState:
        """Every reading for one root, with its wrapper recommendation attached."""
        spec = self.root_spec(root)
        if spec is None:
            raise ConfigError(
                f"{root} is not a declared futures root. Declare it under futures_roots "
                "in universe.yaml and seed with: obliskiq-collect universe"
            )
        shape = self.curve_shape(root, as_of=as_of)
        wrapper = self.wrapper_recommendation(shape)
        positioning = self.positioning(root, as_of=as_of)
        cycle = self.cycle_position(root, as_of=as_of)
        inventory = self.inventory(root, as_of=as_of)
        proxies = tuple(self.proxies_for(root, as_of=as_of))

        gaps = [
            read.reason
            for read in (shape, positioning, cycle, inventory)
            if read.status != STATUS_OK and read.reason
        ]
        return CommodityState(
            root=root,
            bucket=spec.get("bucket"),
            as_of=as_of,
            tradable=bool(spec.get("tradable")),
            macro_signal=bool(spec.get("macro_signal")),
            untradeable_reason=spec.get("untradeable_reason"),
            curve=shape,
            wrapper=wrapper,
            positioning=positioning,
            cycle=cycle,
            inventory=inventory,
            proxies=proxies,
            status=STATUS_OK if not gaps else STATUS_NA,
            reason="; ".join(gaps) if gaps else None,
        )

    def state_all(self, *, as_of: date) -> list[CommodityState]:
        return [self.state(str(row["root"]), as_of=as_of) for row in self.tracked_roots()]

    # ── persistence ──

    def write(self, states: Sequence[CommodityState], *, as_of: date) -> int:
        if not states:
            return 0
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO commodity_state (
                    as_of, root, bucket, front_expiry, front_price, deferred_expiry,
                    deferred_price, annualised_roll, curve_shape, wrapper_recommendation,
                    wrapper_rejected, wrapper_reason, cot_percentile, cot_window_years,
                    cot_extreme, price_confirmation_required, inventory_deviation,
                    inventory_window_years, cycle_position_pctile, cycle_window_years,
                    status, reason, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (as_of, root) DO UPDATE SET
                    bucket = excluded.bucket,
                    front_expiry = excluded.front_expiry,
                    front_price = excluded.front_price,
                    deferred_expiry = excluded.deferred_expiry,
                    deferred_price = excluded.deferred_price,
                    annualised_roll = excluded.annualised_roll,
                    curve_shape = excluded.curve_shape,
                    wrapper_recommendation = excluded.wrapper_recommendation,
                    wrapper_rejected = excluded.wrapper_rejected,
                    wrapper_reason = excluded.wrapper_reason,
                    cot_percentile = excluded.cot_percentile,
                    cot_window_years = excluded.cot_window_years,
                    cot_extreme = excluded.cot_extreme,
                    price_confirmation_required = excluded.price_confirmation_required,
                    inventory_deviation = excluded.inventory_deviation,
                    inventory_window_years = excluded.inventory_window_years,
                    cycle_position_pctile = excluded.cycle_position_pctile,
                    cycle_window_years = excluded.cycle_window_years,
                    status = excluded.status,
                    reason = excluded.reason,
                    computed_at = excluded.computed_at
                """,
                [
                    (
                        iso_date(as_of),
                        state.root,
                        state.bucket,
                        _iso_or_none(state.curve.front_expiry),
                        state.curve.front_price,
                        _iso_or_none(state.curve.deferred_expiry),
                        state.curve.deferred_price,
                        state.curve.annualised_roll,
                        state.curve.shape,
                        state.wrapper.recommended,
                        state.wrapper.rejected,
                        state.wrapper.reason,
                        state.positioning.percentile,
                        state.positioning.window_years,
                        int(state.positioning.extreme),
                        int(state.positioning.requires_price_confirmation),
                        state.inventory.deviation_sd,
                        state.inventory.window_years,
                        state.cycle.percentile,
                        state.cycle.window_years,
                        state.status,
                        state.reason,
                        stamp,
                    )
                    for state in states
                ],
            )
        return len(states)

    def write_proxies(self, proxies: Sequence[ProxyQuality], *, as_of: date) -> int:
        rows = []
        for proxy in proxies:
            contract_id = contract_id_for_symbol(self._store, proxy.symbol)
            if contract_id is None:
                log.warning(
                    "proxy is not in the universe; its regression is not persisted",
                    extra={"symbol": proxy.symbol},
                )
                continue
            rows.append(
                (
                    iso_date(as_of),
                    contract_id,
                    proxy.symbol,
                    proxy.reference or proxy.reference_kind,
                    proxy.beta,
                    proxy.r2,
                    proxy.observations,
                    proxy.window_months,
                    int(proxy.reliable),
                    proxy.reason,
                    utc_stamp(),
                )
            )
        if not rows:
            return 0
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO commodity_proxies (
                    as_of, contract_id, symbol, reference_series, beta, r2, observations,
                    window_months, reliable, reason, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (as_of, contract_id, reference_series) DO UPDATE SET
                    beta = excluded.beta, r2 = excluded.r2,
                    observations = excluded.observations,
                    window_months = excluded.window_months,
                    reliable = excluded.reliable, reason = excluded.reason,
                    computed_at = excluded.computed_at
                """,
                rows,
            )
        return len(rows)

    # ── internals ──

    def _driver_series_id(self) -> str | None:
        """The declared long history for the input-cost root.

        The root is named in ``commodities.agri_linkage.driver_root`` rather than
        matched against a description: gas is 70-80% of ammonia cost, and which
        contract represents it is a data decision, not a string search.
        """
        row = self._store.execute(
            "SELECT history_series FROM futures_roots WHERE root = ?",
            (self._agri_driver_root.upper(),),
        ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def _monthly_returns_for_symbol(self, symbol: str, *, as_of: date) -> list[tuple[str, float]]:
        contract_id = contract_id_for_symbol(self._store, symbol)
        if contract_id is None:
            return []
        since = as_of - timedelta(
            days=round(self._monthly_window_years * self._calendar_days_per_year)
        )
        monthly = bar_series(self._store, contract_id, freq=FREQ_MONTHLY, since=since)
        if len(monthly) == 0:
            # Fall back to daily bars sampled to month end, which is what the
            # backfill actually holds for most equities.
            daily = bar_series(self._store, contract_id, since=since)
            return _month_end_returns(list(zip(daily.ts, daily.close, strict=True)))
        return _month_end_returns(list(zip(monthly.ts, monthly.close, strict=True)))

    def _monthly_reference_returns(
        self, reference: str, kind: str, *, as_of: date
    ) -> list[tuple[str, float]]:
        since = as_of - timedelta(
            days=round(self._monthly_window_years * self._calendar_days_per_year)
        )
        if kind == REFERENCE_CURVE:
            history = curve_front_history(self._store, reference.upper(), since=since)
            return _month_end_returns([(iso_date(day), price) for day, price in history])
        series = macro_series(self._store, reference, since=since)
        return _month_end_returns(
            [
                (iso_date(o.reference_date), o.value)
                for o in series.observations
                if o.release_date is not None and o.release_date <= as_of and o.value > 0
            ]
        )


#: Two rungs, the minimum for a curve to have a shape at all.
_MIN_CURVE_POINTS = 2

#: Below this annualised roll the curve is flat rather than either shape. Small
#: enough that it never masks a wrapper decision: the contango threshold in
#: config is an order of magnitude larger.
_FLAT_TOLERANCE = 0.001

#: Commodity cycles run this long and longer, which is why a shorter window
#: cannot express cycle position. Matches the intent of
#: commodities.history.monthly_window_years without depending on its value.
_MIN_CYCLE_WINDOW_YEARS = 10

#: A lagged linkage needs at least this many paired months to mean anything.
_MIN_LINKAGE_OBSERVATIONS = 12


def _iso_or_none(value: date | None) -> str | None:
    return None if value is None else iso_date(value)


def _week_distance(week: int, reference: int) -> int:
    """Distance between two ISO weeks, wrapping across the year boundary."""
    raw = abs(week - reference)
    return min(raw, _ISO_WEEKS_PER_YEAR - raw)


#: ISO weeks in a year, for wrapping the seasonal band across 1 January.
_ISO_WEEKS_PER_YEAR = 52


def _month_key(stamp: str) -> str:
    return stamp[:_MONTH_KEY_LENGTH]


def _month_end_returns(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Month-end levels turned into monthly log returns, keyed by month."""
    last_by_month: dict[str, float] = {}
    for stamp, value in points:
        if value is None or value <= 0:
            continue
        last_by_month[_month_key(stamp)] = float(value)
    months = sorted(last_by_month)
    levels = [last_by_month[month] for month in months]
    returns = log_returns(levels)
    # log_returns drops a pair when either side is unusable, and the levels here
    # are already filtered to positives, so the alignment holds one-for-one.
    return list(zip(months[1:], returns, strict=True))


def _pair_on_month(
    left: list[tuple[str, float]], right: list[tuple[str, float]], *, lag_months: int = 0
) -> list[tuple[str, float, float]]:
    """Align two monthly return series, optionally lagging the right one.

    ``lag_months`` shifts the right-hand series BACK, so a positive lag pairs this
    month's crop return with the input-cost return that many months earlier.
    """
    right_by_month = dict(right)
    out: list[tuple[str, float, float]] = []
    for month, value in left:
        key = _shift_month(month, -lag_months) if lag_months else month
        other = right_by_month.get(key)
        if other is None:
            continue
        out.append((month, value, other))
    return out


def _shift_month(month: str, delta: int) -> str:
    """Shift a ``YYYY-MM`` key by ``delta`` months, rolling the year over."""
    parsed = datetime.strptime(month, MONTH_KEY_FORMAT)
    total = parsed.year * MONTHS_PER_YEAR + (parsed.month - 1) + delta
    shifted = datetime(year=total // MONTHS_PER_YEAR, month=total % MONTHS_PER_YEAR + 1, day=1)
    return shifted.strftime(MONTH_KEY_FORMAT)
