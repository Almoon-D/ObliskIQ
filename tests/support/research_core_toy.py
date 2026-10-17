"""A toy core study shared by the harness tests: an equity, a bond and a cash role on Xetra, a
reference and four candidates (fixed weights, a banded 60/40, a two-role risk balance and the
equity overlay with the tilt and swap partners), a frontier, an aspiration and a structure check
on a toy monthly market.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
import math
from datetime import UTC, date, datetime

from obliskiq.domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from obliskiq.store import Store
from obliskiq.store.research import replace_research_series

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def month_ends(first: date, count: int) -> list[date]:
    out = []
    year, month = first.year, first.month
    for _ in range(count):
        out.append(date(year, month, calendar.monthrange(year, month)[1]))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


#: Month ends from January 2022 to December 2025; the study starts in July 2022.
MONTHS = month_ends(date(2022, 1, 31), 48)
#: Equity: a rise, a fall of about 35 % through 2023, then a recovery.
EQUITY = [100.0 * math.exp(0.02 * k) for k in range(18)]
EQUITY += [EQUITY[-1] * math.exp(-0.04 * k) for k in range(1, 11)]
EQUITY += [EQUITY[-1] * math.exp(0.03 * k) for k in range(1, 21)]
BOND = [100.0 * math.exp(0.002 * k) for k in range(48)]
CASH = [100.0 * math.exp(0.001 * k) for k in range(48)]
#: A toy US market for the structure check: monthly returns, one of them a crash.
MARKET_MONTHS = month_ends(date(1990, 1, 31), 120)
MARKET = [0.01 if k % 12 else -0.05 for k in range(120)]
MARKET[60:66] = [-0.08] * 6
BILL = [0.003] * 120

INSTRUMENT = """    - key: {key}
      price_series: core.{series}
      income_series: null
      currency: EUR
      venue: XETR
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "1000000000000"
      expense_ratio: {ter}
      security:
        kind: etf
        isin: null
        issuer: {issuer}
        tracked_index: {index}
        listed: true
"""


def _instrument(key: str, series: str, issuer: str, index: str, ter: str = "null") -> str:
    return INSTRUMENT.format(key=key, series=series, issuer=issuer, index=index, ter=ter)


INSTRUMENTS = "".join(
    (
        _instrument("EQ", "equity", "Issuer one", "Equity index", '"0.002"'),
        _instrument("EQ_SWAP", "equity", "Issuer two", "Another equity index", '"0.002"'),
        _instrument("BD", "bond", "Issuer one", "Bond index"),
        _instrument("CASH", "cash", "Issuer one", "Cash index"),
        _instrument("CASH_SWAP", "cash", "Issuer two", "Another cash index"),
    )
)

CORE_TOY = f"""---
id: core_toy
title: A toy core against an equity reference
kind: portfolio_comparison
mechanism: A test of the harness's core strategies.
citation: none; a test of the harness
universe: three toy roles on Xetra
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
  start: 2022-07-01
  end: 2025-12-31
  rebalance: month_end
  calendar: EQ
  initial_capital: "100000"
  fx_dataset: null
  instruments:
{INSTRUMENTS}  roles:
    EQ:
      primary: EQ
      partner: EQ_SWAP
    BD:
      primary: BD
      partner: null
    CASH:
      primary: CASH
      partner: CASH_SWAP
  core:
    cash_role: CASH
    environments:
      growth_up: [EQ]
      growth_down: [BD]
    risk:
      volatilities: {{EQ: "0.16", BD: "0.05"}}
      correlations:
        - [EQ, BD, "0"]
    bands:
      absolute: "0.05"
      relative: "0.25"
    tilt:
      momentum_months: 3
      average_months: 2
      hysteresis: "0.02"
      floor: "0.25"
    tax_aware:
      benefit_fraction: "0.05"
  reference:
    name: equity
    strategy: buy_and_hold
    weights: {{EQ: "1"}}
    simplicity: 1
  candidates:
    - name: fixed
      strategy: fixed_weights
      weights: {{EQ: "0.5", BD: "0.5"}}
      simplicity: 2
    - name: 60/40
      strategy: banded_weights
      weights: {{EQ: "0.6", BD: "0.4"}}
      simplicity: 2
    - name: balance
      strategy: risk_balance
      budgets: {{growth_up: "0.5", growth_down: "0.5"}}
      tilt: false
      simplicity: 3
    - name: overlay
      strategy: equity_overlay
      equity: EQ
      fraction: "1"
      tilt: true
      simplicity: 4
bootstrap:
  n_boot: 30
  block_months: 3
  seed: 11
drawdown_limits: ["0.1", "0.3"]
frontier:
  breach_probabilities: ["0.05", "0.5"]
aspiration: ["0.3"]
subperiods:
  - [2022-07-01, 2023-12-31]
  - [2024-01-01, 2025-12-31]
cost_uncertainty_multiplier: "2"
robustness: the sign in each subperiod and the break-even cost multiplier
adequacy:
  min_coverage: "1"
  point_in_time: [as_observed]
  min_effective_observations: 30
power:
  sd_per_period: "0.03"
  by_candidate:
    overlay: "0.02"
  periods: 41
  periods_per_year: 12
  names_per_period: 1
  rho: "0"
structure:
  market_series: [mkt.Mkt-RF, mkt.RF]
  cash_series: mkt.RF
  start: 1990-01-01
  end: 1999-12-31
  cost_per_leg: "0.003"
  point_in_time: [release_lag]
  subperiods:
    - [1990-01-01, 1994-12-31]
    - [1995-01-01, 1999-12-31]
---

# A toy core against an equity reference

A test fixture, never evidence.
"""


def toy_series(series_id: str, *, kind: PointInTime = PointInTime.AS_OBSERVED) -> ResearchSeries:
    return ResearchSeries(
        series_id=series_id, dataset_id=series_id.split(".")[0], vendor=Vendor.YAHOO,
        field=series_id.split(".")[1], units="price", currency="EUR", frequency=Frequency.DAILY,
        point_in_time=kind, source_url="test fixture", downloaded_at=NOW,
        content_sha256=format(abs(hash(series_id)) % 16**8, "08x") * 8,
        bias_notes=("A toy series written by the test.",),
    )  # fmt: skip


def rows(days: list[date], values: list[float]) -> list[Observation]:
    return [
        Observation(observed_on=d, available_on=d, value=v)
        for d, v in zip(days, values, strict=True)
    ]


def put_core_toy(store: Store, *, extra: dict[str, list[Observation]] | None = None) -> None:
    """The toy's prices and the structure check's market, each as observed (the market as a
    zero-lag release, as the shipped ``ff3_market`` dataset)."""
    added = extra or {}
    for name, values in (("core.equity", EQUITY), ("core.bond", BOND), ("core.cash", CASH)):
        replace_research_series(
            store, toy_series(name), [*rows(MONTHS, values), *added.get(name, [])]
        )
    lagged = PointInTime.RELEASE_LAG
    replace_research_series(
        store, toy_series("mkt.Mkt-RF", kind=lagged),
        rows(MARKET_MONTHS, [m - b for m, b in zip(MARKET, BILL, strict=True)]),
    )  # fmt: skip
    replace_research_series(store, toy_series("mkt.RF", kind=lagged), rows(MARKET_MONTHS, BILL))
