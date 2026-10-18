"""The pre-registration of a total-risk study (DECISIONS D92): its strategies, estimators, twins,
comparisons, the owner's point, the block rule, the sensitivity, the checks and the development
window, each refused when incoherent; and earlier, locked pre-registrations still load.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from pathlib import Path

import pytest
from research_risk_toy import RISK_TOY

from obliskiq.errors import PreregistrationError
from obliskiq.research.prereg import load_any, load_prereg

D = Decimal


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "risk_toy.md"
    path.write_text(text, encoding="utf-8")
    return path


def edited(tmp_path: Path, old: str, new: str) -> Path:
    assert old in RISK_TOY, old
    return write(tmp_path, RISK_TOY.replace(old, new, 1))


def test_the_toy_loads_with_every_new_field(tmp_path):
    spec = load_prereg(write(tmp_path, RISK_TOY)).spec
    assert [s.name for s in spec.study.portfolios] == ["A", "S1", "S2", "T1", "T2"]
    assert [c.name for c in spec.comparisons] == ["S1 against T1", "S2 against T2"]
    assert spec.marks == "daily"
    assert spec.owner_point is not None
    assert spec.owner_point.limit == D("0.3")
    assert spec.study.twins[0].declared
    assert not spec.study.candidates[0].declared
    # The blend's 63-day window is its longest lookback: 63 / 261 of a year, in months.
    assert spec.study.lookback_months() == D(63) * 12 / 261
    assert spec.study.months() == 47
    scaling = spec.study.core.scaling
    assert scaling is not None
    monthly = scaling.estimators["blend"].rule(12 / 261)
    assert monthly.window == 3
    assert monthly.half_life == pytest.approx(10 * 12 / 261)
    assert scaling.lookback_months("declared") == 0


def test_a_study_without_a_development_window_loads(tmp_path):
    text = RISK_TOY.replace("development:\n  start: 2019-02-01\n  end: 2019-12-31\n", "")
    assert load_prereg(write(tmp_path, text)).spec.development is None


def test_the_locked_studies_still_load(repo_root):
    folder = repo_root / "research" / "preregistrations"
    core = load_any(folder / "core-allocation.md")
    assert core.spec.kind == "portfolio_comparison"
    assert core.spec.marks == "month_end"
    assert core.spec.comparisons == []
    assert load_any(folder / "selection-country-momentum.md").spec.kind == "cross_section"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('tests_in_confirmation: 4', 'tests_in_confirmation: 2', "and 2 comparison"),
        ("    candidate: S1\n", "    candidate: S9\n", "unknown portfolios"),
        ("    against: T1\n", "    against: S1\n", "with itself"),
        ("  - name: S2 against T2\n", "  - name: S2\n", "distinct name"),
        ('  limit: "0.3"', '  limit: "0.25"', "owner's limit must be one of"),
        ("block_months: 6\n  seed: 5", "block_months: 3\n  seed: 5", "shorter than twice"),
        ("block_months: [4, 6, 9]", "block_months: [4, 6]", "at least 3"),
        ("block_months: [4, 6, 9]", "block_months: [4, 6, 6]", "must be distinct"),
        ("  end: 2019-12-31\n", "  end: 2020-02-01\n", "end before the study starts"),
        ('      base: {EQ: "0.6", BD: "0.4"}\n      target_volatility: "0.08"',
         '      base: {EQ: "0.6", BD: "0.3"}\n      target_volatility: "0.08"', "sum to 1"),
        ("      estimator: fast", "      estimator: slow", "names no estimator slow"),
        ("      estimator: blend\n      tax_aware: true",
         "      estimator: blend\n      tax_aware: false", "only when tax-aware"),
        ('      base: {EQ: "1"}', '      base: {CASH: "1"}', "without a declared risk"),
        ("    window_days: 63", "    window_days: null", "estimator: a window with weight"),
        ("      blend:\n", "      declared:\n", "names the declared matrix"),
        ("      EQ: {kind: level, series: [risk.equity]}",
         "      GD2: {kind: level, series: [risk.equity]}", r"names roles \['GD2'\]"),
        ("    cash: {kind: level, series: [risk.cash]}",
         "    cash: {kind: level, series: [risk.cash, risk.bond]}", "a level history is one"),
        ("    data_start: 2019-01-01\n    start: 2019-07-01",
         "    data_start: 2019-08-01\n    start: 2019-07-01", "data_start <= start < end"),
        ("      estimation_start: 2019-01-01", "      estimation_start: 2019-03-01",
         "estimation_start must be on or before"),
    ],
)  # fmt: skip
def test_an_incoherent_study_is_refused(tmp_path, old, new, message):
    with pytest.raises(PreregistrationError, match=message):
        load_prereg(edited(tmp_path, old, new))


def test_checks_that_need_what_the_study_lacks_are_refused(tmp_path):
    without_point = RISK_TOY.replace(
        'owner_point:\n  limit: "0.3"\n  probability: "0.05"\n  horizon_years: 3\n',
        "owner_point: null\n",
    )
    with pytest.raises(PreregistrationError, match="breach_horizons_years needs"):
        load_prereg(write(tmp_path, without_point))
    declared = RISK_TOY.replace("      estimator: fast", "      estimator: declared").replace(
        "      estimator: blend", "      estimator: declared"
    )
    with pytest.raises(PreregistrationError, match="forecast check needs a total-risk"):
        load_prereg(write(tmp_path, declared))
    no_tax = RISK_TOY.replace('    tax_aware:\n      benefit_fraction: "0.05"\n', "")
    with pytest.raises(PreregistrationError, match="holds costly reductions: study"):
        load_prereg(write(tmp_path, no_tax))
    no_scaling = (
        RISK_TOY.split("    scaling:\n")[0] + "  reference:" + RISK_TOY.split("  reference:", 1)[1]
    )
    with pytest.raises(PreregistrationError, match=r"scales total risk: study\.core\.scaling"):
        load_prereg(write(tmp_path, no_scaling))
    no_comparisons = RISK_TOY.replace("tests_in_confirmation: 4", "tests_in_confirmation: 2")
    no_comparisons = (
        no_comparisons.split("comparisons:\n")[0]
        + "bootstrap:"
        + (no_comparisons.split("\nbootstrap:", 1)[1])
    )
    with pytest.raises(PreregistrationError, match="development window runs the comparisons"):
        load_prereg(write(tmp_path, no_comparisons))


def test_a_long_history_or_a_tilt_needs_its_parts(tmp_path):
    from research_core_toy import CORE_TOY

    tilted = CORE_TOY.replace(
        "    tilt:\n      momentum_months: 3\n      average_months: 2\n"
        '      hysteresis: "0.02"\n      floor: "0.25"\n',
        "",
    )
    with pytest.raises(PreregistrationError, match=r"tilts: study\.core\.tilt and tax_aware"):
        load_prereg(write(tmp_path, tilted))
    untilted = tilted.replace("      tilt: true\n", "      tilt: false\n")
    with pytest.raises(PreregistrationError, match="structure check uses the core's tilt"):
        load_prereg(write(tmp_path, untilted))
    history = CORE_TOY.replace(
        "structure:\n",
        "long_history:\n  - name: h\n    frequency: monthly\n    periods_per_year: 12\n"
        "    currency: USD\n    data_start: 1990-01-01\n    start: 1991-01-01\n"
        "    end: 1999-12-31\n    roles:\n      EQ: {kind: returns, series: [mkt.Mkt-RF]}\n"
        "    cash: {kind: returns, series: [mkt.RF]}\n    point_in_time: [release_lag]\n"
        '    cost_per_leg: "0.001"\n    block_months: 12\n    n_boot: 2\n    biases: [b]\n'
        "structure:\n",
    )
    with pytest.raises(PreregistrationError, match="long-history check runs total-risk"):
        load_prereg(write(tmp_path, history))
