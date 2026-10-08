"""Four-way return decomposition, the flags, and the carry-illusion test.

Nominal returns from high-inflation, depreciating-currency markets are mirages.
A market up sixty percent in local terms can be down in euros, and a thirty-five
percent nominal bond yield against thirty-five percent inflation and twenty
percent annual depreciation is a negative real return.

Every market return therefore decomposes four ways::

    local nominal  ->  local real  ->  base nominal  ->  base real

and **ranking consumes base real only** (``fx_inflation.rank_on``). Local nominal
is kept because it must be displayed, never because it may be ranked on:
:func:`rank` takes decomposition objects and reads one field, so there is no call
site where a nominal figure could reach a decision.

The FX convention
-----------------

ECB reference rates are quoted as **units of local currency per euro**, so a
rising series is the euro strengthening and the local currency weakening. A local
asset is worth ``local_value / fx`` in euros, which makes the euro return
``(1 + local) * (fx_start / fx_end) - 1``. Getting that ratio upside down inverts
every emerging-market reading, so it is written once, here.

No hedging is ever recommended
------------------------------

``fx_inflation.require_physical_hedging`` is false. On a book this size the
transaction cost and operational complexity of hedging outweigh the benefit on
short-horizon tactical trades. Currency is a **measurement and ranking
discipline**: screen and rank in base-real terms so nominal illusions never reach
the trade table, and carry the exposure knowingly rather than paying to remove
it. The carry-illusion test uses the hedging COST as a yardstick — it does not
propose paying it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..config import Config
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..store.db import Store
from ..util.timeutil import iso_date, utc_stamp
from .frames import (
    PERCENT_TO_DECIMAL,
    MacroSeries,
    bar_series,
    contract_id_for_symbol,
    cpi_yoy,
    fx_ratio,
    macro_series,
)
from .stats import annualised_from_period, simple_return

log = get_logger("compute.fx")

#: Cell statuses, matching the grid's own vocabulary.
STATUS_OK = "ok"
STATUS_NA = "n/a"

#: The only field ranking may read. fx_inflation.rank_on must agree with it.
RANK_FIELD = "base_real"
RANK_ON_BASE_REAL = "base_currency_real"

#: Carry-illusion verdicts.
VERDICT_ILLUSION = "illusion"
VERDICT_OPPORTUNITY = "opportunity"
VERDICT_NA = "n/a"


@dataclass(frozen=True)
class FourWay:
    """One market's return, decomposed. ``base_real`` is the only rankable field."""

    market: str
    currency: str
    horizon_days: int
    as_of: date
    local_nominal: float | None = None
    local_real: float | None = None
    base_nominal: float | None = None
    base_real: float | None = None
    cpi_yoy: float | None = None
    base_cpi_yoy: float | None = None
    fx_return: float | None = None
    depreciation_annualised: float | None = None
    high_inflation: bool = False
    high_depreciation: bool = False
    status: str = STATUS_OK
    reason: str | None = None

    @property
    def is_rankable(self) -> bool:
        return self.status == STATUS_OK and self.base_real is not None

    @property
    def flagged(self) -> bool:
        return self.high_inflation or self.high_depreciation

    def display_note(self) -> str:
        """What a table has to say alongside a local nominal figure."""
        if not self.flagged:
            return ""
        flags = []
        if self.high_inflation:
            flags.append(f"CPI {self.cpi_yoy:.1%} year on year" if self.cpi_yoy else "high CPI")
        if self.high_depreciation:
            flags.append(
                f"{self.depreciation_annualised:.1%} annualised depreciation"
                if self.depreciation_annualised
                else "high depreciation"
            )
        return (
            f"FLAGGED ({'; '.join(flags)}): ranked on base-real figures only, and any "
            "thesis here must address the currency explicitly."
        )


@dataclass(frozen=True)
class CarryTest:
    """Whether a headline yield survives contact with covered interest parity."""

    currency: str
    as_of: date
    nominal_yield: float | None
    local_rate: float | None
    base_rate: float | None
    rate_differential: float | None
    hedged_equivalent: float | None
    base_real_yield: float | None
    verdict: str
    reason: str
    #: How the hedge cost was approximated, and over what tenor. CIP prices the
    #: forward off MONEY-MARKET rates at the hedge tenor; what this reads is each
    #: market's declared policy rate, which is an overnight instrument. On a
    #: steep curve the two differ, and the difference lands straight on the
    #: verdict — so it is stated rather than left to be assumed away.
    basis: str | None = None

    @property
    def is_illusion(self) -> bool:
        return self.verdict == VERDICT_ILLUSION


class BaseCurrencyRates:
    """Converts an amount in some currency into ``account.base_currency``.

    **The direction is decided here, once.** ``account.fx_series_by_currency``
    names ECB reference rates in UNITS OF THAT CURRENCY PER EURO — the same
    convention ``universe.yaml``'s per-market ``fx_series`` uses — so converting
    a premium in currency X into base means DIVIDING by the rate. Inverting it
    at each call site is how a conversion silently runs backwards, which on
    EUR/USD would be a factor of ~1.36 error in the permissive direction.

    Every lookup can fail, and failure is ``None`` rather than 1.0. The caller
    that wanted a rate and did not get one must leave its gate untested: an
    assumed parity is invisible and admits contracts the sleeve cannot afford.
    """

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        account = config.section("account")
        self._base = account.require_str("base_currency").upper()
        self._series_by_currency = {
            str(currency).upper(): str(series)
            for currency, series in dict(account.get("fx_series_by_currency", {}) or {}).items()
        }
        self._cache: dict[str, float | None] = {}

    @property
    def base_currency(self) -> str:
        return self._base

    def to_base(self, currency: str | None, *, as_of: date | None = None) -> float | None:
        """The multiplier taking an amount in ``currency`` to base. ``None`` if unknown.

        ``as_of`` uses the last observation KNOWN at that date, so a backtest
        cannot convert with a rate published after the decision it is pricing.
        Left unset, the newest observation wins, which is what a live session
        wants.
        """
        if not currency:
            return None
        code = currency.upper()
        if code == self._base:
            return 1.0

        cache_key = f"{code}@{as_of.isoformat() if as_of else 'latest'}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        rate = self._lookup(code, as_of)
        self._cache[cache_key] = rate
        return rate

    def _lookup(self, code: str, as_of: date | None) -> float | None:
        series_id = self._series_by_currency.get(code)
        if series_id is None:
            log.warning(
                "no FX series configured for a currency, so nothing can be converted "
                "into base; the caller must leave its comparison untested",
                extra={"currency": code, "base_currency": self._base},
            )
            return None

        series = macro_series(self._store, series_id)
        observation = series.known_at(as_of) if as_of is not None else series.latest
        if observation is None or observation.value is None:
            log.warning(
                "FX series is configured but holds no usable observation",
                extra={"currency": code, "series_id": series_id, "as_of": str(as_of)},
            )
            return None

        units_per_base = float(observation.value)
        if units_per_base <= 0:
            log.warning(
                "FX rate is not positive, so it is not a rate",
                extra={"currency": code, "series_id": series_id, "value": units_per_base},
            )
            return None
        # units_per_base is X per EUR; an amount in X becomes EUR by dividing.
        return 1.0 / units_per_base


class FxEngine:
    """The four-way decomposition and the carry-illusion test, over the store."""

    def __init__(self, store: Store, config: Config, markets: dict[str, dict[str, Any]]) -> None:
        self._store = store
        self._config = config
        self._markets = markets

        fx = config.section("fx_inflation")
        self._rank_on = fx.require_str("rank_on")
        if self._rank_on != RANK_ON_BASE_REAL:
            raise ConfigError(
                f"fx_inflation.rank_on is {self._rank_on!r}. This engine ranks on "
                f"{RANK_ON_BASE_REAL!r} and nothing else: a local nominal figure from a "
                "high-inflation market is a mirage, and there is no code path here that "
                "would let one drive a decision."
            )
        self._require_four_way = fx.require_bool("require_four_way_decomposition")
        self._high_inflation = fx.require_float("high_inflation_cpi_yoy")
        self._high_depreciation = fx.require_float("high_depreciation_3y_annualised")
        self._depreciation_years = fx.require_int("depreciation_window_years")
        self._carry_tolerance = fx.require_float("carry_illusion_tolerance")
        self._require_hedging = fx.require_bool("require_physical_hedging")
        self._flagged_rule = fx.require_str("flagged_market_rule")

        self._base_currency = config.require_str("account.base_currency")
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")
        self._base_market = self._market_for_currency(self._base_currency)

    # ── decomposition ──

    def decompose(self, market: str, *, as_of: date, horizon_days: int) -> FourWay:
        """The four-way return for one market over one horizon."""
        spec = self._markets.get(market)
        if spec is None:
            return self._absent(
                market, "?", horizon_days, as_of, f"{market} is not a declared market"
            )

        currency = str(spec.get("currency") or "?")
        index_symbol = str(spec.get("native_index") or "")
        contract_id = contract_id_for_symbol(self._store, index_symbol) if index_symbol else None
        if contract_id is None:
            return self._absent(
                market,
                currency,
                horizon_days,
                as_of,
                f"native index {index_symbol or '(undeclared)'} is not in the universe, so "
                "there is no level to measure. A US-listed wrapper is not a substitute: it "
                "embeds a dollar trade in every reading.",
            )

        start = as_of - timedelta(days=horizon_days)
        bars = bar_series(self._store, contract_id, since=start - timedelta(days=horizon_days))
        cutoff = start.isoformat()
        window = [
            close
            for ts, close in zip(bars.ts, bars.close, strict=True)
            if ts[: len(cutoff)] >= cutoff
        ]
        if len(window) < _MIN_ENDPOINTS:
            return self._absent(
                market,
                currency,
                horizon_days,
                as_of,
                f"only {len(window)} bar(s) for {index_symbol} inside the {horizon_days}-day "
                "window; a return needs two endpoints",
            )

        local_nominal = simple_return(window[0], window[-1])
        cpi_yoy = self._cpi_yoy(spec, as_of)
        base_cpi_yoy = self._base_cpi_yoy(as_of)
        fx_ratio = self._fx_ratio(spec, as_of=as_of, horizon_days=horizon_days)

        local_real = _deflate(local_nominal, cpi_yoy, horizon_days, self._calendar_days_per_year)
        base_nominal = (
            None
            if local_nominal is None or fx_ratio is None
            else ((1.0 + local_nominal) * fx_ratio - 1.0)
        )
        base_real = _deflate(base_nominal, base_cpi_yoy, horizon_days, self._calendar_days_per_year)
        fx_return = None if fx_ratio is None else fx_ratio - 1.0
        depreciation = self._trailing_depreciation(spec, as_of=as_of)

        missing = self._missing_legs(local_nominal, local_real, base_nominal, base_real, spec)
        status = STATUS_OK if not missing else STATUS_NA
        return FourWay(
            market=market,
            currency=currency,
            horizon_days=horizon_days,
            as_of=as_of,
            local_nominal=local_nominal,
            local_real=local_real,
            base_nominal=base_nominal,
            base_real=base_real,
            cpi_yoy=cpi_yoy,
            base_cpi_yoy=base_cpi_yoy,
            fx_return=fx_return,
            depreciation_annualised=depreciation,
            high_inflation=cpi_yoy is not None and cpi_yoy >= self._high_inflation,
            high_depreciation=(
                depreciation is not None and depreciation <= -self._high_depreciation
            ),
            status=status,
            reason="; ".join(missing) if missing else None,
        )

    def decompose_all(self, *, as_of: date, horizon_days: int) -> list[FourWay]:
        return [
            self.decompose(market, as_of=as_of, horizon_days=horizon_days)
            for market in sorted(self._markets)
        ]

    # ── ranking ──

    def rank(self, results: Sequence[FourWay], *, descending: bool = True) -> list[FourWay]:
        """Sort by base real, and by nothing else.

        Rows that could not be decomposed are excluded rather than sorted to the
        bottom: an unmeasured market is not a poorly performing one.
        """
        rankable = [row for row in results if row.is_rankable]
        return sorted(
            rankable,
            key=lambda row: getattr(row, RANK_FIELD),
            reverse=descending,
        )

    # ── the carry-illusion test ──

    def carry_illusion(
        self,
        currency: str,
        *,
        nominal_yield: float,
        as_of: date,
        local_rate: float | None = None,
        base_rate: float | None = None,
    ) -> CarryTest:
        """Is a headline yield an opportunity, or compensation for depreciation?

        Under covered interest parity the forward discount approximates the rate
        differential, so hedging a high-yield currency costs roughly that
        differential and a hedged high-yield bond returns approximately the base
        rate. A high nominal yield is not an opportunity unless the
        hedged-equivalent or base-real yield is high.

        **The base rate cancels, and that is the theory rather than a slip.**
        ``hedged - base = (yield - local + base) - base = yield - local``: the
        pickup a hedged foreign bond earns over the domestic risk-free rate IS
        its spread over its own money-market rate. Anyone reading the code and
        "fixing" the cancellation would be adding the base rate twice; a test
        pins the identity. What the base rate does affect is the second leg, the
        real yield after base inflation, and that is not an identity.

        **The hedge cost is approximated with policy rates**, which are overnight
        instruments, against what is usually a long bond yield. The result says
        so in ``basis``: on a steep curve the money-market differential at the
        hedge tenor is not this one, and the difference lands on the verdict.
        """
        market = self._market_for_currency(currency)
        resolved_local = local_rate if local_rate is not None else self._policy_rate(market, as_of)
        resolved_base = (
            base_rate if base_rate is not None else self._policy_rate(self._base_market, as_of)
        )
        base_cpi = self._base_cpi_yoy(as_of)

        basis = (
            f"hedge cost approximated by the {market or currency} policy rate less the "
            f"{self._base_market or self._base_currency} policy rate, both overnight. CIP "
            "prices the forward off money-market rates at the HEDGE tenor, so on a steep "
            "curve this differential is not the one a hedge would actually cost."
        )

        def verdict(
            name: str,
            reason: str,
            *,
            differential: float | None = None,
            hedged: float | None = None,
            base_real: float | None = None,
        ) -> CarryTest:
            return CarryTest(
                currency=currency,
                as_of=as_of,
                nominal_yield=nominal_yield,
                local_rate=resolved_local,
                base_rate=resolved_base,
                rate_differential=differential,
                hedged_equivalent=hedged,
                base_real_yield=base_real,
                verdict=name,
                reason=reason,
                basis=basis,
            )

        if resolved_local is None or resolved_base is None:
            return verdict(
                VERDICT_NA,
                "the carry-illusion test needs both the local and the base policy rate; "
                f"{'local' if resolved_local is None else 'base'} is missing from the "
                "store. Reporting the headline yield without it would be exactly the "
                "error the test exists to prevent.",
            )

        differential = resolved_local - resolved_base
        hedged = nominal_yield - differential
        base_real = None if base_cpi is None else hedged - base_cpi
        excess = hedged - resolved_base
        legs = {"differential": differential, "hedged": hedged, "base_real": base_real}

        if excess <= self._carry_tolerance:
            return verdict(
                VERDICT_ILLUSION,
                f"a {nominal_yield:.1%} nominal yield hedges back to {hedged:.1%}, which "
                f"clears the {self._base_currency} base rate of {resolved_base:.1%} by "
                f"{excess:.1%} — inside the {self._carry_tolerance:.1%} tolerance, so it "
                "does not clear it at all. Note what that pickup IS: under CIP the base "
                f"rate cancels, so it is exactly this bond's {excess:.1%} over its OWN "
                f"local rate of {resolved_local:.1%}. The headline yield is compensation "
                "for expected depreciation, not an opportunity. Taking it requires an "
                "explicit view that depreciation will undershoot the forward, stated as "
                "such.",
                **legs,
            )
        if base_real is not None and base_real <= 0:
            return verdict(
                VERDICT_ILLUSION,
                f"the hedged equivalent of {hedged:.1%} is {base_real:.1%} in real "
                f"{self._base_currency} terms after {base_cpi:.1%} base inflation. A "
                "negative real return is not carry.",
                **legs,
            )
        return verdict(
            VERDICT_OPPORTUNITY,
            f"the hedged equivalent of {hedged:.1%} clears the {resolved_base:.1%} base rate "
            f"by {excess:.1%}, beyond the {self._carry_tolerance:.1%} tolerance — which under "
            f"CIP is the same statement as this bond yielding {excess:.1%} over its own local "
            f"rate of {resolved_local:.1%}, since the base rate cancels. The pickup is term "
            "or credit premium, not carry, and it has to be worth taking on those terms. The "
            "currency exposure is carried knowingly and unhedged: hedging is a yardstick "
            "here, never a recommendation.",
            **legs,
        )

    @property
    def recommends_hedging(self) -> bool:
        """Always false. Present so a caller can assert it rather than assume it."""
        return self._require_hedging

    @property
    def flagged_market_rule(self) -> str:
        return self._flagged_rule

    # ── persistence ──

    def write(self, results: Sequence[FourWay], *, as_of: date) -> int:
        rows = [
            (
                iso_date(as_of),
                row.market,
                row.horizon_days,
                row.currency,
                row.local_nominal,
                row.local_real,
                row.base_nominal,
                row.base_real,
                row.cpi_yoy,
                row.base_cpi_yoy,
                row.fx_return,
                row.depreciation_annualised,
                int(row.high_inflation),
                int(row.high_depreciation),
                row.status,
                row.reason,
                utc_stamp(),
            )
            for row in results
        ]
        if not rows:
            return 0
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO fx_state (
                    as_of, market, horizon_days, currency, local_nominal, local_real,
                    base_nominal, base_real, cpi_yoy, base_cpi_yoy, fx_return,
                    depreciation_annualised, high_inflation, high_depreciation, status,
                    reason, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (as_of, market, horizon_days) DO UPDATE SET
                    currency = excluded.currency,
                    local_nominal = excluded.local_nominal,
                    local_real = excluded.local_real,
                    base_nominal = excluded.base_nominal,
                    base_real = excluded.base_real,
                    cpi_yoy = excluded.cpi_yoy,
                    base_cpi_yoy = excluded.base_cpi_yoy,
                    fx_return = excluded.fx_return,
                    depreciation_annualised = excluded.depreciation_annualised,
                    high_inflation = excluded.high_inflation,
                    high_depreciation = excluded.high_depreciation,
                    status = excluded.status,
                    reason = excluded.reason,
                    computed_at = excluded.computed_at
                """,
                rows,
            )
        return len(rows)

    def write_carry_test(self, test: CarryTest) -> None:
        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO carry_tests (
                    as_of, currency, nominal_yield, local_rate, base_rate, rate_differential,
                    hedged_equivalent, base_real_yield, verdict, reason, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (as_of, currency) DO UPDATE SET
                    nominal_yield = excluded.nominal_yield,
                    local_rate = excluded.local_rate,
                    base_rate = excluded.base_rate,
                    rate_differential = excluded.rate_differential,
                    hedged_equivalent = excluded.hedged_equivalent,
                    base_real_yield = excluded.base_real_yield,
                    verdict = excluded.verdict,
                    reason = excluded.reason,
                    computed_at = excluded.computed_at
                """,
                (
                    iso_date(test.as_of),
                    test.currency,
                    test.nominal_yield,
                    test.local_rate,
                    test.base_rate,
                    test.rate_differential,
                    test.hedged_equivalent,
                    test.base_real_yield,
                    test.verdict,
                    test.reason,
                    utc_stamp(),
                ),
            )

    # ── internals ──

    def _absent(
        self, market: str, currency: str, horizon_days: int, as_of: date, reason: str
    ) -> FourWay:
        return FourWay(
            market=market,
            currency=currency,
            horizon_days=horizon_days,
            as_of=as_of,
            status=STATUS_NA,
            reason=reason,
        )

    def _missing_legs(
        self,
        local_nominal: float | None,
        local_real: float | None,
        base_nominal: float | None,
        base_real: float | None,
        spec: dict[str, Any],
    ) -> list[str]:
        if not self._require_four_way:
            return [] if local_nominal is not None else ["no local return"]
        missing: list[str] = []
        if local_nominal is None:
            missing.append("no local nominal return")
        if local_real is None:
            missing.append(
                f"no local real return: CPI series {spec.get('cpi_series') or '(undeclared)'} "
                "is absent from the store"
            )
        if base_nominal is None:
            missing.append(
                f"no base nominal return: FX series {spec.get('fx_series') or '(undeclared)'} "
                "is absent from the store"
            )
        if base_real is None:
            missing.append("no base real return, which is the only rankable figure")
        return missing

    def _series(self, series_id: str | None, *, since: date | None = None) -> MacroSeries | None:
        if not series_id:
            return None
        series = macro_series(self._store, str(series_id), since=since)
        return series if len(series) else None

    def _cpi_yoy(self, spec: dict[str, Any], as_of: date) -> float | None:
        return cpi_yoy(
            self._store,
            spec,
            as_of=as_of,
            calendar_days_per_year=self._calendar_days_per_year,
        )

    def _base_cpi_yoy(self, as_of: date) -> float | None:
        if self._base_market is None:
            return None
        return self._cpi_yoy(self._markets[self._base_market], as_of)

    def _fx_ratio(self, spec: dict[str, Any], *, as_of: date, horizon_days: int) -> float | None:
        return fx_ratio(
            self._store,
            spec,
            as_of=as_of,
            horizon_days=horizon_days,
            base_currency=self._base_currency,
        )

    def _trailing_depreciation(self, spec: dict[str, Any], *, as_of: date) -> float | None:
        """Annualised move of the local currency against the base, over the window.

        Negative is depreciation, which is the direction the flag fires on.
        """
        window_days = round(self._depreciation_years * self._calendar_days_per_year)
        ratio = self._fx_ratio(spec, as_of=as_of, horizon_days=window_days)
        if ratio is None:
            return None
        return annualised_from_period(ratio - 1.0, years=float(self._depreciation_years))

    def _policy_rate(self, market: str | None, as_of: date) -> float | None:
        if market is None:
            return None
        series = self._series(self._markets[market].get("policy_rate_series"))
        if series is None:
            return None
        latest = series.known_at(as_of)
        return None if latest is None else latest.value / PERCENT_TO_DECIMAL

    def _market_for_currency(self, currency: str) -> str | None:
        for name, spec in self._markets.items():
            if str(spec.get("currency", "")).upper() == currency.upper():
                return name
        return None


#: Two endpoints, the arithmetic minimum for a return.
_MIN_ENDPOINTS = 2


def _deflate(
    nominal: float | None, cpi_yoy: float | None, horizon_days: int, days_per_year: float
) -> float | None:
    """Take a nominal return to a real one over the same horizon.

    The CPI figure is annual, so it is compounded down to the horizon rather than
    subtracted whole: subtracting a year of inflation from a five-day return
    would make every short-horizon real return absurdly negative.
    """
    if nominal is None or cpi_yoy is None or days_per_year <= 0:
        return None
    horizon_inflation = (1.0 + cpi_yoy) ** (horizon_days / days_per_year) - 1.0
    if horizon_inflation <= -1.0:
        return None
    return (1.0 + nominal) / (1.0 + horizon_inflation) - 1.0
