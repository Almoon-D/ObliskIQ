"""The volatility smile, and reading an implied vol off it at a given strike.

This module exists because of one line in the discipline rules: N(d2) is
computed from **the strike's own implied vol from the smile, not at-the-money
vol** (``zombie_test.prob_vol_source``). Constant-vol Black-Scholes understates
the terminal probability on out-of-the-money strikes, so an ATM sigma produces
probabilities that are too low exactly where the zombie test looks — and a false
zombie positive forces a premature exit on a position that was still alive.

Construction
------------

At each listed strike the smile takes the **out-of-the-money** contract: puts
below spot, calls at and above it. Under put-call parity both sides share one
implied volatility, but the out-of-the-money quote is nearly all time value,
which is the part that responds to volatility at all. The in-the-money quote is
mostly intrinsic and its implied vol is the noisiest number on the chain — the
same reasoning that makes :func:`obliskiq.quant.bsm.to_out_of_the_money`
convert before solving.

Interpolation is **linear in strike** between the two listed strikes that
bracket the target. A twenty-strike daily snapshot does not support fitting an
arbitrage-free surface, and pretending otherwise would dress an interpolation up
as a model. Beyond the outermost listed strike the value is **clamped to the
wing and labelled as clamped**, never extrapolated: the wing slope of a
truncated chain is an artefact of where collection stopped.

Every read returns its provenance, so a probability computed from a clamped wing
can be reported as such rather than as a measurement.

Hygiene
-------

A quote existing is not the same as a quote carrying information. Before any
strike reaches the smile it must clear four tests, and :class:`SmileHygiene`
records how many strikes each one dropped:

* **Spread**, against ``liquidity.max_spread_pct``. A vol solved off a mid
  nobody can trade at is a vol about nothing.
* **Book depth and session volume**, against ``liquidity.min_open_interest`` and
  ``liquidity.require_session_volume``. The liquidity keys are reused rather
  than copied: the question is the same one, and a rule enforced twice is
  eventually enforced two different ways.
* **Delta band**, ``smile.min_abs_delta`` to ``smile.max_abs_delta``. Outside
  it there is almost no vega, so the implied vol is a rounding artefact of a
  price quoted in cents.
* **Butterfly convexity**, ``smile.butterfly_max_iv_deviation``, in IV space
  against the chord of each strike's two neighbours. A price-space butterfly
  test is the real condition; a twenty-strike daily snapshot does not support
  fitting the surface that would need, which is the same reason interpolation
  here is linear rather than modelled.

This is not tidiness either. ``zombie_test.prob_vol_source`` is
``strike_smile_iv``, so one bad implied vol on an illiquid strike propagates
into N(d2) and out into an exit decision on a position that was still alive.

When hygiene leaves nothing, the smile is **absent**, and every caller reports
the position or the cell as unmeasurable. There is deliberately no second,
laxer smile to fall back to: two standards behind one number is how a figure
nobody can defend reaches a table.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from ..config import Config
from .frames import (
    RIGHT_CALL,
    RIGHT_PUT,
    ChainRow,
    classify_market_data_status,
    worst_market_data_quality,
)

#: How an implied vol was obtained at a strike.
SOURCE_LISTED = "listed"
SOURCE_INTERPOLATED = "interpolated"
SOURCE_CLAMPED_LOW = "clamped_low_wing"
SOURCE_CLAMPED_HIGH = "clamped_high_wing"
SOURCE_SINGLE_POINT = "single_listed_strike"

#: Two points are needed to interpolate between.
MIN_POINTS_FOR_INTERPOLATION = 2

#: Why a strike did not reach the smile. Counted per reason, because "twelve
#: strikes dropped" and "twelve strikes dropped for want of any open interest"
#: lead to different conclusions about the underlying.
DROP_NO_IV = "iv_absent"
DROP_SPREAD = "spread_too_wide"
#: ask < bid. Not a wide spread and not an absence — a broken quote, checked
#: on the raw bid/ask rather than on spread_pct_of_mid, which is negative
#: here and would otherwise clear the max_spread_pct ceiling as though it
#: were the tightest quote on the chain.
DROP_CROSSED = "spread_crossed"
DROP_OPEN_INTEREST = "open_interest_too_thin"
DROP_NO_VOLUME = "no_session_volume"
DROP_DELTA_BAND = "delta_out_of_band"
DROP_NON_CONVEX = "butterfly_non_convex"

#: A butterfly needs a strike on each side of the one being tested.
_NEIGHBOURS_FOR_BUTTERFLY = 3


@dataclass(frozen=True)
class SmileFilters:
    """The hygiene thresholds, bound from config once per build.

    Spread, open interest and session volume come from ``liquidity``; the delta
    band, the convexity tolerance and the floor come from ``smile``. Split that
    way because the first three are the same question the candidate gates ask
    and must not answer it differently.
    """

    max_spread_pct: float
    min_open_interest: int
    require_session_volume: bool
    min_abs_delta: float
    max_abs_delta: float
    butterfly_max_iv_deviation: float
    min_points: int
    #: ``smile.skew_delta``. The |delta| each leg of the risk reversal is read
    #: at — one target, used on both sides, because a risk reversal compares two
    #: strikes the market prices as equally far out. Conventionally 25 delta.
    skew_delta: float = 0.0
    #: ``smile.term_structure.*``. The two tenors the implied term slope is read
    #: between, how far a listed expiry may sit from either target before it
    #: stops being that leg, and how close the two may be before they are not a
    #: term structure at all. Zero throughout means no term structure was bound,
    #: which the leaf reports rather than assuming a pair.
    term_front_target_dte: int = 0
    term_back_target_dte: int = 0
    term_max_target_deviation_days: int = 0
    term_min_separation_days: int = 0
    #: ``data.market_data_status_live``. Carried here so a smile is classified
    #: against the same list the rest of the build uses, without every caller
    #: threading a second config read.
    live_market_data_statuses: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: Config) -> SmileFilters:
        liquidity = config.section("liquidity")
        smile = config.section("smile")
        return cls(
            max_spread_pct=liquidity.require_float("max_spread_pct"),
            min_open_interest=liquidity.require_int("min_open_interest"),
            require_session_volume=liquidity.require_bool("require_session_volume"),
            min_abs_delta=smile.require_float("min_abs_delta"),
            max_abs_delta=smile.require_float("max_abs_delta"),
            butterfly_max_iv_deviation=smile.require_float("butterfly_max_iv_deviation"),
            min_points=smile.require_int("min_points"),
            skew_delta=smile.require_float("skew_delta"),
            term_front_target_dte=smile.require_int("term_structure.front_target_dte"),
            term_back_target_dte=smile.require_int("term_structure.back_target_dte"),
            term_max_target_deviation_days=smile.require_int(
                "term_structure.max_target_deviation_days"
            ),
            term_min_separation_days=smile.require_int("term_structure.min_separation_days"),
            live_market_data_statuses=tuple(
                str(value) for value in config.require_list("data.market_data_status_live")
            ),
        )


@dataclass(frozen=True)
class SmileHygiene:
    """What the filters kept and what they threw away.

    Reported alongside every implied vol read off this smile. A probability
    computed from three surviving strikes out of forty is a different claim
    from one computed off a full book, and only this says which it was.
    """

    considered: int = 0
    kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    #: False when no filters ran at all, which is how a caller that never bound
    #: config is told so rather than reading an empty ``dropped`` as a clean bill.
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


@dataclass(frozen=True)
class SmilePoint:
    """One listed strike's implied volatility."""

    strike: float
    iv: float
    right: str
    open_interest: int | None = None
    #: The contract's own delta, verbatim from ``chain_snapshots``. Signed as the
    #: route served it — negative on a put — and read through :attr:`abs_delta`
    #: wherever the question is how far out the strike is rather than which way
    #: it points. ``None`` where the route served no greeks, which is a different
    #: fact from a delta of zero and is why nothing here defaults it.
    delta: float | None = None

    @property
    def abs_delta(self) -> float | None:
        """How far out of the money this strike is, without its direction."""
        return None if self.delta is None else abs(self.delta)


@dataclass(frozen=True)
class SmileQuote:
    """An implied vol read off the smile, and how it was obtained."""

    iv: float
    source: str
    strike: float
    #: The listed strikes the value came from. One for a listed or clamped read,
    #: two for an interpolation.
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


@dataclass(frozen=True)
class SkewReach:
    """What |delta| each side of a smile spans, for the leaf that refuses.

    A skew leaf reading ``n/a`` because "the band does not reach the target" is
    an assertion the reader cannot check. This is the same statement in numbers:
    the deltas that survived hygiene on each side, and how many strikes carried no
    delta at all — which is a route that served no greeks rather than a thin book,
    and is a different thing to go and fix.
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


@dataclass(frozen=True)
class SkewQuote:
    """A risk reversal read off the smile, and the two strikes behind it."""

    #: Call-side implied vol minus put-side, at the same |delta|. Positive is a
    #: call-side skew: the upper tail is the one being bid.
    value: float
    target_delta: float
    put_strike: float
    call_strike: float
    reach: SkewReach
    #: Strikes surviving hygiene on this smile. The two legs were interpolated
    #: from them, so it is what the reading rests on.
    n_points: int

    def provenance(self) -> str:
        return (
            f"{self.target_delta:g}-delta risk reversal: IV at the "
            f"{self.call_strike:g} call minus IV at the {self.put_strike:g} put, "
            f"interpolated in delta off {self.n_points} surviving strike(s) "
            f"({self.reach.describe()})"
        )


@dataclass(frozen=True)
class Smile:
    """One underlying, one expiry, one session."""

    underlying_id: int
    expiry: date
    snapshot_date: date
    spot: float
    points: tuple[SmilePoint, ...]
    #: What the hygiene pass kept and dropped getting here.
    hygiene: SmileHygiene = field(default_factory=SmileHygiene)
    #: The market-data state of the rows behind this smile, worst first, as
    #: :func:`obliskiq.compute.frames.worst_market_data_quality` labels it.
    #: ``None`` when the rows carried no status at all.
    market_data_quality: str | None = None
    #: The distinct ``market_data_status`` values seen, with counts. What a
    #: table prints beside an indicative label.
    market_data_statuses: dict[str, int] = field(default_factory=dict)
    #: Collection routes behind the rows, with counts.
    sources: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.points)

    @classmethod
    def from_rows(
        cls,
        rows: list[ChainRow],
        *,
        spot: float,
        underlying_id: int | None = None,
        expiry: date | None = None,
        filters: SmileFilters | None = None,
    ) -> Smile | None:
        """Build the out-of-the-money smile from one session's chain rows.

        ``filters`` applies the hygiene pass described in the module docstring.
        Passing ``None`` skips it and the resulting :class:`SmileHygiene` says
        ``applied=False``, so an unfiltered smile is never mistaken for one that
        passed. Every production caller binds it from config.

        ``None`` when nothing survives. A smile with no points is not an empty
        smile, it is an absent one, and a caller must report that rather than
        fall back to a constant — or, now, to a laxer second pass.
        """
        scoped = list(rows)
        if expiry is not None:
            scoped = [row for row in scoped if row.expiry == expiry]
        if underlying_id is not None:
            scoped = [row for row in scoped if row.underlying_id == underlying_id]

        considered = len(scoped)
        dropped: dict[str, int] = {}

        def drop(reason: str) -> None:
            dropped[reason] = dropped.get(reason, 0) + 1

        usable: list[ChainRow] = []
        for row in scoped:
            if row.iv is None or row.iv <= 0 or row.strike <= 0:
                drop(DROP_NO_IV)
                continue
            if row.right not in (RIGHT_CALL, RIGHT_PUT):
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
            preferred_right = RIGHT_PUT if row.strike < spot else RIGHT_CALL
            held = by_strike.get(row.strike)
            if held is None:
                by_strike[row.strike] = row
                continue
            # Out-of-the-money wins. Between two quotes on the same side, the
            # deeper book wins: open interest is what makes an implied vol a
            # price rather than a mark.
            out_of_the_money_beats_held = (
                held.right != preferred_right and row.right == preferred_right
            )
            deeper_book = held.right == row.right and (row.open_interest or 0) > (
                held.open_interest or 0
            )
            if out_of_the_money_beats_held or deeper_book:
                by_strike[row.strike] = row

        # A strike that loses the per-strike contest is not a dropped strike —
        # its twin is standing in the smile at the same strike. Only the ones
        # hygiene removed are counted, which is why this runs after selection.
        selected = [row for _, row in sorted(by_strike.items())]

        if filters is not None:
            selected, non_convex = _drop_non_convex(selected, filters)
            if non_convex:
                dropped[DROP_NON_CONVEX] = dropped.get(DROP_NON_CONVEX, 0) + non_convex
            if len(selected) < filters.min_points:
                return None

        if not selected:
            return None

        points = tuple(
            SmilePoint(
                strike=row.strike,
                iv=float(row.iv),  # type: ignore[arg-type]
                right=row.right,
                open_interest=row.open_interest,
                delta=row.delta,
            )
            for row in selected
        )
        first = selected[0]
        quality = (
            None
            if filters is None
            else worst_market_data_quality(
                classify_market_data_status(
                    row.market_data_status, filters.live_market_data_statuses
                )
                for row in selected
            )
        )
        return cls(
            underlying_id=underlying_id if underlying_id is not None else first.underlying_id,
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
            market_data_quality=quality,
            market_data_statuses=_counted(row.market_data_status for row in selected),
            sources=_counted(row.source for row in selected),
        )

    @property
    def strikes(self) -> list[float]:
        return [p.strike for p in self.points]

    def iv_at(self, strike: float) -> SmileQuote | None:
        """The implied volatility at ``strike``, with its provenance.

        This is what feeds d2. Pass the strike the probability is being computed
        AT — for the zombie test that is the target level, not the position's own
        strike and never the at-the-money strike.
        """
        if not self.points:
            return None
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

    def atm(self) -> SmileQuote | None:
        """Implied volatility at the money, for the grid's IV indicator."""
        return self.iv_at(self.spot)

    def skew(self, *, low_strike: float, high_strike: float) -> float | None:
        """High-strike IV minus low-strike IV. Positive is a call-side skew."""
        low = self.iv_at(low_strike)
        high = self.iv_at(high_strike)
        if low is None or high is None:
            return None
        return high.iv - low.iv

    def risk_reversal(self, target_delta: float) -> SkewQuote | None:
        """The skew at ``target_delta``, or ``None`` when the band cannot reach it.

        A risk reversal is the conventional way to read the DIRECTION of a smile:
        the implied vol of the out-of-the-money call minus that of the
        out-of-the-money put, both taken at the same |delta| so the two strikes
        are equally far out by the market's own measure rather than by a
        moneyness somebody chose. ATM implied vol is symmetric and says nothing
        about which tail is bid; this says which.

        **Delta, never a hardcoded moneyness.** ``chain_snapshots`` carries the
        route's own delta per contract, so the strike is located by the number the
        exchange priced rather than by a percentage of spot — which would mean
        something different on a 15-vol name and a 90-vol one, and would move
        between them for no reason a reader could audit.

        **A band that cannot reach the target refuses.** Each side must BRACKET
        ``target_delta``, and the strike is then interpolated between the two
        points either side of it — the same treatment :meth:`iv_at` gives a target
        strike, and the same refusal it makes rather than clamping to a wing. The
        alternative, quietly taking the nearest listed strike, would publish a
        45-delta reading under a 25-delta name. :class:`SkewReach` carries what
        each side could actually reach so the refusal says so in numbers.
        """
        reach = self.delta_reach()
        low = self._strike_at_delta(target_delta, calls=False)
        high = self._strike_at_delta(target_delta, calls=True)
        if low is None or high is None:
            return None
        value = self.skew(low_strike=low, high_strike=high)
        if value is None:
            return None
        return SkewQuote(
            value=value,
            target_delta=target_delta,
            put_strike=low,
            call_strike=high,
            reach=reach,
            n_points=len(self.points),
        )

    def delta_reach(self) -> SkewReach:
        """The |delta| range each side of this smile actually spans.

        Read off the points that SURVIVED hygiene, because that is the band a
        skew could be computed from. Reported whether or not the read succeeds: on
        a refusal it is the reason, and on a success it is the evidence that the
        target was inside the collected band rather than at the edge of it.
        """
        deltas = [(p.strike, p.abs_delta) for p in self.points if p.abs_delta is not None]
        puts = [delta for strike, delta in deltas if strike < self.spot]
        calls = [delta for strike, delta in deltas if strike >= self.spot]
        return SkewReach(
            put_deltas=(min(puts), max(puts)) if puts else None,
            call_deltas=(min(calls), max(calls)) if calls else None,
            points_without_delta=sum(1 for p in self.points if p.abs_delta is None),
        )

    def _strike_at_delta(self, target: float, *, calls: bool) -> float | None:
        """The strike at ``target`` |delta| on one side, interpolated, or ``None``.

        Linear in |delta| between the two bracketing strikes, for the reason
        :meth:`iv_at` interpolates linearly in strike: a daily snapshot of a few
        dozen strikes does not support fitting a surface, and pretending otherwise
        would dress an interpolation up as a model.
        """
        side = [
            point
            for point in self.points
            if point.abs_delta is not None
            and ((point.strike >= self.spot) if calls else (point.strike < self.spot))
        ]
        if len(side) < MIN_POINTS_FOR_INTERPOLATION:
            return None
        # Ascending in |delta|, which on either side is monotone in strike — the
        # calls falling away from spot, the puts rising toward it — so sorting on
        # the delta rather than the strike keeps one code path for both.
        side.sort(key=lambda point: point.abs_delta)  # type: ignore[arg-type,return-value]
        deltas = [point.abs_delta for point in side]
        if target < deltas[0] or target > deltas[-1]:  # type: ignore[operator]
            return None
        index = bisect_left(deltas, target)  # type: ignore[arg-type]
        if deltas[index] == target:
            return side[index].strike
        low, high = side[index - 1], side[index]
        span = high.abs_delta - low.abs_delta  # type: ignore[operator]
        if span <= 0:
            return low.strike
        weight = (target - low.abs_delta) / span  # type: ignore[operator]
        return low.strike + weight * (high.strike - low.strike)


def _hygiene_failure(row: ChainRow, filters: SmileFilters) -> str | None:
    """The first test this strike fails, or ``None`` when it clears them all.

    Absence is not a failure, with one exception. A missing open interest or
    delta is the route not having served the field, which is a different thing
    from a book being empty, and rejecting on it would empty the smile on any
    route that does not carry greeks. Session volume is the exception because
    ``liquidity.require_session_volume`` asks for the field to be *present and
    positive* — that gate exists precisely to reject a strike that did not
    trade, and a NULL there is indistinguishable from a zero.
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
    """Remove strikes whose implied vol sits off the chord of their neighbours.

    The real condition is a price-space butterfly, ``C(K-h) - 2C(K) + C(K+h) >=
    0``. Testing it properly needs an arbitrage-free surface, and a twenty-strike
    daily snapshot does not support fitting one — the same reason this module
    interpolates linearly rather than modelling. So the test runs in IV space
    against ``smile.butterfly_max_iv_deviation``, which catches what it is
    actually here to catch: a single bad print sitting well above or below the
    two strikes either side of it.

    The worst offender goes first and the check re-runs, because one bad print
    distorts the chord for both its neighbours and dropping the whole triple
    would take two good strikes with it. Stops at ``smile.min_points``: past
    that there is no smile left to clean.
    """
    working = list(selected)
    removed = 0
    while len(working) >= _NEIGHBOURS_FOR_BUTTERFLY:
        worst_index = None
        worst_deviation = filters.butterfly_max_iv_deviation
        for index in range(1, len(working) - 1):
            low, middle, high = working[index - 1], working[index], working[index + 1]
            span = high.strike - low.strike
            if span <= 0:
                continue
            weight = (middle.strike - low.strike) / span
            chord = float(low.iv) + weight * (float(high.iv) - float(low.iv))  # type: ignore[arg-type]
            deviation = abs(float(middle.iv) - chord)  # type: ignore[arg-type]
            if deviation > worst_deviation:
                worst_index, worst_deviation = index, deviation
        if worst_index is None:
            break
        if len(working) - 1 < filters.min_points:
            break
        working.pop(worst_index)
        removed += 1
    return working, removed


def _counted(values: Iterable[str | None]) -> dict[str, int]:
    """Distinct non-null values with their counts, for a provenance line."""
    counts: dict[str, int] = {}
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            counts[text] = counts.get(text, 0) + 1
    return counts
