"""research/forecast.py and research/long_history.py: QLIKE and realised variance by hand, the
forecast check on a path whose volatility changes, a history's alignment (levels within their
age, returns joined by month), the rule run on a history by hand, and the check's refusals.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest

from obliskiq.domain.research import Observation
from obliskiq.errors import SimulationError
from obliskiq.quant.covariance import EstimatorRule
from obliskiq.research.daily import DailyPath
from obliskiq.research.decide import AdequacyCheck
from obliskiq.research.forecast import forecast_check, qlike, realised_variances
from obliskiq.research.long_history import (
    HistoryData,
    _Path,
    history_data,
    long_history_check,
    run_rule,
)
from obliskiq.research.prereg import LongHistorySpec
from obliskiq.research.strategies import Bands, TotalRisk

D = Decimal
FAST = EstimatorRule(half_life=3.0, window=None, ewma_share=1.0, shrinkage=False,
                     prior_observations=1.0)  # fmt: skip
DATES = tuple(date(2024, m, 28) for m in range(1, 13))


def alternating(size: float, count: int) -> np.ndarray:
    return np.array([size if i % 2 else -size for i in range(count)])


def path(sizes: list[float], days: int = 20) -> DailyPath:
    months = [alternating(size, days) for size in sizes]
    returns = np.concatenate([alternating(sizes[0], days), *months]).reshape(-1, 1)
    ends = tuple(days * (k + 1) for k in range(len(sizes) + 1))
    return DailyPath(("EQ",), DATES[: len(sizes) + 1], returns, ends)


# ── the forecast check ──


def test_qlike_is_zero_for_a_perfect_forecast_and_grows_either_way():
    assert qlike(np.array([1.0]), np.array([1.0])).tolist() == [0.0]
    assert qlike(np.array([2.0]), np.array([1.0]))[0] == pytest.approx(1 - math.log(2))
    assert qlike(np.array([1.0]), np.array([2.0]))[0] == pytest.approx(math.log(2) - 0.5)
    with pytest.raises(ValueError, match="positive"):
        qlike(np.array([0.0]), np.array([1.0]))


def test_the_realised_variance_is_a_years_worth_of_the_months_squared_returns():
    found = realised_variances(path([0.01, 0.02]), ["EQ"], np.array([1.0]), 250)
    assert found.tolist() == pytest.approx([250 * 1e-4, 250 * 4e-4])


def test_an_estimator_that_follows_volatility_beats_the_constant_forecast():
    regime = path([0.01] * 4 + [0.03] * 6)
    check = forecast_check(
        name="S", estimator="fast", rule=FAST, declared=np.array([[0.0625]]),
        weights=np.array([1.0]), keys=["EQ"], path=regime, periods_per_year=250, block=2,
        n_boot=50, seed=1, alpha=0.05,
    )  # fmt: skip
    assert check.months == 10
    assert check.loss == "qlike"
    assert check.difference < 0
    assert check.mean_loss < check.mean_declared
    assert check.interval[0] <= check.difference <= check.interval[1]
    assert 0 < check.share_better <= 1


def test_the_forecast_check_refuses_what_it_cannot_score():
    common = {
        "name": "S", "estimator": "fast", "declared": np.array([[0.04]]),
        "weights": np.array([1.0]), "keys": ["EQ"], "periods_per_year": 250, "block": 2,
        "n_boot": 5, "seed": 1, "alpha": 0.05,
    }  # fmt: skip
    gap = path([0.01, 0.01])
    gap.returns[0, 0] = np.nan
    with pytest.raises(SimulationError, match="lack a daily price"):
        forecast_check(rule=FAST, path=gap, **common)
    empty = DailyPath(("EQ",), DATES[:2], alternating(0.01, 20).reshape(-1, 1), (0, 20))
    bare = EstimatorRule(half_life=3.0, window=None, ewma_share=1.0, shrinkage=False,
                         prior_observations=0.0)  # fmt: skip
    with pytest.raises(SimulationError, match="nothing to forecast with"):
        forecast_check(rule=bare, path=empty, **common)
    still = DailyPath(("EQ",), DATES[:2], np.zeros((40, 1)), (20, 40))
    with pytest.raises(SimulationError, match="never moved"):
        forecast_check(rule=FAST, path=still, **common)


# ── the long history ──


def history(**overrides: object) -> LongHistorySpec:
    fields: dict[str, object] = {
        "name": "toy",
        "frequency": "monthly",
        "periods_per_year": 12,
        "currency": "USD",
        "data_start": date(2000, 1, 1),
        "start": date(2000, 3, 1),
        "end": date(2001, 12, 31),
        "roles": {"EQ": {"kind": "returns", "series": ["x.mkt", "x.rf"]}},
        "cash": {"kind": "returns", "series": ["x.rf"]},
        "point_in_time": ["release_lag"],
        "cost_per_leg": "0.001",
        "block_months": 3,
        "n_boot": 3,
        "biases": ["toy"],
    }
    fields.update(overrides)
    return LongHistorySpec.model_validate(fields)


def obs(day: date, value: float) -> Observation:
    return Observation(observed_on=day, available_on=day, value=value)


MONTHS = [date(2000 + (m // 12), m % 12 + 1, 28) for m in range(24)]


def test_return_series_are_joined_by_month_and_summed():
    rows = {
        "x.mkt": [obs(day, 0.01 * (k % 3 - 1)) for k, day in enumerate(MONTHS)],
        "x.rf": [obs(day - timedelta(days=1), 0.001) for day in MONTHS],
    }
    data = history_data(history(), rows, max_age_days=7)
    assert data.periods[0] == MONTHS[1]
    assert data.roles["EQ"][0] == pytest.approx(0.0 + 0.001)
    assert data.cash.tolist() == pytest.approx([0.001] * 23)
    assert data.month_ends == tuple(range(23))
    rows["x.rf"] = rows["x.rf"][:-1]
    with pytest.raises(SimulationError, match=r"x\.rf has no value for \(2001, 12\)"):
        history_data(history(), rows, max_age_days=7)
    with pytest.raises(SimulationError, match="fewer than two periods"):
        history_data(history(start=date(2000, 1, 15), end=date(2000, 1, 30)), rows, max_age_days=7)


def test_levels_are_read_within_their_age_on_the_first_series_days():
    days = [date(2024, 1, 1) + timedelta(days=k) for k in range(40)]
    level = {"EQ": {"kind": "level", "series": ["x.eq"]}}
    spec = history(frequency="daily", periods_per_year=365, data_start=days[0],
                   start=days[5], end=days[-1], roles=level,
                   cash={"kind": "level", "series": ["x.cash"]})  # fmt: skip
    rows = {
        "x.eq": [obs(day, 100.0 + k) for k, day in enumerate(days)],
        "x.cash": [obs(day, 50.0) for day in days[::3]],
    }
    data = history_data(spec, rows, max_age_days=7)
    assert data.roles["EQ"][0] == pytest.approx(1 / 100)
    assert data.cash.tolist() == pytest.approx([0.0] * 39)
    rows["x.cash"] = [obs(days[0], 50.0)]
    with pytest.raises(SimulationError, match="no fresh level on 2024-01-09"):
        history_data(spec, rows, max_age_days=7)


def twin(base: dict[str, Decimal] | None = None, **overrides: object) -> TotalRisk:
    fields: dict[str, object] = {
        "base": base or {"EQ": D(1)},
        "target_volatility": 0.10,
        "floor": D("0.2"),
        "max_change": D(1),
        "estimator": None,
        "declared": np.array([[0.04]]),
        "periods_per_year": 12,
    }
    fields.update(overrides)
    return TotalRisk(**fields)  # type: ignore[arg-type]


def test_the_rule_runs_on_a_history_by_hand():
    # Half in the role (10 % over a declared 20 %), half in cash; 10 % then -10 %, cash 0.
    run_path = _Path(
        roles=np.array([[0.0], [0.10], [-0.10]]),
        cash=np.zeros(3),
        rebalance=(0, 1),
        times=np.array([0.0, 1 / 12, 2 / 12]),
    )
    values, times = run_rule(run_path, twin(), Bands(D("0.05"), D("0.25")), 0.001)
    first = 1 - 0.001
    after = first * 0.5 * 1.1 + first * 0.5
    # At the second rebalance the role is 0.55 / 1.05 of the account, within its band.
    assert values.tolist() == pytest.approx([1.0, after, after - first * 0.5 * 1.1 * 0.1])
    assert times.tolist() == pytest.approx([0.0, 1 / 12, 2 / 12])
    estimated = run_rule(run_path, twin(estimator=FAST), Bands(D("0.05"), D("0.25")), 0.0)[0]
    assert estimated[0] == 1.0
    assert len(estimated) == 3


def test_an_account_that_loses_everything_on_a_history_stops_the_run():
    ruin = _Path(
        roles=np.array([[0.0], [-1.0]]),
        cash=np.array([0.0, -1.0]),
        rebalance=(0,),
        times=np.array([0.0, 1 / 12]),
    )
    with pytest.raises(SimulationError, match="lost everything in a period"):
        run_rule(ruin, twin(), Bands(D("0.05"), D("0.25")), 0.0)


def test_the_check_runs_what_it_covers_and_refuses_what_it_cannot():
    rows = {
        "x.mkt": [obs(day, 0.02 if k % 4 else -0.05) for k, day in enumerate(MONTHS)],
        "x.rf": [obs(day, 0.001) for day in MONTHS],
    }
    spec = history()
    data = history_data(spec, rows, max_age_days=7)
    passed = (AdequacyCheck("data loaded", True, "ok"),)
    strategies = {"T": twin(), "M": twin({"EQ": D("0.5"), "BD": D("0.5")},
                                         declared=np.eye(2) * 0.01)}  # fmt: skip
    common = {
        "bands": Bands(D("0.05"), D("0.25")), "limits": [D("0.1")], "horizons": [1, None],
        "horizon_years": 1, "seed": 3, "days_per_year": 365.25,
    }  # fmt: skip
    result = long_history_check(spec, data, passed, strategies, **common)
    assert result.skipped == ("M",)
    [run] = result.runs
    assert run.name == "T"
    assert run.growth is not None
    assert result.path_months == 12
    assert result.months == 21
    assert [(limit, horizon) for limit, horizon, _ in run.breach] == [
        (D("0.1"), 1),
        (D("0.1"), None),
    ]
    failed = (AdequacyCheck("data loaded", False, "never loaded"),)
    assert long_history_check(spec, None, failed, strategies, **common).runs == ()
    late = history(start=date(2001, 12, 1))
    short = long_history_check(late, data, passed, strategies, **common)
    assert short.runs == ()
    assert short.checks[-1].name == "history"
    empty = HistoryData((), {"EQ": np.zeros(0)}, np.zeros(0), ())
    assert long_history_check(spec, empty, passed, strategies, **common).runs == ()
