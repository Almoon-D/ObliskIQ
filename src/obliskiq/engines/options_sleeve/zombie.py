"""The option side of lite's risk engine: a position's greeks and its odds of reaching its target,
the zombie test over consecutive review sessions, and the theta budget at entry.

**A drawdown is never an exit.** No path here takes a percentage loss as a condition. The forward
measure is N(d2) at the target, from **the strike smile**: the volatility N(d2) reads is the
smile's at the target, the greeks' is the smile's at the position's own strike (two points of one
smile, which differ wherever there is skew). Constant-volatility Black-Scholes understates the
terminal probability on an out-of-the-money strike, which is exactly what produces a false zombie.

**Four conditions, all on ``zombie_test.consecutive_sessions_required`` consecutive reviews**:
|delta| below ``max_delta``, fewer than ``max_dte`` days left, N(d2) below ``max_prob_target`` and
below ``max_prob_ratio_vs_entry`` of the entry's, and a bid still worth at least
``min_recoverable_pct_of_premium`` of the premium (no bid is nothing to rescue: let it run to the
mandatory close). Reviews are counted, not calendar days: a skipped weekend does not reset the
streak, a clean review does.

**The penalty is booked at confirmation** when ``zombie_test.book_penalty_at`` is ``detection``,
once per position: a third confirming session never books it again.

**The dividend yield is required.** q enters d1 and discounts the spot; pricing a payer at zero
overstates N(d2) and delta, which the entry ratio puts straight on the exit gate. A caller with no
collected yield passes zero and says so (``dividend_basis``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ...config.schema import ObliskConfig
from ...domain.options import OptionPosition, ZombieObservation
from ...errors import ObliskError
from ...quant.bsm import BsmInputs, bsm_greeks, bsm_price, probability_itm
from ...quant.smile import Smile, SmileQuote
from ...store import options as options_repo
from ...store.db import Store
from ...util.timeutil import utc_now
from .penalty import ZERO, PenaltyLedger

#: Where a target level came from.
TARGET_STATED = "stated"
TARGET_DERIVED = "derived_payoff_ratio"


class ZombieError(ObliskError):
    """A review was refused (a session that has not happened, or one before the position)."""


@dataclass(frozen=True, slots=True)
class TargetLevel:
    """The underlying price the probability is measured at."""

    level: float
    source: str
    payoff_ratio: float


@dataclass(frozen=True, slots=True)
class OptionRisk:
    """What the zombie test and a review read for one position on one session."""

    as_of: date
    spot: float
    strike: float
    is_call: bool
    dte: int
    premium_per_unit: float
    bid: float | None
    delta: float
    gamma: float
    vega: float
    theta: float
    target: TargetLevel
    prob_target: float
    prob_target_at_entry: float | None
    target_vol: SmileQuote
    strike_vol: SmileQuote
    #: Which volatility N(d2) read (``zombie_test.prob_vol_source``) and where q came from.
    vol_source: str
    dividend_basis: str

    @property
    def prob_ratio_vs_entry(self) -> float | None:
        if not self.prob_target_at_entry:
            return None
        return self.prob_target / self.prob_target_at_entry

    @property
    def recoverable_pct_of_premium(self) -> float | None:
        if self.bid is None or self.premium_per_unit <= 0:
            return None
        return self.bid / self.premium_per_unit


@dataclass(frozen=True, slots=True)
class ZombieCondition:
    name: str
    met: bool
    observed: float | None
    threshold: float
    detail: str


@dataclass(frozen=True, slots=True)
class ZombieAssessment:
    """One session's reading, and whether confirmation has landed."""

    position_id: int | None
    observed_on: date
    conditions: tuple[ZombieCondition, ...]
    consecutive_sessions: int
    required_sessions: int
    penalty_booked: Decimal | None = None

    @property
    def all_conditions_met(self) -> bool:
        return all(condition.met for condition in self.conditions)

    @property
    def fires(self) -> bool:
        """Only once every condition has held on enough consecutive reviews."""
        return self.all_conditions_met and self.consecutive_sessions >= self.required_sessions

    def unmet(self) -> tuple[str, ...]:
        return tuple(condition.name for condition in self.conditions if not condition.met)

    def summary(self) -> str:
        if self.fires:
            return (
                f"zombie confirmed on {self.consecutive_sessions} consecutive sessions "
                f"(required {self.required_sessions})"
            )
        if self.all_conditions_met:
            return (
                f"all conditions met, on {self.consecutive_sessions} of "
                f"{self.required_sessions} consecutive sessions: one session never triggers an exit"
            )
        return "not a zombie: " + "; ".join(c.detail for c in self.conditions if not c.met)


@dataclass(frozen=True, slots=True)
class ThetaBudget:
    """What the position is worth at the thesis's expected resolution, nothing else moving."""

    premium_per_unit: float
    value_at_resolution: float
    remaining_share: float
    floor: float
    resolution_dte: int

    @property
    def passes(self) -> bool:
        return self.remaining_share >= self.floor


class ZombieTest:
    """The option side of the risk engine, bound to configuration and the store once."""

    def __init__(
        self,
        store: Store,
        settings: ObliskConfig,
        *,
        penalty: PenaltyLedger | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._bsm = BsmInputs.from_config(settings)
        self._penalty = penalty or PenaltyLedger(store, settings, now=now)
        self._payoff_ratio = settings.option_sleeve.min_payoff_ratio
        zombie = settings.zombie_test
        self._max_delta = zombie.max_delta
        self._max_dte = zombie.max_dte
        self._max_prob_target = zombie.max_prob_target
        self._max_prob_ratio = zombie.max_prob_ratio_vs_entry
        self._min_recoverable = zombie.min_recoverable_pct_of_premium
        self._required = zombie.consecutive_sessions_required
        self._vol_source = zombie.prob_vol_source
        self._book_at_detection = zombie.book_penalty_at == "detection"
        self._now = now

    # ── the reading ──

    def target_level(
        self, *, strike: float, premium_per_unit: float, is_call: bool, stated: float | None
    ) -> TargetLevel:
        """A stated target wins; otherwise the level where intrinsic value reaches
        ``option_sleeve.min_payoff_ratio`` times the premium, labelled as derived."""
        if stated is not None and stated > 0:
            return TargetLevel(stated, TARGET_STATED, self._payoff_ratio)
        move = self._payoff_ratio * premium_per_unit
        level = strike + move if is_call else max(strike - move, self._bsm.min_price)
        return TargetLevel(level, TARGET_DERIVED, self._payoff_ratio)

    def assess(
        self,
        *,
        as_of: date,
        spot: float,
        strike: float,
        is_call: bool,
        expiry: date,
        premium_per_unit: float,
        bid: float | None,
        smile: Smile,
        rate: float,
        dividend_yield: float,
        dividend_basis: str,
        prob_target_at_entry: float | None = None,
        stated_target: float | None = None,
    ) -> OptionRisk:
        """Greeks on the strike's own smile volatility, N(d2) on the target's
        (``zombie_test.prob_vol_source``)."""
        dte = (expiry - as_of).days
        target = self.target_level(
            strike=strike, premium_per_unit=premium_per_unit, is_call=is_call, stated=stated_target
        )
        at_target = smile.iv_at(target.level)
        at_strike = smile.iv_at(strike)
        greeks = bsm_greeks(
            spot, strike, dte, at_strike.iv, rate, dividend_yield, is_call, self._bsm
        )
        probability = float(
            probability_itm(
                spot, target.level, dte, at_target.iv, rate, dividend_yield, is_call, self._bsm
            )
        )
        return OptionRisk(
            as_of=as_of,
            spot=spot,
            strike=strike,
            is_call=is_call,
            dte=dte,
            premium_per_unit=premium_per_unit,
            bid=bid,
            delta=float(greeks.delta),
            gamma=float(greeks.gamma),
            vega=float(greeks.vega),
            theta=float(greeks.theta),
            target=target,
            prob_target=probability,
            prob_target_at_entry=prob_target_at_entry,
            target_vol=at_target,
            strike_vol=at_strike,
            vol_source=self._vol_source,
            dividend_basis=dividend_basis,
        )

    def conditions(self, risk: OptionRisk) -> tuple[ZombieCondition, ...]:
        delta = abs(risk.delta)
        ratio = risk.prob_ratio_vs_entry
        recoverable = risk.recoverable_pct_of_premium
        odds = risk.prob_target < self._max_prob_target and (
            ratio is not None and ratio < self._max_prob_ratio
        )
        odds_detail = (
            f"N(d2) at the {self._payoff_ratio:g}:1 target is {risk.prob_target:.1%} (ceiling "
            f"{self._max_prob_target:.1%})"
        )
        odds_detail += (
            "; no entry probability is recorded, so the ratio test cannot run"
            if ratio is None
            else f" and {ratio:.2f}x the entry's (ceiling {self._max_prob_ratio:.2f}x)"
        )
        return (
            ZombieCondition(
                "delta",
                delta < self._max_delta,
                delta,
                self._max_delta,
                f"delta {delta:.3f} against a ceiling of {self._max_delta:.3f}",
            ),
            ZombieCondition(
                "dte",
                risk.dte < self._max_dte,
                float(risk.dte),
                float(self._max_dte),
                f"{risk.dte} DTE against a ceiling of {self._max_dte}",
            ),
            ZombieCondition(
                "prob_target", odds, risk.prob_target, self._max_prob_target, odds_detail
            ),
            ZombieCondition(
                "recoverable",
                recoverable is not None and recoverable >= self._min_recoverable,
                recoverable,
                self._min_recoverable,
                "no bid, so there is nothing to rescue; it runs to the mandatory close"
                if recoverable is None
                else f"the bid is {recoverable:.1%} of the premium against a floor of "
                f"{self._min_recoverable:.1%}",
            ),
        )

    # ── the test over sessions ──

    def review(
        self, position: OptionPosition, risk: OptionRisk, *, record: bool = True
    ) -> ZombieAssessment:
        """One session's review of an open position: recorded, counted against the earlier
        reviews, and the penalty booked at confirmation."""
        if position.position_id is None or not position.is_open:
            raise ZombieError("only an open, recorded position is reviewed")
        today = self._now().date()
        if risk.as_of > today:
            raise ZombieError(
                f"a review dated {risk.as_of} is of a session that has not happened (today is "
                f"{today}); the confirmation counts sessions somebody could have reviewed"
            )
        if risk.as_of < position.opened_on:
            raise ZombieError(
                f"a review dated {risk.as_of} is before position {position.position_id} opened on "
                f"{position.opened_on}"
            )
        conditions = self.conditions(risk)
        met = all(condition.met for condition in conditions)
        if record:
            options_repo.record_observation(
                self._store,
                ZombieObservation(
                    position_id=position.position_id,
                    observed_on=risk.as_of,
                    all_conditions_met=met,
                    delta=risk.delta,
                    dte=risk.dte,
                    prob_target=risk.prob_target,
                    prob_ratio_vs_entry=risk.prob_ratio_vs_entry,
                    recoverable_pct_of_premium=risk.recoverable_pct_of_premium,
                    detail={
                        c.name: {"met": c.met, "observed": c.observed, "threshold": c.threshold}
                        for c in conditions
                    },
                    recorded_at=self._now(),
                ),
            )
        streak = self._streak(position.position_id, today=risk.as_of, today_met=met)
        assessment = ZombieAssessment(
            position_id=position.position_id,
            observed_on=risk.as_of,
            conditions=conditions,
            consecutive_sessions=streak,
            required_sessions=self._required,
        )
        if not (assessment.fires and record and self._book_at_detection):
            return assessment
        if self._penalty.booked_loss_penalty(position.position_id) > ZERO:
            return assessment
        recoverable = Decimal(repr(risk.bid or 0.0)) * Decimal(repr(position.multiplier))
        recovered = recoverable * position.contracts * (position.fx_to_base or Decimal(1))
        expected = max(ZERO, position.premium_base - recovered)
        event = self._penalty.book_loss_detected(
            position_id=position.position_id,
            expected_loss=expected,
            on=risk.as_of,
            note=(
                f"zombie confirmed on {streak} consecutive sessions; the penalty is booked at "
                "detection (zombie_test.book_penalty_at)"
            ),
        )
        return ZombieAssessment(
            position_id=assessment.position_id,
            observed_on=assessment.observed_on,
            conditions=assessment.conditions,
            consecutive_sessions=assessment.consecutive_sessions,
            required_sessions=assessment.required_sessions,
            penalty_booked=event.penalty_added,
        )

    def _streak(self, position_id: int, *, today: date, today_met: bool) -> int:
        """How many of the most recent reviews met every condition, this one included whether or
        not it was written (a dry run reports the streak the session actually produces)."""
        readings = [
            (day, met)
            for day, met in options_repo.observations(self._store, position_id, as_of=today)
            if day != today
        ]
        readings.insert(0, (today, today_met))
        streak = 0
        for _, met in readings:
            if not met:
                break
            streak += 1
        return streak

    # ── the theta budget ──

    def theta_budget(
        self,
        *,
        spot: float,
        strike: float,
        is_call: bool,
        sigma: float,
        rate: float,
        dividend_yield: float,
        premium_per_unit: float,
        entry_dte: int,
        expected_resolution_days: int,
    ) -> ThetaBudget:
        """What survives to the expected resolution with nothing else moving: extrinsic value
        decays roughly with the square root of time, so a long hold burns much of it first."""
        resolution_dte = max(entry_dte - expected_resolution_days, 0)
        value = float(
            bsm_price(spot, strike, resolution_dte, sigma, rate, dividend_yield, is_call, self._bsm)
        )
        remaining = value / premium_per_unit if premium_per_unit > 0 else 0.0
        return ThetaBudget(
            premium_per_unit=premium_per_unit,
            value_at_resolution=value,
            remaining_share=remaining,
            floor=self._min_recoverable,
            resolution_dte=resolution_dte,
        )
