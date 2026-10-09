"""Position risk: the option side and the equity side.

What this module refuses to do is as much of its design as what it does.

**A drawdown is never an exit.** There is no code path here that takes a
percentage loss as an input to an exit decision. The forward measure is
``N(d2)`` at the target, recomputed from **the strike's own smile implied
volatility**, and the zombie test needs all four of its conditions on
``zombie_test.consecutive_sessions_required`` consecutive review sessions before
it fires. A long option down on a two-day move with months of tenor left is
noise.

**The penalty is booked at detection.** :meth:`RiskEngine.zombie` writes the
``loss_detected`` row itself, the moment confirmation lands, rather than leaving
it to whoever eventually closes the position. Deferring it would reward holding a
dead position to expiry.

**A stop is volatility-scaled, always.** ``equity_risk.stop_type`` is
``volatility_scaled`` and this module has no branch that produces a fixed
percentage. A 20% stop is noise on a 90%-realised-vol name and far too loose on a
15%-vol one, so the distance comes from ``ATR(data.atr_period)`` and nothing
else.

**A winner is never trimmed to fit a cap.** ``positions.max_initial_size_pct_nav``
binds at purchase, against NAV on the purchase date. A position that compounds
past it is reported and left alone: ``positions.rebalance_winners`` is false.

**Stale external data is UNRELIABLE, not approximate.** IBKR is the single source
of truth for the portfolio, and holdings at brokers with no readable API are
entered by hand. Beyond ``positions.external_data_stale_days`` the whole cap
report says so rather than returning a number known to be wrong.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..config import Config
from ..logging_setup import get_logger
from ..quant.bsm import BsmInputs, bsm_greeks, bsm_price, probability_itm
from ..store.db import Store
from ..store.ledger import LedgerError, Position, get_position, has_new_dated_catalyst
from ..store.nav import record_nav
from ..util.timeutil import iso_date, parse_date, utc_today
from .calendar import CloseDate, TradingCalendars
from .frames import BarSeries
from .penalty import PenaltyLedger
from .smile import Smile, SmileQuote
from .stats import average_true_range

log = get_logger("compute.risk")

#: Where a target level came from.
TARGET_FROM_LEDGER = "ledger"
TARGET_DERIVED = "derived_payoff_ratio"

#: Cap dimensions reported by :meth:`RiskEngine.cap_utilisation`.
CAP_POSITION = "position"
CAP_SECTOR = "sector"
CAP_COUNTRY = "country"
CAP_CURRENCY = "currency"

#: The word the caller prints when external holdings are too old to trust.
UNRELIABLE = "UNRELIABLE"

#: Holding origins.
SOURCE_IBKR = "ibkr"
SOURCE_EXTERNAL = "external"

#: Seconds in a day, for turning a stale-days threshold into an age comparison.
SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class TargetLevel:
    """The underlying price the probability is measured against."""

    level: float
    source: str
    payoff_ratio: float

    @property
    def is_stated(self) -> bool:
        return self.source == TARGET_FROM_LEDGER


@dataclass(frozen=True)
class OptionRisk:
    """Everything the zombie test and the review table need for one option."""

    as_of: date
    spot: float
    strike: float
    is_call: bool
    dte: int
    premium_paid: float
    current_bid: float | None
    delta: float
    gamma: float
    vega: float
    theta: float
    target: TargetLevel
    prob_target: float
    prob_target_at_entry: float | None
    #: The vol N(d2) was computed on: the smile read AT THE TARGET.
    vol_used: float
    vol_quote: SmileQuote
    #: The vol the greeks were computed on: the smile read AT THE STRIKE. A
    #: different point of the same smile, and a different number whenever there
    #: is skew — which is the normal case, and why delta was biased before these
    #: were separated.
    strike_vol: float | None = None
    strike_vol_quote: SmileQuote | None = None
    mandatory_close: CloseDate | None = None

    @property
    def prob_ratio_vs_entry(self) -> float | None:
        if not self.prob_target_at_entry:
            return None
        return self.prob_target / self.prob_target_at_entry

    @property
    def recoverable_pct_of_premium(self) -> float | None:
        if self.current_bid is None or self.premium_paid <= 0:
            return None
        return self.current_bid / self.premium_paid


@dataclass(frozen=True)
class ZombieCondition:
    name: str
    met: bool
    observed: float | None
    threshold: float
    detail: str


@dataclass(frozen=True)
class ZombieAssessment:
    """One session's reading, and whether confirmation has now landed."""

    position_id: int | None
    observed_on: date
    conditions: tuple[ZombieCondition, ...]
    consecutive_sessions: int
    required_sessions: int
    penalty_booked: float | None = None

    @property
    def all_conditions_met(self) -> bool:
        return all(condition.met for condition in self.conditions)

    @property
    def fires(self) -> bool:
        """True only once the condition has held on enough consecutive reviews."""
        return self.all_conditions_met and self.consecutive_sessions >= self.required_sessions

    def unmet(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.conditions if not c.met)

    def summary(self) -> str:
        if self.fires:
            return (
                f"zombie confirmed on {self.consecutive_sessions} consecutive sessions "
                f"(required {self.required_sessions})"
            )
        if self.all_conditions_met:
            return (
                f"all conditions met, but only {self.consecutive_sessions} of "
                f"{self.required_sessions} consecutive sessions. A single session cannot "
                "trigger an exit."
            )
        return "not a zombie: " + ", ".join(c.detail for c in self.conditions if not c.met)


@dataclass(frozen=True)
class ThetaBudget:
    """What the position is worth if the thesis takes its full expected time."""

    premium_paid: float
    value_at_resolution: float
    remaining_pct_of_premium: float
    floor_pct: float
    resolution_dte: int
    passes: bool

    def summary(self) -> str:
        verdict = "clears" if self.passes else "FAILS"
        return (
            f"{verdict} the theta budget: {self.remaining_pct_of_premium:.0%} of premium "
            f"survives to {self.resolution_dte} DTE with nothing else moving, against a "
            f"floor of {self.floor_pct:.0%}"
        )


@dataclass(frozen=True)
class StopLevels:
    """Volatility-scaled stops. There is no fixed-percentage alternative."""

    atr: float
    atr_period: int
    entry_price: float
    stop: float
    stop_distance_pct: float
    trailing_high: float | None
    trailing_stop: float | None
    trailing_enabled: bool
    stop_atr_multiple: float
    trailing_atr_multiple: float

    @property
    def effective_stop(self) -> float:
        """The higher of the two once the position has run in your favour."""
        if self.trailing_stop is None:
            return self.stop
        return max(self.stop, self.trailing_stop)


@dataclass(frozen=True)
class Holding:
    """One position, from IBKR or entered by hand from another broker."""

    symbol: str
    market_value_base: float
    source: str = SOURCE_IBKR
    sector: str | None = None
    country: str | None = None
    currency: str | None = None
    contract_id: int | None = None
    cost_basis_base: float | None = None
    nav_at_purchase: float | None = None
    last_updated: str | None = None


@dataclass(frozen=True)
class CapEntry:
    dimension: str
    key: str
    exposure_base: float
    pct_of_nav: float
    cap: float
    breaches: bool
    #: A position past its INITIAL cap through compounding. Reported, never
    #: flagged for trimming: positions.rebalance_winners is false.
    exceeds_by_compounding: bool = False
    note: str | None = None


@dataclass(frozen=True)
class CapReport:
    as_of: date
    nav: float
    entries: tuple[CapEntry, ...]
    reliable: bool
    status: str
    reason: str | None = None
    stale_holdings: tuple[str, ...] = ()

    @property
    def breaches(self) -> tuple[CapEntry, ...]:
        return tuple(entry for entry in self.entries if entry.breaches)

    def binding(self, *, margin: float) -> tuple[CapEntry, ...]:
        """Caps within ``margin`` of binding — the ones worth reporting."""
        return tuple(
            entry for entry in self.entries if entry.pct_of_nav >= entry.cap * (1.0 - margin)
        )


@dataclass(frozen=True)
class CircuitBreaker:
    as_of: date
    window_days: int
    peak_nav: float | None
    current_nav: float | None
    drawdown: float | None
    threshold: float
    tripped: bool
    action: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class AveragingDownVerdict:
    allowed: bool
    reason: str
    catalyst_dates: tuple[str, ...] = ()


class RiskEngine:
    """Everything in ``risk_engine.py``, bound to config and the store once."""

    def __init__(
        self,
        store: Store,
        config: Config,
        *,
        calendars: TradingCalendars | None = None,
        penalty: PenaltyLedger | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._calendars = calendars or TradingCalendars.from_config(config)
        self._penalty = penalty or PenaltyLedger(store, config)
        self._bsm = BsmInputs.from_config(config)

        sleeve = config.section("option_sleeve")
        self._payoff_ratio = sleeve.require_float("min_payoff_ratio")

        zombie = config.section("zombie_test")
        self._max_delta = zombie.require_float("max_delta")
        self._max_dte = zombie.require_int("max_dte")
        self._max_prob_target = zombie.require_float("max_prob_target")
        self._max_prob_ratio = zombie.require_float("max_prob_ratio_vs_entry")
        self._min_recoverable = zombie.require_float("min_recoverable_pct_of_premium")
        self._required_sessions = zombie.require_int("consecutive_sessions_required")
        self._prob_vol_source = zombie.require_str("prob_vol_source")

        tenor = config.section("tenor")
        self._close_trading_days = tenor.require_int("mandatory_close_trading_days")
        self._fop_close_trading_days = config.require_int(
            "instruments.futures_options_mandatory_close_trading_days"
        )

        equity = config.section("equity_risk")
        self._stop_type = equity.require_str("stop_type")
        self._stop_multiple = equity.require_float("stop_atr_multiple")
        self._trailing_enabled = equity.require_bool("trailing_stop_enabled")
        self._trailing_multiple = equity.require_float("trailing_stop_atr_multiple")
        self._allow_averaging_down = equity.require_bool("allow_averaging_down")
        self._atr_period = config.require_int("data.atr_period")

        positions = config.section("positions")
        self._min_size_pct = positions.require_float("min_size_pct_nav")
        self._max_initial_pct = positions.require_float("max_initial_size_pct_nav")
        self._max_sector_pct = positions.require_float("max_sector_pct_nav")
        self._max_country_pct = positions.require_float("max_country_pct_nav")
        self._max_currency_pct = positions.require_float("max_single_foreign_currency_pct")
        self._rebalance_winners = positions.require_bool("rebalance_winners")
        self._external_stale_days = positions.require_int("external_data_stale_days")

        breaker = config.section("portfolio_circuit_breaker")
        self._breaker_threshold = breaker.require_float("monthly_drawdown_pct")
        self._breaker_action = breaker.require_str("action")
        self._breaker_window_days = breaker.require_int("window_days")
        self._breaker_min_observations = breaker.require_int("min_observations")

        self._base_currency = config.require_str("account.base_currency")

    # ─────────────────────────  the option side  ─────────────────────────

    def target_level(
        self, *, strike: float, premium_paid: float, is_call: bool, stated: float | None
    ) -> TargetLevel:
        """The price the 3:1 payoff is measured at.

        The ledger's own ``target`` wins when one was recorded: the mandate asks
        for "a realistic, NAMED target price", and a named one is a judgment the
        entry made. Otherwise the level at which intrinsic value reaches
        ``option_sleeve.min_payoff_ratio`` times premium is derived, and labelled
        as derived so it is never read back as a stated target.
        """
        if stated is not None and stated > 0:
            return TargetLevel(
                level=stated, source=TARGET_FROM_LEDGER, payoff_ratio=self._payoff_ratio
            )
        move = self._payoff_ratio * premium_paid
        level = strike + move if is_call else max(strike - move, self._bsm.min_price)
        return TargetLevel(level=level, source=TARGET_DERIVED, payoff_ratio=self._payoff_ratio)

    def assess_option(
        self,
        *,
        as_of: date,
        spot: float,
        strike: float,
        is_call: bool,
        expiry: date,
        premium_paid: float,
        current_bid: float | None,
        smile: Smile,
        rate: float,
        dividend_yield: float,
        prob_target_at_entry: float | None = None,
        stated_target: float | None = None,
        exchange: str | None = None,
        is_futures_option: bool = False,
    ) -> OptionRisk:
        """Greeks on the strike's own smile vol, and N(d2) on the target's.

        **Two reads, two points on one smile.** The volatility fed to d2 is read
        at the TARGET level, not at the money: constant-vol Black-Scholes
        understates the terminal probability on an out-of-the-money strike, and
        that understatement is precisely what produces a false zombie positive.
        The greeks price this contract, so they are read at its own STRIKE —
        they used to share the target's vol, which biased delta by the skew
        between the two points and fed that bias straight into
        ``zombie_test.max_delta``.

        Both quotes travel on the result with their provenance. A read that
        clamped to the outermost surviving strike, rather than landing on a
        listed one, is a real answer but a weaker one, and the caller is told
        which it got instead of having to assume.

        ``dividend_yield`` is required, with no default. It used to default to
        zero and no caller ever passed anything else, which made every payer an
        unmarked non-payer: q enters d1 as ``(r - q + sigma^2/2) * tau`` and
        discounts the spot by ``exp(-q * tau)``, so both N(d2) and delta read
        high. On a payer-like case — spot 60, strike 66, 150 days, 28% vol, q=5%
        — N(d2) is overstated by about 15% and delta by 0.048, and
        ``zombie_test.max_prob_ratio_vs_entry`` compares today's N(d2) against
        entry's, which puts that bias directly on the exit gate. A required
        argument is what stops the next caller inheriting the same silence.
        """
        dte = (expiry - as_of).days
        target = self.target_level(
            strike=strike, premium_paid=premium_paid, is_call=is_call, stated=stated_target
        )
        quote = smile.iv_at(target.level)
        if quote is None:
            raise ValueError(
                "the smile carries no implied volatility, so N(d2) cannot be computed. "
                "Report the position as unmeasurable rather than substituting a constant."
            )

        # TWO reads off the same smile, at two different points, because they
        # price two different things. N(d2) asks how likely the underlying is to
        # reach the TARGET, so it takes the vol at the target. The greeks price
        # THIS contract, so they take the vol at its own strike. Feeding the
        # target's vol to the greeks biased delta — on a normal equity skew a
        # call's target sits further out than its strike, where implied vol is
        # lower, so the reported delta came out low and drifted towards firing
        # zombie condition 1. Same class of error as pricing a payer at q = 0.
        strike_quote = smile.iv_at(strike) or quote
        greeks = bsm_greeks(
            spot, strike, dte, strike_quote.iv, rate, dividend_yield, is_call, self._bsm
        )
        probability = float(
            probability_itm(
                spot, target.level, dte, quote.iv, rate, dividend_yield, is_call, self._bsm
            )
        )
        close = self.mandatory_close(expiry, exchange=exchange, is_futures_option=is_futures_option)

        return OptionRisk(
            as_of=as_of,
            spot=spot,
            strike=strike,
            is_call=is_call,
            dte=dte,
            premium_paid=premium_paid,
            current_bid=current_bid,
            delta=float(greeks.delta),
            gamma=float(greeks.gamma),
            vega=float(greeks.vega),
            theta=float(greeks.theta),
            target=target,
            prob_target=probability,
            prob_target_at_entry=prob_target_at_entry,
            vol_used=quote.iv,
            vol_quote=quote,
            strike_vol=strike_quote.iv,
            strike_vol_quote=strike_quote,
            mandatory_close=close,
        )

    def zombie(
        self,
        risk: OptionRisk,
        *,
        position_id: int | None = None,
        record: bool = True,
        book_penalty: bool = True,
    ) -> ZombieAssessment:
        """The four-condition test, with two-session confirmation.

        A percentage drawdown is not among the conditions and cannot be made one:
        it is backward-looking and path-dependent, and the forward probability of
        reaching the target is what actually decides whether capital is still
        working.
        """
        conditions = self._zombie_conditions(risk)
        all_met = all(condition.met for condition in conditions)

        if record and position_id is not None:
            # The date is the caller's `as_of`, and the confirmation counts
            # distinct dates. Bounding it to the position's own life is what
            # stops two back-dated calls in one sitting reaching the count and
            # booking a penalty nothing can reverse.
            position = self._position(position_id)
            if position is not None:
                self._check_observation_date(position, risk.as_of)
            self._record_observation(position_id, risk, conditions, all_met)

        if position_id is None:
            # No ledger row to confirm against: this session's reading stands
            # alone, which by construction cannot reach the required count.
            consecutive = int(all_met)
        else:
            # Today's reading counts whether or not it was written. A dry-run
            # check that reported yesterday's streak would say "not yet" on the
            # session that actually confirms.
            consecutive = self._consecutive_confirmations(
                position_id, today=risk.as_of, today_met=all_met
            )
        assessment = ZombieAssessment(
            position_id=position_id,
            observed_on=risk.as_of,
            conditions=conditions,
            consecutive_sessions=consecutive,
            required_sessions=self._required_sessions,
        )

        if assessment.fires and book_penalty and position_id is not None:
            recoverable = risk.current_bid or 0.0
            expected_loss = max(0.0, risk.premium_paid - recoverable)
            already = self._penalty.detected_penalty_for(position_id)
            if already > 0:
                # Confirmation already booked on an earlier session. Booking again
                # would charge the same loss twice for one decision.
                log.info(
                    "zombie already booked for this position; not booking again",
                    extra={"position_id": position_id, "already_booked": already},
                )
                return assessment
            event = self._penalty.book_loss_detected(
                expected_loss=expected_loss,
                event_at=iso_date(risk.as_of),
                position_id=position_id,
                premium_paid=risk.premium_paid,
                note=(
                    "zombie confirmed across "
                    f"{consecutive} consecutive sessions; penalty booked at DETECTION "
                    "per zombie_test.book_penalty_at"
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
        return assessment

    def _zombie_conditions(self, risk: OptionRisk) -> tuple[ZombieCondition, ...]:
        delta = abs(risk.delta)
        ratio = risk.prob_ratio_vs_entry
        recoverable = risk.recoverable_pct_of_premium

        odds_met = risk.prob_target < self._max_prob_target and (
            ratio is not None and ratio < self._max_prob_ratio
        )
        odds_detail = (
            f"N(d2) at the {self._payoff_ratio:g}:1 target is {risk.prob_target:.1%} "
            f"(ceiling {self._max_prob_target:.1%})"
        )
        if ratio is None:
            odds_detail += "; no entry probability recorded, so the ratio test cannot run"
        else:
            odds_detail += (
                f" and {ratio:.2f}x the entry probability (ceiling {self._max_prob_ratio:.2f}x)"
            )

        return (
            ZombieCondition(
                name="delta",
                met=delta < self._max_delta,
                observed=delta,
                threshold=self._max_delta,
                detail=f"delta {delta:.3f} against a ceiling of {self._max_delta:.3f}",
            ),
            ZombieCondition(
                name="dte",
                met=risk.dte < self._max_dte,
                observed=float(risk.dte),
                threshold=float(self._max_dte),
                detail=f"{risk.dte} DTE against a ceiling of {self._max_dte}",
            ),
            ZombieCondition(
                name="prob_target",
                met=odds_met,
                observed=risk.prob_target,
                threshold=self._max_prob_target,
                detail=odds_detail,
            ),
            ZombieCondition(
                name="recoverable",
                met=recoverable is not None and recoverable >= self._min_recoverable,
                observed=recoverable,
                threshold=self._min_recoverable,
                detail=(
                    "no bid, so there is nothing to rescue; let it run to the mandatory close date"
                    if recoverable is None
                    else f"bid is {recoverable:.1%} of premium against a floor of "
                    f"{self._min_recoverable:.1%}"
                ),
            ),
        )

    def _position(self, position_id: int) -> Position | None:
        """The ledger row, or ``None`` when the reading has no position behind it."""
        return get_position(self._store, position_id)

    def _check_observation_date(self, position: Position, observed_on: date) -> None:
        """Refuse a review dated outside the position's own life.

        The two-session rule counts DISTINCT observation dates, and the date is
        whatever ``as_of`` the caller passed. Nothing bounded it, so two calls in
        one sitting with two different back-dated ``as_of`` values reached the
        confirmation count and booked a penalty — which is irreversible — without
        two sessions ever having happened.

        Both bounds matter. A date in the future is a review of a session that
        has not occurred; a date before the position was opened is a review of a
        position that did not exist. Neither can be a real reading.
        """
        today = utc_today()
        if observed_on > today:
            raise LedgerError(
                f"as_of {iso_date(observed_on)} is in the future, and a review cannot be "
                f"recorded for a session that has not happened (today is {iso_date(today)}). "
                "The zombie test confirms across consecutive REVIEW sessions, so a dated "
                "reading has to be one somebody could have taken."
            )
        opened = parse_date(position.opened_at)
        if opened is not None and observed_on < opened:
            raise LedgerError(
                f"as_of {iso_date(observed_on)} is before position {position.position_id} was "
                f"opened on {position.opened_at}. A review of a position that did not exist "
                "yet cannot count towards the consecutive-session confirmation."
            )

    def _record_observation(
        self,
        position_id: int,
        risk: OptionRisk,
        conditions: tuple[ZombieCondition, ...],
        all_met: bool,
    ) -> None:
        detail = json.dumps(
            {
                c.name: {"met": c.met, "observed": c.observed, "threshold": c.threshold}
                for c in conditions
            }
        )
        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO zombie_observations (
                    position_id, observed_on, all_conditions_met, delta, dte, prob_target,
                    prob_ratio_vs_entry, recoverable_pct_of_premium, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (position_id, observed_on) DO UPDATE SET
                    all_conditions_met = excluded.all_conditions_met,
                    delta = excluded.delta,
                    dte = excluded.dte,
                    prob_target = excluded.prob_target,
                    prob_ratio_vs_entry = excluded.prob_ratio_vs_entry,
                    recoverable_pct_of_premium = excluded.recoverable_pct_of_premium,
                    detail = excluded.detail
                """,
                (
                    position_id,
                    iso_date(risk.as_of),
                    int(all_met),
                    risk.delta,
                    risk.dte,
                    risk.prob_target,
                    risk.prob_ratio_vs_entry,
                    risk.recoverable_pct_of_premium,
                    detail,
                ),
            )

    def _consecutive_confirmations(
        self, position_id: int, *, today: date | None = None, today_met: bool | None = None
    ) -> int:
        """How many of the most recent REVIEW sessions met every condition.

        Consecutive reviews, not consecutive calendar days. A review skipped over
        a weekend or a holiday does not reset the count, and a review that came
        back clean does.

        ``today`` overlays this session's reading whether or not it was written,
        so a dry-run check reports the streak the session actually produces
        rather than the one that preceded it.
        """
        rows = self._store.execute(
            "SELECT observed_on, all_conditions_met FROM zombie_observations "
            "WHERE position_id = ? ORDER BY observed_on DESC",
            (position_id,),
        ).fetchall()
        readings = [(str(row[0]), bool(int(row[1]))) for row in rows]
        if today is not None:
            stamp = iso_date(today)
            readings = [(day, met) for day, met in readings if day != stamp]
            readings.insert(0, (stamp, bool(today_met)))
            readings.sort(key=lambda item: item[0], reverse=True)

        streak = 0
        for _, met in readings:
            if not met:
                break
            streak += 1
        return streak

    def theta_budget(
        self,
        *,
        spot: float,
        strike: float,
        is_call: bool,
        sigma: float,
        rate: float,
        premium_paid: float,
        entry_dte: int,
        expected_resolution_days: int,
        dividend_yield: float,
    ) -> ThetaBudget:
        """What survives to the expected resolution date with nothing else moving.

        Extrinsic value scales roughly with the square root of time, so a long
        hold burns a large share of it before the thesis has had a chance. If the
        remainder falls below ``zombie_test.min_recoverable_pct_of_premium``, the
        tenor is wrong: fix it or drop the idea. Never enter hoping it resolves
        early.

        ``dividend_yield`` is required for the reason given on
        :meth:`assess_option`: a payer discounted at zero is priced as something
        it is not, and here that lands on ``tenor.require_theta_budget_check``.
        """
        resolution_dte = max(entry_dte - expected_resolution_days, 0)
        value = float(
            bsm_price(spot, strike, resolution_dte, sigma, rate, dividend_yield, is_call, self._bsm)
        )
        remaining = value / premium_paid if premium_paid > 0 else 0.0
        return ThetaBudget(
            premium_paid=premium_paid,
            value_at_resolution=value,
            remaining_pct_of_premium=remaining,
            floor_pct=self._min_recoverable,
            resolution_dte=resolution_dte,
            passes=remaining >= self._min_recoverable,
        )

    def mandatory_close(
        self, expiry: date, *, exchange: str | None = None, is_futures_option: bool = False
    ) -> CloseDate:
        """The close date, in trading days, holidays respected.

        A futures option gets the wider buffer in
        ``instruments.futures_options_mandatory_close_trading_days``: overnight
        gap risk is worse, so the exit needs more room.
        """
        days = self._fop_close_trading_days if is_futures_option else self._close_trading_days
        return self._calendars.close_date(expiry, trading_days=days, exchange=exchange)

    # ─────────────────────────  the equity side  ─────────────────────────

    def equity_stops(
        self,
        bars: BarSeries,
        *,
        entry_price: float,
        trailing_high: float | None = None,
    ) -> StopLevels | None:
        """Volatility-scaled stop and trailing stop. ``None`` on short history.

        There is no fixed-percentage branch here, and
        ``equity_risk.stop_type`` is asserted rather than consulted for a choice:
        a 20% stop is noise on a 90%-realised-vol name and far too loose on a
        15%-vol one.
        """
        if self._stop_type != "volatility_scaled":
            raise ValueError(
                f"equity_risk.stop_type is {self._stop_type!r}. This engine produces "
                "volatility-scaled stops only; a fixed-percentage stop is not "
                "implemented because it is not permitted."
            )
        atr = average_true_range(bars.high, bars.low, bars.close, period=self._atr_period)
        if atr is None or atr <= 0 or entry_price <= 0:
            return None

        stop = entry_price - self._stop_multiple * atr
        trailing = None
        if self._trailing_enabled and trailing_high is not None:
            trailing = trailing_high - self._trailing_multiple * atr
        return StopLevels(
            atr=atr,
            atr_period=self._atr_period,
            entry_price=entry_price,
            stop=stop,
            stop_distance_pct=(entry_price - stop) / entry_price,
            trailing_high=trailing_high,
            trailing_stop=trailing,
            trailing_enabled=self._trailing_enabled,
            stop_atr_multiple=self._stop_multiple,
            trailing_atr_multiple=self._trailing_multiple,
        )

    def external_holdings(self, *, as_of: date) -> tuple[list[Holding], list[str]]:
        """Manually entered non-IBKR holdings, and which of them are stale."""
        rows = self._store.execute(
            "SELECT broker, symbol, quantity, currency, market_value_base, last_updated, "
            "sector, country FROM external_positions ORDER BY broker, symbol"
        ).fetchall()
        holdings: list[Holding] = []
        stale: list[str] = []
        cutoff = as_of - timedelta(days=self._external_stale_days)
        for row in rows:
            updated = parse_date(row["last_updated"])
            label = f"{row['broker']}:{row['symbol']}"
            if updated is None or updated < cutoff:
                stale.append(label)
            holdings.append(
                Holding(
                    symbol=str(row["symbol"]),
                    market_value_base=float(row["market_value_base"]),
                    source=SOURCE_EXTERNAL,
                    sector=row["sector"],
                    country=row["country"],
                    currency=row["currency"],
                    last_updated=row["last_updated"],
                )
            )
        return holdings, stale

    def cap_utilisation(
        self,
        holdings: Sequence[Holding],
        *,
        nav: float,
        as_of: date,
        include_external: bool = True,
    ) -> CapReport:
        """Position, sector, country and currency utilisation.

        External holdings are folded in because NAV and every cap are defined over
        the whole book, not over the part with an API. If any of them is older
        than ``positions.external_data_stale_days`` the report is UNRELIABLE:
        returning a number known to be wrong is worse than returning none.
        """
        combined = list(holdings)
        stale: list[str] = []
        if include_external:
            external, stale = self.external_holdings(as_of=as_of)
            combined.extend(external)

        if nav <= 0:
            return CapReport(
                as_of=as_of,
                nav=nav,
                entries=(),
                reliable=False,
                status=UNRELIABLE,
                reason="NAV is not positive, so no cap utilisation can be computed",
                stale_holdings=tuple(stale),
            )

        entries: list[CapEntry] = []
        for holding in combined:
            entries.append(self._position_cap_entry(holding, nav))
        entries.extend(self._grouped_cap_entries(combined, nav))

        reliable = not stale
        return CapReport(
            as_of=as_of,
            nav=nav,
            entries=tuple(entries),
            reliable=reliable,
            status="ok" if reliable else UNRELIABLE,
            reason=(
                None
                if reliable
                else (
                    f"non-IBKR holdings {sorted(stale)} were last confirmed more than "
                    f"{self._external_stale_days} days ago. Cap utilisation over a stale "
                    "book is a number known to be wrong."
                )
            ),
            stale_holdings=tuple(stale),
        )

    def _position_cap_entry(self, holding: Holding, nav: float) -> CapEntry:
        current_pct = holding.market_value_base / nav
        initial_pct = None
        if holding.cost_basis_base is not None and (holding.nav_at_purchase or 0) > 0:
            initial_pct = holding.cost_basis_base / float(holding.nav_at_purchase)

        # The cap binds on the INITIAL size, measured against NAV at the purchase
        # date. A position that compounds past it is reported, never trimmed:
        # positions.rebalance_winners is false.
        measured = initial_pct if initial_pct is not None else current_pct
        breaches = measured > self._max_initial_pct
        by_compounding = (
            initial_pct is not None
            and initial_pct <= self._max_initial_pct
            and current_pct > self._max_initial_pct
        )
        note = None
        if by_compounding:
            note = (
                f"{holding.symbol} has compounded from {initial_pct:.1%} at purchase to "
                f"{current_pct:.1%} of NAV. The cap is on the INITIAL size; winners are "
                "not trimmed to fit it."
            )
        elif current_pct < self._min_size_pct:
            note = (
                f"{holding.symbol} is {current_pct:.1%} of NAV, below the "
                f"{self._min_size_pct:.1%} floor. An orphan: top it up or close it."
            )
        return CapEntry(
            dimension=CAP_POSITION,
            key=holding.symbol,
            exposure_base=holding.market_value_base,
            pct_of_nav=current_pct,
            cap=self._max_initial_pct,
            breaches=breaches and not by_compounding,
            exceeds_by_compounding=by_compounding,
            note=note,
        )

    def _grouped_cap_entries(self, holdings: Sequence[Holding], nav: float) -> list[CapEntry]:
        groups: list[tuple[str, str, float]] = [
            (CAP_SECTOR, "sector", self._max_sector_pct),
            (CAP_COUNTRY, "country", self._max_country_pct),
            (CAP_CURRENCY, "currency", self._max_currency_pct),
        ]
        entries: list[CapEntry] = []
        for dimension, attribute, cap in groups:
            totals: dict[str, float] = {}
            for holding in holdings:
                key = getattr(holding, attribute) or "unmapped"
                if dimension == CAP_CURRENCY and key == self._base_currency:
                    # The cap is on a single FOREIGN currency. Base-currency
                    # exposure is not a currency risk to this account.
                    continue
                totals[key] = totals.get(key, 0.0) + holding.market_value_base
            for key, exposure in sorted(totals.items()):
                pct = exposure / nav
                entries.append(
                    CapEntry(
                        dimension=dimension,
                        key=key,
                        exposure_base=exposure,
                        pct_of_nav=pct,
                        cap=cap,
                        breaches=pct > cap,
                        note=(
                            "exposure with no "
                            f"{attribute} recorded; it still counts against the cap"
                            if key == "unmapped"
                            else None
                        ),
                    )
                )
        return entries

    def circuit_breaker(self, *, as_of: date, nav: float | None = None) -> CircuitBreaker:
        """Rolling-month drawdown against ``portfolio_circuit_breaker``.

        Freezes NEW risk, never the management of open risk: existing positions
        keep their own exit rules.
        """
        since = as_of - timedelta(days=self._breaker_window_days)
        rows = self._store.execute(
            "SELECT as_of, nav_base FROM nav_history WHERE as_of >= ? AND as_of <= ? "
            "ORDER BY as_of",
            (since.isoformat(), as_of.isoformat()),
        ).fetchall()
        observations = [(parse_date(row["as_of"]), float(row["nav_base"])) for row in rows]
        observations = [(d, v) for d, v in observations if d is not None]

        if len(observations) < self._breaker_min_observations:
            return CircuitBreaker(
                as_of=as_of,
                window_days=self._breaker_window_days,
                peak_nav=None,
                current_nav=nav,
                drawdown=None,
                threshold=self._breaker_threshold,
                tripped=False,
                action=self._breaker_action,
                status="n/a",
                reason=(
                    f"nav_history holds {len(observations)} observation(s) in the last "
                    f"{self._breaker_window_days} days, below the "
                    f"{self._breaker_min_observations} needed to measure a drawdown. The "
                    "breaker cannot be evaluated and is not reported as clear."
                ),
            )

        peak = max(value for _, value in observations)
        current = nav if nav is not None else observations[-1][1]
        drawdown = current / peak - 1.0 if peak > 0 else None
        tripped = drawdown is not None and drawdown < -self._breaker_threshold
        return CircuitBreaker(
            as_of=as_of,
            window_days=self._breaker_window_days,
            peak_nav=peak,
            current_nav=current,
            drawdown=drawdown,
            threshold=self._breaker_threshold,
            tripped=tripped,
            action=self._breaker_action,
            status="ok",
            reason=None,
        )

    def averaging_down(self, position: Position, *, as_of: date) -> AveragingDownVerdict:
        """May this losing position be added to?

        Only with a formal re-underwrite citing a NEW, DATED catalyst that did not
        exist at entry. Falling price is not a catalyst. This is the single most
        destructive pattern available to this account, and both of the book's
        worst positions were natural candidates for it.
        """
        if self._allow_averaging_down:
            return AveragingDownVerdict(
                allowed=True,
                reason="equity_risk.allow_averaging_down is true",
            )
        dates = tuple(
            str(entry.get("catalyst_date"))
            for entry in position.reunderwrite_log
            if entry.get("catalyst_date")
        )
        if has_new_dated_catalyst(position, as_of=as_of):
            return AveragingDownVerdict(
                allowed=True,
                reason=(
                    "a re-underwrite is logged with a new dated catalyst still ahead of "
                    f"{iso_date(as_of)}"
                ),
                catalyst_dates=dates,
            )
        return AveragingDownVerdict(
            allowed=False,
            reason=(
                "averaging down is prohibited while equity_risk.allow_averaging_down is "
                "false. It requires a formal re-underwrite logged against this position "
                "citing a NEW, DATED catalyst that did not exist at entry and has not yet "
                "passed. Falling price is not a catalyst."
            ),
            catalyst_dates=dates,
        )

    # ─────────────────────────  helpers  ─────────────────────────

    @property
    def prob_vol_source(self) -> str:
        return self._prob_vol_source

    @property
    def rebalance_winners(self) -> bool:
        return self._rebalance_winners

    def record_nav(self, *, as_of: date, nav: float, source: str = SOURCE_IBKR) -> None:
        """Append a NAV observation, so the circuit breaker has something to read.

        Delegates to ``store.nav.record_nav``, which is the single writer: the
        collect layer records one from the account ledger and cannot import this
        module, so the upsert cannot live here.
        """
        record_nav(self._store, as_of=as_of, nav=nav, source=source)


def holdings_from_rows(rows: Sequence[dict[str, Any]]) -> list[Holding]:
    """Adapt broker position rows to :class:`Holding`.

    The server feeds this from ``get_account_positions``; tests feed it literals.
    """
    out: list[Holding] = []
    for row in rows:
        out.append(
            Holding(
                symbol=str(row.get("symbol") or row.get("ticker") or ""),
                market_value_base=float(row.get("market_value_base") or 0.0),
                source=str(row.get("source") or SOURCE_IBKR),
                sector=row.get("sector"),
                country=row.get("country"),
                currency=row.get("currency"),
                contract_id=row.get("contract_id"),
                cost_basis_base=row.get("cost_basis_base"),
                nav_at_purchase=row.get("nav_at_purchase"),
                last_updated=row.get("last_updated"),
            )
        )
    return out


#: Every dimension cap_utilisation reports on.
CAP_DIMENSIONS = (CAP_POSITION, CAP_SECTOR, CAP_COUNTRY, CAP_CURRENCY)
