"""The volatility smile, and reading an implied volatility off it at a given strike.

Why it exists: N(d2) is computed from **the strike's own implied volatility from the smile, not
the at-the-money volatility**. Constant-volatility Black-Scholes understates the terminal
probability on out-of-the-money strikes, so an at-the-money sigma produces probabilities that
are too low exactly where a zombie test looks, and a false positive forces a premature exit on a
position that was still alive.

Construction
------------

At each listed strike the smile takes the **out-of-the-money** contract: puts below spot, calls
at and above it. Under put-call parity both sides share one implied volatility, but the
out-of-the-money quote is nearly all time value, the part that responds to volatility. The
in-the-money quote is mostly intrinsic and its implied volatility is the noisiest number on the
chain; :func:`obliskiq.quant.bsm.to_out_of_the_money` converts before solving for the same reason.

Interpolation is **linear in strike** between the two listed strikes that bracket the target. A
twenty-strike daily snapshot does not support fitting an arbitrage-free surface, and pretending
otherwise would dress an interpolation up as a model. Beyond the outermost listed strike the
value is **clamped to the wing and labelled as clamped**, never extrapolated: the wing slope of a
truncated chain is an artefact of where collection stopped. Every read returns its provenance,
so a probability computed from a clamped wing is reported as such.

Hygiene
-------

A quote existing is not the same as a quote carrying information. Before any strike reaches the
smile it must clear four tests, and :class:`SmileHygiene` records how many strikes each dropped:

* **Spread**, against ``liquidity.max_spread_pct``. A volatility solved off a mid nobody can
  trade at is a volatility about nothing.
* **Book depth and session volume**, against ``liquidity.min_open_interest`` and
  ``liquidity.require_session_volume``. The liquidity keys are shared with the gates: the
  question is the same one, and a rule enforced twice is eventually enforced two ways.
* **Delta band**, ``smile.min_abs_delta`` to ``smile.max_abs_delta``. Outside it there is almost
  no vega, so the implied volatility is a rounding artefact of a price quoted in cents.
* **Butterfly convexity**, ``smile.butterfly_max_iv_deviation``, in volatility space against the
  chord of each strike's two neighbours. The real condition is in price space; a daily snapshot
  does not support fitting the surface that needs.

When hygiene leaves nothing the smile is **absent**, and every caller reports the position or the
cell as unmeasurable. There is deliberately no second, laxer smile to fall back to: two standards
behind one number is how a figure nobody can defend reaches a table.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from ..config import ObliskConfig
from ..domain.enums import (
    MarketDataQuality,
    OptionRight,
    market_data_quality,
    worst_market_data_quality,
)
from ..domain.market import ChainRow

#: How an implied volatility was obtained at a strike.
SOURCE_LISTED = "listed"
SOURCE_INTERPOLATED = "interpolated"
SOURCE_CLAMPED_LOW = "clamped_low_wing"
SOURCE_CLAMPED_HIGH = "clamped_high_wing"
SOURCE_SINGLE_POINT = "single_listed_strike"

#: Two points are needed to interpolate between.
MIN_POINTS_FOR_INTERPOLATION = 2

#: Why a strike did not reach the smile. Counted per reason, because "twelve strikes dropped" and
#: "twelve strikes dropped for want of any open interest" lead to different conclusions.
DROP_NO_IV = "iv_absent"
DROP_SPREAD = "spread_too_wide"
#: ``ask < bid``: a broken quote, checked on the raw bid and ask. The spread of a crossed book is
#: negative and would otherwise clear the ceiling as though it were the tightest quote on the chain.
DROP_CROSSED = "spread_crossed"
DROP_OPEN_INTEREST = "open_interest_too_thin"
DROP_NO_VOLUME = "no_session_volume"
DROP_DELTA_BAND = "delta_out_of_band"
DROP_NON_CONVEX = "butterfly_non_convex"

#: A butterfly needs a strike on each side of the one being tested.
_NEIGHBOURS_FOR_BUTTERFLY = 3


@dataclass(frozen=True, slots=True)
class SmileFilters:
    """The hygiene thresholds, bound from configuration once per build.

    Spread, open interest and session volume come from ``liquidity``; the delta band, the
    convexity tolerance and the floor come from ``smile``.
    """

    max_spread_pct: float
    min_open_interest: int
    require_session_volume: bool
    min_abs_delta: float
    max_abs_delta: float
    butterfly_max_iv_deviation: float
    min_points: int

    @classmethod
    def from_config(cls, settings: ObliskConfig) -> SmileFilters:
        liquidity = settings.liquidity
        smile = settings.smile
        return cls(
            max_spread_pct=liquidity.max_spread_pct,
            min_open_interest=liquidity.min_open_interest,
            require_session_volume=liquidity.require_session_volume,
            min_abs_delta=smile.min_abs_delta,
            max_abs_delta=smile.max_abs_delta,
            butterfly_max_iv_deviation=smile.butterfly_max_iv_deviation,
            min_points=smile.min_points,
        )


@dataclass(frozen=True, slots=True)
class SmileHygiene:
    """What the filters kept and what they threw away.

    Reported with every implied volatility read off the smile: a probability computed from three
    surviving strikes out of forty is a different claim from one computed off a full book.
    """

    considered: int = 0
    kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    #: False when no filters ran, so an empty ``dropped`` is not read as a clean bill.
    applied: bool = False

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())

    def summary(self) -> str:
        if not self.applied:
            return (
                f"{self.kept} strike(s), UNFILTERED: no hygiene thresholds were bound, so "
                "spread, depth, delta band and convexity were not tested"
            )
        if not self.dropped:
            return f"{self.kept} of {self.considered} strike(s) kept; none dropped"
        detail = ", ".join(f"{reason} {count}" for reason, count in sorted(self.dropped.items()))
        return f"{self.kept} of {self.considered} strike(s) kept; dropped {detail}"


@dataclass(frozen=True, slots=True)
class SmilePoint:
    """One listed strike's implied volatility."""

    strike: float
    iv: float
    right: OptionRight
    open_interest: int | None = None
    #: The contract's own delta as the route served it, negative on a put; ``None`` where the
    #: route served no greeks, which is a different fact from a delta of zero.
    delta: float | None = None

    @property
    def abs_delta(self) -> float | None:
        """How far out of the money this strike is, without its direction."""
        return None if self.delta is None else abs(self.delta)


@dataclass(frozen=True, slots=True)
class SmileQuote:
    """An implied volatility read off the smile, and how it was obtained."""

    iv: float
    source: str
    strike: float
    #: The listed strikes the value came from: one for a listed or clamped read, two for an
    #: interpolation.
    from_strikes: tuple[float, ...]

    @property
    def is_measured(self) -> bool:
        """True only for a value read directly off a listed strike."""
        return self.source == SOURCE_LISTED

    @property
    def is_clamped(self) -> bool:
        """True when the target sat outside the collected strike range."""
        return self.source in (SOURCE_CLAMPED_LOW, SOURCE_CLAMPED_HIGH)

    def provenance(self) -> str:
        strikes = ", ".join(f"{s:g}" for s in self.from_strikes)
        return f"smile IV {self.iv:.4f} at strike {self.strike:g} ({self.source} from {strikes})"


@dataclass(frozen=True, slots=True)
class SkewReach:
    """What |delta| each side of a smile spans, for the reading that refuses.

    A skew reading refusing because "the band does not reach the target" is an assertion the
    reader cannot check; this is the same statement in numbers: the deltas that survived hygiene
    on each side, and how many strikes carried no delta at all (a route that served no greeks, not
    a thin book).
    """

    put_deltas: tuple[float, float] | None
    call_deltas: tuple[float, float] | None
    points_without_delta: int = 0

    def describe(self) -> str:
        def band(label: str, span: tuple[float, float] | None) -> str:
            if span is None:
                return f"no {label} strike carried a delta"
            return f"{label}s span |delta| {span[0]:.2f}-{span[1]:.2f}"

        detail = f"{band('put', self.put_deltas)}, {band('call', self.call_deltas)}"
        if self.points_without_delta:
            detail += f"; {self.points_without_delta} surviving strike(s) carried no delta"
        return detail


@dataclass(frozen=True, slots=True)
class SkewQuote:
    """A risk reversal read off the smile, and the two strikes behind it."""

    #: Call-side implied volatility minus put-side at the same |delta|. Positive is a call-side
    #: skew: the upper tail is the one being bid.
    value: float
    target_delta: float
    put_strike: float
    call_strike: float
    reach: SkewReach
    #: Strikes surviving hygiene on this smile, which the two legs were interpolated from.
    n_points: int

    def provenance(self) -> str:
        return (
            f"{self.target_delta:g}-delta risk reversal: IV at the "
            f"{self.call_strike:g} call minus IV at the {self.put_strike:g} put, "
            f"interpolated in delta off {self.n_points} surviving strike(s) "
            f"({self.reach.describe()})"
        )


@dataclass(frozen=True, slots=True)
class Smile:
    """One underlying, one expiry, one session."""

    underlying_conid: int
    expiry: date
    snapshot_date: date
    spot: float
    points: tuple[SmilePoint, ...]
    #: What the hygiene pass kept and dropped getting here.
    hygiene: SmileHygiene = field(default_factory=SmileHygiene)
    #: The worst quality of the rows behind this smile; ``None`` when there were none.
    market_data_quality: MarketDataQuality | None = None
    #: The distinct decoded ``market_data_status`` values seen, with counts: what a table prints
    #: beside an indicative label. A row whose status was not reported is counted as ``unreported``.
    market_data_statuses: dict[str, int] = field(default_factory=dict)
    #: Collection routes behind the rows, with counts.
    sources: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError("a smile has at least one point; an absent smile is None")

    def __len__(self) -> int:
        return len(self.points)

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[ChainRow],
        *,
        spot: float,
        underlying_conid: int | None = None,
        expiry: date | None = None,
        filters: SmileFilters | None = None,
    ) -> Smile | None:
        """Build the out-of-the-money smile from one session's chain rows.

        ``filters`` applies the hygiene pass. Passing ``None`` skips it and the result's
        :class:`SmileHygiene` says ``applied=False``, so an unfiltered smile is never mistaken for
        one that passed. Every production caller binds it from configuration.

        ``None`` when nothing survives. A smile with no points is not an empty smile, it is an
        absent one, and a caller must report that rather than fall back to a constant or to a
        laxer second pass.
        """
        scoped = list(rows)
        if expiry is not None:
            scoped = [row for row in scoped if row.expiry == expiry]
        if underlying_conid is not None:
            scoped = [row for row in scoped if row.underlying_conid == underlying_conid]

        considered = len(scoped)
        dropped: dict[str, int] = {}

        def drop(reason: str) -> None:
            dropped[reason] = dropped.get(reason, 0) + 1

        usable: list[ChainRow] = []
        for row in scoped:
            if row.iv is None or row.iv <= 0:
                drop(DROP_NO_IV)
                continue
            if filters is not None:
                reason = _hygiene_failure(row, filters)
                if reason is not None:
                    drop(reason)
                    continue
            usable.append(row)

        if not usable:
            return None

        by_strike: dict[float, ChainRow] = {}
        for row in usable:
            preferred = OptionRight.PUT if row.strike < spot else OptionRight.CALL
            held = by_strike.get(row.strike)
            if held is None:
                by_strike[row.strike] = row
                continue
            # Out-of-the-money wins. Between two quotes on the same side the deeper book wins:
            # open interest is what makes an implied volatility a price rather than a mark.
            out_of_the_money_beats_held = held.right != preferred and row.right == preferred
            deeper_book = held.right == row.right and (row.open_interest or 0) > (
                held.open_interest or 0
            )
            if out_of_the_money_beats_held or deeper_book:
                by_strike[row.strike] = row

        # A strike that loses the per-strike contest is not a dropped strike: its twin stands in
        # the smile at the same strike. Only strikes hygiene removed are counted, which is why
        # this runs after selection.
        selected = [row for _, row in sorted(by_strike.items())]

        if filters is not None:
            selected, non_convex = _drop_non_convex(selected, filters)
            if non_convex:
                dropped[DROP_NON_CONVEX] = dropped.get(DROP_NON_CONVEX, 0) + non_convex
            if len(selected) < filters.min_points:
                return None

        points = tuple(
            SmilePoint(
                strike=row.strike,
                iv=_iv(row),
                right=row.right,
                open_interest=row.open_interest,
                delta=row.delta,
            )
            for row in selected
        )
        first = selected[0]
        return cls(
            underlying_conid=underlying_conid
            if underlying_conid is not None
            else first.underlying_conid,
            expiry=expiry if expiry is not None else first.expiry,
            snapshot_date=first.snapshot_date,
            spot=spot,
            points=points,
            hygiene=SmileHygiene(
                considered=considered,
                kept=len(points),
                dropped=dropped,
                applied=filters is not None,
            ),
            market_data_quality=worst_market_data_quality(
                market_data_quality(row.market_data_status) for row in selected
            ),
            market_data_statuses=_counted(
                row.market_data_status.value if row.market_data_status is not None else "unreported"
                for row in selected
            ),
            sources=_counted(row.source for row in selected),
        )

    @property
    def strikes(self) -> list[float]:
        return [p.strike for p in self.points]

    def iv_at(self, strike: float) -> SmileQuote:
        """The implied volatility at ``strike``, with its provenance.

        This is what feeds d2. Pass the strike the probability is being computed AT: the target
        level, not the position's own strike and never the at-the-money strike.
        """
        if len(self.points) < MIN_POINTS_FOR_INTERPOLATION:
            only = self.points[0]
            return SmileQuote(
                iv=only.iv,
                source=SOURCE_SINGLE_POINT,
                strike=strike,
                from_strikes=(only.strike,),
            )

        strikes = self.strikes
        if strike <= strikes[0]:
            point = self.points[0]
            source = SOURCE_LISTED if strike == strikes[0] else SOURCE_CLAMPED_LOW
            return SmileQuote(
                iv=point.iv, source=source, strike=strike, from_strikes=(point.strike,)
            )
        if strike >= strikes[-1]:
            point = self.points[-1]
            source = SOURCE_LISTED if strike == strikes[-1] else SOURCE_CLAMPED_HIGH
            return SmileQuote(
                iv=point.iv, source=source, strike=strike, from_strikes=(point.strike,)
            )

        index = bisect_left(strikes, strike)
        if strikes[index] == strike:
            point = self.points[index]
            return SmileQuote(
                iv=point.iv, source=SOURCE_LISTED, strike=strike, from_strikes=(point.strike,)
            )
        low, high = self.points[index - 1], self.points[index]
        span = high.strike - low.strike
        weight = 0.0 if span <= 0 else (strike - low.strike) / span
        return SmileQuote(
            iv=low.iv + weight * (high.iv - low.iv),
            source=SOURCE_INTERPOLATED,
            strike=strike,
            from_strikes=(low.strike, high.strike),
        )

    def atm(self) -> SmileQuote:
        """Implied volatility at the money."""
        return self.iv_at(self.spot)

    def skew(self, *, low_strike: float, high_strike: float) -> float:
        """High-strike IV minus low-strike IV. Positive is a call-side skew."""
        return self.iv_at(high_strike).iv - self.iv_at(low_strike).iv

    def risk_reversal(self, target_delta: float) -> SkewQuote | None:
        """The skew at ``target_delta``, or ``None`` when the band cannot reach it.

        A risk reversal reads the direction of a smile: the implied volatility of the
        out-of-the-money call minus that of the out-of-the-money put, both at the same |delta| so
        the two strikes are equally far out by the market's own measure rather than by a moneyness
        somebody chose. At-the-money volatility is symmetric and says nothing about which tail is
        bid; this says which.

        **Delta, never a hardcoded moneyness.** ``chain_snapshots`` carries the route's own delta
        per contract, so the strike is located by the number the exchange priced, which means the
        same thing on a 15-vol name and a 90-vol one.

        **A band that cannot reach the target refuses.** Each side must bracket ``target_delta``
        and the strike is interpolated between the two points either side of it. Quietly taking
        the nearest listed strike would publish a 45-delta reading under a 25-delta name.
        """
        reach = self.delta_reach()
        low = self._strike_at_delta(target_delta, calls=False)
        high = self._strike_at_delta(target_delta, calls=True)
        if low is None or high is None:
            return None
        return SkewQuote(
            value=self.skew(low_strike=low, high_strike=high),
            target_delta=target_delta,
            put_strike=low,
            call_strike=high,
            reach=reach,
            n_points=len(self.points),
        )

    def iv_at_delta(self, target_delta: float, *, calls: bool) -> SmileQuote | None:
        """The implied volatility at ``target_delta`` |delta| on one wing (calls at and above the
        spot, puts below it), the strike interpolated in delta; ``None`` when that wing does not
        bracket the target (the nearest listed strike is never substituted)."""
        strike = self._strike_at_delta(target_delta, calls=calls)
        return None if strike is None else self.iv_at(strike)

    def delta_reach(self) -> SkewReach:
        """The |delta| range each side of this smile spans, read off the surviving points.

        Reported whether or not a read succeeds: on a refusal it is the reason, and on a success
        it is the evidence that the target was inside the collected band and not at its edge.
        """
        deltas = [(p.strike, abs(p.delta)) for p in self.points if p.delta is not None]
        puts = [delta for strike, delta in deltas if strike < self.spot]
        calls = [delta for strike, delta in deltas if strike >= self.spot]
        return SkewReach(
            put_deltas=(min(puts), max(puts)) if puts else None,
            call_deltas=(min(calls), max(calls)) if calls else None,
            points_without_delta=sum(1 for p in self.points if p.abs_delta is None),
        )

    def _strike_at_delta(self, target: float, *, calls: bool) -> float | None:
        """The strike at ``target`` |delta| on one side, interpolated, or ``None``.

        Linear in |delta| between the two bracketing strikes, for the reason :meth:`iv_at`
        interpolates linearly in strike.
        """
        side = sorted(
            (
                (abs(point.delta), point)
                for point in self.points
                if point.delta is not None
                and ((point.strike >= self.spot) if calls else (point.strike < self.spot))
            ),
            key=lambda pair: pair[0],
        )
        if len(side) < MIN_POINTS_FOR_INTERPOLATION:
            return None
        deltas = [delta for delta, _ in side]
        if target < deltas[0] or target > deltas[-1]:
            return None
        index = bisect_left(deltas, target)
        if deltas[index] == target:
            return side[index][1].strike
        (low_delta, low), (high_delta, high) = side[index - 1], side[index]
        weight = (target - low_delta) / (high_delta - low_delta)
        return low.strike + weight * (high.strike - low.strike)


def _iv(row: ChainRow) -> float:
    """The implied volatility of a row that has already passed the presence check."""
    assert row.iv is not None
    return row.iv


def _hygiene_failure(row: ChainRow, filters: SmileFilters) -> str | None:
    """The first test this strike fails, or ``None`` when it clears them all.

    Absence is not a failure, with one exception. A missing open interest or delta is the route
    not having served the field, which is different from an empty book, and rejecting on it would
    empty the smile on any route that carries no greeks. Session volume is the exception because
    ``liquidity.require_session_volume`` asks for the field to be present and positive: that gate
    exists to reject a strike that did not trade, and a missing value there is indistinguishable
    from zero.
    """
    if row.bid is not None and row.ask is not None and row.ask < row.bid:
        return DROP_CROSSED
    spread = row.spread_pct_of_mid
    if spread is not None and spread > filters.max_spread_pct:
        return DROP_SPREAD
    if row.open_interest is not None and row.open_interest < filters.min_open_interest:
        return DROP_OPEN_INTEREST
    if filters.require_session_volume and not (row.volume or 0) > 0:
        return DROP_NO_VOLUME
    if row.delta is not None:
        magnitude = abs(row.delta)
        if magnitude < filters.min_abs_delta or magnitude > filters.max_abs_delta:
            return DROP_DELTA_BAND
    return None


def _drop_non_convex(selected: list[ChainRow], filters: SmileFilters) -> tuple[list[ChainRow], int]:
    """Remove strikes whose implied volatility sits off the chord of their neighbours.

    The real condition is a price-space butterfly, ``C(K-h) - 2C(K) + C(K+h) >= 0``. Testing it
    properly needs an arbitrage-free surface, which a twenty-strike daily snapshot does not
    support. So the test runs in volatility space against ``smile.butterfly_max_iv_deviation``,
    which catches what it is here to catch: a single bad print well above or below the two
    strikes either side of it.

    The worst offender goes first and the check re-runs, because one bad print distorts the chord
    for both its neighbours and dropping the whole triple would take two good strikes with it.
    Stops at ``smile.min_points``: past that there is no smile left to clean.
    """
    working = list(selected)
    removed = 0
    while len(working) >= _NEIGHBOURS_FOR_BUTTERFLY:
        worst_index = None
        worst_deviation = filters.butterfly_max_iv_deviation
        for index in range(1, len(working) - 1):
            low, middle, high = working[index - 1], working[index], working[index + 1]
            weight = (middle.strike - low.strike) / (high.strike - low.strike)
            low_iv, middle_iv, high_iv = _iv(low), _iv(middle), _iv(high)
            deviation = abs(middle_iv - (low_iv + weight * (high_iv - low_iv)))
            if deviation > worst_deviation:
                worst_index, worst_deviation = index, deviation
        if worst_index is None:
            break
        if len(working) - 1 < filters.min_points:
            break
        working.pop(worst_index)
        removed += 1
    return working, removed


def _counted(values: Iterable[str]) -> dict[str, int]:
    """Distinct values with their counts, for a provenance line."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts
