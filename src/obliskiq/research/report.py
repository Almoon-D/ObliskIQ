"""The report of a study run: what was read and with which hashes and seeds, whether the data
were adequate, each candidate's outcome with its labels and power, the frontier, every portfolio
before and after tax and year by year, the robustness checks, the structure check, the biases,
the tax rules applied, and what the test cannot conclude (DECISIONS D60, D62, D64).

An exploratory run says so at the top and is never evidence: only a confirmation run of a merged,
locked pre-registration counts.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Iterable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

from ..util.timeutil import utc_stamp
from .decide import Decision
from .simulate import SimulationResult
from .structure import StructureArm, StructureResult
from .study import SIMULATION_NOTES, CandidateTest, StrategyStats, StudyResult

#: The note a report carries about the universe it measured (D62's "survivorship measured").
SURVIVORSHIP_NOTE: Final = (
    "A universe listed today cannot show members that disappeared before it was listed; the "
    "report measures what the list can show."
)
#: What a portfolio comparison cannot conclude whatever its outcome.
COMPARISON_LIMITS: Final = (
    "That a portfolio will keep its place after the sample: past growth after tax is no promise "
    "of future superiority.",
    "How a portfolio behaves in a crisis unlike the episodes in the sample; the bootstrap "
    "reshuffles the sample's own months and cannot create a new kind of crisis.",
    "Anything about a period, market or cost regime outside this sample.",
)

#: Decimals of a reported statistic, and of a probability or a multiple.
DIGITS: Final = 4
SHORT_DIGITS: Final = 3


def _number(value: float | None, digits: int = DIGITS) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _grid(value: Decimal) -> str:
    """A pre-registered fraction (a limit, a probability, an aspiration) as a percent, with
    the digits it was written with (2.5 % stays 2.5 %)."""
    return f"{value.scaleb(2).normalize():f}%"


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _table(header: Iterable[str], rows: Iterable[Iterable[str]]) -> list[str]:
    head = list(header)
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _outcome(decision: Decision) -> str:
    labels = ", ".join(label.value for label in decision.labels)
    return f"**{decision.outcome.value}**" + (f" ({labels})" if labels else "")


def _interval(decision: Decision) -> str:
    if decision.interval is None:
        return "n/a"
    low, high = decision.interval
    return f"[{_percent(low)}, {_percent(high)}]"


def _run_row(stats: StrategyStats, run: SimulationResult | None) -> list[str]:
    return [
        stats.name,
        _money(None if run is None else run.liquidation_value),
        _percent(None if run is None else run.log_growth),
        _percent(None if run is None else run.max_drawdown),
        _money(None if run is None else run.costs),
        _money(None if run is None else run.tax_paid),
        "n/a" if run is None else str(run.trades),
    ]


def yearly_returns(run: SimulationResult) -> dict[int, float]:
    """Each calendar year's change of mark-to-market value; the first year from the initial
    capital, the last to the horizon's value before the terminal sale."""
    year_end: dict[int, float] = {}
    for day, value in zip(run.dates, run.nav, strict=True):
        year_end[day.year] = value
    out: dict[int, float] = {}
    previous = float(run.initial)
    for year in sorted(year_end):
        out[year] = year_end[year] / previous - 1
        previous = year_end[year]
    return out


def _aspiration_distance(stats: StrategyStats, aspiration: Decimal) -> str:
    if stats.median_log_growth is None:
        return "n/a"
    return _percent(stats.median_log_growth - math.log1p(float(aspiration)))


def _unsold(stats: StrategyStats) -> str:
    historical = "n/a" if stats.historical is None else str(stats.historical.unsold)
    paths = "n/a" if stats.unsold_paths is None else str(stats.unsold_paths)
    return f"{stats.name} {historical} on the historical path, on {paths} bootstrap paths"


def _frontier_lines(result: StudyResult) -> list[str]:
    if result.prereg.spec.frontier is None:
        return ["No frontier was pre-registered."]
    if not result.frontier:
        return ["Not computed: the bootstrap did not run (see the adequacy checks)."]
    tests = {test.name: test for test in result.tests}

    def against(name: str | None) -> str:
        if name is None:
            return "none respects this point"
        if name == result.reference.name:
            return "the reference itself"
        return _outcome(tests[name].decision)

    rows = (
        (
            _grid(point.limit),
            _grid(point.probability),
            point.chosen or "none",
            against(point.chosen),
            point.best or "none",
            ", ".join(point.indistinguishable) or "none",
            str(len(point.eligible)),
        )
        for point in result.frontier
    )
    return [
        "For each drawdown limit and largest acceptable probability of breaching it: the "
        "portfolio with the highest median after-tax log growth among those that respect it "
        "(best), and the one it resolves to (chosen) — the simplest, then the lowest-turnover, "
        "of the best and the portfolios whose growth the bootstrap cannot tell apart from it "
        "(their paired interval at the confirmation level holds zero). Choosing a point is the "
        "owner's authorisation (DECISIONS D60), not this report's.",
        "",
        *_table(
            (
                "Drawdown limit",
                "Breach probability at most",
                "Chosen",
                "Its outcome against the reference",
                "Best by median growth",
                "Cannot be told apart from the best",
                "Respecting it",
            ),
            rows,
        ),
    ]


def _structure_arm(name: str, arm: StructureArm) -> list[str]:
    breach = ", ".join(f"{_grid(limit)}: {p:.1%}" for limit, p in arm.breach)
    return [name, _percent(arm.log_growth), _percent(arm.max_drawdown), breach or "n/a"]


def _structure_lines(structure: StructureResult | None) -> list[str]:
    if structure is None:
        return ["No structure check was pre-registered."]
    spec = structure.spec
    lines = [
        "Descriptive, before tax and never an outcome: the tilt with this study's constants on "
        f"{' + '.join(spec.market_series)} against {spec.cash_series}, monthly, "
        f"{float(spec.cost_per_leg):.2%} a leg, against holding the market.",
        "",
        *_table(
            ("Check", "Passed", "Detail"),
            ((c.name, "yes" if c.passed else "**no**", c.detail) for c in structure.checks),
        ),
        "",
    ]
    if structure.overlay is None or structure.held is None:
        return [*lines, "Not run: a check failed."]
    first = "n/a" if structure.first is None else structure.first.isoformat()
    last = "n/a" if structure.last is None else structure.last.isoformat()
    interval = structure.interval
    lines += [
        f"- From {first} to {last}; the tilt held less than the whole market in "
        f"{structure.months_reduced} months.",
        "",
        *_table(
            ("Arm", "Log growth / yr", "Max drawdown", "Breach probability by limit"),
            (
                _structure_arm("tilt", structure.overlay),
                _structure_arm("held", structure.held),
            ),
        ),
        "",
        "- Difference in log growth, tilt minus held: "
        + _percent(structure.overlay.log_growth - structure.held.log_growth)
        + (
            ""
            if interval is None
            else f", block-bootstrap interval of the realised returns "
            f"[{_percent(interval[0])}, {_percent(interval[1])}]"
        ),
        "",
        *_table(
            ("Subperiod", "Growth difference / yr", "Max drawdown difference"),
            (
                (
                    f"{p.start.isoformat()} to {p.end.isoformat()}",
                    _percent(p.growth_difference),
                    _percent(p.drawdown_difference),
                )
                for p in structure.subperiods
            ),
        ),
    ]
    return lines


def _robustness_lines(test: CandidateTest) -> list[str]:
    robustness = test.robustness
    if robustness is None:
        return [f"- {test.name}: not run (the outcome is not evaluable)."]
    parts = ", ".join(
        f"{p.start.isoformat()} to {p.end.isoformat()} {_percent(p.estimate)}"
        + ("" if p.sign_holds is None else (" sign holds" if p.sign_holds else " **sign fails**"))
        for p in robustness.subperiods
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
        f"- {test.name}: subperiods {parts}; break-even cost multiplier {found} "
        f"(declared cost uncertainty {robustness.cost_uncertainty}: {exceeded})."
    ]


def _years_table(portfolios: Sequence[StrategyStats], before_tax: bool) -> list[str]:
    runs = [(s.name, s.pre_tax if before_tax else s.historical) for s in portfolios]
    yearly = {name: yearly_returns(run) for name, run in runs if run is not None}
    years = sorted({year for table in yearly.values() for year in table})
    if not years:
        return ["Not run."]
    return _table(
        ("Year", *(name for name, _ in runs)),
        (
            (
                str(year),
                *(_percent(yearly.get(name, {}).get(year)) for name, _ in runs),
            )
            for year in years
        ),
    )


def _taxes_table(portfolios: Sequence[StrategyStats]) -> list[str]:
    paid: dict[str, dict[int, Decimal]] = {}
    for stats in portfolios:
        run = stats.historical
        if run is None:
            continue
        by_year: dict[int, Decimal] = {}
        for payment in run.taxes:
            by_year[payment.paid_on.year] = by_year.get(payment.paid_on.year, Decimal(0)) + (
                payment.amount
            )
        paid[stats.name] = by_year
    years = sorted({year for table in paid.values() for year in table})
    if not years:
        return ["No tax was paid."]
    return _table(
        ("Paid in", *paid),
        ((str(y), *(_money(paid[n].get(y)) for n in paid)) for y in years),
    )


def _weights_table(portfolios: Sequence[StrategyStats]) -> list[str]:
    weighted = [s for s in portfolios if s.static_weights is not None]
    if not weighted:
        return ["No core strategy."]
    roles = list(dict.fromkeys(role for s in weighted for role in s.static_weights or {}))
    return _table(
        ("Role", *(s.name for s in weighted)),
        (
            (
                role,
                *(
                    _percent(
                        None if (w := (s.static_weights or {}).get(role)) is None else float(w)
                    )
                    for s in weighted
                ),
            )
            for role in roles
        ),
    )


def render(result: StudyResult) -> str:
    """The report as Markdown."""
    spec = result.prereg.spec
    study = spec.study
    tests = spec.tests_in_confirmation
    level = 1 - 2 * result.alpha / tests
    stage = "EXPLORATORY — never evidence (DECISIONS D64)" if result.explore else "Confirmation run"
    lock = result.lock
    aspirations = spec.aspiration or []
    lines = [f"# {spec.title}", "", f"**{stage}.** Study `{spec.id}` ({spec.kind}).", ""]
    lines += [
        "## Inputs",
        "",
        f"- Pre-registration: `{result.prereg.path.name}`, SHA-256 `{result.prereg.sha256}`",
        f"- Lock: merge commit `{lock.merge_commit}`" if lock else "- Lock: none (exploratory)",
        f"- Run at: {utc_stamp(result.run_at)}",
        f"- Inputs SHA-256: `{result.inputs_sha256}`",
        f"- Bootstrap: {spec.bootstrap.n_boot} paths of {spec.bootstrap.block_months}-month "
        f"blocks, seed {spec.bootstrap.seed}",
        f"- alpha {result.alpha} ({result.alpha_source}), {tests} test(s) in the confirmation "
        f"run (Bonferroni), each interval at level {level:.4f}; target power "
        f"{result.target_power} ({result.target_power_source})",
        f"- Horizon: {study.start.isoformat()} to {study.end.isoformat()}, "
        f"{len(result.dates)} rebalance dates"
        + (
            f" ({result.dates[0].isoformat()} to {result.dates[-1].isoformat()})"
            if result.dates
            else ""
        )
        + f"; initial capital {_money(study.initial_capital)}",
        "",
        "## Outcomes",
        "",
        f"Each candidate's primary effect is {spec.primary_metric}: its annualised log growth of "
        f"liquidation value after costs and tax minus {study.reference.name}'s. theta "
        f"{_percent(float(spec.theta))}: {spec.theta_justification}",
        "",
        *_table(
            (
                "Candidate",
                "Outcome",
                "Estimate / yr",
                "Interval",
                "Power to detect theta",
                "Detectable at target power",
                "Before data: power / detectable",
            ),
            (
                (
                    test.name,
                    _outcome(test.decision),
                    _percent(test.estimate),
                    _interval(test.decision),
                    _number(test.decision.power, SHORT_DIGITS),
                    _percent(test.detectable),
                    f"{test.declared_power:.3f} / {_percent(test.declared_detectable)}",
                )
                for test in result.tests
            ),
        ),
        "",
        "## The frontier",
        "",
        *_frontier_lines(result),
        "",
        "## What this test cannot conclude",
        "",
        *(
            f"- {line}"
            for line in dict.fromkeys(
                line for test in result.tests for line in test.decision.cannot_conclude
            )
        ),
        *(f"- {line}" for line in COMPARISON_LIMITS),
        "",
        "## Adequacy checks",
        "",
        *_table(
            ("Check", "Passed", "Detail"),
            ((c.name, "yes" if c.passed else "**no**", c.detail) for c in result.checks),
        ),
        "",
        "## Portfolios after tax",
        "",
        *_table(
            (
                "Portfolio",
                "Liquidation value",
                "Log growth / yr",
                "Max drawdown",
                "Costs",
                "Tax paid",
                "Trades",
                "Turnover / yr",
                "Median bootstrap growth",
                "P5 terminal wealth multiple",
                *(f"Median minus ln(1 + {_grid(a)})" for a in aspirations),
            ),
            (
                (
                    *_run_row(stats, stats.historical),
                    _percent(stats.turnover),
                    _percent(stats.median_log_growth),
                    _number(stats.terminal_wealth_p5, SHORT_DIGITS),
                    *(_aspiration_distance(stats, a) for a in aspirations),
                )
                for stats in result.portfolios
            ),
        ),
        "",
    ]
    if aspirations:
        lines += [
            "The owner's stated aspiration (DECISIONS D60) is reported as a distance from each "
            "portfolio's median after-tax log growth, never as a gate.",
            "",
        ]
    lines += [
        "Sales not made because their costs would exceed what they raise (the units stay held, "
        "and count as nothing at the horizon): "
        + "; ".join(_unsold(stats) for stats in result.portfolios)
        + ".",
        "",
    ]
    limits = spec.drawdown_limits
    lines += [
        "Probability that a bootstrap path's mark-to-market drawdown reaches each limit:",
        "",
        *_table(
            ("Portfolio", *(_grid(limit) for limit in limits)),
            (
                (s.name, *([f"{p:.1%}" for _, p in s.breach] or ["n/a"] * len(limits)))
                for s in result.portfolios
            ),
        ),
        "",
        "## Portfolios before tax",
        "",
        "The same strategies, trading the same way, with no tax withheld, settled or paid.",
        "",
        *_table(
            (
                "Portfolio",
                "Liquidation value",
                "Log growth / yr",
                "Max drawdown",
                "Costs",
                "Tax paid",
                "Trades",
            ),
            (_run_row(stats, stats.pre_tax) for stats in result.portfolios),
        ),
        "",
        "## Static weights",
        "",
        "Each core strategy's weights by role before the tilt.",
        "",
        *_weights_table(result.portfolios),
        "",
        "## Year by year",
        "",
        "Mark-to-market value, after tax paid as it arose (the last year ends at the horizon "
        "before the terminal sale and its tax):",
        "",
        *_years_table(result.portfolios, before_tax=False),
        "",
        "Before tax:",
        "",
        *_years_table(result.portfolios, before_tax=True),
        "",
        "Tax paid, by the year it was paid (the horizon's includes the terminal sale's):",
        "",
        *_taxes_table(result.portfolios),
        "",
        "## Robustness",
        "",
        *(line for test in result.tests for line in _robustness_lines(test)),
        "",
        f"- Placebo: {result.tests[0].robustness.placebo}"
        if result.tests and result.tests[0].robustness is not None
        else "- Placebo: not run",
        "",
        "## Selection across trials",
        "",
        f"- Trials of this study, this run's candidates included: {result.trials}",
        *(
            f"- {test.name}: deflated Sharpe n/a"
            if test.deflated is None
            else f"- {test.name}: monthly Sharpe {test.deflated.sharpe:.4f} against "
            f"{test.deflated.benchmark:.4f} expected from the best of {result.trials} trial(s); "
            f"probability {test.deflated.probability:.3f}"
            for test in result.tests
        ),
        "",
        "## Structure check",
        "",
        *_structure_lines(result.structure),
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
        f"- Coverage: {_percent(result.coverage)} of rebalance dates fully priced",
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
        *(f"- {note}" for note in SIMULATION_NOTES),
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


def report_name(result: StudyResult) -> str:
    """``<id>.md`` for the confirmation run (there is one); exploratory runs are stamped."""
    if not result.explore:
        return f"{result.prereg.spec.id}.md"
    stamp = utc_stamp(result.run_at).replace("-", "").replace(":", "")
    return f"{result.prereg.spec.id}-exploration-{stamp}.md"


def write_report(result: StudyResult, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / report_name(result)
    path.write_text(render(result), encoding="utf-8")
    return path
