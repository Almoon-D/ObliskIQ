"""A toy cross-section shared by the harness tests: four served members with designed momentum, a
fifth the vendor does not serve, a development and a confirmation sample, the universe, the top
group, the top group with a stop and a lagged placebo.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
from datetime import date

from research_toy import NOW

from obliskiq.domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from obliskiq.store import Store
from obliskiq.store.research import replace_research_series


def month_end_weekday(year: int, month: int) -> date:
    day = date(year, month, calendar.monthrange(year, month)[1])
    while day.weekday() >= 5:
        day = date.fromordinal(day.toordinal() - 1)
    return day


#: Every month end from October 2021 to December 2024.
MONTHS = [month_end_weekday(2021 + (9 + k) // 12, (9 + k) % 12 + 1) for k in range(39)]
#: Monthly growth by member: A rises fastest, B slowly, C falls, D alternates.
GROWTH = {"A": 0.03, "B": 0.01, "C": -0.01}


def member_prices(key: str) -> list[float]:
    level, out = 100.0, []
    for k in range(len(MONTHS)):
        out.append(round(level, 6))
        rate = GROWTH.get(key, 0.06 if k % 2 == 0 else -0.05)
        level *= 1 + rate
    return out


def cross_series(key: str) -> ResearchSeries:
    return ResearchSeries(
        series_id=f"cross.{key.lower()}", dataset_id="cross", vendor=Vendor.MSCI,
        field=key.lower(), units="index level", currency="EUR", frequency=Frequency.DAILY,
        point_in_time=PointInTime.AS_OBSERVED, source_url="test fixture", downloaded_at=NOW,
        content_sha256=key.lower() * 64, bias_notes=("A toy index written by the test.",),
    )  # fmt: skip


def put_cross_toy(store: Store, *, extra: dict[str, list[Observation]] | None = None) -> None:
    for key in ("A", "B", "C", "D"):
        rows = [
            Observation(observed_on=d, available_on=d, value=p)
            for d, p in zip(MONTHS, member_prices(key), strict=True)
        ]
        replace_research_series(store, cross_series(key), [*rows, *(extra or {}).get(key, [])])


MEMBER = """    - key: {key}
      name: Toy index {key}
      price_series: {series}
      spans: {spans}
"""

MEMBERS = (
    MEMBER.format(key="A", series="cross.a", spans="[[2021-01-01, null]]")
    + MEMBER.format(key="B", series="cross.b", spans="[[2021-01-01, null]]")
    + MEMBER.format(key="C", series="cross.c", spans="[[2021-01-01, null]]")
    + MEMBER.format(key="D", series="cross.d", spans="[[2021-01-01, null]]")
    + MEMBER.format(key="X", series="null", spans="[[2021-01-01, 2022-03-31]]")
)

CROSS = f"""---
id: toy_cross
title: A toy country momentum
kind: cross_section
mechanism: A test of the harness. Members that rose keep rising.
citation: none; a test of the harness
universe: four toy indices and one the vendor does not serve
data_status: as observed, written by the test
primary_metric: top_group_excess
alpha: null
target_power: null
tests_in_confirmation: 2
adjustment: Bonferroni over two tests; the deflated Sharpe over the registry
trial_budget: 4
study:
  development:
    start: 2022-01-01
    end: 2022-12-31
  confirmation:
    start: 2023-01-01
    end: 2024-12-31
  calendar: A
  initial_capital: "100000"
  fx_dataset: null
  vehicle:
    currency: EUR
    venue: XETR
    asset_class: etf
    market_cap_usd: "1000000000000"
    expense_ratio: null
    kind: etf
    issuer: Toy issuer
  members:
{MEMBERS}  signal:
    lookback_months: 2
    skip_months: 1
  selection:
    enter_fraction: "0.5"
    keep_fraction: "0.75"
    reset_month: 12
  stop:
    volatility_sessions: 4
    horizon_sessions: 1
    multiple: "1"
  portfolios:
    universe:
      rule: universe
      lag_months: 0
      stop: false
    momentum:
      rule: top_group
      lag_months: 0
      stop: false
    momentum_stop:
      rule: top_group
      lag_months: 0
      stop: true
    placebo:
      rule: top_group
      lag_months: 3
      stop: false
tests:
  - name: country momentum
    candidate: momentum
    against: universe
    theta: "0.02"
    theta_justification: a test value
    sd_per_period: "0.015"
  - name: stop
    candidate: momentum_stop
    against: momentum
    theta: "0.01"
    theta_justification: a test value
    sd_per_period: "0.008"
placebo:
  candidate: placebo
  against: universe
  test: country momentum
bootstrap:
  n_boot: 50
  block_months: 3
  seed: 11
newey_west_lags: 3
periods_per_year: 12
subperiods:
  - [2023-01-01, 2023-12-31]
  - [2024-01-01, 2024-12-31]
cost_uncertainty_multiplier: "2"
robustness: the sign in each year, the break-even cost multiplier, a lagged placebo
adequacy:
  min_coverage: "1"
  max_lost_share: "0.25"
  point_in_time: [as_observed]
  min_effective_observations: 20
  min_development_observations: 10
---

# A toy country momentum

A test fixture, never evidence.
"""
