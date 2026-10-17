"""A toy research study shared by the harness tests: two ETFs on Xetra, a rebalanced and a held
portfolio, a pre-registration text and a git repository to merge it in.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from obliskiq.domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from obliskiq.store import Store
from obliskiq.store.research import replace_research_series

DATES = [
    date(2024, 1, 31),
    date(2024, 7, 31),
    date(2025, 1, 31),
    date(2025, 7, 31),
    date(2025, 12, 31),
]
A_PRICES = [100.0, 150.0, 150.0, 150.0, 150.0]
B_PRICES = [100.0] * 5
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

INSTRUMENT = """    - key: {key}
      price_series: toy.{lower}_close
      income_series: null
      currency: EUR
      venue: XETR
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "1000000000000"
      security:
        kind: etf
        isin: null
        issuer: Toy {key}
        tracked_index: Index {key}
        listed: true
"""

TOY = f"""---
id: toy_rotation
title: A toy rotation against holding
kind: portfolio_comparison
mechanism: A test of the harness. Rebalancing sells the winner and pays its tax early.
citation: none; a test of the harness
universe: two toy ETFs on Xetra
data_status: as observed, written by the test
primary_metric: log_growth_difference
theta: "0.005"
theta_justification: a test value
alpha: null
target_power: null
tests_in_confirmation: 1
adjustment: Bonferroni over one test; the deflated Sharpe over the registry
trial_budget: 2
study:
  start: 2024-01-01
  end: 2025-12-31
  rebalance: month_end
  calendar: A
  initial_capital: "10000"
  fx_dataset: null
  instruments:
{INSTRUMENT.format(key="A", lower="a")}{INSTRUMENT.format(key="B", lower="b")}  candidate:
    name: rebalanced
    strategy: fixed_weights
    weights: {{A: "0.5", B: "0.5"}}
  reference:
    name: held
    strategy: buy_and_hold
    weights: {{A: "0.5", B: "0.5"}}
bootstrap:
  n_boot: 20
  block_months: 2
  seed: 7
drawdown_limits: ["0.2"]
subperiods:
  - [2024-01-01, 2024-12-31]
  - [2025-01-01, 2025-12-31]
cost_uncertainty_multiplier: "2"
robustness: the sign in each year and the break-even cost multiplier
adequacy:
  min_coverage: "1"
  point_in_time: [as_observed]
  min_effective_observations: 4
power:
  sd_per_period: "0.02"
  periods: 4
  periods_per_year: 12
  names_per_period: 1
  rho: "0"
---

# A toy rotation against holding

A test fixture, never evidence.
"""

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.org",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.org",
}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env={**os.environ, **GIT_IDENTITY},
    )
    return done.stdout.decode().strip()


def toy_series(series_id: str) -> ResearchSeries:
    return ResearchSeries(
        series_id=series_id, dataset_id="toy", vendor=Vendor.YAHOO, field=series_id[4:],
        units="price", currency="EUR", frequency=Frequency.DAILY,
        point_in_time=PointInTime.AS_OBSERVED, source_url="test fixture", downloaded_at=NOW,
        content_sha256=("a" if series_id.endswith("a_close") else "b") * 64,
        bias_notes=("A toy series written by the test.",),
    )  # fmt: skip


def put_toy(store: Store, *, extra: list[Observation] | None = None) -> None:
    for name, prices in (("toy.a_close", A_PRICES), ("toy.b_close", B_PRICES)):
        rows = [
            Observation(observed_on=d, available_on=d, value=p)
            for d, p in zip(DATES, prices, strict=True)
        ]
        replace_research_series(store, toy_series(name), [*rows, *(extra or [])])
