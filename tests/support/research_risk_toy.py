"""A toy total-risk study shared by the harness tests: daily prices of an equity, a bond, a cash
and a USD gold role on weekdays from 2019 to 2023 (a crash in the spring of 2020), a static risk
balance as the reference, two total-risk candidates and their declared twins, comparisons, daily
marks, the owner's point, a block-length sensitivity, the forecast check, a daily and a monthly
long history and a development window in 2019.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
from datetime import UTC, date, datetime, timedelta

import numpy as np

from obliskiq.domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from obliskiq.store import Store
from obliskiq.store.research import replace_research_series

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def weekdays(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


DAYS = weekdays(date(2019, 1, 1), date(2023, 12, 29))
CRASH = (date(2020, 2, 20), date(2020, 4, 30))


def _paths() -> dict[str, list[float]]:
    rng = np.random.default_rng(20261018)
    count = len(DAYS)
    crash = np.array([CRASH[0] <= d <= CRASH[1] for d in DAYS])
    equity = np.where(crash, rng.normal(-0.003, 0.03, count), rng.normal(0.0004, 0.007, count))
    bond = rng.normal(0.0001, 0.002, count)
    gold = rng.normal(0.0002, 0.009, count)
    fx = rng.normal(0.0, 0.004, count)
    cash = np.full(count, 0.00005)

    def level(changes: np.ndarray, first: float) -> list[float]:
        return (first * np.exp(np.cumsum(changes))).tolist()

    return {
        "risk.equity": level(equity, 100.0),
        "risk.bond": level(bond, 100.0),
        "risk.gold": level(gold, 50.0),
        "risk.cash": level(cash, 100.0),
        "fx.USD": level(fx, 1.1),
    }


PATHS = _paths()


def month_ends(first: date, count: int) -> list[date]:
    out = []
    year, month = first.year, first.month
    for _ in range(count):
        out.append(date(year, month, calendar.monthrange(year, month)[1]))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


#: A monthly history from 2000: an excess return dated on the last weekday, a bill on the
#: calendar month end (joined by month, as AQR's and Ken French's are).
HISTORY_MONTHS = month_ends(date(2000, 1, 31), 288)
_MONTHLY = np.random.default_rng(7).normal(0.004, 0.045, len(HISTORY_MONTHS))
_MONTHLY[100:106] = -0.12
EXCESS = _MONTHLY.tolist()
BILL = [0.002] * len(HISTORY_MONTHS)


def last_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


INSTRUMENT = """    - key: {key}
      price_series: {series}
      income_series: null
      currency: {currency}
      venue: {venue}
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "1000000000000"
      expense_ratio: null
      security:
        kind: etf
        isin: null
        issuer: {issuer}
        tracked_index: {index}
        listed: true
"""


def _instrument(key: str, series: str, issuer: str, index: str, currency: str = "EUR") -> str:
    venue = "XETR" if currency == "EUR" else "XLON"
    return INSTRUMENT.format(
        key=key, series=series, issuer=issuer, index=index, currency=currency, venue=venue
    )


INSTRUMENTS = "".join(
    (
        _instrument("EQ", "risk.equity", "Issuer one", "Equity index"),
        _instrument("EQ_SWAP", "risk.equity", "Issuer two", "Another equity index"),
        _instrument("BD", "risk.bond", "Issuer one", "Bond index"),
        _instrument("BD_SWAP", "risk.bond", "Issuer two", "Another bond index"),
        _instrument("GD", "risk.gold", "Issuer one", "Gold", currency="USD"),
        _instrument("CASH", "risk.cash", "Issuer one", "Cash index"),
        _instrument("CASH_SWAP", "risk.cash", "Issuer two", "Another cash index"),
    )
)

RISK_TOY = f"""---
id: risk_toy
title: A toy total-risk study against a static risk balance
kind: portfolio_comparison
mechanism: A test of the harness's total-risk strategies.
citation: none; a test of the harness
universe: four toy roles
data_status: as observed, written by the test
primary_metric: log_growth_difference
theta: "0.01"
theta_justification: a test value
alpha: null
target_power: null
tests_in_confirmation: 4
adjustment: Bonferroni over four tests
trial_budget: 8
study:
  start: 2020-01-01
  end: 2023-12-31
  rebalance: month_end
  calendar: EQ
  initial_capital: "100000"
  fx_dataset: fx
  instruments:
{INSTRUMENTS}  roles:
    EQ:
      primary: EQ
      partner: EQ_SWAP
    BD:
      primary: BD
      partner: BD_SWAP
    GD:
      primary: GD
      partner: null
    CASH:
      primary: CASH
      partner: CASH_SWAP
  core:
    cash_role: CASH
    environments:
      growth_up: [EQ]
      growth_down: [BD]
      inflation_up: [GD]
    risk:
      volatilities: {{EQ: "0.16", BD: "0.04", GD: "0.15"}}
      correlations:
        - [EQ, BD, "0"]
        - [EQ, GD, "0.2"]
        - [BD, GD, "0.1"]
    bands:
      absolute: "0.05"
      relative: "0.25"
    tax_aware:
      benefit_fraction: "0.05"
    scaling:
      estimation_start: 2019-01-01
      periods_per_year: 261
      estimators:
        fast:
          half_life_days: "10"
          window_days: null
          ewma_share: "1"
          shrinkage: none
          prior_observations: "5"
        blend:
          half_life_days: "10"
          window_days: 63
          ewma_share: "0.5"
          shrinkage: ledoit_wolf
          prior_observations: "21"
  reference:
    name: A
    strategy: risk_balance
    budgets: {{growth_up: "0.34", growth_down: "0.33", inflation_up: "0.33"}}
    tilt: false
    simplicity: 3
  candidates:
    - name: S1
      strategy: total_risk
      base: {{EQ: "1"}}
      target_volatility: "0.10"
      floor: "0.2"
      max_change: "0.5"
      estimator: fast
      tax_aware: true
      hold_costly_reductions: false
      simplicity: 6
    - name: S2
      strategy: total_risk
      base: {{EQ: "0.6", BD: "0.4"}}
      target_volatility: "0.08"
      floor: "0.2"
      max_change: "0.25"
      estimator: blend
      tax_aware: true
      hold_costly_reductions: true
      simplicity: 6
  twins:
    - name: T1
      strategy: total_risk
      base: {{EQ: "1"}}
      target_volatility: "0.10"
      floor: "0.2"
      max_change: "0.5"
      estimator: declared
      tax_aware: true
      hold_costly_reductions: false
      simplicity: 5
    - name: T2
      strategy: total_risk
      base: {{EQ: "0.6", BD: "0.4"}}
      target_volatility: "0.08"
      floor: "0.2"
      max_change: "0.25"
      estimator: declared
      tax_aware: true
      hold_costly_reductions: true
      simplicity: 5
comparisons:
  - name: S1 against T1
    candidate: S1
    against: T1
    theta: "0.005"
    theta_justification: a test value
    sd_per_period: "0.01"
  - name: S2 against T2
    candidate: S2
    against: T2
    theta: "0.005"
    theta_justification: a test value
    sd_per_period: "0.01"
bootstrap:
  n_boot: 12
  block_months: 6
  seed: 5
  sensitivity:
    block_months: [4, 6, 9]
    n_boot: 6
drawdown_limits: ["0.1", "0.3"]
frontier:
  breach_probabilities: ["0.05", "0.5"]
aspiration: null
subperiods:
  - [2020-01-01, 2021-12-31]
  - [2022-01-01, 2023-12-31]
cost_uncertainty_multiplier: "2"
robustness: the sign in each subperiod and the break-even cost multiplier
adequacy:
  min_coverage: "1"
  point_in_time: [as_observed]
  min_effective_observations: 40
power:
  sd_per_period: "0.03"
  by_candidate: null
  periods: 47
  periods_per_year: 12
  names_per_period: 1
  rho: "0"
structure: null
marks: daily
owner_point:
  limit: "0.3"
  probability: "0.05"
  horizon_years: 3
breach_horizons_years: [1, 2, 3]
forecast_check:
  loss: qlike
development:
  start: 2019-02-01
  end: 2019-12-31
long_history:
  - name: the toy in EUR
    frequency: daily
    periods_per_year: 261
    currency: EUR
    data_start: 2019-01-01
    start: 2019-07-01
    end: 2023-12-31
    roles:
      EQ: {{kind: level, series: [risk.equity]}}
      BD: {{kind: level, series: [risk.bond]}}
    cash: {{kind: level, series: [risk.cash]}}
    point_in_time: [as_observed]
    cost_per_leg: "0.001"
    block_months: 6
    n_boot: 4
    biases: [the toy's own series]
  - name: a toy century
    frequency: monthly
    periods_per_year: 12
    currency: local excess returns plus a bill
    data_start: 2000-01-01
    start: 2002-01-01
    end: 2023-12-31
    roles:
      EQ: {{kind: returns, series: [century.Market, century.RF]}}
    cash: {{kind: returns, series: [century.RF]}}
    point_in_time: [release_lag]
    cost_per_leg: "0.002"
    block_months: 12
    n_boot: 4
    biases: [monthly toy returns]
---

# A toy total-risk study

A test fixture, never evidence.
"""


def toy_series(series_id: str, *, kind: PointInTime = PointInTime.AS_OBSERVED) -> ResearchSeries:
    currency = "USD" if series_id in ("risk.gold", "fx.USD") else "EUR"
    return ResearchSeries(
        series_id=series_id, dataset_id=series_id.split(".")[0], vendor=Vendor.YAHOO,
        field=series_id.split(".")[1], units="price", currency=currency,
        frequency=Frequency.DAILY, point_in_time=kind, source_url="test fixture",
        downloaded_at=NOW, content_sha256=format(abs(hash(series_id)) % 16**8, "08x") * 8,
        bias_notes=("A toy series written by the test.",),
    )  # fmt: skip


def rows(days: list[date], values: list[float]) -> list[Observation]:
    return [
        Observation(observed_on=d, available_on=d, value=v)
        for d, v in zip(days, values, strict=True)
    ]


def put_risk_toy(store: Store, *, paths: dict[str, list[float]] | None = None) -> None:
    """The toy's daily prices and rates, and the monthly century (a zero-lag release)."""
    for name, values in (paths or PATHS).items():
        replace_research_series(store, toy_series(name), rows(DAYS, values))
    lagged = PointInTime.RELEASE_LAG
    replace_research_series(
        store, toy_series("century.Market", kind=lagged),
        rows([last_weekday(d) for d in HISTORY_MONTHS], EXCESS),
    )  # fmt: skip
    replace_research_series(
        store, toy_series("century.RF", kind=lagged), rows(HISTORY_MONTHS, BILL)
    )
