"""The report of a cross-section run: what was read and with which hashes and seeds, whether
the data were adequate (survivorship measured year by year), each test's outcome with its labels
and power, the robustness checks and the placebo, every portfolio before and after tax, the
monthly excess year by year, the biases, the tax rules applied and what the test cannot
conclude (DECISIONS D62, D63, D64).

An exploratory run says so at the top and is never evidence: only a confirmation run of a merged,
locked pre-registration counts.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from ..util.timeutil import utc_stamp
from .cross_section import (
    CROSS_SECTION_NOTES,
    CrossSectionResult,
    PlaceboResult,
    PortfolioRun,
    SelectionTest,
)
from .report import SURVIVORSHIP_NOTE, _money, _number, _outcome, _percent, _table

#: What a cross-section cannot conclude whatever its outcome.
CROSS_SECTION_LIMITS: Final = (
    "That the signal keeps its effect after the sample, or in a period, market or cost regime "
    "outside it.",
    "Anything about members the vendor does not serve beyond their measured share.",
    "That a portfolio of these index proxies is what the vehicles held would have earned: fund "
    "costs are a declared rate and tracking difference is not modelled.",
)
SHORT_DIGITS: Final = 3


def _interval(interval: tuple[float, float] | None) -> str:
    return "n/a" if interval is None else f"[{_percent(interval[0])}, {_percent(interval[1])}]"


def _test_row(test: SelectionTest, scale: int) -> list[str]:
    nw = test.newey_west
    nw_text = "n/a"
    if nw is not None and nw.mean is not None:
        nw_text = f"{_percent(scale * nw.mean)} " + (
            "n/a"
            if nw.ci is None
            else f"[{_percent(scale * nw.ci[0])}, {_percent(scale * nw.ci[1])}]"
        )
    return [
        test.spec.name,
        f"{test.spec.candidate} minus {test.spec.against}",
        _outcome(test.decision),
        _percent(test.estimate),
        _interval(test.decision.interval),
        nw_text,
        _percent(float(test.spec.theta)),
        _number(test.decision.power, SHORT_DIGITS),
        _percent(test.detectable),
        f"{test.declared_power:.3f} / {_percent(test.declared_detectable)}",
        _percent(test.pre_tax_estimate),
    ]


def _robustness_lines(test: SelectionTest) -> list[str]:
    robustness = test.robustness
    if robustness is None:
        return [f"- {test.spec.name}: not run (the outcome is not evaluable)."]
    parts = (
        ", ".join(
            f"{p.start.isoformat()} to {p.end.isoformat()} {_percent(p.estimate)}"
            + (
                ""
                if p.sign_holds is None
                else (" sign holds" if p.sign_holds else " **sign fails**")
            )
            for p in robustness.subperiods
        )
        or "none registered"
    )
    breakeven = robustness.breakeven
    found = (
        f"{breakeven.multiplier:.3f}"
        if breakeven.multiplier is not None
        else (
            f"above {breakeven.bound:g}"
            if breakeven.status == "above_search"
            else "none (below theta even without costs)"
        )
    )
    exceeded = "exceeded" if robustness.breakeven_exceeds_uncertainty else "**not exceeded**"
    return [
        f"- {test.spec.name}: subperiods {parts}; break-even cost multiplier {found} "
        f"(declared cost uncertainty {robustness.cost_uncertainty}: {exceeded}); placebo: "
        f"{robustness.placebo}."
    ]


def _placebo_lines(placebo: PlaceboResult | None) -> list[str]:
    if placebo is None:
        return ["No placebo was registered."]
    if placebo.fails is None:
        verdict = "not run (the data were not evaluable)"
    elif placebo.fails:
        verdict = "fails, as it should: its interval's lower end is below the test's theta"
    else:
        verdict = "**shows the effect**: its interval's lower end reaches the test's theta"
    return [
        f"- {placebo.candidate} minus {placebo.against}, against {placebo.test}'s theta "
        f"{_percent(placebo.theta)}: estimate {_percent(placebo.estimate)}, interval "
        f"{_interval(placebo.interval)}; {verdict}."
    ]


def _run_row(run: PortfolioRun, before_tax: bool) -> list[str]:
    result = run.before_tax if before_tax else run.after_tax
    return [
        run.name,
        _money(result.liquidation_value),
        _percent(result.log_growth),
        _percent(result.max_drawdown),
        _money(result.costs),
        _money(result.tax_paid),
        str(result.trades),
        str(result.stops),
        str(result.unsold),
    ]


def _portfolio_table(result: CrossSectionResult, *, before_tax: bool) -> list[str]:
    if not result.portfolios:
        return ["Not run."]
    header = [
        "Portfolio",
        "Liquidation value",
        "Log growth / yr",
        "Max drawdown",
        "Costs",
        "Tax paid",
        "Trades",
        "Stops",
        "Sales not made",
    ]
    if before_tax:
        return _table(header, (_run_row(run, True) for run in result.portfolios))
    return _table(
        (*header, "Turnover / yr"),
        ((*_run_row(run, False), _percent(run.turnover)) for run in result.portfolios),
    )


def _yearly_excess(result: CrossSectionResult) -> list[str]:
    """Each calendar year's summed monthly excess log return of every test (by the year the
    month ends in)."""
    if not result.portfolios:
        return ["Not run."]
    ends = result.dates[1:]
    columns: list[dict[int, float]] = []
    runs = {run.name: run for run in result.portfolios}
    for test in result.tests:
        candidate, against = runs[test.spec.candidate], runs[test.spec.against]
        by_year: dict[int, float] = {}
        for day, c, a in zip(ends, candidate.returns, against.returns, strict=True):
            by_year[day.year] = by_year.get(day.year, 0.0) + c - a
        columns.append(by_year)
    years = sorted({year for column in columns for year in column})
    return _table(
        ("Year", *(test.spec.name for test in result.tests)),
        ((str(year), *(_percent(column.get(year)) for column in columns)) for year in years),
    )


def _lost_lines(result: CrossSectionResult) -> list[str]:
    if not result.lost_by_year:
        return ["No rebalance dates."]
    return _table(
        ("Year", "Members (most on a date)", "Not served", "Largest share on a date"),
        (
            (
                str(year.year),
                str(year.members),
                ", ".join(year.lost) or "none",
                f"{year.share:.1%}",
            )
            for year in result.lost_by_year
        ),
    )


def _deflated_lines(tests: Sequence[SelectionTest], trials: int) -> list[str]:
    return [
        f"- {test.spec.name}: deflated Sharpe n/a"
        if test.deflated is None
        else f"- {test.spec.name}: monthly Sharpe {test.deflated.sharpe:.4f} against "
        f"{test.deflated.benchmark:.4f} expected from the best of {trials} trial(s); "
        f"probability {test.deflated.probability:.3f}"
        for test in tests
    ]


def render(result: CrossSectionResult) -> str:
    """The report as Markdown."""
    spec = result.prereg.spec
    study = spec.study
    tests = spec.tests_in_confirmation
    level = 1 - 2 * result.alpha / tests
    scale = spec.periods_per_year
    stage = "EXPLORATORY — never evidence (DECISIONS D64)" if result.explore else "Confirmation run"
    sample = "development" if result.explore else "confirmation"
    lock = result.lock
    lines = [f"# {spec.title}", "", f"**{stage}.** Study `{spec.id}` ({spec.kind}).", ""]
    lines += [
        "## Inputs",
        "",
        f"- Pre-registration: `{result.prereg.path.name}`, SHA-256 `{result.prereg.sha256}`",
        f"- Lock: merge commit `{lock.merge_commit}`" if lock else "- Lock: none (exploratory)",
        f"- Run at: {utc_stamp(result.run_at)}",
        f"- Inputs SHA-256: `{result.inputs_sha256}`",
        f"- Bootstrap: {spec.bootstrap.n_boot} resamples of {spec.bootstrap.block_months}-month "
        f"blocks of each monthly series, seed {spec.bootstrap.seed}; Newey-West with "
        f"{spec.newey_west_lags} lags beside it",
        f"- alpha {result.alpha} ({result.alpha_source}), {tests} test(s) in the confirmation "
        f"run (Bonferroni), each interval at level {level:.4f}; target power "
        f"{result.target_power} ({result.target_power_source})",
        f"- Sample ({sample}): {result.sample.start.isoformat()} to "
        f"{result.sample.end.isoformat()}, {len(result.dates)} rebalance dates"
        + (
            f" ({result.dates[0].isoformat()} to {result.dates[-1].isoformat()})"
            if result.dates
            else ""
        )
        + f"; initial capital {_money(study.initial_capital)}",
        f"- Members: {len(study.members)} named, {len(study.served())} with a series; vehicle "
        f"{study.vehicle.asset_class.value} on {study.vehicle.venue} in "
        f"{study.vehicle.currency}, yearly cost "
        + (
            "none"
            if study.vehicle.expense_ratio is None
            else _percent(float(study.vehicle.expense_ratio))
        ),
        "",
        "## Outcomes",
        "",
        f"Each test's primary effect is {spec.primary_metric}: the mean of the candidate's "
        "monthly log return minus the other's, both accounts after costs and tax (the last month "
        f"to the liquidation value), times {scale}. The interval is the block bootstrap's at the "
        "confirmation level; the Newey-West estimate and interval are beside it.",
        "",
        *_table(
            (
                "Test",
                "Comparison",
                "Outcome",
                "Estimate / yr",
                "Interval",
                "Newey-West",
                "Theta",
                "Power to detect theta",
                "Detectable at target power",
                "Before data: power / detectable",
                "Estimate before tax",
            ),
            (_test_row(test, scale) for test in result.tests),
        ),
        "",
        *(f"- {test.spec.name}: theta — {test.spec.theta_justification}" for test in result.tests),
        "",
        "## What this test cannot conclude",
        "",
        *(
            f"- {line}"
            for line in dict.fromkeys(
                line for test in result.tests for line in test.decision.cannot_conclude
            )
        ),
        *(f"- {line}" for line in CROSS_SECTION_LIMITS),
        "",
        "## Adequacy checks",
        "",
        *_table(
            ("Check", "Passed", "Detail"),
            ((c.name, "yes" if c.passed else "**no**", c.detail) for c in result.checks),
        ),
        "",
        "Members lost to survivorship (named in the pre-registration, not served by the vendor), "
        "by year:",
        "",
        *_lost_lines(result),
        "",
        "## Portfolios after tax",
        "",
        *_portfolio_table(result, before_tax=False),
        "",
        "## Portfolios before tax",
        "",
        "The same portfolios, trading the same way, with no tax withheld, settled or paid.",
        "",
        *_portfolio_table(result, before_tax=True),
        "",
        "## Excess year by year",
        "",
        "Each test's monthly excess log return after tax, summed over the months that end in the "
        "year:",
        "",
        *_yearly_excess(result),
        "",
        "## Robustness",
        "",
        *(line for test in result.tests for line in _robustness_lines(test)),
        "",
        "Placebo:",
        "",
        *_placebo_lines(result.placebo),
        "",
        "## Selection across trials",
        "",
        f"- Trials of this study, this run's tests included: {result.trials}",
        *_deflated_lines(result.tests, result.trials),
        "",
        "## Data",
        "",
        *_table(
            ("Series", "Vendor", "Point in time", "Downloaded", "SHA-256", "Source"),
            (
                (
                    s.series_id,
                    s.vendor.value,
                    s.point_in_time.value,
                    utc_stamp(s.downloaded_at),
                    f"`{s.content_sha256}`",
                    s.source_url,
                )
                for s in result.series
            ),
        ),
        "",
        f"- Coverage: {_percent(result.coverage)} of served member-dates priced",
    ]
    report = result.survivorship
    lines += [
        f"- Without data: {', '.join(report.without_data) or 'none'}",
        f"- Entering after the start: {', '.join(report.late_entries) or 'none'}",
        f"- Ending before the horizon: {', '.join(report.early_exits) or 'none'}",
        f"- {SURVIVORSHIP_NOTE}",
    ]
    notes = sorted({note for s in result.series for note in s.bias_notes})
    lines += [
        "",
        "## Bias notes",
        "",
        *(f"- {note}" for note in notes),
        *(f"- {note}" for note in CROSS_SECTION_NOTES),
        "",
        "Unverified figures the costs rest on: "
        + (", ".join(f"`{label}`" for label in result.cost_labels) or "none")
        + ".",
        "",
        "## Tax rules applied",
        "",
        *(f"- {rule}" for rule in result.tax_rules),
        "",
        "Not tax advice.",
        "",
    ]
    return "\n".join(lines)


def report_name(result: CrossSectionResult) -> str:
    """``<id>.md`` for the confirmation run (there is one); exploratory runs are stamped."""
    if not result.explore:
        return f"{result.prereg.spec.id}.md"
    stamp = utc_stamp(result.run_at).replace("-", "").replace(":", "")
    return f"{result.prereg.spec.id}-exploration-{stamp}.md"


def write_report(result: CrossSectionResult, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / report_name(result)
    path.write_text(render(result), encoding="utf-8")
    return path
