"""The harness's refusals and edges: malformed pre-registrations and strategies, git that cannot
answer, a venue without a schedule, two identical strategies, a study in a foreign currency with
withheld income, and the data commands.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
import math
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from research_toy import DATES, NOW, TOY, git, put_toy, toy_series

from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.domain.research import Observation, Vendor
from obliskiq.errors import DataSourceError, PreregistrationError, SimulationError
from obliskiq.jurisdiction import load_profile
from obliskiq.research import registry
from obliskiq.research.decide import Label, Outcome
from obliskiq.research.prereg import load_prereg
from obliskiq.research.registry import Git, file_sha256, write_lock
from obliskiq.research.simulate import Panel
from obliskiq.research.strategies import build_strategy
from obliskiq.research.study import StudyRunner, _growth, _months, _synthetic, _Test, gather
from obliskiq.store import Store, open_research_store
from obliskiq.store.research import replace_research_series, research_series_in

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


def write(tmp_path: Path, text: str, name: str = "toy_rotation.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def runner(repo_root: Path, shipped_settings: ObliskConfig) -> StudyRunner:
    return StudyRunner(shipped_settings, load_profile(repo_root / "config", "es"))


def run_study(runner: StudyRunner, path: Path, store: Store):
    return runner.run(
        load_prereg(path), store, registry.TrialRegistry(store), explore=True, lock=None, now=NOW
    )


# ── the pre-registration file ──


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("  end: 2025-12-31", "  end: 2023-12-31", "end must follow start"),
        ("  calendar: A", "  calendar: Z", "calendar Z is not one of the instruments"),
        ('weights: {A: "0.5", B: "0.5"}\n    simplicity: 1',
         'weights: {A: "0.5", C: "0.5"}\n    simplicity: 1',
         r"weights unknown instruments \['C'\]"),
        ("tests_in_confirmation: 1", "tests_in_confirmation: 2", "one test each"),
        ("    - name: rebalanced", "    - name: held", "distinct names"),
        ("      strategy: fixed_weights", "      strategy: momentum", "strategy"),
        ("  by_candidate: null", "  by_candidate: {other: '0.01'}", "names no candidate"),
        ("      simplicity: 2", "      simplicity: 0", "simplicity"),
        ("  - [2025-01-01, 2025-12-31]", "  - [2025-01-01, 2026-12-31]", "is not inside the study"),
        ("kind: portfolio_comparison", "kind: event_study", "kind"),
        ("    - key: B", "    - key: A", "instrument keys must be unique"),
    ],
)  # fmt: skip
def test_a_malformed_pre_registration_is_refused_with_its_problem(tmp_path, old, new, message):
    assert old in TOY
    path = write(tmp_path, TOY.replace(old, new, 1))
    with pytest.raises(PreregistrationError, match=message):
        load_prereg(path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# no front matter\n", "opens with a front matter fence"),
        ("---\nid: x\n", "no closing fence"),
        ("---\nid: [x\n---\n", "not valid YAML"),
        ("---\nid: x\nid: y\n---\n", "not valid YAML"),
    ],
)
def test_a_pre_registration_without_valid_front_matter_is_refused(tmp_path, text, message):
    with pytest.raises(PreregistrationError, match=message):
        load_prereg(write(tmp_path, text))


def test_the_toy_strategies_are_built_from_their_specification(shipped_settings, tmp_path):
    study = load_prereg(write(tmp_path, TOY)).spec.study
    held = build_strategy(study.reference, study, shipped_settings)
    rebalanced = build_strategy(study.candidates[0], study, shipped_settings)
    assert (type(held).__name__, held.name) == ("BuyAndHold", "held")
    assert (type(rebalanced).__name__, rebalanced.name) == ("FixedWeights", "rebalanced")


# ── git ──


def test_git_that_cannot_run_or_read_fails_the_lock(tmp_path, monkeypatch):
    with pytest.raises(PreregistrationError, match="cannot be read"):
        file_sha256(tmp_path / "missing.md")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "other.txt").write_text("x", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "before the pre-registration")
    before = git(repo, "rev-parse", "HEAD")
    path = write(repo, TOY)
    with pytest.raises(PreregistrationError, match=r"holds no toy_rotation\.md"):
        write_lock(path, before)

    def broken(*args, **kwargs):
        raise OSError("no git here")

    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(PreregistrationError, match="git cannot be run"):
        Git(path)


# ── the study's edges ──


def test_a_venue_without_a_schedule_is_not_evaluable(runner, tmp_path, shipped_settings):
    path = write(tmp_path, TOY.replace("venue: XETR", "venue: XTKS"))
    with open_research_store(shipped_settings, tmp_path / "data") as store:
        put_toy(store)
        result = run_study(runner, path, store)
    [test] = result.tests
    assert test.decision.outcome is Outcome.NOT_EVALUABLE
    check = result.checks[-1]
    assert (check.name, check.passed) == ("simulation", False)
    assert "no_schedule" in check.detail
    assert test.estimate is None


def test_identical_strategies_have_no_spread_no_power_and_no_sharpe(
    runner, tmp_path, shipped_settings
):
    text = TOY.replace("    strategy: buy_and_hold", "    strategy: fixed_weights").replace(
        "  - [2024-01-01, 2024-12-31]", "  - [2024-01-01, 2024-03-31]"
    )
    path = write(tmp_path, text)
    with open_research_store(shipped_settings, tmp_path / "data") as store:
        put_toy(store)
        result = run_study(runner, path, store)
    [test] = result.tests
    assert test.estimate == 0
    assert test.se == 0
    assert test.decision.power is None
    assert test.decision.outcome is Outcome.INSUFFICIENT
    assert Label.POWER_UNKNOWN in test.decision.labels
    assert test.deflated is None
    assert test.robustness is not None
    one_date = test.robustness.subperiods[0]
    assert (one_date.estimate, one_date.sign_holds) == (None, None)


def test_a_run_that_ends_with_nothing_has_no_growth(runner, tmp_path, shipped_settings):
    path = write(tmp_path, TOY)
    with open_research_store(shipped_settings, tmp_path / "data") as store:
        put_toy(store)
        result = run_study(runner, path, store)
    assert result.candidates[0].historical is not None
    empty = replace(result.candidates[0].historical, log_growth=None)
    with pytest.raises(SimulationError, match="ended with nothing"):
        _growth(empty)


def test_the_breakeven_search_reads_an_untradeable_cost_as_no_effect(
    runner, tmp_path, shipped_settings
):
    path = write(tmp_path, TOY)
    with open_research_store(shipped_settings, tmp_path / "data") as store:
        put_toy(store)
        prereg = load_prereg(path)
        inputs = gather(store, prereg, "EUR", 365.25)
        result = run_study(runner, path, store)
    candidate, reference = result.candidates[0].historical, result.reference.historical

    winner = replace(candidate, log_growth=0.1)
    loser = replace(reference, log_growth=0.0)

    def simulate(panel, dates, multiplier, names):
        if multiplier > 1:
            raise SimulationError("costs more than it raises")
        return {"rebalanced": winner, "held": loser}

    test = _Test("rebalanced", "rebalanced", "held", 0.01, 0.02, comparison=False)
    robustness = runner._robustness(prereg, inputs.panel, inputs.dates, test, 0.1, simulate)
    # The effect holds up to the model's own costs and vanishes past them: break-even near 1.
    assert robustness.breakeven.status == "found"
    assert robustness.breakeven.multiplier == pytest.approx(1, abs=0.001)
    assert not robustness.breakeven_exceeds_uncertainty
    assert all(p.sign_holds for p in robustness.subperiods)


USD = """    - key: U
      price_series: toy.u_close
      income_series: toy.u_dividend
      currency: USD
      venue: XLON
      asset_class: etf
      issuer_country: null
      income_country: US
      market_cap_usd: "1000000000000"
      expense_ratio: null
      security:
        kind: etf
        isin: null
        issuer: Toy U
        tracked_index: Index U
        listed: true
"""


def test_a_study_in_a_foreign_currency_converts_and_withholds(runner, tmp_path, shipped_settings):
    text = (
        TOY.replace("  fx_dataset: null", "  fx_dataset: ecb_fx")
        .replace('weights: {A: "0.5", B: "0.5"}', 'weights: {A: "0.5", U: "0.5"}')
        .replace("  roles: null", USD + "  roles: null")
    )
    path = write(tmp_path, text)
    rates = [1.0, 1.25, 1.25, 1.0, 1.0]
    with open_research_store(shipped_settings, tmp_path / "data") as store:
        put_toy(store)
        paid = date(2024, 3, 15)
        for series_id, values in (("toy.u_close", [100.0] * 5), ("ecb_fx.USD", rates)):
            rows = [
                Observation(observed_on=d, available_on=d, value=v)
                for d, v in zip(DATES, values, strict=True)
            ]
            if series_id == "ecb_fx.USD":  # the dividend converts at its ex-date's rate
                rows.append(Observation(observed_on=paid, available_on=paid, value=1.1))
            replace_research_series(store, toy_series(series_id), rows)
        dividend = Observation(
            observed_on=date(2024, 3, 15), available_on=date(2024, 3, 15), value=2.0
        )
        replace_research_series(store, toy_series("toy.u_dividend"), [dividend])
        result = run_study(runner, path, store)
        prereg = load_prereg(path)
        inputs = gather(store, prereg, "EUR", 365.25)
    assert all(check.passed for check in result.checks), result.checks
    candidate = result.candidates[0].historical
    assert candidate is not None
    assert candidate.withheld > 0
    assert result.tests[0].decision.interval is not None
    # Before tax nothing is withheld.
    pre_tax = result.candidates[0].pre_tax
    assert pre_tax is not None
    assert pre_tax.withheld == 0
    months = _months(inputs.panel, inputs.dates, 7, 7)
    assert months.rate_changes["USD"][0] == pytest.approx(math.log(1.25))
    assert months.yields["U"][0] == pytest.approx(0.02)
    path_panel = _synthetic(months, inputs.dates, [0, 0, 0, 0])
    assert [round(o.value, 6) for o in path_panel.fx["USD"]] == pytest.approx(
        [1.0, 1.25, 1.5625, 1.953125, 2.44140625]
    )
    assert [o.observed_on for o in path_panel.income["U"]] == DATES[1:]


def test_bootstrap_months_need_every_value(shipped_settings):
    panel = Panel(
        prices={"A": (Observation(observed_on=DATES[0], available_on=DATES[0], value=1.0),)},
        income={},
        fx={},
    )
    with pytest.raises(SimulationError, match="A lacks a value at a rebalance date"):
        _months(panel, DATES[:2], 7, 7)


# ── the data commands ──


def cli(repo_root: Path, *argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(
        ["--config-dir", str(repo_root / "config"), *argv], env_file=None, stdout=out, stderr=err
    )
    return status, out.getvalue() + err.getvalue()


@respx.mock
def test_research_data_load_fills_the_research_store(
    repo_root, tmp_path, monkeypatch, shipped_settings
):
    monkeypatch.setenv("OBLISK_DATA_DIR", str(tmp_path))
    data = shipped_settings.research.data
    respx.get(f"{data.ken_french.base_url}F-F_Research_Data_Factors_CSV.zip").mock(
        return_value=httpx.Response(
            200, content=(FIXTURES / "ken_french_ff3_factors.zip").read_bytes()
        )
    )
    respx.get(data.ecb.url).mock(return_value=httpx.Response(404))
    respx.route().mock(return_value=httpx.Response(404))

    def offline(symbol, start, end):
        raise DataSourceError(f"Yahoo is not reached in a test ({symbol})")

    monkeypatch.setattr("obliskiq.cli.research.yfinance_history", offline)
    status, out = cli(repo_root, "research", "data", "load", "ff3_monthly", "ecb_fx")
    assert status == EXIT_INVALID
    assert "ff3_monthly: " in out
    assert "rows" in out
    assert "ecb_fx: FAILED: " in out
    with open_research_store(shipped_settings, tmp_path) as store:
        assert {s.vendor for s in research_series_in(store, "ff3_monthly")} == {Vendor.KEN_FRENCH}
    status, out = cli(repo_root, "research", "data", "load", "nope")
    assert (status, "no dataset 'nope'" in out) == (EXIT_INVALID, True)
    status, out = cli(repo_root, "research", "data", "load")
    assert (status, "name the datasets" in out) == (EXIT_INVALID, True)
    status, out = cli(repo_root, "research", "data", "load", "--all")
    assert status == EXIT_INVALID  # every other download is refused by the mock
    assert "eunl: FAILED: Yahoo is not reached in a test (EUNL.DE)" in out


def test_research_data_coverage_reports_by_year(repo_root, tmp_path, monkeypatch, shipped_settings):
    monkeypatch.setenv("OBLISK_DATA_DIR", str(tmp_path))
    with open_research_store(shipped_settings, tmp_path) as store:
        put_toy(store)
    status, out = cli(
        repo_root, "research", "data", "coverage", "--series", "toy.a_close", "toy.missing",
        "--from", "2024", "--to", "2025",
    )  # fmt: skip
    assert status == EXIT_OK, out
    assert "2024  50.0% without a price (toy.missing)" in out
    assert "without data: toy.missing" in out
    prereg = write(tmp_path, TOY)
    status, out = cli(repo_root, "research", "data", "coverage", "--prereg", str(prereg))
    assert status == EXIT_OK, out
    assert "coverage of 2 series as of 2025-12-31" in out
    status, out = cli(repo_root, "research", "data", "coverage", "--series", "x")
    assert (status, "give --prereg" in out) == (EXIT_INVALID, True)
    status, out = cli(repo_root, "research", "data", "coverage", "--prereg", str(tmp_path / "x.md"))
    assert (status, "cannot be read" in out) == (EXIT_INVALID, True)


def test_research_lock_refuses_a_commit_it_cannot_find(repo_root, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    path = write(repo, TOY)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "pre-register")
    status, out = cli(repo_root, "research", "lock", str(path), "--commit", "f" * 40)
    assert status == EXIT_INVALID
    assert "refused: git rev-parse" in out
    status, out = cli(repo_root, "research", "run", str(tmp_path / "missing.md"))
    assert (status, "cannot be read" in out) == (EXIT_INVALID, True)


def test_month_ends_keep_only_the_study_window():
    from obliskiq.research.study import month_ends

    rows = [
        Observation(observed_on=date(2023, 12, 29), available_on=date(2023, 12, 29), value=1.0),
        Observation(observed_on=date(2024, 1, 30), available_on=date(2024, 1, 30), value=1.0),
        Observation(observed_on=date(2024, 1, 31), available_on=date(2024, 1, 31), value=1.0),
        Observation(observed_on=date(2024, 2, 29), available_on=date(2025, 1, 1), value=1.0),
    ]
    assert month_ends(rows, date(2024, 1, 1), date(2024, 12, 31)) == (date(2024, 1, 31),)


def test_a_run_without_data_reports_no_estimate(repo_root, tmp_path, monkeypatch):
    monkeypatch.setenv("OBLISK_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    path = write(repo, TOY)
    status, out = cli(repo_root, "research", "run", str(path), "--explore")
    assert status == EXIT_OK, out
    assert out.startswith("toy_rotation:\n  rebalanced: not evaluable")
    assert "estimate" not in out
