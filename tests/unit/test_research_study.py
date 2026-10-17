"""A toy study end to end: data in the research store, a pre-registration merged and locked in a
git repository, `obliskiq research run`, the report, the trial registry, and a hand calculation
of the primary effect. Also: an unlocked or edited pre-registration is refused, exploration is
labelled and budgeted, and nothing published after the horizon changes any output.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import json
import logging
import math
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from research_toy import A_PRICES, DATES, NOW, TOY, git, put_toy, toy_series

from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.domain.research import (
    Observation,
    PointInTime,
    TrialStage,
)
from obliskiq.errors import PreregistrationError
from obliskiq.jurisdiction import load_profile
from obliskiq.research.decide import Outcome
from obliskiq.research.prereg import front_matter, load_any, load_prereg
from obliskiq.research.registry import (
    TrialRegistry,
    canonical_sha256,
    file_sha256,
    lock_path,
    verify_lock,
    write_lock,
)
from obliskiq.research.report import render
from obliskiq.research.study import StudyRunner
from obliskiq.store import Store, open_research_store
from obliskiq.store.research import replace_research_series, trials_for


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "research" / "preregistrations").mkdir(parents=True)
    git(root, "init", "-q")
    prereg = root / "research" / "preregistrations" / "toy_rotation.md"
    prereg.write_text(TOY, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "pre-register the toy")
    return root


@pytest.fixture
def prereg_path(repo: Path) -> Path:
    return repo / "research" / "preregistrations" / "toy_rotation.md"


@pytest.fixture
def data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shipped_settings: ObliskConfig
) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("OBLISK_DATA_DIR", str(directory))
    with open_research_store(shipped_settings, directory) as store:
        put_toy(store)
    return directory


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


def cli(repo_root: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(
        ["--config-dir", str(repo_root / "config"), *argv], env_file=None, stdout=out, stderr=err
    )
    return status, out.getvalue(), err.getvalue()


# ── the hand calculation ──

# Costs on Xetra (the shipped schedules): 0.10 % commission, at least EUR 4.00, plus the liquid
# tier's 0.10 % spread and 0.10 % slippage; c(N) = 0.002 N + max(0.001 N, 4).
#
# Rebalanced 50/50. 31 Jan 2024: 49 A and 49 B at 100 cost 4,914.70 each; cash 170.60.
# 31 Jul 2024 (A 150): worth 12,420.60, half 6,210.30. Sell 8 A: 1,200 - 6.40 = 1,193.60 against
# 802.40 of cost, a gain of 391.20. Buy 13 B: 1,306.60. Cash 57.60.
# 31 Jul 2025: 2024's tax, 391.20 x 19 % = 74.328, is due. One B is sold (95.80 against 100.30, a
# loss of 4.50) and the tax paid: cash 79.072.
# 31 Dec 2025: 41 A raise 6,131.55 (a gain of 2,019.25); 61 B raise 6,081.70 (losses of 28.80 and
# 10.50). 2025's gains are 1,975.45 and its tax 375.3355. Liquidation value: 79.072 + 6,131.55 +
# 6,081.70 - 375.3355 = 11,916.9865.
REBALANCED = Decimal("11916.9865")
#
# Held 50/50: the same purchase; at the horizon 49 A raise 7,327.95 (a gain of 2,413.25) and 49 B
# 4,885.30 (a loss of 29.40). Tax: 2,383.85 x 19 % = 452.9315. Liquidation value: 170.60 +
# 7,327.95 + 4,885.30 - 452.9315 = 11,930.9185.
HELD = Decimal("11930.9185")
YEARS = 700 / 365.25
EFFECT = math.log(float(REBALANCED) / float(HELD)) / YEARS


@pytest.fixture
def runner(repo_root: Path, shipped_settings: ObliskConfig) -> StudyRunner:
    return StudyRunner(shipped_settings, load_profile(repo_root / "config", "es"))


def run_study(runner: StudyRunner, prereg_path: Path, store: Store):
    return runner.run(
        load_prereg(prereg_path), store, TrialRegistry(store), explore=True, lock=None, now=NOW
    )


def test_the_toy_study_matches_the_hand_calculation(
    runner, prereg_path, data_dir, shipped_settings
):
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_study(runner, prereg_path, store)
    assert result.dates == tuple(DATES)
    candidate, reference = result.candidates[0].historical, result.reference.historical
    [test] = result.tests
    assert candidate is not None and reference is not None  # noqa: PT018
    assert candidate.liquidation_value == REBALANCED
    assert reference.liquidation_value == HELD
    assert [(t.tax_year, t.amount, t.paid_on) for t in candidate.taxes] == [
        (2024, Decimal("74.328"), date(2025, 7, 31)),
        (2025, Decimal("375.3355"), date(2025, 12, 31)),
    ]
    assert [(t.tax_year, t.amount) for t in reference.taxes] == [(2025, Decimal("452.9315"))]
    assert test.estimate == pytest.approx(EFFECT)
    assert result.coverage == 1.0
    assert all(check.passed for check in result.checks)
    assert test.decision.outcome is not Outcome.NOT_EVALUABLE
    assert test.decision.interval is not None
    low, high = test.decision.interval
    assert low <= high
    assert test.robustness is not None
    assert [p.estimate is not None for p in test.robustness.subperiods] == [True, True]
    # Before tax the same trades leave more: no tax is withheld, settled or paid.
    pre_tax = result.candidates[0].pre_tax
    assert pre_tax is not None
    assert pre_tax.tax_paid == 0
    assert pre_tax.liquidation_value > candidate.liquidation_value
    assert result.cost_labels == (
        "commission:europe_eur",
        "friction_assumption",
        "tax_not_modelled",
    )


def test_the_same_inputs_and_seed_give_the_same_result(
    runner, prereg_path, data_dir, shipped_settings
):
    with open_research_store(shipped_settings, data_dir) as store:
        first = run_study(runner, prereg_path, store)
        second = run_study(runner, prereg_path, store)
    assert first.tests == second.tests
    assert first.inputs_sha256 == second.inputs_sha256
    assert first.candidates[0].median_log_growth == second.candidates[0].median_log_growth


def test_a_changed_cost_or_tax_rule_changes_the_inputs_hash(
    repo_root, prereg_path, data_dir, shipped_settings
):
    profile = load_profile(repo_root / "config", "es")
    assert profile.tax is not None
    commissions = [
        c.model_copy(update={"rate": c.rate * 2}) for c in shipped_settings.costs.commissions
    ]
    costlier = shipped_settings.model_copy(
        update={"costs": shipped_settings.costs.model_copy(update={"commissions": commissions})}
    )
    withholding = [
        row.model_copy(update={"rate": row.rate * 2}) for row in profile.tax.withholding_at_source
    ]
    taxed = profile.model_copy(
        update={"tax": profile.tax.model_copy(update={"withholding_at_source": withholding})}
    )
    runners = [
        StudyRunner(shipped_settings, profile),
        StudyRunner(costlier, profile),
        StudyRunner(shipped_settings, taxed),
    ]
    with open_research_store(shipped_settings, data_dir) as store:
        digests = {run_study(r, prereg_path, store).inputs_sha256 for r in runners}
    assert len(digests) == len(runners)


def test_nothing_published_after_the_horizon_changes_any_output(
    runner, prereg_path, tmp_path, shipped_settings
):
    """D64's point-in-time test: perturb every observation after as_of; every output stays."""
    plain, perturbed = tmp_path / "plain", tmp_path / "perturbed"
    with open_research_store(shipped_settings, plain) as store:
        put_toy(store)
        before = run_study(runner, prereg_path, store)
    later = [
        # A later session, and a revision of a horizon value published after the horizon.
        Observation(observed_on=date(2026, 1, 30), available_on=date(2026, 1, 30), value=1.0),
        Observation(observed_on=DATES[-1], available_on=date(2026, 2, 2), value=999.0),
        Observation(observed_on=DATES[1], available_on=date(2026, 3, 3), value=0.01),
    ]
    with open_research_store(shipped_settings, perturbed) as store:
        put_toy(store, extra=later)
        after = run_study(runner, prereg_path, store)
    for field in ("coverage", "dates", "tests", "frontier", "checks"):
        assert getattr(before, field) == getattr(after, field), field
    for b, a in zip(before.portfolios, after.portfolios, strict=True):
        assert b.median_log_growth == a.median_log_growth
        assert b.breach == a.breach
        assert b.terminal_wealth_p5 == a.terminal_wealth_p5


def test_missing_or_late_data_is_not_evaluable(runner, prereg_path, tmp_path, shipped_settings):
    with open_research_store(shipped_settings, tmp_path / "empty") as store:
        result = run_study(runner, prereg_path, store)
    [test] = result.tests
    assert test.decision.outcome is Outcome.NOT_EVALUABLE
    failed = {c.name for c in result.checks if not c.passed}
    assert {"data loaded", "coverage", "effective observations"} <= failed
    assert test.robustness is None
    report = render(result)
    assert "**not evaluable**" in report
    assert "rebalanced: not run (the outcome is not evaluable)." in report
    with open_research_store(shipped_settings, tmp_path / "lagged") as store:
        put_toy(store)
        lagged = toy_series("toy.a_close").model_copy(
            update={"point_in_time": PointInTime.RELEASE_LAG}
        )
        rows = [
            Observation(observed_on=d, available_on=d, value=p)
            for d, p in zip(DATES, A_PRICES, strict=True)
        ]
        replace_research_series(store, lagged, rows)
        result = run_study(runner, prereg_path, store)
    check = next(c for c in result.checks if c.name == "point in time")
    assert not check.passed
    assert "toy.a_close" in check.detail


# ── the command line, the lock and the registry ──


def test_a_toy_study_runs_end_to_end_through_the_command_line(
    repo_root, repo, prereg_path, data_dir
):
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path))
    assert status == EXIT_INVALID
    assert "is not locked" in out
    commit = git(repo, "rev-parse", "HEAD")
    status, out, _ = cli(repo_root, "research", "lock", str(prereg_path), "--commit", commit)
    assert status == EXIT_OK, out
    assert lock_path(prereg_path).is_file()
    status, out, err = cli(repo_root, "research", "run", str(prereg_path))
    assert status == EXIT_OK, out + err
    assert out.startswith("toy_rotation:\n  rebalanced: ")
    assert f"estimate {EFFECT:.4%} a year" in out
    report = repo / "research" / "reports" / "toy_rotation.md"
    text = report.read_text(encoding="utf-8")
    for fragment in (
        "**Confirmation run.**",
        f"merge commit `{commit}`",
        "Inputs SHA-256:",
        "seed 7",
        "`" + "a" * 64 + "`",
        "## What this test cannot conclude",
        "## The frontier",
        "No frontier was pre-registered.",
        "## Year by year",
        "No structure check was pre-registered.",
        "## Bias notes",
        "Not tax advice.",
        "two-month rule",
    ):
        assert fragment in text, fragment
    with open_research_store(load_settings(repo_root), data_dir) as store:
        [trial] = trials_for(store, "toy_rotation")
    assert trial.stage is TrialStage.CONFIRMATION
    assert trial.estimate == pytest.approx(EFFECT)
    # The confirmation is single-shot once it has an outcome.
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path))
    assert status == EXIT_INVALID
    assert "already has its confirmation run" in out


def load_settings(repo_root: Path) -> ObliskConfig:
    from obliskiq.config import load_config

    return load_config(repo_root / "config").settings


def test_an_edited_pre_registration_is_refused_and_exploration_is_budgeted(
    repo_root, repo, prereg_path, data_dir
):
    commit = git(repo, "rev-parse", "HEAD")
    write_lock(prereg_path, commit)
    prereg_path.write_text(TOY + "\nAn afterthought.\n", encoding="utf-8")
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path))
    assert status == EXIT_INVALID
    assert "changed since its lock" in out
    for _ in range(2):
        status, out, _ = cli(repo_root, "research", "run", str(prereg_path), "--explore")
        assert status == EXIT_OK, out
    reports = list((repo / "research" / "reports").glob("toy_rotation-exploration-*.md"))
    assert reports
    assert "EXPLORATORY" in reports[0].read_text(encoding="utf-8")
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path), "--explore")
    assert status == EXIT_INVALID
    assert "trial budget (2 of 2" in out


def test_a_lock_must_name_a_commit_that_holds_this_text(repo, prereg_path):
    first = git(repo, "rev-parse", "HEAD")
    prereg_path.write_text(TOY.replace('theta: "0.005"', 'theta: "0.006"'), encoding="utf-8")
    with pytest.raises(PreregistrationError, match="is not the text the lock records"):
        write_lock(prereg_path, first)
    git(repo, "commit", "-q", "-am", "a second pre-registration text")
    second = git(repo, "rev-parse", "HEAD")
    lock = write_lock(prereg_path, second)
    assert verify_lock(prereg_path) == lock
    with pytest.raises(PreregistrationError, match="failed"):
        write_lock(prereg_path, "0" * 40)
    # The same text committed on another branch is not in the history the run stands on.
    text = prereg_path.read_text(encoding="utf-8")
    git(repo, "checkout", "-q", "-b", "side", first)
    prereg_path.write_text(text, encoding="utf-8")
    git(repo, "commit", "-q", "-am", "the same text on a side branch")
    side = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-")
    with pytest.raises(PreregistrationError, match="not in the history"):
        write_lock(prereg_path, side)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not json", "is not a lock file"),
        ('{"file": "other.md", "sha256": "x", "merge_commit": "y"}', "locks other.md"),
        ('{"file": "toy_rotation.md", "sha256": "x", "merge_commit": "HEAD"}', "names no commit"),
    ],
)
def test_a_malformed_lock_is_refused(prereg_path, payload, message):
    lock_path(prereg_path).write_text(payload, encoding="utf-8")
    with pytest.raises(PreregistrationError, match=message):
        verify_lock(prereg_path)


def test_research_power_states_power_before_any_data(repo_root, prereg_path):
    status, out, _ = cli(repo_root, "research", "power", str(prereg_path))
    assert status == EXIT_OK
    # sd 0.02 a month over 4 independent months, annualised: 0.02 x 12 / 2 = 12 % a year.
    # Power: Phi(0.005 / 0.12 - 1.6449) = Phi(-1.6032) = 0.054; detectable at 0.8:
    # (1.6449 + 0.8416) x 12 % = 29.84 %.
    row = next(line for line in out.splitlines() if line.strip().startswith("rebalanced"))
    assert row.split() == ["rebalanced", "12.0000%", "0.054", "29.8377%"]
    assert "effective observations  4 (1 per period)" in out
    status, out, _ = cli(repo_root, "research", "power", str(prereg_path.with_name("missing.md")))
    assert status == EXIT_INVALID
    assert "cannot be read" in out


def test_every_locked_preregistration_matches_its_lock_and_states_its_constants_hash(repo_root):
    """The repository's own pre-registrations: unchanged since their lock (the merge commit is
    checked by ``research run``, which needs the history a shallow checkout lacks), valid, and
    stating the hash of their front matter where they state one."""
    folder = repo_root / "research" / "preregistrations"
    locks = sorted(folder.glob("*.lock"))
    assert "core-allocation.lock" in [lock.name for lock in locks]
    for lock in locks:
        payload = json.loads(lock.read_text())
        prereg = folder / payload["file"]
        assert file_sha256(prereg) == payload["sha256"], prereg.name
        load_any(prereg)
        data, body = front_matter(prereg.read_text())
        if "## Constants hash" in body:
            assert canonical_sha256(data) in body, prereg.name


def test_the_template_carries_every_field_and_parses(repo_root):
    template = load_prereg(repo_root / "research" / "preregistrations" / "TEMPLATE.md")
    spec = template.spec
    for field in (
        "mechanism", "citation", "universe", "data_status", "primary_metric", "theta",
        "theta_justification", "power", "adjustment", "robustness", "trial_budget",
        "subperiods", "cost_uncertainty_multiplier", "adequacy", "bootstrap",
    ):  # fmt: skip
        assert getattr(spec, field) is not None, field
    assert spec.study.end > spec.study.start
